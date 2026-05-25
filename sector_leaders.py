import json
import math
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

from kis_api import (
    get_access_token,
    get_sector_codes,
    APP_KEY,
    APP_SECRET,
    BASE_URL,
)

KOSPI_BENCHMARK_CODE = "0001"
US_BENCHMARK_TICKER = "^GSPC"

PERIODS = {"1w": 5, "1m": 21, "3m": 63, "6m": 126, "1y": 252}

US_SECTORS = {
    "SOXX": "반도체",
    "XLF": "금융",
    "IAI": "증권/자산운용",
    "KIE": "보험",
    "IHE": "제약",
    "IHI": "의료기기",
    "XLY": "임의소비재",
    "XRT": "소매유통",
    "CARZ": "자동차",
    "PBJ": "식음료",
    "XLI": "산업재",
    "IYT": "운송",
    "XLB": "소재",
    "SLX": "철강",
    "XME": "광물",
    "WOOD": "목재",
    "XLU": "유틸리티",
    "ITB": "주택건설",
    "XLC": "통신서비스",
}

SECTOR_MAPPING = {
    "음식료·담배": "PBJ",
    "섬유·의류": "XLY",
    "종이·목재": "WOOD",
    "화학": "XLB",
    "제약": "IHE",
    "비금속": "XME",
    "금속": "SLX",
    "기계·장비": "XLI",
    "전기·전자": "SOXX",
    "의료·정밀기기": "IHI",
    "운송장비·부품": "CARZ",
    "유통": "XRT",
    "전기·가스": "XLU",
    "건설": "ITB",
    "운송·창고": "IYT",
    "통신": "XLC",
    "금융": "XLF",
    "증권": "IAI",
    "보험": "KIE",
    "일반서비스": "XLC",
}

CACHE_PATH = Path("cache/sector_leaders.json")

KR_THEMES = {
    "4412": "2차전지 TOP10",
    "4413": "바이오 TOP10",
    "4414": "인터넷 TOP10",
    "4415": "게임 TOP10",
    "4421": "전기차 Top15",
    "4422": "반도체 Top15",
}

KR_SIZES = {
    "4448": "전체 TMI",
    "4449": "중대형 TMI",
    "4450": "중형 TMI",
    "4451": "소형 TMI",
    "4452": "초소형 TMI",
}

US_THEMES = {
    "LIT": "리튬·배터리",
    "XBI": "바이오테크",
    "FDN": "인터넷",
    "HERO": "게임·e스포츠",
    "DRIV": "자율주행·전기차",
    "SOXX": "반도체",
}

US_SIZES = {
    "IWV": "Russell 3000",
    "IWB": "Russell 1000",
    "IWR": "Russell Midcap",
    "IWM": "Russell 2000",
    "IWC": "Micro-Cap",
}

THEME_MAPPING = {
    "2차전지 TOP10": "LIT",
    "바이오 TOP10": "XBI",
    "인터넷 TOP10": "FDN",
    "게임 TOP10": "HERO",
    "전기차 Top15": "DRIV",
    "반도체 Top15": "SOXX",
}

SIZE_MAPPING = {
    "전체 TMI": "IWV",
    "중대형 TMI": "IWB",
    "중형 TMI": "IWR",
    "소형 TMI": "IWM",
    "초소형 TMI": "IWC",
}


def _fetch_kr_index_long(sector_code, total_trading_days=260):
    token = get_access_token()
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKUP03500100",
    }

    end_date = datetime.now()
    start_date = end_date - timedelta(days=int(total_trading_days * 1.6))

    all_dates = []
    all_closes = []
    cur_end = end_date

    for _ in range(8):
        if cur_end < start_date:
            break

        params = {
            "FID_COND_MRKT_DIV_CODE": "U",
            "FID_INPUT_ISCD": sector_code,
            "FID_INPUT_DATE_1": start_date.strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": cur_end.strftime("%Y%m%d"),
            "FID_PERIOD_DIV_CODE": "D",
        }

        resp = requests.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice",
            headers=headers,
            params=params,
        )

        body = resp.json()
        if body.get("rt_cd") != "0":
            break

        records = body.get("output2") or []
        if not records:
            break

        for rec in records:
            dt_str = rec.get("stck_bsop_date", "")
            close_str = rec.get("bstp_nmix_prpr", "")
            if dt_str and close_str:
                all_dates.append(pd.Timestamp(dt_str))
                all_closes.append(float(close_str))

        if len(records) < 50:
            break

        earliest = min(pd.Timestamp(r["stck_bsop_date"]) for r in records)
        cur_end = earliest - timedelta(days=1)
        time.sleep(0.05)

    if not all_dates:
        return pd.Series(dtype=float, name=sector_code)

    series = pd.Series(all_closes, index=pd.DatetimeIndex(all_dates), name=sector_code)
    return series[~series.index.duplicated(keep="first")].sort_index()


def fetch_kr_sector_series():
    codes = get_sector_codes()
    result = {}
    for i, c in enumerate(codes):
        try:
            s = _fetch_kr_index_long(c["code"])
            if len(s) > 0:
                result[c["name"]] = s
                print(f"  KR [{i+1}/{len(codes)}] {c['name']}: {len(s)} days")
            else:
                print(f"  KR [{i+1}/{len(codes)}] {c['name']}: empty")
        except Exception as e:
            print(f"  KR [{i+1}/{len(codes)}] {c['name']}: ERROR {e}")
        time.sleep(0.05)
    return result


def fetch_kr_benchmark_series():
    print("  KR benchmark (KOSPI 종합)...")
    return _fetch_kr_index_long(KOSPI_BENCHMARK_CODE)


def fetch_us_sector_series():
    tickers = list(US_SECTORS.keys())
    print(f"  US sectors: downloading {len(tickers)} ETFs...")
    try:
        df = yf.download(tickers, period="1y", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            closes = df["Close"]
        else:
            closes = df
    except Exception as e:
        print(f"  US sector download failed: {e}")
        return {}

    result = {}
    for ticker in tickers:
        try:
            s = closes[ticker].dropna()
            if len(s) > 0:
                result[ticker] = s
                print(f"    {ticker} ({US_SECTORS[ticker]}): {len(s)} days")
        except Exception as e:
            print(f"    {ticker}: ERROR {e}")
    return result


def fetch_us_benchmark_series():
    print("  US benchmark (S&P 500)...")
    try:
        df = yf.download(US_BENCHMARK_TICKER, period="1y", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            s = df["Close"][US_BENCHMARK_TICKER].dropna()
        else:
            s = df["Close"].dropna()
        print(f"    ^GSPC: {len(s)} days")
        return s
    except Exception as e:
        print(f"    ^GSPC: ERROR {e}")
        return pd.Series(dtype=float)


def _fetch_kr_group_series(code_dict, label):
    result = {}
    items = list(code_dict.items())
    for i, (code, name) in enumerate(items):
        try:
            s = _fetch_kr_index_long(code)
            if len(s) > 0:
                result[name] = s
                print(f"  {label} [{i+1}/{len(items)}] {name}: {len(s)} days")
            else:
                print(f"  {label} [{i+1}/{len(items)}] {name}: empty")
        except Exception as e:
            print(f"  {label} [{i+1}/{len(items)}] {name}: ERROR {e}")
        time.sleep(0.05)
    return result


def _fetch_us_etfs(ticker_dict, label):
    tickers = list(ticker_dict.keys())
    print(f"  {label}: downloading {len(tickers)} ETFs...")
    try:
        df = yf.download(tickers, period="1y", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            closes = df["Close"]
        else:
            closes = df
    except Exception as e:
        print(f"  {label} download failed: {e}")
        return {}
    result = {}
    for ticker in tickers:
        try:
            s = closes[ticker].dropna()
            if len(s) > 0:
                result[ticker] = s
                print(f"    {ticker} ({ticker_dict[ticker]}): {len(s)} days")
        except Exception as e:
            print(f"    {ticker}: ERROR {e}")
    return result


def compute_rs(series, benchmark, period_days):
    min_required = max(period_days - 5, int(period_days * 0.95))
    if len(series) < min_required or len(benchmark) < min_required:
        return None, None, None
    period_days = min(period_days, len(series), len(benchmark))

    s_recent = series.iloc[-period_days:]
    b_recent = benchmark.iloc[-period_days:]

    sector_ret = (s_recent.iloc[-1] / s_recent.iloc[0] - 1) * 100
    bench_ret = (b_recent.iloc[-1] / b_recent.iloc[0] - 1) * 100
    rs = sector_ret - bench_ret

    return round(rs, 2), round(sector_ret, 2), round(bench_ret, 2)


def _reverse_mapping():
    rev = {}
    for kr_name, us_ticker in SECTOR_MAPPING.items():
        rev.setdefault(us_ticker, []).append(kr_name)
    return rev


def _safe_float(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return round(f, 2)


def _build_series(sectors, benchmark):
    dates = benchmark.index.sort_values()
    out = {
        "_benchmark": {
            "dates": [d.strftime("%Y-%m-%d") for d in dates],
            "values": [_safe_float(v) for v in benchmark.reindex(dates).values],
        }
    }
    for name, s in sectors.items():
        aligned = s.reindex(dates)
        out[name] = {
            "values": [_safe_float(v) for v in aligned.values],
        }
    return out


def _compute_kr_group(series_dict, benchmark, mapping):
    result = {}
    for period_name, period_days in PERIODS.items():
        rows = []
        for name, s in series_dict.items():
            rs, s_ret, b_ret = compute_rs(s, benchmark, period_days)
            rows.append({
                "sector": name,
                "rs": rs,
                "sector_return": s_ret,
                "benchmark_return": b_ret,
                "mapped_us": mapping.get(name),
            })
        rows.sort(key=lambda x: (x["rs"] is None, -(x["rs"] or 0)))
        result[period_name] = rows
    result["series"] = _build_series(series_dict, benchmark)
    return result


def _compute_us_group(series_dict, benchmark, names, kr_to_us_mapping):
    rev = {}
    for kr_name, us_ticker in kr_to_us_mapping.items():
        rev.setdefault(us_ticker, []).append(kr_name)
    result = {}
    for period_name, period_days in PERIODS.items():
        rows = []
        for ticker, s in series_dict.items():
            rs, s_ret, b_ret = compute_rs(s, benchmark, period_days)
            rows.append({
                "ticker": ticker,
                "name": names.get(ticker, ""),
                "rs": rs,
                "sector_return": s_ret,
                "benchmark_return": b_ret,
                "mapped_kr": rev.get(ticker, []),
            })
        rows.sort(key=lambda x: (x["rs"] is None, -(x["rs"] or 0)))
        result[period_name] = rows
    result["series"] = _build_series(series_dict, benchmark)
    return result


def compute_all_scores():
    print("[1/6] Fetching KR sector data...")
    kr_sectors = fetch_kr_sector_series()
    kr_bench = fetch_kr_benchmark_series()

    print("[2/6] Fetching KR themes + sizes...")
    kr_themes = _fetch_kr_group_series(KR_THEMES, "KR theme")
    kr_sizes = _fetch_kr_group_series(KR_SIZES, "KR size")

    print("[3/6] Fetching US sector data...")
    us_sectors = fetch_us_sector_series()
    us_bench = fetch_us_benchmark_series()

    print("[4/6] Fetching US themes + sizes...")
    us_themes = _fetch_us_etfs(US_THEMES, "US themes")
    us_sizes = _fetch_us_etfs(US_SIZES, "US sizes")

    rev_map = _reverse_mapping()

    result = {
        "last_updated": datetime.now().isoformat(timespec="seconds"),
        "kr": {},
        "us": {},
    }

    print("[5/6] Computing KR RS scores...")
    for period_name, period_days in PERIODS.items():
        rows = []
        for sector_name, series in kr_sectors.items():
            rs, s_ret, b_ret = compute_rs(series, kr_bench, period_days)
            rows.append({
                "sector": sector_name,
                "rs": rs,
                "sector_return": s_ret,
                "benchmark_return": b_ret,
                "mapped_us": SECTOR_MAPPING.get(sector_name),
            })
        rows.sort(key=lambda x: (x["rs"] is None, -(x["rs"] or 0)))
        result["kr"][period_name] = rows

    print("[6/6] Computing US RS scores...")
    for period_name, period_days in PERIODS.items():
        rows = []
        for ticker, series in us_sectors.items():
            rs, s_ret, b_ret = compute_rs(series, us_bench, period_days)
            rows.append({
                "ticker": ticker,
                "name": US_SECTORS.get(ticker, ""),
                "rs": rs,
                "sector_return": s_ret,
                "benchmark_return": b_ret,
                "mapped_kr": rev_map.get(ticker, []),
            })
        rows.sort(key=lambda x: (x["rs"] is None, -(x["rs"] or 0)))
        result["us"][period_name] = rows

    result["kr"]["series"] = _build_series(kr_sectors, kr_bench)
    result["us"]["series"] = _build_series(us_sectors, us_bench)
    result["kr"]["themes"] = _compute_kr_group(kr_themes, kr_bench, THEME_MAPPING)
    result["kr"]["sizes"] = _compute_kr_group(kr_sizes, kr_bench, SIZE_MAPPING)
    result["us"]["themes"] = _compute_us_group(us_themes, us_bench, US_THEMES, THEME_MAPPING)
    result["us"]["sizes"] = _compute_us_group(us_sizes, us_bench, US_SIZES, SIZE_MAPPING)

    return result


def save_cache(data):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"Saved to {CACHE_PATH}")


def main(argv):
    mode = argv[1] if len(argv) > 1 else "all"

    existing = {}
    if CACHE_PATH.exists():
        try:
            existing = json.loads(CACHE_PATH.read_text())
        except Exception:
            pass

    t0 = time.time()

    if mode == "all":
        data = compute_all_scores()
    elif mode == "kr":
        print("[KR only] Fetching KR sector data...")
        kr_sectors = fetch_kr_sector_series()
        kr_bench = fetch_kr_benchmark_series()
        print("[KR only] Fetching KR themes + sizes...")
        kr_themes = _fetch_kr_group_series(KR_THEMES, "KR theme")
        kr_sizes = _fetch_kr_group_series(KR_SIZES, "KR size")
        kr_result = {}
        for period_name, period_days in PERIODS.items():
            rows = []
            for sector_name, series in kr_sectors.items():
                rs, s_ret, b_ret = compute_rs(series, kr_bench, period_days)
                rows.append({
                    "sector": sector_name,
                    "rs": rs,
                    "sector_return": s_ret,
                    "benchmark_return": b_ret,
                    "mapped_us": SECTOR_MAPPING.get(sector_name),
                })
            rows.sort(key=lambda x: (x["rs"] is None, -(x["rs"] or 0)))
            kr_result[period_name] = rows
        kr_result["series"] = _build_series(kr_sectors, kr_bench)
        kr_result["themes"] = _compute_kr_group(kr_themes, kr_bench, THEME_MAPPING)
        kr_result["sizes"] = _compute_kr_group(kr_sizes, kr_bench, SIZE_MAPPING)
        data = {
            "last_updated": datetime.now().isoformat(timespec="seconds"),
            "kr": kr_result,
            "us": existing.get("us", {}),
        }
    elif mode == "us":
        print("[US only] Fetching US sector data...")
        us_sectors = fetch_us_sector_series()
        us_bench = fetch_us_benchmark_series()
        print("[US only] Fetching US themes + sizes...")
        us_themes = _fetch_us_etfs(US_THEMES, "US themes")
        us_sizes = _fetch_us_etfs(US_SIZES, "US sizes")
        rev_map = _reverse_mapping()
        us_result = {}
        for period_name, period_days in PERIODS.items():
            rows = []
            for ticker, series in us_sectors.items():
                rs, s_ret, b_ret = compute_rs(series, us_bench, period_days)
                rows.append({
                    "ticker": ticker,
                    "name": US_SECTORS.get(ticker, ""),
                    "rs": rs,
                    "sector_return": s_ret,
                    "benchmark_return": b_ret,
                    "mapped_kr": rev_map.get(ticker, []),
                })
            rows.sort(key=lambda x: (x["rs"] is None, -(x["rs"] or 0)))
            us_result[period_name] = rows
        us_result["series"] = _build_series(us_sectors, us_bench)
        us_result["themes"] = _compute_us_group(us_themes, us_bench, US_THEMES, THEME_MAPPING)
        us_result["sizes"] = _compute_us_group(us_sizes, us_bench, US_SIZES, SIZE_MAPPING)
        data = {
            "last_updated": datetime.now().isoformat(timespec="seconds"),
            "kr": existing.get("kr", {}),
            "us": us_result,
        }
    else:
        print(f"Unknown mode: {mode}. Use 'all', 'kr', or 'us'.")
        return

    save_cache(data)
    elapsed = time.time() - t0
    print(f"\nCompleted in {elapsed:.1f}s")

    for period_name in PERIODS:
        print(f"\n=== {period_name} ===")
        if data.get("kr", {}).get(period_name):
            top3 = [s for s in data["kr"][period_name] if s["rs"] is not None][:3]
            labels = [f"{s['sector']}({s['rs']:+.1f}%)" for s in top3]
            print(f"  KR top 3: {', '.join(labels)}")
        if data.get("us", {}).get(period_name):
            top3 = [s for s in data["us"][period_name] if s["rs"] is not None][:3]
            labels = [f"{s['name']}({s['rs']:+.1f}%)" for s in top3]
            print(f"  US top 3: {', '.join(labels)}")


if __name__ == "__main__":
    main(sys.argv)
