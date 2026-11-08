"""추정치 묶기 (5-C): 재무 요약 표 맨 왼쪽 "추정 열"에 넣을 다음 분기·올해·다음해 전망치를 항목별로 고른다.

우선순위(항목마다 따로): 컨콜 가이던스(미국, earnings_calls.extract_guidance 결과) → 증권사 리포트 추정 평균(최근 3개월, 증권사별 최신 1건)
→ 컨센서스(forward_estimates: 미국 yfinance 매출·EPS, 한국 fnguide/네이버 매출·영업이익·순이익·EPS).
단위는 재무 표와 같은 원 단위(한국 원, 미국 USD). 컨콜 범위(low/high)는 중간값을 값으로, detail 에 범위 표기.
AI 호출 없음. 저장: cache/estimates/<MARKET>_<code>.json (입력 파일 mtime 서명이 바뀌거나 날짜가 바뀌면 다시 계산).
"""
import glob
import json
import os
import re
import threading
from datetime import date, datetime, timedelta

import earnings_calls
import forward_estimates
import stock_reports

_BD = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(_BD, "cache", "estimates")
REPORT_WINDOW_DAYS = 92
ITEM_KEYS = ("revenue", "operating_income", "net_income", "eps", "fcf", "ebitda")
_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
_UNIT_MULT = {"억원": 1e8, "십억원": 1e9, "조원": 1e12, "백만원": 1e6, "원": 1.0,
              "백만달러": 1e6, "십억달러": 1e9, "USD M": 1e6, "USD B": 1e9, "USD": 1.0}
_lock = threading.Lock()
_mem = {}


# ---------- 공통 ----------
def _path(market, code):
    return os.path.join(CACHE_DIR, f"{market}_{code}.json")


def _mtime(p):
    try:
        return int(os.stat(p).st_mtime)
    except OSError:
        return 0


def _next_label(label):
    return earnings_calls._next_label(label)


def _prev_year(label):
    return f"{int(label[:4]) - 1}{label[4:]}"


def _fy_end_after(label, fm):
    """분기 말 라벨 뒤에 오는 첫 회계연도 말 라벨 (라벨 자체가 연도 말이면 다음 연도)"""
    y, m = int(label[:4]), int(label[5:7])
    fy = y if m < fm else y + 1
    return f"{fy}.{fm:02d}"


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return (sum(vals) / len(vals)) if vals else None


def _drop_outliers(by_b):
    """증권사별 (published, value) 중 단위 오류로 보이는 값 제거: 3건 이상이면 중앙값의 1/3~3배 밖, 2건이면 5배 넘게 벌어질 때 둘 다 버림(불일치). 반환 (남은 dict, 버린 증권사)"""
    vals = sorted(abs(v) for _, v in by_b.values() if v)
    if len(vals) >= 3:
        med = vals[len(vals) // 2]
        keep = {b: pv for b, pv in by_b.items() if med / 3 <= abs(pv[1]) <= med * 3}
    elif len(vals) == 2 and vals[1] > vals[0] * 5:
        keep = {}
    else:
        keep = dict(by_b)
    return keep, sorted(set(by_b) - set(keep))


def _fmt_money(v, market):
    if v is None:
        return "-"
    if market == "US":
        a = abs(v)
        s = "-" if v < 0 else ""
        return f"{s}${a / 1e9:.2f}B" if a >= 1e9 else f"{s}${a / 1e6:.0f}M"
    a = abs(v)
    s = "-" if v < 0 else ""
    return f"{s}{a / 1e12:.2f}조" if a >= 1e12 else f"{s}{a / 1e8:,.0f}억"


# ---------- 재무 저장본 ----------
def _financials(market, code, name):
    """(compute 결과, 결산월) — 저장본 없으면 (None, None). 받아오기 시작하지 않음."""
    if market == "US":
        import us_financials
        cache = us_financials._load_cache(code)
        if not cache or not cache.get("facts"):
            return None, None
        out = us_financials.compute(cache, 5, name, persist=False)
        return out, int(out.get("fiscal_month") or 12)
    import kr_financials
    cache = kr_financials._load_cache(code)
    if not cache or not cache.get("acc_mt"):
        return None, None
    out = kr_financials.compute(cache, 5, name, persist=False)
    return out, int(out.get("fiscal_month") or cache.get("acc_mt") or 12)


def _actual(fin, key, view, label):
    for sheet in (fin.get("statements") or {}).values():
        it = (sheet.get("items") or {}).get(key)
        if it:
            v = (it.get(view) or {}).get(label)
            if v is not None:
                return v
    return None


# ---------- ① 컨콜 가이던스 ----------
def _resolve_pending_growth(pending, call_c, columns_lower):
    """성장률 가이던스를 '올해E' 추정값 기준으로 환산해 컨콜 후보에 넣는다 (컨콜·환산 > 리포트 > 컨센 우선순위 유지)."""
    for p in pending:
        if (call_c.get(p["col"]) or {}).get("revenue"):
            continue
        ref_it = (columns_lower.get(p["ref_col"]) or {}).get("revenue")
        if not ref_it or ref_it.get("value") is None:
            continue
        ref = ref_it["value"]
        src_nm = {"call": "컨콜", "reports": "리포트", "consensus": "컨센"}.get(ref_it.get("source"), ref_it.get("source"))
        it = {"value": ref * (1 + p["gpct"] / 100), "source": "call", "converted": True,
              "detail": f"추정 기준 환산: 올해E {_fmt_money(ref, 'US')}({src_nm}) × (1{p['gpct']:+.1f}%) · " + p["detail"]}
        if p.get("quotes"):
            it["detail"] += "\n인용: " + " / ".join(p["quotes"])
        if p.get("gm") is not None:
            it["detail"] += f"\n매출총이익률 가이던스 {p['gm']:.1f}%"
        call_c.setdefault(p["col"], {})["revenue"] = it


def _call_candidates(code, fin, fm, labels, pending):
    """컨콜 가이던스 → {열이름: {항목: {value, detail, source:'call'}}}. 전년 실적이 없어 환산 못 한 성장률 가이던스는 pending 에 쌓는다."""
    out = {}
    lg = earnings_calls.latest_guidance(code)
    if not lg:
        return out
    g = lg["guidance"]
    quotes = g.get("quotes") or []
    for kind, col in (("next_quarter", "next_quarter"), ("fiscal_year", "this_year"), ("fiscal_year", "next_year")):
        blk = g.get(kind) or {}
        if not blk.get("has_any") or blk.get("period_end") != labels[col]:
            continue
        items = {}
        unit = blk.get("unit")
        rng = lambda lo, hi, f: (f" (범위 {f(lo)}~{f(hi)})" if lo is not None and hi is not None else "")
        base = f"컨콜 {lg['quarter']} 가이던스 · 기간 {blk.get('period') or labels[col]}"
        # 매출: 절대값 → 아니면 성장률로 환산
        rev = earnings_calls._adj_amount(blk.get("revenue"), unit)
        if rev is not None:
            lo, hi = earnings_calls._adj_amount(blk.get("revenue_low"), unit), earnings_calls._adj_amount(blk.get("revenue_high"), unit)
            items["revenue"] = {"value": rev, "source": "call", "detail": base + rng(lo, hi, lambda x: _fmt_money(x, "US"))}
        elif blk.get("revenue_growth_pct") is not None and blk.get("growth_basis") in ("yoy", "qoq"):   # 기준(yoy/qoq)이 불명확하면 환산하지 않음
            gpct = blk["revenue_growth_pct"]
            basis = blk["growth_basis"]
            lo, hi = blk.get("revenue_growth_low"), blk.get("revenue_growth_high")
            rng_txt = f" (범위 {lo:+.1f}%~{hi:+.1f}%)" if lo is not None and hi is not None else ""
            if col == "next_quarter":
                ref_lb = _prev_year(labels[col]) if basis == "yoy" else labels["latest_quarter"]
                ref = _actual(fin, "revenue", "quarter", ref_lb)
            else:
                ref_lb = _prev_year(labels[col])
                ref = _actual(fin, "revenue", "annual", ref_lb)
            if ref is not None:
                items["revenue"] = {"value": ref * (1 + gpct / 100), "source": "call", "converted": True,
                                    "detail": base + f" · 실적 기준 환산: {ref_lb} 실적 {_fmt_money(ref, 'US')} × (1{gpct:+.1f}% {basis.upper()})" + rng_txt}
            elif basis == "yoy" and col == "next_year":
                # 전년(올해E)이 아직 추정이면 뒤에서 올해E 매출 추정값으로 환산 (_resolve_pending_growth)
                pending.append({"col": col, "ref_col": "this_year", "gpct": gpct, "detail": base + rng_txt, "quotes": quotes[:2],
                                "gm": blk.get("gross_margin_pct")})
        for src_key, item in (("op_income", "operating_income"), ("adj_ebitda", "ebitda")):
            v = earnings_calls._adj_amount(blk.get(src_key), blk.get(src_key + "_unit") or unit)
            if v is None:
                continue
            note = " · 조정 EBITDA 가이던스" if item == "ebitda" else ""
            if rev is not None and v > rev * 1.5 and v / 1000 <= rev * 1.5:   # 블록 단위(USD B)를 M 값에 잘못 적용한 경우 보정 (예: EBITDA 450 → $450M)
                v = v / 1000
                note += " · 단위 보정(USD B→M, 매출 대비 크기로 판단)"
            items[item] = {"value": v, "source": "call", "detail": base + note}
        if blk.get("eps") is not None:
            items["eps"] = {"value": float(blk["eps"]), "source": "call", "detail": base}
        if items:
            q = " / ".join(quotes[:2])
            for it in items.values():
                if q:
                    it["detail"] += f"\n인용: {q}"
            if blk.get("gross_margin_pct") is not None:
                for it in items.values():
                    it["detail"] += f"\n매출총이익률 가이던스 {blk['gross_margin_pct']:.1f}%"
            out[col] = items
    return out


# ---------- ② 리포트 추정 ----------
def _report_period_end(period, market, fm, today):
    """리포트 추정 기간 라벨 → (달력 종료월 'YYYY.MM', 종류 'annual'|'quarter'). 추정(E/F/P 또는 미래 연도)만, 실적(A)·해석 불가는 None.
    연간 라벨은 올해/다음해 열에만, 분기 라벨은 다음분기 열에만 맞춘다(결산 12월 회사는 연말 라벨이 다음 분기 라벨과 같을 수 있음)."""
    s = str(period or "").strip().upper().replace(" ", "").replace("(", "").replace(")", "")
    m = re.match(r"^FY([A-Z]{3})-?(\d\d)[EFP]?$", s)          # FYJAN-27E
    if m and m.group(1) in _MONTHS:
        return f"20{m.group(2)}.{_MONTHS[m.group(1)]:02d}", "annual"
    m = re.match(r"^(?:FY)?(20\d\d|\d\d)([EFPA])?$", s)        # 2026E, FY27E, 2026
    if m:
        y = int(m.group(1)) if len(m.group(1)) == 4 else 2000 + int(m.group(1))
        k = m.group(2)
        if k == "A" or (k is None and y < today.year):
            return None
        return f"{y}.{fm:02d}", "annual"
    m = re.match(r"^(20\d\d)\.(\d\d)([EFP])?$", s)              # 2026.12E
    if m:
        return f"{m.group(1)}.{m.group(2)}", "annual"
    m = re.match(r"^(\d\d)/(\d\d)([EFP])?$", s)                  # 26/12E (연/월, 회계연도 말)
    if m:
        return f"20{m.group(1)}.{m.group(2)}", "annual"
    m = re.match(r"^(20\d\d)Q([1-4])([EFP])?$", s)                 # 2026Q3E
    m2 = None if m else re.match(r"^([1-4])Q(20\d\d|\d\d)([EFP])?$", s)   # 3Q26E
    if m or m2:
        if m:
            y, q = int(m.group(1)), int(m.group(2))
        else:
            q = int(m2.group(1))
            y = int(m2.group(2)) if len(m2.group(2)) == 4 else 2000 + int(m2.group(2))
        if market == "KR" or fm == 12:
            return f"{y}.{q * 3:02d}", "quarter"
        return earnings_calls._fq_end(y, q, fm), "quarter"
    return None


def _report_candidates(market, code, fm, labels, today):
    """리포트 추정 → {열이름: {항목: {value, source:'reports', n, brokers, detail}}}"""
    summ = stock_reports._load_json(stock_reports._sum_path(market, code), {}) or {}
    since = (today - timedelta(days=REPORT_WINDOW_DAYS)).isoformat()
    by_col = {col: {} for col in ("next_quarter", "this_year", "next_year")}   # col → item → broker → (published, value)
    for s in summ.values():
        if s.get("status") != "ok" or s.get("unit_suspect"):
            continue
        pub = (s.get("published") or s.get("date") or "")[:10]
        if not pub or pub < since:
            continue
        broker = s.get("broker") or f"#{s.get('message_id')}"
        for e in s.get("estimates") or []:
            pe = _report_period_end(e.get("period_raw") or e.get("period"), market, fm, today)
            if not pe:
                continue
            end, kind = pe
            cols = ("next_quarter",) if kind == "quarter" else ("this_year", "next_year")
            col = next((c for c in cols if labels[c] == end), None)
            if not col:
                continue
            mult = _UNIT_MULT.get(e.get("unit") or "", 1e8 if market == "KR" else 1e6)
            for key in ("revenue", "operating_income", "net_income", "eps", "ebitda"):
                v = e.get(key)
                if key == "ebitda" and v in (None, 0, 0.0):
                    v = e.get("adj_ebitda")                   # EBITDA 행이 없고 조정 EBITDA 만 있으면 그것
                if v in (None, 0, 0.0):
                    continue
                val = float(v) * (1 if key == "eps" else mult)
                cur = by_col[col].setdefault(key, {}).get(broker)
                if not cur or pub > cur[0]:
                    by_col[col][key][broker] = (pub, val)
    out = {}
    for col, items in by_col.items():
        res = {}
        for key, by_b_all in items.items():
            by_b, dropped = _drop_outliers(by_b_all)
            if not by_b:
                continue
            vals = [v for _, v in by_b.values()]
            brokers = sorted(by_b, key=lambda b: by_b[b][0], reverse=True)
            fm_ = (lambda x: (f"{x:,.2f}" if market == "US" else f"{x:,.0f}")) if key == "eps" else (lambda x: _fmt_money(x, market))
            detail = (f"리포트 {len(vals)}건 평균 (최근 {REPORT_WINDOW_DAYS}일, 증권사별 최신): " + ", ".join(f"{b} {fm_(by_b[b][1])}({by_b[b][0][5:]})" for b in brokers)
                      if len(vals) > 1 else f"리포트 1건: {brokers[0]} {fm_(vals[0])} ({by_b[brokers[0]][0]})")
            if dropped:
                detail += f" · 단위 의심으로 제외: {', '.join(dropped)}"
            res[key] = {"value": _mean(vals), "source": "reports", "n": len(vals), "brokers": brokers, "detail": detail}
        if res:
            out[col] = res
    return out


# ---------- ③ 컨센서스 ----------
def _consensus_candidates(market, code, fm, labels):
    try:
        est = forward_estimates._load(market, code)
        if not est or (est.get("fetched_at") or "")[:10] != date.today().isoformat():
            est = forward_estimates.kr_consensus(code, fm) if market == "KR" else forward_estimates.us_consensus(code)
    except Exception as e:  # 네트워크 실패 → 저장본이라도
        est = forward_estimates._load(market, code)
        if not est:
            return {}
    out = {}
    src = est.get("source") or "consensus"
    asof = est.get("asof") or (est.get("fetched_at") or "")[:10]
    for lb, yd in (est.get("years") or {}).items():
        m = re.match(r"^(?:FY)?(20\d\d)(?:\.(\d\d))?E$", str(lb))
        if not m:
            continue
        end = f"{m.group(1)}.{m.group(2) or f'{fm:02d}'}"
        col = next((c for c in ("this_year", "next_year") if labels[c] == end), None)
        if not col:
            continue
        items = {}
        for key in ("revenue", "operating_income", "net_income", "eps"):
            v = yd.get(key)
            if v is None:
                continue
            n = yd.get("analysts_n") or est.get("analysts_n")
            det = f"컨센서스 {src} ({asof})" + (f" · 애널리스트 {n}명" if n else "")
            if key == "eps" and yd.get("eps_low") is not None and yd.get("eps_high") is not None:
                det += f" · 범위 {yd['eps_low']:.2f}~{yd['eps_high']:.2f}"
            items[key] = {"value": float(v), "source": "consensus", "detail": det}
        if items:
            out[col] = items
    return out


# ---------- 합치기 ----------
def _signature(market, code):
    sig = {"reports": _mtime(stock_reports._sum_path(market, code)), "consensus": _mtime(forward_estimates._path(market, code))}
    if market == "US":
        import us_financials
        sig["fin"] = _mtime(us_financials._cache_path(code))
        sig["calls"] = max([_mtime(p) for p in glob.glob(os.path.join(earnings_calls._dir(code), "*Q[1-4].json"))] or [0])
    else:
        import kr_financials
        sig["fin"] = _mtime(kr_financials._cache_path(code))
    return sig


def compute(market, code, name=""):
    today = date.today()
    fin, fm = _financials(market, code, name)
    base = {"ok": False, "market": market, "code": code, "name": name, "computed_at": datetime.now().isoformat(timespec="seconds"),
            "columns": {}, "has_any": False}
    if not fin:
        return dict(base, error="not_cached", message="재무 저장본이 없어 추정 열을 만들 수 없음")
    qs = (fin.get("periods") or {}).get("quarter") or []
    if not qs:
        return dict(base, error="no_periods")
    latest = qs[-1]
    actual_q, actual_a = set(qs), set((fin.get("periods") or {}).get("annual") or [])
    nq = _next_label(latest)
    while nq in actual_q:                      # 실적이 이미 나온 분기는 추정 대상에서 제외 (옛 "2026Q3E" 리포트가 남아 있어도 안 쓰이게)
        nq = _next_label(nq)
    ty = _fy_end_after(latest, fm)
    while ty in actual_a:
        ty = f"{int(ty[:4]) + 1}{ty[4:]}"
    labels = {"latest_quarter": latest, "next_quarter": nq, "this_year": ty, "next_year": f"{int(ty[:4]) + 1}{ty[4:]}"}
    pending = []
    call_c = _call_candidates(code, fin, fm, labels, pending) if market == "US" else {}
    rep_c = _report_candidates(market, code, fm, labels, today)
    con_c = _consensus_candidates(market, code, fm, labels)
    if pending:   # 성장률 가이던스의 기준(올해E)을 리포트·컨센·컨콜 중 우선순위대로 골라 환산
        lower = {}
        for col in ("this_year",):
            lower[col] = {}
            for c in (call_c, rep_c, con_c):
                for key, it in (c.get(col) or {}).items():
                    lower[col].setdefault(key, it)
        _resolve_pending_growth(pending, call_c, lower)
    cands = [("call", call_c), ("reports", rep_c), ("consensus", con_c)]
    columns = {}
    for col in ("next_quarter", "this_year", "next_year"):
        items = {}
        for _, c in cands:
            for key, it in (c.get(col) or {}).items():
                if key not in items and it.get("value") is not None:
                    items[key] = it
        if not items:
            continue
        view = "quarter" if col == "next_quarter" else "annual"
        for key, it in items.items():
            it["prev_actual"] = _actual(fin, key, view, _prev_year(labels[col]))   # 전년 같은 기간 실적 (YoY 기준)
        srcs = [it["source"] for it in items.values()]
        columns[col] = {"label": labels[col], "view": view, "items": items,
                        "source": max(set(srcs), key=srcs.count), "sources": sorted(set(srcs))}
    return dict(base, ok=True, fiscal_month=fm, latest_quarter=latest, labels=labels, columns=columns, has_any=bool(columns),
                unit="USD" if market == "US" else "KRW")


def get_estimates(market, code, name="", refresh=False):
    """저장본이 오늘 것이고 입력 파일 서명이 같으면 그대로, 아니면 다시 계산해 저장."""
    market = (market or "").upper()
    key = (market, code)
    sig = _signature(market, code)
    today = date.today().isoformat()
    with _lock:
        cur = _mem.get(key)
        if cur is None:
            try:
                with open(_path(market, code), "r", encoding="utf-8") as f:
                    cur = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                cur = None
        if cur and not refresh and cur.get("_sig") == sig and (cur.get("computed_at") or "")[:10] == today:
            _mem[key] = cur
            return {k: v for k, v in cur.items() if not k.startswith("_")}
    out = compute(market, code, name)
    out["_sig"] = sig
    with _lock:
        _mem[key] = out
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmp = _path(market, code) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=1)
            os.replace(tmp, _path(market, code))
        except OSError:
            pass
    return {k: v for k, v in out.items() if not k.startswith("_")}
