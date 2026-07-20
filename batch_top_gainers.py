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
from kis_api import get_market_cap_ranking, get_daily_price_history

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TICKERS_DIR = os.path.join(BASE_DIR, "tickers")
CACHE_DIR = os.path.join(BASE_DIR, "cache")

OUTLIER_THRESHOLDS = {
    "1d": 50.0,
    "1w": 100.0,
    "1mo": 200.0,
}

# 티커 유니버스가 이 개수 미만이면 파싱 실패로 간주 (빈/깨진 목록 캐시 방지)
MIN_UNIVERSE = {"us": 1000, "kr": 200, "jp": 100, "eu": 300}

_ISHARES_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/csv,text/plain,*/*",
}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _read_cached_tickers(path, key=None):
    """캐시 파일을 나이와 무관하게 읽는다. 없거나 비었으면 None."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if key is not None:
            data = data.get(key, [])
        if isinstance(data, list) and data:
            return data
    except Exception:
        pass
    return None


def _load_universe(path, market, fetch_fn, max_age_days, save_fn, key=None):
    """신선한 캐시 → 다운로드 → (실패 시) 만료된 캐시 순으로 티커 목록 확보.

    다운로드가 실패하거나 파싱 결과가 비정상적으로 적어도, 예전 캐시가 있으면
    그것으로 배치를 계속 진행한다. (기존에는 sys.exit로 배치 전체가 죽어서
    cache/top_gainers_*.json 이 영영 갱신되지 않았음)
    """
    min_count = MIN_UNIVERSE.get(market, 1)
    stale = None
    if os.path.exists(path):
        age_days = (time.time() - os.path.getmtime(path)) / 86400
        cached = _read_cached_tickers(path, key)
        if cached and len(cached) >= min_count:
            if age_days < max_age_days:
                log(f"Cached ticker list loaded: {len(cached)} tickers (age: {age_days:.1f}d)")
                return cached
            stale = cached
        elif cached:
            print(f"WARNING: cached ticker list too small ({len(cached)} < {min_count}), re-downloading",
                  file=sys.stderr)

    try:
        tickers = fetch_fn()
        if len(tickers) < min_count:
            raise RuntimeError(f"parsed only {len(tickers)} tickers (min {min_count})")
    except Exception as e:
        if stale:
            print(f"WARNING: ticker download failed ({e}); falling back to stale cache "
                  f"({len(stale)} tickers)", file=sys.stderr)
            return stale
        print(f"ERROR: ticker download failed and no usable cache: {e}", file=sys.stderr)
        sys.exit(1)

    save_fn(tickers)
    log(f"Saved {len(tickers)} tickers to {path}")
    return tickers


def _fetch_ishares_csv(url, header_tokens=("Ticker",)):
    resp = req.get(url, headers=_ISHARES_HEADERS, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"iShares returned HTTP {resp.status_code}; body: {resp.text[:200]!r}")

    lines = resp.text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if any(tok in line for tok in header_tokens):
            header_idx = i
            break
    if header_idx is None:
        raise RuntimeError(f"could not find CSV header row; first lines: {lines[:5]!r}")

    csv_text = "\n".join(lines[header_idx:])
    return pd.read_csv(io.StringIO(csv_text))


def _parse_russell_df(df):
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
    return tickers


def _save_plain_list(path):
    def _save(tickers):
        os.makedirs(TICKERS_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(tickers, f, ensure_ascii=False, indent=1)
    return _save


def load_russell1000():
    path = os.path.join(TICKERS_DIR, "us_russell1000.json")

    def fetch():
        log("Downloading Russell 1000 holdings from iShares...")
        url = (
            "https://www.ishares.com/us/products/239707/"
            "ishares-russell-1000-etf/1467271812596.ajax"
            "?fileType=csv&fileName=IWB_holdings&dataType=fund"
        )
        df = _fetch_ishares_csv(url, header_tokens=("Ticker,", '"Ticker"'))
        return _parse_russell_df(df)

    return _load_universe(path, "us", fetch, max_age_days=7, save_fn=_save_plain_list(path))


def load_russell3000():
    path = os.path.join(TICKERS_DIR, "us_russell3000.json")

    def fetch():
        log("Downloading Russell 3000 holdings from iShares...")
        url = (
            "https://www.ishares.com/us/products/239714/"
            "ishares-russell-3000-etf/1467271812596.ajax"
            "?fileType=csv&fileName=IWV_holdings&dataType=fund"
        )
        df = _fetch_ishares_csv(url, header_tokens=("Ticker,", '"Ticker"'))
        return _parse_russell_df(df)

    return _load_universe(path, "us", fetch, max_age_days=7, save_fn=_save_plain_list(path))


def load_kr_top600(top_n=300):
    path = os.path.join(TICKERS_DIR, "kr_top600.json")

    def fetch():
        log("Downloading KR top stocks from KIS Stock Master File...")
        tickers = []
        for market, suffix in [("KOSPI", ".KS"), ("KOSDAQ", ".KQ")]:
            ranking = get_market_cap_ranking(market, top_n)
            for s in ranking:
                tickers.append({
                    "ticker": s["code"] + suffix,
                    "code": s["code"],
                    "name": s["name"],
                    "market_sub": market,
                    "sector": "",
                })
            log(f"  {market}: {len(ranking)} stocks")
        return tickers

    def save(tickers):
        from zoneinfo import ZoneInfo
        now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
        os.makedirs(TICKERS_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "last_updated": now_kst.isoformat(),
                "source": "KIS Stock Master File",
                "universe": f"KOSPI top {top_n} + KOSDAQ top {top_n}",
                "tickers": tickers,
            }, f, ensure_ascii=False, indent=1)

    return _load_universe(path, "kr", fetch, max_age_days=7, save_fn=save, key="tickers")


def _pick_nikkei_table(tables):
    """컬럼명이 조금 바뀌어도 Code/Ticker 컬럼이 있는 표를 찾는다."""
    for df in tables:
        cols = {str(c).strip().lower(): c for c in df.columns}
        code_col = None
        for cand in ("code", "ticker", "symbol"):
            if cand in cols:
                code_col = cols[cand]
                break
        name_col = None
        for cand in ("company name", "company", "name"):
            if cand in cols:
                name_col = cols[cand]
                break
        if code_col is not None and name_col is not None and len(df) >= 100:
            sector_col = cols.get("sector")
            return df, code_col, name_col, sector_col
    raise RuntimeError(
        "could not find Nikkei 225 table; available columns: "
        + "; ".join(str(list(t.columns)) for t in tables[:3])
    )


def load_jp_nikkei225():
    path = os.path.join(TICKERS_DIR, "jp_nikkei225.json")

    def fetch():
        log("Downloading Nikkei 225 component list...")
        url = "https://topforeignstocks.com/indices/the-components-of-the-nikkei-225-index/"
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
        resp = req.get(url, headers=headers, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"topforeignstocks returned HTTP {resp.status_code}")

        tables = pd.read_html(io.StringIO(resp.text), flavor="lxml")
        if not tables:
            raise RuntimeError("no tables found in Nikkei 225 page")

        df, code_col, name_col, sector_col = _pick_nikkei_table(tables)
        tickers = []
        for _, row in df.iterrows():
            code = str(row.get(code_col, "")).strip()
            name = str(row.get(name_col, "")).strip()
            sector = str(row.get(sector_col, "")).strip() if sector_col else "-"
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
        return tickers

    def save(tickers):
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Asia/Tokyo"))
        os.makedirs(TICKERS_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "last_updated": now.isoformat(),
                "source": "topforeignstocks Nikkei 225",
                "universe": "Nikkei 225",
                "tickers": tickers,
            }, f, ensure_ascii=False, indent=1)

    return _load_universe(path, "jp", fetch, max_age_days=30, save_fn=save, key="tickers")


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

    def fetch():
        log("Downloading STOXX 600 holdings from iShares DE...")
        url = (
            "https://www.ishares.com/de/privatanleger/de/produkte/251931/"
            "ishares-stoxx-europe-600-ucits-etf-de-fund/1478358465952.ajax"
            "?fileType=csv&fileName=EXSA_holdings&dataType=fund"
        )
        df = _fetch_ishares_csv(url, header_tokens=("Emittententicker", "Ticker"))

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
        return tickers

    def save(tickers):
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

    return _load_universe(path, "eu", fetch, max_age_days=7, save_fn=save, key="tickers")


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


def _download_kr_via_kis(kr_ticker_list):
    got = {}
    missed = []
    total = len(kr_ticker_list)
    consecutive_fail = 0
    for i, ticker in enumerate(kr_ticker_list):
        try:
            series = get_daily_price_history(ticker, 90)
            if len(series) >= 2:
                got[ticker] = series
                consecutive_fail = 0
            else:
                missed.append(ticker)
        except Exception as e:
            missed.append(ticker)
            consecutive_fail += 1
            # 처음 몇 건은 원인 파악을 위해 에러를 그대로 출력
            if len(missed) <= 3:
                print(f"WARNING: KIS fetch failed for {ticker}: {e}", file=sys.stderr)
            # 연속 30건 실패면 토큰/키 문제 등 전체 장애로 보고 조기 중단
            if consecutive_fail >= 30:
                print(f"ERROR: {consecutive_fail} consecutive KIS failures — aborting KR download "
                      f"(last error: {e})", file=sys.stderr)
                missed.extend(kr_ticker_list[i + 1:])
                break
        if (i + 1) % 50 == 0 or i + 1 == total:
            log(f"KR KIS: {i + 1}/{total} done")
        time.sleep(0.05)
    return got, missed


def download_prices(ticker_list, chunk_size=50):
    symbols = [t["ticker"] for t in ticker_list]

    kr_symbols = [s for s in symbols if s.endswith(".KS") or s.endswith(".KQ")]
    other_symbols = [s for s in symbols if not (s.endswith(".KS") or s.endswith(".KQ"))]

    all_data = {}
    all_failed = []

    if kr_symbols:
        log(f"Downloading {len(kr_symbols)} KR tickers via KIS API...")
        kr_got, kr_missed = _download_kr_via_kis(kr_symbols)
        all_data.update(kr_got)
        all_failed.extend(kr_missed)

    if other_symbols:
        total_chunks = (len(other_symbols) + chunk_size - 1) // chunk_size

        retry_queue = []
        for ci in range(0, len(other_symbols), chunk_size):
            chunk = other_symbols[ci:ci + chunk_size]
            chunk_num = ci // chunk_size + 1
            log(f"Downloading chunk {chunk_num}/{total_chunks} ({len(chunk)} tickers)")
            got, missed = _download_chunk(chunk)
            all_data.update(got)
            retry_queue.extend(missed)
            if ci + chunk_size < len(other_symbols):
                time.sleep(2)

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

        all_failed.extend(final_failed)

    log(f"Downloaded {len(all_data)} tickers, {len(all_failed)} failed")
    return all_data, all_failed


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

    # 가격을 하나도 못 받아서 순위가 전부 비었으면, 멀쩡한 기존 캐시를
    # 빈 데이터로 덮어쓰지 않고 실패로 종료한다. (화면 '데이터 없음' 방지)
    if not any(rankings.get(p) for p in ("1d", "1w", "1mo")):
        print(f"ERROR: all rankings empty for {market} "
              f"({len(price_data)} priced / {len(failed)} failed) — keeping previous cache",
              file=sys.stderr)
        sys.exit(1)

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
