import os
import sys
import json
import time
import requests
import pandas as pd
from datetime import datetime
from bs4 import BeautifulSoup
from kis_api import download_kr_stock_master, get_current_price
from stock_leaders import _load_wics_cache

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}

def _num(s):
    if s is None:
        return None
    t = s.replace(",", "").strip()
    if t in ("", "-", "N/A", "n/a"):
        return None
    try:
        return float(t)
    except ValueError:
        return None

def _metric_key(label):
    if label.startswith("EPS"):
        return "eps"
    if label.startswith("PER"):
        return "per"
    if label.startswith("BPS"):
        return "bps"
    if label.startswith("PBR"):
        return "pbr"
    if label.startswith("주당배당"):
        return "dps"
    return None

def fetch_naver_valuation(code):
    url = f"https://finance.naver.com/item/main.naver?code={code}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
    except requests.RequestException:
        return None
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")
    table = None
    for t in soup.find_all("table"):
        if "기업실적분석" in t.get("summary", ""):
            table = t
            break
    if table is None:
        return None
    rows = table.find_all("tr")
    annual_count = 0
    for tr in rows:
        for th in tr.find_all("th"):
            if "최근 연간 실적" in th.get_text():
                annual_count = int(th.get("colspan", "0") or 0)
        if annual_count:
            break
    period_labels = []
    for tr in rows:
        texts = [th.get_text(" ", strip=True) for th in tr.find_all("th")]
        if any(("." in x and any(ch.isdigit() for ch in x)) for x in texts):
            period_labels = texts
            break
    if not period_labels or not annual_count:
        return None
    annual_labels = period_labels[:annual_count]
    forward_idx = None
    trailing_idx = None
    for i, lab in enumerate(annual_labels):
        if "(E)" in lab:
            forward_idx = i
        else:
            trailing_idx = i
    fwd = {}
    trl = {}
    for tr in rows:
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        key = _metric_key(cells[0].get_text(" ", strip=True))
        if not key:
            continue
        values = [c.get_text(" ", strip=True) for c in cells[1:]]
        if forward_idx is not None and forward_idx < len(values):
            fwd[key] = _num(values[forward_idx])
        if trailing_idx is not None and trailing_idx < len(values):
            trl[key] = _num(values[trailing_idx])
    result = {"code": code, "trailing": None, "forward": None}
    if trailing_idx is not None:
        result["trailing"] = dict(period=annual_labels[trailing_idx], **trl)
    if forward_idx is not None:
        result["forward"] = dict(period=annual_labels[forward_idx], **fwd)
    return result

def _kr_flag(df, col):
    return df[col].astype(str).str.strip()

def get_universe(min_cap_eok=3000):
    markets = {
        "KOSPI": {"cap": "시가총액", "name": "한글명", "group": "그룹코드",
                  "pref": "우선주", "spac": "SPAC", "halt": "거래정지",
                  "admin": "관리종목", "liq": "정리매매"},
        "KOSDAQ": {"cap": "전일기준 시가총액 (억)", "name": "한글종목명", "group": "증권그룹구분코드",
                   "pref": "우선주 구분 코드", "spac": "기업인수목적회사여부", "halt": "거래정지 여부",
                   "admin": "관리 종목 여부", "liq": "정리매매 여부"},
    }
    out = []
    for market, c in markets.items():
        df = download_kr_stock_master(market)
        cap = pd.to_numeric(df[c["cap"]], errors="coerce").fillna(0)
        keep = (
            (_kr_flag(df, c["group"]) == "ST")
            & (_kr_flag(df, c["pref"]) == "0")
            & (_kr_flag(df, c["spac"]) == "N")
            & (_kr_flag(df, c["halt"]) == "N")
            & (_kr_flag(df, c["admin"]) == "N")
            & (_kr_flag(df, c["liq"]) == "N")
            & (cap >= min_cap_eok)
        )
        sub = df[keep].copy()
        sub["_cap"] = cap[keep]
        for _, row in sub.iterrows():
            out.append({
                "code": str(row["단축코드"]).strip(),
                "name": str(row[c["name"]]).strip(),
                "market": market,
                "market_cap_eok": int(row["_cap"]),
            })
    out.sort(key=lambda x: x["market_cap_eok"], reverse=True)
    return out

def _ratio(price, denom):
    if price is None or denom is None or denom <= 0:
        return None
    return round(price / denom, 2)

def _safe_price(code, retries=2):
    for _ in range(retries):
        try:
            d = get_current_price(code)
            p = int(d.get("stck_prpr", 0))
            if p:
                return p
        except Exception:
            pass
        time.sleep(0.5)
    return None

def _safe_naver(code, retries=2):
    for _ in range(retries):
        v = fetch_naver_valuation(code)
        if v is not None:
            return v
        time.sleep(0.5)
    return None

def load_bands():
    path = os.path.join("cache", "valuation_bands.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f).get("bands", {})

def _position(value, vals):
    if value is None or not vals:
        return None
    count = sum(1 for v in vals if v <= value)
    return round(count / len(vals) * 100, 1)

def collect_valuations(universe, bands, wics, sleep=0.3, limit=None):
    rows = universe if limit is None else universe[:limit]
    total = len(rows)
    items = []
    for i, u in enumerate(rows):
        code = u["code"]
        price = _safe_price(code)
        nav = _safe_naver(code)
        fwd = (nav or {}).get("forward") or {}
        feps = fwd.get("eps")
        fdps = fwd.get("dps") or 0

        band = bands.get(code)
        latest_eps = band["latest_eps"] if band else None
        latest_bps = band["latest_bps"] if band else None

        trailing_per = _ratio(price, latest_eps)
        trailing_pbr = _ratio(price, latest_bps)
        fper = _ratio(price, feps)

        fbps_dart = None
        if latest_bps is not None and feps is not None:
            fbps_dart = latest_bps + feps - fdps
        fpbr = _ratio(price, fbps_dart)

        per_vals = [s["per"] for s in band["series"] if s["per"] is not None] if band else []
        pbr_vals = [s["pbr"] for s in band["series"] if s["pbr"] is not None] if band else []

        per_band = band["per_band"] if band else None
        pbr_band = band["pbr_band"] if band else None

        items.append({
            "code": code,
            "name": u["name"],
            "market": u["market"],
            "market_cap_eok": u["market_cap_eok"],
            "price": price,
            "per": trailing_per,
            "pbr": trailing_pbr,
            "fper": fper,
            "fpbr": fpbr,
            "per_p10": per_band["p10"] if per_band else None,
            "pbr_p10": pbr_band["p10"] if pbr_band else None,
            "per_position": _position(trailing_per, per_vals),
            "fper_position": _position(fper, per_vals),
            "pbr_position": _position(trailing_pbr, pbr_vals),
            "fpbr_position": _position(fpbr, pbr_vals),
            "sector": wics.get(code, {}).get("wics_mcls_nm") or "미분류",
            "sector_code": wics.get(code, {}).get("wics_mcls_cd") or "",
        })
        if (i + 1) % 50 == 0:
            print(f"  진행 {i+1}/{total}", flush=True)
        time.sleep(sleep)
    return items

def build_and_save(min_cap_eok=3000, sleep=0.3, limit=None):
    universe = get_universe(min_cap_eok)
    bands = load_bands()
    wics = _load_wics_cache()
    items = collect_valuations(universe, bands, wics, sleep=sleep, limit=limit)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "min_cap_eok": min_cap_eok,
        "count": len(items),
        "items": items,
    }
    os.makedirs("cache", exist_ok=True)
    path = os.path.join("cache", "valuation_screener.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload, path

if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    if limit <= 0:
        limit = None
    payload, path = build_and_save(3000, sleep=0.3, limit=limit)
    print("저장:", path, "/ 수집:", payload["count"], "종목")
    for it in payload["items"]:
        print(json.dumps(it, ensure_ascii=False))
