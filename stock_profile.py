"""종목 탭: 종목 요약(/api/stock/summary)과 공시 목록(/api/stock/disclosures).

기존 함수를 불러 조립한다.
  - 한국 현재가: kis_api.get_current_price, 전일 종가: kis_api.get_daily_price_history
  - 한국 시장 구분·상장주수: kis_api.download_kr_stock_master (하루 캐시, 시총 3000억 미만 포함 전 종목)
  - PER/PBR: 모든 한국 종목 같은 기준 — 현재 시가총액 / 4분기누적 지배주주순이익,
             현재 시가총액 / 최근 분기 말 지배주주지분 (재무 3표 저장본, 없으면 빈 값)
  - ROE·결산월·연결/별도: 재무 3표 저장본 (없으면 빈 값)
  - 미국: yfinance Ticker.info 에서 얻을 수 있는 값만
"""
import json
import os
import threading
import time
from datetime import date, datetime, timedelta

import dart_report
import kr_financials
import watchlist as watchlist_store

_BD = os.path.dirname(os.path.abspath(__file__))
DISC_DIR = os.path.join(_BD, "cache", "kr_disclosures")

SUMMARY_TTL = 60          # 요약 메모리 캐시 (초)
US_SUMMARY_TTL = 300
DISC_TTL = 1800           # 공시 목록 저장본 재사용 (초)
DISC_DAYS = 365
DISC_EXCLUDE = ("임원ㆍ주요주주", "임원·주요주주")   # 보고서 이름에 들어가면 목록에서 뺌 (소유상황보고 등)
PRICE_TTL = 86400

_summary_cache = {}
_summary_lock = threading.Lock()
_master = {"time": 0, "map": {}}
_master_lock = threading.Lock()


def _num(v):
    try:
        if v is None:
            return None
        f = float(v)
        return None if f != f else f   # NaN
    except (TypeError, ValueError):
        return None


# ===== 한국 =====
def _kr_master():
    """{종목코드: {market, shares(주), prev_cap(원), acc_mt}} — KIS 종목 마스터(하루 캐시)."""
    with _master_lock:
        if _master["map"] and time.time() - _master["time"] < 3600:
            return _master["map"]
        import kis_api
        m = {}
        for market, cols in (("KOSPI", ("상장주수", "시가총액", "결산월")),
                             ("KOSDAQ", ("상장 주수(천)", "전일기준 시가총액 (억)", "결산 월"))):
            try:
                df = kis_api.download_kr_stock_master(market)
            except Exception as e:
                print(f"[stock_profile] {market} 마스터 실패: {type(e).__name__}", flush=True)
                continue
            for _, row in df.iterrows():
                code = str(row["단축코드"]).strip()
                shares = _num(row.get(cols[0]))
                cap = _num(row.get(cols[1]))
                mt = _num(row.get(cols[2]))
                m[code] = {"market": market,
                           "shares": int(shares * 1000) if shares else None,     # 천주 → 주
                           "prev_cap": int(cap * 1e8) if cap else None,          # 억원 → 원
                           "acc_mt": int(mt) if mt else None}
        if m:
            _master.update(time=time.time(), map=m)
        return _master["map"]


def _kr_quote(code):
    """(현재가, 전일 종가). 실패한 값은 None."""
    import kis_api
    price = prev = None
    try:
        price = int(kis_api.get_current_price(code).get("stck_prpr") or 0) or None
    except Exception as e:
        print(f"[stock_profile] {code} 현재가 실패: {type(e).__name__}", flush=True)
    try:
        # 등락률 기준 = 마지막 거래일의 전 거래일 종가 (주말·장 시작 전에도 마지막 거래일 등락률이 나오게)
        s = kis_api.get_daily_price_history(code, 10)
        prev = float(s.iloc[-2]) if len(s) >= 2 else None
        if price is None and len(s):
            price = int(s.iloc[-1])
    except Exception as e:
        print(f"[stock_profile] {code} 일별 시세 실패: {type(e).__name__}", flush=True)
    return price, prev


def kr_summary(code, name, corp_code):
    master = _kr_master().get(code, {})
    price, prev = _kr_quote(code)
    change_pct = round((price / prev - 1) * 100, 2) if price and prev else None
    shares = master.get("shares")
    if price and shares:
        mcap, mcap_basis = price * shares, "현재가 × 상장주수"
    elif master.get("prev_cap"):
        mcap, mcap_basis = master["prev_cap"], "전일 기준 (종목 마스터)"
    else:
        mcap, mcap_basis = None, None

    # 재무 3표 저장본 (받아오지 않음 — 종목 탭이 /api/stock/financials 로 받는다)
    fin = None
    cache = kr_financials._load_cache(code)
    if cache and cache.get("acc_mt"):
        try:
            fin = kr_financials.compute(cache, 5, name)
        except Exception as e:
            print(f"[stock_profile] {code} 재무 계산 실패: {type(e).__name__}: {e}", flush=True)
    roe = last = None
    if fin and fin["periods"]["ttm"]:
        last = fin["periods"]["ttm"][-1]
        roe = fin["metrics"]["ttm"].get(last, {}).get("roe")

    # PER/PBR: 스크리너·밴드와 같은 공통 함수 (오늘 기준 공시된 보고서만, TTM 지배주주순이익 / 최근 분기 말 지배주주지분)
    import valuation_ttm
    v = valuation_ttm.valuation_asof(code) if fin else {"ttm_ni": None, "equity": None, "ttm_end": None, "equity_end": None}
    per, pbr = valuation_ttm.per_pbr(mcap, v)
    val_basis = (f"현재 시가총액 / {v['ttm_end']} 기준 4분기누적 지배주주순이익, 현재 시가총액 / {v['equity_end']} 말 지배주주지분 (공시 시점 기준, basis=ttm)"
                 if fin and v.get("equity_end") else None)

    return {
        "market": "KR", "code": code, "name": name,
        "exchange": master.get("market"),
        "currency": "KRW",
        "price": price, "prev_close": prev, "change_pct": change_pct,
        "market_cap": mcap, "market_cap_basis": mcap_basis,
        "shares": shares,                      # 현재 상장주식수 (과거 시가총액 근사용)
        "per": per, "pbr": pbr, "valuation_basis": val_basis,
        "roe": roe,
        "fiscal_month": (fin or {}).get("fiscal_month") or master.get("acc_mt"),
        "fs_div": (fin or {}).get("fs_div"),
        "fs_div_label": (fin or {}).get("fs_div_label"),
        "financials_ready": bool(fin),
        "in_watchlist": watchlist_store.is_in_watchlist("KR", code),
        "as_of": datetime.now().isoformat(timespec="seconds"),
    }


# ===== 미국 =====
def us_summary(code, name):
    import yfinance as yf
    info = {}
    try:
        info = yf.Ticker(watchlist_store.to_source_ticker(code, "yahoo")).info or {}
    except Exception as e:
        print(f"[stock_profile] {code} yfinance 실패: {type(e).__name__}", flush=True)
    price = _num(info.get("currentPrice") or info.get("regularMarketPrice"))
    prev = _num(info.get("previousClose") or info.get("regularMarketPreviousClose"))
    roe = _num(info.get("returnOnEquity"))
    fye = info.get("lastFiscalYearEnd")
    return {
        "market": "US", "code": code, "name": info.get("longName") or name,
        "exchange": info.get("exchange"),
        "currency": info.get("currency") or "USD",
        "price": price, "prev_close": prev,
        "change_pct": round((price / prev - 1) * 100, 2) if price and prev else None,
        "market_cap": _num(info.get("marketCap")), "market_cap_basis": "yfinance" if info.get("marketCap") else None,
        "per": _num(info.get("trailingPE")), "pbr": _num(info.get("priceToBook")),
        "valuation_basis": "yfinance" if info else None,
        "roe": round(roe * 100, 2) if roe is not None else None,
        "fiscal_month": datetime.utcfromtimestamp(fye).month if isinstance(fye, (int, float)) else None,
        "fs_div": None, "fs_div_label": None,
        "financials_ready": False,
        "in_watchlist": watchlist_store.is_in_watchlist("US", code),
        "as_of": datetime.now().isoformat(timespec="seconds"),
    }


def get_summary(market, code, name, corp_code=None):
    key = (market, code)
    ttl = SUMMARY_TTL if market == "KR" else US_SUMMARY_TTL
    with _summary_lock:
        hit = _summary_cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            out = dict(hit[1])
            out["in_watchlist"] = watchlist_store.is_in_watchlist(market, code)
            return out
    out = kr_summary(code, name, corp_code) if market == "KR" else us_summary(code, name)
    # 재무 3표가 아직 없으면 짧게만 재사용 (받아온 뒤 PER/PBR/ROE 가 채워지도록)
    with _summary_lock:
        _summary_cache[key] = (time.time() - (ttl - 10 if market == "KR" and not out["financials_ready"] else 0), out)
    return out


# ===== 공시 =====
def _disc_excluded(title):
    return any(x in (title or "") for x in DISC_EXCLUDE)


def _disc_path(code):
    return os.path.join(DISC_DIR, f"{code}.json")


def kr_disclosures(code, corp_code, refresh=False):
    """최근 1년 공시 목록. 저장본이 DISC_TTL 안이면 재사용.
    DART list.json 을 종류 필터 없이 전 페이지 조회 (기존 list_periodic_filings 와 같은 호출 방식,
    정기공시 구분은 dart_report.PERIODIC_KEYWORDS 로 표시)."""
    path = _disc_path(code)
    if not refresh:
        try:
            with open(path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if time.time() - cached.get("fetched_ts", 0) < DISC_TTL:
                cached["items"] = [x for x in cached.get("items", []) if not _disc_excluded(x["title"])]
                cached["count"] = len(cached["items"])
                cached["cached"] = True
                return cached
        except (FileNotFoundError, json.JSONDecodeError):
            pass
    end = date.today()
    bgn = end - timedelta(days=DISC_DAYS)
    items, page, excluded = [], 1, 0
    while True:
        d = kr_financials._dart_get("list.json", {
            "corp_code": corp_code, "bgn_de": bgn.strftime("%Y%m%d"), "end_de": end.strftime("%Y%m%d"),
            "page_count": 100, "page_no": page,
        })
        if d.get("status") != "000":
            break
        for it in d.get("list", []):
            title = (it.get("report_nm") or "").strip()
            if _disc_excluded(title):
                excluded += 1
                continue
            dt = it.get("rcept_dt", "")
            rno = it.get("rcept_no", "")
            items.append({
                "date": f"{dt[:4]}-{dt[4:6]}-{dt[6:8]}" if len(dt) == 8 else dt,
                "title": title,
                "rcept_no": rno,
                "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rno}",
                "filer": it.get("flr_nm", ""),
                "periodic": any(k in title for k in dart_report.PERIODIC_KEYWORDS),
                "remark": it.get("rm", ""),
            })
        if page >= int(d.get("total_page", 1) or 1):
            break
        page += 1
    items.sort(key=lambda x: (x["date"], x["rcept_no"]), reverse=True)
    out = {"market": "KR", "code": code, "from": bgn.isoformat(), "to": end.isoformat(),
           "count": len(items), "excluded": excluded, "excluded_rule": "보고서 이름에 '임원ㆍ주요주주' 포함",
           "items": items,
           "fetched_at": datetime.now().isoformat(timespec="seconds"), "fetched_ts": time.time()}
    os.makedirs(DISC_DIR, exist_ok=True)
    watchlist_store.atomic_write_json(path, out)
    out["cached"] = False
    return out


# ===== 월말 주가 (재무정보 차트의 주가·주가수익률 선) =====
_price_cache = {}
_price_lock = threading.Lock()


def kr_monthly_prices(code):
    """최근 10년 월말 종가 {"YYYY.MM": 종가}. yfinance 월봉(분할 조정), 하루 메모리 캐시."""
    with _price_lock:
        hit = _price_cache.get(code)
        if hit and time.time() - hit[0] < PRICE_TTL:
            return hit[1]
    import yfinance as yf
    exch = _kr_master().get(code, {}).get("market")
    suffix = ".KQ" if exch == "KOSDAQ" else ".KS"
    out = {"market": "KR", "code": code, "symbol": code + suffix, "source": "yfinance 1mo", "prices": {}}
    try:
        df = yf.download(code + suffix, period="10y", interval="1mo", progress=False, auto_adjust=False, threads=False)
        col = df["Close"]
        if hasattr(col, "columns"):
            col = col.iloc[:, 0]
        for d, v in col.items():
            v = _num(v)
            if v is not None:
                out["prices"][f"{d.year}.{d.month:02d}"] = round(v, 2)
    except Exception as e:
        print(f"[stock_profile] {code} 월봉 실패: {type(e).__name__}", flush=True)
    out["count"] = len(out["prices"])
    out["fetched_at"] = datetime.now().isoformat(timespec="seconds")
    if out["prices"]:
        with _price_lock:
            _price_cache[code] = (time.time(), out)
    return out
