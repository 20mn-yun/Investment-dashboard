"""
DART 영업(잠정)실적 공시 추적기.

매일 평일 20:00 KST 실행. DART에서 당일 '연결재무제표 기준 영업(잠정)실적' 공시를
감지해 Google Drive 엑셀(잠정실적_누적.xlsx)에 누적 기록.

Phase 1: 메타데이터(기업명, 공시일, 종목코드, DART 링크)만 기록.
Phase 2~4에서 재무 수치 파싱, 과거 분기, 필터링 추가 예정.
"""

import os
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

EARNINGS_KEYWORD = "연결재무제표 기준 영업(잠정)실적"


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
    """target_date의 공시 중 EARNINGS_KEYWORD를 포함하는 것만 반환."""
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
            if EARNINGS_KEYWORD in item.get("report_nm", ""):
                matched.append(item)

        total_page = data.get("total_page", 1)
        if page_no >= total_page:
            break
        page_no += 1

    return matched


def append_rows(rows):
    """새 행들을 엑셀에 누적. 중복 방지: (공시일자, 종목코드) 기준."""
    if not rows:
        return 0

    os.makedirs(os.path.dirname(EXCEL_PATH), exist_ok=True)

    if os.path.exists(EXCEL_PATH):
        wb = load_workbook(EXCEL_PATH)
        ws = wb.active
        existing = set()
        for r in ws.iter_rows(min_row=2, values_only=True):
            if r and len(r) >= 3 and r[0] and r[2]:
                existing.add((str(r[0]), str(r[2])))
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = "잠정실적"
        ws.append(HEADERS)
        existing = set()

    added = 0
    for row in rows:
        key = (str(row[0]), str(row[2]))
        if key in existing:
            continue
        ws.append(row)
        existing.add(key)
        added += 1

    wb.save(EXCEL_PATH)
    return added


def run_daily(force=False):
    """
    하루 1회 실행 진입점.
    force=False: 오늘 이미 실행했으면 skip. 주말 skip.
    force=True: 위 조건 무시하고 무조건 실행.
    """
    today = date.today()

    if not force and today.weekday() >= 5:
        print(f"[earnings_tracker] {today} 주말이므로 skip")
        return {"status": "skipped_weekend"}

    state = load_state()
    if not force and state.get("last_run_date") == today.isoformat():
        print(f"[earnings_tracker] {today} 이미 실행됨 skip")
        return {"status": "skipped_already_run"}

    print(f"[earnings_tracker] {today} 실행 시작")

    try:
        filings = fetch_filings(today)
        print(f"[earnings_tracker] 공시 {len(filings)}건 발견")

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

        added = append_rows(rows)
        print(f"[earnings_tracker] 엑셀에 {added}건 추가 (중복 제외)")

        state = {
            "last_run_date": today.isoformat(),
            "last_run_at": datetime.now().isoformat(),
            "last_run_filings_found": len(filings),
            "last_run_added": added,
        }
        save_state(state)

        return {"status": "ok", "filings_found": len(filings), "added": added}

    except Exception as e:
        import traceback
        print(f"[earnings_tracker] 에러: {e}")
        traceback.print_exc()
        return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    import sys
    force = "--force" in sys.argv
    result = run_daily(force=force)
    print(f"[earnings_tracker] 결과: {result}")
