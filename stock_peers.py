"""관련 기업 — AI 후보 제안 → 사용자 확정, 밸류에이션 비교표, 컨콜 요약 모음.

저장: cache/peers/<MARKET>_<code>.json
  {confirmed: [{market, code, name, reason, relation, added_at, source(ai|user)}],
   suggested: [{market, code, name, relation, reason, confidence, suggested_at}],
   rejected: ["KR:005930", ...], ai_called_at, ai_log}
후보 풀(참고용): 한국 = 같은 WICS 중분류(cache/stock_wics.json) + 네이버 동일업종(industryCompareInfo),
                미국 = 러셀3000(tickers/us_russell3000.json) 같은 GICS 대분류 상위 150 (파일 순서 = 지수 비중순).
AI: us_financials._call_ai(Haiku) 재사용, 한도는 ai_budget kind 'peers'(기본 50).
비교표·컨콜: 해당 종목을 열 때와 같은 함수(stock_profile/us_financials.get_summary, kr/us_financials.compute, earnings_calls)만 쓴다.
"""
import json
import os
import re
import threading
import time
import urllib.request
from datetime import date, datetime

import ai_budget
import watchlist as watchlist_store

_BD = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(_BD, "cache", "peers")
WICS_PATH = os.path.join(_BD, "cache", "stock_wics.json")
RUSSELL_PATH = os.path.join(_BD, "tickers", "us_russell3000.json")
SCREENER_PATH = os.path.join(_BD, "cache", "valuation_screener.json")
DART_MAP_PATH = os.path.join(_BD, "dart_corp_map.json")
US_MAP_PATH = os.path.join(_BD, "us_stock_map.json")
OVERVIEW_PATH = os.path.join(_BD, "business_overview_cache.json")
US_POOL_N = 150
RELATIONS = ("경쟁사", "고객", "공급사", "동일 밸류체인", "대체재")

_lock = threading.Lock()
_maps = {}


# ---------- 공통 ----------
def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _path(market, code):
    return os.path.join(CACHE_DIR, f"{market}_{code}.json")


def _cached_map(name, path):
    """json 파일을 수정 시각 기준으로 메모리 캐시"""
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return {}
    hit = _maps.get(name)
    if hit and hit[0] == mt:
        return hit[1]
    data = _load_json(path, {})
    _maps[name] = (mt, data)
    return data


def dart_map():
    return _cached_map("dart", DART_MAP_PATH)


def us_map():
    return _cached_map("us", US_MAP_PATH)


def resolve(market, code):
    """(market, 정규화 코드, 이름, corp_code|None) — 맵에 없으면 None"""
    market = (market or "").strip().upper()
    c = watchlist_store.normalize_code(market, code)
    if not c:
        return None
    if market == "KR":
        info = dart_map().get(c)
        return (market, c, info.get("name", ""), info.get("corp_code")) if info else None
    if market == "US":
        info = us_map().get(c)
        return (market, c, _short_us_name(info.get("name", "")), None) if info else None
    return None


def _short_us_name(name):
    """'Micron Technology, Inc. - Common Stock' → 'Micron Technology, Inc.' (거래소 종목 설명 꼬리 제거)"""
    return re.sub(r"\s+-\s+(Common Stock|Class [A-Z].*|Ordinary Shares|American Depositary.*|Depositary.*).*$", "", name or "").strip()


def _key(market, code):
    return f"{market}:{code}"


def load(market, code):
    st = _load_json(_path(market, code), None) or {}
    st.setdefault("market", market)
    st.setdefault("code", code)
    st.setdefault("confirmed", [])
    st.setdefault("suggested", [])
    st.setdefault("rejected", [])
    st.setdefault("ai_called_at", None)
    st.setdefault("ai_log", [])
    return st


def save(st):
    st["updated_at"] = _now()
    _save_json(_path(st["market"], st["code"]), st)


# ---------- 후보 풀 ----------
def _kr_caps():
    scr = _load_json(SCREENER_PATH, {})
    return {i["code"]: i.get("market_cap_eok") for i in scr.get("items", []) if i.get("code")}


def _naver_peers(code):
    """네이버 동일업종 비교 목록 [{code, name, cap_eok}]"""
    try:
        req = urllib.request.Request(f"https://m.stock.naver.com/api/stock/{code}/integration", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8"))
        out = []
        for x in d.get("industryCompareInfo") or []:
            mv = (x.get("marketValue") or "").replace(",", "")
            out.append({"code": x.get("itemCode"), "name": x.get("stockName"),
                        "cap_eok": round(int(mv) / 100) if mv.isdigit() else None})
        return out, d.get("industryCode")
    except Exception as e:
        print(f"[stock_peers] 네이버 동일업종 실패 {code}: {type(e).__name__}", flush=True)
        return [], None


def candidate_pool(market, code):
    """{"label": 설명, "items": [{market, code, name, cap}], "industry": 업종명}"""
    if market == "KR":
        wics = _load_json(WICS_PATH, {}).get("mapping", {})
        me = wics.get(code) or {}
        mid = me.get("wics_mcls_cd")
        caps = _kr_caps()
        dm = dart_map()
        items, seen = [], set()
        for c, v in wics.items():
            if mid and v.get("wics_mcls_cd") == mid and c != code and c in dm:
                items.append({"market": "KR", "code": c, "name": dm[c]["name"], "cap_eok": caps.get(c)})
                seen.add(c)
        nv, _ = _naver_peers(code)
        for x in nv:
            if x["code"] and x["code"] != code and x["code"] not in seen and x["code"] in dm:
                items.append({"market": "KR", "code": x["code"], "name": x["name"], "cap_eok": x["cap_eok"], "naver": True})
                seen.add(x["code"])
        items.sort(key=lambda x: -(x.get("cap_eok") or 0))
        return {"label": f"WICS 중분류 '{me.get('wics_mcls_nm') or '?'}' {sum(1 for i in items if not i.get('naver'))}종목 + 네이버 동일업종 {len(nv)}종목",
                "items": items, "industry": me.get("wics_mcls_nm")}
    rows = _load_json(RUSSELL_PATH, [])
    me = next((r for r in rows if r.get("ticker") == code), None)
    sector = me.get("sector") if me else None
    items = [{"market": "US", "code": r["ticker"], "name": r.get("name"), "rank": i + 1}
             for i, r in enumerate(r2 for r2 in rows if sector and r2.get("sector") == sector and r2.get("ticker") != code)][:US_POOL_N]
    sic = None
    try:
        import us_financials
        c = us_financials._load_cache(code)
        if c and c.get("sic_desc"):
            sic = f"SIC {c.get('sic')} {c.get('sic_desc')}"
    except Exception:
        pass
    return {"label": f"러셀3000 GICS '{sector or '?'}' 상위 {len(items)}종목(지수 비중순)" + (f", {sic}" if sic else ""),
            "items": items, "industry": (sic or sector)}


def _overview(market, code, name):
    """사업 개요: business_overview_cache(한국) → 없으면 최근 리포트 요약 2건 → 없으면 None. AI 호출 없음."""
    if market == "KR":
        ov = _load_json(OVERVIEW_PATH, {}).get(code, {}).get("overview")
        if ov:
            return ov, "business_overview_cache"
    try:
        import stock_reports
        summaries = _load_json(stock_reports._sum_path(market, code), {})
        ok = sorted([s for s in summaries.values() if s.get("status") == "ok" and s.get("summary")],
                    key=lambda s: s.get("published") or "", reverse=True)[:2]
        if ok:
            return "\n".join(f"- [{s.get('broker')} {s.get('published')}] " + " / ".join(s["summary"][:5]) for s in ok), "reports"
    except Exception:
        pass
    return None, None


# ---------- AI 제안 ----------
PEERS_SYSTEM = """당신은 주식 애널리스트입니다. 기준 종목과 사업상 밀접한 관련 기업을 고릅니다.

규칙
- 5~8개를 고른다. 관계 유형은 경쟁사 | 고객 | 공급사 | 동일 밸류체인 | 대체재 중 하나.
- 후보 풀은 참고일 뿐이다. 풀 밖의 기업, 다른 시장(한국 종목이면 미국 상장사, 미국 종목이면 한국 상장사)도 사업 관련성이 높으면 반드시 넣는다. 예: 메모리 반도체 회사면 Micron(US:MU)과 삼성전자(KR:005930).
- 한국 종목은 market "KR" 과 6자리 종목코드, 미국 종목은 market "US" 와 티커(예: MU, BRK.B). 비상장사·ETF·지수는 제외. 확실하지 않은 코드는 넣지 않는다.
- 근거는 한 줄(한국어)로 어떤 사업에서 어떻게 연결되는지 쓴다. confidence 는 0~1.
- 기준 종목 자신은 제외.
- 출력은 JSON 하나만. 설명·마크다운·코드펜스 금지. 형식:
{"peers": [{"market": "KR", "code": "000660", "name": "SK하이닉스", "relation": "경쟁사", "reason": "", "confidence": 0.9}]}"""


def _parse_json(text):
    text = text.replace("```json", "").replace("```", "").strip()
    i, j = text.find("{"), text.rfind("}")
    if i == -1 or j == -1:
        raise ValueError("JSON 없음")
    return json.loads(text[i:j + 1])


def suggest(market, code, name, limit=None, force=False):
    """AI 후보 제안. 조건: confirmed·suggested 모두 비었고 오늘 안 불렀을 때, 또는 force. 반환 {status, suggested, dropped, tokens}"""
    import us_financials
    st = load(market, code)
    today = date.today().isoformat()
    if not force and (st["confirmed"] or st["suggested"] or (st.get("ai_called_at") or "")[:10] == today):
        return {"status": "skipped", "reason": "이미 목록이 있거나 오늘 호출함", "suggested": st["suggested"]}
    allowed, n = ai_budget.take("peers", limit=limit)
    if not allowed:
        return {"status": "ai_limit", "used": n, "suggested": st["suggested"]}
    pool = candidate_pool(market, code)
    ov, ov_src = _overview(market, code, name)
    pool_txt = "\n".join(
        f"- {i['market']}:{i['code']} {i['name']}" + (f" (시총 {i['cap_eok']:,}억원)" if i.get("cap_eok") else (f" (지수 비중 {i['rank']}위)" if i.get("rank") else ""))
        for i in pool["items"])
    exclude = set(st["rejected"]) | {_key(c["market"], c["code"]) for c in st["confirmed"]}
    user = (f"기준 종목: {name} ({market}:{code})\n업종: {pool.get('industry') or '모름'}\n\n"
            f"사업 개요({ov_src or '없음'}):\n{ov or '(자료 없음 — 종목명과 업종으로 판단)'}\n\n"
            + (f"이미 확정됐거나 제외된 기업(다시 제안하지 말 것): {', '.join(sorted(exclude))}\n\n" if exclude else "")
            + f"후보 풀(참고용, {pool['label']}):\n{pool_txt or '(없음)'}")
    t0 = time.time()
    try:
        text, tin, tout = us_financials._call_ai(PEERS_SYSTEM, user, max_tokens=2000)
        raw = _parse_json(text).get("peers") or []
    except Exception as e:
        print(f"[stock_peers] {market}:{code} AI 제안 실패: {type(e).__name__}: {str(e)[:120]}", flush=True)
        st["ai_called_at"] = _now()
        st["ai_log"].append({"at": _now(), "status": f"error:{type(e).__name__}"})
        save(st)
        return {"status": f"ai_error:{type(e).__name__}", "suggested": st["suggested"]}
    out, dropped, seen = [], [], set()
    for p in raw:
        if not isinstance(p, dict):
            continue
        r = resolve(p.get("market"), p.get("code"))
        if not r:
            dropped.append(f"{p.get('market')}:{p.get('code')} {p.get('name')}")
            continue
        m, c, nm, _ = r
        k = _key(m, c)
        if k == _key(market, code) or k in exclude or k in seen:
            continue
        seen.add(k)
        rel = p.get("relation") if p.get("relation") in RELATIONS else "동일 밸류체인"
        try:
            conf = max(0.0, min(1.0, float(p.get("confidence") or 0)))
        except (TypeError, ValueError):
            conf = None
        out.append({"market": m, "code": c, "name": nm, "ai_name": p.get("name"), "relation": rel,
                    "reason": str(p.get("reason") or "")[:200], "confidence": conf, "suggested_at": _now(),
                    "in_pool": any(i["market"] == m and i["code"] == c for i in pool["items"])})
    if dropped:
        print(f"[stock_peers] {market}:{code} 유효하지 않은 코드 {len(dropped)}건 버림: {dropped}", flush=True)
    log = {"at": _now(), "status": "ok", "tokens": {"input": tin, "output": tout}, "raw": len(raw), "kept": len(out),
           "dropped": dropped, "pool": pool["label"], "overview_src": ov_src, "seconds": round(time.time() - t0, 1), "daily": n}
    with _lock:
        st = load(market, code)
        st["suggested"] = out
        st["ai_called_at"] = _now()
        st["ai_log"] = (st["ai_log"] + [log])[-10:]
        save(st)
    print(f"[stock_peers] {market}:{code} AI 제안 {len(out)}건 (버림 {len(dropped)}), 토큰 {tin}+{tout}, 오늘 {n}", flush=True)
    return {"status": "ok", "suggested": out, "dropped": dropped, "tokens": log["tokens"], "pool": pool["label"]}


# ---------- 확정·추가·제거 ----------
def confirm(market, code, pm, pc):
    with _lock:
        st = load(market, code)
        k = _key(pm, pc)
        s = next((x for x in st["suggested"] if _key(x["market"], x["code"]) == k), None)
        if not s:
            return st, "suggested 에 없음"
        st["suggested"] = [x for x in st["suggested"] if _key(x["market"], x["code"]) != k]
        st["rejected"] = [x for x in st["rejected"] if x != k]
        if not any(_key(c["market"], c["code"]) == k for c in st["confirmed"]):
            st["confirmed"].append({"market": s["market"], "code": s["code"], "name": s["name"], "relation": s.get("relation"),
                                    "reason": s.get("reason"), "added_at": _now(), "source": "ai"})
        save(st)
        return st, None


def add(market, code, pm, pc, reason=""):
    r = resolve(pm, pc)
    if not r:
        return None, "종목을 찾을 수 없음"
    pm, pc, nm, _ = r
    if _key(pm, pc) == _key(market, code):
        return None, "기준 종목 자신"
    with _lock:
        st = load(market, code)
        k = _key(pm, pc)
        st["suggested"] = [x for x in st["suggested"] if _key(x["market"], x["code"]) != k]
        st["rejected"] = [x for x in st["rejected"] if x != k]
        if not any(_key(c["market"], c["code"]) == k for c in st["confirmed"]):
            st["confirmed"].append({"market": pm, "code": pc, "name": nm, "relation": None, "reason": reason or "직접 추가",
                                    "added_at": _now(), "source": "user"})
        save(st)
        return st, None


def remove(market, code, pm, pc):
    """confirmed 또는 suggested 에서 제거하고 rejected 에 기록 (다음 제안에서 제외)"""
    pm = (pm or "").upper()
    pc = watchlist_store.normalize_code(pm, pc) or pc
    with _lock:
        st = load(market, code)
        k = _key(pm, pc)
        before = len(st["confirmed"]) + len(st["suggested"])
        st["confirmed"] = [x for x in st["confirmed"] if _key(x["market"], x["code"]) != k]
        st["suggested"] = [x for x in st["suggested"] if _key(x["market"], x["code"]) != k]
        if k not in st["rejected"]:
            st["rejected"].append(k)
        save(st)
        return st, before != len(st["confirmed"]) + len(st["suggested"])


# ---------- 비교표 ----------
def _metrics(market, code, name):
    """재무 저장본 → (roe, revenue_yoy, operating_margin, ttm_label) 또는 None(저장본 없음)"""
    if market == "KR":
        import kr_financials
        cache = kr_financials._load_cache(code)
        if not cache or not cache.get("acc_mt"):
            return None
        fin = kr_financials.compute(cache, 5, name)
    else:
        import us_financials
        cache = us_financials._load_cache(code)
        if not cache or not cache.get("facts"):
            return None
        fin = us_financials.compute(cache, 5, name, persist=False)
    ttm = fin["periods"]["ttm"]
    if not ttm:
        return {}
    m = fin["metrics"]["ttm"].get(ttm[-1], {})
    return {"roe": m.get("roe"), "revenue_yoy": m.get("revenue_yoy"), "operating_margin": m.get("operating_margin"), "ttm": ttm[-1]}


def _start_fetch(market, code, corp_code, name):
    if market == "KR":
        import kr_financials
        if corp_code:
            kr_financials.start_fetch(code, corp_code)
    else:
        import us_financials
        us_financials.start_fetch(code, entity=name)


def _row(market, code, name, corp_code, fx, is_base=False):
    import stock_profile
    import us_financials
    try:
        s = us_financials.get_summary(code, name) if market == "US" else stock_profile.get_summary(market, code, name, corp_code)
    except Exception as e:
        print(f"[stock_peers] {market}:{code} 요약 실패: {type(e).__name__}", flush=True)
        s = {}
    try:
        m = _metrics(market, code, name)
    except Exception as e:
        print(f"[stock_peers] {market}:{code} 지표 실패: {type(e).__name__}", flush=True)
        m = {}
    status = "ok"
    if m is None:                      # 저장본 없음 → 뒤에서 받기 시작
        status = "fetching"
        try:
            _start_fetch(market, code, corp_code, name)
        except Exception as e:
            status = f"fetch_error:{type(e).__name__}"
        m = {}
    mcap = s.get("market_cap")
    mcap_usd = (mcap / fx if market == "KR" and mcap and fx else (mcap if market == "US" else None))
    return {"market": market, "code": code, "name": s.get("name") or name, "is_base": is_base, "status": status,
            "currency": s.get("currency") or ("KRW" if market == "KR" else "USD"),
            "price": s.get("price"), "change_pct": s.get("change_pct"), "market_cap": mcap, "market_cap_usd": mcap_usd,
            "per": s.get("per"), "fper": s.get("fper"), "pbr": s.get("pbr"), "fpbr": s.get("fpbr"),
            "roe": m.get("roe") if m.get("roe") is not None else s.get("roe"),
            "revenue_yoy": m.get("revenue_yoy"), "operating_margin": m.get("operating_margin"), "ttm": m.get("ttm"),
            "fsource": s.get("fsource")}


NUM_COLS = ("per", "fper", "pbr", "fpbr", "roe", "revenue_yoy", "operating_margin")


def compare_table(market, code, name, corp_code, confirmed, fx):
    rows = [_row(market, code, name, corp_code, fx, True)]
    for c in confirmed:
        r = resolve(c["market"], c["code"])
        if not r:
            rows.append({"market": c["market"], "code": c["code"], "name": c.get("name"), "status": "unknown"})
            continue
        rows.append(_row(r[0], r[1], c.get("name") or r[2], r[3], fx))
    avg = {"name": "평균", "is_avg": True, "n": {}}
    for col in NUM_COLS:
        vals = [r[col] for r in rows if r.get(col) is not None]
        avg[col] = round(sum(vals) / len(vals), 2) if vals else None
        avg["n"][col] = len(vals)
    mixed = len({r["market"] for r in rows}) > 1
    return {"rows": rows, "avg": avg, "mixed": mixed, "fx_usdkrw": fx if mixed else None,
            "fetching": [f"{r['market']}:{r['code']}" for r in rows if r.get("status") == "fetching"]}


# ---------- 컨콜 모음 ----------
def _latest_call_summary(code):
    import earnings_calls
    for q in reversed(earnings_calls.quarters(code)):
        summ = earnings_calls._load_json(earnings_calls._p(code, q["quarter"], "_summary"), None)
        if summ and summ.get("status") == "ok":
            return {"quarter": q["quarter"], "label": q["label"], "summary": summ["summary"], "at": summ.get("at")}
        st, _ = earnings_calls.transcript_state(code, q["quarter"])
        if st == "ok":
            return {"quarter": q["quarter"], "label": q["label"], "summary": None, "has_transcript": True}
    return None


def calls_digest(market, code, name, confirmed):
    """{outlook: [{market, code, name, quarter, lines}], calls: [{market, code, name, quarter, summary|None, kr_items}]}"""
    import earnings_calls
    outlook, calls = [], []
    targets = [{"market": market, "code": code, "name": name, "is_base": True}] + [dict(c, is_base=False) for c in confirmed]
    for t in targets:
        if t["market"] == "US":
            try:
                ls = _latest_call_summary(t["code"])
            except Exception as e:
                print(f"[stock_peers] {t['code']} 컨콜 조회 실패: {type(e).__name__}", flush=True)
                ls = None
            entry = {"market": "US", "code": t["code"], "name": t.get("name"), "is_base": t["is_base"],
                     "quarter": ls["quarter"] if ls else None, "label": ls["label"] if ls else None,
                     "summary": ls["summary"] if ls else None, "has_transcript": bool(ls and (ls.get("summary") or ls.get("has_transcript")))}
            calls.append(entry)
            if ls and ls.get("summary"):
                outlook.append({"market": "US", "code": t["code"], "name": t.get("name"), "is_base": t["is_base"],
                                "quarter": ls["quarter"], "lines": (ls["summary"].get("전망") or [])[:3], "tone": (ls["summary"].get("톤") or {}).get("판정")})
        else:
            try:
                r = resolve("KR", t["code"])
                items = earnings_calls.status_kr(t["code"], t.get("name") or (r[2] if r else ""))["items"][:2]
            except Exception as e:
                print(f"[stock_peers] {t['code']} 한국 컨콜 자료 실패: {type(e).__name__}", flush=True)
                items = []
            calls.append({"market": "KR", "code": t["code"], "name": t.get("name"), "is_base": t["is_base"], "kr_items": items})
            rep_ = next((i for i in items if i.get("source") == "리포트" and i.get("summary")), None)   # 텔레그램 글은 업황 종합에 넣지 않음
            if rep_:
                outlook.append({"market": "KR", "code": t["code"], "name": t.get("name"), "is_base": t["is_base"],
                                "quarter": rep_.get("date"), "lines": rep_["summary"][:3], "tone": None, "from_report": True})
    return {"outlook": outlook, "calls": calls}


# ---------- 상태 (API) ----------
def status(market, code, name, corp_code, fx):
    st = load(market, code)
    return {"market": market, "code": code, "name": name,
            "confirmed": st["confirmed"], "suggested": st["suggested"], "rejected": st["rejected"],
            "ai_called_at": st.get("ai_called_at"), "ai_log": st.get("ai_log", [])[-1:] ,
            "compare": compare_table(market, code, name, corp_code, st["confirmed"], fx),
            "calls": calls_digest(market, code, name, st["confirmed"]),
            "ai": {"used": ai_budget.used("peers"), "limit": ai_budget.DEFAULT_LIMITS["peers"]}}


def prefetch_confirmed(market, code, name=""):
    """05:30 미리 받기: 확정된 관련 기업의 재무 저장본과 최근 컨콜 요약(미국) 갱신. 반환 결과 문자열 목록"""
    import kr_financials
    import us_financials
    import earnings_calls
    out = []
    for c in load(market, code)["confirmed"]:
        r = resolve(c["market"], c["code"])
        if not r:
            continue
        m, cc, nm, corp = r
        try:
            if m == "KR":
                if corp and kr_financials.needs_fetch(cc):
                    kr_financials.fetch_blocking(cc, corp)
                    out.append(f"{cc} 재무")
            else:
                if us_financials.needs_fetch(cc):
                    us_financials.fetch_blocking(cc, entity=nm)
                    out.append(f"{cc} 재무")
                calls = earnings_calls.ensure_recent(cc, nm, 1, True)
                out.append(f"{cc} 컨콜 " + " ".join(f"{x['quarter']}:{x.get('transcript')}/{x.get('summary', '-')}" for x in calls))
        except Exception as e:
            out.append(f"{cc} 실패({type(e).__name__})")
    return out
