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

def _shares_map():
    """현재 상장주식수(주) — KIS 종목마스터 (stock_profile 요약과 같은 출처)"""
    m = {}
    for market, col in (("KOSPI", "상장주수"), ("KOSDAQ", "상장 주수(천)")):
        df = download_kr_stock_master(market)
        for _, row in df.iterrows():
            try:
                n = float(str(row.get(col, "")).replace(",", "").strip())
            except ValueError:
                continue
            if n > 0:
                m[str(row["단축코드"]).strip()] = int(n * 1000)
    return m


def _corp_map():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dart_corp_map.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def _is_quota(err):
    return "status 020" in (err or "")


def ensure_financials(code, corp_code, years=1):
    """kr_financials 저장본이 없거나 모자라면 받아온다(호출 간격·재시도는 kr_financials 규칙). 반환: ok | quota | failed:<이유>"""
    import kr_financials
    if not kr_financials.needs_fetch(code, years):
        return "ok"
    job = kr_financials.fetch_blocking(code, corp_code, years)
    if job["state"] == "done":
        return "ok"
    err = job.get("error") or "unknown"
    return "quota" if _is_quota(err) else f"failed:{err}"


def prefetch_financials(universe, years=1, workers=3, log=print):
    """저장본 없는 종목을 미리 받는다. 한도 초과(020)를 만나면 남은 종목은 건너뛴다. 반환: {code: 상태}"""
    import kr_financials
    from concurrent.futures import ThreadPoolExecutor, as_completed
    corp = _corp_map()
    todo = [u["code"] for u in universe if kr_financials.needs_fetch(u["code"], years)]
    log(f"[screener] 저장본 받기: {len(todo)}종목 (종목당 약 19회 호출, {workers}개 동시) 예상 {len(todo) * 19 * 0.9 / workers / 60:.0f}분")
    status, quota_hit = {}, False
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {}
        for code in todo:
            info = corp.get(code)
            if not info:
                status[code] = "failed:corp_code 없음"
                continue
            futs[ex.submit(ensure_financials, code, info["corp_code"], years)] = code
        for n, fut in enumerate(as_completed(futs), 1):
            code = futs[fut]
            try:
                status[code] = fut.result()
            except Exception as e:
                status[code] = f"failed:{type(e).__name__}"
            if status[code] == "quota" and not quota_hit:
                quota_hit = True
                log(f"[screener] DART 일일 한도 초과 감지 ({code}) — 남은 종목은 이번 실행에서 빈 값")
                for f in futs:
                    f.cancel()
            if n % 25 == 0:
                log(f"[screener] 받기 진행 {n}/{len(futs)} ({time.time() - t0:.0f}s)")
    for f, code in futs.items():
        if code not in status:
            status[code] = "quota" if quota_hit else "failed:cancelled"
    log(f"[screener] 받기 완료: ok {sum(1 for v in status.values() if v == 'ok')}, "
        f"quota {sum(1 for v in status.values() if v == 'quota')}, 실패 {sum(1 for v in status.values() if v.startswith('failed'))} ({time.time() - t0:.0f}s)")
    return status


def collect_valuations(universe, bands, wics, sleep=0.3, limit=None, fetch_status=None, shares_map=None):
    """PER = 현재 시가총액 / 오늘 기준 TTM 지배주주순이익, PBR = 현재 시가총액 / 최근 분기 말 지배주주지분 (valuation_ttm)."""
    import valuation_ttm
    from datetime import date as _date
    rows = universe if limit is None else universe[:limit]
    total = len(rows)
    shares_map = shares_map if shares_map is not None else _shares_map()
    fetch_status = fetch_status or {}
    today = _date.today()
    items, per_missing = [], {}
    for i, u in enumerate(rows):
        code = u["code"]
        price = _safe_price(code)
        nav = _safe_naver(code)
        fwd = (nav or {}).get("forward") or {}
        feps = fwd.get("eps")
        fdps = fwd.get("dps") or 0

        v = valuation_ttm.valuation_asof(code, today)
        shares_now = shares_map.get(code)
        mcap = price * shares_now if price and shares_now else None
        trailing_per, trailing_pbr = valuation_ttm.per_pbr(mcap, v)
        if trailing_per is None:
            st = fetch_status.get(code, "")
            per_missing[code] = ("no_cache_quota" if st == "quota" else "no_cache_failed" if st.startswith("failed") else "no_cache") \
                if v["reason"] == "no_cache" else (v["reason"] or ("loss" if v["ttm_ni"] is not None and v["ttm_ni"] <= 0 else
                                                   "no_price" if not price else "no_shares" if not shares_now else "unknown"))
        latest_bps = (v["equity"] / shares_now) if v.get("equity") and shares_now else None
        fper = _ratio(price, feps)

        fbps_dart = None
        if latest_bps is not None and feps is not None:
            fbps_dart = latest_bps + feps - fdps        # 출발점: 최근 분기 말 BPS
        fpbr = _ratio(price, fbps_dart)

        band = bands.get(code)
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
    return items, per_missing


def build_and_save(min_cap_eok=3000, sleep=0.3, limit=None, prefetch=True):
    universe = get_universe(min_cap_eok)
    rows = universe if limit is None else universe[:limit]
    fetch_status = prefetch_financials(rows, years=1) if prefetch else {}
    bands = load_bands()
    wics = _load_wics_cache()
    shares_map = _shares_map()
    items, per_missing = collect_valuations(rows, bands, wics, sleep=sleep, limit=None, fetch_status=fetch_status, shares_map=shares_map)
    failed = sorted(c for c, st in fetch_status.items() if st != "ok")
    if failed:
        print(f"[screener] 저장본 받기 실패 {len(failed)}종목: {failed[:30]}{' ...' if len(failed) > 30 else ''}", flush=True)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "min_cap_eok": min_cap_eok,
        "count": len(items),
        "items": items,
        "basis": "ttm",                       # PER/PBR 기준: 최근 4개 분기 합계(공시 시점 기준), PBR 은 최근 분기 말 지배주주지분
        "bands_generated_at": None,
        "fetch_failed": {c: fetch_status[c] for c in failed},
        "per_missing": per_missing,           # PER 빈 값 이유: no_cache(_quota/_failed) | loss | insufficient_quarters | not_yet_filed | ...
    }
    try:
        with open(os.path.join("cache", "valuation_bands.json"), "r", encoding="utf-8") as f:
            payload["bands_generated_at"] = json.load(f).get("generated_at")
    except Exception:
        pass
    os.makedirs("cache", exist_ok=True)
    path = os.path.join("cache", "valuation_screener.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return payload, path

if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    if limit <= 0:
        limit = None
    payload, path = build_and_save(3000, sleep=0.3, limit=limit)
    print("저장:", path, "/ 수집:", payload["count"], "종목 / basis:", payload["basis"],
          "/ PER 있음:", sum(1 for it in payload["items"] if it["per"] is not None),
          "/ PER 빈 값 이유:", json.dumps({k: sum(1 for v in payload["per_missing"].values() if v == k) for k in set(payload["per_missing"].values())}, ensure_ascii=False))
    for it in payload["items"][:5]:
        print(json.dumps(it, ensure_ascii=False))
