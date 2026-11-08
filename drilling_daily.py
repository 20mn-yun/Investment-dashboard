"""시추 업황 탭 (B): 일간 수집기 — 뉴스(Google News RSS·업계 RSS)·8-K(7.01/8.01)·Fleet Status Report(FSR) 에서
계약 이벤트와 시장 수치를 모으고, FSR 표로 커버리지·체결 단가 시계열을 만든다. 매일 06:00 launchd(com.yun.drilling-daily).

저장: cache/drilling/events.json {"events": [...]}, market_stats.json {"stats": [...]}, news_seen.json {해시: 날짜},
      fsr/<code>/index.json + <YYYY-MM-DD>.json(+.pdf), 요약 로그 logs/drilling_daily.log
AI: llm_client 'drilling_news'(기사·8-K 1건, 한도 40/일) · 'drilling_fsr'(FSR PDF 1건, 한도 6/일). 모든 단계는 독립적으로 실패해도 다음 단계로.
실행: python3 drilling_daily.py --once | --news-only | --fsr-only [--max-ai N]
"""
import argparse
import hashlib
import json
import os
import re
import statistics
import time
import urllib.parse
from datetime import date, datetime, timedelta

import requests

import ai_budget
import drilling_tracker as DT

_BD = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = DT.CACHE_DIR
EVENTS_PATH = os.path.join(CACHE_DIR, "events.json")
STATS_PATH = os.path.join(CACHE_DIR, "market_stats.json")
SEEN_PATH = os.path.join(CACHE_DIR, "news_seen.json")
FSR_DIR = os.path.join(CACHE_DIR, "fsr")
LOG_PATH = os.path.join(_BD, "logs", "drilling_daily.log")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
SEEN_KEEP_DAYS = 30
ARTICLE_MAX_CHARS = 8000
FSR_MAX_CHARS = 60000
DAYRATE_CHANGE_MIN_K = 5          # FSR 비교에서 단가 변경 이벤트로 잡는 최소 변화폭 ($K/일)
EVENT_TYPES = ("award", "extension", "option", "termination", "suspension", "reactivation", "stacking", "retirement", "newbuild", "tender", "ma",
               "guidance", "dayrate_change", "other")
_MONTHS = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"), 1)}
_MON3 = {k[:3]: v for k, v in _MONTHS.items()}

NEWS_SYSTEM = """You extract offshore-drilling facts from ONE news article or SEC 8-K exhibit (title, source, date, body).
Scope: offshore FLOATING rigs only — drillships and semisubmersibles (incl. harsh-environment semis). Jackups, land rigs, OSVs, FPSOs, wind vessels are out of scope.
If the text has nothing about floating-rig contracts, fleet moves, dayrates, utilization or rig counts, set "relevant": false and return empty arrays.
Rules:
- Use only the text. Copy numbers as written (no conversion, no estimates). Missing -> null. Every event and stat needs "quote": one verbatim sentence that supports it.
- One article may hold several contracts: one event per rig/contract.
- type: award (new contract) | extension | option (option exercised/priced) | termination | suspension | reactivation | stacking (cold/warm stack) | retirement (scrap/recycle) | newbuild | tender (tender/bid/FID news) | ma (M&A) | guidance | dayrate_change | other
- rig_type: drillship | harsh (harsh-environment semi) | benign_semi | jackup | unknown. region: us_gulf | brazil | west_africa | mediterranean | asia_pacific | india | norway_harsh | other.
- dayrate_usd_k: dayrate in USD thousands per day (e.g. "$450,000 per day" -> 450). tcv_usd_m: total contract value in USD millions. duration_days: contract length in days if stated (convert months only when the text says months: months*30).
- market_stats: ONLY industry/market-wide figures (e.g. "Westwood: 108 contracted drillships", "marketed utilization 92%", "leading-edge dayrates $500k"). metric: contracted_rigs | utilization | dayrate | duration | fleet_count | retirements. segment: drillship | harsh | floater_total | jackup. as_of: the date the figure refers to if stated (YYYY-MM-DD or YYYY-MM), else null.
Output JSON only:
{"relevant": true|false,
 "events": [{"type": "", "company": "", "rig": "", "rig_type": "", "region": "", "customer": "", "start": "", "duration_days": n|null, "dayrate_usd_k": n|null, "tcv_usd_m": n|null, "quote": "", "headline": ""}],
 "market_stats": [{"source_name": "", "metric": "", "segment": "", "region": "", "value": n, "unit": "", "as_of": null, "quote": ""}]}"""

FSR_SYSTEM = """You convert the text of an offshore driller's Fleet Status Report (FSR) PDF into JSON. The text includes table rows joined with tabs.
Rules:
- Only what the report states. No estimates. Missing values -> null. Dates as "YYYY-MM" (month precision is enough). Dayrates in USD thousands per day (e.g. "$475,000" -> 475; "mid-$400s" -> 450).
- One row per rig per contract segment (firm term, option, priced option, available/idle period, stacked). A rig with three contract legs -> three rows.
- status: firm | option | priced_option | available | stacked | retired | shipyard. rig_type: drillship | harsh | benign_semi | jackup. region: us_gulf | brazil | west_africa | mediterranean | asia_pacific | india | norway_harsh | other.
- avg_dayrate_table: if the report has a table of average contracted dayrates by segment and future quarter, list every cell.
- fleet_count: number of rigs in the report (marketed + stacked), backlog_usd_b: contract backlog in USD billions if stated.
Output JSON only:
{"report_date": "YYYY-MM-DD", "fleet_count": n|null, "backlog_usd_b": n|null,
 "avg_dayrate_table": [{"segment": "", "period": "Q4 2026", "value_usd_k": n}],
 "rigs": [{"rig": "", "rig_type": "", "region": "", "customer": "", "status": "", "start": "YYYY-MM"|null, "end": "YYYY-MM"|null, "dayrate_usd_k": n|null, "note": ""}]}"""


# ---------- 공통 ----------
def _load(path, default):
    return DT._load(path, default)


def _save(path, data):
    DT._save(path, data)


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _sha(*parts):
    return hashlib.sha1("|".join(str(p or "") for p in parts).encode("utf-8")).hexdigest()[:16]


def _log(line):
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{_now()} {line}\n")
    except OSError:
        pass


def _get(url, timeout=20, **kw):
    return requests.get(url, headers={"User-Agent": UA, "Accept": "*/*"}, timeout=timeout, allow_redirects=True, **kw)


def _num(v):
    try:
        return None if v is None or isinstance(v, bool) else float(v)
    except (TypeError, ValueError):
        return None


def _events():
    return _load(EVENTS_PATH, {"events": []})


def _stats():
    return _load(STATS_PATH, {"stats": []})


def _seen():
    d = _load(SEEN_PATH, {})
    cutoff = (date.today() - timedelta(days=SEEN_KEEP_DAYS)).isoformat()
    return {k: v for k, v in d.items() if (v or "9999")[:10] >= cutoff}


def _add_events(store, new_events):
    ids = {e["id"] for e in store["events"]}
    n = 0
    for e in new_events:
        if e["id"] not in ids:
            store["events"].append(e)
            ids.add(e["id"])
            n += 1
    store["events"].sort(key=lambda e: (e.get("date") or "", e.get("id")))
    return n


def _add_stats(store, new_stats):
    ids = {s["id"] for s in store["stats"]}
    n = 0
    for s in new_stats:
        if s["id"] not in ids:
            store["stats"].append(s)
            ids.add(s["id"])
            n += 1
    store["stats"].sort(key=lambda s: (s.get("as_of") or s.get("date") or "", s.get("id")))
    return n


def _company_code(name):
    """발주 시추사명 → 추적 3사 코드 (없으면 None)"""
    t = (name or "").lower()
    if "transocean" in t:
        return "RIG"
    if "seadrill" in t:
        return "SDRL"
    if "noble" in t:
        return "NE"
    return None


def _norm_events(raw_events, src, src_name, url, day, headline):
    out = []
    for e in raw_events or []:
        if not isinstance(e, dict) or not e.get("quote"):
            continue
        typ = e.get("type") if e.get("type") in EVENT_TYPES else "other"
        rig = (e.get("rig") or "").strip()
        comp = (e.get("company") or "").strip()
        out.append({"id": _sha(url, rig, typ, day), "date": day, "source": src, "source_name": src_name, "url": url, "type": typ,
                    "company": comp, "code": _company_code(comp), "rig": rig,
                    "rig_type": e.get("rig_type") if e.get("rig_type") in ("drillship", "harsh", "benign_semi", "jackup") else "unknown",
                    "region": DT._norm_region(e.get("region")), "customer": e.get("customer") or None, "start": e.get("start") or None,
                    "duration_days": _num(e.get("duration_days")), "dayrate_usd_k": _num(e.get("dayrate_usd_k")), "tcv_usd_m": _num(e.get("tcv_usd_m")),
                    "quote": str(e["quote"])[:400], "headline": (e.get("headline") or headline or "")[:200]})
    return out


def _norm_stats(raw_stats, src_name_default, url, day):
    out = []
    for s in raw_stats or []:
        if not isinstance(s, dict) or not s.get("quote") or _num(s.get("value")) is None:
            continue
        metric = s.get("metric") if s.get("metric") in ("contracted_rigs", "utilization", "dayrate", "duration", "fleet_count", "retirements") else None
        if not metric:
            continue
        as_of = str(s.get("as_of") or "")[:10] or day
        if len(as_of) == 7:
            as_of += "-01"
        seg = s.get("segment") if s.get("segment") in ("drillship", "harsh", "floater_total", "jackup") else "floater_total"
        out.append({"id": _sha(url, metric, seg, as_of, s.get("value")), "date": day, "as_of": as_of, "source_name": (s.get("source_name") or src_name_default or "")[:60],
                    "url": url, "metric": metric, "segment": seg, "region": DT._norm_region(s.get("region")), "value": _num(s.get("value")),
                    "unit": s.get("unit") or "", "quote": str(s["quote"])[:400]})
    return out


def _extract_article(title, source_name, day, body, url, src="news", max_ai=None):
    """기사 1건 → (events, stats, status). AI 1회 (drilling_news)"""
    import llm_client
    allowed, n = ai_budget.take("drilling_news", limit=max_ai)
    if not allowed:
        return [], [], "ai_limit"
    user = f"Title: {title}\nSource: {source_name}\nDate: {day}\nURL: {url}\n\n{body[:ARTICLE_MAX_CHARS]}"
    r = llm_client.complete("drilling_news", user, system=NEWS_SYSTEM, json_mode=True, max_tokens=1500)
    if not r.ok or not isinstance(r.data, dict):
        return [], [], f"ai_error:{(r.error or '')[:40]}"
    d = r.data
    if not d.get("relevant"):
        return [], [], "irrelevant"
    return _norm_events(d.get("events"), src, source_name, url, day, title), _norm_stats(d.get("market_stats"), source_name, url, day), "ok"


# ---------- 1) 뉴스 ----------
def _annotate_tables(txt):
    """html2text 표(| 구분)의 각 데이터 행 앞에 머리글을 '열=값' 형식으로 붙인다 (AI 인용이 'Drillship| 23| …' 꼴이 되지 않게)."""
    out, header = [], None
    for line in txt.split("\n"):
        if line.strip().startswith("|") and line.count("|") >= 2:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c or "-") for c in cells):
                continue                                   # 구분선
            if header is None or (cells and not any(re.search(r"\d", c) for c in cells)):
                header = cells
                out.append(line)
                continue
            pairs = [f"{h}={v}" for h, v in zip(header, cells) if v]
            out.append(("[row] " + ", ".join(pairs)) if pairs else line)
        else:
            if line.strip() == "":
                header = None
            out.append(line)
    return "\n".join(out)


def _article_body(url, annotate_tables=False):
    """본문 받기 (10초, html2text, 8,000자). annotate_tables 면 표 행에 머리글을 붙임. 실패하면 ''"""
    try:
        res = _get(url, timeout=10)
        if res.status_code != 200 or "html" not in (res.headers.get("content-type") or ""):
            return ""
        import html2text
        h = html2text.HTML2Text()
        h.ignore_links = True
        h.ignore_images = True
        h.body_width = 0
        txt = h.handle(res.text)
        txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
        if annotate_tables:
            txt = _annotate_tables(txt)
        return txt[:ARTICLE_MAX_CHARS]
    except Exception:
        return ""


def _feed_items(url, source_hint=None):
    import feedparser
    from email.utils import parsedate_to_datetime
    out = []
    try:
        feed = feedparser.parse(url, request_headers={"User-Agent": UA})
    except Exception:
        return out
    for e in getattr(feed, "entries", []) or []:
        pub = None
        for k in ("published", "updated"):
            if e.get(k):
                try:
                    pub = parsedate_to_datetime(e[k]).date().isoformat()
                    break
                except Exception:
                    try:
                        pub = str(e[k])[:10]
                        break
                    except Exception:
                        pass
        src = (e.get("source") or {}).get("title") if isinstance(e.get("source"), dict) else None
        out.append({"title": e.get("title") or "", "link": e.get("link") or "", "published": pub, "summary": re.sub(r"<[^>]+>", " ", e.get("summary") or "")[:1500],
                    "source": src or source_hint or urllib.parse.urlparse(url).netloc})
    return out


def _page_links(pcfg):
    """업계 리서치 목록 페이지 → pattern 에 맞는 기사 링크 (최근순: URL 에 날짜가 있으면 날짜 내림차순, 없으면 문서 순서)"""
    res = _get(pcfg["url"], timeout=30)
    rx = re.compile(pcfg["link_pattern"], re.I)
    seen, out = set(), []
    for m in re.finditer(r'href=["\']([^"\']+)["\']', res.text, re.I):
        href = urllib.parse.urljoin(pcfg["url"], m.group(1).replace("&#038;", "&"))
        href = href.split("#")[0]
        if href in seen or not rx.search(href) or href.rstrip("/") == pcfg["url"].rstrip("/"):
            continue
        seen.add(href)
        ctx = re.sub(r"<[^>]+>", " ", res.text[m.end():m.end() + 300])
        out.append({"link": href, "title": re.sub(r"\s+", " ", ctx).strip()[:120], "date": None})
    for o in out:
        d = re.search(r"/(20\d\d)/(\d\d)/(\d\d)/", o["link"])
        o["date"] = f"{d.group(1)}-{d.group(2)}-{d.group(3)}" if d else None
    if any(o["date"] for o in out):
        out.sort(key=lambda o: o["date"] or "", reverse=True)
    return out


def scan_pages(max_ai=None, seen=None, ev_store=None, st_store=None):
    """config.news.pages: 업계 리서치 목록 페이지(Westwood·Esgian)에서 새 글을 최근순 5개(첫 실행은 3개)까지 본문 받아 NEWS_SYSTEM 으로 처리"""
    cfg = DT.load_config().get("news") or {}
    own = seen is None
    seen = _seen() if seen is None else seen
    ev_store = _events() if ev_store is None else ev_store
    st_store = _stats() if st_store is None else st_store
    res = {"pages": {}, "fetched": 0, "ai": 0, "relevant": 0, "events": 0, "stats": 0, "errors": 0, "ai_limit": False}
    first_run = not any(k.startswith("page:") for k in seen)
    per_page = 3 if first_run else 5
    budget_used = 0
    for pcfg in cfg.get("pages") or []:
        name = pcfg.get("name") or pcfg.get("url")
        try:
            links = _page_links(pcfg)
        except Exception as e:
            res["pages"][name] = {"error": f"{type(e).__name__}"}
            continue
        new = [l for l in links if f"page:{_sha(l['link'])}" not in seen][:per_page]
        pr = {"links": len(links), "new": len(new), "ai": 0, "relevant": 0}
        for l in new:
            if max_ai is not None and budget_used >= max_ai:
                break
            body = _article_body(l["link"], annotate_tables=True)
            if not body:
                seen[f"page:{_sha(l['link'])}"] = date.today().isoformat()
                res["errors"] += 1
                continue
            res["fetched"] += 1
            title = l["title"] or l["link"].rsplit("/", 1)[-1].replace("-", " ")
            day = l["date"] or date.today().isoformat()
            try:
                evs, sts, status = _extract_article(title, name, day, body, l["link"], "news", None)
            except Exception as e:
                status, evs, sts = f"error:{type(e).__name__}", [], []
            if status == "ai_limit":
                res["ai_limit"] = True
                break
            budget_used += 1
            pr["ai"] += 1
            res["ai"] += 1
            if status.startswith("ai_error") or status.startswith("error"):
                res["errors"] += 1
                continue
            seen[f"page:{_sha(l['link'])}"] = day
            if status == "ok":
                pr["relevant"] += 1
                res["relevant"] += 1
                res["events"] += _add_events(ev_store, evs)
                res["stats"] += _add_stats(st_store, sts)
            time.sleep(1)
        res["pages"][name] = pr
        if res["ai_limit"]:
            break
    if own:
        _save(EVENTS_PATH, ev_store)
        _save(STATS_PATH, st_store)
        _save(SEEN_PATH, seen)
    return res


def scan_news(max_ai=None):
    cfg = DT.load_config().get("news") or {}
    lookback = int(cfg.get("lookback_days") or 3)
    since = (date.today() - timedelta(days=lookback)).isoformat()
    pre = re.compile(cfg.get("prefilter") or ".", re.I)
    max_ai = max_ai if max_ai is not None else int(cfg.get("max_ai_per_run") or 40)
    items = []
    for q in cfg.get("google_queries") or []:
        url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(q) + "&hl=en-US&gl=US&ceid=US:en"
        items += _feed_items(url)
    n_google = len(items)
    for u in cfg.get("rss") or []:
        items += _feed_items(u)
    n_rss = len(items) - n_google
    seen = _seen()
    res = {"google": n_google, "rss": n_rss, "in_window": 0, "prefilter": 0, "ai": 0, "relevant": 0, "events": 0, "stats": 0, "ai_limit": False, "errors": 0}
    ev_store, st_store = _events(), _stats()
    todo, keys = [], set()
    for it in items:
        if not it["link"] or (it["published"] and it["published"] < since):
            continue
        res["in_window"] += 1
        key = _sha(it["link"])
        if key in seen or key in keys:
            continue
        if not pre.search(f"{it['title']} {it['summary']}"):
            seen[key] = date.today().isoformat()          # 걸러진 것도 다시 보지 않음
            continue
        keys.add(key)
        todo.append((key, it))
    res["prefilter"] = len(todo)
    for key, it in todo:
        if res["ai"] >= max_ai:
            break
        body = _article_body(it["link"]) or it["summary"]
        day = it["published"] or date.today().isoformat()
        try:
            evs, sts, status = _extract_article(it["title"], it["source"], day, body, it["link"], "news", None)
        except Exception as e:
            status, evs, sts = f"error:{type(e).__name__}", [], []
        if status == "ai_limit":
            res["ai_limit"] = True
            break
        res["ai"] += 1
        if status.startswith("ai_error") or status.startswith("error"):
            res["errors"] += 1
            continue
        seen[key] = day
        if status == "ok":
            res["relevant"] += 1
            res["events"] += _add_events(ev_store, evs)
            res["stats"] += _add_stats(st_store, sts)
        time.sleep(1)
    # 업계 리서치 페이지 (남은 AI 상한 안에서)
    left = None if max_ai is None else max(0, max_ai - res["ai"])
    if left is None or left > 0:
        res["pages"] = scan_pages(left, seen, ev_store, st_store)
        res["ai"] += res["pages"].get("ai", 0)
        res["relevant"] += res["pages"].get("relevant", 0)
        res["events"] += res["pages"].get("events", 0)
        res["stats"] += res["pages"].get("stats", 0)
    _save(EVENTS_PATH, ev_store)
    _save(STATS_PATH, st_store)
    _save(SEEN_PATH, seen)
    return res


# ---------- 2) 8-K 7.01 / 8.01 ----------
def scan_8k(max_ai=None):
    import sec_filings
    import us_financials
    cfg = DT.load_config()
    items = tuple(cfg.get("eight_k_items") or ("7.01", "8.01"))
    lookback = int((cfg.get("news") or {}).get("lookback_days") or 3) + 2
    seen = _seen()
    ev_store, st_store = _events(), _stats()
    res = {"filings": 0, "ai": 0, "events": 0, "stats": 0, "errors": [], "by_code": {}}
    for code in cfg["tickers"]:
        try:
            cik10, _ = us_financials.resolve_cik(code)
            if not cik10:
                res["errors"].append(f"{code}:no_cik")
                continue
            entries = [e for e in sec_filings.list_earnings_8k(code, cik10, since_days=lookback, items=items) if "2.02" not in (e.get("items") or "")]
        except Exception as e:
            res["errors"].append(f"{code}:{type(e).__name__}")
            continue
        n_code = 0
        done_acc = set()
        for ent in entries:
            if ent["accession"] in done_acc or ent["exhibit"] != "EX-99.1":
                continue
            done_acc.add(ent["accession"])
            key = _sha("8k", ent["accession"])
            if key in seen:
                continue
            res["filings"] += 1
            n_code += 1
            try:
                _, _, text = sec_filings.fetch_exhibit(code, ent)
                evs, sts, status = _extract_article(f"{code} 8-K {ent.get('description') or ent['exhibit']}", f"{cfg['names'].get(code, code)} 8-K", ent["date"], text, ent["url"], "8-K", max_ai)
            except Exception as e:
                res["errors"].append(f"{code}:{ent['accession']}:{type(e).__name__}")
                continue
            if status == "ai_limit":
                res["errors"].append("ai_limit")
                break
            res["ai"] += 1
            if status == "ok":
                res["events"] += _add_events(ev_store, evs)
                res["stats"] += _add_stats(st_store, sts)
            if not status.startswith("ai_error"):
                seen[key] = ent["date"]
        res["by_code"][code] = n_code
    _save(EVENTS_PATH, ev_store)
    _save(STATS_PATH, st_store)
    _save(SEEN_PATH, seen)
    return res


# ---------- 3) Fleet Status Report ----------
def _fsr_dir(code):
    return os.path.join(FSR_DIR, code)


def _fsr_index(code):
    return _load(os.path.join(_fsr_dir(code), "index.json"), {"items": []})


def _fsr_links(code, fcfg):
    """목록 페이지에서 pattern 에 맞는 PDF 링크 → [{url, hint}] (hint = 링크 앞 문맥 텍스트, 날짜 추출용). 상대경로 보정·공백 %20"""
    res = _get(fcfg["page"], timeout=30)
    html = res.text
    rx = re.compile(fcfg["pattern"], re.I)
    out, seen = [], set()
    for m in re.finditer(r'href=["\']([^"\']+)["\']', res.text, re.I):
        href = m.group(1)
        if not rx.search(href):
            continue
        href = re.sub(r"[&?]refresh=[^&\"']*", "", href).replace("&#038;", "&").replace("&amp;", "&")
        url = urllib.parse.urljoin(fcfg["page"], href.replace(" ", "%20"))
        if url in seen:
            continue
        seen.add(url)
        ctx = re.sub(r"<[^>]+>", " ", html[max(0, m.start() - 600):m.start()])
        out.append({"url": url, "hint": re.sub(r"\s+", " ", ctx)[-200:]})
    return out


def _date_from_text(*texts):
    """URL·파일명·문맥에서 날짜: 'November 2026', 'October 26, 2026', '26-Oct-26', 'FSR-01-26-26', '2026-11'"""
    for t in texts:
        t = urllib.parse.unquote(str(t or ""))
        m = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s*(20\d\d)", t, re.I)
        if m:
            return date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2))).isoformat()
        m = re.search(r"(\d{1,2})-(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-(\d{2})", t, re.I)
        if m:
            return date(2000 + int(m.group(3)), _MON3[m.group(2).lower()], int(m.group(1))).isoformat()
        m = re.search(r"FSR-(\d{2})-(\d{2})-(\d{2})", t, re.I)
        if m:
            return date(2000 + int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
        m = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)[-\s]+(20\d\d)", t, re.I)
        if m:
            return date(int(m.group(2)), _MONTHS[m.group(1).lower()], 1).isoformat()
        m = re.search(r"(20\d\d)[-/](\d{2})(?:[-/](\d{2}))?", t)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3) or 1)).isoformat()
    return None


def _pdf_text(path):
    """pdfplumber 전체 텍스트 + 표(탭 구분). 합쳐서 FSR_MAX_CHARS"""
    import pdfplumber
    parts = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages):
            parts.append(f"=== page {i + 1} ===")
            parts.append(page.extract_text() or "")
            try:
                for tb in page.extract_tables() or []:
                    parts.append("[table]")
                    for row in tb:
                        parts.append("\t".join((c or "").replace("\n", " ").strip() for c in row))
            except Exception:
                pass
    text = "\n".join(parts)
    return text[:FSR_MAX_CHARS]


def _fsr_drive_copy(code, name, pdf_path, day):
    """Drive Analysis/<종목폴더>/IR/<종목명>_FSR_<date>.pdf (이미 있으면 건너뜀)"""
    try:
        import telegram_report
        import shutil
        base = telegram_report.report_download_base()
        if not base:
            return None
        kw = telegram_report.stock_folder_keyword("US", code, name)
        d = os.path.join(telegram_report.stock_folder(base, kw), "IR")
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, f"{telegram_report._safe_name(telegram_report.us_short_name(code, name), 30)}_FSR_{day}.pdf")
        if not os.path.exists(dst):
            shutil.copy2(pdf_path, dst)
        return dst
    except Exception as e:
        return f"error:{type(e).__name__}"


def _process_fsr(code, name, link, max_ai=None):
    """PDF 받기 → 텍스트 → AI → <date>.json 저장. 반환 {status, date, path}"""
    import llm_client
    d = _fsr_dir(code)
    os.makedirs(d, exist_ok=True)
    res = _get(link["url"], timeout=60)
    if res.status_code != 200 or not res.content[:5].startswith(b"%PDF"):
        return {"status": f"download_failed:{res.status_code}"}
    cd = res.headers.get("content-disposition") or ""
    fname = re.search(r'filename="?([^";]+)', cd)
    guess = _date_from_text(link["url"], fname.group(1) if fname else "", link.get("hint")) or date.today().isoformat()
    pdf_path = os.path.join(d, f"{guess}.pdf")
    with open(pdf_path, "wb") as f:
        f.write(res.content)
    text = _pdf_text(pdf_path)
    if len(text) < 500:
        return {"status": "no_text", "date": guess}
    allowed, n = ai_budget.take("drilling_fsr", limit=max_ai)
    if not allowed:
        return {"status": "ai_limit", "date": guess}
    user = f"Company: {name} ({code})\nFile: {fname.group(1) if fname else link['url']}\nDate hint: {guess}\n\n{text}"
    t0 = time.time()
    r = llm_client.complete("drilling_fsr", user, system=FSR_SYSTEM, json_mode=True, max_tokens=8000)
    if not r.ok or not isinstance(r.data, dict):
        return {"status": f"ai_error:{(r.error or '')[:40]}", "date": guess}
    j = r.data
    rep_date = str(j.get("report_date") or "")[:10]
    try:
        date.fromisoformat(rep_date)
    except ValueError:
        rep_date = guess
    rows = []
    for x in j.get("rigs") or []:
        if not isinstance(x, dict) or not x.get("rig"):
            continue
        rows.append({"rig": str(x["rig"]).strip(), "rig_type": x.get("rig_type") if x.get("rig_type") in ("drillship", "harsh", "benign_semi", "jackup") else "unknown",
                     "region": DT._norm_region(x.get("region")), "customer": (x.get("customer") or "").strip() or None,
                     "status": x.get("status") if x.get("status") in ("firm", "option", "priced_option", "available", "stacked", "retired", "shipyard") else "firm",
                     "start": (str(x.get("start"))[:7] if x.get("start") else None), "end": (str(x.get("end"))[:7] if x.get("end") else None),
                     "dayrate_usd_k": _num(x.get("dayrate_usd_k")), "note": (x.get("note") or "")[:120]})
    out = {"status": "ok", "code": code, "report_date": rep_date, "url": link["url"], "pdf": os.path.relpath(pdf_path, _BD),
           "fleet_count": _num(j.get("fleet_count")), "backlog_usd_b": _num(j.get("backlog_usd_b")),
           "avg_dayrate_table": [{"segment": t.get("segment"), "period": t.get("period"), "value_usd_k": _num(t.get("value_usd_k"))} for t in (j.get("avg_dayrate_table") or []) if isinstance(t, dict)],
           "rigs": rows, "text_chars": len(text), "model": r.model, "tokens": {"input": r.input_tokens, "output": r.output_tokens}, "seconds": round(time.time() - t0, 1), "at": _now()}
    if rep_date != guess:
        new_pdf = os.path.join(d, f"{rep_date}.pdf")
        if not os.path.exists(new_pdf):
            os.replace(pdf_path, new_pdf)
            out["pdf"] = os.path.relpath(new_pdf, _BD)
            pdf_path = new_pdf
    _save(os.path.join(d, f"{rep_date}.json"), out)
    out["drive"] = _fsr_drive_copy(code, name, pdf_path, rep_date)
    return {"status": "ok", "date": rep_date, "rows": len(rows), "drive": out["drive"]}


def process_fsr_url(code, url, max_ai=None):
    """특정 FSR PDF 1부를 index 와 무관하게 처리하고 index 에 기록 (F). 반환 _process_fsr 결과"""
    cfg = DT.load_config()
    name = cfg["names"].get(code, code)
    idx = _fsr_index(code)
    out = _process_fsr(code, name, {"url": url, "hint": url}, max_ai)
    if out.get("status") == "ok":
        idx["items"] = [it for it in idx["items"] if it.get("url") != url] + [{"url": url, "date": out["date"], "status": "ok", "at": _now(), "forced": True}]
        idx["items"].sort(key=lambda x: x.get("date") or "")
        _save(os.path.join(_fsr_dir(code), "index.json"), idx)
    return out


def check_fsr(max_ai=None, force_url=None):
    """force_url = (code, url) 이면 그 1부만 처리"""
    cfg = DT.load_config()
    res = {}
    if force_url:
        code, url = force_url
        res[code] = process_fsr_url(code, url, max_ai)
        return res
    for code, fcfg in (cfg.get("fsr") or {}).items():
        name = cfg["names"].get(code, code)
        idx = _fsr_index(code)
        known = {it["url"] for it in idx["items"]}
        r = {"found": 0, "new": 0, "processed": 0, "statuses": [], "error": None}
        try:
            links = _fsr_links(code, fcfg)
        except Exception as e:
            r["error"] = f"{type(e).__name__}: {str(e)[:80]}"
            res[code] = r
            continue
        r["found"] = len(links)
        new = [l for l in links if l["url"] not in known]
        # 날짜 추정으로 최신순 정렬. 첫 실행(index 비어 있음)은 최신 2개까지만 (diff 기준선)
        new.sort(key=lambda l: _date_from_text(l["url"], l.get("hint")) or "", reverse=True)
        if not idx["items"]:
            new = new[:2]
        r["new"] = len(new)
        for l in new:
            try:
                out = _process_fsr(code, name, l, max_ai)
            except Exception as e:
                out = {"status": f"error:{type(e).__name__}:{str(e)[:60]}"}
            r["statuses"].append(f"{out.get('date') or '?'}:{out['status']}")
            if out["status"] == "ok":
                idx["items"].append({"url": l["url"], "date": out["date"], "status": "ok", "at": _now()})
                r["processed"] += 1
            elif out["status"] == "ai_limit":
                break
            else:
                idx["items"].append({"url": l["url"], "date": out.get("date"), "status": out["status"], "at": _now()})
        idx["items"].sort(key=lambda x: x.get("date") or "")
        _save(os.path.join(_fsr_dir(code), "index.json"), idx)
        res[code] = r
    return res


def fsr_reports(code):
    """저장된 FSR json 목록 (report_date 오름차순)"""
    d = _fsr_dir(code)
    out = []
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if re.match(r"^\d{4}-\d{2}-\d{2}\.json$", fn):
                j = _load(os.path.join(d, fn), None)
                if j and j.get("status") == "ok":
                    out.append(j)
    out.sort(key=lambda j: j["report_date"])
    return out


def latest_fsr(code):
    r = fsr_reports(code)
    return r[-1] if r else None


def diff_fsr(code):
    """최신 2개 FSR 비교 → 이벤트(source fsr_diff). 새 리그-고객 = award, end 뒤로 = extension, dayrate 변경 = dayrate_change,
    stacked→firm = reactivation, firm→stacked = stacking, 행 사라짐+retired = retirement"""
    reps = fsr_reports(code)
    if len(reps) < 2:
        return {"events": 0, "note": "FSR 2개 미만"}
    name = DT.load_config()["names"].get(code, code)
    total, compared = 0, []
    for old, new in zip(reps[:-1], reps[1:]):   # 연속한 보고서 쌍마다 비교 (id = 새 보고서 url+rig+type+날짜 → 중복 생성 없음)
        r = _diff_pair(code, name, old, new)
        total += r["events"]
        compared.append([old["report_date"], new["report_date"], r["events"]])
    store = _events()
    before = len(store["events"])
    store["events"] = [e for e in store["events"] if not (e.get("type") == "dayrate_change" and e.get("prev_dayrate_usd_k") is not None and e.get("dayrate_usd_k") is not None
                                                           and abs(e["dayrate_usd_k"] - e["prev_dayrate_usd_k"]) <= DAYRATE_CHANGE_MIN_K)]
    removed = before - len(store["events"])
    if removed:
        _save(EVENTS_PATH, store)
    return {"events": total, "removed_small_dayrate_changes": removed, "compared": compared}


def _diff_pair(code, name, old, new):
    day, url = new["report_date"], new["url"]

    def by_pair(rep):
        out = {}
        for r in rep["rigs"]:
            if r["status"] in ("firm", "option", "priced_option"):
                k = (r["rig"].lower(), (r.get("customer") or "").lower())
                cur = out.get(k)
                if not cur or (r.get("end") or "") > (cur.get("end") or ""):
                    out[k] = r
        return out

    def rig_status(rep):
        st = {}
        for r in rep["rigs"]:
            st.setdefault(r["rig"].lower(), set()).add(r["status"])
        return st
    o_pairs, n_pairs = by_pair(old), by_pair(new)
    o_st, n_st = rig_status(old), rig_status(new)
    evs = []

    def ev(typ, r, headline, extra=None):
        e = {"id": _sha(url, r["rig"], typ, day), "date": day, "source": "fsr_diff", "source_name": f"{name} FSR", "url": url, "type": typ, "company": name, "code": code,
             "rig": r["rig"], "rig_type": r.get("rig_type") or "unknown", "region": r.get("region") or "other", "customer": r.get("customer"), "start": r.get("start"),
             "duration_days": None, "dayrate_usd_k": r.get("dayrate_usd_k"), "tcv_usd_m": None, "quote": f"FSR {day}: {r['rig']} {r.get('customer') or ''} {r.get('start') or ''}~{r.get('end') or ''} {r.get('status')}".strip(),
             "headline": headline}
        if extra:
            e.update(extra)
        evs.append(e)
    for k, r in n_pairs.items():
        o = o_pairs.get(k)
        if not o:
            ev("award", r, f"{r['rig']}: 새 계약 ({r.get('customer') or '고객 미상'})")
            continue
        if (r.get("end") or "") > (o.get("end") or "") and o.get("end"):
            ev("extension", r, f"{r['rig']}: 계약 종료 {o['end']} → {r['end']}", {"prev_end": o["end"]})
        if r.get("dayrate_usd_k") is not None and o.get("dayrate_usd_k") is not None and abs(r["dayrate_usd_k"] - o["dayrate_usd_k"]) > DAYRATE_CHANGE_MIN_K:
            ev("dayrate_change", r, f"{r['rig']}: 단가 ${o['dayrate_usd_k']:g}K → ${r['dayrate_usd_k']:g}K", {"prev_dayrate_usd_k": o["dayrate_usd_k"]})
    for rig, sts in n_st.items():
        o = o_st.get(rig, set())
        row = next((r for r in new["rigs"] if r["rig"].lower() == rig), None)
        if not row:
            continue
        if "stacked" in o and "firm" in sts and "firm" not in o:
            ev("reactivation", row, f"{row['rig']}: 스택 → 계약")
        if "firm" in o and "stacked" in sts and "stacked" not in o:
            ev("stacking", row, f"{row['rig']}: 계약 → 스택")
        if "retired" in sts and "retired" not in o:
            ev("retirement", row, f"{row['rig']}: 퇴역")
    for rig in set(o_st) - set(n_st):
        row = next((r for r in old["rigs"] if r["rig"].lower() == rig), None)
        if row and "retired" in o_st[rig]:
            ev("retirement", row, f"{row['rig']}: 목록에서 제외(퇴역)")
    store = _events()
    n = _add_events(store, evs)
    _save(EVENTS_PATH, store)
    return {"events": n}


# ---------- 4) 파생 지표 ----------
def _month_days(y, m):
    import calendar
    return calendar.monthrange(y, m)[1]


def _overlap_days(start, end, year):
    """'YYYY-MM' 구간과 연도의 겹치는 일수 (start 없으면 연초, end 없으면 연말로 가정하지 않고 0)"""
    if not end and not start:
        return 0
    ys, ye = date(year, 1, 1), date(year, 12, 31)
    try:
        s = date(int(start[:4]), int(start[5:7]), 1) if start else ys
        e = date(int(end[:4]), int(end[5:7]), _month_days(int(end[:4]), int(end[5:7]))) if end else ye
    except (ValueError, TypeError):
        return 0
    lo, hi = max(s, ys), min(e, ye)
    return max(0, (hi - lo).days + 1)


def coverage_from_fsr(code, year):
    """최신 FSR rows 로 해당 연도 커버리지: firm 일수 합 ÷ (마케팅 리그 수 × 365). 마케팅 리그 = stacked·retired 아닌 리그.
    옵션(option·priced_option) 포함 수치도. gap_rigs = 그 연도 안에 마지막 firm/option 구간이 끝나고 뒤 구간이 없는 리그"""
    rep = latest_fsr(code)
    if not rep:
        return None
    rigs = {}
    for r in rep["rigs"]:
        rigs.setdefault(r["rig"], []).append(r)
    marketed = [rig for rig, rows in rigs.items() if not all(r["status"] in ("stacked", "retired") for r in rows)]
    if not marketed:
        return {"year": year, "firm_pct": None, "with_options_pct": None, "marketed_rigs": 0, "gap_rigs": [], "report_date": rep["report_date"]}
    firm_days = opt_days = 0
    gaps = []
    for rig in marketed:
        rows = rigs[rig]
        f = sum(_overlap_days(r.get("start"), r.get("end"), year) for r in rows if r["status"] == "firm")
        o = sum(_overlap_days(r.get("start"), r.get("end"), year) for r in rows if r["status"] in ("option", "priced_option"))
        firm_days += min(f, 365)
        opt_days += min(f + o, 365)
        ends = [r.get("end") for r in rows if r["status"] in ("firm", "option", "priced_option") and r.get("end")]
        last_end = max(ends) if ends else None
        if last_end and last_end < f"{year}-12":
            gaps.append({"rig": rig, "last_end": last_end})
    denom = len(marketed) * 365
    return {"year": year, "firm_pct": round(firm_days / denom * 100, 1), "with_options_pct": round(opt_days / denom * 100, 1), "marketed_rigs": len(marketed),
            "stacked_rigs": len(rigs) - len(marketed), "gap_rigs": sorted(gaps, key=lambda g: g["last_end"]), "report_date": rep["report_date"]}


def _start_month(e):
    """이벤트의 점 월 = date(발표·보고서일). start(계약 시작월)는 툴팁·최근 체결 표·지역 매트릭스 건수에만 쓴다 (E)."""
    return (e.get("date") or "")[:7]


def _pct(vals, p):
    vals = sorted(vals)
    if not vals:
        return None
    k = (len(vals) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def leading_dayrates(months=24):
    """events(dayrate 있음; 점의 월 = date 발표·보고서일) + 최신 FSR 의 최근 6개월 시작 firm 행(점의 월 = 보고서일) →
    {segment: {"months": [{month, median, p25, p75, n, rolling3, rolling3_n, points}], "summary": {...}}}"""
    today = date.today()
    since = (today.replace(day=1) - timedelta(days=months * 31)).isoformat()[:7]
    pts = {"drillship": {}, "harsh": {}}
    def seg_of(rt):
        return "harsh" if rt == "harsh" else "drillship" if rt in ("drillship", "benign_semi", "unknown", None) else None
    for e in _events()["events"]:
        if e.get("dayrate_usd_k") is None or e.get("rig_type") == "jackup":
            continue
        sg = seg_of(e.get("rig_type"))
        mo = _start_month(e)
        if sg and mo >= since:
            pts[sg].setdefault(mo, []).append({"rig": e.get("rig"), "customer": e.get("customer"), "value": e["dayrate_usd_k"], "source": e.get("source_name"), "url": e.get("url"),
                                                "date": e.get("date"), "start": e.get("start"), "region": e.get("region"), "code": e.get("code"), "company": e.get("company"),
                                                "duration_days": e.get("duration_days"), "quote": e.get("quote"), "type": e.get("type"), "event_id": e.get("id")})
    six = (today - timedelta(days=183)).isoformat()[:7]
    cfg = DT.load_config()
    for code in cfg["tickers"]:
        rep = latest_fsr(code)
        if not rep:
            continue
        for r in rep["rigs"]:
            if r["status"] != "firm" or r.get("dayrate_usd_k") is None or not r.get("start") or r["start"] < six or r["start"] > today.isoformat()[:7]:
                continue
            sg = seg_of(r.get("rig_type"))
            if sg:
                pts[sg].setdefault(rep["report_date"][:7], []).append({"rig": r["rig"], "customer": r.get("customer"), "value": r["dayrate_usd_k"], "source": f"{cfg['names'].get(code, code)} FSR", "url": rep["url"],
                                                           "date": rep["report_date"], "start": r["start"], "region": r.get("region"), "code": code, "company": cfg["names"].get(code, code),
                                                           "duration_days": None, "quote": f"FSR {rep['report_date']}: {r['rig']} {r.get('customer') or ''} {r['start']}~{r.get('end') or ''}", "type": "fsr_row", "event_id": None})
    out = {}
    for sg, by_m in pts.items():
        rows = []
        for m in sorted(by_m):
            ps = by_m[m]
            y, mo = int(m[:4]), int(m[5:7])
            win = []
            for k in range(3):   # 해당 월 포함 직전 3개월
                yy, mm = y, mo - k
                while mm <= 0:
                    mm += 12
                    yy -= 1
                win += by_m.get(f"{yy}-{mm:02d}", [])
            vals = [p["value"] for p in ps]
            wv = [p["value"] for p in win]
            rows.append({"month": m, "median": statistics.median(vals), "p25": _pct(vals, 0.25), "p75": _pct(vals, 0.75), "n": len(ps), "points": ps,
                         "rolling3": statistics.median(wv) if wv else None, "rolling3_p25": _pct(wv, 0.25), "rolling3_p75": _pct(wv, 0.75), "rolling3_n": len(win)})
        # 요약: 최근 3개월 중앙값 vs 직전 3개월(최근 월 기준 -3개월 창)
        summ = {"recent": None, "prev": None, "chg_pct": None, "n": 0, "insufficient": True}
        if rows:
            last = rows[-1]
            y, mo = int(last["month"][:4]), int(last["month"][5:7])
            prev_win = []
            for k in range(3, 6):
                yy, mm = y, mo - k
                while mm <= 0:
                    mm += 12
                    yy -= 1
                prev_win += by_m.get(f"{yy}-{mm:02d}", [])
            pv = [p["value"] for p in prev_win]
            summ = {"recent": last["rolling3"], "recent_month": last["month"], "n": last["rolling3_n"], "prev": statistics.median(pv) if pv else None, "prev_n": len(pv),
                    "chg_pct": round((last["rolling3"] / statistics.median(pv) - 1) * 100, 1) if pv and last["rolling3"] else None,
                    "insufficient": last["rolling3_n"] < 5 or len(pv) < 5}      # 신호와 같은 기준(양쪽 창 n>=5) (G)
        out[sg] = {"months": rows, "summary": summ}
    # 최근 체결 10건 (세그먼트 무관, start 내림차순)
    allp = [p for sg in pts.values() for ps in sg.values() for p in ps]
    allp.sort(key=lambda p: ((p.get("start") or p.get("date") or ""), p.get("date") or ""), reverse=True)
    seen_k, recent = set(), []
    for p in allp:
        k = ((p.get("rig") or "").lower(), (p.get("start") or "")[:7], p.get("value"))
        if k in seen_k:
            continue
        seen_k.add(k)
        recent.append(p)
    out["recent"] = recent[:10]
    return out


def market_series():
    """market_stats → {metric: {segment: [{as_of, value, unit, source_name, region, url, quote}]}} (as_of 오름차순)"""
    out = {}
    for s in _stats()["stats"]:
        out.setdefault(s["metric"], {}).setdefault(s["segment"], []).append(
            {"as_of": s["as_of"], "date": s["date"], "value": s["value"], "unit": s.get("unit"), "source_name": s.get("source_name"), "region": s.get("region"), "url": s.get("url"), "quote": s.get("quote")})
    for m in out.values():
        for lst in m.values():
            lst.sort(key=lambda x: x["as_of"])
    return out


def recent_events(days=60, type_=None, code=None):
    since = (date.today() - timedelta(days=days)).isoformat()
    evs = [e for e in _events()["events"] if (e.get("date") or "") >= since]
    if type_:
        evs = [e for e in evs if e.get("type") == type_]
    if code:
        evs = [e for e in evs if e.get("code") == code.upper()]
    evs.sort(key=lambda e: (e.get("date") or "", e.get("id")), reverse=True)
    return evs


# ---------- 5) 주가 기준선 (C1) ----------
PRICES_PATH = os.path.join(CACHE_DIR, "prices.json")
PRICES_MIN_INTERVAL_SEC = 3600


def _yahoo_backoff_active():
    try:
        import batch_top_gainers as B
        until = B._yahoo_backoff_until()
        from datetime import timezone
        return until is not None and datetime.now(timezone.utc) < until
    except Exception:
        return False


def _set_backoff(reason):
    try:
        import batch_top_gainers as B
        B._set_yahoo_backoff("drilling", reason)
    except Exception:
        pass


def fetch_prices(force=False):
    """RIG·SDRL·NE·OIH·브렌트(BZ=F, 실패 시 BNO)·XOM 일간 종가 3년 → cache/drilling/prices.json {"as_of", "series": {ticker: [[date, close], ...]}}.
    1시간 안 재호출 금지(저장본 반환). Yahoo 백오프 중이면 건너뜀. 반환에 status 포함."""
    cur = _load(PRICES_PATH, None)
    if cur and not force:
        try:
            age = (datetime.now() - datetime.fromisoformat(cur.get("fetched_at"))).total_seconds()
            if age < PRICES_MIN_INTERVAL_SEC:
                return dict(cur, status="cached")
        except (TypeError, ValueError):
            pass
    if _yahoo_backoff_active():
        return dict(cur or {"as_of": None, "series": {}}, status="backoff")
    cfg = DT.load_config()
    bm = cfg.get("benchmark") or {}
    tickers = list(cfg["tickers"]) + [bm.get("sector") or "OIH", bm.get("oil") or "BZ=F", "XOM"]
    import yfinance as yf
    series = {}
    try:
        df = yf.download(tickers, period="3y", interval="1d", progress=False, threads=False, auto_adjust=True)
        closes = df["Close"] if "Close" in df else df
        for t in tickers:
            col = closes[t] if t in closes else None
            if col is None:
                continue
            col = col.dropna()
            series[t] = [[d.strftime("%Y-%m-%d"), round(float(v), 4)] for d, v in col.items()]
    except Exception as e:
        msg = str(e)
        if "429" in msg or "Too Many" in msg or "rate" in msg.lower():
            _set_backoff(msg)
        return dict(cur or {"as_of": None, "series": {}}, status=f"error:{type(e).__name__}")
    oil = bm.get("oil") or "BZ=F"
    if not series.get(oil) and bm.get("oil_fallback"):
        try:
            col = yf.download(bm["oil_fallback"], period="3y", interval="1d", progress=False, threads=False, auto_adjust=True)["Close"].dropna()
            if hasattr(col, "columns"):
                col = col.iloc[:, 0]
            series[oil] = [[d.strftime("%Y-%m-%d"), round(float(v), 4)] for d, v in col.items()]
            series["_oil_source"] = bm["oil_fallback"]
        except Exception:
            pass
    oil_src = series.pop("_oil_source", oil)
    if not series:
        return dict(cur or {"as_of": None, "series": {}}, status="empty")
    as_of = max(v[-1][0] for v in series.values() if v)
    out = {"as_of": as_of, "fetched_at": _now(), "tickers": tickers, "oil_ticker": oil, "oil_source": oil_src, "series": series}
    _save(PRICES_PATH, out)
    return dict(out, status="ok")


def _series_map(rows):
    return {d: v for d, v in rows}


def _weekly(rows):
    """일간 [[date, close]] → 주간(금요일 기준 마지막 거래일) 종가 [[week_end, close]]"""
    out, cur_week, last = [], None, None
    for d, v in rows:
        y, w, _ = date.fromisoformat(d).isocalendar()
        key = (y, w)
        if cur_week is not None and key != cur_week:
            out.append(last)
        cur_week, last = key, [d, v]
    if last:
        out.append(last)
    return out


def _returns(rows):
    return [(rows[i][0], rows[i][1] / rows[i - 1][1] - 1) for i in range(1, len(rows)) if rows[i - 1][1]]


def _beta(y, x):
    """최소제곱 베타 (y = a + b x). 반환 (beta, corr) — 표본 < 8 이면 (None, None)"""
    n = min(len(y), len(x))
    if n < 8:
        return None, None
    y, x = y[-n:], x[-n:]
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    if sxx <= 0 or syy <= 0:
        return None, None
    return sxy / sxx, sxy / (sxx * syy) ** 0.5


def benchmark_stats():
    """prices.json → rebased(1년=100), rs(종가/OIH, 60일선 대비 %), beta(52주 주간), decomposition(5일·20일 총/업종분/고유분), corr13w(13주 롤링)"""
    pr = _load(PRICES_PATH, None)
    if not pr or not pr.get("series"):
        return None
    cfg = DT.load_config()
    bm = cfg.get("benchmark") or {}
    sector, oil = bm.get("sector") or "OIH", pr.get("oil_ticker") or bm.get("oil") or "BZ=F"
    S = pr["series"]
    codes = [c for c in cfg["tickers"] if S.get(c)]
    if not S.get(sector):
        return {"as_of": pr.get("as_of"), "error": f"{sector} 시세 없음"}
    today = date.fromisoformat(pr["as_of"])
    one_y = (today - timedelta(days=365)).isoformat()
    two_y = (today - timedelta(days=730)).isoformat()
    out = {"as_of": pr.get("as_of"), "sector": sector, "oil": oil, "oil_source": pr.get("oil_source"), "codes": codes}
    # (a) rebased 1년
    reb = {}
    for t in codes + [sector, oil, "XOM"]:
        rows = [r for r in S.get(t) or [] if r[0] >= one_y]
        if rows and rows[0][1]:
            base = rows[0][1]
            reb[t] = [[d, round(v / base * 100, 2)] for d, v in rows]
    out["rebased"] = reb
    # (b) 상대강도 X/OIH 1년 + 60일 이동평균, 현재 위치 %
    sec_map = _series_map(S[sector])
    rs = {}
    for c in codes:
        ratio = [[d, v / sec_map[d]] for d, v in S[c] if d in sec_map and sec_map[d] and d >= one_y]
        if len(ratio) < 5:
            continue
        ma = []
        for i in range(len(ratio)):
            win = [r[1] for r in ratio[max(0, i - 59):i + 1]]
            ma.append([ratio[i][0], sum(win) / len(win)])
        pos = (ratio[-1][1] / ma[-1][1] - 1) * 100 if ma[-1][1] else None
        rs[c] = {"ratio": [[d, round(v, 4)] for d, v in ratio], "ma60": [[d, round(v, 4)] for d, v in ma], "pos_vs_ma60_pct": round(pos, 1) if pos is not None else None}
    out["rs"] = rs
    # (c) 베타 (주간 52주)
    wk = {t: _weekly([r for r in S.get(t) or [] if r[0] >= two_y]) for t in codes + [sector, oil]}
    wret = {t: {d: r for d, r in _returns(v)} for t, v in wk.items()}
    beta = {}
    for c in codes:
        common_s = sorted(set(wret[c]) & set(wret[sector]))[-52:]
        common_o = sorted(set(wret[c]) & set(wret.get(oil) or {}))[-52:]
        bs, cs = _beta([wret[c][d] for d in common_s], [wret[sector][d] for d in common_s])
        bo, co = _beta([wret[c][d] for d in common_o], [wret[oil][d] for d in common_o]) if common_o else (None, None)
        beta[c] = {"sector_beta": round(bs, 2) if bs is not None else None, "sector_corr": round(cs, 2) if cs is not None else None,
                   "oil_beta": round(bo, 2) if bo is not None else None, "oil_corr": round(co, 2) if co is not None else None, "weeks": len(common_s)}
    out["beta"] = beta
    # (d) 분해: 5일·20일 수익률 = 업종분(베타×OIH) + 고유분
    def ret_n(t, n):
        rows = S.get(t) or []
        if len(rows) <= n or not rows[-1 - n][1]:
            return None
        return rows[-1][1] / rows[-1 - n][1] - 1
    dec = {}
    for c in codes:
        b = (beta.get(c) or {}).get("sector_beta")
        dec[c] = {}
        for n, thr in ((5, 5), (20, 10)):
            tot, sec = ret_n(c, n), ret_n(sector, n)
            if tot is None or sec is None or b is None:
                dec[c][f"d{n}"] = None
                continue
            part = b * sec
            resid = tot - part
            dec[c][f"d{n}"] = {"total_pct": round(tot * 100, 1), "sector_part_pct": round(part * 100, 1), "residual_pct": round(resid * 100, 1), "sector_ret_pct": round(sec * 100, 1),
                              "flag": "회사 고유 움직임" if abs(resid * 100) > thr else None, "window_end": S[c][-1][0], "window_start": S[c][-1 - n][0]}
    out["decomposition"] = dec
    # (e) 13주 롤링 상관: 3사 평균 주간 수익률 vs OIH / 브렌트 (최근 2년)
    weeks = sorted(set.intersection(*[set(wret[c]) for c in codes]) & set(wret[sector])) if codes else []
    avg = {d: sum(wret[c][d] for c in codes) / len(codes) for d in weeks}
    corr = []
    for i in range(12, len(weeks)):
        win = weeks[i - 12:i + 1]
        ya = [avg[d] for d in win]
        _, cs = _beta(ya, [wret[sector][d] for d in win])
        ow = [d for d in win if d in (wret.get(oil) or {})]
        _, co = _beta([avg[d] for d in ow], [wret[oil][d] for d in ow]) if len(ow) >= 8 else (None, None)
        corr.append([weeks[i], round(cs, 2) if cs is not None else None, round(co, 2) if co is not None else None])
    out["corr13w"] = corr
    return out


# ---------- 6) 텔레그램 일간 요약 (C5) ----------
NOTIFY_TYPES = ("award", "extension", "termination", "suspension", "reactivation", "stacking", "retirement", "ma", "guidance")
NOTIFIED_PATH = os.path.join(CACHE_DIR, "notified.json")


def _region_ko(key):
    return dict(DT.REGIONS).get(key, key or "-")


def notify_daily(dry_run=False):
    """그날 새로 들어온 이벤트(주요 type 또는 단가 있음)·시장 수치를 텔레그램 1건으로. 없으면 발송 안 함. notified.json 으로 중복 방지."""
    cfg = DT.load_config()
    if not cfg.get("telegram_daily", True) and not dry_run:
        return {"sent": False, "reason": "telegram_daily=false"}
    sent_ids = set((_load(NOTIFIED_PATH, {}) or {}).get("ids") or [])
    evs = [e for e in _events()["events"] if e["id"] not in sent_ids and (e.get("type") in NOTIFY_TYPES or e.get("dayrate_usd_k") is not None)]
    sts = [s for s in _stats()["stats"] if s["id"] not in sent_ids]
    if not evs and not sts:
        return {"sent": False, "reason": "새 항목 없음"}
    evs.sort(key=lambda e: (e.get("date") or "", e.get("type") == "dayrate_change"), reverse=True)
    ov = _load(DT.OVERVIEW, {}) or {}
    ck = ov.get("checklist") or {}
    dr = ((ov.get("signals") or {}).get("dayrate_leading") or {}).get("drillship") or {}
    dr_txt = f"{dr['latest']['value_text']}({dr.get('direction_ko') or '-'})" if dr.get("latest") else "-"
    lines = [f"⛽ 시추 업황 {date.today().isoformat()}",
             f"체크리스트 {ck.get('on', '-')}/{ck.get('of', 5)} · 단가 3개월 중앙값 {dr_txt}"]
    for e in evs[:8]:
        who = (e.get("code") or e.get("company") or "").strip()
        bits = [x for x in (who, e.get("rig"), _region_ko(e.get("region")), e.get("customer"),
                            f"{e['duration_days']:g}일" if e.get("duration_days") else (e.get("start") or None),
                            f"${e['dayrate_usd_k']:g}K" if e.get("dayrate_usd_k") is not None else None) if x]
        src = {"news": e.get("source_name") or "뉴스", "8-K": "8-K", "fsr_diff": "FSR"}.get(e.get("source"), e.get("source"))
        lines.append(f"• [{e.get('type')}] " + " / ".join(bits) + f" ({src})")
    if len(evs) > 8:
        lines.append(f"  … {len(evs) - 8}건 더")
    for s in sts[:4]:
        m = {"contracted_rigs": "계약", "utilization": "가동률", "dayrate": "단가", "duration": "계약기간", "fleet_count": "선대", "retirements": "퇴역"}.get(s["metric"], s["metric"])
        seg = {"drillship": "드릴십", "harsh": "한랭지", "floater_total": "부유식", "jackup": "잭업"}.get(s.get("segment"), s.get("segment"))
        reg = f" {_region_ko(s['region'])}" if s.get("region") and s["region"] != "other" else ""
        md = s["as_of"][5:].replace("-", "/") if s.get("as_of") else ""
        lines.append(f"📊 {s.get('source_name') or '-'}: {m} {seg}{reg} {s['value']:g}{(' ' + s['unit']) if s.get('unit') and not s['unit'].startswith('rig') else '척' if s['metric'] in ('contracted_rigs', 'fleet_count') else ''}({md})")
    if len(sts) > 4:
        lines.append(f"  … 시장 수치 {len(sts) - 4}건 더")
    text = "\n".join(lines)
    if dry_run:
        return {"sent": False, "dry_run": True, "events": len(evs), "stats": len(sts), "text": text}
    import earnings_calls
    ok = earnings_calls._tg_send_html(text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    if ok:
        _save(NOTIFIED_PATH, {"ids": sorted(sent_ids | {e["id"] for e in evs} | {s["id"] for s in sts})[-5000:], "last_sent": _now()})
    _log(f"telegram sent={ok} events={len(evs)} stats={len(sts)}")
    return {"sent": bool(ok), "events": len(evs), "stats": len(sts), "chars": len(text)}


# ---------- 실행 ----------
def run(news=True, eight_k=True, fsr=True, max_ai=None):
    t0 = time.time()
    out = {"started": _now()}
    if news:
        try:
            out["news"] = scan_news(max_ai)
        except Exception as e:
            out["news"] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    if eight_k:
        try:
            out["eight_k"] = scan_8k(max_ai)
        except Exception as e:
            out["eight_k"] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    if fsr:
        try:
            out["fsr"] = check_fsr(max_ai)
        except Exception as e:
            out["fsr"] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
        out["fsr_diff"] = {}
        for code in DT.load_config()["tickers"]:
            try:
                out["fsr_diff"][code] = diff_fsr(code)
            except Exception as e:
                out["fsr_diff"][code] = {"error": f"{type(e).__name__}: {str(e)[:80]}"}
    try:
        pr = fetch_prices()
        out["prices"] = {"as_of": pr.get("as_of"), "status": pr.get("status"), "n": {k: len(v) for k, v in (pr.get("series") or {}).items()}}
    except Exception as e:
        out["prices"] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    try:
        ov = DT.overview()
        out["overview"] = {"headline": ov.get("headline"), "events_60d": len(ov.get("events") or [])}
    except Exception as e:
        out["overview"] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    try:
        out["telegram"] = notify_daily()
    except Exception as e:
        out["telegram"] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    out["seconds"] = round(time.time() - t0, 1)
    nw, ek, fs = out.get("news") or {}, out.get("eight_k") or {}, out.get("fsr") or {}
    summary = (f"news ai={nw.get('ai', '-')} rel={nw.get('relevant', '-')} ev={nw.get('events', '-')} st={nw.get('stats', '-')}"
               + (f" err={nw['error']}" if nw.get("error") else "")
               + f" | 8k filings={ek.get('filings', '-')} ev={ek.get('events', '-')}" + (f" err={ek['error']}" if ek.get("error") else "")
               + " | fsr " + (" ".join(f"{c}:{v.get('processed', 0)}/{v.get('found', 0)}" for c, v in fs.items() if isinstance(v, dict)) if isinstance(fs, dict) and not fs.get("error") else str(fs))
               + f" | {out['seconds']}s")
    _log(summary)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="시추 업황 일간 수집기")
    ap.add_argument("--once", action="store_true", help="전체 실행 (news → 8-K → FSR → diff → overview)")
    ap.add_argument("--news-only", action="store_true")
    ap.add_argument("--fsr-only", action="store_true")
    ap.add_argument("--max-ai", type=int, default=None, help="이번 실행의 AI 호출 상한(ai_budget limit override)")
    ap.add_argument("--fsr-url", nargs=2, metavar=("CODE", "URL"), help="특정 FSR PDF 1부 처리 + diff + overview")
    a = ap.parse_args()
    if a.fsr_url:
        code, url = a.fsr_url[0].upper(), a.fsr_url[1]
        print(json.dumps({"fsr": check_fsr(a.max_ai, force_url=(code, url)), "diff": diff_fsr(code)}, ensure_ascii=False, indent=1))
        ov = DT.overview()
        print("[drilling] overview:", ov.get("headline"))
    elif a.news_only:
        run(news=True, eight_k=True, fsr=False, max_ai=a.max_ai)
    elif a.fsr_only:
        run(news=False, eight_k=False, fsr=True, max_ai=a.max_ai)
    else:
        run(max_ai=a.max_ai)
