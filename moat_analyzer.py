import json
import os
import sys
import time
from datetime import date

import anthropic
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(BASE_DIR, "cache", "stock_moat.json")
TTL_DAYS = 90
MODEL = "claude-haiku-4-5-20251001"
MAX_NEWS = 5

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    return _client


def _load_cache():
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_cache(cache):
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[moat] cache save failed: {e}")


def _strip_code(ticker):
    return ticker.replace(".KS", "").replace(".KQ", "")


def fetch_foreign_news(ticker, max_items=MAX_NEWS):
    try:
        news = yf.Ticker(ticker).news or []
    except Exception:
        return []
    results = []
    for n in news[:max_items]:
        content = n.get("content", {})
        provider = content.get("provider", {})
        results.append({
            "title": content.get("title", n.get("title", "")),
            "summary": content.get("summary", ""),
            "publisher": provider.get("displayName", n.get("publisher", "")),
            "date": str(content.get("pubDate", ""))[:10],
        })
    return results


def analyze_moat(ticker, name, business_overview, news_items):
    news_block = ""
    if news_items:
        for n in news_items:
            news_block += f"- [{n['publisher']}] {n['title']}\n"
            if n.get("summary"):
                news_block += f"  {n['summary'][:200]}\n"
    else:
        news_block = "해외 뉴스 없음"

    overview_block = business_overview if business_overview else "사업의 개요 정보 없음"

    prompt = (
        f"다음은 한국 상장사 {name}({ticker})의 정보입니다.\n\n"
        f"[DART 사업의 개요]\n{overview_block}\n\n"
        f"[최근 해외 뉴스]\n{news_block}\n\n"
        f"이 종목의 해자(경쟁우위)와 병목(약점/리스크)을 객관적으로 분석해주세요.\n"
        f"각각 1~2문장씩, 시장 시각에서 작성해주세요.\n"
        f"마크다운 문법(#, *, **, -, 등)이나 제목/헤더를 절대 포함하지 마. 평문 한국어로만 답변해.\n"
        f"형식:\n해자: ...\n병목: ..."
    )

    client = _get_client()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()

    moat = ""
    bottleneck = ""
    for line in raw.split("\n"):
        line = line.strip()
        if line.startswith("해자:"):
            moat = line[len("해자:"):].strip()
        elif line.startswith("병목:"):
            bottleneck = line[len("병목:"):].strip()

    if not moat and not bottleneck:
        parts = raw.split("\n", 1)
        moat = parts[0]
        bottleneck = parts[1].strip() if len(parts) > 1 else ""

    return {
        "moat": moat,
        "bottleneck": bottleneck,
        "raw_response": raw,
        "model": MODEL,
        "news_count": len(news_items),
    }


def get_or_analyze_moat(ticker, name=None):
    stock_code = _strip_code(ticker)

    cache = _load_cache()
    if stock_code in cache:
        entry = cache[stock_code]
        try:
            cached_at = date.fromisoformat(entry.get("last_updated", ""))
            if (date.today() - cached_at).days < TTL_DAYS:
                return entry
        except Exception:
            pass

    from earnings_tracker import get_corp_code, get_business_overview_cached

    corp_code = get_corp_code(stock_code)
    business_overview = None
    if corp_code:
        business_overview = get_business_overview_cached(stock_code, name or stock_code, corp_code)

    if not business_overview and not name:
        return None

    news_items = fetch_foreign_news(ticker, MAX_NEWS)

    result = analyze_moat(ticker, name or stock_code, business_overview, news_items)
    result["ticker"] = ticker
    result["name"] = name or stock_code
    result["last_updated"] = date.today().isoformat()
    result["has_data"] = True

    cache[stock_code] = result
    _save_cache(cache)
    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 moat_analyzer.py <ticker> | status")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "status":
        cache = _load_cache()
        print(f"캐시된 종목 수: {len(cache)}")
        for code, entry in list(cache.items())[:10]:
            print(f"  {code}: {entry.get('name', '?')} ({entry.get('last_updated', '?')})")
        sys.exit(0)

    ticker = cmd
    if not ticker.endswith(".KS") and not ticker.endswith(".KQ"):
        ticker = ticker + ".KS"

    stock_code = _strip_code(ticker)

    try:
        info = yf.Ticker(ticker).info
        name = info.get("longName") or info.get("shortName") or stock_code
    except Exception:
        name = stock_code

    print(f"=== {ticker} ({name}) ===")
    t0 = time.time()
    result = get_or_analyze_moat(ticker, name)
    elapsed = time.time() - t0

    if result:
        print(f"해자: {result['moat']}")
        print(f"병목: {result['bottleneck']}")
        print(f"뉴스 수: {result['news_count']}")
        print(f"분석일: {result['last_updated']}")
        print(f"소요시간: {elapsed:.1f}초")
    else:
        print("분석 불가 (DART 사업의 개요 없음)")
