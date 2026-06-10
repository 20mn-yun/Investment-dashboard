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
    "4412": {"name": "2차전지 TOP10", "source": "index"},
    "4413": {"name": "바이오 TOP10", "source": "index"},
    "4414": {"name": "인터넷 TOP10", "source": "index"},
    "4415": {"name": "게임 TOP10", "source": "index"},
    "4421": {"name": "전기차 Top15", "source": "index"},
    "4422": {"name": "반도체 Top15", "source": "index"},
    "0080G0": {"name": "KODEX 방산TOP10", "source": "etf"},
    "421320": {"name": "PLUS 우주항공&UAM", "source": "etf"},
    "0148J0": {"name": "TIGER 휴머노이드로봇", "source": "etf"},
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
    "ITA": "방산",
    "ARKX": "우주항공",
    "BOTZ": "로봇·AI",
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
    "KODEX 방산TOP10": "ITA",
    "PLUS 우주항공&UAM": "ARKX",
    "TIGER 휴머노이드로봇": "BOTZ",
}

SIZE_MAPPING = {
    "전체 TMI": "IWV",
    "중대형 TMI": "IWB",
    "중형 TMI": "IWR",
    "소형 TMI": "IWM",
    "초소형 TMI": "IWC",
}

WICS_TO_US_ETF = {
    "G1010": {"name": "에너지", "us_etf": "XLE"},
    "G1510": {"name": "소재", "us_etf": "XLB"},
    "G2010": {"name": "자본재", "us_etf": "XLI"},
    "G2020": {"name": "상업서비스와공급품", "us_etf": "XLI"},
    "G2030": {"name": "운송", "us_etf": "IYT"},
    "G2510": {"name": "자동차와부품", "us_etf": "CARZ"},
    "G2520": {"name": "내구소비재와의류", "us_etf": "XLY"},
    "G2530": {"name": "호텔,레스토랑,레저등", "us_etf": "PEJ"},
    "G2550": {"name": "소매(유통)", "us_etf": "XRT"},
    "G2560": {"name": "교육서비스", "us_etf": None},
    "G3010": {"name": "식품과기본식료품소매", "us_etf": "XLP"},
    "G3020": {"name": "식품,음료,담배", "us_etf": "PBJ"},
    "G3030": {"name": "가정용품과개인용품", "us_etf": "XLP"},
    "G3510": {"name": "건강관리장비와서비스", "us_etf": "IHI"},
    "G3520": {"name": "제약과생물공학", "us_etf": "IHE"},
    "G4010": {"name": "은행", "us_etf": "KBE"},
    "G4020": {"name": "증권", "us_etf": "IAI"},
    "G4030": {"name": "다각화된금융", "us_etf": "XLF"},
    "G4040": {"name": "보험", "us_etf": "KIE"},
    "G4050": {"name": "부동산", "us_etf": "XLRE"},
    "G4510": {"name": "소프트웨어와서비스", "us_etf": "IGV"},
    "G4520": {"name": "기술하드웨어와장비", "us_etf": "XLK"},
    "G4530": {"name": "반도체와반도체장비", "us_etf": "SOXX"},
    "G4535": {"name": "전자와전기제품", "us_etf": "LIT"},
    "G4540": {"name": "디스플레이", "us_etf": None},
    "G5010": {"name": "전기통신서비스", "us_etf": "IYZ"},
    "G5020": {"name": "미디어와엔터테인먼트", "us_etf": "XLC"},
    "G5510": {"name": "유틸리티", "us_etf": "XLU"},
}

WICS_US_ETFS = sorted({
    v["us_etf"] for v in WICS_TO_US_ETF.values() if v["us_etf"]
})


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


def _fetch_kr_etf_long(stock_code, total_trading_days=260):
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

    all_dates = []
    all_closes = []
    cur_end = end_date

    for _ in range(8):
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
            close_str = rec.get("stck_clpr", "")
            if dt_str and close_str:
                all_dates.append(pd.Timestamp(dt_str))
                all_closes.append(float(close_str))

        if len(records) < 50:
            break

        earliest = min(pd.Timestamp(r["stck_bsop_date"]) for r in records)
        cur_end = earliest - timedelta(days=1)
        time.sleep(0.05)

    if not all_dates:
        return pd.Series(dtype=float, name=stock_code)

    series = pd.Series(all_closes, index=pd.DatetimeIndex(all_dates), name=stock_code)
    return series[~series.index.duplicated(keep="first")].sort_index()


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
    for i, (code, entry) in enumerate(items):
        if isinstance(entry, dict):
            name = entry["name"]
            source = entry.get("source", "index")
        else:
            name = entry
            source = "index"
        try:
            if source == "etf":
                s = _fetch_kr_etf_long(code)
            else:
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


def _fetch_kr_index_volume_long(sector_code, total_trading_days=504):
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
    all_volumes = []
    cur_end = end_date
    for _ in range(14):
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
            vol_str = rec.get("acml_tr_pbmn", "")
            if dt_str and vol_str:
                all_dates.append(pd.Timestamp(dt_str))
                all_volumes.append(float(vol_str))
        if len(records) < 50:
            break
        earliest = min(pd.Timestamp(r["stck_bsop_date"]) for r in records)
        cur_end = earliest - timedelta(days=1)
        time.sleep(0.05)
    if not all_dates:
        return pd.Series(dtype=float, name=sector_code)
    series = pd.Series(all_volumes, index=pd.DatetimeIndex(all_dates), name=sector_code)
    return series[~series.index.duplicated(keep="first")].sort_index()


def _fetch_kr_etf_volume_long(stock_code, total_trading_days=504):
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
    all_dates = []
    all_volumes = []
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
            vol_str = rec.get("acml_tr_pbmn", "")
            if dt_str and vol_str:
                all_dates.append(pd.Timestamp(dt_str))
                all_volumes.append(float(vol_str))
        if len(records) < 50:
            break
        earliest = min(pd.Timestamp(r["stck_bsop_date"]) for r in records)
        cur_end = earliest - timedelta(days=1)
        time.sleep(0.05)
    if not all_dates:
        return pd.Series(dtype=float, name=stock_code)
    series = pd.Series(all_volumes, index=pd.DatetimeIndex(all_dates), name=stock_code)
    return series[~series.index.duplicated(keep="first")].sort_index()


def _fetch_kr_group_volume(code_dict, label):
    result = {}
    items = list(code_dict.items())
    for i, (code, entry) in enumerate(items):
        if isinstance(entry, dict):
            name = entry["name"]
            source = entry.get("source", "index")
        else:
            name = entry
            source = "index"
        try:
            if source == "etf":
                s = _fetch_kr_etf_volume_long(code)
            else:
                s = _fetch_kr_index_volume_long(code)
            if len(s) > 0:
                result[name] = s
                print(f"  {label} [{i+1}/{len(items)}] {name}: {len(s)} days")
            else:
                print(f"  {label} [{i+1}/{len(items)}] {name}: empty")
        except Exception as e:
            print(f"  {label} [{i+1}/{len(items)}] {name}: ERROR {e}")
        time.sleep(0.05)
    return result


def winsorized_zscores(values, clip_pct=0.1):
    valid = [(i, v) for i, v in enumerate(values) if v is not None]
    result = [None] * len(values)
    if len(valid) < 2:
        return result
    sorted_vals = sorted(v for _, v in valid)
    n = len(sorted_vals)
    lo_idx = clip_pct * (n - 1)
    hi_idx = (1 - clip_pct) * (n - 1)
    lo_bound = sorted_vals[int(lo_idx)] + (lo_idx % 1) * (sorted_vals[min(int(lo_idx) + 1, n - 1)] - sorted_vals[int(lo_idx)])
    hi_bound = sorted_vals[int(hi_idx)] + (hi_idx % 1) * (sorted_vals[min(int(hi_idx) + 1, n - 1)] - sorted_vals[int(hi_idx)])
    clipped = {i: max(lo_bound, min(hi_bound, v)) for i, v in valid}
    mean = sum(clipped.values()) / len(clipped)
    std = (sum((c - mean) ** 2 for c in clipped.values()) / len(clipped)) ** 0.5
    for i, v in valid:
        result[i] = 0.0 if std == 0 else round((v - mean) / std, 4)
    return result


def _add_zscores(rows):
    rs_vals = [r.get("rs") for r in rows]
    vc_vals = [r.get("volume_change") for r in rows]
    rs_zs = winsorized_zscores(rs_vals)
    vc_zs = winsorized_zscores(vc_vals)
    for i, r in enumerate(rows):
        r["rs_z"] = rs_zs[i]
        r["vc_z"] = vc_zs[i]


WICS_US_CACHE_PATH = Path("cache/wics_us_etfs.json")


def compute_wics_us_etfs():
    tickers = WICS_US_ETFS
    print(f"[WICS-US] Downloading {len(tickers)} ETFs (2y close)...")
    try:
        df_close = yf.download(tickers, period="2y", progress=False, auto_adjust=True)
    except Exception as e:
        print(f"  Download failed: {e}")
        return {}

    print(f"[WICS-US] Downloading {len(tickers)} ETFs (3y volume)...")
    try:
        df_vol = yf.download(tickers, period="3y", progress=False, auto_adjust=True)
    except Exception as e:
        print(f"  Volume download failed: {e}")
        df_vol = None

    print("[WICS-US] Fetching US benchmark (^GSPC)...")
    try:
        bench_df = yf.download("^GSPC", period="2y", progress=False, auto_adjust=True)
        if isinstance(bench_df.columns, pd.MultiIndex):
            bench_series = bench_df["Close"]["^GSPC"].dropna()
        else:
            bench_series = bench_df["Close"].dropna()
    except Exception as e:
        print(f"  Benchmark failed: {e}")
        bench_series = pd.Series(dtype=float)

    result = {"last_updated": datetime.now().isoformat(timespec="seconds"), "etfs": {}}
    if len(bench_series) > 0:
        bench_sorted = bench_series.sort_index()
        result["benchmark_series"] = {
            "dates": [d.strftime("%Y-%m-%d") for d in bench_sorted.index],
            "closes": [_safe_float(v) for v in bench_sorted.values],
        }

    for ticker in tickers:
        try:
            if isinstance(df_close.columns, pd.MultiIndex):
                close_s = df_close["Close"][ticker].dropna()
            else:
                close_s = df_close["Close"].dropna()
        except Exception:
            continue

        vol_s = pd.Series(dtype=float)
        if df_vol is not None:
            try:
                if isinstance(df_vol.columns, pd.MultiIndex):
                    c = df_vol["Close"][ticker].dropna()
                    v = df_vol["Volume"][ticker].dropna()
                else:
                    c = df_vol["Close"].dropna()
                    v = df_vol["Volume"].dropna()
                common = c.index.intersection(v.index)
                vol_s = (c[common] * v[common]).dropna()
            except Exception:
                pass

        etf_data = {"ticker": ticker}
        for period_name, period_days in PERIODS.items():
            s_ret = None
            b_ret = None
            rs = None
            if len(close_s) >= period_days + 1:
                s_ret = round((close_s.iloc[-1] / close_s.iloc[-(period_days + 1)] - 1) * 100, 2)
            if len(bench_series) >= period_days + 1:
                b_ret = round((bench_series.iloc[-1] / bench_series.iloc[-(period_days + 1)] - 1) * 100, 2)
            if s_ret is not None and b_ret is not None:
                rs = round(s_ret - b_ret, 2)

            vc = compute_volume_change(vol_s, period_days) if len(vol_s) >= 2 * period_days else None

            etf_data[period_name] = {
                "sector_return": s_ret,
                "benchmark_return": b_ret,
                "rs": rs,
                "volume_change": vc,
            }

        close_sorted = close_s.sort_index()
        etf_data["series"] = {
            "dates": [d.strftime("%Y-%m-%d") for d in close_sorted.index],
            "closes": [_safe_float(v) for v in close_sorted.values],
        }

        result["etfs"][ticker] = etf_data
        print(f"  {ticker}: {len(close_s)} close days, {len(vol_s)} vol days")

    WICS_US_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(WICS_US_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"  Saved to {WICS_US_CACHE_PATH}")
    return result


def _fetch_us_dollar_volume(ticker_dict, label):
    tickers = list(ticker_dict.keys())
    print(f"  {label} volume: downloading {len(tickers)} ETFs (3y)...")
    try:
        df = yf.download(tickers, period="3y", progress=False, auto_adjust=True)
    except Exception as e:
        print(f"  {label} volume download failed: {e}")
        return {}
    result = {}
    for ticker in tickers:
        try:
            if isinstance(df.columns, pd.MultiIndex):
                c = df["Close"][ticker].dropna()
                v = df["Volume"][ticker].dropna()
            else:
                c = df["Close"].dropna()
                v = df["Volume"].dropna()
            common = c.index.intersection(v.index)
            dollar_vol = (c[common] * v[common]).dropna()
            if len(dollar_vol) > 0:
                result[ticker] = dollar_vol
                print(f"    {ticker}: {len(dollar_vol)} vol days")
        except Exception as e:
            print(f"    {ticker} volume: ERROR {e}")
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


def compute_volume_change(volume_series, period_days):
    if len(volume_series) < 2 * period_days:
        return None
    recent = volume_series.iloc[-period_days:]
    previous = volume_series.iloc[-2 * period_days:-period_days]
    prev_avg = previous.mean()
    if prev_avg == 0:
        return None
    return round((recent.mean() / prev_avg - 1) * 100, 2)


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


def _compute_kr_group(series_dict, benchmark, mapping, volume_dict=None):
    vol = volume_dict or {}
    result = {}
    for period_name, period_days in PERIODS.items():
        rows = []
        for name, s in series_dict.items():
            rs, s_ret, b_ret = compute_rs(s, benchmark, period_days)
            vc = None
            if name in vol:
                vc = compute_volume_change(vol[name], period_days)
            rows.append({
                "sector": name,
                "rs": rs,
                "sector_return": s_ret,
                "benchmark_return": b_ret,
                "mapped_us": mapping.get(name),
                "volume_change": vc,
            })
        _add_zscores(rows)
        rows.sort(key=lambda x: (x["rs"] is None, -(x["rs"] or 0)))
        result[period_name] = rows
    result["series"] = _build_series(series_dict, benchmark)
    return result


def _compute_us_group(series_dict, benchmark, names, kr_to_us_mapping, volume_dict=None):
    vol = volume_dict or {}
    rev = {}
    for kr_name, us_ticker in kr_to_us_mapping.items():
        rev.setdefault(us_ticker, []).append(kr_name)
    result = {}
    for period_name, period_days in PERIODS.items():
        rows = []
        for ticker, s in series_dict.items():
            rs, s_ret, b_ret = compute_rs(s, benchmark, period_days)
            vc = None
            if ticker in vol:
                vc = compute_volume_change(vol[ticker], period_days)
            rows.append({
                "ticker": ticker,
                "name": names.get(ticker, ""),
                "rs": rs,
                "sector_return": s_ret,
                "benchmark_return": b_ret,
                "mapped_kr": rev.get(ticker, []),
                "volume_change": vc,
            })
        _add_zscores(rows)
        rows.sort(key=lambda x: (x["rs"] is None, -(x["rs"] or 0)))
        result[period_name] = rows
    result["series"] = _build_series(series_dict, benchmark)
    return result


def _load_wics_kr_cache():
    path = Path("cache/wics_sectors.json")
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _build_wics_kr_result(wics_data):
    kr_wics = wics_data.get("kr_wics", {})
    for period_name in PERIODS:
        rows = kr_wics.get(period_name, [])
        for row in rows:
            code = row.get("code", "")
            mapping = WICS_TO_US_ETF.get(code, {})
            row["mapped_us"] = mapping.get("us_etf")
    return kr_wics


def _build_wics_us_result():
    if not WICS_US_CACHE_PATH.exists():
        return {}
    with open(WICS_US_CACHE_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    etfs = raw.get("etfs", {})

    us_to_wics = {}
    for code, mapping in WICS_TO_US_ETF.items():
        etf = mapping.get("us_etf")
        if etf:
            us_to_wics.setdefault(etf, []).append((code, mapping["name"]))

    result = {}
    for period_name in PERIODS:
        rows = []
        for ticker, etf_data in etfs.items():
            pd_data = etf_data.get(period_name, {})
            mapped = us_to_wics.get(ticker, [])
            rows.append({
                "etf": ticker,
                "mapped_kr_wics": [c for c, _ in mapped],
                "mapped_kr_names": [n for _, n in mapped],
                "sector_return": pd_data.get("sector_return"),
                "benchmark_return": pd_data.get("benchmark_return"),
                "rs": pd_data.get("rs"),
                "volume_change": pd_data.get("volume_change"),
            })
        _add_zscores(rows)
        rows.sort(key=lambda x: (x["rs"] is None, -(x["rs"] or 0)))
        result[period_name] = rows
    return result


_sector_custom_cache = {}
WICS_CACHE_SERIES_PATH = Path("cache/wics_sectors.json")
STOCK_PRICE_CACHE_PATH = Path("cache/stock_price_series.json")
STOCK_WICS_PATH = Path("cache/stock_wics.json")
STOCK_THEMES_PATH = Path("cache/stock_themes.json")


def _pd_series(dates, values):
    import pandas as pd
    pairs = [(d, v) for d, v in zip(dates, values) if v is not None]
    if not pairs:
        return pd.Series(dtype=float)
    idx = pd.DatetimeIndex([p[0] for p in pairs])
    s = pd.Series([p[1] for p in pairs], index=idx).sort_index()
    return s[~s.index.duplicated(keep="first")]


def _ret_abs_window(s, dt_s, dt_e):
    w = s.loc[dt_s:dt_e].dropna()
    if len(w) < 2:
        return None, None, None
    first = float(w.iloc[0])
    if abs(first) < 1e-9:
        return None, str(w.index[0].date()), str(w.index[-1].date())
    r = round((float(w.iloc[-1]) / first - 1) * 100, 2)
    return r, str(w.index[0].date()), str(w.index[-1].date())


def _ret_cum_window(s, dt_s, dt_e):
    w = s.loc[dt_s:dt_e].dropna()
    if len(w) < 2:
        return None
    denom = 1 + float(w.iloc[0]) / 100
    if abs(denom) < 1e-9:
        return None
    return round(((1 + float(w.iloc[-1]) / 100) / denom - 1) * 100, 2)


def _load_dvol_by_code():
    import pandas as pd
    if not STOCK_PRICE_CACHE_PATH.exists():
        return {}
    try:
        with open(STOCK_PRICE_CACHE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    out = {}
    for code, entry in raw.get("data", {}).items():
        if "dvol_dates" in entry and "dvol_vals" in entry:
            s = pd.Series(
                entry["dvol_vals"],
                index=pd.DatetimeIndex(entry["dvol_dates"]),
            ).sort_index()
            out[code] = s[~s.index.duplicated(keep="first")]
    return out


def _custom_group_vc(dvol_by_code, code_groups, dt_s, dt_e):
    import pandas as pd
    span = dt_e - dt_s
    prev_s = dt_s - span
    prev_e = dt_s - pd.Timedelta(days=1)
    out = {}
    for gk, codes in code_groups.items():
        series_list = [dvol_by_code[c] for c in codes if c in dvol_by_code]
        if not series_list:
            continue
        combined = pd.concat(series_list, axis=1).sum(axis=1).dropna()
        cur = combined.loc[dt_s:dt_e]
        prev = combined.loc[prev_s:prev_e]
        if len(cur) > 0 and len(prev) > 0:
            pa = prev.mean()
            if pa > 0:
                out[gk] = round((cur.mean() / pa - 1) * 100, 2)
    return out


def get_custom_bounds():
    starts = []
    ends = []

    def _scan(dates):
        if dates:
            starts.append(dates[0])
            ends.append(dates[-1])

    if CACHE_PATH.exists():
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            sl = json.load(f)
        for path in [
            ("kr", "themes"), ("kr", "sizes"),
        ]:
            ser = sl.get(path[0], {}).get(path[1], {}).get("series", {})
            _scan(ser.get("_benchmark", {}).get("dates", []))
        _scan(sl.get("us", {}).get("series", {}).get("_benchmark", {}).get("dates", []))
        _scan(sl.get("us", {}).get("themes", {}).get("series", {}).get("_benchmark", {}).get("dates", []))
        _scan(sl.get("us", {}).get("sizes", {}).get("series", {}).get("_benchmark", {}).get("dates", []))

    if WICS_CACHE_SERIES_PATH.exists():
        with open(WICS_CACHE_SERIES_PATH, "r", encoding="utf-8") as f:
            wraw = json.load(f)
        _scan(wraw.get("series", {}).get("_benchmark", {}).get("dates", []))

    if WICS_US_CACHE_PATH.exists():
        with open(WICS_US_CACHE_PATH, "r", encoding="utf-8") as f:
            wus = json.load(f)
        _scan(wus.get("benchmark_series", {}).get("dates", []))

    if not starts or not ends:
        return {"oldest": None, "newest": None}
    return {"oldest": max(starts), "newest": min(ends)}


def compute_sector_leaders_custom(start_date, end_date):
    import pandas as pd

    cache_key = f"{start_date}:{end_date}"
    cached = _sector_custom_cache.get(cache_key)
    if cached and time.time() - cached["ts"] < 3600:
        return cached["data"]

    dt_s = pd.Timestamp(start_date)
    dt_e = pd.Timestamp(end_date)

    if not CACHE_PATH.exists():
        return {"error": "sector_leaders.json 캐시가 없습니다 – 먼저 배치를 실행하세요"}
    with open(CACHE_PATH, "r", encoding="utf-8") as f:
        sl = json.load(f)

    wics_kr_series = {}
    wics_kr_bench = pd.Series(dtype=float)
    wics_kr_base = {}
    if WICS_CACHE_SERIES_PATH.exists():
        with open(WICS_CACHE_SERIES_PATH, "r", encoding="utf-8") as f:
            wraw = json.load(f)
        wser = wraw.get("series", {})
        wbench = wser.get("_benchmark", {})
        wdates = wbench.get("dates", [])
        wics_kr_bench = _pd_series(wdates, wbench.get("values", []))
        for code, sd in wser.items():
            if code.startswith("_"):
                continue
            wics_kr_series[code] = _pd_series(wdates, sd.get("values", []))
        for row in (wraw.get("kr_wics", {}).get("1y", [])):
            wics_kr_base[row["code"]] = row

    dvol_by_code = _load_dvol_by_code()

    wics_map = {}
    if STOCK_WICS_PATH.exists():
        with open(STOCK_WICS_PATH, "r", encoding="utf-8") as f:
            wmap_raw = json.load(f)
        wics_map = wmap_raw.get("mapping", wmap_raw)
    wics_groups = {}
    for code, info in wics_map.items():
        mcls = info.get("wics_mcls_cd")
        if mcls:
            wics_groups.setdefault(mcls, []).append(code)

    theme_groups = {}
    if STOCK_THEMES_PATH.exists():
        with open(STOCK_THEMES_PATH, "r", encoding="utf-8") as f:
            traw = json.load(f)
        for _code, entry in traw.get("themes", {}).items():
            nm = entry.get("name")
            cons = entry.get("constituents", [])
            if nm and cons:
                theme_groups[nm] = cons

    actual = {"start": None, "end": None}

    def _track(a, b):
        if a and (actual["start"] is None or a < actual["start"]):
            actual["start"] = a
        if b and (actual["end"] is None or b > actual["end"]):
            actual["end"] = b

    kr_wics_vc = _custom_group_vc(dvol_by_code, wics_groups, dt_s, dt_e)
    kr_theme_vc = _custom_group_vc(dvol_by_code, theme_groups, dt_s, dt_e)

    kr_wics_rows = []
    for code, s in wics_kr_series.items():
        base = wics_kr_base.get(code, {})
        sec_ret = _ret_cum_window(s, dt_s, dt_e)
        bench_ret, a0, a1 = _ret_abs_window(wics_kr_bench, dt_s, dt_e)
        _track(a0, a1)
        rs = None
        if sec_ret is not None and bench_ret is not None:
            rs = round(sec_ret - bench_ret, 2)
        kr_wics_rows.append({
            "code": code,
            "name": base.get("name", WICS_TO_US_ETF.get(code, {}).get("name", code)),
            "lcls_code": base.get("lcls_code", ""),
            "lcls_name": base.get("lcls_name", ""),
            "sector_return": sec_ret,
            "benchmark_return": bench_ret,
            "rs": rs,
            "volume_change": kr_wics_vc.get(code),
            "mapped_us": WICS_TO_US_ETF.get(code, {}).get("us_etf"),
        })
    _add_zscores(kr_wics_rows)
    kr_wics_rows.sort(key=lambda x: (x["rs"] is None, -(x["rs"] or 0)))

    def _kr_group_rows(group_key, mapping, vc_dict):
        grp = sl.get("kr", {}).get(group_key, {})
        gser = grp.get("series", {})
        gbench = gser.get("_benchmark", {})
        gdates = gbench.get("dates", [])
        bench_s = _pd_series(gdates, gbench.get("values", []))
        bench_ret, a0, a1 = _ret_abs_window(bench_s, dt_s, dt_e)
        _track(a0, a1)
        rows = []
        for name, sd in gser.items():
            if name.startswith("_"):
                continue
            s = _pd_series(gdates, sd.get("values", []))
            sec_ret, _, _ = _ret_abs_window(s, dt_s, dt_e)
            rs = None
            if sec_ret is not None and bench_ret is not None:
                rs = round(sec_ret - bench_ret, 2)
            rows.append({
                "sector": name,
                "rs": rs,
                "sector_return": sec_ret,
                "benchmark_return": bench_ret,
                "mapped_us": mapping.get(name),
                "volume_change": vc_dict.get(name),
            })
        _add_zscores(rows)
        rows.sort(key=lambda x: (x["rs"] is None, -(x["rs"] or 0)))
        return rows

    kr_theme_rows = _kr_group_rows("themes", THEME_MAPPING, kr_theme_vc)
    kr_size_rows = _kr_group_rows("sizes", SIZE_MAPPING, {})

    def _us_rs_rows(series_dict, name_map, mapped_map, label_field):
        bench = series_dict.get("_benchmark", {})
        bdates = bench.get("dates", [])
        bench_s = _pd_series(bdates, bench.get("values", []))
        bench_ret, a0, a1 = _ret_abs_window(bench_s, dt_s, dt_e)
        _track(a0, a1)
        rows = []
        for key, sd in series_dict.items():
            if key.startswith("_"):
                continue
            s = _pd_series(bdates, sd.get("values", []))
            sec_ret, _, _ = _ret_abs_window(s, dt_s, dt_e)
            rs = None
            if sec_ret is not None and bench_ret is not None:
                rs = round(sec_ret - bench_ret, 2)
            row = {
                label_field: key,
                "rs": rs,
                "sector_return": sec_ret,
                "benchmark_return": bench_ret,
                "volume_change": None,
            }
            if name_map is not None:
                row["name"] = name_map.get(key, "")
            if mapped_map is not None:
                row["mapped_kr"] = mapped_map.get(key, [])
            rows.append(row)
        rs_zs = winsorized_zscores([r["rs"] for r in rows])
        for i, r in enumerate(rows):
            r["rs_z"] = rs_zs[i]
            r["vc_z"] = None
        rows.sort(key=lambda x: (x["rs"] is None, -(x["rs"] or 0)))
        return rows

    rev_map = _reverse_mapping()
    us_legacy = _us_rs_rows(sl.get("us", {}).get("series", {}), US_SECTORS, rev_map, "ticker")

    us_theme_rev = {}
    for kr_name, us_t in THEME_MAPPING.items():
        us_theme_rev.setdefault(us_t, []).append(kr_name)
    us_themes = _us_rs_rows(
        sl.get("us", {}).get("themes", {}).get("series", {}),
        {t: US_THEMES.get(t, "") for t in US_THEMES},
        us_theme_rev, "ticker",
    )

    us_size_rev = {}
    for kr_name, us_t in SIZE_MAPPING.items():
        us_size_rev.setdefault(us_t, []).append(kr_name)
    us_sizes = _us_rs_rows(
        sl.get("us", {}).get("sizes", {}).get("series", {}),
        {t: US_SIZES.get(t, "") for t in US_SIZES},
        us_size_rev, "ticker",
    )

    us_wics_rows = []
    if WICS_US_CACHE_PATH.exists():
        with open(WICS_US_CACHE_PATH, "r", encoding="utf-8") as f:
            wus = json.load(f)
        wus_bench = wus.get("benchmark_series", {})
        wus_bench_s = _pd_series(wus_bench.get("dates", []), wus_bench.get("closes", []))
        bench_ret, a0, a1 = _ret_abs_window(wus_bench_s, dt_s, dt_e)
        _track(a0, a1)
        us_to_wics = {}
        for code, mp in WICS_TO_US_ETF.items():
            etf = mp.get("us_etf")
            if etf:
                us_to_wics.setdefault(etf, []).append((code, mp["name"]))
        for ticker, etf_data in wus.get("etfs", {}).items():
            ser = etf_data.get("series", {})
            s = _pd_series(ser.get("dates", []), ser.get("closes", []))
            sec_ret, _, _ = _ret_abs_window(s, dt_s, dt_e)
            rs = None
            if sec_ret is not None and bench_ret is not None:
                rs = round(sec_ret - bench_ret, 2)
            mapped = us_to_wics.get(ticker, [])
            us_wics_rows.append({
                "etf": ticker,
                "mapped_kr_wics": [c for c, _ in mapped],
                "mapped_kr_names": [n for _, n in mapped],
                "sector_return": sec_ret,
                "benchmark_return": bench_ret,
                "rs": rs,
                "volume_change": None,
            })
        rs_zs = winsorized_zscores([r["rs"] for r in us_wics_rows])
        for i, r in enumerate(us_wics_rows):
            r["rs_z"] = rs_zs[i]
            r["vc_z"] = None
        us_wics_rows.sort(key=lambda x: (x["rs"] is None, -(x["rs"] or 0)))

    result = {
        "start_date": start_date,
        "end_date": end_date,
        "actual_start": actual["start"] or start_date,
        "actual_end": actual["end"] or end_date,
        "kr": {
            "wics_sectors": kr_wics_rows,
            "themes": kr_theme_rows,
            "sizes": kr_size_rows,
        },
        "us": {
            "sectors": us_legacy,
            "wics_sectors": us_wics_rows,
            "themes": us_themes,
            "sizes": us_sizes,
        },
    }

    _sector_custom_cache[cache_key] = {"ts": time.time(), "data": result}
    return result


def compute_all_scores():
    print("[1/7] Fetching KR benchmark + themes + sizes...")
    kr_bench = fetch_kr_benchmark_series()
    kr_themes = _fetch_kr_group_series(KR_THEMES, "KR theme")
    kr_sizes = _fetch_kr_group_series(KR_SIZES, "KR size")

    print("[2/7] Fetching KR volume data (2y)...")
    kr_theme_vol = _fetch_kr_group_volume(KR_THEMES, "KR theme vol")
    kr_size_vol = _fetch_kr_group_volume(KR_SIZES, "KR size vol")

    print("[3/7] Fetching US sector data...")
    us_sectors = fetch_us_sector_series()
    us_bench = fetch_us_benchmark_series()

    print("[4/7] Fetching US themes + sizes...")
    us_themes = _fetch_us_etfs(US_THEMES, "US themes")
    us_sizes = _fetch_us_etfs(US_SIZES, "US sizes")

    print("[5/7] Fetching US volume data (3y)...")
    us_sector_vol = _fetch_us_dollar_volume(US_SECTORS, "US sectors")
    us_theme_vol = _fetch_us_dollar_volume(US_THEMES, "US themes")
    us_size_vol = _fetch_us_dollar_volume(US_SIZES, "US sizes")

    rev_map = _reverse_mapping()

    result = {
        "last_updated": datetime.now().isoformat(timespec="seconds"),
        "kr": {},
        "us": {},
    }

    print("[6/7] Computing US RS scores...")
    for period_name, period_days in PERIODS.items():
        rows = []
        for ticker, series in us_sectors.items():
            rs, s_ret, b_ret = compute_rs(series, us_bench, period_days)
            vc = compute_volume_change(
                us_sector_vol.get(ticker, pd.Series(dtype=float)), period_days
            )
            rows.append({
                "ticker": ticker,
                "name": US_SECTORS.get(ticker, ""),
                "rs": rs,
                "sector_return": s_ret,
                "benchmark_return": b_ret,
                "mapped_kr": rev_map.get(ticker, []),
                "volume_change": vc,
            })
        _add_zscores(rows)
        rows.sort(key=lambda x: (x["rs"] is None, -(x["rs"] or 0)))
        result["us"][period_name] = rows

    result["us"]["series"] = _build_series(us_sectors, us_bench)
    result["kr"]["themes"] = _compute_kr_group(kr_themes, kr_bench, THEME_MAPPING, kr_theme_vol)
    result["kr"]["sizes"] = _compute_kr_group(kr_sizes, kr_bench, SIZE_MAPPING, kr_size_vol)
    result["us"]["themes"] = _compute_us_group(us_themes, us_bench, US_THEMES, THEME_MAPPING, us_theme_vol)
    result["us"]["sizes"] = _compute_us_group(us_sizes, us_bench, US_SIZES, SIZE_MAPPING, us_size_vol)

    print("[7/7] Loading WICS KR sector data...")
    wics_kr = _load_wics_kr_cache()
    if wics_kr:
        result["kr"]["wics_sectors"] = _build_wics_kr_result(wics_kr)

    print("  Computing WICS US ETF data...")
    compute_wics_us_etfs()
    result["us"]["wics_sectors"] = _build_wics_us_result()

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
        print("[KR only] Fetching KR benchmark + themes + sizes...")
        kr_bench = fetch_kr_benchmark_series()
        kr_themes = _fetch_kr_group_series(KR_THEMES, "KR theme")
        kr_sizes = _fetch_kr_group_series(KR_SIZES, "KR size")
        print("[KR only] Fetching KR volume data (2y)...")
        kr_theme_vol = _fetch_kr_group_volume(KR_THEMES, "KR theme vol")
        kr_size_vol = _fetch_kr_group_volume(KR_SIZES, "KR size vol")
        kr_result = {}
        kr_result["themes"] = _compute_kr_group(kr_themes, kr_bench, THEME_MAPPING, kr_theme_vol)
        kr_result["sizes"] = _compute_kr_group(kr_sizes, kr_bench, SIZE_MAPPING, kr_size_vol)
        wics_kr = _load_wics_kr_cache()
        if wics_kr:
            kr_result["wics_sectors"] = _build_wics_kr_result(wics_kr)
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
        print("[US only] Fetching US volume data (3y)...")
        us_sector_vol = _fetch_us_dollar_volume(US_SECTORS, "US sectors")
        us_theme_vol = _fetch_us_dollar_volume(US_THEMES, "US themes")
        us_size_vol = _fetch_us_dollar_volume(US_SIZES, "US sizes")
        rev_map = _reverse_mapping()
        us_result = {}
        for period_name, period_days in PERIODS.items():
            rows = []
            for ticker, series in us_sectors.items():
                rs, s_ret, b_ret = compute_rs(series, us_bench, period_days)
                vc = compute_volume_change(
                    us_sector_vol.get(ticker, pd.Series(dtype=float)), period_days
                )
                rows.append({
                    "ticker": ticker,
                    "name": US_SECTORS.get(ticker, ""),
                    "rs": rs,
                    "sector_return": s_ret,
                    "benchmark_return": b_ret,
                    "mapped_kr": rev_map.get(ticker, []),
                    "volume_change": vc,
                })
            _add_zscores(rows)
            rows.sort(key=lambda x: (x["rs"] is None, -(x["rs"] or 0)))
            us_result[period_name] = rows
        us_result["series"] = _build_series(us_sectors, us_bench)
        us_result["themes"] = _compute_us_group(us_themes, us_bench, US_THEMES, THEME_MAPPING, us_theme_vol)
        us_result["sizes"] = _compute_us_group(us_sizes, us_bench, US_SIZES, SIZE_MAPPING, us_size_vol)
        compute_wics_us_etfs()
        us_result["wics_sectors"] = _build_wics_us_result()
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
        kr_wics = (data.get("kr", {}).get("wics_sectors") or {}).get(period_name, [])
        if kr_wics:
            top3 = [s for s in kr_wics if s["rs"] is not None][:3]
            labels = [f"{s['name']}({s['rs']:+.1f}%)" for s in top3]
            print(f"  KR top 3: {', '.join(labels)}")
        if data.get("us", {}).get(period_name):
            top3 = [s for s in data["us"][period_name] if s["rs"] is not None][:3]
            labels = [f"{s['name']}({s['rs']:+.1f}%)" for s in top3]
            print(f"  US top 3: {', '.join(labels)}")


if __name__ == "__main__":
    main(sys.argv)
