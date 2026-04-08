from flask import Flask, jsonify, request
from flask_cors import CORS
import yfinance as yf
import requests
import feedparser
import anthropic
import json
import os

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# 티커 매핑
MAIN_TICKERS = {
    "sp500": "^GSPC",
    "nasdaq": "^IXIC",
    "vix": "^VIX",
    "usdkrw": "KRW=X",
    "wti": "CL=F",
    "gold": "GC=F",
}

EXTRA_TICKERS = {
    "us10y": "^TNX",
    "us2y": "^IRX",
    "dxy": "DX=F",
    "brent": "BZ=F",
    "bitcoin": "BTC-USD",
}

SECTOR_TICKERS = {
    "에너지": "XLE",
    "헬스케어": "XLV",
    "부동산": "XLRE",
    "필수소비재": "XLP",
    "금융": "XLF",
    "통신서비스": "XLC",
    "산업재": "XLI",
    "정보기술": "XLK",
}


def fetch_ticker_data(symbol):
    """티커 하나의 현재가와 등락률을 가져온다."""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="5d")
        if hist.empty or len(hist) < 2:
            return None
        current = hist["Close"].iloc[-1]
        previous = hist["Close"].iloc[-2]
        change = current - previous
        change_pct = (change / previous) * 100
        return {
            "price": round(float(current), 2),
            "change": round(float(change), 2),
            "change_pct": round(float(change_pct), 2),
        }
    except Exception:
        return None


@app.route("/api/market", methods=["GET"])
def get_market_data():
    """핵심 지표 6개 (S&P500, NASDAQ, VIX, USD/KRW, WTI, Gold)"""
    result = {}
    for key, symbol in MAIN_TICKERS.items():
        data = fetch_ticker_data(symbol)
        result[key] = data if data else {"price": 0, "change": 0, "change_pct": 0}
    return jsonify(result)


@app.route("/api/extra", methods=["GET"])
def get_extra_data():
    """채권/환율/원자재 추가 데이터"""
    result = {}
    for key, symbol in EXTRA_TICKERS.items():
        data = fetch_ticker_data(symbol)
        result[key] = data if data else {"price": 0, "change": 0, "change_pct": 0}
    return jsonify(result)


@app.route("/api/sectors", methods=["GET"])
def get_sector_data():
    """섹터별 등락률"""
    sectors = []
    for name, symbol in SECTOR_TICKERS.items():
        data = fetch_ticker_data(symbol)
        if data:
            sectors.append({"name": name, "value": data["change_pct"]})
        else:
            sectors.append({"name": name, "value": 0})
    sectors.sort(key=lambda x: x["value"], reverse=True)
    return jsonify(sectors)


@app.route("/api/fear-greed", methods=["GET"])
def get_fear_greed():
    """CNN Fear & Greed Index 실제 데이터"""
    try:
        res = requests.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://edition.cnn.com/",
                "Origin": "https://edition.cnn.com",
            },
            timeout=10,
        )
        data = res.json()
        score = round(data["fear_and_greed"]["score"])
        rating = data["fear_and_greed"]["rating"]
        return jsonify({"value": score, "rating": rating})
    except Exception:
        # 폴백: VIX 기반 간이 계산
        data = fetch_ticker_data("^VIX")
        if data:
            vix = data["price"]
            score = max(0, min(100, int(100 - ((vix - 10) / 30) * 100)))
            return jsonify({"value": score, "rating": "N/A"})
        return jsonify({"value": 50, "rating": "N/A"})


# 한국어 → 영어 키워드 매핑
KO_TO_EN = {
    "반도체": "semiconductor",
    "환율": "exchange rate USD KRW",
    "금리": "interest rate",
    "인플레이션": "inflation",
    "경기침체": "recession",
    "부동산": "real estate",
    "원유": "crude oil",
    "금": "gold",
    "은행": "banking",
    "주식": "stock market",
    "채권": "bond",
    "무역": "trade",
    "관세": "tariff",
    "전쟁": "war",
    "AI": "artificial intelligence",
    "인공지능": "artificial intelligence",
    "암호화폐": "cryptocurrency",
    "비트코인": "bitcoin",
    "테슬라": "Tesla",
    "애플": "Apple",
    "엔비디아": "Nvidia",
    "삼성": "Samsung",
    "중국": "China",
    "일본": "Japan",
    "유럽": "Europe",
}

TRUSTED_SOURCES = [
    "reuters", "bloomberg", "wsj", "wall street journal",
    "cnbc", "financial times", "ft.com", "barron",
    "marketwatch", "economist", "associated press", "ap news",
]

ANTHROPIC_API_KEY = "***REMOVED***"
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def fetch_rss_articles(search_terms):
    """구글 뉴스 RSS에서 신뢰 소스 기사를 수집한다."""
    articles = []
    seen_titles = set()

    for term in search_terms:
        try:
            url = ("https://news.google.com/rss/search?q="
                   + requests.utils.quote(term)
                   + "&hl=en-US&gl=US&ceid=US:en")
            feed = feedparser.parse(url)
            for entry in feed.entries:
                source = (entry.get("source", {}).get("title", "")
                          if hasattr(entry, "source") else "")
                source_lower = source.lower()
                if not any(t in source_lower for t in TRUSTED_SOURCES):
                    continue
                title = entry.get("title", "")
                if title in seen_titles:
                    continue
                seen_titles.add(title)
                articles.append({
                    "title": title,
                    "source": source,
                    "url": entry.get("link", ""),
                    "publishedAt": entry.get("published", ""),
                })
        except Exception:
            continue

    articles.sort(key=lambda a: a.get("publishedAt", ""), reverse=True)
    return articles[:20]


def rank_group_articles(group_name, articles, top_n=3):
    """Claude Haiku로 그룹 내 기사를 금융시장 영향력 기준으로 선별하고 3줄 요약한다."""
    if not articles:
        return []

    article_list = "\n".join(
        f"{i+1}. [{a['source']}] {a['title']}"
        for i, a in enumerate(articles)
    )

    response = claude_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": (
                f"주제: \"{group_name}\"\n\n"
                f"다음은 이 주제와 관련된 글로벌 금융 뉴스 목록이다.\n"
                f"금융시장(주식, 채권, 환율, 원자재)에 미치는 영향력이 큰 순서대로 "
                f"상위 {top_n}개를 선별해라. 중복되거나 사소한 뉴스는 제외해라.\n\n"
                "각 뉴스에 대해:\n"
                "1. 제목을 바탕으로 뉴스의 핵심 내용을 한국어 3줄로 요약해라.\n"
                "2. 각 줄은 '- '로 시작하는 완결된 문장이어야 한다.\n"
                "3. 금융시장에 미치는 영향을 반드시 포함해라.\n\n"
                f"{article_list}\n\n"
                "반드시 아래 JSON 배열 형식으로만 응답해라. 다른 텍스트는 절대 포함하지 마라.\n"
                '[{"rank":1,"index":원래번호,"summary":["요약1","요약2","요약3"]},...]\n'
                "index는 위 목록의 번호(1부터 시작)이다."
            ),
        }],
    )

    raw = response.content[0].text.strip()
    start = raw.find("[")
    end = raw.rfind("]") + 1
    if start == -1 or end == 0:
        return articles[:top_n]

    ranked = json.loads(raw[start:end])
    result = []
    for item in ranked[:top_n]:
        idx = item["index"] - 1
        if 0 <= idx < len(articles):
            a = articles[idx].copy()
            a["summary"] = item.get("summary", [])
            result.append(a)
    return result


@app.route("/api/news", methods=["POST"])
def get_news():
    """그룹별 구글 RSS + Claude Haiku 기반 금융 뉴스 선별"""
    body = request.get_json()
    if not body:
        return jsonify([])

    groups = body.get("groups", [])
    if not groups:
        return jsonify([])

    results = []
    for group in groups:
        name = group.get("name", "")
        terms = group.get("terms", [])
        if not terms:
            continue

        # 한국어 검색어를 영어로 변환
        translated = [KO_TO_EN.get(t, t) for t in terms]

        articles = fetch_rss_articles(translated)
        if not articles:
            results.append({"group": name, "articles": []})
            continue

        try:
            ranked = rank_group_articles(name, articles, top_n=3)
            results.append({"group": name, "articles": ranked})
        except Exception:
            results.append({"group": name, "articles": articles[:3]})

    return jsonify(results)


KEYWORDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keywords.json")

DEFAULT_GROUPS = [
    {"name": "미국 금리/Fed", "terms": ["Fed interest rate", "FOMC", "Federal Reserve policy"]},
    {"name": "반도체 산업", "terms": ["semiconductor", "Nvidia earnings", "chip export"]},
    {"name": "국제 유가", "terms": ["crude oil price", "OPEC production", "WTI Brent"]},
]


def load_keywords():
    try:
        with open(KEYWORDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        save_keywords(DEFAULT_GROUPS)
        return DEFAULT_GROUPS


def save_keywords(groups):
    with open(KEYWORDS_FILE, "w", encoding="utf-8") as f:
        json.dump(groups, f, ensure_ascii=False, indent=2)


@app.route("/api/keywords", methods=["GET"])
def get_keywords():
    return jsonify(load_keywords())


@app.route("/api/keywords", methods=["POST"])
def set_keywords():
    groups = request.get_json()
    if not isinstance(groups, list):
        return jsonify({"error": "groups must be a list"}), 400
    save_keywords(groups)
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=8000, debug=True)
