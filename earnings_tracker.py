"""
DART 영업(잠정)실적 공시 추적기.

매일 평일 20:00 KST 실행. DART에서 당일 '연결재무제표 기준 영업(잠정)실적' 공시를
감지해 Google Drive 엑셀(잠정실적_누적.xlsx)에 누적 기록.

Phase 1: 메타데이터(기업명, 공시일, 종목코드, DART 링크)만 기록.
Phase 2~4에서 재무 수치 파싱, 과거 분기, 필터링 추가 예정.
"""

import os
import re
import json
import requests
from datetime import datetime, date, timedelta
from dotenv import load_dotenv
from openpyxl import Workbook, load_workbook

load_dotenv()

DART_API_KEY = os.environ.get("DART_API_KEY")
EXCEL_PATH = os.path.expanduser(
    "~/Library/CloudStorage/GoogleDrive-changyun1222@gmail.com/"
    "내 드라이브/공시정리/잠정실적_누적.xlsx"
)
STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "earnings_tracker_state.json"
)

HEADERS = ["공시일자", "기업명", "종목코드", "보고서명", "DART 링크"]

EARNINGS_PATTERN = re.compile(r"영업\s*\(\s*잠정\s*\)\s*실적")


def _is_earnings_filing(report_nm: str) -> bool:
    if not report_nm:
        return False
    return bool(EARNINGS_PATTERN.search(report_nm))


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"last_run_date": None, "last_run_at": None}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch_filings(target_date):
    """target_date의 공시 중 잠정실적 공시만 반환."""
    if not DART_API_KEY:
        raise RuntimeError("DART_API_KEY 환경변수가 설정되지 않았습니다.")

    target_str = target_date.strftime("%Y%m%d")
    matched = []
    page_no = 1

    while True:
        try:
            res = requests.get(
                "https://opendart.fss.or.kr/api/list.json",
                params={
                    "crtfc_key": DART_API_KEY,
                    "bgn_de": target_str,
                    "end_de": target_str,
                    "page_count": 100,
                    "page_no": page_no,
                },
                timeout=15,
            )
            data = res.json()
        except Exception as e:
            print(f"[earnings_tracker] DART list.json fetch 실패 (page {page_no}): {e}")
            break

        if data.get("status") != "000":
            if data.get("status") == "013":
                break
            print(f"[earnings_tracker] DART API 에러: {data.get('status')} {data.get('message')}")
            break

        items = data.get("list", [])
        for item in items:
            if _is_earnings_filing(item.get("report_nm", "")):
                matched.append(item)

        total_page = data.get("total_page", 1)
        if page_no >= total_page:
            break
        page_no += 1

    return matched


def append_rows(rows):
    """
    새 행들을 엑셀에 누적 기록.
    같은 (공시일자, 종목코드) 키가 이미 있으면 새 데이터로 덮어쓰기 (정정공시 우선).
    """
    if not rows:
        return {"added": 0, "updated": 0}

    os.makedirs(os.path.dirname(EXCEL_PATH), exist_ok=True)

    if os.path.exists(EXCEL_PATH):
        wb = load_workbook(EXCEL_PATH)
        ws = wb.active
        existing = {}
        for idx, r in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            if r and len(r) >= 3 and r[0] and r[2]:
                existing[(str(r[0]), str(r[2]))] = idx
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = "잠정실적"
        ws.append(HEADERS)
        existing = {}

    added = 0
    updated = 0
    for row in rows:
        key = (str(row[0]), str(row[2]))
        if key in existing:
            target_row = existing[key]
            for col_idx, value in enumerate(row, start=1):
                ws.cell(row=target_row, column=col_idx, value=value)
            updated += 1
        else:
            ws.append(row)
            existing[key] = ws.max_row
            added += 1

    wb.save(EXCEL_PATH)
    return {"added": added, "updated": updated}


def run_daily(force=False, target_date=None):
    """
    하루 1회 실행 진입점.
    target_date=None이면 오늘 기준. 백필 시 특정 날짜 지정 가능 (state 미갱신).
    """
    is_backfill = target_date is not None
    today = target_date or date.today()

    if today.weekday() >= 5:
        msg = f"[earnings_tracker] {today} 주말이므로 skip"
        if is_backfill:
            msg += " (backfill)"
        print(msg)
        return {"status": "skipped_weekend", "date": today.isoformat()}

    if not is_backfill:
        state = load_state()
        if not force and state.get("last_run_date") == today.isoformat():
            print(f"[earnings_tracker] {today} 이미 실행됨 skip")
            return {"status": "skipped_already_run", "date": today.isoformat()}

    mode_label = "[backfill]" if is_backfill else "[daily]"
    print(f"[earnings_tracker] {mode_label} {today} 실행 시작")

    try:
        filings = fetch_filings(today)
        print(f"[earnings_tracker] {mode_label} {today} 공시 {len(filings)}건 발견")

        rows = []
        for f in filings:
            rcept_no = f.get("rcept_no", "")
            row = [
                today.isoformat(),
                f.get("corp_name", ""),
                f.get("stock_code", ""),
                f.get("report_nm", ""),
                f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}",
            ]
            rows.append(row)

        result = append_rows(rows)
        print(f"[earnings_tracker] {mode_label} {today} 신규 {result['added']}건, 정정 {result['updated']}건")

        if not is_backfill:
            state = {
                "last_run_date": today.isoformat(),
                "last_run_at": datetime.now().isoformat(),
                "last_run_filings_found": len(filings),
                "last_run_added": result["added"],
                "last_run_updated": result["updated"],
            }
            save_state(state)

        return {
            "status": "ok",
            "date": today.isoformat(),
            "filings_found": len(filings),
            "added": result["added"],
            "updated": result["updated"],
            "mode": "backfill" if is_backfill else "daily",
        }

    except Exception as e:
        import traceback
        print(f"[earnings_tracker] {mode_label} {today} 에러: {e}")
        traceback.print_exc()
        return {"status": "error", "date": today.isoformat(), "message": str(e)}


def run_backfill(days):
    """오늘 포함 N일치 백필. 주말 자동 skip."""
    if days < 1:
        return []

    today = date.today()
    results = []

    print(f"[earnings_tracker] 백필 시작: {days}일")

    for i in range(days):
        target = today - timedelta(days=i)
        result = run_daily(target_date=target)
        results.append(result)

    total_added = sum(r.get("added", 0) for r in results if r.get("status") == "ok")
    total_updated = sum(r.get("updated", 0) for r in results if r.get("status") == "ok")
    print(f"[earnings_tracker] 백필 완료: 총 신규 {total_added}건, 정정 {total_updated}건")

    return results


if __name__ == "__main__":
    import sys

    backfill_days = None
    for arg in sys.argv[1:]:
        if arg.startswith("--backfill-days="):
            try:
                backfill_days = int(arg.split("=", 1)[1])
            except ValueError:
                print(f"잘못된 값: {arg}. --backfill-days=숫자 형식 사용")
                sys.exit(1)

    if backfill_days is not None:
        results = run_backfill(backfill_days)
        print(f"[earnings_tracker] 백필 결과 (일자별):")
        for r in results:
            print(f"  {r.get('date')}: {r.get('status')} added={r.get('added', 0)} updated={r.get('updated', 0)}")
    else:
        force = "--force" in sys.argv
        result = run_daily(force=force)
        print(f"[earnings_tracker] 결과: {result}")
