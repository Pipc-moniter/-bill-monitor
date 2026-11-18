#!/usr/bin/env python3
"""국회 의원발의 법률안을 수집하고 개인정보 관련 여부를 AI로 분류해 data/bills.json을 만든다.

GitHub Actions에서 매일 실행된다. 필요한 환경변수:
  ASSEMBLY_API_KEY   열린국회정보 오픈API 인증키
  ANTHROPIC_API_KEY  Anthropic API 키
외부 패키지 없이 표준 라이브러리만 사용한다.
"""
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KST = timezone(timedelta(hours=9))

SETTINGS = json.load(open(os.path.join(ROOT, "config/settings.json"), encoding="utf-8"))
CRITERIA = open(os.path.join(ROOT, "config/criteria.md"), encoding="utf-8").read()
OVERRIDES = json.load(open(os.path.join(ROOT, "config/overrides.json"), encoding="utf-8"))
OUT_PATH = os.path.join(ROOT, "data/bills.json")
CACHE_PATH = os.path.join(ROOT, "data/classify_cache.json")

# 열린국회정보 '국회의원 발의법률안' API
LIST_API = "https://open.assembly.go.kr/portal/openapi/nzmimeepazxkubdpn"
SUMMARY_API = "https://open.assembly.go.kr/portal/openapi/BPMBILLSUMMARY"
# 한 번 실행에서 AI 분류에 쓸 최대 시간(분). 넘으면 그때까지 결과를 저장하고 마친다.
TIME_BUDGET_MIN = float(SETTINGS.get("time_budget_minutes", 150))
DETAIL_URL = "https://likms.assembly.go.kr/bill/billDetail.do?billId={}"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# 판단 기준이나 모델이 바뀌면 캐시가 무효화되어 전체 재분류된다.
CRITERIA_HASH = hashlib.sha256((CRITERIA + SETTINGS["model"]).encode()).hexdigest()[:12]


def http_get(url, timeout=30, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (bill-monitor)"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            if i == retries - 1:
                raise
            print(f"  재시도 {i + 1}: {e}")
            time.sleep(2 * (i + 1))


def fetch_bill_list_once(api_key, size):
    rows, page, total = [], 1, 0
    while True:
        q = urllib.parse.urlencode({
            "KEY": api_key, "Type": "json", "pIndex": page, "pSize": size,
            "AGE": SETTINGS["assembly_age"],
        })
        data = json.loads(http_get(f"{LIST_API}?{q}"))
        block = data.get("nzmimeepazxkubdpn")
        if not block:
            # 데이터 없음(INFO-200) 또는 오류
            result = data.get("RESULT", {})
            if page == 1:
                raise RuntimeError(f"국회 API 응답 오류: {result}")
            break
        total = block[0]["head"][0]["list_total_count"]
        page_rows = block[1].get("row", [])
        rows.extend(page_rows)
        if len(rows) >= total or not page_rows:
            break
        page += 1
        time.sleep(0.3)
    return rows, total


def fetch_bill_list(api_key):
    """해당 대수 의원발의 법률안 전체를 받는다.

    국회 API는 페이지를 넘길 때 순서가 흔들려 같은 법안이 중복되고 일부가 빠질 수 있다.
    그래서 의안ID 기준으로 중복을 없애고, 전체 건수가 다 모일 때까지 여러 번 받아 합친다.
    """
    unique, total = {}, 0
    for attempt, size in enumerate([1000, 500, 300, 200, 100], start=1):
        rows, total = fetch_bill_list_once(api_key, size)
        before = len(unique)
        for r in rows:
            unique[r["BILL_ID"]] = r
        dup = len(rows) - len({r["BILL_ID"] for r in rows})
        print(f"  {attempt}회차(페이지 {size}건): 받은 {len(rows)}건, 중복 {dup}건, "
              f"새로 추가 {len(unique) - before}건 → 누적 {len(unique)}/{total}")
        if len(unique) >= total:
            break
    if len(unique) < total:
        print(f"  ⚠ 전체 {total}건 중 {total - len(unique)}건은 받지 못했습니다(다음 실행에서 다시 시도).")
    return list(unique.values())


def fetch_summary(api_key, bill_no):
    """열린국회정보 '법률안 제안이유 및 주요내용' API(BPMBILLSUMMARY)에서 본문을 가져온다."""
    q = urllib.parse.urlencode({"KEY": api_key, "Type": "json", "pIndex": 1, "pSize": 5, "BILL_NO": bill_no})
    try:
        data = json.loads(http_get(f"{SUMMARY_API}?{q}", timeout=15, retries=2))
    except Exception as e:
        print(f"  요약 수집 실패 {bill_no}: {e}")
        return ""
    block = data.get("BPMBILLSUMMARY")
    if not block or len(block) < 2:
        return ""
    for row in block[1].get("row", []):
        text = row.get("SUMMARY") or row.get("BILL_SUMMARY") or ""
        if text:
            text = html.unescape(re.sub(r"<[^>]+>", "\n", text))
            return re.sub(r"[ \t]+", " ", text).strip()[:6000]
    return ""


class RateLimiter:
    """여러 작업자가 동시에 AI를 호출해도 분당 요청 수를 넘지 않게 간격을 둔다.
    한도 초과(429)를 받으면 간격을 늘리고, 잘 되면 조금씩 줄인다."""

    def __init__(self, rpm):
        self.interval = 60.0 / max(1, rpm)
        self.min_interval = self.interval
        self.next_at = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.time()
            at = max(now, self.next_at)
            self.next_at = at + self.interval
        if at > now:
            time.sleep(at - now)

    def slow_down(self, seconds):
        with self.lock:
            self.interval = min(self.interval * 1.5, 10.0)
            self.next_at = max(self.next_at, time.time() + seconds)

    def speed_up(self):
        with self.lock:
            self.interval = max(self.min_interval, self.interval * 0.98)


LIMITER = RateLimiter(int(SETTINGS.get("anthropic_rpm", 40)))


def classify(api_key, bill, summary):
    prompt = f"""당신은 개인정보보호위원회의 주간 입법동향 모니터링 담당자입니다.
아래 기준에 따라 법안을 ①, ②, ③, 무관 중 하나로 분류하세요.
이 분류는 담당자가 최종 검토하기 전 1차 선별이므로, 놓치는 것보다 넓게 고르는 편이 낫습니다.

<기준>
{CRITERIA}
</기준>

<법안>
의안명: {bill.get('BILL_NAME', '')}
제안이유 및 주요내용:
{summary or '(본문 없음: 의안명만으로 판단)'}
</법안>

다음 JSON 한 줄로만 답하세요. tier는 "1", "2", "3", "0"(무관) 중 하나입니다.
{{"tier": "1", "reason": "판단 근거 한 문장"}}"""
    body = json.dumps({
        "model": SETTINGS["model"],
        "max_tokens": 250,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    if workspace:
        headers["anthropic-workspace-id"] = workspace
    for i in range(10):
        try:
            LIMITER.wait()
            req = urllib.request.Request(ANTHROPIC_URL, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                res = json.loads(r.read())
            LIMITER.speed_up()
            text = "".join(c.get("text", "") for c in res.get("content", []))
            m = re.search(r"\{.*\}", text, re.DOTALL)
            parsed = json.loads(m.group(0))
            tier = str(parsed.get("tier", "0")).strip()
            tier = tier if tier in ("1", "2", "3") else "0"
            return {"tier": tier, "relevant": tier != "0", "reason": str(parsed.get("reason", ""))[:300]}
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 529):
                try:
                    wait = float(e.headers.get("retry-after") or 0)
                except ValueError:
                    wait = 0
                LIMITER.slow_down(max(wait, 5 * (i + 1)))
                continue
            detail = e.read().decode("utf-8", errors="replace")[:500]
            hint = {
                401: "Anthropic API 키가 올바르지 않습니다. 키를 새로 만들어 ANTHROPIC_API_KEY에 다시 등록하세요. "
                     "회사·기관 계정 키라면 ANTHROPIC_WORKSPACE_ID도 등록해야 할 수 있습니다.",
                400: "요청이 거절되었습니다. 크레딧 잔액(Billing)을 확인하세요.",
                403: "이 키로는 API를 쓸 권한이 없습니다. 콘솔에서 키 권한을 확인하세요.",
                404: "모델 이름이 잘못되었습니다. config/settings.json의 model 값을 확인하세요.",
            }.get(e.code, "")
            sys.exit(f"\n[오류] Anthropic API {e.code}\n{hint}\n응답 내용: {detail}")
        except (AttributeError, json.JSONDecodeError):
            time.sleep(1)
    return None


def load_json(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except FileNotFoundError:
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def main():
    sys.stdout.reconfigure(line_buffering=True)  # 진행 상황이 로그에 바로 보이도록
    assembly_key = os.environ.get("ASSEMBLY_API_KEY", "").strip()
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not assembly_key or not anthropic_key:
        sys.exit("ASSEMBLY_API_KEY, ANTHROPIC_API_KEY 환경변수가 필요합니다.")

    print("1) 법안 목록 수집")
    rows = fetch_bill_list(assembly_key)
    # 수집 누락 점검: 과거 주간 보고서에 실린 법안이 목록에 있는지 확인
    nos = {str(r.get("BILL_NO", "")) for r in rows}
    for no in SETTINGS.get("check_bill_nos", []):
        print(f"  점검 {no}: {'있음' if str(no) in nos else '없음(수집 누락)'}")
    rows = [r for r in rows if (r.get("PROPOSE_DT") or "") >= SETTINGS["start_date"]]
    print(f"  기간 내 법안 {len(rows)}건")

    cache = load_json(CACHE_PATH, {})
    todo = [r for r in rows if cache.get(r["BILL_ID"], {}).get("h") != CRITERIA_HASH]
    todo.sort(key=lambda r: r.get("PROPOSE_DT", ""), reverse=True)  # 최신부터
    limit = int(SETTINGS["max_classify_per_run"])
    print(f"2) AI 분류 대상 {len(todo)}건 (이번 실행 최대 {limit}건)")

    def work(r):
        summary = fetch_summary(assembly_key, r.get("BILL_NO", ""))
        return r, summary, classify(anthropic_key, r, summary)

    # 여러 건을 동시에 처리해 속도를 높인다.
    workers = int(SETTINGS.get("workers", 8))
    queue = todo[:limit]
    done, no_summary, failed, started, last_report = 0, 0, 0, time.time(), 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i in range(0, len(queue), workers * 3):
            if (time.time() - started) / 60 > TIME_BUDGET_MIN:
                print(f"  시간 한도({TIME_BUDGET_MIN:.0f}분) 도달 — 나머지는 다음 실행에서 이어서 분류")
                break
            for r, summary, result in pool.map(work, queue[i:i + workers * 3]):
                if not summary:
                    no_summary += 1
                if result is None:
                    failed += 1  # 다음 실행에서 다시 시도
                    continue
                cache[r["BILL_ID"]] = {"h": CRITERIA_HASH, "has_summary": bool(summary), **result}
                done += 1
            if done - last_report >= 50:
                last_report = done
                save_json(CACHE_PATH, cache)  # 중간 저장
                mins = (time.time() - started) / 60
                print(f"  분류 {done}건 완료 ({mins:.0f}분 경과, 본문 없음 {no_summary}건, 실패 {failed}건)")
    save_json(CACHE_PATH, cache)
    print(f"  이번 실행 분류 {done}건 (본문 없이 의안명으로만 판단 {no_summary}건, 실패 {failed}건은 다음 실행에서 재시도)")

    print("3) 결과 생성")
    include, exclude = OVERRIDES.get("include", {}), OVERRIDES.get("exclude", {})
    bills, pending = [], 0
    for r in rows:
        no = str(r.get("BILL_NO", ""))
        c = cache.get(r["BILL_ID"])
        if c is None or c.get("h") != CRITERIA_HASH:
            pending += 1
        if no in exclude:
            continue
        if no in include:
            reason, source, tier = include[no], "manual", "M"
        elif c and c.get("h") == CRITERIA_HASH and c.get("relevant"):
            reason, source, tier = c.get("reason", ""), "ai", c.get("tier", "2")
        else:
            continue
        bills.append({
            "no": no,
            "name": r.get("BILL_NAME", ""),
            "proposer": r.get("PROPOSER", ""),
            "rep": r.get("RST_PROPOSER", ""),
            "date": r.get("PROPOSE_DT", ""),
            "committee": r.get("COMMITTEE", "") or "",
            "status": r.get("PROC_RESULT") or "계류",
            "link": r.get("DETAIL_LINK") or DETAIL_URL.format(r["BILL_ID"]),
            "reason": reason,
            "tier": tier,
            "source": source,
            "title_only": bool(c and not c.get("has_summary")),
        })
    bills.sort(key=lambda b: (b["date"], b["no"]), reverse=True)

    # 모니터링 기간: 최신 법안부터 거슬러 올라가며 '빠짐없이' 분류를 마친 구간만 기간으로 표시한다.
    # 중간에 분류 안 된 법안이 있는 날짜는 기간에 넣지 않는다.
    by_date = {}
    for r in rows:
        d = r.get("PROPOSE_DT") or ""
        if d:
            ok = cache.get(r["BILL_ID"], {}).get("h") == CRITERIA_HASH
            by_date.setdefault(d, []).append(ok)
    checked, period_from, period_to = [], "", ""
    for d in sorted(by_date, reverse=True):
        if not all(by_date[d]):
            break
        checked.extend([d] * len(by_date[d]))
        period_to = period_to or d
        period_from = d
    # 화면의 관련 법안 수와 검토 건수가 같은 기간을 가리키도록, 기간 밖의 AI 분류 결과는 빼고 보여준다.
    bills = [b for b in bills if b["source"] == "manual" or (period_from and b["date"] >= period_from)]

    save_json(OUT_PATH, {
        "site_title": SETTINGS["site_title"],
        "updated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "start_date": SETTINGS["start_date"],
        "period_from": period_from,
        "period_to": period_to,
        "checked": len(checked),
        "scanned": len(rows),
        "pending": pending,
        "bills": bills,
    })
    # 검토한 법안 전체 목록(무관 포함) — 주간 보고서와 대조·검증용
    reviewed = []
    for r in rows:
        c = cache.get(r["BILL_ID"])
        if c and c.get("h") == CRITERIA_HASH:
            reviewed.append([str(r.get("BILL_NO", "")), r.get("PROPOSE_DT", ""), r.get("BILL_NAME", ""),
                             c.get("tier", "0"), c.get("reason", "")])
    reviewed.sort(key=lambda x: (x[1], x[0]), reverse=True)
    save_json(os.path.join(ROOT, "data/reviewed.json"), reviewed)
    print(f"  관련 법안 {len(bills)}건 / 검토 {len(checked)}건 / 분류 대기 {pending}건")


if __name__ == "__main__":
    main()
