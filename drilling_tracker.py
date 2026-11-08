"""시추 업황 탭 (A): 해양 시추사(SDRL·RIG·NE) 실적 컨콜 녹취록·8-K 보도자료에서 업황(시장)·회사 KPI 를 AI 로 뽑아 분기별로 쌓는다.

- 방식은 earnings_calls.extract_adjusted 와 같다: 저장본만 읽고, 키워드 문단만 발췌해 AI 1회, 원문 인용(quote)을 함께 저장, 한 번 뽑으면(ok) 재호출 없음.
- 경로: cache/drilling/kpi/<code>/<quarter>.json (분기 KPI), cache/drilling/overview.json (탭 응답)
- 설정: drilling_config.json {tickers, names, quarters_back, merger_marks}
- AI: llm_client task 'drilling_kpi' (sonnet, json_mode), ai_budget 'drilling_kpi' 20/일
- 실행: python3 drilling_tracker.py --backfill [--force RIG:2026Q2] / --extract / --overview
"""
import argparse
import calendar
import json
import os
import re
import time
from datetime import date, datetime, timedelta

import ai_budget
import earnings_calls

_BD = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_BD, "drilling_config.json")
CACHE_DIR = os.path.join(_BD, "cache", "drilling")
KPI_DIR = os.path.join(CACHE_DIR, "kpi")
OVERVIEW = os.path.join(CACHE_DIR, "overview.json")
EXCERPT_MAX_CHARS = 35_000
RELEASE_HEAD_CHARS = 12_000
RELEASE_WINDOW_DAYS = 60

KPI_RE = re.compile(r"dayrate|day rate|day-rate|\$[0-9]{3},?000|backlog|contracted|coverage|available days|utili[sz]ation|stack|reactivat|retire|scrap|"
                    r"repric|legacy|free cash flow|buyback|repurchase|tender|FID|rig-years|rig years|contract (duration|length|term)|white space|gap|idle|"
                    r"mobili[sz]|SPS|special periodic survey|guidance|O&M|average duration|duration of contracts|\b\d{3} days\b", re.I)
RELEASE_RE = re.compile(r"backlog|free cash flow|repurchase|buyback|guidance", re.I)

KPI_SYSTEM = """You extract offshore-drilling KPIs from excerpts of ONE company's quarterly earnings call transcript (and, if given, its earnings press release).
Rules:
- Use only the excerpts. Do not convert units, estimate, annualize or compute. Copy numbers as stated, with their unit.
- Anything not stated in the excerpts -> null (or an empty array). Never invent values.
- Every value must come with "quote": one verbatim sentence from the excerpts that contains it (required whenever the value is not null).
- "market" items are ONLY statements management makes about the overall market / industry / fleet (e.g. "industry-wide contracted drillships", "market utilization", "leading-edge dayrates"). Company-specific figures never go into "market".
- market.contracted_rigs / utilization / dayrate_leading / contract_duration: CURRENT or most recent actual levels only. Any figure tied to a future point ("by 2027", "expected to", "approaching", "will reach", "forecast", "should move to") must NOT go there; put it in market.outlook instead.
- market.outlook: forward-looking market statements (metric: contracted_rigs|utilization|dayrate|duration|demand; horizon as stated, e.g. "2027", "2H26"; value if a number is given; direction up|flat|down). At most 6.
- "company" items are the company's own figures and outlook.
- dayrate_leading: the leading-edge / recently fixed market dayrate in USD thousands per day. A range "$450,000 to $500,000" -> low 450, high 500. A single stated level counts too: "$500,000" -> low 500, high 500; "mid-$400,000s" / "mid-400s" -> low 450, high 450; "high-$400,000s" -> 480/480; "low-$500,000s" -> 520/520. Always copy the sentence into quote. Include it even when the company is describing rates it recently achieved as representative of the market.
- contract_duration: the AVERAGE contract duration / term length of recent industry fixtures in days, as management states it (e.g. "average contract duration rose from 245 days to 380 days" -> value_days 380, prior_days 245). If stated in months or years, convert nothing: leave value_days null and put the sentence in quote.
- contracted_next_year / contracted_year_after: percent of available rig days (or fleet) already contracted for the coming years; put the calendar year in "year".
- region: one of us_gulf (US Gulf of Mexico/America), brazil, west_africa, mediterranean (incl. Black Sea, Egypt, Cyprus, Turkey), asia_pacific (SE Asia, Australia), india, norway_harsh (Norway/North Sea/Canada harsh environment), other (global or unspecified).
- demand_comments.topic: tender (tenders/awards), fid (project sanctions), rig_move (rigs relocating between regions), dayrate, supply (oversupply/undersupply), other.
- Arrays (contracted_rigs, utilization, demand_comments, gap_rigs): at most 6 entries each.
Output JSON only, no prose, exactly this shape:
{"market": {
   "contracted_rigs": [{"segment": "drillship|harsh_semi|floater_total", "region": "us_gulf|brazil|west_africa|mediterranean|asia_pacific|india|norway_harsh|other", "count": n|null, "direction": "up|flat|down|null", "quote": ""}],
   "utilization": [{"segment": "", "region": "", "value_pct": n|null, "quote": ""}],
   "dayrate_leading": {"low": n|null, "high": n|null, "unit": "USD K/day", "segment": "", "quote": ""},
   "contract_duration": {"value_days": n|null, "prior_days": n|null, "quote": ""},
   "supply": {"retirements": n|null, "reactivation_stance": "none|conditional|planned|done|null", "quote": ""},
   "outlook": [{"metric": "contracted_rigs|utilization|dayrate|duration|demand", "segment": "", "region": "", "horizon": "", "value": n|null, "direction": "up|flat|down", "quote": ""}],
   "demand_comments": [{"region": "us_gulf|brazil|west_africa|mediterranean|asia_pacific|india|norway_harsh|other", "direction": "up|flat|down", "topic": "tender|fid|rig_move|dayrate|supply|other", "quote": ""}]},
 "company": {
   "contracted_next_year": {"value_pct": n|null, "year": n|null, "quote": ""},
   "contracted_year_after": {"value_pct": n|null, "year": n|null, "quote": ""},
   "gap_rigs": [{"rig": "", "when": "", "quote": ""}],
   "legacy_repricing": {"status": "none|pending|partial|done|null", "effective": "", "quote": ""},
   "avg_dayrate": {"value": n|null, "unit": "USD/day", "quote": ""},
   "operating_days_change": {"direction": "up|flat|down|null", "reason": "", "quote": ""},
   "backlog": {"value": n|null, "unit": "USD B|USD M", "quote": ""},
   "backlog_added": {"value": n|null, "unit": "", "quote": ""},
   "guidance_change": {"direction": "raised|maintained|lowered|narrowed|null", "quote": ""},
   "om_cost_outlook": {"quote": ""},
   "revenue": {"value": n|null, "unit": "", "quote": ""},
   "fcf": {"value": n|null, "unit": "", "quote": ""},
   "buyback": {"value": n|null, "unit": "", "quote": ""}}}"""

_ARRAY_KEYS = {"market": ("contracted_rigs", "utilization", "demand_comments", "outlook"), "company": ("gap_rigs",)}
_USD_FIELDS = ("backlog", "backlog_added", "revenue", "fcf", "buyback")


# ---------- 공통 ----------
def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("tickers", [])
    cfg.setdefault("names", {})
    cfg.setdefault("quarters_back", 8)
    cfg.setdefault("merger_marks", {})
    return cfg


def _kpi_path(code, quarter):
    return os.path.join(KPI_DIR, code, f"{quarter}.json")


def _load(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _excerpts(transcript):
    """KPI 키워드 문단 ±1 문단 (earnings_calls._excerpts 공용 규칙: 세그먼트→빈 줄 문단→600자 묶음, 상한 넘으면 ±1→0, 그래도 넘으면 앞에서 자름).
    반환 (텍스트, 걸린 문단 수)"""
    return earnings_calls._excerpts(transcript, KPI_RE, 1, EXCERPT_MAX_CHARS)


def _quarter_end_date(label):
    y, m = int(label[:4]), int(label[5:7])
    return date(y, m, calendar.monthrange(y, m)[1])


def _release_text(code, quarter, label=None):
    """분기 말 이후 60일 안에 접수된 8-K EX-99.1 본문(.md) 앞 12,000자 + backlog/FCF/자사주/가이던스 문단. 없으면 ''"""
    import sec_filings
    idx = sec_filings._load_index(code) or {}
    if not label:
        q = next((x for x in earnings_calls.quarters(code, 12) if x["quarter"] == quarter), None)
        label = q["label"] if q else None
    if not label:
        return ""
    qend = _quarter_end_date(label)
    best = None
    for acc, ent in idx.items():
        try:
            d = date.fromisoformat((ent.get("date") or "")[:10])
        except ValueError:
            continue
        if not (qend < d <= qend + timedelta(days=RELEASE_WINDOW_DAYS)):
            continue
        ex = (ent.get("exhibits") or {}).get("EX-99.1")
        if not ex or not ex.get("md"):
            continue
        if best is None or d < best[0]:
            best = (d, ex["md"])
    if not best:
        return ""
    try:
        with open(os.path.join(_BD, best[1]), "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""
    head = text[:RELEASE_HEAD_CHARS]
    extra = [p.strip() for p in re.split(r"\n\s*\n", text[RELEASE_HEAD_CHARS:]) if p.strip() and RELEASE_RE.search(p)]
    return head + ("\n\n[... key paragraphs from the rest of the release ...]\n\n" + "\n\n".join(extra)[:8000] if extra else "")


def _usd(value, unit):
    """(값, 단위) → 달러 원값. earnings_calls._adj_amount 재사용 (USD K/M/B 처리), 단위 없으면 None"""
    return earnings_calls._adj_amount(value, unit)


def _num(v):
    try:
        return None if v is None or isinstance(v, bool) else float(v)
    except (TypeError, ValueError):
        return None


def _clean(data):
    """AI 응답 모양 정리: market/company dict 보장, 배열 최대 6개, 숫자 필드 숫자화"""
    out = {"market": {}, "company": {}}
    for sec in ("market", "company"):
        src = data.get(sec) if isinstance(data, dict) and isinstance(data.get(sec), dict) else {}
        for k, v in src.items():
            if k in _ARRAY_KEYS[sec]:
                out[sec][k] = [x for x in (v or []) if isinstance(x, dict)][:6] if isinstance(v, list) else []
            elif isinstance(v, dict):
                out[sec][k] = v
            elif v is None:
                out[sec][k] = {}
    comp = out["company"]
    for f in _USD_FIELDS:
        it = comp.get(f) or {}
        comp[f + "_usd"] = _usd(_num(it.get("value")), it.get("unit")) if isinstance(it, dict) else None
    return out


# ---------- 추출 ----------
def extract_kpis(code, quarter, force=False, limit=None):
    """분기 1건 KPI 추출 (한도 kind='drilling_kpi', Sonnet). 반환 {status: ok|cached|no_transcript|no_excerpt|ai_limit|ai_error, kpi}.
    결과는 cache/drilling/kpi/<code>/<quarter>.json 에 저장하고, ok 이면 force 없이는 다시 부르지 않는다."""
    import llm_client
    st, d = earnings_calls.transcript_state(code, quarter)
    if st != "ok":
        return {"status": "no_transcript"}
    cur = _load(_kpi_path(code, quarter))
    if cur and cur.get("status") == "ok" and not force:
        return {"status": "cached", "kpi": cur}
    text, n_hits = _excerpts(d["transcript"])
    if not text:
        return {"status": "no_excerpt"}
    qinfo = next((x for x in earnings_calls.quarters(code, 12) if x["quarter"] == quarter), None)
    label = qinfo["label"] if qinfo else None
    release = _release_text(code, quarter, label)
    allowed, n = ai_budget.take("drilling_kpi", limit=limit)
    if not allowed:
        return {"status": "ai_limit", "used": n}
    cfg = load_config()
    name = cfg["names"].get(code, code)
    user = (f"Company: {name} ({code})\nQuarter being discussed: {quarter} (fiscal; calendar quarter ended {label or '?'})\n\n"
            f"=== Earnings call excerpts ({n_hits} keyword paragraphs) ===\n{text}")
    if release:
        user += f"\n\n=== Earnings press release (8-K EX-99.1) ===\n{release}"
    t0 = time.time()
    r = llm_client.complete("drilling_kpi", user, system=KPI_SYSTEM, json_mode=True, max_tokens=3000)
    if not r.ok or not isinstance(r.data, dict):
        print(f"[drilling] {code} {quarter} KPI 추출 실패: {r.error}", flush=True)
        return {"status": f"ai_error:{(r.error or 'unknown')[:60]}"}
    kpi = _clean(r.data)
    adj = (d.get("adjusted") or {})
    kpi["company"]["adj_ebitda_usd"] = adj.get("adj_ebitda") if adj.get("status") == "ok" else None
    res = {"status": "ok", "code": code, "quarter": quarter, "label": label, "market": kpi["market"], "company": kpi["company"],
           "model": r.model, "tokens": {"input": r.input_tokens, "output": r.output_tokens}, "seconds": round(time.time() - t0, 1), "at": _now(),
           "source": {"transcript_chars": len(text), "transcript_hits": n_hits, "release_chars": len(release)}}
    _save(_kpi_path(code, quarter), res)
    c = res["company"]
    print(f"[drilling] {code} {quarter} KPI ok: 내년계약 {(c.get('contracted_next_year') or {}).get('value_pct')}% 백로그 {(c.get('backlog') or {}).get('value')} "
          f"{(c.get('backlog') or {}).get('unit')} FCF {(c.get('fcf') or {}).get('value')} (오늘 {n}/{ai_budget.resolve_limit('drilling_kpi', limit)}, {res['seconds']}s)", flush=True)
    return {"status": "ok", "kpi": res}


def extract_missing(code, max_n=None):
    """녹취록은 있는데 KPI 가 없는 분기만 오래된 순으로 추출 (05:30 루프·backfill). ai_limit 이면 중단. 반환 [{quarter, status}]"""
    cfg = load_config()
    out = []
    for q in earnings_calls.quarters(code, cfg["quarters_back"]):
        st, _ = earnings_calls.transcript_state(code, q["quarter"])
        if st != "ok":
            continue
        cur = _load(_kpi_path(code, q["quarter"]))
        if cur and cur.get("status") == "ok":
            continue
        r = extract_kpis(code, q["quarter"])
        out.append({"quarter": q["quarter"], "status": r.get("status")})
        if r.get("status") == "ai_limit":
            break
        if max_n and len(out) >= max_n:
            break
    return out


def backfill(force_quarters=None):
    """설정 티커의 최근 quarters_back 분기 녹취록 받기(요약 X) → KPI 추출. force_quarters: ["RIG:2026Q2", ...] 는 다시 받기.
    Alpha Vantage 한도(waiting)면 그 티커는 멈추고 다음 티커로."""
    cfg = load_config()
    force = set(force_quarters or [])
    summary = {}
    for code in cfg["tickers"]:
        counts = {"ok": 0, "none": 0, "waiting": 0, "error": 0}
        stopped = None
        for q in earnings_calls.quarters(code, cfg["quarters_back"]):
            key = f"{code}:{q['quarter']}"
            r = earnings_calls.fetch_transcript(code, q["quarter"], force=key in force)
            stt = r.get("status")
            counts[stt if stt in counts else "error"] += 1
            print(f"[drilling] {key} fetch {stt}{' (cached)' if r.get('cached') else ''}{' FORCE' if key in force else ''}"
                  + (f" — {r.get('reason')}" if stt in ("waiting", "error") else ""), flush=True)
            if stt == "waiting":
                stopped = q["quarter"]
                break
        ext = extract_missing(code)
        summary[code] = {"fetch": counts, "stopped_at": stopped, "extract": ext}
        print(f"[drilling] {code} fetch ok {counts['ok']} none {counts['none']} waiting {counts['waiting']}"
              + (f" (멈춤 {stopped})" if stopped else "") + " / extract " + (" ".join(f"{e['quarter']}:{e['status']}" for e in ext) or "없음"), flush=True)
    return summary


# ---------- 조회 ----------
def series(code):
    """저장된 KPI 를 분기 오름차순으로 평탄화 {code, quarters, items:[{quarter,label,market,company,at}], market{q:..}, company{q:..}}"""
    d = os.path.join(KPI_DIR, code)
    items = []
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if not re.match(r"^20\d\dQ[1-4]\.json$", fn):
                continue
            k = _load(os.path.join(d, fn))
            if k and k.get("status") == "ok":
                items.append({"quarter": k["quarter"], "label": k.get("label"), "market": k.get("market") or {}, "company": k.get("company") or {}, "at": k.get("at")})
    items.sort(key=lambda x: x["quarter"])
    return {"code": code, "quarters": [x["quarter"] for x in items], "items": items,
            "market": {x["quarter"]: x["market"] for x in items}, "company": {x["quarter"]: x["company"] for x in items}}


def _market_statements(code, quarter, market):
    """최신 분기 market 블록 → 발언 목록 [{code, quarter, item, value, quote}]"""
    out = []
    for it in market.get("contracted_rigs") or []:
        if it.get("quote"):
            out.append({"code": code, "quarter": quarter, "item": "contracted_rigs", "value": f"{it.get('segment') or ''}/{it.get('region') or ''} {it.get('count') if it.get('count') is not None else '-'} {it.get('direction') or ''}".strip(), "quote": it["quote"]})
    for it in market.get("utilization") or []:
        if it.get("quote"):
            out.append({"code": code, "quarter": quarter, "item": "utilization", "value": f"{it.get('segment') or ''}/{it.get('region') or ''} {it.get('value_pct') if it.get('value_pct') is not None else '-'}%", "quote": it["quote"]})
    dl = market.get("dayrate_leading") or {}
    if dl.get("quote"):
        out.append({"code": code, "quarter": quarter, "item": "dayrate_leading", "value": f"{dl.get('low')}~{dl.get('high')} {dl.get('unit') or 'USD K/day'} {dl.get('segment') or ''}".strip(), "quote": dl["quote"]})
    cd = market.get("contract_duration") or {}
    if cd.get("quote"):
        out.append({"code": code, "quarter": quarter, "item": "contract_duration", "value": f"{cd.get('prior_days')}→{cd.get('value_days')}일", "quote": cd["quote"]})
    sp = market.get("supply") or {}
    if sp.get("quote"):
        out.append({"code": code, "quarter": quarter, "item": "supply", "value": f"퇴역 {sp.get('retirements') if sp.get('retirements') is not None else '-'} · 재가동 {sp.get('reactivation_stance') or '-'}", "quote": sp["quote"]})
    for it in market.get("demand_comments") or []:
        if it.get("quote"):
            out.append({"code": code, "quarter": quarter, "item": "demand", "value": f"{it.get('region') or ''} {it.get('direction') or ''}".strip(), "quote": it["quote"]})
    return out


# ---------- 집계 계층 (A2): 화면 로직을 서버로 ----------
REGIONS = [("us_gulf", "미국 걸프"), ("brazil", "브라질"), ("west_africa", "서아프리카"), ("mediterranean", "지중해"), ("asia_pacific", "아시아·호주"),
           ("india", "인도"), ("norway_harsh", "노르웨이·한랭지"), ("other", "기타")]
_REGION_RULES = [("us_gulf", r"gulf|gom|mexico|america\b"), ("brazil", r"brazil|petrobras"), ("west_africa", r"africa|angola|namibia|nigeria|ghana|ivory"),
                 ("mediterranean", r"mediterranean|cyprus|egypt|turkey|black sea"), ("asia_pacific", r"asia|indonesia|malaysia|australia|vietnam|thailand|pacific"),
                 ("india", r"india|ongc"), ("norway_harsh", r"norway|harsh|north sea|canada|barents")]
SIGNAL_DEFS = [("contracted_rigs", "계약 리그 수", "척", 2), ("utilization", "가동률", "%", 2), ("dayrate_leading", "체결 단가", "$K/일", 15),
               ("contract_duration", "계약 기간", "일", 30), ("supply", "공급", "", None)]
DIRECTION_KO = {"up": "상승", "down": "하락", "flat": "보합", "none": "-", "mixed": "혼재"}


def _norm_region(text):
    t = str(text or "").lower()
    for key, rx in _REGION_RULES:
        if re.search(rx, t):
            return key
    return "other"


def _median(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def _majority(dirs):
    """발언 방향 다수결: up/down/flat 중 최다, 동률이면 mixed, 없으면 none"""
    c = {}
    for d in dirs:
        if d in ("up", "down", "flat"):
            c[d] = c.get(d, 0) + 1
    if not c:
        return "none"
    top = max(c.values())
    winners = [k for k, v in c.items() if v == top]
    return winners[0] if len(winners) == 1 else "mixed"


SEGMENTS = [("drillship", "드릴십"), ("harsh", "한랭지")]


def _seg(text):
    """세그먼트 분류: harsh_semi|harsh → harsh, 그 외(drillship|floater_total|미지정) → drillship"""
    return "harsh" if re.search(r"harsh", str(text or "").lower()) else "drillship"


def _qidx(q):
    m = re.match(r"^(20\d\d)Q([1-4])$", str(q or ""))
    return int(m.group(1)) * 4 + int(m.group(2)) if m else None


def _signal_values(key, market, seg=None):
    """분기 market 블록 → (수치 또는 None, 발언 방향 목록, 근거 항목 목록). seg 가 있으면 그 세그먼트 항목만"""
    def keep(x):
        return seg is None or _seg(x.get("segment")) == seg
    if key == "contracted_rigs":
        items = [x for x in market.get("contracted_rigs") or [] if x.get("quote") and keep(x)]
        # 수치는 업계 전체(global/worldwide/other) 발언만 — 지역별 척수를 합치면 뜻이 달라지므로 지역 발언은 방향·근거로만 쓴다
        glob = [x for x in items if re.search(r"global|world|industry|total|other", str(x.get("region") or "other").lower())]
        nums = [x.get("count") for x in glob if isinstance(x.get("count"), (int, float))]
        return _median(nums), [x.get("direction") for x in items], items
    if key == "utilization":
        items = [x for x in market.get("utilization") or [] if x.get("quote") and keep(x)]
        nums = [x.get("value_pct") for x in items if isinstance(x.get("value_pct"), (int, float))]
        return _median(nums), [], items
    if key == "dayrate_leading":
        d = market.get("dayrate_leading") or {}
        if not d.get("quote") or not keep(d):
            return None, [], []
        lo, hi = d.get("low"), d.get("high")
        v = _median([x for x in (lo, hi) if isinstance(x, (int, float))])
        return v, [], [d]
    if key == "contract_duration":
        d = market.get("contract_duration") or {}
        if not d.get("quote") or not keep(d):
            return None, [], []
        v = d.get("value_days") if isinstance(d.get("value_days"), (int, float)) else None
        dirs = []
        if v is not None and isinstance(d.get("prior_days"), (int, float)):
            dirs = ["up" if v > d["prior_days"] else "down" if v < d["prior_days"] else "flat"]
        return v, dirs, [d]
    if key == "supply":
        d = market.get("supply") or {}
        if not d.get("quote"):
            return None, [], []
        ret = d.get("retirements") if isinstance(d.get("retirements"), (int, float)) else 0
        stance = d.get("reactivation_stance")
        # 긴축(up) = 퇴역>0 또는 재가동 none/conditional, 완화(down) = planned/done
        tone = "up" if (ret > 0 or stance in ("none", "conditional")) else "down" if stance in ("planned", "done") else "none"
        return ret if ret else None, [tone] if tone != "none" else [], [dict(d, stance=stance)]
    return None, [], []


def _fmt_signal(key, v):
    if v is None:
        return None
    if key == "utilization":
        return f"{v:g}%"
    if key == "dayrate_leading":
        return f"${v:g}K"
    if key == "contract_duration":
        return f"{v:g}일"
    if key == "contracted_rigs":
        return f"{v:g}척"
    if key == "supply":
        return f"퇴역 {v:g}"
    return f"{v:g}"


def signals(series_by_code, latest_quarter=None):
    """지표 5개. contracted_rigs·utilization·dayrate_leading·contract_duration 은 {"drillship": {...}|None, "harsh": {...}|None}, supply 는 세그먼트 없이 {...}.
    각 블록 = {latest, prev, delta, direction, n_sources, history}. 같은 분기 여러 회사 값은 중앙값. 방향: 임계값 → 발언 다수결 → none.
    latest.quarter 가 기준 분기보다 2분기 이상 앞서면 direction="stale", stale_quarter 에 그 분기."""
    quarters = sorted({q for s in series_by_code.values() for q in s["quarters"]})
    ref = _qidx(latest_quarter or (quarters[-1] if quarters else None))
    out = {}
    for key, label, unit, thr in SIGNAL_DEFS:
        if key == "supply":
            out[key] = _signal_block(series_by_code, quarters, key, label, unit, thr, None, ref)
        else:
            out[key] = {seg: _signal_block(series_by_code, quarters, key, label, unit, thr, seg, ref) for seg, _ in SEGMENTS}
    return out


def _months_back(month, k):
    y, m = int(month[:4]), int(month[5:7]) - k
    while m <= 0:
        m += 12
        y -= 1
    return f"{y}-{m:02d}"


def _quarter_end_iso(q):
    """'2026Q3' → 달력 분기 말 ISO 날짜 (결산 12월 가정: 시추 3사 모두 12월)"""
    m = re.match(r"^(20\d\d)Q([1-4])$", str(q or ""))
    if not m:
        return None
    y, qn = int(m.group(1)), int(m.group(2))
    mth = qn * 3
    return date(y, mth, calendar.monthrange(y, mth)[1]).isoformat()


STALE_DAYS = 120


def _apply_daily(out):
    """B4: drilling_daily 산출물(시장 수치·체결 단가·FSR 커버리지·이벤트)을 overview 에 합친다. 실패해도 overview 는 유지."""
    try:
        import drilling_daily as DD
    except Exception as e:
        out["daily_error"] = f"import:{type(e).__name__}"
        return out
    today = date.today()

    def stale_by_date(as_of):
        try:
            return (today - date.fromisoformat(str(as_of)[:10])).days >= STALE_DAYS
        except (TypeError, ValueError):
            return False
    sig = out.get("signals") or {}
    # (a) 모든 블록: 날짜 기준 stale 재판정 (분기 기준 대신)
    for key, v in sig.items():
        blocks = [v] if key == "supply" else [b for b in (v or {}).values() if b]
        for b in blocks:
            if not b or not b.get("latest"):
                continue
            as_of = b["latest"].get("as_of")
            if stale_by_date(as_of):
                b["direction"], b["direction_ko"], b["stale_quarter"] = "stale", "오래됨", b["latest"]["quarter"]
                b["stale_as_of"] = as_of
            elif b.get("direction") == "stale":   # 분기 기준으로는 오래됐지만 날짜로는 아님 → 되살림(방향 none)
                b["direction"], b["direction_ko"], b["stale_quarter"] = "none", "-", None
    # (b) 계약 리그 수·가동률: market_stats 1순위
    try:
        ms = DD.market_series()
    except Exception as e:
        ms, out["daily_error"] = {}, f"market_series:{type(e).__name__}"
    seg_map = {"drillship": ("drillship", "floater_total"), "harsh": ("harsh",)}
    for key, metric, thr, fmt in (("contracted_rigs", "contracted_rigs", 2, lambda v: f"{v:g}척"), ("utilization", "utilization", 2, lambda v: f"{v:g}%")):
        for seg, segs in seg_map.items():
            pts = []
            for sg in segs:
                pts += (ms.get(metric) or {}).get(sg) or []
            pts = [p for p in pts if p.get("value") is not None and (p.get("region") in (None, "", "other"))]   # 업계 전체(global)만 — 지역 수치는 섞지 않음
            if not pts:
                continue
            pts.sort(key=lambda p: p["as_of"])
            by_day = {}
            for p in pts:
                by_day.setdefault(p["as_of"], []).append(p)
            days = sorted(by_day)
            def pack(d):
                ps = by_day[d]
                v = _median([p["value"] for p in ps])
                return {"quarter": d[:7], "as_of": d, "value": v, "value_text": fmt(v), "source_name": " · ".join(sorted({p.get("source_name") or "?" for p in ps})),
                        "sources": [{"code": None, "value": fmt(p["value"]), "quote": p.get("quote"), "detail": {"source": p.get("source_name"), "region": p.get("region"), "unit": p.get("unit"), "url": p.get("url")}} for p in ps], "dirs": []}
            latest = pack(days[-1])
            prev = pack(days[-2]) if len(days) >= 2 else None
            delta = (latest["value"] - prev["value"]) if prev else None
            direction = "stale" if stale_by_date(latest["as_of"]) else ("up" if delta is not None and delta > thr else "down" if delta is not None and delta < -thr else "flat" if delta is not None else "none")
            old = (sig.get(key) or {}).get(seg)
            sig.setdefault(key, {})[seg] = {"label": old["label"] if old else dict((k, l) for k, l, _, _ in SIGNAL_DEFS)[key], "unit": old["unit"] if old else "", "segment": seg, "segment_ko": dict(SEGMENTS)[seg],
                                           "latest": latest, "prev": prev, "delta": delta, "direction": direction, "direction_ko": "오래됨" if direction == "stale" else DIRECTION_KO.get(direction, "-"),
                                           "stale_quarter": latest["quarter"] if direction == "stale" else None, "stale_as_of": latest["as_of"] if direction == "stale" else None,
                                           "n_sources": len(latest["sources"]), "statement_quarter": latest["quarter"], "primary": "market_stats",
                                           "history": [{"quarter": d[:7], "as_of": d, "value": pack(d)["value"], "by_code": {}} for d in days],
                                           "call_fallback": old}
    # (c) 체결 단가: leading_dayrates 월별 중앙값 1순위
    try:
        ld = DD.leading_dayrates()
    except Exception as e:
        ld, out["daily_error"] = {}, f"leading_dayrates:{type(e).__name__}"
    for seg in ("drillship", "harsh"):
        months = [m for m in ((ld.get(seg) or {}).get("months") or []) if m.get("rolling3") is not None]
        sm = (ld.get(seg) or {}).get("summary") or {}
        if not months or sm.get("recent") is None:
            continue
        # 신호 = summary(최근 3개월 창 vs 직전 3개월 창), 임계 ±5%, 양쪽 n>=5 (G). 월 rolling3 는 차트(history)용으로만
        last = months[-1]
        pts_recent = [p for m in months if m["month"] >= _months_back(last["month"], 2) for p in m["points"]]
        latest = {"quarter": sm.get("recent_month") or last["month"], "as_of": (sm.get("recent_month") or last["month"]) + "-01", "value": sm["recent"], "value_text": f"${sm['recent']:g}K",
                  "n": sm.get("n") or 0, "window": "최근 3개월", "source_name": f"체결 {sm.get('n') or 0}건/최근 3개월(뉴스·FSR)",
                  "sources": [{"code": None, "value": f"${p['value']:g}K", "quote": f"{p.get('rig') or ''} / {p.get('customer') or ''} ({p.get('source') or ''})", "detail": {"url": p.get("url"), "date": p.get("date")}} for p in pts_recent], "dirs": []}
        prev = None
        if sm.get("prev") is not None:
            prev = {"quarter": _months_back(last["month"], 3), "as_of": _months_back(last["month"], 3) + "-01", "value": sm["prev"], "value_text": f"${sm['prev']:g}K", "n": sm.get("prev_n") or 0,
                    "window": "직전 3개월", "source_name": f"체결 {sm.get('prev_n') or 0}건/직전 3개월", "sources": [], "dirs": []}
        chg = sm.get("chg_pct")
        delta = (latest["value"] - prev["value"]) if prev else None
        enough = latest["n"] >= 5 and prev is not None and prev["n"] >= 5
        direction = ("stale" if stale_by_date(latest["as_of"]) else "insufficient" if not enough
                     else ("up" if chg > 5 else "down" if chg < -5 else "flat"))
        old = (sig.get("dayrate_leading") or {}).get(seg)
        sig.setdefault("dayrate_leading", {})[seg] = {"label": "체결 단가", "unit": "$K/일", "segment": seg, "segment_ko": dict(SEGMENTS)[seg], "latest": latest, "prev": prev, "delta": delta, "chg_pct": chg,
                                                     "direction": direction,
                                                     "direction_ko": "오래됨" if direction == "stale" else (f"표본 부족(최근 {latest['n']}건, 직전 {(prev or {}).get('n', 0)}건)" if direction == "insufficient" else DIRECTION_KO.get(direction, "-")),
                                                     "stale_quarter": latest["quarter"] if direction == "stale" else None, "n_sources": latest["n"], "statement_quarter": latest["quarter"],
                                                     "primary": "dayrates", "history": [{"quarter": m["month"], "as_of": m["month"] + "-01", "value": m["rolling3"], "month_median": m["median"], "n": m.get("rolling3_n"), "by_code": {}} for m in months], "call_fallback": old}
    for p in (ld.get("recent") or []):
        sg = "harsh" if p.get("rig_type") == "harsh" else "drillship"
        mon = (p.get("date") or "")[:7]
        row = next((m for m in ((ld.get(sg) or {}).get("months") or []) if m["month"] == mon), None)
        ref = (row or {}).get("rolling3")
        p["vs_median_pct"] = round((p["value"] / ref - 1) * 100, 1) if ref else None
    out["dayrates"] = ld
    out["market_series"] = ms
    # (b2) 시장 수치 카드: 세그먼트별 최신값과 지난주·4주 전·전 분기 대비 (global 시계열에서 가장 가까운 과거 값)
    def nearest_before(pts, target):
        cand = [p for p in pts if p["as_of"] <= target]
        return cand[-1] if cand else None
    cards = {}
    for metric in ("contracted_rigs", "utilization"):
        cards[metric] = {}
        for seg in ("floater_total", "drillship", "harsh", "jackup"):
            pts = sorted([p for p in ((ms.get(metric) or {}).get(seg) or []) if p.get("value") is not None and (p.get("region") in (None, "", "other"))], key=lambda p: p["as_of"])
            if not pts:
                continue
            last = pts[-1]
            ld_ = date.fromisoformat(last["as_of"][:10])
            def delta(days):
                ref = nearest_before(pts[:-1], (ld_ - timedelta(days=days)).isoformat())
                return {"delta": round(last["value"] - ref["value"], 1), "ref_as_of": ref["as_of"]} if ref else None
            cards[metric][seg] = {"value": last["value"], "as_of": last["as_of"], "unit": last.get("unit"), "source_name": last.get("source_name"), "url": last.get("url"),
                                  "week": delta(7), "w4": delta(28), "quarter": delta(91), "n": len(pts)}
    out["market_cards"] = cards
    # (d) FSR 커버리지·공백 리그 → company
    yrs = (today.year + 1, today.year + 2)
    out["fsr"] = {}
    for code in out["tickers"]:
        cov = {}
        for y in yrs:
            try:
                cov[str(y)] = DD.coverage_from_fsr(code, y)
            except Exception as e:
                cov[str(y)] = {"error": type(e).__name__}
        rep = None
        try:
            rep = DD.latest_fsr(code)
        except Exception:
            pass
        out["fsr"][code] = {"report_date": rep["report_date"], "url": rep.get("url"), "fleet_count": rep.get("fleet_count"), "backlog_usd_b": rep.get("backlog_usd_b"),
                            "avg_dayrate_table": rep.get("avg_dayrate_table") or [], "rigs_n": len(rep.get("rigs") or [])} if rep else None
        cd = (out.get("company_deltas") or {}).get(code)
        c1 = cov.get(str(yrs[0])) or {}
        if cd is not None:
            cd["coverage_fsr"] = cov
            cd["gap_rigs_fsr"] = c1.get("gap_rigs") or []
            cd["metrics"]["coverage_fsr_next"] = {"latest": c1.get("firm_pct"), "with_options": c1.get("with_options_pct"), "prev": None, "delta": None}
            cd["metrics"]["coverage_fsr_after"] = {"latest": (cov.get(str(yrs[1])) or {}).get("firm_pct"), "with_options": (cov.get(str(yrs[1])) or {}).get("with_options_pct"), "prev": None, "delta": None}
        else:
            out.setdefault("company_deltas", {})[code] = {"quarter": None, "prev_quarter": None, "metrics": {}, "coverage_fsr": cov, "gap_rigs_fsr": c1.get("gap_rigs") or []}
    # (e) 이벤트 (최근 60일) + 지역 매트릭스 셀에 분기별 계약 건수
    try:
        evs = DD.recent_events(60)
    except Exception as e:
        evs, out["daily_error"] = [], f"events:{type(e).__name__}"
    out["events"] = evs
    rm = out.get("region_matrix") or {}
    try:
        all_evs = DD._events()["events"]
    except Exception:
        all_evs = []
    for rkey, row in (rm.get("rows") or {}).items():
        for q, cell in (row.get("cells") or {}).items():
            qi = _qidx(q)
            n = 0
            for e in all_evs:
                if e.get("region") != rkey or e.get("type") not in ("award", "extension", "option"):
                    continue
                d = str(e.get("start") or "")
                if not re.match(r"^20\d\d-\d\d", d):
                    d = e.get("date") or ""
                m = re.match(r"^(20\d\d)-(\d\d)", d)
                if m and (int(m.group(1)) * 4 + (int(m.group(2)) - 1) // 3 + 1) == qi:
                    n += 1
            cell["events_n"] = n
    # (f) 헤드라인 덧붙임: 최근 30일 계약 n건 · 단가 중앙값
    since30 = (today - timedelta(days=30)).isoformat()
    recent = [e for e in all_evs if (e.get("date") or "") >= since30 and e.get("type") in ("award", "extension", "option")]
    rates = [e["dayrate_usd_k"] for e in recent if e.get("dayrate_usd_k") is not None]
    out["recent30"] = {"contracts": len(recent), "dayrate_median_k": _median(rates)}
    return out


def _signal_block(series_by_code, quarters, key, label, unit, thr, seg, ref):
    if True:
        per_q = {}   # quarter → {"nums": [], "dirs": [], "sources": []}
        for code, s in series_by_code.items():
            for q, m in s["market"].items():
                v, dirs, items = _signal_values(key, m or {}, seg)
                if v is None and not items:
                    continue
                slot = per_q.setdefault(q, {"nums": [], "dirs": [], "sources": []})
                if v is not None:
                    slot["nums"].append(v)
                slot["dirs"].extend(d for d in dirs if d)
                for it in items:
                    slot["sources"].append({"code": code, "value": _fmt_signal(key, v) if len(items) == 1 else None, "quote": it.get("quote"),
                                            "detail": {k: it.get(k) for k in ("segment", "region", "count", "value_pct", "low", "high", "value_days", "prior_days", "retirements", "stance", "direction") if it.get(k) is not None}})
        qs = [q for q in quarters if q in per_q]
        def pack(q):
            if not q:
                return None
            slot = per_q[q]
            v = _median(slot["nums"])
            return {"quarter": q, "value": v, "value_text": _fmt_signal(key, v), "sources": slot["sources"], "dirs": slot["dirs"]}
        qs_num = [q for q in qs if per_q[q]["nums"]]          # 수치가 있는 분기만 (지역 발언만 있는 분기는 근거로만)
        if key != "supply" and len(qs_num) >= 2:
            latest, prev = pack(qs_num[-1]), pack(qs_num[-2])
        else:
            latest = pack(qs[-1]) if qs else None
            prev = pack(qs[-2]) if len(qs) >= 2 else None
        delta, direction = None, "none"
        if key == "supply":
            direction = _majority(latest["dirs"]) if latest else "none"
        elif latest and prev and latest["value"] is not None and prev["value"] is not None:
            delta = latest["value"] - prev["value"]
            direction = "up" if delta > thr else "down" if delta < -thr else "flat"
        elif latest:
            direction = _majority(latest["dirs"])
            if direction == "mixed":
                direction = "flat"
        if not latest:
            return None
        stale_q = None
        if ref is not None and _qidx(latest["quarter"]) is not None and ref - _qidx(latest["quarter"]) >= 2:
            direction, stale_q = "stale", latest["quarter"]
        for blk in (latest, prev):
            if blk:
                blk["as_of"] = _quarter_end_iso(blk["quarter"])
                blk["source_name"] = "컨콜 발언"
        return {"label": label, "unit": unit, "segment": seg, "segment_ko": dict(SEGMENTS).get(seg), "latest": latest, "prev": prev, "delta": delta, "direction": direction,
                "direction_ko": "오래됨" if direction == "stale" else (("긴축" if direction == "up" else "완화" if direction == "down" else "-") if key == "supply" else DIRECTION_KO.get(direction, "-")),
                "stale_quarter": stale_q, "n_sources": len(latest["sources"]), "statement_quarter": qs[-1] if qs else None,
                "history": [{"quarter": q, "value": pack(q)["value"], "by_code": {src["code"]: True for src in per_q[q]["sources"]}} for q in qs]}


def region_matrix(series_by_code, n_quarters=4):
    """최근 n분기 × 8지역. 셀 {direction(다수결, 상충 mixed), comments[{code,direction,topic,quote}]}, 지역별 trend, summary {up,down,mixed}"""
    quarters = sorted({q for s in series_by_code.values() for q in s["quarters"]})[-n_quarters:]
    rows = {}
    for rkey, rlabel in REGIONS:
        cells = {}
        for q in quarters:
            comments = []
            for code, s in series_by_code.items():
                m = s["market"].get(q) or {}
                for x in m.get("demand_comments") or []:
                    if x.get("quote") and _norm_region(x.get("region")) == rkey:
                        comments.append({"code": code, "direction": x.get("direction"), "topic": x.get("topic") or "other", "quote": x["quote"]})
            cells[q] = {"direction": _majority([c["direction"] for c in comments]) if comments else None, "comments": comments}
        recent = [cells[q]["direction"] for q in quarters[-2:] if cells[q]["direction"]]
        if len(recent) == 2 and recent[0] == recent[1] and recent[0] in ("up", "down"):
            trend = recent[0]
        elif not recent:
            trend = "none"
        elif len(recent) == 1:
            trend = recent[0] if recent[0] in ("up", "down", "flat") else "mixed"
        else:
            trend = "mixed" if "mixed" in recent or {"up", "down"} <= set(recent) else "flat"
        rows[rkey] = {"label": rlabel, "cells": cells, "trend": trend}
    summary = {"up": sum(1 for r in rows.values() if r["trend"] == "up"), "down": sum(1 for r in rows.values() if r["trend"] == "down"),
               "mixed": sum(1 for r in rows.values() if r["trend"] == "mixed"), "flat": sum(1 for r in rows.values() if r["trend"] == "flat")}
    return {"quarters": quarters, "rows": rows, "summary": summary}


def company_deltas(series):
    """회사 1곳: 최신 분기 vs 직전 분기 값·차이 (coverage_next pp, backlog_usd, fcf_usd, buyback_usd, avg_dayrate, gap_rigs 수)"""
    items = series["items"]
    if not items:
        return None
    cur, prev = items[-1], (items[-2] if len(items) >= 2 else None)

    def pick(co, key):
        if key == "coverage_next":
            v = (co.get("contracted_next_year") or {}).get("value_pct")
        elif key == "coverage_after":
            v = (co.get("contracted_year_after") or {}).get("value_pct")
        elif key == "avg_dayrate":
            v = (co.get("avg_dayrate") or {}).get("value")
        elif key == "gap_rigs":
            v = len(co.get("gap_rigs") or [])
        else:
            v = co.get(key)
        return v if isinstance(v, (int, float)) else None
    out = {"quarter": cur["quarter"], "prev_quarter": prev["quarter"] if prev else None, "metrics": {}}
    for key in ("coverage_next", "coverage_after", "backlog_usd", "fcf_usd", "buyback_usd", "avg_dayrate", "adj_ebitda_usd", "gap_rigs"):
        a, b = pick(cur["company"], key), (pick(prev["company"], key) if prev else None)
        out["metrics"][key] = {"latest": a, "prev": b, "delta": (a - b) if a is not None and b is not None else None}
    return out


def _parse_when(text, ref):
    """gap_rigs.when 텍스트 → 대략의 날짜 (YYYY, 'Q1 2027', 'January 2027', 'late 2027', 'mid-2027', 'early 2027'). 실패 시 None"""
    t = str(text or "").lower()
    months = {m.lower(): i + 1 for i, m in enumerate(calendar.month_name) if m}
    months.update({m.lower(): i + 1 for i, m in enumerate(calendar.month_abbr) if m})
    ordinal = {"first": 1, "second": 2, "third": 3, "fourth": 4, "1st": 1, "2nd": 2, "3rd": 3, "4th": 4}
    # '1q27' / 'q1 27' / 'q1-27' 처럼 두 자리 연도가 붙은 분기 표기
    m = re.search(r"\b(?:q([1-4])|([1-4])q)[\s'\-]?(\d{2})\b", t)
    y = re.search(r"(20\d\d)", t)
    if m and not y:
        q = int(m.group(1) or m.group(2))
        return date(2000 + int(m.group(3)), q * 3, 1)
    if not y:
        return None
    yr = int(y.group(1))
    m = re.search(r"\b(q[1-4]|[1-4]q)\b", t) or re.search(r"\b(first|second|third|fourth|1st|2nd|3rd|4th)\s+quarter\b", t)
    if m:
        g = m.group(1)
        q = ordinal.get(g) or int(re.sub(r"\D", "", g))
        return date(yr, q * 3, 1)
    for name, num in months.items():
        if re.search(r"\b" + re.escape(name) + r"\b", t):
            return date(yr, num, 1)
    if "early" in t or "first half" in t or "1h" in t:
        return date(yr, 2, 1)
    if "mid" in t:
        return date(yr, 6, 1)
    if "late" in t or "second half" in t or "2h" in t or "end of" in t:
        return date(yr, 10, 1)
    return date(yr, 1, 1)


def upcoming(series_by_code, names, window_days=180):
    """180일 창: 회사별 gap_rigs(when 이 최근 분기 이후 또는 해석 불가) + earnings_calls.earnings_dates 의 다음 발표일"""
    today = date.today()
    horizon = today + timedelta(days=window_days)
    gaps, idle, dates = [], [], []
    idle_rx = re.compile(r"currently idle|\bidle\b|available|warm.?stack|cold.?stack", re.I)
    for code, s in series_by_code.items():
        if not s["items"]:
            continue
        cur = s["items"][-1]
        qend = _quarter_end_date(cur["label"]) if cur.get("label") else today
        for g in cur["company"].get("gap_rigs") or []:
            when = g.get("when") or ""
            d = _parse_when(when, qend)
            row = {"code": code, "rig": g.get("rig") or "?", "when": when, "when_date": d.isoformat() if d else None, "quote": g.get("quote") or "", "quarter": cur["quarter"]}
            if idle_rx.search(when) and (d is None or d <= today):
                idle.append(row)                       # 현재 유휴/가용 리그 (시점이 과거이거나 없음)
                continue
            if d is not None and (d < max(qend, today) or d > horizon):
                continue
            gaps.append(row)
        try:
            all_d = sorted(earnings_calls.earnings_dates(code))
            nxt = [d for d in all_d if today <= d <= horizon]
            if nxt:
                dates.append({"code": code, "name": names.get(code, code), "date": nxt[0].isoformat(), "estimated": False})
            else:
                past = [d for d in all_d if d < today]
                if past:
                    est = past[-1] + timedelta(days=91)
                    if est <= horizon:
                        dates.append({"code": code, "name": names.get(code, code), "date": est.isoformat(), "estimated": True, "basis": f"마지막 발표 {past[-1].isoformat()} + 91일"})
        except Exception:
            pass
    gaps.sort(key=lambda g: (g["when_date"] or "9999", g["code"]))
    idle.sort(key=lambda g: (g["code"], g["rig"]))
    dates.sort(key=lambda d: d["date"])
    return {"window_days": window_days, "gap_rigs": gaps, "idle_rigs": idle, "earnings_dates": dates[:3]}


def cycle_checklist(sig, cds, rm, benchmark=None):
    """상승 사이클 조건 5개. 각 {name, on: True|False|None, value_text, basis}. stale·insufficient 는 None(판단 불가)."""
    def blk(key, seg="drillship"):
        v = sig.get(key)
        return v if key == "supply" else (v or {}).get(seg)
    items = []
    # ① 계약 리그 수 증가
    b = blk("contracted_rigs")
    if not b or b["direction"] in ("stale", "insufficient", "none"):
        items.append({"name": "계약 리그 수 증가", "on": None, "value_text": (b or {}).get("direction_ko") or "자료 없음", "basis": "드릴십, 최신 vs 직전"})
    else:
        items.append({"name": "계약 리그 수 증가", "on": b["direction"] == "up", "value_text": f"{(b['prev'] or {}).get('value_text') or '-'} → {b['latest']['value_text']}", "basis": f"{b['latest'].get('source_name') or ''} {b['latest'].get('as_of') or ''}".strip()})
    # ② 체결 단가 상승
    b = blk("dayrate_leading")
    if not b or b["direction"] in ("stale", "insufficient", "none"):
        items.append({"name": "체결 단가 상승", "on": None, "value_text": (b or {}).get("direction_ko") or "자료 없음", "basis": "최근 3개월 창 vs 직전 3개월 창 · 임계 ±5%"})
    else:
        items.append({"name": "체결 단가 상승", "on": b["direction"] == "up", "value_text": f"{(b['prev'] or {}).get('value_text') or '-'} → {b['latest']['value_text']}" + (f" ({b['chg_pct']:+.1f}%)" if b.get("chg_pct") is not None else ""), "basis": f"최근 3개월 {b['latest'].get('n')}건 vs 직전 3개월 {(b['prev'] or {}).get('n')}건 · 임계 ±5%"})
    # ③ 공급 긴축
    b = blk("supply")
    if not b or b["direction"] in ("stale", "none"):
        items.append({"name": "공급 긴축", "on": None, "value_text": (b or {}).get("direction_ko") or "자료 없음", "basis": "재가동 none·conditional 또는 퇴역>0"})
    else:
        src = (b["latest"]["sources"] or [{}])[0].get("detail") or {}
        items.append({"name": "공급 긴축", "on": b["direction"] == "up", "value_text": b["direction_ko"], "basis": f"재가동 {src.get('stance') or '-'} · 퇴역 {src.get('retirements') if src.get('retirements') is not None else '-'} ({b['latest']['quarter']})"})
    # ④ 수요 확산
    sm = (rm or {}).get("summary") or {}
    if not rm:
        items.append({"name": "수요 확산", "on": None, "value_text": "자료 없음", "basis": "up 지역 ≥ down 지역 + 2"})
    else:
        items.append({"name": "수요 확산", "on": sm.get("up", 0) >= sm.get("down", 0) + 2, "value_text": f"확대 {sm.get('up', 0)} · 약세 {sm.get('down', 0)} · 혼재 {sm.get('mixed', 0)}", "basis": "지역 매트릭스 최근 2분기 추세"})
    # ⑤ 회사 커버리지 개선 (3사 중 2사 이상: 내년 커버리지 Δ ≥ 0 또는 FSR 내년 커버리지 ≥ 80%)
    good, known, bits = 0, 0, []
    for code, cd in (cds or {}).items():
        m = (cd or {}).get("metrics") or {}
        d = (m.get("coverage_next") or {}).get("delta")
        f = (m.get("coverage_fsr_next") or {}).get("latest")
        ok = (d is not None and d >= 0) or (f is not None and f >= 80)
        if d is None and f is None:
            continue
        known += 1
        good += ok
        bits.append(f"{code} {'Δ' + format(d, '+.0f') + 'pp' if d is not None else ''}{' FSR ' + format(f, '.0f') + '%' if f is not None else ''}".strip())
    items.append({"name": "회사 커버리지 개선", "on": (good >= 2) if known else None, "value_text": f"{good}/{known}사" if known else "자료 없음", "basis": " · ".join(bits) or "내년 커버리지 Δ≥0 또는 FSR ≥80%"})
    return {"items": items, "on": sum(1 for i in items if i["on"] is True), "off": sum(1 for i in items if i["on"] is False), "unknown": sum(1 for i in items if i["on"] is None), "of": len(items)}


def headline(sig, rm):
    """템플릿 한 문장: '상승 n · 보합 n · 하락 n — <상승 지표>, <상승 지역 수>개 지역 수요 확대, <하락 지역명> 약세 (한랭지: …) (오래된 값 n개 제외)'.
    세그먼트가 둘인 지표는 드릴십 기준으로 집계, 한랭지는 괄호로 덧붙임. stale 은 집계에서 제외."""
    up, down, flat, stale_n, insuff_n, harsh_notes = [], [], [], 0, 0, []
    for key, label, _u, _t in SIGNAL_DEFS:
        v = sig.get(key)
        if key == "supply":
            blk, hblk = v, None
        else:
            blk, hblk = (v or {}).get("drillship"), (v or {}).get("harsh")
        if hblk:
            if hblk["direction"] == "stale":
                stale_n += 1
            elif hblk["direction"] in ("up", "down"):
                harsh_notes.append(f"{label} {DIRECTION_KO[hblk['direction']]}")
        if not blk:
            continue
        d = blk["direction"]
        if d == "stale":
            stale_n += 1
        elif d == "insufficient":
            insuff_n += 1
        elif d == "up":
            up.append(label + ("(긴축)" if key == "supply" else ""))
        elif d == "down":
            down.append(label + ("(완화)" if key == "supply" else ""))
        elif d == "flat":
            flat.append(label)
    parts = []
    if up:
        parts.append(", ".join(up) + " 상승")
    if down:
        parts.append(", ".join(down) + " 하락")
    sm = rm["summary"]
    if sm["up"]:
        parts.append(f"{sm['up']}개 지역 수요 확대")
    weak = [r["label"] for r in rm["rows"].values() if r["trend"] == "down"]
    if weak:
        parts.append(", ".join(weak) + " 약세")
    head = f"상승 {len(up)} · 보합 {len(flat)} · 하락 {len(down)}"
    text = head + (" — " + ", ".join(parts) if parts else " — 방향 신호 없음")
    if harsh_notes:
        text += f" (한랭지: {', '.join(harsh_notes)})"
    if stale_n or insuff_n:
        text += " (" + " · ".join(x for x in (f"오래된 값 {stale_n}개 제외" if stale_n else "", f"표본 부족 {insuff_n}개 제외" if insuff_n else "") if x) + ")"
    return text


def overview():
    """탭 응답: 티커별 series + 이름·합병 표시 + 최신 분기 시장 발언 목록. overview.json 에 저장하고 돌려준다."""
    cfg = load_config()
    out = {"generated_at": _now(), "tickers": cfg["tickers"], "names": cfg["names"], "merger_marks": cfg["merger_marks"], "quarters_back": cfg["quarters_back"],
           "series": {}, "latest_quarter": None, "market_statements": []}
    for code in cfg["tickers"]:
        s = series(code)
        out["series"][code] = s
        if s["quarters"]:
            lq = s["quarters"][-1]
            out["latest_quarter"] = max(out["latest_quarter"] or "", lq)
            out["market_statements"].extend(_market_statements(code, lq, s["market"].get(lq) or {}))
    out["outlook"] = []
    for code, s in out["series"].items():
        if s["quarters"]:
            lq = s["quarters"][-1]
            for x in (s["market"].get(lq) or {}).get("outlook") or []:
                if isinstance(x, dict) and x.get("quote"):
                    out["outlook"].append({"code": code, "quarter": lq, "metric": x.get("metric"), "segment": x.get("segment"), "region": x.get("region"),
                                           "horizon": x.get("horizon"), "value": x.get("value"), "direction": x.get("direction"), "quote": x["quote"]})
    out["regions"] = [{"key": k, "label": v} for k, v in REGIONS]
    out["signals"] = signals(out["series"], out["latest_quarter"])
    out["region_matrix"] = region_matrix(out["series"])
    out["company_deltas"] = {code: company_deltas(s) for code, s in out["series"].items()}
    out["upcoming"] = upcoming(out["series"], cfg["names"])
    _apply_daily(out)                      # 일간 수집물 반영 뒤에 문장·체크리스트 (신호판 최종 direction 기준)
    out["headline"] = headline(out["signals"], out["region_matrix"])
    try:   # C1 주가 기준선
        import drilling_daily as DD
        out["benchmark"] = DD.benchmark_stats()
    except Exception as e:
        out["benchmark"] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    out["checklist"] = cycle_checklist(out["signals"], out.get("company_deltas"), out.get("region_matrix"), out.get("benchmark"))
    r30 = out.get("recent30") or {}
    out["headline"] = (f"상승 사이클 조건 {out['checklist']['on']}/{out['checklist']['of']} · " + out["headline"]
                       + f" · 최근 30일 계약 {r30.get('contracts', 0)}건 · 단가 중앙값 " + (f"${r30['dayrate_median_k']:g}K" if r30.get("dayrate_median_k") is not None else "-"))
    _save(OVERVIEW, out)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="시추 업황 KPI 수집")
    ap.add_argument("--backfill", action="store_true", help="녹취록 받기 + KPI 추출")
    ap.add_argument("--force", nargs="*", default=[], help="다시 받을 분기 CODE:YYYYQn")
    ap.add_argument("--extract", action="store_true", help="KPI 없는 분기만 추출")
    ap.add_argument("--overview", action="store_true", help="overview.json 재생성")
    a = ap.parse_args()
    if a.backfill:
        res = backfill(a.force)
        print(json.dumps(res, ensure_ascii=False, indent=1))
    if a.extract:
        for code in load_config()["tickers"]:
            print(code, extract_missing(code))
    if a.overview or a.backfill or a.extract:
        ov = overview()
        print(f"[drilling] overview: 최신 분기 {ov['latest_quarter']}, 시장 발언 {len(ov['market_statements'])}건, "
              + ", ".join(f"{c} {len(s['quarters'])}분기" for c, s in ov["series"].items()))
