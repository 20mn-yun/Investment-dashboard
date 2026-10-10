"""종목 리포트 수집·AI 정리 — 텔레그램 리포트 DB(report_index.db)에서 최근 3개월치를 찾아 PDF 를 받고 Haiku 로 정리한다.

저장: cache/reports/<MARKET>_<code>/state.json(검색·받기 상태), pdf/<message_id>.pdf, summaries.json(message_id 별 AI 결과)
Drive: Analysis/리포트/<종목명>_<코드>/<날짜>_<증권사>_<제목>.pdf, _요약.md, Analysis/리포트/리포트_인덱스.md (drive_path 헬퍼만 사용)
Telethon: telegram_report 의 _shared_client/_shared_loop 만 재사용 (download_message 코루틴을 공유 루프에 던짐).
AI: Haiku(us_financials._call_ai 재사용), 리포트 정리용 일일 한도 AI_DAILY_LIMIT. 한 번 정리한 리포트는 다시 부르지 않는다.
"""
import asyncio
import json
import os
import re
import shutil
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta

import ai_budget
import telegram_report
import watchlist as watchlist_store

_BD = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(_BD, "cache", "reports")
AI_STATE_PATH = os.path.join(CACHE_DIR, "_ai_state.json")
DRIVE_SUBDIR = ("Analysis", "리포트")
INDEX_NAME = "리포트_인덱스.md"
SUMMARY_MD = "_요약.md"
MONTHS = 3
MAX_PER_STOCK = 40
AI_DAILY_LIMIT = ai_budget.DEFAULT_LIMITS["reports"]   # 300 — 공통 한도 모듈(ai_budget)에서 관리
AI_MODEL = "claude-haiku-4-5-20251001"
MIN_TEXT_CHARS = 200             # 이보다 적으면 이미지 PDF 로 보고 AI 를 부르지 않음
GROUP_PREFIXES = ("SK", "LG", "HD", "CJ", "GS", "DB", "NH", "KB", "HL", "DL", "LS", "HJ", "OCI", "KT", "LX", "BGF", "SNT")
# 발행사(증권사) 이름 — 종목명이 발행사 이름 안에서만 나오면(예: KB증권·삼성증권) 오탐으로 뺀다
ISSUERS = tuple(telegram_report.BROKERAGES) + ("KB증권", "삼성증권", "한국투자증권", "NH투자증권", "현대차증권", "SK증권",
                                                 "LS증권", "DB금융투자", "DS투자증권", "iM증권", "한양증권", "리딩투자증권", "토스증권")

_state_lock = threading.Lock()
_jobs = {}
_jobs_guard = threading.Lock()
_ai_lock = threading.Lock()


# ===== 경로·파일 =====
def key_of(market, code):
    return f"{market.upper()}_{str(code).upper()}"


def _dir(market, code):
    return os.path.join(CACHE_DIR, key_of(market, code))


def _state_path(market, code):
    return os.path.join(_dir(market, code), "state.json")


def _sum_path(market, code):
    return os.path.join(_dir(market, code), "summaries.json")


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    watchlist_store.atomic_write_json(path, data)


def _now():
    return datetime.now().isoformat(timespec="seconds")


def drive_folder_name(market, code, name):
    """Drive 폴더명: 미국은 티커만, 한국은 <종목명>_<코드> (3-C 규칙. 리포트·컨콜 공통)"""
    return code if market == "US" else f"{_safe(name) or code}_{code}"


def _safe(name):
    return re.sub(r'[/\\:*?"<>|\n\r]', "_", (name or "").strip())[:80] or "리포트"


# ===== 1. 검색 =====
def name_variants(market, code, name):
    """검색어: 종목명, 공백 제거형, 공백 삽입형(그룹 접두 뒤), 접두 그룹명을 뗀 약칭, 종목코드"""
    name = (name or "").strip()
    out = []
    if name:
        out.append(name)
        if " " in name:
            out.append(name.replace(" ", ""))
        for pfx in GROUP_PREFIXES:
            if name.upper().startswith(pfx) and len(name) > len(pfx):
                rest = name[len(pfx):].lstrip(" ")
                out.append(f"{pfx} {rest}")                 # 공백 삽입형 (SK 하이닉스)
                if len(rest) >= 3 and not rest.startswith("증권"):
                    out.append(rest)                        # 약칭 (하이닉스)
                break
    if market == "KR" and re.fullmatch(r"[0-9A-Z]{6}", str(code)):
        out.append(str(code))
    if market == "US":
        out.append(str(code).upper())
        first = re.split(r"[\s,.-]+", name)[0] if name else ""
        if len(first) >= 4 and first.lower() not in ("the", "common", "class"):
            out.append(first)
    seen, uniq = set(), []
    for v in out:
        if v and v.lower() not in seen:
            seen.add(v.lower())
            uniq.append(v)
    return uniq


def _esc(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _contains_outside_issuer(text, variants):
    """발행사 이름을 지운 뒤에도 검색어가 남아 있는지 (KB증권·삼성증권 오탐 제거)"""
    t = text or ""
    for iss in ISSUERS:
        t = t.replace(iss, " ")
    low = t.lower()
    return any(v.lower() in low or v.lower().replace(" ", "_") in low for v in variants)


def search_reports(market, code, name, months=MONTHS, limit=MAX_PER_STOCK):
    """report_index.db 검색 → [{channel, message_id, date, filename, text_preview, matched}] 최신순, 중복 제거, 상한"""
    db = os.path.join(_BD, telegram_report.REPORT_DB) if not os.path.isabs(telegram_report.REPORT_DB) else telegram_report.REPORT_DB
    if not os.path.exists(db):
        return [], {"variants": [], "raw": 0, "excluded_issuer": 0, "dedup": 0}
    variants = name_variants(market, code, name)
    if not variants:
        return [], {"variants": [], "raw": 0, "excluded_issuer": 0, "dedup": 0}
    since = (date.today() - timedelta(days=30 * months)).isoformat()
    conds, params = [], []
    for v in variants:
        conds.append("filename LIKE ? ESCAPE '\\'")
        params.append(f"%{_esc(v)}%")
        if " " in v:
            conds.append("filename LIKE ? ESCAPE '\\'")
            params.append(f"%{_esc(v.replace(' ', '_'))}%")
        conds.append("text_preview LIKE ? ESCAPE '\\'")
        params.append(f"%{_esc(v)}%")
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT channel, message_id, date, filename, text_preview FROM messages "
            f"WHERE date >= ? AND ({' OR '.join(conds)}) ORDER BY date DESC", [since] + params).fetchall()
    finally:
        conn.close()
    stats = {"variants": variants, "raw": len(rows), "excluded_issuer": 0, "dedup": 0}
    out, seen = [], set()
    for ch, mid, dt, fn, tp in rows:
        if not _contains_outside_issuer((fn or "") + " " + (tp or ""), variants):
            stats["excluded_issuer"] += 1
            continue
        norm = re.sub(r"[\s_]+", "", (fn or "").lower())
        if norm in seen:
            stats["dedup"] += 1
            continue
        seen.add(norm)
        matched = [v for v in variants if v.lower() in ((fn or "") + " " + (tp or "")).lower()
                   or v.lower().replace(" ", "_") in (fn or "").lower()]
        out.append({"channel": ch, "message_id": mid, "date": dt, "filename": fn or "", "text_preview": tp or "",
                    "matched": matched, "broker": guess_broker(fn, tp)})
        if len(out) >= limit:
            break
    return out, stats


def guess_broker(filename, preview=""):
    text = f"{filename or ''} {preview or ''}"
    for b in sorted(set(ISSUERS), key=len, reverse=True):
        if b in text:
            return b
    m = re.search(r"([가-힣A-Za-z&]{2,10}(?:투자증권|증권|금융투자))", text)
    return m.group(1) if m else None


# ===== 2. 받기 =====
def _pdf_path(market, code, message_id):
    return os.path.join(_dir(market, code), "pdf", f"{message_id}.pdf")


def _drive_dir(name, code):
    market = "KR" if re.fullmatch(r"\d{6}", code or "") else "US"     # 6자리 숫자 = 한국 종목코드
    return telegram_report.drive_path(*DRIVE_SUBDIR, drive_folder_name(market, code, name))


def _copy_to_drive(local_pdf, name, code, item):
    d = _drive_dir(name, code)
    if not d:
        return None, "Drive 를 찾지 못함"
    try:
        os.makedirs(d, exist_ok=True)
        title = re.sub(r"\.pdf$", "", item.get("filename") or str(item["message_id"]), flags=re.I)
        dst = os.path.join(d, _safe(f"{(item.get('date') or '')[:10]}_{item.get('broker') or '증권사'}_{title}") + ".pdf")
        if not os.path.exists(dst):
            shutil.copy2(local_pdf, dst)
        return dst, None
    except OSError as e:
        return None, f"{type(e).__name__}"


def download_one(market, code, name, item, timeout=180):
    """공유 루프에서 download_message 실행. 반환 (로컬 경로 또는 None, 오류)"""
    dest = _pdf_path(market, code, item["message_id"])
    if os.path.exists(dest) and os.path.getsize(dest) > 1000:
        return dest, None
    if telegram_report._shared_client is None or telegram_report._shared_loop is None:
        return None, "Telethon 클라이언트 준비 중"
    fut = asyncio.run_coroutine_threadsafe(telegram_report.download_message(item["channel"], item["message_id"], dest),
                                           telegram_report._shared_loop)
    try:
        fn, size = fut.result(timeout=timeout)
    except Exception as e:
        return None, f"{type(e).__name__}"
    if not fn or size <= 0:
        return None, "첨부 없음"
    return dest, None


# ===== 3. 텍스트 추출·AI 정리 =====
def extract_text(pdf_path, head=2, tail=2):
    """pypdf 로 1~head 페이지 + 마지막 tail 페이지. 반환 (텍스트, 페이지 수, 추출 글자 수)"""
    from pypdf import PdfReader
    r = PdfReader(pdf_path)
    n = len(r.pages)
    idx = list(range(min(head, n))) + [i for i in range(max(n - tail, head), n)]
    parts = []
    for i in idx:
        try:
            t = r.pages[i].extract_text() or ""
        except Exception:
            t = ""
        parts.append(f"=== {i + 1}/{n} 페이지 ===\n{t.strip()}")
    text = "\n\n".join(parts)
    return text, n, sum(len(p) for p in parts) - sum(len(f"=== {i + 1}/{n} 페이지 ===\n") for i in idx)


AI_SYSTEM = """당신은 한국 증권사 리포트를 구조화하는 분석 보조자입니다. 주어진 리포트 텍스트(앞 1~2페이지와 마지막 1~2페이지, 마지막 페이지에는 보통 Compliance·목표주가 변경 이력이 있음)만 근거로 JSON 하나를 만듭니다.

규칙
- 텍스트에 없는 값은 null. 추측하지 않는다. 코멘트·시황·산업 리포트처럼 투자의견·목표주가가 없으면 null 로 둔다.
- 숫자는 쉼표 없는 숫자(단위는 별도 필드). 금액 단위는 표에 적힌 그대로 적는다(예: "십억원", "억원", "조원", "백만달러").
- 연도 라벨은 표 머리글 그대로 쓴다(예: 2026F, 2026E, 2027(E), 3Q26P). 분기 추정도 허용.
- 추정 실적의 순이익은 지배주주(지배지분) 순이익이 있으면 그것, 없으면 당기순이익을 쓰고 어느 쪽인지 "순이익기준"에 적는다.
- 목표주가 이력은 Compliance 표(제시일자·투자의견·목표주가)에서 날짜순으로 적는다. 없으면 빈 배열.
- 이전 목표주가 = 이력에서 직전 제시 값, 또는 본문의 "상향/하향/유지" 문구로 알 수 있으면 그 값. 변경 방향은 상향|하향|유지|신규|null.
- 리포트 유형: 기업분석 | 실적리뷰 | 코멘트 | 산업 | 전략시황 | 기타.
- 요약은 5줄 이내, 각 줄은 사실 하나(숫자 포함), 한국어.
- 출력은 JSON 하나만. 설명·마크다운·코드펜스 금지. 형식:
{"증권사": "", "애널리스트": "", "발행일": "YYYY-MM-DD 또는 null", "리포트유형": "", "투자의견": "", "목표주가": 0, "이전목표주가": 0,
 "목표주가변경": "", "요약": ["", ""], "순이익기준": "지배주주|당기",
 "추정실적": [{"기간": "2026F", "매출액": 0, "영업이익": 0, "순이익": 0, "EPS": 0, "단위": "십억원"}],
 "목표주가이력": [{"날짜": "YYYY-MM-DD", "목표주가": 0, "투자의견": ""}]}"""


def _ai_allowed():
    """일일 한도(ai_budget kind='reports', 기본 AI_DAILY_LIMIT) 확인 + 카운터 증가. 반환 (허용 여부, 오늘 호출 수)"""
    return ai_budget.take("reports")


def _parse_json(text):
    text = text.replace("```json", "").replace("```", "").strip()
    i, j = text.find("{"), text.rfind("}")
    if i == -1 or j == -1:
        raise ValueError("JSON 없음")
    return json.loads(text[i:j + 1])


UNIT_TO_EOK = {"억원": 1, "억": 1, "십억원": 10, "십억": 10, "조원": 10000, "조": 10000, "백만원": 0.01, "백만": 0.01,
               "천억원": 1000, "백억원": 100, "십만원": 0.001, "만원": 0.0001}


def _to_eok(v, unit):
    if v is None:
        return None
    u = (unit or "").replace(" ", "")
    for k in sorted(UNIT_TO_EOK, key=len, reverse=True):
        if u.startswith(k):
            return round(float(v) * UNIT_TO_EOK[k], 1)
    return None if u and "원" not in u else float(v)   # 단위 모르면 그대로(억원 가정) — 달러 등은 None


def _norm_period(lb):
    s = str(lb or "").strip().upper().replace(" ", "")
    m = re.match(r"^(20\d\d)(?:\.\d\d)?\(?([AEFP])?\)?$", s) or re.match(r"^FY?(\d\d)([AEFP])?$", s)
    if m:
        y = m.group(1)
        y = ("20" + y) if len(y) == 2 else y
        k = m.group(2) or ""
        if k == "A" or int(y) < date.today().year:   # 지난 연도는 실적(A)
            return f"{y}A"
        return f"{y}E"
    m = re.match(r"^([1-4])Q(\d\d|20\d\d)([PEAF]?)$", s)
    if m:
        y = m.group(2) if len(m.group(2)) == 4 else "20" + m.group(2)
        return f"{y}Q{m.group(1)}{m.group(3) or ''}"
    return s


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).replace(",", "").replace("원", "").strip()
    try:
        return float(t)
    except ValueError:
        return None


def normalize_summary(raw, item, price=None):
    """AI 응답 → 저장 형식. 단위 억원 통일, 기간 라벨 정규화, 목표주가 범위 검사"""
    out = {
        "message_id": item["message_id"], "channel": item["channel"], "date": item.get("date"), "filename": item.get("filename"),
        "broker": (raw.get("증권사") or item.get("broker") or None), "analyst": raw.get("애널리스트") or None,
        "published": raw.get("발행일") or (item.get("date") or "")[:10], "type": raw.get("리포트유형") or None,
        "opinion": raw.get("투자의견") or None, "target": _num(raw.get("목표주가")), "prev_target": _num(raw.get("이전목표주가")),
        "target_change": raw.get("목표주가변경") or None, "summary": [str(x) for x in (raw.get("요약") or [])][:5],
        "net_income_basis": raw.get("순이익기준") or None, "estimates": [], "target_history": [], "flags": [],
    }
    for e in raw.get("추정실적") or []:
        if not isinstance(e, dict):
            continue
        unit = e.get("단위") or ""
        out["estimates"].append({"period": _norm_period(e.get("기간")), "period_raw": e.get("기간"),
                                 "revenue": _to_eok(_num(e.get("매출액")), unit), "operating_income": _to_eok(_num(e.get("영업이익")), unit),
                                 "net_income": _to_eok(_num(e.get("순이익")), unit), "eps": _num(e.get("EPS")), "unit_raw": unit, "unit": "억원"})
    for h in raw.get("목표주가이력") or []:
        if isinstance(h, dict) and _num(h.get("목표주가")):
            d = str(h.get("날짜") or "")
            m = re.match(r"^(\d\d)\.(\d\d)\.(\d\d)$", d)
            if m:
                d = f"20{m.group(1)}-{m.group(2)}-{m.group(3)}"
            out["target_history"].append({"date": d, "target": _num(h.get("목표주가")), "opinion": h.get("투자의견")})
    if out["target"] and price:
        r = out["target"] / price
        if r < 0.2 or r > 5:
            out["flags"].append(f"확인 필요: 목표주가 {out['target']:,.0f}가 현재가의 {r:.1f}배")
    if out["prev_target"] and price and not (0.2 <= out["prev_target"] / price <= 5):
        out["flags"].append(f"이전 목표주가 {out['prev_target']:,.0f}는 범위 밖이라 제외")
        out["prev_target"] = None
    if out["prev_target"] is None and len(out["target_history"]) >= 2:
        out["prev_target"] = out["target_history"][-2]["target"]
    return out


def summarize_one(market, code, name, item, pdf_path, price=None):
    """PDF 1건 → AI 정리 결과(dict). AI 를 부르지 않은 경우 status 에 이유."""
    import us_financials
    base = {"message_id": item["message_id"], "channel": item["channel"], "date": item.get("date"), "filename": item.get("filename"),
            "broker": item.get("broker"), "status": "ok", "text_ok": True, "tokens": None, "summarized_at": _now()}
    try:
        text, n_pages, n_chars = extract_text(pdf_path)
    except Exception as e:
        return dict(base, status=f"pdf_error:{type(e).__name__}", text_ok=False)
    base.update(pages=n_pages, chars=n_chars)
    if n_chars < MIN_TEXT_CHARS:
        return dict(base, status="no_text", text_ok=False)
    allowed, n = _ai_allowed()
    if not allowed:
        return dict(base, status="ai_limit", ai_pending=True)
    user = (f"종목: {name} ({code})\n파일명: {item.get('filename')}\n텔레그램 게시: {item.get('date')}\n"
            f"현재 주가: {price if price else '모름'}\n\n{text[:14000]}")
    tokens = {"input": 0, "output": 0}
    raw, err = None, None
    for attempt in range(2):
        try:
            t, ti, to = us_financials._call_ai(AI_SYSTEM, user)
            tokens["input"] += ti or 0
            tokens["output"] += to or 0
            raw = _parse_json(t)
            break
        except Exception as e:
            err = type(e).__name__
    if raw is None:
        return dict(base, status=f"ai_error:{err}", tokens=tokens)
    out = normalize_summary(raw, item, price)
    out.update(base, tokens=tokens, status="ok", text_ok=True, pages=n_pages, chars=n_chars, broker=out["broker"] or item.get("broker"))
    return out


# ===== 4. 집계 =====
def _opinion_class(op):
    s = (op or "").lower()
    if not s:
        return None
    if any(w in s for w in ("buy", "매수", "outperform", "overweight", "비중확대", "강력")):
        return "buy"
    if any(w in s for w in ("sell", "매도", "underperform", "비중축소", "reduce")):
        return "sell"
    return "hold"


def aggregate(summaries, price=None):
    ok = [s for s in summaries.values() if s.get("status") == "ok"]
    latest_by_broker = {}
    for s in sorted(ok, key=lambda x: x.get("published") or x.get("date") or ""):
        if s.get("target") and s.get("broker"):
            latest_by_broker[s["broker"]] = s
    targets = [s["target"] for s in latest_by_broker.values()]
    dist = {"buy": 0, "hold": 0, "sell": 0}
    for s in latest_by_broker.values():
        c = _opinion_class(s.get("opinion"))
        if c:
            dist[c] += 1
    traj, seen = [], set()
    for s in ok:
        if s.get("target"):
            k = (s.get("broker"), s.get("published"))
            if k not in seen:
                seen.add(k)
                traj.append({"date": s.get("published"), "broker": s.get("broker"), "target": s["target"], "opinion": s.get("opinion"), "from": "report"})
        for h in s.get("target_history") or []:
            k = (s.get("broker"), h.get("date"))
            if h.get("date") and k not in seen:
                seen.add(k)
                traj.append({"date": h["date"], "broker": s.get("broker"), "target": h["target"], "opinion": h.get("opinion"), "from": "history"})
    traj.sort(key=lambda x: (x["date"] or "", x["broker"] or ""))
    est = {}
    for s in sorted(ok, key=lambda x: x.get("published") or ""):
        for e in s.get("estimates") or []:
            per = _norm_period(e.get("period"))          # 저장 뒤 규칙이 바뀌어도 집계 때 다시 정규화 (지난 연도는 A)
            if not per or not per.endswith("E"):
                continue
            slot = est.setdefault(per, {})
            slot[s.get("broker") or s["message_id"]] = e        # 증권사별 최신
    est_table = {}
    for per, by_b in sorted(est.items()):
        row = {}
        for k in ("revenue", "operating_income", "net_income", "eps"):
            vals = [b[k] for b in by_b.values() if b.get(k) is not None]
            row[k] = {"mean": round(sum(vals) / len(vals), 1), "min": min(vals), "max": max(vals), "n": len(vals)} if vals else None
        est_table[per] = row
    mean = round(sum(targets) / len(targets)) if targets else None
    return {
        "target_mean": mean, "target_max": max(targets) if targets else None, "target_min": min(targets) if targets else None,
        "target_upside_pct": round((mean / price - 1) * 100, 1) if mean and price else None,
        "brokers_n": len(latest_by_broker), "opinion_dist": dist,
        "trajectory": traj, "estimates": est_table,
        "reports_ok": len(ok), "reports_total": len(summaries),
    }


# ===== 5. Drive 내보내기 =====
def _render_summary_md(name, code, items, summaries):
    lines = [f"# {name} ({code}) 리포트 요약", "", f"- 갱신: {_now()}", f"- 리포트 {len(items)}건, AI 정리 {sum(1 for s in summaries.values() if s.get('status') == 'ok')}건", ""]
    for it in sorted(items, key=lambda x: x.get("date") or "", reverse=True):
        s = summaries.get(str(it["message_id"])) or {}
        lines.append(f"## {(it.get('date') or '')[:10]} · {s.get('broker') or it.get('broker') or '-'} · {re.sub(r'\\.pdf$', '', it.get('filename') or '', flags=re.I)}")
        if s.get("status") == "ok":
            lines.append(f"- 유형 {s.get('type') or '-'} · 투자의견 {s.get('opinion') or '-'} · 목표주가 {int(s['target']):,} " if s.get("target") else f"- 유형 {s.get('type') or '-'} · 투자의견 {s.get('opinion') or '-'} · 목표주가 -")
            for line in s.get("summary") or []:
                lines.append(f"  - {line}")
            for e in s.get("estimates") or []:
                lines.append(f"  - {e['period']}: 매출 {e.get('revenue')} / 영업이익 {e.get('operating_income')} / 순이익 {e.get('net_income')} (억원), EPS {e.get('eps')}")
        elif s.get("status") == "no_text":
            lines.append("- 텍스트 추출 불가(이미지 PDF)")
        else:
            lines.append(f"- 정리 전 ({s.get('status') or '대기'})")
        lines.append("")
    return "\n".join(lines) + "\n"


def export_drive(market, code, name, state, summaries):
    d = _drive_dir(name, code)
    if not d:
        return {"ok": False, "error": "Drive 를 찾지 못함"}
    try:
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, SUMMARY_MD)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(_render_summary_md(name, code, list(state["items"].values()), summaries))
        os.replace(tmp, p)
        _write_index()
        return {"ok": True, "dir": d}
    except OSError as e:
        return {"ok": False, "error": f"{type(e).__name__}"}


def _write_index():
    root = telegram_report.drive_path(*DRIVE_SUBDIR)
    if not root:
        return
    rows = []
    if os.path.isdir(CACHE_DIR):
        for k in sorted(os.listdir(CACHE_DIR)):
            if "_" not in k or k.startswith("_"):
                continue
            st = _load_json(os.path.join(CACHE_DIR, k, "state.json"), None)
            if not st:
                continue
            sm = _load_json(os.path.join(CACHE_DIR, k, "summaries.json"), {})
            rows.append((st.get("name"), st.get("code"), st.get("market"), len(st.get("items", {})),
                         sum(1 for s in sm.values() if s.get("status") == "ok"), st.get("updated_at")))
    lines = ["# 리포트 인덱스", "", f"- 갱신: {_now()}", "", "| 종목 | 코드 | 시장 | 리포트 수 | AI 정리 | 마지막 갱신 | 폴더 |", "|---|---|---|---|---|---|---|"]
    for nm, code, mk, n, nok, up in rows:
        lines.append(f"| {nm or '-'} | {code} | {'한국' if mk == 'KR' else '미국'} | {n} | {nok} | {(up or '')[:16]} | {_safe(nm) or code}_{code}/ |")
    os.makedirs(root, exist_ok=True)
    p = os.path.join(root, INDEX_NAME)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, p)


# ===== 6. 작업(뒤에서 받기·정리) =====
def _load_state(market, code, name=""):
    st = _load_json(_state_path(market, code), None) or {"market": market, "code": code, "name": name, "items": {},
                                                          "last_search": None, "search_stats": None, "updated_at": None, "drive": None}
    if name and not st.get("name"):
        st["name"] = name
    return st


def _current_price(market, code):
    try:
        import stock_profile
        if market == "KR":
            s = stock_profile.get_summary("KR", code, "", None)
        else:
            import us_financials
            s = us_financials.get_summary(code, "")
        return s.get("price")
    except Exception:
        return None


def run_job(market, code, name, job, ai_limit=None, refresh_search=True):
    try:
        job.update(stage="searching")
        with _state_lock:
            st = _load_state(market, code, name)
        if refresh_search or not st["items"]:
            found, stats = search_reports(market, code, name)
            for it in found:
                k = str(it["message_id"])
                cur = st["items"].get(k, {})
                cur.update(it)
                st["items"][k] = cur
            st["last_search"] = _now()
            st["search_stats"] = stats
            st["updated_at"] = _now()
            _save_json(_state_path(market, code), st)
        todo = [it for it in st["items"].values() if not it.get("pdf")]
        job.update(stage="downloading", done=0, total=len(todo))
        for n, it in enumerate(todo, 1):
            p, err = download_one(market, code, name, it)
            if p:
                it["pdf"] = os.path.relpath(p, _BD)
                it["size"] = os.path.getsize(p)
                dst, derr = _copy_to_drive(p, name, code, it)
                it["drive"] = dst
                it["drive_error"] = derr
            else:
                it["error"] = err
            job.update(done=n)
            _save_json(_state_path(market, code), st)
        summaries = _load_json(_sum_path(market, code), {})
        price = _current_price(market, code)
        pend = [it for it in st["items"].values() if it.get("pdf") and str(it["message_id"]) not in summaries
                or (it.get("pdf") and (summaries.get(str(it["message_id"])) or {}).get("ai_pending"))]
        pend.sort(key=lambda x: x.get("date") or "", reverse=True)
        if ai_limit is not None:
            pend = pend[:ai_limit]
        job.update(stage="summarizing", done=0, total=len(pend))
        for n, it in enumerate(pend, 1):
            res = summarize_one(market, code, name, it, os.path.join(_BD, it["pdf"]), price)
            summaries[str(it["message_id"])] = res
            _save_json(_sum_path(market, code), summaries)
            job.update(done=n)
        st["updated_at"] = _now()
        st["drive"] = export_drive(market, code, name, st, summaries)
        _save_json(_state_path(market, code), st)
        job.update(stage="done", finished_at=_now())
    except Exception as e:
        job.update(stage="failed", error=f"{type(e).__name__}: {e}", finished_at=_now())
        print(f"[stock_reports] {market} {code} 작업 실패: {type(e).__name__}: {e}", flush=True)


def start_job(market, code, name, ai_limit=None, refresh_search=True):
    k = key_of(market, code)
    with _jobs_guard:
        job = _jobs.get(k)
        if job and job["stage"] not in ("done", "failed"):
            return job
        job = {"stage": "queued", "done": 0, "total": 0, "error": None, "started_at": _now(), "finished_at": None}
        _jobs[k] = job
        threading.Thread(target=run_job, args=(market, code, name, job, ai_limit, refresh_search), daemon=True).start()
        return job


def run_blocking(market, code, name, ai_limit=None):
    """미리 받기용: 작업을 돌리고 끝날 때까지 기다린다"""
    job = start_job(market, code, name, ai_limit)
    while job["stage"] not in ("done", "failed"):
        time.sleep(1)
    return job


def status(market, code, name="", start=True, ai_limit=None):
    """GET /api/stock/reports 응답"""
    st = _load_state(market, code, name)
    summaries = _load_json(_sum_path(market, code), {})
    k = key_of(market, code)
    with _jobs_guard:
        job = _jobs.get(k)
    today = date.today().isoformat()
    if start and (job is None or job["stage"] in ("done", "failed")) and (st.get("last_search") or "")[:10] != today:
        job = start_job(market, code, name, ai_limit, refresh_search=True)
    price = None
    items = []
    for it in sorted(st["items"].values(), key=lambda x: x.get("date") or "", reverse=True):
        s = summaries.get(str(it["message_id"])) or {}
        items.append({
            "message_id": it["message_id"], "channel": it["channel"], "date": it.get("date"), "filename": it.get("filename"),
            "title": re.sub(r"\.pdf$", "", it.get("filename") or "", flags=re.I).replace("_", " "),
            "broker": s.get("broker") or it.get("broker"), "analyst": s.get("analyst"), "type": s.get("type"),
            "opinion": s.get("opinion"), "target": s.get("target"), "prev_target": s.get("prev_target"), "target_change": s.get("target_change"),
            "summary": s.get("summary") or [], "estimates": s.get("estimates") or [], "flags": s.get("flags") or [],
            "downloaded": bool(it.get("pdf")), "download_error": it.get("error"), "drive": it.get("drive"),
            "text_ok": s.get("text_ok") if s else None, "ai_status": s.get("status") if s else ("downloaded" if it.get("pdf") else "pending"),
        })
    agg = aggregate(summaries, _current_price(market, code) if summaries else None)
    return {
        "market": market, "code": code, "name": st.get("name") or name,
        "job": {k2: v for k2, v in (job or {}).items()} if job else None,
        "last_search": st.get("last_search"), "search_stats": st.get("search_stats"),
        "period": {"from": (date.today() - timedelta(days=30 * MONTHS)).isoformat(), "to": today},
        "count": len(items), "items": items, "aggregate": agg, "drive": st.get("drive"),
        "ai_daily": {"counter_date": ai_budget.snapshot()["date"], "counter_value": ai_budget.used("reports"), "limit": AI_DAILY_LIMIT},
    }
