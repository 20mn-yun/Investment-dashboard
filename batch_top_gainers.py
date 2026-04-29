#!/usr/bin/env python3
"""배치로 상승률 TOP 10을 계산하여 cache/ 에 저장."""
import sys
import os
import json
import time
import io
from datetime import datetime

import pandas as pd
import yfinance as yf
import requests as req

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TICKERS_DIR = os.path.join(BASE_DIR, "tickers")
CACHE_DIR = os.path.join(BASE_DIR, "cache")

OUTLIER_THRESHOLDS = {
    "1d": 50.0,
    "1w": 100.0,
    "1mo": 200.0,
}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_russell1000():
    path = os.path.join(TICKERS_DIR, "us_russell1000.json")
    if os.path.exists(path):
        age_days = (time.time() - os.path.getmtime(path)) / 86400
        if age_days < 7:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            log(f"Cached ticker list loaded: {len(data)} tickers (age: {age_days:.1f}d)")
            return data

    log("Downloading Russell 1000 holdings from iShares...")
    url = (
        "https://www.ishares.com/us/products/239707/"
        "ishares-russell-1000-etf/1467271812596.ajax"
        "?fileType=csv&fileName=IWB_holdings&dataType=fund"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/csv,text/plain,*/*",
    }
    resp = req.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        print(f"ERROR: iShares returned HTTP {resp.status_code}", file=sys.stderr)
        print(f"Response headers: {dict(resp.headers)}", file=sys.stderr)
        print(f"Body preview: {resp.text[:500]}", file=sys.stderr)
        sys.exit(1)

    lines = resp.text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if line.startswith("Ticker,") or line.startswith('"Ticker"'):
            header_idx = i
            break
    if header_idx is None:
        print("ERROR: Could not find CSV header row", file=sys.stderr)
        print(f"First 15 lines:\n" + "\n".join(lines[:15]), file=sys.stderr)
        sys.exit(1)

    csv_text = "\n".join(lines[header_idx:])
    df = pd.read_csv(io.StringIO(csv_text))

    if "Asset Class" in df.columns:
        df = df[df["Asset Class"] == "Equity"]

    tickers = []
    for _, row in df.iterrows():
        t = str(row.get("Ticker", "")).strip()
        if not t or t == "-" or t == "nan":
            continue
        t = t.replace(".", "-")
        name = str(row.get("Name", "")).strip()
        sector = str(row.get("Sector", "")).strip()
        if name == "nan":
            name = t
        if sector == "nan":
            sector = "-"
        tickers.append({"ticker": t, "name": name, "sector": sector})

    os.makedirs(TICKERS_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tickers, f, ensure_ascii=False, indent=1)
    log(f"Saved {len(tickers)} tickers to {path}")
    return tickers


def load_russell3000():
    path = os.path.join(TICKERS_DIR, "us_russell3000.json")
    if os.path.exists(path):
        age_days = (time.time() - os.path.getmtime(path)) / 86400
        if age_days < 7:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            log(f"Cached ticker list loaded: {len(data)} tickers (age: {age_days:.1f}d)")
            return data

    log("Downloading Russell 3000 holdings from iShares...")
    url = (
        "https://www.ishares.com/us/products/239714/"
        "ishares-russell-3000-etf/1467271812596.ajax"
        "?fileType=csv&fileName=IWV_holdings&dataType=fund"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/csv,text/plain,*/*",
    }
    resp = req.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        print(f"ERROR: iShares returned HTTP {resp.status_code}", file=sys.stderr)
        print(f"Response headers: {dict(resp.headers)}", file=sys.stderr)
        print(f"Body preview: {resp.text[:500]}", file=sys.stderr)
        sys.exit(1)

    lines = resp.text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if line.startswith("Ticker,") or line.startswith('"Ticker"'):
            header_idx = i
            break
    if header_idx is None:
        print("ERROR: Could not find CSV header row", file=sys.stderr)
        print(f"First 15 lines:\n" + "\n".join(lines[:15]), file=sys.stderr)
        sys.exit(1)

    csv_text = "\n".join(lines[header_idx:])
    df = pd.read_csv(io.StringIO(csv_text))

    if "Asset Class" in df.columns:
        df = df[df["Asset Class"] == "Equity"]

    tickers = []
    for _, row in df.iterrows():
        t = str(row.get("Ticker", "")).strip()
        if not t or t == "-" or t == "nan":
            continue
        t = t.replace(".", "-")
        name = str(row.get("Name", "")).strip()
        sector = str(row.get("Sector", "")).strip()
        if name == "nan":
            name = t
        if sector == "nan":
            sector = "-"
        tickers.append({"ticker": t, "name": name, "sector": sector})

    os.makedirs(TICKERS_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tickers, f, ensure_ascii=False, indent=1)
    log(f"Saved {len(tickers)} tickers to {path}")
    return tickers


def load_kr_top600(top_n=300):
    path = os.path.join(TICKERS_DIR, "kr_top600.json")
    if os.path.exists(path):
        age_days = (time.time() - os.path.getmtime(path)) / 86400
        if age_days < 7:
            with open(path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            tickers = cached.get("tickers", [])
            log(f"Cached KR ticker list loaded: {len(tickers)} tickers (age: {age_days:.1f}d)")
            return tickers

    log("Downloading KR top stocks from Naver Finance...")
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
    tickers = []

    for market, suffix in [("KOSPI", ".KS"), ("KOSDAQ", ".KQ")]:
        market_tickers = []
        for page in range(1, (top_n // 100) + 2):
            url = (f"https://m.stock.naver.com/api/stocks/marketValue/{market}"
                   f"?page={page}&pageSize=100")
            resp = req.get(url, headers=headers, timeout=15)
            if resp.status_code != 200:
                break
            stocks = resp.json().get("stocks", [])
            if not stocks:
                break
            for s in stocks:
                code = s.get("itemCode", "")
                name = s.get("stockName", "")
                stock_end_type = s.get("stockEndType", "")
                if stock_end_type != "stock":
                    continue
                if not code or not code.isdigit():
                    continue
                market_tickers.append({
                    "ticker": code + suffix,
                    "code": code,
                    "name": name,
                    "market_sub": market,
                    "sector": "",
                })
                if len(market_tickers) >= top_n:
                    break
            if len(market_tickers) >= top_n:
                break
        tickers.extend(market_tickers[:top_n])
        log(f"  {market}: {len(market_tickers[:top_n])} stocks")

    from zoneinfo import ZoneInfo
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    os.makedirs(TICKERS_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "last_updated": now_kst.isoformat(),
            "source": "Naver Finance",
            "universe": f"KOSPI top {top_n} + KOSDAQ top {top_n}",
            "tickers": tickers,
        }, f, ensure_ascii=False, indent=1)
    log(f"Saved {len(tickers)} KR tickers to {path}")
    return tickers


def load_jp_nikkei225():
    path = os.path.join(TICKERS_DIR, "jp_nikkei225.json")
    if os.path.exists(path):
        age_days = (time.time() - os.path.getmtime(path)) / 86400
        if age_days < 30:
            with open(path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            tickers = cached.get("tickers", [])
            log(f"Cached JP ticker list loaded: {len(tickers)} tickers (age: {age_days:.1f}d)")
            return tickers

    log("Downloading Nikkei 225 component list...")
    print("WARNING: iShares JP TOPIX ETF URLs unavailable, using Nikkei 225 fallback (225 tickers instead of 500)", file=sys.stderr)

    url = "https://topforeignstocks.com/indices/the-components-of-the-nikkei-225-index/"
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
    resp = req.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        print(f"ERROR: topforeignstocks returned HTTP {resp.status_code}", file=sys.stderr)
        sys.exit(1)

    tables = pd.read_html(io.StringIO(resp.text), flavor="lxml")
    if not tables:
        print("ERROR: Could not find Nikkei 225 table", file=sys.stderr)
        sys.exit(1)

    df = tables[0]
    tickers = []
    for _, row in df.iterrows():
        code = str(row.get("Code", "")).strip()
        name = str(row.get("Company Name", "")).strip()
        sector = str(row.get("Sector", "")).strip()
        if not code or code == "nan":
            continue
        if name == "nan":
            name = code
        if sector == "nan":
            sector = "-"
        ticker = code if ".T" in code else code + ".T"
        tickers.append({
            "ticker": ticker,
            "code": code.replace(".T", ""),
            "name": name,
            "sector": sector,
        })

    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Tokyo"))
    os.makedirs(TICKERS_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "last_updated": now.isoformat(),
            "source": "Wikipedia Nikkei 225",
            "universe": "Nikkei 225",
            "tickers": tickers,
        }, f, ensure_ascii=False, indent=1)
    log(f"Saved {len(tickers)} JP tickers to {path}")
    return tickers


_EU_EXCHANGE_SUFFIX = {
    "Xetra": ".DE",
    "London Stock Exchange": ".L",
    "Euronext Amsterdam": ".AS",
    "Nyse Euronext - Euronext Paris": ".PA",
    "SIX Swiss Exchange": ".SW",
    "Borsa Italiana": ".MI",
    "Bolsa De Madrid": ".MC",
    "Nasdaq Omx Nordic": ".ST",
    "Nasdaq Omx Helsinki Ltd.": ".HE",
    "Omx Nordic Exchange Copenhagen A/S": ".CO",
    "Nyse Euronext - Euronext Brussels": ".BR",
    "Nyse Euronext - Euronext Lisbon": ".LS",
    "Oslo Bors Asa": ".OL",
    "Irish Stock Exchange - All Market": ".IR",
    "Wiener Boerse Ag": ".VI",
    "Warsaw Stock Exchange/Equities/Main Market": ".WA",
}


def load_eu_stoxx600():
    path = os.path.join(TICKERS_DIR, "eu_stoxx600.json")
    if os.path.exists(path):
        age_days = (time.time() - os.path.getmtime(path)) / 86400
        if age_days < 7:
            with open(path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            tickers = cached.get("tickers", [])
            log(f"Cached EU ticker list loaded: {len(tickers)} tickers (age: {age_days:.1f}d)")
            return tickers

    log("Downloading STOXX 600 holdings from iShares DE...")
    url = (
        "https://www.ishares.com/de/privatanleger/de/produkte/251931/"
        "ishares-stoxx-europe-600-ucits-etf-de-fund/1478358465952.ajax"
        "?fileType=csv&fileName=EXSA_holdings&dataType=fund"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/csv,text/plain,*/*",
    }
    resp = req.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        print(f"ERROR: iShares DE returned HTTP {resp.status_code}", file=sys.stderr)
        sys.exit(1)

    lines = resp.text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if "Emittententicker" in line or "Ticker" in line:
            header_idx = i
            break
    if header_idx is None:
        print("ERROR: Could not find CSV header row in iShares DE", file=sys.stderr)
        sys.exit(1)

    csv_text = "\n".join(lines[header_idx:])
    df = pd.read_csv(io.StringIO(csv_text))

    col_ticker = "Emittententicker" if "Emittententicker" in df.columns else "Ticker"
    col_name = "Name" if "Name" in df.columns else df.columns[1]
    col_sector = "Sektor" if "Sektor" in df.columns else "Sector"
    col_asset = "Anlageklasse" if "Anlageklasse" in df.columns else "Asset Class"
    col_exchange = "Börse" if "Börse" in df.columns else "Exchange"

    if col_asset in df.columns:
        df = df[df[col_asset] == "Aktien"]

    tickers = []
    for _, row in df.iterrows():
        t = str(row.get(col_ticker, "")).strip()
        if not t or t == "-" or t == "nan":
            continue
        name = str(row.get(col_name, "")).strip()
        sector = str(row.get(col_sector, "")).strip()
        exchange = str(row.get(col_exchange, "")).strip()
        if name == "nan":
            name = t
        if sector == "nan":
            sector = "-"

        suffix = _EU_EXCHANGE_SUFFIX.get(exchange, "")
        if not suffix:
            for key, val in _EU_EXCHANGE_SUFFIX.items():
                if key.lower() in exchange.lower():
                    suffix = val
                    break
        if not suffix:
            suffix = ".DE"

        yahoo_ticker = t.replace(" ", "-").replace(".", "-").rstrip("-") + suffix
        tickers.append({
            "ticker": yahoo_ticker,
            "raw_ticker": t,
            "name": name,
            "sector": sector,
            "exchange": exchange,
        })

    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Europe/Berlin"))
    os.makedirs(TICKERS_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "last_updated": now.isoformat(),
            "source": "iShares STOXX Europe 600 (DE)",
            "universe": "STOXX Europe 600",
            "tickers": tickers,
        }, f, ensure_ascii=False, indent=1)
    log(f"Saved {len(tickers)} EU tickers to {path}")
    return tickers


def _download_chunk(chunk):
    """단일 청크 다운로드. 성공 dict + 미수신 리스트 반환."""
    got = {}
    missed = []
    try:
        df = yf.download(
            tickers=chunk, period="3mo", interval="1d",
            group_by="ticker", auto_adjust=True,
            progress=False, threads=False,
        )
        if df.empty:
            return got, chunk
        if len(chunk) == 1:
            closes = df["Close"].dropna()
            if len(closes) >= 2:
                got[chunk[0]] = closes
            else:
                missed.append(chunk[0])
        else:
            for sym in chunk:
                try:
                    closes = df[sym]["Close"].dropna()
                    if len(closes) >= 2:
                        got[sym] = closes
                    else:
                        missed.append(sym)
                except (KeyError, TypeError):
                    missed.append(sym)
    except Exception:
        missed = chunk
    return got, missed


def download_prices(ticker_list, chunk_size=50):
    symbols = [t["ticker"] for t in ticker_list]
    all_data = {}
    total_chunks = (len(symbols) + chunk_size - 1) // chunk_size

    # 1차: 50개씩 다운로드
    retry_queue = []
    for ci in range(0, len(symbols), chunk_size):
        chunk = symbols[ci:ci + chunk_size]
        chunk_num = ci // chunk_size + 1
        log(f"Downloading chunk {chunk_num}/{total_chunks} ({len(chunk)} tickers)")
        got, missed = _download_chunk(chunk)
        all_data.update(got)
        retry_queue.extend(missed)
        if ci + chunk_size < len(symbols):
            time.sleep(2)

    # 2차: 실패 티커를 20개씩 재시도 (sleep 길게)
    if retry_queue:
        log(f"Retrying {len(retry_queue)} failed tickers (20/chunk, 5s gap)...")
        time.sleep(10)
        still_failed = []
        for ci in range(0, len(retry_queue), 20):
            chunk = retry_queue[ci:ci + 20]
            got, missed = _download_chunk(chunk)
            all_data.update(got)
            still_failed.extend(missed)
            if ci + 20 < len(retry_queue):
                time.sleep(5)
    else:
        still_failed = []

    # 3차: 아직 실패한 것 10개씩 마지막 시도
    if still_failed:
        log(f"Final retry for {len(still_failed)} tickers (10/chunk, 8s gap)...")
        time.sleep(15)
        final_failed = []
        for ci in range(0, len(still_failed), 10):
            chunk = still_failed[ci:ci + 10]
            got, missed = _download_chunk(chunk)
            all_data.update(got)
            final_failed.extend(missed)
            if ci + 10 < len(still_failed):
                time.sleep(8)
    else:
        final_failed = []

    log(f"Downloaded {len(all_data)} tickers, {len(final_failed)} failed")
    return all_data, final_failed


def calc_rankings(price_data, ticker_meta):
    meta_map = {t["ticker"]: t for t in ticker_meta}
    periods = {"1d": 1, "1w": 5, "1mo": 21}
    result = {}
    excluded_counts = {}

    for period_key, lookback in periods.items():
        changes = []
        for sym, series in price_data.items():
            if len(series) < lookback + 1:
                continue
            cur = float(series.iloc[-1])
            prev = float(series.iloc[-(lookback + 1)])
            if prev <= 0:
                continue
            pct = (cur / prev - 1) * 100
            changes.append((sym, pct))

        changes.sort(key=lambda x: x[1], reverse=True)
        threshold = OUTLIER_THRESHOLDS.get(period_key, float("inf"))
        filtered_changes = [(s, p) for s, p in changes if p <= threshold]
        excluded = [(s, p) for s, p in changes if p > threshold]
        if excluded:
            excluded_str = ", ".join(f"{s}({p:+.1f}%)" for s, p in excluded[:5])
            log(f"  [{period_key}] outlier 제외 {len(excluded)}개: {excluded_str}{'...' if len(excluded) > 5 else ''}")
        excluded_counts[period_key] = len(excluded)
        top = filtered_changes[:10]

        items = []
        for sym, pct in top:
            m = meta_map.get(sym, {})
            items.append({
                "ticker": sym,
                "name": m.get("name", sym),
                "sector": m.get("sector", "-"),
                "industry": "",
                "change_pct": round(pct, 2),
            })
        result[period_key] = items

    return result, excluded_counts


def main():
    if len(sys.argv) < 2:
        print("Usage: python batch_top_gainers.py <market>", file=sys.stderr)
        sys.exit(1)

    market = sys.argv[1]
    if market not in ("us", "kr", "jp", "eu"):
        print(f"ERROR: market '{market}' not yet supported", file=sys.stderr)
        sys.exit(1)

    log(f"=== Batch top gainers: {market} ===")

    if market == "us":
        tickers = load_russell3000()
        universe_label = "Russell 3000"
    elif market == "kr":
        tickers = load_kr_top600()
        universe_label = "KOSPI top 300 + KOSDAQ top 300"
    elif market == "jp":
        tickers = load_jp_nikkei225()
        universe_label = "Nikkei 225"
    elif market == "eu":
        tickers = load_eu_stoxx600()
        universe_label = "STOXX Europe 600"

    log(f"Loaded {len(tickers)} tickers")

    price_data, failed = download_prices(tickers)

    fail_rate = len(failed) / max(len(tickers), 1) * 100
    if fail_rate > 15:
        print(f"WARNING: high failure rate {fail_rate:.1f}% ({len(failed)}/{len(tickers)})", file=sys.stderr)

    rankings, excluded_counts = calc_rankings(price_data, tickers)

    from zoneinfo import ZoneInfo
    tz_map = {"us": "America/New_York", "kr": "Asia/Seoul", "jp": "Asia/Tokyo", "eu": "Europe/Berlin"}
    now = datetime.now(ZoneInfo(tz_map.get(market, "Asia/Seoul")))

    output = {
        "market": market,
        "universe": universe_label,
        "universe_size": len(tickers),
        "last_updated": now.isoformat(),
        "failed_tickers": failed[:50],
        "failed_count": len(failed),
        "data": rankings,
        "excluded_counts": excluded_counts,
    }

    os.makedirs(CACHE_DIR, exist_ok=True)
    out_path = os.path.join(CACHE_DIR, f"top_gainers_{market}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=1)

    log(f"Saved to {out_path}")
    for p in ["1d", "1w", "1mo"]:
        items = rankings.get(p, [])
        if items:
            log(f"  {p} top: {items[0]['ticker']} ({items[0]['change_pct']:+.2f}%)")
    log("=== Done ===")


if __name__ == "__main__":
    main()
