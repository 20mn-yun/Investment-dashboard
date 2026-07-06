"""이격도(Disparity) 모니터링 백엔드.

이격도 = 종가 / N일 이동평균 * 100  (N = 20, 50, 120; 50일이 핵심)

- KOSPI(0001)/KOSDAQ(1001) 지수와 config 종목: KIS API로 370영업일 이상 일봉 종가 수집(페이지네이션).
- WICS 28개 중분류 업종: WiseIndex ChartData로 수집. VAL은 fromDT 기준 누적수익률(%)이므로
  레벨[t] = 100 * (1 + VAL[t]/100)로 합성 레벨을 복원한 뒤 이평/이격도를 계산한다.
- KOSPI/KOSDAQ는 50일 이격도의 국지 고점을 감지(앞뒤 10영업일보다 높고 105 이상)하고 마지막 두 고점으로 추세 판정.
- 결과를 cache/disparity.json에 기준일(base_date) 포함해 저장. 기준일이 최신 영업일이면 재수집 없이 캐시 반환.
  (장중 09:00~15:30에는 전일 종가 기준 캐시를 그대로 사용 — as_of 필드로 표현)
"""
import json
import os
import time as _time
from datetime import datetime, timedelta, time

import requests
import pandas as pd

import kis_api
import stock_leaders

CONFIG_PATH = "disparity_config.json"
CACHE_PATH = "cache/disparity.json"

INDEX_TARGETS = [("KOSPI", "0001"), ("KOSDAQ", "1001")]
INDEX_TARGET_DAYS = 520      # 2년 고점 감지 + 250일 차트 + 120 이평 여유
STOCK_TARGET_DAYS = 400      # 370영업일 이상
SECTOR_DAYS = 160

DEFAULT_CONFIG = {
    "stocks": [
        {"code": "005930", "name": "삼성전자"},
        {"code": "000660", "name": "SK하이닉스"},
    ]
}


# ────────────────────────────── config ──────────────────────────────
def load_config():
    """종목 설정을 읽는다. 파일이 없으면 기본값으로 생성한다."""
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        return list(DEFAULT_CONFIG["stocks"])
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    stocks = cfg.get("stocks") or []
    return [s for s in stocks if s.get("code")]


# ─────────────────────────── KIS 페이지네이션 ───────────────────────────
def _paginated_kis(iscd, mrkt_div, tr_id, close_key, target_days, max_pages=14):
    """일봉 종가를 FID_INPUT_DATE_2를 뒤로 이동시키며 반복 조회해 이어붙인다.
    지수(U)는 콜당 50건, 종목(J)은 콜당 약 100건."""
    token = kis_api.get_access_token()
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": kis_api.APP_KEY,
        "appsecret": kis_api.APP_SECRET,
        "tr_id": tr_id,
    }
    endpoint = ("inquire-daily-indexchartprice" if mrkt_div == "U"
                else "inquire-daily-itemchartprice")
    url = f"{kis_api.BASE_URL}/uapi/domestic-stock/v1/quotations/{endpoint}"

    got = {}
    cur_end = datetime.now()
    hard_start = datetime.now() - timedelta(days=int(target_days * 2.2))

    for _ in range(max_pages):
        start = cur_end - timedelta(days=140)
        if start < hard_start:
            start = hard_start
        params = {
            "FID_COND_MRKT_DIV_CODE": mrkt_div,
            "FID_INPUT_ISCD": iscd,
            "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": cur_end.strftime("%Y%m%d"),
            "FID_PERIOD_DIV_CODE": "D",
        }
        if mrkt_div == "J":
            params["FID_ORG_ADJ_PRC"] = "0"
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        body = resp.json()
        if body.get("rt_cd") != "0":
            raise ValueError(f"KIS {iscd}: {body.get('msg1', resp.text)[:120]}")
        recs = body.get("output2") or []
        if not recs:
            break
        for r in recs:
            d = r.get("stck_bsop_date", "")
            c = r.get(close_key, "")
            if d and c:
                got[d] = float(c)
        earliest = min(r["stck_bsop_date"] for r in recs if r.get("stck_bsop_date"))
        cur_end = datetime.strptime(earliest, "%Y%m%d") - timedelta(days=1)
        if len(got) >= target_days or cur_end < hard_start:
            break
        _time.sleep(0.1)

    if not got:
        return pd.Series(dtype=float)
    items = sorted(got.items())
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d, _ in items])
    return pd.Series([v for _, v in items], index=idx)


def _drop_incomplete_today(s, now=None):
    """장중(평일 15:30 이전) 수집 시 KIS가 주는 오늘자 미확정 봉을 제거한다.
    15:30 이후 또는 주말이면 확정 종가로 보고 유지한다. 이격도는 확정 종가 기준 지표."""
    now = now or datetime.now()
    if not len(s):
        return s
    if now.weekday() < 5 and now.time() < time(15, 30):
        today = pd.Timestamp(now.date())
        s = s[s.index.normalize() != today]
    return s


def _fetch_index_closes(code):
    s = _paginated_kis(code, "U", "FHKUP03500100", "bstp_nmix_prpr", INDEX_TARGET_DAYS)
    return _drop_incomplete_today(s)


def _fetch_stock_closes(code):
    s = _paginated_kis(code, "J", "FHKST03010100", "stck_clpr", STOCK_TARGET_DAYS)
    return _drop_incomplete_today(s)


# ─────────────────────────── WiseIndex 업종 ───────────────────────────
def _reconstruct_level(dates, cum_returns):
    """누적수익률(%) 시계열을 레벨[t] = 100 * (1 + VAL/100)로 복원."""
    out_dates, out_levels = [], []
    for d, v in zip(dates, cum_returns):
        if v is None:
            continue
        out_dates.append(d)
        out_levels.append(100.0 * (1.0 + float(v) / 100.0))
    if not out_dates:
        return pd.Series(dtype=float)
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in out_dates])
    s = pd.Series(out_levels, index=idx)
    return s[~s.index.duplicated(keep="first")].sort_index()


def fetch_sector_levels(days=SECTOR_DAYS):
    """WICS 28개 중분류의 합성 레벨 시계열 dict{code: pd.Series} 반환."""
    raw = stock_leaders.fetch_wics_index_series(days=days)
    out = {}
    for code in stock_leaders.WICS_MID_NAMES:
        d = raw.get(code) or {}
        s = _drop_incomplete_today(_reconstruct_level(d.get("dates", []), d.get("values", [])))
        if len(s):
            out[code] = s
    return out


# ─────────────────────────── 이평/이격도 ───────────────────────────
def _r(x):
    return None if x is None or pd.isna(x) else round(float(x), 2)


def _build_series(closes):
    """종가 Series → 날짜별 {close, ma20/50/120, disp20/50/120} 리스트."""
    closes = closes.sort_index()
    ma = {n: closes.rolling(n).mean() for n in (20, 50, 120)}
    disp = {n: closes / ma[n] * 100 for n in (20, 50, 120)}
    rows = []
    for dt in closes.index:
        rows.append({
            "date": dt.strftime("%Y-%m-%d"),
            "close": round(float(closes[dt]), 2),
            "ma20": _r(ma[20][dt]), "ma50": _r(ma[50][dt]), "ma120": _r(ma[120][dt]),
            "disp20": _r(disp[20][dt]), "disp50": _r(disp[50][dt]), "disp120": _r(disp[120][dt]),
        })
    return rows


def _latest_disparity(rows):
    if not rows:
        return {}
    last = rows[-1]
    return {"date": last["date"], "close": last["close"],
            "disp20": last["disp20"], "disp50": last["disp50"], "disp120": last["disp120"]}


def _detect_peaks(rows, window=10, min_disp=105.0, lookback=504):
    """50일 이격도의 국지 고점 감지: 앞뒤 window 영업일보다 높고 이격도 min_disp 이상.
    마지막 두 고점 비교로 추세('상승'/'하락') 판정."""
    sub = rows[-lookback:] if len(rows) > lookback else rows
    disp = [r["disp50"] for r in sub]
    dates = [r["date"] for r in sub]
    peaks = []
    n = len(sub)
    for i in range(window, n - window):
        v = disp[i]
        if v is None or v < min_disp:
            continue
        left = disp[i - window:i]
        right = disp[i + 1:i + 1 + window]
        if any(x is None for x in left) or any(x is None for x in right):
            continue
        if all(v > x for x in left) and all(v > x for x in right):
            peaks.append({"date": dates[i], "value": round(v, 2)})
    trend = None
    if len(peaks) >= 2:
        trend = "상승" if peaks[-1]["value"] > peaks[-2]["value"] else "하락"
    return peaks, trend


# ─────────────────────────── 수집·계산 ───────────────────────────
def _collect_all():
    """전 대상 수집·계산. 실패한 대상은 errors에만 기록하고 데이터에서 제외."""
    errors = {}
    indices = {}
    stocks = {}
    sectors = {}
    base_dates = []

    # 지수 (고점 감지 포함)
    for name, code in INDEX_TARGETS:
        try:
            closes = _fetch_index_closes(code)
            if not len(closes):
                raise ValueError("빈 응답")
            rows = _build_series(closes)
            peaks, trend = _detect_peaks(rows)
            indices[name] = {
                "name": name, "code": code, "series": rows,
                "latest": _latest_disparity(rows),
                "peaks": peaks, "peak_trend": trend,
            }
            base_dates.append(rows[-1]["date"])
        except Exception as e:
            errors[name] = f"{type(e).__name__}: {e}"

    # 종목 (고점 감지 없음)
    for s in load_config():
        code, nm = s["code"], s.get("name", s["code"])
        try:
            closes = _fetch_stock_closes(code)
            if not len(closes):
                raise ValueError("빈 응답")
            rows = _build_series(closes)
            stocks[code] = {
                "name": nm, "code": code, "series": rows,
                "latest": _latest_disparity(rows),
            }
            base_dates.append(rows[-1]["date"])
        except Exception as e:
            errors[code] = f"{type(e).__name__}: {e}"

    # 업종 (최신 20/50/120 이격도만 저장, 시계열 미저장)
    try:
        levels = fetch_sector_levels()
        for code, s in levels.items():
            rows = _build_series(s)
            if not rows:
                continue
            last = rows[-1]
            sectors[code] = {
                "code": code, "name": stock_leaders.WICS_MID_NAMES.get(code, code),
                "date": last["date"],
                "disp20": last["disp20"], "disp50": last["disp50"], "disp120": last["disp120"],
            }
            base_dates.append(last["date"])
    except Exception as e:
        errors["sectors"] = f"{type(e).__name__}: {e}"

    if not base_dates:
        raise ValueError("모든 대상 수집 실패: " + json.dumps(errors, ensure_ascii=False))

    base_date = max(base_dates)  # 데이터 마지막 영업일
    return {
        "base_date": base_date.replace("-", ""),
        "base_date_iso": base_date,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "indices": indices,
        "stocks": stocks,
        "sectors": sectors,
        "errors": errors,
    }


# ─────────────────────────── 캐시 로직 ───────────────────────────
def _market_open_now(now=None):
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    return time(9, 0) <= now.time() <= time(15, 30)


def _expected_data_date(now=None):
    """완결된 최근 영업일(YYYYMMDD). 장 마감(15:30) 전/주말이면 직전 영업일."""
    now = now or datetime.now()
    d = now
    if now.weekday() < 5 and now.time() < time(15, 30):
        d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d = d - timedelta(days=1)
    return d.strftime("%Y%m%d")


def _load_cache():
    if not os.path.exists(CACHE_PATH):
        return None
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_cache(data):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _as_of_text(base_date_iso):
    if _market_open_now():
        return f"{base_date_iso} 종가 기준 (장중 — 전일 종가 캐시 유지)"
    return f"{base_date_iso} 종가 기준"


def get_disparity_data(force=False):
    """캐시 로직을 거쳐 이격도 데이터를 반환한다.
    캐시 기준일이 최신 완결 영업일 이상이면 재수집하지 않는다."""
    cache = _load_cache()
    if not force and cache and cache.get("base_date", "") >= _expected_data_date():
        cache["as_of"] = _as_of_text(cache.get("base_date_iso", ""))
        cache["cached"] = True
        return cache

    data = _collect_all()
    _save_cache(data)
    data = dict(data)
    data["as_of"] = _as_of_text(data.get("base_date_iso", ""))
    data["cached"] = False
    return data


if __name__ == "__main__":
    d = get_disparity_data(force=True)
    print("base_date:", d["base_date_iso"], "| as_of:", d["as_of"])
    print("errors:", d["errors"])
    k = d["indices"].get("KOSPI", {}).get("latest", {})
    print("KOSPI 이격도  20/50/120:", k.get("disp20"), k.get("disp50"), k.get("disp120"))
    s = d["stocks"].get("005930", {}).get("latest", {})
    print("삼성전자 이격도 20/50/120:", s.get("disp20"), s.get("disp50"), s.get("disp120"))
    semi = d["sectors"].get("G4530", {})
    print("반도체(G4530) 50일 이격도:", semi.get("disp50"))
    print("KOSPI 고점 리스트:", d["indices"].get("KOSPI", {}).get("peaks"))
    print("KOSPI 고점 추세:", d["indices"].get("KOSPI", {}).get("peak_trend"))
