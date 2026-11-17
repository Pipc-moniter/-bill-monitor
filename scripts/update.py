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


def fetch_bill_list(api_key):
    """해당 대수 의원발의 법률안 전체를 페이지 단위로 받는다."""
    rows, page, size = [], 1, 1000
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
        print(f"  목록 {len(rows)}/{total}")
        if len(rows) >= total or not page_rows:
            break
        page += 1
        time.sleep(0.3)
    return rows


def fetch_summary(bill_id):
    """의안정보시스템 상세 페이지에서 제안이유 및 주요내용을 추출한다."""
    try:
        page = http_get(DETAIL_URL.format(bill_id), timeout=20)
    except Exception as e:
        print(f"  요약 수집 실패 {bill_id}: {e}")
        return ""
    m = re.search(r'id="summaryContentDiv"[^>]*>(.*?)</div>', page, re.DOTALL)
    if not m:
        m = re.search(r'제안이유\s*및\s*주요내용(.*?)</div>', page, re.DOTALL)
    if not m:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", m.group(1))
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"[ \t]+", " ", text).strip()[:6000]


def classify(api_key, bill, summary):
    prompt = f"""당신은 개인정보보호위원회의 입법 모니터링 담당자입니다.
아래 기준에 따라 법안이 개인정보와 관련 있는지 판단하세요.

<기준>
{CRITERIA}
</기준>

<법안>
의안명: {bill.get('BILL_NAME', '')}
제안이유 및 주요내용:
{summary or '(본문 없음: 의안명만으로 판단)'}
</법안>

다음 JSON 한 줄로만 답하세요.
{{"relevant": true 또는 false, "reason": "판단 근거 한 문장"}}"""
    body = json.dumps({
        "model": SETTINGS["model"],
        "max_tokens": 200,
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
    for i in range(4):
        try:
            req = urllib.request.Request(ANTHROPIC_URL, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                res = json.loads(r.read())
            text = "".join(c.get("text", "") for c in res.get("content", []))
            m = re.search(r"\{.*\}", text, re.DOTALL)
            parsed = json.loads(m.group(0))
            return {"relevant": bool(parsed.get("relevant")), "reason": str(parsed.get("reason", ""))[:300]}
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 529):
                time.sleep(5 * (i + 1))
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
    assembly_key = os.environ.get("ASSEMBLY_API_KEY", "").strip()
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not assembly_key or not anthropic_key:
        sys.exit("ASSEMBLY_API_KEY, ANTHROPIC_API_KEY 환경변수가 필요합니다.")

    print("1) 법안 목록 수집")
    rows = fetch_bill_list(assembly_key)
    rows = [r for r in rows if (r.get("PROPOSE_DT") or "") >= SETTINGS["start_date"]]
    print(f"  기간 내 법안 {len(rows)}건")

    cache = load_json(CACHE_PATH, {})
    todo = [r for r in rows if cache.get(r["BILL_ID"], {}).get("h") != CRITERIA_HASH]
    todo.sort(key=lambda r: r.get("PROPOSE_DT", ""), reverse=True)  # 최신부터
    limit = int(SETTINGS["max_classify_per_run"])
    print(f"2) AI 분류 대상 {len(todo)}건 (이번 실행 최대 {limit}건)")

    done = 0
    for r in todo[:limit]:
        summary = fetch_summary(r["BILL_ID"])
        result = classify(anthropic_key, r, summary)
        if result is None:
            continue
        cache[r["BILL_ID"]] = {"h": CRITERIA_HASH, "has_summary": bool(summary), **result}
        done += 1
        if done % 50 == 0:
            save_json(CACHE_PATH, cache)  # 중간 저장
            print(f"  분류 {done}건 완료")
        time.sleep(0.2)
    save_json(CACHE_PATH, cache)
    print(f"  이번 실행 분류 {done}건")

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
            reason, source = include[no], "manual"
        elif c and c.get("relevant"):
            reason, source = c.get("reason", ""), "ai"
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
            "source": source,
            "title_only": bool(c and not c.get("has_summary")),
        })
    bills.sort(key=lambda b: (b["date"], b["no"]), reverse=True)

    save_json(OUT_PATH, {
        "site_title": SETTINGS["site_title"],
        "updated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "start_date": SETTINGS["start_date"],
        "scanned": len(rows),
        "pending": pending,
        "bills": bills,
    })
    print(f"  관련 법안 {len(bills)}건 / 검토 {len(rows)}건 / 분류 대기 {pending}건")


if __name__ == "__main__":
    main()
