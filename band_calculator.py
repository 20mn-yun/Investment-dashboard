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

def _month_end(y, m):
    import calendar
    return datetime.date(y, m, calendar.monthrange(y, m)[1])


def compute_band(code, market, shares, end_year=None, series=None):
    """월별 PER/PBR (4분기누적·공시 시점 기준). 그 월말 시가총액(월말 종가 × 그 시점 유통주식수) ÷ 그 월말 기준 TTM 지배주주순이익,
    PBR 은 그 시점 최근 분기 말 지배주주지분. 분모가 0 이하인 달은 빈 값(백분위에서 제외). 결과 필드 이름은 기존과 같다."""
    import valuation_ttm
    series = series or valuation_ttm.get_series(code, refresh=True)
    if not series:
        return None
    today = datetime.date.today()
    try:
        hist = yf.Ticker(_yf_symbol(code, market)).history(period="5y", interval="1mo", auto_adjust=False)
    except Exception:
        return None
    per_series, pbr_series, rows = [], [], []
    approx_months = 0
    for ts, row in hist.iterrows():
        price = float(row.get("Close", 0) or 0)
        if price <= 0:
            continue
        asof = min(_month_end(ts.year, ts.month), today)
        v = valuation_ttm.valuation_asof(code, asof, series)
        sh = v["shares"]["common"] if v.get("shares") else shares     # 공시 주식수 없으면 현재 주식수로 근사
        if not v.get("shares"):
            approx_months += 1
        mcap = price * sh if sh else None
        per, pbr = valuation_ttm.per_pbr(mcap, v)
        rows.append({"date": ts.strftime("%Y-%m"), "per": per, "pbr": pbr})
        if per is not None:
            per_series.append(per)
        if pbr is not None:
            pbr_series.append(pbr)
    if not rows:
        return None
    now = valuation_ttm.valuation_asof(code, today, series)
    latest_eps = round(now["ttm_ni"] / shares) if now.get("ttm_ni") is not None and shares else None
    latest_bps = round(now["equity"] / shares, 1) if now.get("equity") is not None and shares else None
    # 연도별 값: 각 회계연도 말 기준 TTM EPS·BPS (그 시점 주식수)
    eps_by_year, bps_by_year = {}, {}
    acc_mt = series["acc_mt"]
    for y in range(today.year - 6, today.year + 1):
        fy_end = _month_end(y, acc_mt)
        if fy_end > today:
            continue
        vy = valuation_ttm.valuation_asof(code, fy_end, series)
        shy = vy["shares"]["common"] if vy.get("shares") else shares
        if vy.get("ttm_ni") is not None and shy:
            eps_by_year[y] = round(vy["ttm_ni"] / shy)
        if vy.get("equity") is not None and shy:
            bps_by_year[y] = round(vy["equity"] / shy, 1)
    return {
        "code": code,
        "market": market,
        "shares": shares,
        "latest_eps_year": int(now["ttm_end"][:4]) if now.get("ttm_end") else (max(eps_by_year) if eps_by_year else None),
        "latest_eps": latest_eps,
        "latest_bps": latest_bps,
        "eps_by_year": eps_by_year,
        "bps_by_year": bps_by_year,
        "per_band": _percentiles(per_series),
        "pbr_band": _percentiles(pbr_series),
        "series": rows,
        "basis": valuation_ttm.BASIS,
        "ttm_end": now.get("ttm_end"),
        "approx_share_months": approx_months,
        "computed_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }


_BANDS_PATH = os.path.join("cache", "valuation_bands.json")
PRIORITY_CODES = ["005930", "010140", "007370", "105560", "005380"]   # 표본 검증용 — 먼저 계산


def _load_existing_bands():
    try:
        with open(_BANDS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"bands": {}}


def _write_bands(payload):
    os.makedirs("cache", exist_ok=True)
    tmp = _BANDS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _BANDS_PATH)


def build_all_bands(min_cap_eok=3000, sleep=0.3, limit=None, years=5):
    """전 종목 밴드를 TTM 기준으로 다시 계산. 기존 파일에 종목별로 덮어쓰며(계산 안 된 종목은 옛 값 유지) 25종목마다 저장.
    DART 일일 한도 초과(020)를 만나면 멈추고 저장 — 다음 실행에서 이어서 계산한다."""
    from valuation_screener import get_universe, ensure_financials, _corp_map, _is_quota
    import valuation_ttm
    universe = get_universe(min_cap_eok)
    shares_map = build_shares_map()
    corp = _corp_map()
    rows = universe if limit is None else universe[:limit]
    order = sorted(rows, key=lambda u: (u["code"] not in PRIORITY_CODES, PRIORITY_CODES.index(u["code"]) if u["code"] in PRIORITY_CODES else 0))
    existing = _load_existing_bands()
    bands = dict(existing.get("bands", {}))
    # 워치리스트 등 이미 TTM 으로 계산된 종목은 건너뛰지 않고 다시 계산(주가·공시 갱신 반영)
    total = len(order)
    done = fail = 0
    stopped = None
    t0 = time.time()
    try:
        from datetime import date as _date
        if existing.get("basis") == valuation_ttm.BASIS and existing.get("generated_at", "")[:10] == _date.today().isoformat():
            skip = {c for c, b in bands.items() if b.get("basis") == valuation_ttm.BASIS and (b.get("computed_at") or "")[:10] == _date.today().isoformat()}
        else:
            skip = set()
    except Exception:
        skip = set()
    if skip:
        print(f"[band] 오늘 이미 TTM 계산된 {len(skip)}종목은 건너뜀(이어하기)", flush=True)

    def _payload(final):
        return {
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "min_cap_eok": min_cap_eok,
            "count": len(bands),
            "bands": bands,
            "basis": valuation_ttm.BASIS,
            "progress": {"ttm_done": sum(1 for b in bands.values() if b.get("basis") == valuation_ttm.BASIS),
                         "total": total, "finished": final, "stopped_reason": stopped},
        }

    for i, u in enumerate(order):
        code = u["code"]
        if code in skip:
            continue
        info = corp.get(code)
        st = ensure_financials(code, info["corp_code"], years) if info else "failed:corp_code 없음"
        if st == "quota":
            stopped = f"DART 일일 한도 초과 ({code} 에서 중단, {i}/{total})"
            print(f"[band] {stopped}", flush=True)
            break
        if st != "ok":
            print(f"[band] {code} 저장본 받기 실패: {st}", flush=True)
        try:
            b = compute_band(code, u["market"], shares_map.get(code))
        except Exception as e:
            print(f"[band] {code} 계산 예외: {type(e).__name__}: {e}", flush=True)
            b = None
        if b is not None:
            bands[code] = b
            done += 1
        else:
            fail += 1
        if (i + 1) % 25 == 0 or (i + 1) == len(PRIORITY_CODES):   # 표본 종목이 끝나면 바로 한 번 저장
            _write_bands(_payload(False))
            print(f"  진행 {i+1}/{total} (성공 {done}, 실패 {fail}, {time.time() - t0:.0f}s)", flush=True)
        time.sleep(sleep)
    payload = _payload(stopped is None)
    _write_bands(payload)
    print(f"[band] 저장: TTM 밴드 {payload['progress']['ttm_done']}/{total}종목, 이번 실행 성공 {done} 실패 {fail}, {time.time() - t0:.0f}s"
          + (f", 중단: {stopped}" if stopped else ""), flush=True)
    return payload, _BANDS_PATH

if __name__ == "__main__":
    import sys
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    if limit <= 0:
        limit = None
    payload, path = build_all_bands(3000, sleep=0.3, limit=limit)
    print("저장:", path, "/ 밴드 산출:", payload["count"], "종목 / basis:", payload.get("basis"), "/ progress:", payload.get("progress"))
    first = payload["bands"].get("005930") or next(iter(payload["bands"].values()))
    print("샘플 종목:", first["code"], "/ series 길이:", len(first["series"]))
    print("series 앞 2개:", first["series"][:2])
    print("series 뒤 2개:", first["series"][-2:])
