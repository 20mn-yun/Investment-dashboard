"""주요 지표 수집 모듈 (대시보드 '주요 지표' 탭 백엔드).

지표 목록은 indicators.json에서 읽는다 (source: kis_investor | yfinance | ecos).
저장: cache/indicators_history.json — {지표id: {"YYYY-MM-DD": 값 또는 {필드: 값}}}
파생값(이평·누적)은 저장하지 않는다 (표시 계층에서 계산).

CLI:
  python3 indicators_tracker.py --backfill   # KIS 2회 조회(≥490영업일) + yfinance 2y + ECOS 2y
  python3 indicators_tracker.py --update     # KIS 1회 조회, 신규 날짜 추가 + 최근 3영업일 덮어쓰기
"""

import os
import sys
import json
import argparse
from datetime import datetime, date, timedelta

import requests
from dotenv import load_dotenv

# cwd 무관하게 동작: 파일 위치 기준 절대경로
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
load_dotenv(os.path.join(BASE_DIR, ".env"))

import kis_api  # 인증(get_access_token/APP_KEY/APP_SECRET/BASE_URL) 재사용 — 토큰 캐시도 kis_api의 절대경로 사용

CONFIG_PATH = os.path.join(BASE_DIR, "indicators.json")
CACHE_DIR = os.path.join(BASE_DIR, "cache")
HISTORY_PATH = os.path.join(CACHE_DIR, "indicators_history.json")

KIS_INVESTOR_URL = f"{kis_api.BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-investor-daily-by-market"
KIS_INVESTOR_TR_ID = "FHPTJ04040000"
# 확정 파라미터 (진단 세션 실측). ISCD_1이 'KSP'가 아니면 에러 없이 전 금액 0 반환 — 절대 변경 금지
KIS_INVESTOR_PARAMS = {
    "FID_COND_MRKT_DIV_CODE": "U",
    "FID_INPUT_ISCD": "0001",
    "FID_INPUT_ISCD_1": "KSP",
    "FID_INPUT_ISCD_2": "0001",
}
KIS_INVESTOR_FIELDS = ("frgn_ntby_tr_pbmn", "orgn_ntby_tr_pbmn", "prsn_ntby_tr_pbmn")
KIS_UPDATE_OVERWRITE_DAYS = 3        # 잠정치→확정치 대비 최근 N영업일 덮어쓰기
BACKFILL_MIN_ROWS = 490

ECOS_URL = "https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/{limit}/{stat}/{cycle}/{start}/{end}/{item}"


def log(msg):
    print(f"[indicators] {msg}", flush=True)


# ===== 설정·저장 =====

def load_indicators():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return [i for i in cfg.get("indicators", []) if i.get("enabled", True)]


def load_history():
    if not os.path.exists(HISTORY_PATH):
        return {}
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"history 로드 실패, 빈 상태로 시작: {type(e).__name__}: {e}")
        return {}


def save_history(hist):
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = HISTORY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False)
    os.replace(tmp, HISTORY_PATH)


def _fmt(d):
    return f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 and d.isdigit() else d


# ===== KIS 투자자매매동향 =====

def fetch_kis_investor(base_date):
    """기준일(YYYYMMDD)부터 역순 300영업일 코스피 투자자별 순매수. {YYYY-MM-DD: {field: int(백만원)}}.

    검증 실패(rt_cd≠0 / 빈 응답 / 투자자 금액 전량 0)는 예외 — 전량 0은 파라미터 오설정의 조용한 실패 신호.
    """
    token = kis_api.get_access_token()
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": kis_api.APP_KEY,
        "appsecret": kis_api.APP_SECRET,
        "tr_id": KIS_INVESTOR_TR_ID,
        "custtype": "P",
    }
    params = dict(KIS_INVESTOR_PARAMS)
    params["FID_INPUT_DATE_1"] = base_date
    params["FID_INPUT_DATE_2"] = base_date
    r = requests.get(KIS_INVESTOR_URL, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    body = r.json()
    if body.get("rt_cd") != "0":
        raise RuntimeError(f"KIS 투자자동향 실패 base={base_date}: rt_cd={body.get('rt_cd')} "
                           f"msg_cd={body.get('msg_cd')} msg1={body.get('msg1')}")
    rows = body.get("output") or []
    if not rows:
        raise RuntimeError(f"KIS 투자자동향 빈 응답 base={base_date}")

    out = {}
    nonzero = 0
    for rec in rows:
        d = rec.get("stck_bsop_date")
        if not d:
            continue
        vals = {}
        for f in KIS_INVESTOR_FIELDS:
            v = int(rec.get(f) or 0)
            vals[f] = v
            if v != 0:
                nonzero += 1
        out[_fmt(d)] = vals
    if nonzero == 0:
        raise RuntimeError(f"KIS 투자자동향 전량 0 응답 base={base_date} ({len(rows)}건) — "
                           f"파라미터 오설정 의심(FID_INPUT_ISCD_1=KSP 확인). 캐시 저장 안 함")
    return out


def _kis_field_ids(indicators):
    """kis_investor 지표 → {지표id: [필드...]}"""
    return {i["id"]: i["source_params"]["fields"] for i in indicators if i["source"] == "kis_investor"}


def _store_kis(hist, indicators, data, overwrite_recent=0):
    """data({날짜: {필드: 값}})를 지표별로 저장. overwrite_recent>0이면 캐시 기존 날짜 중
    data에 포함된 최근 N영업일은 덮어쓰고, 그 외 기존 날짜는 유지(신규 날짜만 추가)."""
    field_map = _kis_field_ids(indicators)
    if not field_map:
        return 0
    added = 0
    for ind_id, fields in field_map.items():
        series = hist.setdefault(ind_id, {})
        existing = set(series)
        recent_allowed = set(sorted(data)[-overwrite_recent:]) if overwrite_recent else set()
        for d, vals in data.items():
            if overwrite_recent and d in existing and d not in recent_allowed:
                continue
            value = vals[fields[0]] if len(fields) == 1 else {f: vals[f] for f in fields}
            if d not in existing:
                added += 1
            series[d] = value
    return added // max(1, len(field_map))


def backfill_kis(hist, indicators):
    """오늘 기준 1회 + 가장 오래된 날짜 전일 기준 2회 → 약 600영업일. 중복 날짜는 최신 조회 결과로 덮어씀."""
    today = date.today().strftime("%Y%m%d")
    log(f"KIS 1차 조회 base={today}")
    first = fetch_kis_investor(today)
    oldest = min(first)
    prev = (datetime.strptime(oldest, "%Y-%m-%d").date() - timedelta(days=1)).strftime("%Y%m%d")
    log(f"KIS 1차 {len(first)}건 ({oldest} ~ {max(first)}), 2차 조회 base={prev}")
    second = fetch_kis_investor(prev)
    log(f"KIS 2차 {len(second)}건 ({min(second)} ~ {max(second)})")
    merged = dict(second)
    merged.update(first)          # 최신 조회(1차)가 우선
    _store_kis(hist, indicators, merged, overwrite_recent=0)
    n = len(hist.get(next(iter(_kis_field_ids(indicators))), {}))
    log(f"KIS 병합 {len(merged)}건 → 캐시 {n}건")
    if n < BACKFILL_MIN_ROWS:
        log(f"경고: KIS 저장 {n}건 < {BACKFILL_MIN_ROWS}건")
    return len(merged)


def update_kis(hist, indicators):
    """오늘 기준 1회 조회 → 캐시에 없는 날짜 추가 + 캐시에 있는 최근 3영업일 덮어쓰기."""
    today = date.today().strftime("%Y%m%d")
    log(f"KIS 갱신 조회 base={today}")
    data = fetch_kis_investor(today)
    added = _store_kis(hist, indicators, data, overwrite_recent=KIS_UPDATE_OVERWRITE_DAYS)
    recent = sorted(data)[-KIS_UPDATE_OVERWRITE_DAYS:]
    log(f"KIS 갱신: 신규 {added}건 추가, 최근 {KIS_UPDATE_OVERWRITE_DAYS}영업일({recent[0]}~{recent[-1]}) 덮어쓰기")
    return added


# ===== yfinance =====

def fetch_yfinance(ticker, period="2y"):
    """일봉 종가 {YYYY-MM-DD: float}. 오늘 날짜 행(미마감 장중가)은 제외."""
    import yfinance as yf
    df = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=True)
    if df is None or df.empty:
        raise RuntimeError(f"yfinance 빈 응답: {ticker}")
    today = date.today().isoformat()
    out = {}
    for ts, v in df["Close"].dropna().items():
        d = ts.strftime("%Y-%m-%d")
        if d >= today:
            continue                # 오늘 행은 장중가 → 제외
        out[d] = round(float(v), 6)
    return out


def collect_yfinance(hist, indicators, period="2y"):
    for ind in indicators:
        if ind["source"] != "yfinance":
            continue
        ticker = ind["source_params"]["ticker"]
        try:
            data = fetch_yfinance(ticker, period=period)
        except Exception as e:
            log(f"yfinance {ind['id']}({ticker}) 실패, 건너뜀: {type(e).__name__}: {e}")
            continue
        series = hist.setdefault(ind["id"], {})
        before = len(series)
        series.update(data)
        log(f"yfinance {ind['id']}({ticker}): 수신 {len(data)}건 ({min(data)} ~ {max(data)}), 캐시 {before}→{len(series)}건")


# ===== ECOS =====

def fetch_ecos(api_key, stat_code, item_code, cycle, start, end):
    """ECOS StatisticSearch: {YYYY-MM-DD: float}. 일별(cycle=D) 기준 TIME=YYYYMMDD."""
    url = ECOS_URL.format(key=api_key, limit=10000, stat=stat_code, cycle=cycle,
                          start=start, end=end, item=item_code)
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    body = r.json()
    if "RESULT" in body:                       # 에러 응답 형식
        res = body["RESULT"]
        raise RuntimeError(f"ECOS 오류 {res.get('CODE')}: {res.get('MESSAGE')}")
    rows = (body.get("StatisticSearch") or {}).get("row") or []
    if not rows:
        raise RuntimeError(f"ECOS 빈 응답 stat={stat_code} item={item_code} {start}~{end}")
    out = {}
    for row in rows:
        t = row.get("TIME", "")
        v = row.get("DATA_VALUE")
        if v in (None, "", "-"):
            continue
        out[_fmt(t)] = float(v)
    return out, rows[0].get("ITEM_NAME1")


def collect_ecos(hist, indicators, years=2):
    targets = [i for i in indicators if i["source"] == "ecos"]
    if not targets:
        return
    key = os.environ.get("ECOS_API_KEY", "").strip()
    if not key:
        for ind in targets:
            log(f"경고: ECOS_API_KEY 없음 — {ind['id']}({ind['name']}) 건너뜀 (.env에 ECOS_API_KEY 추가 필요)")
        return
    end = date.today()
    start = end - timedelta(days=365 * years)
    for ind in targets:
        p = ind["source_params"]
        try:
            data, item_name = fetch_ecos(key, p["stat_code"], p["item_code"], p.get("cycle", "D"),
                                         start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
        except Exception as e:
            log(f"ECOS {ind['id']} 실패, 건너뜀: {type(e).__name__}: {e}")
            continue
        series = hist.setdefault(ind["id"], {})
        before = len(series)
        series.update(data)
        log(f"ECOS {ind['id']}({p['stat_code']}/{p['item_code']} '{item_name}'): 수신 {len(data)}건 "
            f"({min(data)} ~ {max(data)}), 캐시 {before}→{len(series)}건")


# ===== 실행 =====

def run(mode):
    indicators = load_indicators()
    hist = load_history()
    log(f"모드={mode}, 활성 지표 {len(indicators)}개: {', '.join(i['id'] for i in indicators)}")

    # KIS: 실패 시 예외 전파(캐시 미저장) — 조용한 실패 금지
    if any(i["source"] == "kis_investor" for i in indicators):
        if mode == "backfill":
            backfill_kis(hist, indicators)
        else:
            update_kis(hist, indicators)

    collect_yfinance(hist, indicators, period="2y")
    collect_ecos(hist, indicators, years=2)

    hist["_meta"] = {"updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "mode": mode}
    save_history(hist)
    log(f"저장 완료: {HISTORY_PATH}")
    for ind in indicators:
        s = hist.get(ind["id"], {})
        if s:
            ks = sorted(s)
            log(f"  {ind['id']:<20} {len(s):>4}건  {ks[0]} ~ {ks[-1]}")
        else:
            log(f"  {ind['id']:<20}    0건  (미수집)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="주요 지표 수집기")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--backfill", action="store_true", help="2년치 초기 수집")
    g.add_argument("--update", action="store_true", help="일일 증분 갱신")
    args = ap.parse_args()
    try:
        run("backfill" if args.backfill else "update")
    except Exception as e:
        log(f"실패: {type(e).__name__}: {e}")
        sys.exit(1)
