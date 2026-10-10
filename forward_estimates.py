"""컨센서스(예상 실적) — fPER·fPBR 출처.

한국: FnGuide 종목 페이지(comp.fnguide.com)를 먼저 시도하고, 차단·실패하면 네이버 모바일 API로 대체(출처 표시).
  - 네이버 integration: cnsEps(올해E EPS), bps, consensusInfo(투자의견 평균 1~5, 목표주가 평균, 기준일)
  - 네이버 finance/annual: isConsensus=Y 연도의 매출액·영업이익·당기순이익·EPS·BPS (억원·원)
미국: yfinance earnings_estimate 0y(진행 중 회계연도) EPS, 없으면 forwardEps(+1y, 기준 연도 표시). 추정 BPS 없음.
종목별 하루 1회 저장(cache/forward_estimates/<MARKET>_<code>.json), 호출 간격 0.5초. 전 종목 배치는 하지 않는다.
"""
import json
import os
import re
import threading
import time
from datetime import date, datetime

import requests

_BD = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(_BD, "cache", "forward_estimates")
MIN_INTERVAL = 0.5
UA_PC = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
         "Chrome/131.0.0.0 Safari/537.36")
UA_MOBILE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"
FNGUIDE_URL = "https://comp.fnguide.com/SVO2/ASP/SVD_Main.asp?pGB=1&gicode=A{code}"
NAVER_INTEGRATION = "https://m.stock.naver.com/api/stock/{code}/integration"
NAVER_ANNUAL = "https://m.stock.naver.com/api/stock/{code}/finance/annual"

_lock = threading.Lock()
_last_call = [0.0]
_mem = {}


def _throttle():
    with _lock:
        wait = MIN_INTERVAL - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()


def _num(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).replace(",", "").replace("배", "").replace("원", "").replace("%", "").strip()
    if t in ("", "-", "N/A", "n/a", "null"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _path(market, code):
    return os.path.join(CACHE_DIR, f"{market}_{code}.json")


def _load(market, code):
    try:
        with open(_path(market, code), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save(d):
    os.makedirs(CACHE_DIR, exist_ok=True)
    p = _path(d["market"], d["code"])
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def _empty(market, code):
    return {"market": market, "code": code, "source": None, "asof": None, "this_year": None, "next_year": None,
            "years": {}, "eps_this": None, "bps_this": None, "eps_next": None,
            "target_price_mean": None, "opinion_mean": None, "opinion_scale": None, "analysts_n": None,
            "error": None, "fetched_at": None}


# ===== 한국 =====
def _fiscal_label(acc_mt, today=None):
    """진행 중인 회계연도 라벨 (올해E, 내년E). 결산 12월이면 올해 = 달력 연도."""
    today = today or date.today()
    y = today.year if today.month <= acc_mt else today.year + 1
    return f"{y}E", f"{y + 1}E"


def _fnguide(code):
    """FnGuide 종목 페이지. 차단(오류 페이지)이면 None."""
    _throttle()
    r = requests.get(FNGUIDE_URL.format(code=code), headers={"User-Agent": UA_PC, "Referer": "https://comp.fnguide.com/"}, timeout=15)
    r.encoding = "utf-8"
    html = r.text
    if r.status_code != 200 or len(html) < 20000 or "error_wrap" in html[:3000] or "컨센서스" not in html:
        return None
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    out = {"years": {}}
    for tb in soup.find_all("table"):
        ths = [th.get_text(" ", strip=True) for th in tb.find_all("th")]
        years = [t for t in ths if re.search(r"20\d\d/\d\d\([AEP]\)", t)]
        if not years:
            continue
        rows = {}
        for tr in tb.find_all("tr"):
            th = tr.find("th")
            if not th:
                continue
            tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            rows[th.get_text(" ", strip=True)] = tds
        for i, y in enumerate(years):
            lb = y[:4] + ("E" if "(E)" in y else "A")
            yd = out["years"].setdefault(lb, {})
            for key, names in (("revenue", ("매출액",)), ("operating_income", ("영업이익",)), ("net_income", ("당기순이익", "지배주주순이익")),
                               ("eps", ("EPS",)), ("bps", ("BPS",))):
                for nm in names:
                    for rk, vals in rows.items():
                        if rk.startswith(nm) and i < len(vals) and key not in yd:
                            v = _num(vals[i])
                            if v is not None:
                                yd[key] = v * 1e8 if key in ("revenue", "operating_income", "net_income") else v
    if not out["years"]:
        return None
    txt = soup.get_text(" ", strip=True)
    m = re.search(r"투자의견\s*([0-9.]+)", txt)
    out["opinion_mean"] = _num(m.group(1)) if m else None
    m = re.search(r"목표주가\s*([0-9,]+)", txt)
    out["target_price_mean"] = _num(m.group(1)) if m else None
    out["opinion_scale"] = "fnguide(1~5, 높을수록 매수)"
    return out


def _naver(code):
    """네이버 모바일 API: integration(컨센서스 EPS·목표주가·투자의견) + finance/annual(연도별 E)."""
    H = {"User-Agent": UA_MOBILE}
    _throttle()
    d = requests.get(NAVER_INTEGRATION.format(code=code), headers=H, timeout=15).json()
    ti = {x.get("code"): x.get("value") for x in d.get("totalInfos") or []}
    cons = d.get("consensusInfo") or {}
    out = {"years": {}, "name": d.get("stockName"),
           "cns_eps": _num(ti.get("cnsEps")), "bps_trailing": _num(ti.get("bps")),
           "target_price_mean": _num(cons.get("priceTargetMean")), "opinion_mean": _num(cons.get("recommMean")),
           "opinion_scale": "naver(1~5, 높을수록 매수)", "asof": cons.get("createDate")}
    _throttle()
    a = requests.get(NAVER_ANNUAL.format(code=code), headers=H, timeout=15).json()
    fi = a.get("financeInfo") or {}
    keys = {t["key"]: (t.get("title", ""), t.get("isConsensus") == "Y") for t in fi.get("trTitleList") or []}
    names = {"매출액": "revenue", "영업이익": "operating_income", "당기순이익": "net_income", "지배주주순이익": "net_income_owner",
             "EPS": "eps", "BPS": "bps", "주당배당금": "dps"}
    for row in fi.get("rowList") or []:
        key = names.get((row.get("title") or "").strip())
        if not key:
            continue
        for k, cell in (row.get("columns") or {}).items():
            title, is_e = keys.get(k, ("", False))
            v = _num(cell.get("value") if isinstance(cell, dict) else cell)
            if v is None:
                continue
            lb = f"{k[:4]}{'E' if is_e else 'A'}"
            yd = out["years"].setdefault(lb, {"period_end": title.rstrip(".")})
            yd[key] = v * 1e8 if key in ("revenue", "operating_income", "net_income", "net_income_owner") else v
    return out


def kr_consensus(code, acc_mt=12, refresh=False):
    """한국 컨센서스. 하루 1회 저장본 재사용."""
    code = str(code).strip()
    key = ("KR", code)
    today = date.today().isoformat()
    cached = _mem.get(key) or _load("KR", code)
    if cached and not refresh and (cached.get("fetched_at") or "")[:10] == today:
        _mem[key] = cached
        return cached
    out = _empty("KR", code)
    this_lb, next_lb = _fiscal_label(acc_mt)
    out.update(this_year=this_lb, next_year=next_lb)
    data, source, err = None, None, None
    try:
        data = _fnguide(code)
        source = "fnguide" if data else None
    except Exception as e:
        err = f"fnguide {type(e).__name__}"
    if data is None:
        try:
            data = _naver(code)
            source = "naver"
        except Exception as e:
            err = (err + "; " if err else "") + f"naver {type(e).__name__}"
            data = None
    if data:
        out["source"] = source
        out["years"] = data.get("years") or {}
        out["target_price_mean"] = data.get("target_price_mean")
        out["opinion_mean"] = data.get("opinion_mean")
        out["opinion_scale"] = data.get("opinion_scale")
        out["asof"] = data.get("asof") or today
        out["name"] = data.get("name")
        ty, ny = out["years"].get(this_lb) or {}, out["years"].get(next_lb) or {}
        out["eps_this"] = ty.get("eps") if ty.get("eps") is not None else data.get("cns_eps")
        out["bps_this"] = ty.get("bps")
        out["eps_next"] = ny.get("eps")
        out["bps_source"] = "estimate" if ty.get("bps") is not None else None
    out["error"] = err
    out["fetched_at"] = datetime.now().isoformat(timespec="seconds")
    if data or not cached:
        _save(out)
        _mem[key] = out
        return out
    return cached


# ===== 미국 =====
def us_consensus(code, refresh=False):
    code = str(code).strip().upper()
    key = ("US", code)
    today = date.today().isoformat()
    cached = _mem.get(key) or _load("US", code)
    if cached and not refresh and (cached.get("fetched_at") or "")[:10] == today:
        _mem[key] = cached
        return cached
    out = _empty("US", code)
    try:
        import yfinance as yf
        import watchlist as watchlist_store
        t = yf.Ticker(watchlist_store.to_source_ticker(code, "yahoo"))
        info = t.info or {}
        fye = info.get("nextFiscalYearEnd") or info.get("lastFiscalYearEnd")
        fy_end = datetime.utcfromtimestamp(fye).date() if isinstance(fye, (int, float)) else None
        # 0y = 진행 중 회계연도 (nextFiscalYearEnd 에 끝남)
        this_lb = f"FY{fy_end.year}.{fy_end.month:02d}E" if fy_end else "0yE"
        next_lb = f"FY{fy_end.year + 1}.{fy_end.month:02d}E" if fy_end else "+1yE"
        out.update(this_year=this_lb, next_year=next_lb, source="yfinance", asof=today)
        try:
            ee = t.earnings_estimate
        except Exception:
            ee = None
        if ee is not None and len(ee):
            for idx, lb in (("0y", this_lb), ("+1y", next_lb)):
                if idx in ee.index:
                    row = ee.loc[idx]
                    out["years"][lb] = {"eps": _num(row.get("avg")), "eps_low": _num(row.get("low")), "eps_high": _num(row.get("high")),
                                        "analysts_n": int(row.get("numberOfAnalysts")) if _num(row.get("numberOfAnalysts")) else None}
        try:
            re_ = t.revenue_estimate
            if re_ is not None and len(re_):
                for idx, lb in (("0y", this_lb), ("+1y", next_lb)):
                    if idx in re_.index and lb in out["years"]:
                        out["years"][lb]["revenue"] = _num(re_.loc[idx].get("avg"))
        except Exception:
            pass
        ty = out["years"].get(this_lb) or {}
        if ty.get("eps") is not None:
            out["eps_this"] = ty["eps"]
            out["analysts_n"] = ty.get("analysts_n")
            out["eps_basis"] = f"earnings_estimate 0y ({this_lb})"
        elif _num(info.get("forwardEps")) is not None:
            out["eps_this"] = _num(info.get("forwardEps"))
            out["eps_basis"] = f"forwardEps ({next_lb}, 차기 회계연도)"
            out["eps_this_label"] = next_lb
        out["eps_next"] = (out["years"].get(next_lb) or {}).get("eps")
        out["bps_this"] = None                      # 추정 BPS 없음
        out["target_price_mean"] = _num(info.get("targetMeanPrice"))
        out["opinion_mean"] = _num(info.get("recommendationMean"))
        out["opinion_scale"] = "yfinance(1~5, 낮을수록 매수)"
        if out["analysts_n"] is None:
            out["analysts_n"] = info.get("numberOfAnalystOpinions")
        out["currency"] = info.get("currency") or "USD"
    except Exception as e:
        out["error"] = f"{type(e).__name__}"
    out["fetched_at"] = datetime.now().isoformat(timespec="seconds")
    if out.get("source") or not cached:
        _save(out)
        _mem[key] = out
        return out
    return cached


def fper_fpbr(price, est):
    """현재 주가 ÷ 올해E EPS, ÷ 올해E BPS. 분모 0 이하·없음 → None"""
    if not price or not est:
        return None, None
    eps, bps = est.get("eps_this"), est.get("bps_this")
    fper = round(price / eps, 2) if eps and eps > 0 else None
    fpbr = round(price / bps, 2) if bps and bps > 0 else None
    return fper, fpbr


def summary_fields(market, code, price, acc_mt=12):
    """/api/stock/summary 용 요약 필드"""
    est = kr_consensus(code, acc_mt) if market == "KR" else us_consensus(code)
    fper, fpbr = fper_fpbr(price, est)
    return {"fper": fper, "fpbr": fpbr, "fsource": est.get("source"), "f_year": est.get("eps_this_label") or est.get("this_year"),
            "f_eps": est.get("eps_this"), "f_bps": est.get("bps_this"),
            "target_price_mean": est.get("target_price_mean"), "opinion_mean": est.get("opinion_mean"),
            "opinion_scale": est.get("opinion_scale"), "analysts_n": est.get("analysts_n"), "f_asof": est.get("asof"),
            "f_years": est.get("years") or {}}
