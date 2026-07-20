import json
import sys
import time
from collections import Counter
from pathlib import Path

import requests

from kis_api import (
    download_kr_stock_master,
    get_access_token,
    APP_KEY,
    APP_SECRET,
    BASE_URL,
)
from sector_leaders import winsorized_zscores

TOP600_PATH = Path("tickers/kr_top600.json")
OVERRIDES_PATH = Path("cache/stock_sector_overrides.json")
THEMES_PATH = Path("cache/stock_themes.json")
WICS_CACHE_PATH = Path("cache/stock_wics.json")

WICS_SECTORS = [
    "G10", "G15", "G20", "G25", "G30", "G35", "G40", "G45", "G50", "G55",
]

WICS_MID_SECTORS = [
    "G1010", "G1510",
    "G2010", "G2020", "G2030",
    "G2510", "G2520", "G2530", "G2550", "G2560",
    "G3010", "G3020", "G3030",
    "G3510", "G3520",
    "G4010", "G4020", "G4030", "G4040", "G4050",
    "G4510", "G4520", "G4530", "G4535", "G4540",
    "G5010", "G5020",
    "G5510",
]

WISEINDEX_BASE = "https://www.wiseindex.com/Index/GetIndexComponets"
WISEINDEX_CHART = "https://www.wiseindex.com/DataCenter/ChartData"
WICS_BENCHMARK = "WI100"

WICS_CACHE_SERIES_PATH = Path("cache/wics_sectors.json")

WICS_PERIODS = {"1w": 5, "1m": 21, "3m": 63, "6m": 126, "1y": 252}

WICS_MID_NAMES = {
    "G1010": "에너지", "G1510": "소재",
    "G2010": "자본재", "G2020": "상업서비스와공급품", "G2030": "운송",
    "G2510": "자동차와부품", "G2520": "내구소비재와의류",
    "G2530": "호텔,레스토랑,레저등", "G2550": "소매(유통)", "G2560": "교육서비스",
    "G3010": "식품과기본식료품소매", "G3020": "식품,음료,담배", "G3030": "가정용품과개인용품",
    "G3510": "건강관리장비와서비스", "G3520": "제약과생물공학",
    "G4010": "은행", "G4020": "증권", "G4030": "다각화된금융",
    "G4040": "보험", "G4050": "부동산",
    "G4510": "소프트웨어와서비스", "G4520": "기술하드웨어와장비",
    "G4530": "반도체와반도체장비", "G4535": "전자와전기제품", "G4540": "디스플레이",
    "G5010": "전기통신서비스", "G5020": "미디어와엔터테인먼트",
    "G5510": "유틸리티",
}

WICS_LCLS_FOR_MID = {
    "G1010": ("G10", "에너지"), "G1510": ("G15", "소재"),
    "G2010": ("G20", "산업재"), "G2020": ("G20", "산업재"), "G2030": ("G20", "산업재"),
    "G2510": ("G25", "경기관련소비재"), "G2520": ("G25", "경기관련소비재"),
    "G2530": ("G25", "경기관련소비재"), "G2550": ("G25", "경기관련소비재"),
    "G2560": ("G25", "경기관련소비재"),
    "G3010": ("G30", "필수소비재"), "G3020": ("G30", "필수소비재"),
    "G3030": ("G30", "필수소비재"),
    "G3510": ("G35", "건강관리"), "G3520": ("G35", "건강관리"),
    "G4010": ("G40", "금융"), "G4020": ("G40", "금융"), "G4030": ("G40", "금융"),
    "G4040": ("G40", "금융"), "G4050": ("G40", "금융"),
    "G4510": ("G45", "IT"), "G4520": ("G45", "IT"), "G4530": ("G45", "IT"),
    "G4535": ("G45", "IT"), "G4540": ("G45", "IT"),
    "G5010": ("G50", "커뮤니케이션서비스"), "G5020": ("G50", "커뮤니케이션서비스"),
    "G5510": ("G55", "유틸리티"),
}

SECTOR_NAMES = {
    "0005": "음식료·담배",
    "0006": "섬유·의류",
    "0007": "종이·목재",
    "0008": "화학",
    "0009": "제약",
    "0010": "비금속",
    "0011": "금속",
    "0012": "기계·장비",
    "0013": "전기·전자",
    "0014": "의료·정밀기기",
    "0015": "운송장비·부품",
    "0016": "유통",
    "0017": "전기·가스",
    "0018": "건설",
    "0019": "운송·창고",
    "0020": "통신",
    "0021": "금융",
    "0024": "증권",
    "0025": "보험",
    "0026": "일반서비스",
}

KOSPI_MAJOR_MAP = {
    "16": "0016",
    "17": "0017",
    "18": "0018",
    "19": "0019",
    "20": "0020",
    "26": "0026",
    "28": "0018",
    "29": "0026",
    "30": "0026",
}

KOSPI_MID_SECTORS = {str(i): f"00{i:02d}" for i in range(5, 16)}

KOSDAQ_MID_MAP = {
    "1019": "0005",
    "1020": "0006",
    "1021": "0007",
    "1023": "0008",
    "1024": "0009",
    "1025": "0010",
    "1026": "0011",
    "1027": "0012",
    "1028": "0013",
    "1029": "0014",
    "1030": "0015",
    "1031": "0026",
}

KOSDAQ_MAJOR_MAP = {
    "1006": "0009",
    "1010": "0018",
    "1011": "0026",
    "1014": "0026",
    "1015": "0026",
}


def _wics_latest_date():
    from datetime import datetime, timedelta
    for offset in range(5):
        dt = (datetime.now() - timedelta(days=offset)).strftime("%Y%m%d")
        url = f"{WISEINDEX_BASE}?ceil_yn=0&dt={dt}&sec_cd=G45"
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
            if resp.json().get("list"):
                return dt
        except Exception:
            pass
    return (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")


def fetch_wics_mapping():
    dt = _wics_latest_date()

    lcls_map = {}
    for sec_cd in WICS_SECTORS:
        url = f"{WISEINDEX_BASE}?ceil_yn=0&dt={dt}&sec_cd={sec_cd}"
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            items = resp.json().get("list", [])
            for item in items:
                code = item.get("CMP_CD", "")
                if code:
                    lcls_map[code] = {
                        "wics_lcls_cd": sec_cd,
                        "wics_lcls_nm": item.get("SEC_NM_KOR", ""),
                    }
        except Exception:
            pass
        time.sleep(0.1)

    mcls_map = {}
    for mid_cd in WICS_MID_SECTORS:
        url = f"{WISEINDEX_BASE}?ceil_yn=0&dt={dt}&sec_cd={mid_cd}"
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            items = resp.json().get("list", [])
            for item in items:
                code = item.get("CMP_CD", "")
                if code:
                    idx_nm = item.get("IDX_NM_KOR", "")
                    short_nm = idx_nm.replace("WICS ", "") if idx_nm.startswith("WICS ") else idx_nm
                    mcls_map[code] = {
                        "wics_mcls_cd": mid_cd,
                        "wics_mcls_nm": short_nm,
                    }
        except Exception:
            pass
        time.sleep(0.1)

    result = {}
    for code in (set(lcls_map) | set(mcls_map)):
        entry = {}
        if code in lcls_map:
            entry.update(lcls_map[code])
        if code in mcls_map:
            entry.update(mcls_map[code])
        if entry:
            result[code] = entry

    return result


def build_wics_cache():
    result = fetch_wics_mapping()
    WICS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(WICS_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump({"mapping": result, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}, f, ensure_ascii=False, indent=2)
    return result


def _fetch_chart_batch(index_ids, from_dt, end_dt):
    params = {
        "index_ids": ",".join(index_ids),
        "fromDT": from_dt,
        "endDT": end_dt,
        "term": 1,
        "isEnd": 1,
    }
    resp = requests.get(WISEINDEX_CHART, params=params, headers={
        "User-Agent": "Mozilla/5.0",
        "X-Requested-With": "XMLHttpRequest",
    }, timeout=15)
    rows = resp.json()
    if not isinstance(rows, list) or not rows:
        return {}

    from datetime import datetime, timezone
    dates = []
    series = {idx: [] for idx in index_ids}
    for row in rows:
        ts_ms = row.get("TRD_DT_CHART", 0)
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        dates.append(dt)
        for i, idx in enumerate(index_ids):
            val_key = f"VAL{i + 1}"
            v = row.get(val_key)
            series[idx].append(v if v is not None else 0.0)

    result = {}
    for idx in index_ids:
        result[idx] = {"dates": dates, "values": series[idx]}
    return result


def _fetch_wics_sector_volumes():
    import yfinance as yf
    import pandas as pd

    with open(TOP600_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    tickers = data.get("tickers", []) if isinstance(data, dict) else data

    ks_tickers = [t["code"] + ".KS" for t in tickers if t.get("market_sub") == "KOSPI"]
    kq_tickers = [t["code"] + ".KQ" for t in tickers if t.get("market_sub") == "KOSDAQ"]

    stock_dvol = {}
    for batch, label in [(ks_tickers, "KOSPI"), (kq_tickers, "KOSDAQ")]:
        if not batch:
            continue
        print(f"    {label} {len(batch)} tickers downloading (3y)...")
        df = yf.download(batch, period="3y", progress=False, auto_adjust=True)
        for t in batch:
            try:
                if isinstance(df.columns, pd.MultiIndex):
                    c = df["Close"][t].dropna()
                    v = df["Volume"][t].dropna()
                else:
                    c = df["Close"].dropna()
                    v = df["Volume"].dropna()
                common = c.index.intersection(v.index)
                dv = (c[common] * v[common]).dropna()
                if len(dv) > 20:
                    code = t.replace(".KS", "").replace(".KQ", "")
                    stock_dvol[code] = dv
            except Exception:
                pass

    wics_cache = _load_wics_cache()
    sector_volumes = {}
    for mid_cd in WICS_MID_NAMES:
        codes_in_sector = [c for c, w in wics_cache.items() if w.get("wics_mcls_cd") == mid_cd]
        sector_dv_list = [stock_dvol[c] for c in codes_in_sector if c in stock_dvol]
        if sector_dv_list:
            combined = pd.concat(sector_dv_list, axis=1).sum(axis=1).dropna()
            sector_volumes[mid_cd] = combined
    return sector_volumes


def _compute_volume_change(vol_series, period_days):
    if len(vol_series) < 2 * period_days:
        return None
    recent = vol_series.iloc[-period_days:]
    previous = vol_series.iloc[-2 * period_days:-period_days]
    prev_avg = previous.mean()
    if prev_avg == 0:
        return None
    return round((recent.mean() / prev_avg - 1) * 100, 2)


def fetch_wics_index_series(days=260):
    from datetime import datetime, timedelta
    end_dt = _wics_latest_date()
    start = datetime.strptime(end_dt, "%Y%m%d") - timedelta(days=int(days * 1.5))
    from_dt = start.strftime("%Y%m%d")

    all_ids = list(WICS_MID_NAMES.keys())
    result = {}

    for i in range(0, len(all_ids), 5):
        batch = all_ids[i:i + 5]
        batch_data = _fetch_chart_batch(batch, from_dt, end_dt)
        result.update(batch_data)
        time.sleep(0.2)

    return result


def _period_return_cum(cum_values, period_days):
    if len(cum_values) < period_days + 1:
        return None
    end_cum = cum_values[-1]
    start_cum = cum_values[-(period_days + 1)]
    denom = 1 + start_cum / 100
    if abs(denom) < 1e-9:
        return None
    return ((1 + end_cum / 100) / denom - 1) * 100


def _period_return_abs(prices, period_days):
    if len(prices) < period_days + 1:
        return None
    end_p = prices[-1]
    start_p = prices[-(period_days + 1)]
    if start_p is None or end_p is None or abs(start_p) < 1e-9:
        return None
    return (end_p / start_p - 1) * 100


def _load_kospi_benchmark():
    sl_path = Path("cache/sector_leaders.json")
    if not sl_path.exists():
        return [], []
    with open(sl_path, "r", encoding="utf-8") as f:
        sl = json.load(f)
    kr = sl.get("kr", {})
    bench = kr.get("series", {}).get("_benchmark", {})
    if not bench.get("values"):
        bench = kr.get("themes", {}).get("series", {}).get("_benchmark", {})
    return bench.get("dates", []), bench.get("values", [])


def _align_to_kospi(wics_dates, wics_vals, kospi_dates, kospi_vals):
    kospi_map = {}
    for d, v in zip(kospi_dates, kospi_vals):
        if v is not None:
            kospi_map[d] = v

    common_dates = [d for d in wics_dates if d in kospi_map]
    wics_map = dict(zip(wics_dates, wics_vals))

    aligned_wics = [wics_map[d] for d in common_dates]
    aligned_kospi = [kospi_map[d] for d in common_dates]
    return common_dates, aligned_wics, aligned_kospi


def _apply_wics_zscores(rows):
    rs_vals = [r.get("rs") for r in rows]
    vc_vals = [r.get("volume_change") for r in rows]
    rs_zs = winsorized_zscores(rs_vals)
    vc_zs = winsorized_zscores(vc_vals)
    for i, r in enumerate(rows):
        r["rs_z"] = rs_zs[i]
        r["vc_z"] = vc_zs[i]


def compute_wics_rs():
    print("[WICS] Fetching index series (1y)...")
    series = fetch_wics_index_series(days=260)

    kospi_dates, kospi_vals = _load_kospi_benchmark()
    print(f"  KOSPI benchmark: {len(kospi_vals)} days")

    print("[WICS] Fetching stock volumes (2y)...")
    sector_volumes = _fetch_wics_sector_volumes()
    print(f"  Sector volumes: {len(sector_volumes)} sectors")

    result = {"last_updated": time.strftime("%Y-%m-%dT%H:%M:%S"), "kr_wics": {}}

    for period_name, period_days in WICS_PERIODS.items():
        rows = []
        for mid_cd, mid_nm in WICS_MID_NAMES.items():
            sec = series.get(mid_cd, {})
            sec_dates = sec.get("dates", [])
            sec_vals = sec.get("values", [])

            common, aligned_sec, aligned_kospi = _align_to_kospi(
                sec_dates, sec_vals, kospi_dates, kospi_vals
            )

            sec_ret = _period_return_cum(aligned_sec, period_days)
            bench_ret = _period_return_abs(aligned_kospi, period_days)

            rs = None
            if sec_ret is not None and bench_ret is not None:
                rs = round(sec_ret - bench_ret, 2)

            vc = None
            vol_s = sector_volumes.get(mid_cd)
            if vol_s is not None:
                vc = _compute_volume_change(vol_s, period_days)

            lcls_cd, lcls_nm = WICS_LCLS_FOR_MID.get(mid_cd, ("", ""))
            rows.append({
                "code": mid_cd,
                "name": mid_nm,
                "lcls_code": lcls_cd,
                "lcls_name": lcls_nm,
                "sector_return": round(sec_ret, 2) if sec_ret is not None else None,
                "benchmark_return": round(bench_ret, 2) if bench_ret is not None else None,
                "rs": rs,
                "volume_change": vc,
            })
        rows.sort(key=lambda x: (x["rs"] is None, -(x["rs"] or 0)))
        _apply_wics_zscores(rows)
        result["kr_wics"][period_name] = rows

    wics_series = {"_benchmark": {"dates": kospi_dates, "values": kospi_vals}}
    for mid_cd in WICS_MID_NAMES:
        sec = series.get(mid_cd, {})
        sec_dates = sec.get("dates", [])
        sec_vals = sec.get("values", [])
        common, aligned_sec, aligned_kospi = _align_to_kospi(
            sec_dates, sec_vals, kospi_dates, kospi_vals
        )
        wics_series[mid_cd] = {"values": aligned_sec}
    wics_series["_benchmark"]["dates"] = (
        wics_series["_benchmark"]["dates"]
    )
    result["series"] = wics_series

    WICS_CACHE_SERIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(WICS_CACHE_SERIES_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"  Saved to {WICS_CACHE_SERIES_PATH}")
    return result


def _classify_kospi(row):
    major = str(row.get("지수업종대분류", "")).strip()
    mid = str(row.get("지수업종중분류", "")).strip()
    if major == "27" and mid in KOSPI_MID_SECTORS:
        return KOSPI_MID_SECTORS[mid]
    if major == "21":
        if mid == "24":
            return "0024"
        if mid == "25":
            return "0025"
        return "0021"
    if major in KOSPI_MAJOR_MAP:
        return KOSPI_MAJOR_MAP[major]
    return None


def _classify_kosdaq(row):
    major = str(row.get("지수업종 대분류 코드", "")).strip()
    mid = str(row.get("지수 업종 중분류 코드", "")).strip()
    if major == "1009" and mid in KOSDAQ_MID_MAP:
        return KOSDAQ_MID_MAP[mid]
    if major in KOSDAQ_MAJOR_MAP:
        return KOSDAQ_MAJOR_MAP[major]
    return None


KIS_MCLS_FALLBACK = {
    "042": "0026",
}


def _fetch_sector_via_api(code):
    token = get_access_token()
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "CTPF1002R",
    }
    params = {"PDNO": code, "PRDT_TYPE_CD": "300"}
    try:
        resp = requests.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/quotations/search-stock-info",
            headers=headers,
            params=params,
        )
        body = resp.json()
        if body.get("rt_cd") != "0":
            return "0026"
        output = body.get("output", {})
        mcls = output.get("idx_bztp_mcls_cd", "").strip()
        padded = mcls.zfill(4) if mcls and mcls != "000" else ""
        if padded in SECTOR_NAMES:
            return padded
        if mcls in KIS_MCLS_FALLBACK:
            return KIS_MCLS_FALLBACK[mcls]
    except Exception:
        pass
    return "0026"


def _resolve_overrides(unmapped_codes):
    overrides = {}
    if OVERRIDES_PATH.exists():
        with open(OVERRIDES_PATH, "r", encoding="utf-8") as f:
            overrides = json.load(f)

    changed = False
    for code in unmapped_codes:
        if code in overrides:
            continue
        sector_code = _fetch_sector_via_api(code)
        overrides[code] = sector_code
        changed = True
        time.sleep(0.05)

    if changed:
        OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(OVERRIDES_PATH, "w", encoding="utf-8") as f:
            json.dump(overrides, f, ensure_ascii=False, indent=2)

    return overrides


def _load_theme_reverse_index():
    if not THEMES_PATH.exists():
        return {}
    with open(THEMES_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    reverse = {}
    for theme_code, info in data.get("themes", {}).items():
        theme_name = info.get("name", theme_code)
        for stock_code in info.get("constituents", []):
            reverse.setdefault(stock_code, []).append(theme_name)
    return reverse


WICS_MANUAL_OVERRIDES = {
    "001570": {
        "wics_lcls_cd": "G15", "wics_lcls_nm": "소재",
        "wics_mcls_cd": "G1510", "wics_mcls_nm": "소재",
    },
}


def _load_wics_cache():
    if not WICS_CACHE_PATH.exists():
        return {}
    with open(WICS_CACHE_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    mapping = data.get("mapping", {})
    for code, override in WICS_MANUAL_OVERRIDES.items():
        if code not in mapping:
            mapping[code] = override
    return mapping


def load_stock_sector_mapping():
    with open(TOP600_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    tickers = data.get("tickers", []) if isinstance(data, dict) else data

    kospi_df = download_kr_stock_master("KOSPI")
    kosdaq_df = download_kr_stock_master("KOSDAQ")

    kospi_idx = kospi_df.drop_duplicates(subset="단축코드").set_index("단축코드")
    kosdaq_idx = kosdaq_df.drop_duplicates(subset="단축코드").set_index("단축코드")

    theme_idx = _load_theme_reverse_index()
    wics_cache = _load_wics_cache()

    result = {}
    for t in tickers:
        code = t.get("code", "")
        market = t.get("market_sub", "")
        name = t.get("name", "")

        sector_code = None
        if market == "KOSPI" and code in kospi_idx.index:
            sector_code = _classify_kospi(kospi_idx.loc[code])
        elif market == "KOSDAQ" and code in kosdaq_idx.index:
            sector_code = _classify_kosdaq(kosdaq_idx.loc[code])

        wics = wics_cache.get(code, {})
        result[code] = {
            "market": market,
            "sector_code": sector_code,
            "sector_name": SECTOR_NAMES.get(sector_code, "미분류"),
            "stock_name": name,
            "themes": theme_idx.get(code, []),
            "wics_lcls_cd": wics.get("wics_lcls_cd"),
            "wics_lcls_nm": wics.get("wics_lcls_nm"),
            "wics_mcls_cd": wics.get("wics_mcls_cd"),
            "wics_mcls_nm": wics.get("wics_mcls_nm"),
        }

    unmapped_codes = [c for c, v in result.items() if v["sector_code"] is None]
    if unmapped_codes:
        overrides = _resolve_overrides(unmapped_codes)
        for code in unmapped_codes:
            if code in overrides:
                sc = overrides[code]
                result[code]["sector_code"] = sc
                result[code]["sector_name"] = SECTOR_NAMES.get(sc, "미분류")

    return result


STOCK_LEADERS_PATH = Path("cache/stock_leaders.json")
STOCK_PERIODS = {"1w": 5, "1m": 21, "3m": 63, "6m": 126, "1y": 252}


def _fetch_stock_kis(stock_code, total_trading_days=504):
    import pandas as pd
    from datetime import datetime, timedelta

    token = get_access_token()
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST03010100",
    }

    end_date = datetime.now()
    start_date = end_date - timedelta(days=int(total_trading_days * 1.6))

    all_dates, all_closes, all_dvols = [], [], []
    cur_end = end_date

    for _ in range(14):
        if cur_end < start_date:
            break
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code,
            "FID_INPUT_DATE_1": start_date.strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": cur_end.strftime("%Y%m%d"),
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "0",
        }
        resp = requests.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            headers=headers, params=params,
        )
        body = resp.json()
        if body.get("rt_cd") != "0":
            break
        records = body.get("output2") or []
        if not records:
            break
        for rec in records:
            dt_str = rec.get("stck_bsop_date", "")
            close_str = rec.get("stck_clpr", "")
            dvol_str = rec.get("acml_tr_pbmn", "")
            if dt_str and close_str:
                all_dates.append(pd.Timestamp(dt_str))
                all_closes.append(float(close_str))
                all_dvols.append(float(dvol_str) if dvol_str else 0.0)
        if len(records) < 50:
            break
        earliest = min(pd.Timestamp(r["stck_bsop_date"]) for r in records)
        cur_end = earliest - timedelta(days=1)
        time.sleep(0.05)

    if not all_dates:
        return pd.Series(dtype=float), pd.Series(dtype=float)

    idx = pd.DatetimeIndex(all_dates)
    close_s = pd.Series(all_closes, index=idx)
    close_s = close_s[~close_s.index.duplicated(keep="first")].sort_index()
    dvol_s = pd.Series(all_dvols, index=idx)
    dvol_s = dvol_s[~dvol_s.index.duplicated(keep="first")].sort_index()
    return close_s, dvol_s


def _fetch_all_stock_prices(codes):
    import pandas as pd
    closes = {}
    dvols = {}
    total = len(codes)
    for i, code in enumerate(codes):
        try:
            c, d = _fetch_stock_kis(code, total_trading_days=504)
            if len(c) > 20:
                closes[code] = c
            if len(d) > 20:
                dvols[code] = d
        except Exception:
            pass
        if (i + 1) % 100 == 0:
            print(f"    [{i+1}/{total}] fetched...")
        time.sleep(0.05)
    return closes, dvols


PRICE_CACHE_PATH = Path("cache/stock_price_series.json")


def _save_price_cache(closes, dvols):
    data = {}
    for code in set(list(closes.keys()) + list(dvols.keys())):
        entry = {}
        if code in closes:
            s = closes[code]
            entry["close_dates"] = [str(d.date()) for d in s.index]
            entry["close_vals"] = [float(v) for v in s.values]
        if code in dvols:
            s = dvols[code]
            entry["dvol_dates"] = [str(d.date()) for d in s.index]
            entry["dvol_vals"] = [float(v) for v in s.values]
        data[code] = entry
    PRICE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PRICE_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump({"ts": time.time(), "data": data}, f)
    print(f"  Price cache saved: {len(data)} stocks")


def _load_price_cache(max_age=86400*2):
    import pandas as pd
    if not PRICE_CACHE_PATH.exists():
        return None, None
    try:
        with open(PRICE_CACHE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None, None
    if max_age and time.time() - raw.get("ts", 0) > max_age:
        return None, None
    closes = {}
    dvols = {}
    for code, entry in raw.get("data", {}).items():
        if "close_dates" in entry:
            closes[code] = pd.Series(
                entry["close_vals"],
                index=pd.DatetimeIndex(entry["close_dates"])
            ).sort_index()
        if "dvol_dates" in entry:
            dvols[code] = pd.Series(
                entry["dvol_vals"],
                index=pd.DatetimeIndex(entry["dvol_dates"])
            ).sort_index()
    return closes, dvols


def _stock_period_return(prices, period_days):
    if len(prices) < period_days + 1:
        return None
    end_p = float(prices.iloc[-1])
    start_p = float(prices.iloc[-(period_days + 1)])
    if abs(start_p) < 1e-9:
        return None
    return round((end_p / start_p - 1) * 100, 2)


def _stock_volume_change(dvol, period_days):
    if len(dvol) < 2 * period_days:
        return None
    recent = dvol.iloc[-period_days:]
    previous = dvol.iloc[-2 * period_days:-period_days]
    prev_avg = previous.mean()
    if prev_avg == 0:
        return None
    return round((recent.mean() / prev_avg - 1) * 100, 2)


def _load_wics_sector_returns():
    if not WICS_CACHE_SERIES_PATH.exists():
        return {}
    with open(WICS_CACHE_SERIES_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    result = {}
    for period_name in STOCK_PERIODS:
        period_rows = data.get("kr_wics", {}).get(period_name, [])
        for row in period_rows:
            result[(period_name, row["code"])] = row.get("sector_return")
    return result


def _load_kospi_returns():
    dates, values = _load_kospi_benchmark()
    if not values:
        return {}
    import pandas as pd
    bench = pd.Series(values, index=pd.DatetimeIndex(dates)).sort_index()
    result = {}
    for period_name, period_days in STOCK_PERIODS.items():
        ret = _stock_period_return(bench, period_days)
        result[period_name] = ret
    return result


def _fetch_all_estimates(codes):
    from kis_api import get_estimate_perform
    results = {}
    for i, code in enumerate(codes):
        try:
            data = get_estimate_perform(code)
            if data.get("periods"):
                results[code] = data
        except Exception:
            pass
        if (i + 1) % 100 == 0:
            print(f"    [{i+1}/{len(codes)}] estimates fetched...")
        time.sleep(0.05)
    return results


def _parse_earnings(est):
    if not est:
        return None, None
    periods = est.get("periods", [])
    eps = est.get("eps", [])
    if not periods or not eps or len(periods) != len(eps):
        return None, None

    actual_idx = [i for i, p in enumerate(periods) if not p.endswith("E")]
    est_idx = [i for i, p in enumerate(periods) if p.endswith("E")]

    yoy = None
    if len(actual_idx) >= 2:
        prev_eps = eps[actual_idx[-2]]
        curr_eps = eps[actual_idx[-1]]
        if prev_eps is not None and prev_eps > 0:
            yoy = round((curr_eps / prev_eps - 1) * 100, 2)

    fwd = None
    if actual_idx and est_idx:
        base_eps = eps[actual_idx[-1]]
        fwd_eps = eps[est_idx[0]]
        if base_eps is not None and base_eps > 0:
            fwd = round((fwd_eps / base_eps - 1) * 100, 2)

    return yoy, fwd


def compute_stock_leaders():
    import pandas as pd

    print("[1/5] Loading stock-sector mapping...")
    mapping = load_stock_sector_mapping()
    print(f"  {len(mapping)} stocks mapped")

    print("[2/5] Fetching stock prices + volumes (KIS API 2y)...")
    t0 = time.time()
    all_codes = list(mapping.keys())
    closes, dvols = _fetch_all_stock_prices(all_codes)
    print(f"  {len(closes)} closes, {len(dvols)} dvols in {time.time()-t0:.1f}s")

    _save_price_cache(closes, dvols)

    print("[3/5] Fetching earnings estimates (KIS API)...")
    t1 = time.time()
    estimates = _fetch_all_estimates(all_codes)
    print(f"  {len(estimates)} estimates in {time.time()-t1:.1f}s")

    earnings = {}
    for code in all_codes:
        yoy, fwd = _parse_earnings(estimates.get(code))
        earnings[code] = {"yoy": yoy, "fwd": fwd}

    print("[4/5] Loading benchmarks...")
    kospi_returns = _load_kospi_returns()
    sector_returns = _load_wics_sector_returns()
    print(f"  KOSPI returns: {len(kospi_returns)}, WICS sector-period returns: {len(sector_returns)}")

    yoy_vals = [earnings.get(c, {}).get("yoy") for c in all_codes]
    fwd_vals = [earnings.get(c, {}).get("fwd") for c in all_codes]
    yoy_zs = winsorized_zscores(yoy_vals)
    fwd_zs = winsorized_zscores(fwd_vals)
    Z_CAP = 5.0
    for i, code in enumerate(all_codes):
        y_z = max(-Z_CAP, min(Z_CAP, yoy_zs[i])) if yoy_zs[i] is not None else None
        f_z = max(-Z_CAP, min(Z_CAP, fwd_zs[i])) if fwd_zs[i] is not None else None
        if y_z is not None and f_z is not None:
            e_z = round((y_z + f_z) / 2, 4)
        elif y_z is not None:
            e_z = y_z
        elif f_z is not None:
            e_z = f_z
        else:
            e_z = None
        earnings[code]["yoy_z"] = y_z
        earnings[code]["fwd_z"] = f_z
        earnings[code]["earnings_z"] = e_z

    q_eps_cache = {}
    try:
        from quarterly_eps_tracker import _load_cache as _load_qeps_cache
        raw_qeps = _load_qeps_cache()
        for code, entry in raw_qeps.items():
            t = entry.get("trend")
            if t:
                q_eps_cache[code] = t
    except Exception:
        pass
    print(f"  Quarterly EPS cache: {len(q_eps_cache)} stocks")

    print("[5/5] Computing 5-period RS + volume change...")
    result = {"last_updated": time.strftime("%Y-%m-%dT%H:%M:%S"), "kr_stocks": {}}

    with open(TOP600_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    tickers_info = data.get("tickers", []) if isinstance(data, dict) else data
    ticker_meta = {}
    for t in tickers_info:
        ticker_meta[t["code"]] = {"name": t["name"], "market": t.get("market_sub", "")}

    for period_name, period_days in STOCK_PERIODS.items():
        rows = []
        kospi_ret = kospi_returns.get(period_name)

        for code, meta in ticker_meta.items():
            m = mapping.get(code, {})
            wics_cd = m.get("wics_mcls_cd")
            wics_nm = m.get("wics_mcls_nm")
            lcls_nm = m.get("wics_lcls_nm")
            themes = m.get("themes", [])

            stock_ret = None
            rs_market = None
            rs_sector = None
            vc = None

            if code in closes:
                stock_ret = _stock_period_return(closes[code], period_days)

            if stock_ret is not None and kospi_ret is not None:
                rs_market = round(stock_ret - kospi_ret, 2)

            if stock_ret is not None and wics_cd:
                sec_ret = sector_returns.get((period_name, wics_cd))
                if sec_ret is not None:
                    rs_sector = round(stock_ret - sec_ret, 2)

            if code in dvols:
                vc = _stock_volume_change(dvols[code], period_days)

            e = earnings.get(code, {})
            qe = q_eps_cache.get(code, {})
            rows.append({
                "ticker": code,
                "name": meta["name"],
                "market": meta["market"],
                "wics_mcls_cd": wics_cd,
                "wics_mcls_nm": wics_nm,
                "wics_lcls_nm": lcls_nm,
                "themes": themes,
                "stock_return": stock_ret,
                "rs_market": rs_market,
                "rs_sector": rs_sector,
                "volume_change": vc,
                "yoy_eps_growth": e.get("yoy"),
                "fwd_eps_growth": e.get("fwd"),
                "yoy_eps_z": e.get("yoy_z"),
                "fwd_eps_z": e.get("fwd_z"),
                "earnings_z": e.get("earnings_z"),
                "q_eps_qoq": qe.get("qoq"),
                "q_eps_yoy": qe.get("yoy"),
                "q_eps_period": qe.get("latest_period"),
            })

        rs_m_vals = [r["rs_market"] for r in rows]
        rs_s_vals = [r["rs_sector"] for r in rows]
        vc_vals = [r["volume_change"] for r in rows]
        rs_m_zs = winsorized_zscores(rs_m_vals)
        rs_s_zs = winsorized_zscores(rs_s_vals)
        vc_zs = winsorized_zscores(vc_vals)
        Z_CAP = 5.0
        for i, r in enumerate(rows):
            r["rs_market_z"] = max(-Z_CAP, min(Z_CAP, rs_m_zs[i])) if rs_m_zs[i] is not None else None
            r["rs_sector_z"] = max(-Z_CAP, min(Z_CAP, rs_s_zs[i])) if rs_s_zs[i] is not None else None
            r["vc_z"] = max(-Z_CAP, min(Z_CAP, vc_zs[i])) if vc_zs[i] is not None else None

        rows.sort(key=lambda x: (x["rs_market"] is None, -(x["rs_market"] or 0)))
        result["kr_stocks"][period_name] = rows

    STOCK_LEADERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STOCK_LEADERS_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"  Saved to {STOCK_LEADERS_PATH}")
    return result


_custom_cache = {}

def compute_stock_leaders_custom(start_date, end_date):
    import pandas as pd
    from datetime import datetime

    cache_key = f"{start_date}:{end_date}"
    cached = _custom_cache.get(cache_key)
    if cached and time.time() - cached["ts"] < 3600:
        return cached["data"]

    dt_start = pd.Timestamp(start_date)
    dt_end = pd.Timestamp(end_date)

    if not STOCK_LEADERS_PATH.exists():
        return {"error": "stock_leaders.json not found – run batch first"}
    with open(STOCK_LEADERS_PATH, "r", encoding="utf-8") as f:
        base_data = json.load(f)
    base_rows_by_ticker = {}
    first_period = list(base_data.get("kr_stocks", {}).values())
    if first_period:
        for r in first_period[0]:
            base_rows_by_ticker[r["ticker"]] = r

    mapping = load_stock_sector_mapping()
    with open(TOP600_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    tickers_info = data.get("tickers", []) if isinstance(data, dict) else data
    ticker_meta = {}
    for t in tickers_info:
        ticker_meta[t["code"]] = {"name": t["name"], "market": t.get("market_sub", "")}

    closes, dvols = _load_price_cache()
    if closes is None:
        return {"error": "price cache not available – run batch first"}

    kospi_dates, kospi_vals = _load_kospi_benchmark()
    kospi_series = None
    kospi_ret = None
    if kospi_dates and kospi_vals:
        kospi_series = pd.Series(kospi_vals, index=pd.DatetimeIndex(kospi_dates)).sort_index()
        ks = kospi_series.loc[dt_start:dt_end]
        if len(ks) >= 2:
            kospi_ret = round((float(ks.iloc[-1]) / float(ks.iloc[0]) - 1) * 100, 2)

    wics_series = {}
    if WICS_CACHE_SERIES_PATH.exists():
        with open(WICS_CACHE_SERIES_PATH, "r", encoding="utf-8") as f:
            wics_raw = json.load(f)
        for sec_code, sec_data in wics_raw.get("series", {}).items():
            if sec_code.startswith("_"):
                continue
            sd = sec_data.get("dates", [])
            sv = sec_data.get("values", [])
            if sd and sv:
                wics_series[sec_code] = pd.Series(sv, index=pd.DatetimeIndex(sd)).sort_index()

    q_eps_cache = {}
    try:
        from quarterly_eps_tracker import _load_cache as _load_qeps_cache
        raw_qeps = _load_qeps_cache()
        for code, entry in raw_qeps.items():
            t = entry.get("trend")
            if t:
                q_eps_cache[code] = t
    except Exception:
        pass

    rows = []
    actual_start = None
    actual_end = None

    for code, meta in ticker_meta.items():
        m = mapping.get(code, {})
        wics_cd = m.get("wics_mcls_cd")
        wics_nm = m.get("wics_mcls_nm")
        lcls_nm = m.get("wics_lcls_nm")
        themes = m.get("themes", [])

        stock_ret = None
        rs_market = None
        rs_sector = None
        vc = None

        if code in closes:
            cs = closes[code]
            window = cs.loc[dt_start:dt_end]
            if len(window) >= 2:
                s_price = float(window.iloc[0])
                e_price = float(window.iloc[-1])
                if abs(s_price) > 1e-9:
                    stock_ret = round((e_price / s_price - 1) * 100, 2)
                if actual_start is None:
                    actual_start = str(window.index[0].date())
                    actual_end = str(window.index[-1].date())

        if stock_ret is not None and kospi_ret is not None:
            rs_market = round(stock_ret - kospi_ret, 2)

        if stock_ret is not None and wics_cd and wics_cd in wics_series:
            ws = wics_series[wics_cd].loc[dt_start:dt_end]
            if len(ws) >= 2:
                sec_ret = round((float(ws.iloc[-1]) / float(ws.iloc[0]) - 1) * 100, 2)
                rs_sector = round(stock_ret - sec_ret, 2)

        if code in dvols:
            dv = dvols[code]
            dv_window = dv.loc[dt_start:dt_end]
            period_len = len(dv_window)
            if period_len > 0:
                prev_start = dt_start - (dt_end - dt_start)
                dv_prev = dv.loc[prev_start:dt_start - pd.Timedelta(days=1)]
                if len(dv_prev) > 0:
                    prev_avg = dv_prev.mean()
                    if prev_avg > 0:
                        vc = round((dv_window.mean() / prev_avg - 1) * 100, 2)

        base = base_rows_by_ticker.get(code, {})
        qe = q_eps_cache.get(code, {})
        rows.append({
            "ticker": code,
            "name": meta["name"],
            "market": meta["market"],
            "wics_mcls_cd": wics_cd,
            "wics_mcls_nm": wics_nm,
            "wics_lcls_nm": lcls_nm,
            "themes": themes,
            "stock_return": stock_ret,
            "rs_market": rs_market,
            "rs_sector": rs_sector,
            "volume_change": vc,
            "yoy_eps_growth": base.get("yoy_eps_growth"),
            "fwd_eps_growth": base.get("fwd_eps_growth"),
            "yoy_eps_z": base.get("yoy_eps_z"),
            "fwd_eps_z": base.get("fwd_eps_z"),
            "earnings_z": base.get("earnings_z"),
            "q_eps_qoq": qe.get("qoq", base.get("q_eps_qoq")),
            "q_eps_yoy": qe.get("yoy", base.get("q_eps_yoy")),
            "q_eps_period": qe.get("latest_period", base.get("q_eps_period")),
        })

    rs_m_vals = [r["rs_market"] for r in rows]
    rs_s_vals = [r["rs_sector"] for r in rows]
    vc_vals = [r["volume_change"] for r in rows]
    rs_m_zs = winsorized_zscores(rs_m_vals)
    rs_s_zs = winsorized_zscores(rs_s_vals)
    vc_zs = winsorized_zscores(vc_vals)
    Z_CAP = 5.0
    for i, r in enumerate(rows):
        r["rs_market_z"] = max(-Z_CAP, min(Z_CAP, rs_m_zs[i])) if rs_m_zs[i] is not None else None
        r["rs_sector_z"] = max(-Z_CAP, min(Z_CAP, rs_s_zs[i])) if rs_s_zs[i] is not None else None
        r["vc_z"] = max(-Z_CAP, min(Z_CAP, vc_zs[i])) if vc_zs[i] is not None else None

    rows.sort(key=lambda x: (x["rs_market"] is None, -(x["rs_market"] or 0)))

    result = {
        "start_date": start_date,
        "end_date": end_date,
        "actual_start": actual_start or start_date,
        "actual_end": actual_end or end_date,
        "stocks": rows,
    }
    _custom_cache[cache_key] = {"ts": time.time(), "data": result}
    return result


CONSENSUS_DIR = Path("cache/consensus_snapshots")


def snapshot_consensus_today():
    from datetime import datetime

    CONSENSUS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    out_path = CONSENSUS_DIR / f"{today}.json"

    print(f"[consensus] Snapshotting {today}...")
    mapping = load_stock_sector_mapping()
    codes = list(mapping.keys())
    print(f"  {len(codes)} stocks")

    t0 = time.time()
    raw = _fetch_all_estimates(codes)
    print(f"  {len(raw)} estimates in {time.time()-t0:.1f}s")

    consensus = {}
    for code, est in raw.items():
        consensus[code] = {
            "periods": est.get("periods", []),
            "eps": est.get("eps", []),
            "eps_growth": est.get("eps_growth", []),
        }

    result = {
        "snapshot_date": today,
        "snapshot_time": datetime.now().isoformat(timespec="seconds"),
        "consensus": consensus,
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"  Saved to {out_path} ({len(consensus)} stocks)")
    return result


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stocks":
        try:
            compute_wics_rs()
        except Exception as e:
            print(f"[WICS] refresh failed, using existing cache: {e}")
        compute_stock_leaders()
    elif len(sys.argv) > 1 and sys.argv[1] == "wics":
        r = build_wics_cache()
        print(f"WICS 매핑 저장: {len(r)} 종목")
    elif len(sys.argv) > 1 and sys.argv[1] == "snapshot-consensus":
        snapshot_consensus_today()
    else:
        mapping = load_stock_sector_mapping()
        total = len(mapping)
        mapped = sum(1 for v in mapping.values() if v["sector_name"] != "미분류")
        print(f"Total: {total}, Mapped: {mapped} ({mapped / total * 100:.1f}%)")
        sectors = Counter(v["sector_name"] for v in mapping.values())
        for s, c in sectors.most_common():
            print(f"  {s}: {c}")
