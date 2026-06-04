import os
import time
import json
import datetime
import numpy as np
import yfinance as yf
from earnings_tracker import get_corp_code, _fetch_fnltt_raw
from quarterly_eps_tracker import _extract_eps
from kis_api import download_kr_stock_master

ANN_REPORT = "11011"

def _extract_equity(data):
    if not data:
        return None
    controlling = None
    total = None
    for item in data.get("list", []):
        if item.get("sj_div") != "BS":
            continue
        nm = (item.get("account_nm") or "").replace("　", " ").strip()
        raw = (item.get("thstrm_amount") or "").replace(",", "").strip()
        if not raw or raw == "-":
            continue
        try:
            num = float(raw)
        except ValueError:
            continue
        if "비지배" in nm:
            continue
        if "지배" in nm and ("지분" in nm or "자본" in nm):
            if controlling is None:
                controlling = num
        elif nm == "자본총계":
            if total is None:
                total = num
    return controlling if controlling is not None else total

def build_shares_map():
    m = {}
    for market, col in [("KOSPI", "상장주수"), ("KOSDAQ", "상장 주수(천)")]:
        df = download_kr_stock_master(market)
        for _, row in df.iterrows():
            raw = str(row.get(col, "")).replace(",", "").strip()
            try:
                s = float(raw)
            except ValueError:
                continue
            if s > 0:
                m[str(row["단축코드"]).strip()] = s * 1000.0
    return m

def _yf_symbol(code, market):
    return code + (".KQ" if market == "KOSDAQ" else ".KS")

def _applicable_fy(year, month):
    return year - 1 if month >= 4 else year - 2

def _percentiles(series):
    if len(series) < 24:
        return None
    arr = np.array(series, dtype=float)
    qs = np.percentile(arr, [10, 25, 50, 75, 90])
    return {"p10": round(float(qs[0]), 2), "p25": round(float(qs[1]), 2),
            "p50": round(float(qs[2]), 2), "p75": round(float(qs[3]), 2),
            "p90": round(float(qs[4]), 2), "n": len(series)}

def compute_band(code, market, shares, end_year=None):
    corp = get_corp_code(code)
    if not corp:
        return None
    if end_year is None:
        end_year = datetime.date.today().year
    eps_by_year = {}
    bps_by_year = {}
    for year in range(end_year - 6, end_year + 1):
        data = _fetch_fnltt_raw(corp, year, ANN_REPORT)
        eps = _extract_eps(data)
        if eps is not None:
            eps_by_year[year] = eps
        eq = _extract_equity(data)
        if eq is not None and shares:
            bps_by_year[year] = eq / shares
    if not eps_by_year:
        return None
    try:
        hist = yf.Ticker(_yf_symbol(code, market)).history(period="5y", interval="1mo", auto_adjust=False)
    except Exception:
        return None
    per_series = []
    pbr_series = []
    series = []
    for ts, row in hist.iterrows():
        price = float(row.get("Close", 0) or 0)
        if price <= 0:
            continue
        fy = _applicable_fy(ts.year, ts.month)
        eps = eps_by_year.get(fy)
        bps = bps_by_year.get(fy)
        per = round(price / eps, 2) if (eps is not None and eps > 0) else None
        pbr = round(price / bps, 2) if (bps is not None and bps > 0) else None
        series.append({"date": ts.strftime("%Y-%m"), "per": per, "pbr": pbr})
        if per is not None:
            per_series.append(per)
        if pbr is not None:
            pbr_series.append(pbr)
    latest_eps_year = max(eps_by_year)
    latest_bps = round(bps_by_year[max(bps_by_year)], 1) if bps_by_year else None
    return {
        "code": code,
        "market": market,
        "shares": shares,
        "latest_eps_year": latest_eps_year,
        "latest_eps": eps_by_year[latest_eps_year],
        "latest_bps": latest_bps,
        "eps_by_year": {y: eps_by_year[y] for y in sorted(eps_by_year)},
        "bps_by_year": {y: round(v, 1) for y, v in sorted(bps_by_year.items())},
        "per_band": _percentiles(per_series),
        "pbr_band": _percentiles(pbr_series),
        "series": series,
    }

def build_all_bands(min_cap_eok=3000, sleep=0.3, limit=None):
    from valuation_screener import get_universe
    universe = get_universe(min_cap_eok)
    shares_map = build_shares_map()
    rows = universe if limit is None else universe[:limit]
    total = len(rows)
    bands = {}
    for i, u in enumerate(rows):
        code = u["code"]
        try:
            b = compute_band(code, u["market"], shares_map.get(code))
        except Exception:
            b = None
        if b is not None:
            bands[code] = b
        if (i + 1) % 25 == 0:
            print(f"  진행 {i+1}/{total} (성공 {len(bands)})", flush=True)
        time.sleep(sleep)
    payload = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "min_cap_eok": min_cap_eok,
        "count": len(bands),
        "bands": bands,
    }
    os.makedirs("cache", exist_ok=True)
    path = os.path.join("cache", "valuation_bands.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload, path

if __name__ == "__main__":
    import sys
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    if limit <= 0:
        limit = None
    payload, path = build_all_bands(3000, sleep=0.3, limit=limit)
    print("저장:", path, "/ 밴드 산출:", payload["count"], "종목")
    first = next(iter(payload["bands"].values()))
    print("샘플 종목:", first["code"], "/ series 길이:", len(first["series"]))
    print("series 앞 2개:", first["series"][:2])
    print("series 뒤 2개:", first["series"][-2:])
