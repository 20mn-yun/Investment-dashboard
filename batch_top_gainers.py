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
        top = changes[:10]

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

    return result


def main():
    if len(sys.argv) < 2:
        print("Usage: python batch_top_gainers.py <market>", file=sys.stderr)
        sys.exit(1)

    market = sys.argv[1]
    if market != "us":
        print(f"ERROR: market '{market}' not yet supported", file=sys.stderr)
        sys.exit(1)

    log(f"=== Batch top gainers: {market} ===")
    tickers = load_russell1000()
    log(f"Loaded {len(tickers)} tickers")

    price_data, failed = download_prices(tickers)
    rankings = calc_rankings(price_data, tickers)

    from zoneinfo import ZoneInfo
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))

    output = {
        "market": market,
        "universe": "Russell 1000",
        "universe_size": len(tickers),
        "last_updated": now_kst.isoformat(),
        "failed_tickers": failed[:50],
        "failed_count": len(failed),
        "data": rankings,
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
