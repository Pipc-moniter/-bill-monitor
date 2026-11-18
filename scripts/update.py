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


def build_prompt(bill, summary):
    return f"""당신은 개인정보보호위원회의 주간 입법동향 모니터링 담당자입니다.
아래 기준에 따라 법안을 ①, ②, ③, 무관 중 하나로 분류하세요.
주간 입법동향 보고서에 실을 법안만 고르세요. 조문에 근거가 있어야 하며, "수반될 수 있다"는 추측만으로는 고르지 마세요.

<기준>
{CRITERIA}
</기준>

<법안>
의안명: {bill.get('BILL_NAME', '')}
제안이유 및 주요내용:
{summary or '(본문 없음: 의안명만으로 판단)'}
</법안>

다른 설명 없이 다음 JSON 한 줄로만 답하세요. tier는 "1", "2", "3", "0"(무관) 중 하나이고,
reason은 100자 이내 한 문장이며 큰따옴표(")를 쓰지 마세요.
{{"tier": "1", "reason": "판단 근거 한 문장"}}"""


def parse_answer(text):
    """AI 답변에서 구분(tier)과 근거를 꺼낸다. JSON이 잘리거나 따옴표가 섞여도 최대한 읽어낸다."""
    text = text or ""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            parsed = json.loads(m.group(0))
            tier = str(parsed.get("tier", "")).strip()
            if tier in ("0", "1", "2", "3"):
                return {"tier": tier, "relevant": tier != "0", "reason": str(parsed.get("reason", ""))[:300]}
        except json.JSONDecodeError:
            pass
    # JSON으로 못 읽으면 글자 패턴으로 찾는다
    t = re.search(r'"?tier"?\s*[:=]\s*"?\s*([0-3①②③])', text)
    if not t:
        return None
    tier = {"①": "1", "②": "2", "③": "3"}.get(t.group(1), t.group(1))
    r = re.search(r'"?reason"?\s*[:=]\s*"?(.*)', text, re.DOTALL)
    reason = r.group(1) if r else ""
    reason = re.sub(r'["}\s`]+$', "", reason.replace("\n", " ")).strip()
    return {"tier": tier, "relevant": tier != "0", "reason": reason[:300]}


def anthropic_call(api_key, method, path, body=None, raw=False):
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    if workspace:
        headers["anthropic-workspace-id"] = workspace
    url = path if path.startswith("http") else "https://api.anthropic.com" + path
    data = json.dumps(body).encode() if body is not None else None
    for i in range(6):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=300) as r:
                out = r.read()
            return out.decode("utf-8") if raw else json.loads(out)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 529) and i < 5:
                time.sleep(10 * (i + 1))
                continue
            detail = e.read().decode("utf-8", errors="replace")[:500]
            hint = {
                401: "Anthropic API 키가 올바르지 않습니다. ANTHROPIC_API_KEY를 다시 등록하세요.",
                400: "요청이 거절되었습니다. 크레딧 잔액(Billing)을 확인하세요.",
                403: "이 키로는 API를 쓸 권한이 없습니다.",
                404: "모델 이름 또는 주소가 잘못되었습니다. config/settings.json의 model 값을 확인하세요.",
            }.get(e.code, "")
            sys.exit(f"\n[오류] Anthropic API {e.code}\n{hint}\n응답 내용: {detail}")
        except (urllib.error.URLError, TimeoutError) as e:
            if i < 5:
                time.sleep(10 * (i + 1))
                continue
            raise


PENDING_PATH = os.path.join(ROOT, "data/pending_batch.json")


def collect_batch(api_key, batch_id, cache, meta, deadline):
    """일괄 처리가 끝날 때까지 기다렸다가 결과를 캐시에 넣는다. 시간 안에 안 끝나면 False."""
    last = ""
    while True:
        b = anthropic_call(api_key, "GET", f"/v1/messages/batches/{batch_id}")
        rc = b.get("request_counts", {})
        line = (f"  일괄 처리 상태: {b.get('processing_status')} — 완료 {rc.get('succeeded', 0)}, "
                f"처리 중 {rc.get('processing', 0)}, 오류 {rc.get('errored', 0)}")
        if line != last:
            print(line + f" ({datetime.now(KST).strftime('%H:%M')})")
            last = line
        if b.get("processing_status") == "ended":
            break
        if time.time() > deadline:
            print("  시간 한도 도달 — 일괄 처리는 계속 진행되며, 결과는 다음 실행에서 받아옵니다.")
            return False
        time.sleep(30)
    text = anthropic_call(api_key, "GET", b["results_url"], raw=True)
    ok = bad = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        res = item.get("result", {})
        parsed = None
        if res.get("type") == "succeeded":
            msg = res.get("message", {})
            parsed = parse_answer("".join(c.get("text", "") for c in msg.get("content", [])))
        info = meta.get(item.get("custom_id"), {})
        if not parsed and bad < 3:
            if res.get("type") == "succeeded":
                msg = res.get("message", {})
                raw = "".join(c.get("text", "") for c in msg.get("content", []))
                print(f"    [실패 예시] 중단사유={msg.get('stop_reason')} 답변={raw[:300]!r}")
            else:
                print(f"    [실패 예시] {json.dumps(res, ensure_ascii=False)[:300]}")
        if parsed and info.get("h") == CRITERIA_HASH:
            cache[info["bill_id"]] = {"h": CRITERIA_HASH, "has_summary": info.get("has_summary", False), **parsed}
            ok += 1
        else:
            bad += 1
    print(f"  결과 반영 {ok}건, 실패 {bad}건(다음 실행에서 재시도)")
    return True


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
주간 입법동향 보고서에 실을 법안만 고르세요. 조문에 근거가 있어야 하며, "수반될 수 있다"는 추측만으로는 고르지 마세요.

<기준>
{CRITERIA}
</기준>

<법안>
의안명: {bill.get('BILL_NAME', '')}
제안이유 및 주요내용:
{summary or '(본문 없음: 의안명만으로 판단)'}
</법안>

다른 설명 없이 다음 JSON 한 줄로만 답하세요. tier는 "1", "2", "3", "0"(무관) 중 하나이고,
reason은 100자 이내 한 문장이며 큰따옴표(")를 쓰지 마세요.
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

    deadline = time.time() + TIME_BUDGET_MIN * 60

    # 지난 실행에서 맡겨 둔 일괄 처리가 있으면 그 결과부터 받아온다.
    pending = load_json(PENDING_PATH, None)
    if pending:
        print(f"  지난 실행의 일괄 처리({len(pending['meta'])}건) 결과 확인")
        if collect_batch(anthropic_key, pending["id"], cache, pending["meta"], deadline):
            os.remove(PENDING_PATH)
            save_json(CACHE_PATH, cache)
            todo = [r for r in todo if cache.get(r["BILL_ID"], {}).get("h") != CRITERIA_HASH]
        else:
            todo = []  # 아직 진행 중이면 새로 맡기지 않는다

    queue = todo[:limit]
    for round_no in (1, 2):  # 실패한 건은 같은 실행 안에서 한 번 더 맡긴다
        queue = [r for r in queue if cache.get(r["BILL_ID"], {}).get("h") != CRITERIA_HASH]
        if not queue or time.time() > deadline:
            break
        if round_no == 2:
            print(f"  실패한 {len(queue)}건 다시 요청")
        # 2-1) 제안이유를 국회 API에서 동시에 받아온다.
        print(f"  제안이유 수집 {len(queue)}건")
        summaries, no_summary = {}, 0
        with ThreadPoolExecutor(max_workers=int(SETTINGS.get("workers", 8))) as pool:
            for n, (r, sm) in enumerate(pool.map(lambda r: (r, fetch_summary(assembly_key, r.get("BILL_NO", ""))), queue), 1):
                summaries[r["BILL_ID"]] = sm
                no_summary += not sm
                if n % 200 == 0 or n == len(queue):
                    print(f"    {n}/{len(queue)} (본문 없음 {no_summary}건)")

        # 2-2) AI 분류를 일괄 처리로 한 번에 맡긴다(분당 요청 한도에 걸리지 않고 요금도 절반).
        requests, meta = [], {}
        for r in queue:
            cid = re.sub(r"[^A-Za-z0-9_-]", "_", r["BILL_ID"])[:64]
            sm = summaries.get(r["BILL_ID"], "")
            meta[cid] = {"bill_id": r["BILL_ID"], "has_summary": bool(sm), "h": CRITERIA_HASH}
            requests.append({"custom_id": cid, "params": {
                "model": SETTINGS["model"], "max_tokens": 2000,
                "messages": [{"role": "user", "content": build_prompt(r, sm)}]}})
        b = anthropic_call(anthropic_key, "POST", "/v1/messages/batches", {"requests": requests})
        print(f"  AI 일괄 처리 요청 완료: {len(requests)}건 (보통 수 분~1시간 소요)")
        save_json(PENDING_PATH, {"id": b["id"], "meta": meta})
        finished = collect_batch(anthropic_key, b["id"], cache, meta, deadline)
        if finished:
            os.remove(PENDING_PATH)
        save_json(CACHE_PATH, cache)
        if not finished:
            break

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
