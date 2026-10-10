"""밸류에이션 공통 계산 — 4분기누적(TTM) 기준, 공시 시점 기준.

기준일(as-of)에 공시돼 있던 보고서(접수일 ≤ 기준일)만으로
  - 최근 4개 분기 지배주주순이익 합(TTM)
  - 그 시점 최근 분기 말 지배주주지분
  - 그 시점 유통주식수(DART 주식총수, 없으면 직전 공시값 이월; 1단계-G 규칙)
를 돌려준다. 자료는 kr_financials 저장본(cache/kr_financials)만 쓴다(새 DART 호출 없음).
별도재무제표만 있는 회사는 kr_financials 의 OWNER_FALLBACK 규칙대로 당기순이익·자본총계가 이미 대체돼 있다.
스크리너(valuation_screener.py), 밴드(band_calculator.py), 종목 요약(stock_profile.py)이 모두 이 모듈을 쓴다.
"""
import threading
from datetime import date, datetime

import kr_financials

BASIS = "ttm"

_series_cache = {}
_series_lock = threading.Lock()


def _d(s):
    """'YYYYMMDD' 또는 'YYYY-MM-DD' → date"""
    if not s:
        return None
    s = s.replace("-", "")[:8]
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def _label_end(lb):
    y, m = lb.split(".")
    return kr_financials._month_end(int(y), int(m))


def series_from_output(code, out, shares=None):
    """compute() 응답(한국 kr_financials / 미국 us_financials 공통 구조) → 분기별 점 목록.
    shares 를 주지 않으면 응답의 shares_by_period(기간 말 주식수)를 쓴다 — 공시일 대신 기간 말을 기준으로 as-of 선택."""
    ni = out["statements"]["IS"]["items"]["net_income_owner"]
    eq = out["statements"]["BS"]["items"]["equity_owner"]
    avail_q = out["available_from"]["quarter"]
    used_q = out["reports_used"]["quarter"]
    labels = out["periods"]["quarter"]
    points = []
    for i, lb in enumerate(labels):
        last4 = labels[max(0, i - 3):i + 1]
        ttm_ni = ni["ttm"].get(lb) if len(last4) == 4 else None
        avails = [_d(avail_q.get(x)) for x in last4]
        ttm_avail = max(avails) if ttm_ni is not None and all(avails) else None
        points.append({
            "end": _label_end(lb), "label": lb,
            "ttm_ni": ttm_ni, "ttm_avail": ttm_avail,
            "equity": eq["quarter"].get(lb), "equity_avail": _d(avail_q.get(lb)),
            "reports": [{"period": x, "rcept": r} for x in last4 for r in (used_q.get(x) or [])],
            "fs": out["fs_div_by_period"]["quarter"].get(lb),
        })
    if shares is None:
        shares = []
        for lb, d in (out.get("shares_by_period") or {}).get("quarter", {}).items():
            if d and d.get("common"):
                end = _label_end(lb)
                shares.append({"stlm": _d(d.get("stlm_dt")) or end, "common": d["common"], "rcept": end, "rid": lb,
                               "source": d.get("source")})
    return {"code": code, "market": out.get("market", "KR"), "name": out.get("name") or "", "fs_div": out.get("fs_div"),
            "points": points, "shares": shares, "acc_mt": out.get("fiscal_month") or 12, "updated_at": out.get("cache_updated_at")}


def build_series(code, cache=None):
    """한국 저장본 → 분기별 점 목록(오래된 순). 저장본이 없으면 None.
    점: {end, label, ttm_ni, equity, avail(TTM 이 공시된 날), equity_avail, reports}
    shares: [{stlm, common, rcept}] (DART 주식총수, 공시 접수일 기준 as-of 선택용)"""
    cache = cache or kr_financials._load_cache(code)
    if not cache or not cache.get("acc_mt"):
        return None
    out = kr_financials.compute(cache, kr_financials.MAX_YEARS)
    shares = []
    reports = cache.get("reports", {})
    for rid, sh in (cache.get("shares") or {}).items():
        if sh.get("status") != "ok" or sh.get("common") is None:
            continue
        rc = (reports.get(rid) or {}).get("rcept_no", "")
        shares.append({"stlm": _d(sh.get("stlm_dt")), "common": sh["common"], "rcept": _d(rc), "rid": rid})
    shares = [x for x in shares if x["stlm"] and x["rcept"]]
    s = series_from_output(code, out, shares)
    s["name"] = s["name"] or cache.get("corp_name_dart", "")
    return s


def get_series(code, refresh=False):
    """메모리 캐시(저장본 updated_at 이 바뀌면 다시 계산)"""
    cache = kr_financials._load_cache(code)
    if not cache or not cache.get("acc_mt"):
        return None
    key = (code, cache.get("updated_at"))
    with _series_lock:
        hit = _series_cache.get(code)
        if hit and hit[0] == key and not refresh:
            return hit[1]
    s = build_series(code, cache)
    with _series_lock:
        _series_cache[code] = (key, s)
    return s


def shares_asof(series, asof):
    """기준일에 공시돼 있던 주식총수 중 결산기준일이 기준일 이전인 가장 최근 값 (없으면 None)"""
    cands = [x for x in series["shares"] if x["rcept"] <= asof and x["stlm"] <= asof]
    if not cands:
        return None
    best = max(cands, key=lambda x: (x["stlm"], x["rcept"]))
    return {"common": best["common"], "stlm_dt": best["stlm"].isoformat(), "rid": best["rid"]}


def valuation_asof(code, asof=None, series=None):
    """기준일 기준 TTM 지배주주순이익·최근 분기 말 지배주주지분·유통주식수.
    반환: {asof, ttm_ni, ttm_end, equity, equity_end, shares, reports, reason} (없는 값은 None)"""
    asof = asof or date.today()
    if isinstance(asof, datetime):
        asof = asof.date()
    series = series or get_series(code)
    out = {"basis": BASIS, "asof": asof.isoformat(), "ttm_ni": None, "ttm_end": None, "equity": None, "equity_end": None,
           "shares": None, "reports": [], "reason": None}
    if not series:
        out["reason"] = "no_cache"
        return out
    pts = [p for p in series["points"] if p["ttm_avail"] and p["ttm_avail"] <= asof and p["end"] <= asof]
    if pts:
        p = max(pts, key=lambda x: x["end"])
        out.update(ttm_ni=p["ttm_ni"], ttm_end=p["label"], reports=p["reports"], fs_div=p["fs"])
    else:
        # 4개 분기가 모두 없거나 아직 공시 전
        any_q = [p for p in series["points"] if p["equity_avail"] and p["equity_avail"] <= asof]
        out["reason"] = "insufficient_quarters" if any_q else "not_yet_filed"
    eqs = [p for p in series["points"] if p["equity"] is not None and p["equity_avail"] and p["equity_avail"] <= asof and p["end"] <= asof]
    if eqs:
        q = max(eqs, key=lambda x: x["end"])
        out.update(equity=q["equity"], equity_end=q["label"])
    out["shares"] = shares_asof(series, asof)
    return out


def per_pbr(mcap, v):
    """시가총액과 valuation_asof 결과 → (PER, PBR). 분모가 0 이하이면 None"""
    per = round(mcap / v["ttm_ni"], 2) if mcap and v.get("ttm_ni") and v["ttm_ni"] > 0 else None
    pbr = round(mcap / v["equity"], 2) if mcap and v.get("equity") and v["equity"] > 0 else None
    return per, pbr
