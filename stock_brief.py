"""종목 브리프 — 종목별로 Drive 에 마크다운 한 장(<종목 폴더>/_브리프.md)을 쓴다. LLM 호출 없음(저장본만 읽음).

사용: build(market, code, name, corp_code=None) → {"ok", "md", "name", "key", "failed": [섹션...]}
      write_drive(market, code, name, md) → {"ok", "path", "bytes", "local"}
      python -m stock_brief KR 298040 | python -m stock_brief US NVDA | python -m stock_brief --all
섹션마다 try/except — 실패한 섹션은 "(수집 실패: ...)" 로 쓰고 나머지는 계속. 값이 없으면 "-" (추정값 없음).
"""
import os
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone

import telegram_report
import watchlist as watchlist_store

_BD = os.path.dirname(os.path.abspath(__file__))
LOCAL_DIR = os.path.join(_BD, "cache", "briefs")
BRIEF_NAME = "_브리프.md"
VERSION = "stock_brief v1"
KST = timezone(timedelta(hours=9))
REPORT_DAYS = 90
DISC_DAYS = 30
MAX_REPORTS = 40


def _now():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M")


def _key(market, code):
    return f"{market}_{code}"


def _n(v, digits=0):
    """숫자 표기: None → '-', 그 외 천 단위 구분"""
    if v is None or v == "":
        return "-"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{f:,.{digits}f}"


def _pct(v):
    return "-" if v is None else f"{float(v):+.1f}%"


def _d(s):
    return (s or "")[:10] or "-"


def _cell(s):
    return str(s if s is not None else "-").replace("|", "／").replace("\n", " ").strip() or "-"


def _fail(e):
    return f"(수집 실패: {type(e).__name__}: {str(e)[:120]})"


# ===== 섹션 =====
def _sec_header(market, code, name, corp_code):
    import stock_profile
    s = stock_profile.get_summary(market, code, name, corp_code)
    cur = "원" if market == "KR" else "$"
    lines = [f"- 기준일시(KST): {_now()}",
             f"- 주가: {_n(s.get('price'), 0 if market == 'KR' else 2)}{cur} (전일대비 {_pct(s.get('change_pct'))})",
             f"- 시가총액: {_n((s.get('market_cap') or 0) / 1e8 if market == 'KR' and s.get('market_cap') else (s.get('market_cap') or 0) / 1e6 if s.get('market_cap') else None)}"
             + (" 억원" if market == "KR" else " 백만달러") + (f" ({s.get('market_cap_basis')})" if s.get("market_cap_basis") else ""),
             f"- PER/PBR(TTM): {_n(s.get('per'), 2)} / {_n(s.get('pbr'), 2)}" + (f" — {s.get('valuation_basis')}" if s.get("valuation_basis") else ""),
             f"- ROE: {_pct(s.get('roe'))}"]
    if market == "KR":
        try:
            import valuation_ttm
            v = valuation_ttm.valuation_asof(code)
            lines.append(f"- TTM 기준 분기: 순이익 {v.get('ttm_end') or '-'} 까지 4분기 누적 / 자본 {v.get('equity_end') or '-'} 말"
                         + (f" ({v.get('reason')})" if v.get("reason") else ""))
        except Exception as e:
            lines.append(f"- TTM 기준 분기: {_fail(e)}")
    return lines


def _sec_financials(market, code, name):
    if market == "KR":
        import kr_financials as fm
        cache = fm._load_cache(code)
        if not cache or not cache.get("acc_mt"):
            return ["(재무 저장본 없음)"]
        fin = fm.compute(cache, 5, name)
        div, unit = 1e8, "억원"
    else:
        import us_financials as fm
        cache = fm._load_cache(code)
        if not cache or not cache.get("facts"):
            return ["(재무 저장본 없음)"]
        fin = fm.compute(cache, 5, name, persist=False)
        div, unit = 1e6, "백만달러"
    labels = (fin.get("periods") or {}).get("quarter") or []
    labels = labels[-4:]
    if not labels:
        return ["(분기 데이터 없음)"]
    items = ((fin.get("statements") or {}).get("IS") or {}).get("items") or {}
    mq = (fin.get("metrics") or {}).get("quarter") or {}
    rows = [("매출액", "revenue"), ("영업이익", "operating_income"), ("지배주주순이익", "net_income_owner")]
    out = [f"단위: {unit}", "", "| 항목 | " + " | ".join(labels) + " |", "|---|" + "---:|" * len(labels)]
    for label, key in rows:
        it = items.get(key)
        if not it and key == "revenue":
            out.append("| 매출액 | " + " | ".join(["매출 계정 없음"] * len(labels)) + " |")
            continue
        q = (it or {}).get("quarter") or {}
        cells = []
        for lb in labels:
            v = q.get(lb)
            yoy = (mq.get(lb) or {}).get(f"{key}_yoy")
            cells.append("-" if v is None else f"{_n(v / div)}" + (f" ({_pct(yoy)})" if yoy is not None else ""))
        out.append(f"| {label} | " + " | ".join(cells) + " |")
    out.append("")
    out.append("(괄호 안은 YoY)")
    return out


def _sec_reports(market, code, name):
    import stock_reports
    st = stock_reports.status(market, code, name, start=False)
    since = (date.today() - timedelta(days=REPORT_DAYS)).isoformat()
    items = [it for it in st.get("items") or [] if (it.get("date") or "")[:10] >= since and it.get("ai_status") != "duplicate"]
    items.sort(key=lambda x: x.get("date") or "", reverse=True)
    if not items:
        return ["(최근 3개월 리포트 없음)"]
    out = ["| 날짜 | 증권사 | 애널리스트 | 의견 | 목표가 | 핵심 한 줄 | 파일명 |", "|---|---|---|---|---|---|---|"]
    for it in items[:MAX_REPORTS]:
        t, p = it.get("target"), it.get("prev_target")
        if t and p and t != p:
            tgt = f"{_n(p)}→{_n(t)} {'↑' if t > p else '↓'}"
        elif t:
            tgt = _n(t) + (" (유지)" if it.get("target_change") == "유지" else "")
        else:
            tgt = "-"
        head = (it.get("summary") or [""])[0] if it.get("summary") else ("-" if it.get("ai_status") == "ok" else f"(정리 {it.get('ai_status') or '대기'})")
        out.append(f"| {_d(it.get('date'))} | {_cell(it.get('broker'))} | {_cell(it.get('analyst'))} | {_cell(it.get('opinion'))} | {tgt} | {_cell(head)} | {_cell(it.get('filename'))} |")
    if len(items) > MAX_REPORTS:
        out.append(f"| … | | | | | 외 {len(items) - MAX_REPORTS}건 | |")
    agg = st.get("aggregate") or {}
    dist = agg.get("opinion_dist") or {}
    out.append("")
    out.append(f"집계(증권사별 최신 {agg.get('brokers_n') or 0}곳): 목표가 평균 {_n(agg.get('target_mean'))} / 최고 {_n(agg.get('target_max'))} / 최저 {_n(agg.get('target_min'))}"
               + (f" (현재가 대비 {_pct(agg.get('target_upside_pct'))})" if agg.get("target_upside_pct") is not None else "")
               + (f" · 의견 매수 {dist.get('buy', 0)} / 중립 {dist.get('hold', 0)} / 매도 {dist.get('sell', 0)}" if dist else "")
               + f" · AI 정리 {agg.get('reports_ok', 0)}/{agg.get('reports_total', 0)}건")
    return out


def _sec_actions(code, days=90, max_rows=25):
    import us_analyst_actions
    rows = us_analyst_actions.recent(code, days)
    if not rows:
        return ["(최근 90일 액션 없음 또는 미수집)"]
    out = ["| 날짜 | 기관 | 액션 | 등급(전→후) | 목표가(전→후) |", "|---|---|---|---|---|"]
    up = down = keep = 0
    for r in rows[:max_rows]:
        grade = f"{r.get('from_grade') or '-'}→{r.get('to_grade') or '-'}" if r.get("from_grade") and r.get("from_grade") != r.get("to_grade") else (r.get("to_grade") or "-")
        p, c = r.get("prior_pt"), r.get("current_pt")
        pt = f"{_n(p)}→{_n(c)}" if p and c and p != c else (_n(c) if c else "-")
        out.append(f"| {_d(r.get('date'))} | {_cell(r.get('firm'))} | {_cell(r.get('pt_action') or r.get('action'))} | {_cell(grade)} | {pt} |")
    for r in rows:
        p, c = r.get("prior_pt"), r.get("current_pt")
        if p and c:
            up += c > p
            down += c < p
            keep += c == p
    if len(rows) > max_rows:
        out.append(f"| … | | | | 외 {len(rows) - max_rows}건 |")
    out.append("")
    out.append(f"목표가 변경 있는 건: 상향 {up} / 하향 {down} / 유지 {keep} (총 {len(rows)}건)")
    return out


def _sec_8k(code):
    import sec_filings
    st = sec_filings.status(code)
    items = st.get("items") or []
    if not items:
        return ["(저장된 8-K 실적 보도자료 없음)"]
    out = []
    seen_acc = []
    for it in items:
        if it["accession"] in seen_acc:
            continue
        seen_acc.append(it["accession"])
        if len(seen_acc) > 2:
            break
        exs = [x for x in items if x["accession"] == it["accession"]]
        out.append(f"### {it['date']} · " + " · ".join(f"{x['exhibit']} {x.get('filename') or ''}" for x in exs))
        s = next((x.get("summary") for x in exs if x.get("summary")), None)
        if s:
            if s.get("분기"):
                out.append(f"- 분기: {_cell(s['분기'])}" + (f" (단위 {s.get('숫자단위')})" if s.get("숫자단위") else ""))
            for line in (s.get("실적핵심") or [])[:3]:
                out.append(f"- 실적핵심: {_cell(line)}")
            g = s.get("가이던스") or {}
            for line in ((g.get("다음분기") or []) + (g.get("연간") or []))[:3]:
                out.append(f"- 가이던스: {_cell(line)}")
        else:
            out.append("- (요약 없음)")
        out.append("")
    return out


def _sec_calls(market, code, name):
    import earnings_calls
    if market == "US":
        st = earnings_calls.status_us(code, name)
        rows = [q for q in st.get("quarters") or [] if q.get("summary")][:2]
        if not rows:
            return ["(컨콜 요약 없음)"]
        out = []
        for q in rows:
            s = q["summary"]
            out.append(f"### {q.get('label') or q.get('quarter')} ({q.get('quarter')})")
            for title, key in (("실적핵심", "실적핵심"), ("리스크", "리스크")):
                for line in (s.get(key) or [])[:3]:
                    out.append(f"- {title}: {_cell(line)}")
            g = s.get("가이던스") or {}
            for line in ((g.get("다음분기") or []) + (g.get("연간") or []))[:3]:
                out.append(f"- 가이던스: {_cell(line)}")
            tone = s.get("톤") or {}
            if tone.get("판정"):
                out.append(f"- 톤: {tone.get('판정')} — {_cell(tone.get('근거'))}")
            out.append("")
        return out
    st = earnings_calls.status_kr(code, name)
    items = (st.get("items") or [])[:5]
    if not items:
        return ["(실적발표·컨콜 관련 자료 없음)"]
    return [f"- {_d(it.get('date'))} · {_cell(it.get('title') or it.get('filename'))}" for it in items]


def _sec_disclosures(market, code, corp_code):
    since = (date.today() - timedelta(days=DISC_DAYS)).isoformat()
    if market == "KR":
        import stock_profile
        d = stock_profile.kr_disclosures(code, corp_code)
    else:
        import us_financials
        d = us_financials.disclosures(code)
    items = [it for it in d.get("items") or [] if (it.get("date") or "")[:10] >= since]
    items.sort(key=lambda x: x.get("date") or "", reverse=True)
    if not items:
        return ["(최근 30일 공시 없음)"]
    out = ["| 날짜 | 제목 | 비고 |", "|---|---|---|"]
    for it in items:
        note = ", ".join(x for x in ("정기공시" if it.get("periodic") else "", it.get("remark") or "", it.get("form") or "") if x)
        out.append(f"| {_d(it.get('date'))} | {_cell(it.get('title'))} | {_cell(note)} |")
    return out


def _sec_notes(market, code, name):
    import stock_notes
    note = stock_notes._read(market, code)
    if not note or not stock_notes.has_content(note):
        return ["(아직 기록 없음)"]
    out = []
    last_by_field = {}
    for h in note.get("history") or []:
        if (h.get("at") or "") > (last_by_field.get(h.get("field")) or ""):
            last_by_field[h.get("field")] = h.get("at")
    for k, label in stock_notes.FIELDS:
        upd = last_by_field.get(k) or note.get("updated_at")
        out += [f"### {label} (갱신 {_d(upd)})", "", (note["fields"].get(k) or "").strip() or "(비어 있음)", ""]
    memos = sorted(note.get("memos") or [], key=lambda m: m.get("at", ""), reverse=True)[:5]
    out.append("### 메모 (최근 5건)")
    out += [f"- {(m.get('at') or '')[:16].replace('T', ' ')} · {_cell(m.get('text'))}" for m in memos] or ["(없음)"]
    hist = sorted(note.get("history") or [], key=lambda h: h.get("at", ""), reverse=True)[:10]
    out.append("")
    out.append("### 변경 이력 (최근 10건)")
    out += [f"- {_d(h.get('at'))} · {h.get('label') or h.get('field')}" for h in hist] or ["(없음)"]
    return out


def _sec_peers(market, code, name, corp_code, fx):
    import stock_peers
    st = stock_peers.status(market, code, name, corp_code, fx)
    confirmed = {(c.get("market"), c.get("code")) for c in st.get("confirmed") or []}
    rows = [r for r in (st.get("compare") or {}).get("rows") or [] if r.get("is_base") or (r.get("market"), r.get("code")) in confirmed]
    if len(rows) <= 1 and not confirmed:
        return ["(확정된 관련 기업 없음)"]
    out = ["| 종목 | PER | fPER | PBR | ROE | 매출성장 | 영업이익률 |", "|---|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        nm = f"{r.get('name') or r.get('code')}" + (" (기준)" if r.get("is_base") else "")
        out.append(f"| {_cell(nm)} | {_n(r.get('per'), 1)} | {_n(r.get('fper'), 1)} | {_n(r.get('pbr'), 2)} | {_pct(r.get('roe'))} | {_pct(r.get('revenue_yoy'))} | {_pct(r.get('operating_margin'))} |")
    outlook = (st.get("calls") or {}).get("outlook") or []
    if outlook:
        out.append("")
        out.append("최근 컨콜·리포트 전망:")
        for o in outlook:
            for line in (o.get("lines") or [])[:2]:
                out.append(f"- {o.get('name') or o.get('code')}" + (f" ({o.get('quarter')})" if o.get("quarter") else "") + f": {_cell(line)}")
    return out


# ===== 조립 =====
def build(market, code, name, corp_code=None, fx=None):
    market = (market or "").strip().upper()
    code = watchlist_store.normalize_code(market, code) or code
    if not name or (market == "KR" and corp_code is None):
        try:
            import stock_peers
            r = stock_peers.resolve(market, code)
            if r:
                name = name or r[2]
                corp_code = corp_code or r[3]
        except Exception:
            pass
    name = name or code
    failed = []

    def run(title, fn):
        try:
            body = fn()
        except Exception as e:
            failed.append(f"{title}: {type(e).__name__}: {str(e)[:120]}")
            body = [_fail(e)]
        return [f"## {title}", ""] + list(body) + [""]

    lines = [f"# {name} ({market}:{code}) 브리프", ""]
    try:
        lines += _sec_header(market, code, name, corp_code)
    except Exception as e:
        failed.append(f"개요: {type(e).__name__}: {str(e)[:120]}")
        lines.append(_fail(e))
    lines.append("")
    lines += run("최근 실적 (분기)", lambda: _sec_financials(market, code, name))
    lines += run("증권사 리포트 (최근 3개월)", lambda: _sec_reports(market, code, name))
    if market == "US":
        lines += run("애널리스트 액션 (최근 90일)", lambda: _sec_actions(code))
    if market == "US":
        lines += run("회사 발표 (8-K 실적 보도자료)", lambda: _sec_8k(code))
    lines += run("컨콜", lambda: _sec_calls(market, code, name))
    lines += run("공시 (최근 30일)", lambda: _sec_disclosures(market, code, corp_code))
    lines += run("나의 판단", lambda: _sec_notes(market, code, name))
    lines += run("관련 기업", lambda: _sec_peers(market, code, name, corp_code, fx))
    lines += ["---", f"생성: {VERSION}, {_now()}. 이 파일은 매일 05:30 배치가 통째로 다시 씁니다. 원문은 같은 폴더의 리포트/·컨콜/·판단/ 참조.", ""]
    return {"ok": True, "md": "\n".join(lines), "name": name, "key": _key(market, code), "failed": failed}


def write_drive(market, code, name, md):
    """<종목 폴더>/_브리프.md (NFC) + 로컬 사본 cache/briefs/<KEY>.md. 반환 {ok, path, bytes, local} 또는 {ok: False, error}"""
    key = _key(market, code)
    os.makedirs(LOCAL_DIR, exist_ok=True)
    local = os.path.join(LOCAL_DIR, f"{key}.md")
    data = md.encode("utf-8")
    tmp = local + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, local)
    base = telegram_report.report_download_base()
    if not base:
        return {"ok": False, "error": "Drive 를 찾지 못함 (로컬 사본만 저장)", "local": local}
    try:
        kw = telegram_report.stock_folder_keyword(market, code, name or None)
        folder = telegram_report.stock_folder(base, kw)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, unicodedata.normalize("NFC", BRIEF_NAME))
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return {"ok": True, "path": path, "bytes": len(data), "local": local}
    except OSError as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}", "local": local}


def build_and_write(market, code, name, corp_code=None, fx=None):
    b = build(market, code, name, corp_code, fx)
    w = write_drive(market, code, b["name"], b["md"])
    return {"ok": w.get("ok", False), "path": w.get("path"), "bytes": w.get("bytes"), "local": w.get("local"),
            "failed_sections": b["failed"], "error": w.get("error")}


def _main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 1
    targets = []
    if argv[0] == "--all":
        targets = [(i["market"], i["code"], i.get("name", "")) for i in watchlist_store.load_watchlist()]
    else:
        targets = [(argv[0], argv[1], argv[2] if len(argv) > 2 else "")]
    rc = 0
    for market, code, name in targets:
        r = build_and_write(market, code, name)
        print(f"[{market}:{code}] ok={r['ok']} bytes={r.get('bytes')} path={r.get('path')} 실패섹션={r['failed_sections'] or '없음'}" + (f" error={r.get('error')}" if r.get("error") else ""), flush=True)
        if not r["ok"]:
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
