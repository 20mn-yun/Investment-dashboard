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
import hashlib
import time
from datetime import date, datetime, timedelta

import ai_budget
import telegram_report
import watchlist as watchlist_store

_BD = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(_BD, "cache", "reports")
AI_STATE_PATH = os.path.join(CACHE_DIR, "_ai_state.json")
DRIVE_SUBFOLDER = "리포트"       # 종목 폴더(기존 증권사 리포트 기능과 같은 Analysis/<종목>/) 안의 하위 폴더 이름
SUMMARY_MD = "_요약.md"
DRIVE_SKIP_SUBFOLDERS = ("사업보고서",)   # DART 보고서 폴더는 증권사 리포트 후보에서 제외
MONTHS = 3
MAX_PER_STOCK = None             # 4-E: 건수 상한 없음 (기간 3개월만). AI 정리는 ai_budget 한도
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
    except (OSError, json.JSONDecodeError):
        return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    watchlist_store.atomic_write_json(path, data)


def _now():
    return datetime.now().isoformat(timespec="seconds")


def stock_folder_keyword(market, code, name):
    """종목 폴더 키워드 — telegram_report.stock_folder_keyword (모든 기능 공통)"""
    return telegram_report.stock_folder_keyword(market, code, name or None)


def stock_root(market, code, name):
    """종목 폴더 절대 경로 (telegram_report.stock_folder 재사용). Drive 없으면 None"""
    base = telegram_report.report_download_base()
    if not base:
        return None
    return telegram_report.stock_folder(base, stock_folder_keyword(market, code, name))


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
        if limit and len(out) >= limit:
            break
    return out, stats


def guess_broker(filename, preview=""):
    """증권사 추정 — telegram_report.guess_broker (두 기능 공통)"""
    return telegram_report.guess_broker(filename, preview)


# ===== 2. 받기 =====
def _pdf_path(market, code, message_id):
    return os.path.join(_dir(market, code), "pdf", f"{message_id}.pdf")


def _drive_dir(name, code, market=None):
    """Drive 저장 폴더 = 종목 폴더/리포트 (종목 폴더는 기존 기능과 같은 규칙)"""
    market = market or ("KR" if re.fullmatch(r"\d{6}", code or "") else "US")
    root = stock_root(market, code, name)
    return os.path.join(root, DRIVE_SUBFOLDER) if root else None


def _copy_to_drive(local_pdf, name, code, item, market=None):
    """로컬 사본 → Drive 종목 폴더/리포트/<종목명>_<yymmdd>_<증권사|미확인>_<레포트명>.pdf (공통 규칙). 중복이면 기존 파일을 쓴다.
    .index.json 에 {message_id: 파일명} 기록 (기존 증권사 리포트 기능과 공유)."""
    d = _drive_dir(name, code, market)
    if not d:
        return None, "Drive 를 찾지 못함"
    root = os.path.dirname(d)
    try:
        fname = _drive_filename(item, name, market)
        mid = item["message_id"] if str(item.get("channel")) != "drive" else None
        dup = telegram_report.find_duplicate(root, mid, fname, item.get("filename"))
        if dup:
            if mid is not None:
                telegram_report.record_report(root, mid, os.path.basename(dup))
            return dup, None
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, fname)
        shutil.copy2(local_pdf, dst)
        if mid is not None:
            telegram_report.record_report(root, mid, fname)
        return dst, None
    except OSError as e:
        return None, f"{type(e).__name__}"


def _stock_label(name, code, market=None):
    market = market or ("KR" if re.fullmatch(r"\d{6}", code or "") else "US")
    return telegram_report.us_short_name(code, name or None) if market == "US" else (name or code)


def _drive_filename(item, name=None, market=None, broker=None):
    """공통 파일명 규칙 (telegram_report.report_filename). 증권사는 받을 때 아는 값, 모르면 '미확인'"""
    code = item.get("_code") or ""
    return telegram_report.report_filename(_stock_label(name, code, market), item.get("date"), broker or item.get("broker"),
                                           item.get("filename") or f"{item['message_id']}.pdf")


def rename_if_broker_found(market, code, name, item, broker):
    """'미확인' 으로 저장된 Drive 파일을 AI 요약이 알아낸 증권사 이름으로 바꾼다 (로컬 message_id.pdf 는 그대로). 반환 새 경로 또는 None"""
    old = item.get("drive")
    if not broker or not old or "_미확인_" not in os.path.basename(old) or not os.path.isfile(old):
        return None
    new_name = _drive_filename(item, name, market, broker)
    dst = os.path.join(os.path.dirname(old), new_name)
    if os.path.exists(dst):
        return None
    try:
        os.rename(old, dst)
    except OSError:
        return None
    item["drive"] = dst
    item["broker"] = broker
    root = stock_root(market, code, name)
    if root and str(item.get("channel")) != "drive":
        telegram_report.record_report(root, item["message_id"], new_name)
    return dst


def download_one(market, code, name, item, timeout=180):
    """공유 루프에서 download_message 실행. 반환 (로컬 경로 또는 None, 오류).
    받기 전에 Drive 종목 폴더에 같은 파일명이 있으면(기존 기능이 받아둔 것 포함) Drive→로컬 복사만 하고 다시 받지 않는다."""
    dest = _pdf_path(market, code, item["message_id"])
    if os.path.exists(dest) and os.path.getsize(dest) > 1000:
        return dest, None
    root = stock_root(market, code, name)
    src = item.get("drive_src")
    if not src and root:
        src = telegram_report.find_duplicate(root, item["message_id"] if str(item.get("channel")) != "drive" else None,
                                             _drive_filename(item, name, market), item.get("filename"))
    if src and os.path.isfile(src):
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(src, dest)
            item["drive"] = src
            item["from_drive"] = True
            return dest, None
        except OSError as e:
            return None, f"Drive 복사 실패 {type(e).__name__}"
    if str(item.get("channel")) == "drive":
        return None, "Drive 원본 없음"
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
            t, ti, to = us_financials._call_ai(AI_SYSTEM, user, task="reports")
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
    d = _drive_dir(name, code, market)
    if not d:
        return {"ok": False, "error": "Drive 를 찾지 못함"}
    try:
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, SUMMARY_MD)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(_render_summary_md(name, code, list(state["items"].values()), summaries))
        os.replace(tmp, p)
        return {"ok": True, "dir": d}
    except OSError as e:
        return {"ok": False, "error": f"{type(e).__name__}"}


def _drive_id(filename):
    return "d_" + hashlib.sha1(filename.encode("utf-8")).hexdigest()[:12]


def _db_row_by_mid(mid):
    db = os.path.join(_BD, telegram_report.REPORT_DB) if not os.path.isabs(telegram_report.REPORT_DB) else telegram_report.REPORT_DB
    if not os.path.exists(db):
        return None
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT channel, message_id, date, filename, text_preview FROM messages WHERE message_id=? ORDER BY date DESC LIMIT 1", (int(mid),)).fetchone()
    except (ValueError, sqlite3.Error):
        return None
    finally:
        conn.close()


def _add_drive_candidates(market, code, name, found, stats, months=MONTHS, extra_names=(), extra_mids=()):
    """종목 Drive 폴더(하위 폴더 1단계, 사업보고서 폴더 제외)의 PDF 중 최근 months 개월(수정 시각) 것을 후보에 넣는다.
    같은 파일명이 텔레그램 후보에 이미 있으면 건너뜀. 항목 키는 d_<sha1(파일명)>, channel 'drive'."""
    root = stock_root(market, code, name)
    stats = dict(stats or {}, drive=0)
    if not root or not os.path.isdir(root):
        return found, stats
    since = time.time() - 30 * months * 86400
    norm_ = lambda x: re.sub(r"[\s_]+", "", telegram_report.nfc(x or "").lower())
    have = {norm_(it.get("filename")) for it in found}
    have |= {norm_(fn) for fn in extra_names if fn}   # 이미 목록에 있는 파일(단건 받기 등)은 중복 후보로 넣지 않음
    known_mids = {str(it.get("message_id")) for it in found} | set(str(m) for m in (extra_mids or []))
    idx_by_name = {norm_(fn): mid for mid, fn in telegram_report.load_report_index(root).items() if fn}   # .index.json: 파일명 → message_id
    out = list(found)
    try:
        subs = telegram_report._listdir_retry(root) or []
    except Exception:
        subs = []
    for sub in subs:
        if sub in DRIVE_SKIP_SUBFOLDERS:
            continue
        sp = os.path.join(root, sub)
        if not os.path.isdir(sp):
            continue
        for fn in telegram_report._listdir_retry(sp) or []:
            if not fn.lower().endswith(".pdf") or fn.startswith("."):
                continue
            p = os.path.join(sp, fn)
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            if mt < since:
                continue
            norm = norm_(fn)
            if norm in have:
                continue
            have.add(norm)
            mid = idx_by_name.get(norm)
            if mid is not None:                       # 기존 기능이 받아 .index.json 에 기록한 파일: message_id 로 DB 에서 원래 정보를 찾아 일반 항목으로
                if str(mid) in known_mids:
                    continue
                row = _db_row_by_mid(mid)
                if row:
                    out.append({"channel": row[0], "message_id": row[1], "date": row[2], "filename": row[3] or telegram_report.nfc(fn), "text_preview": row[4] or "",
                                "matched": ["drive"], "broker": guess_broker(row[3], row[4]), "drive_src": p, "drive": p})
                    known_mids.add(str(mid))
                    stats["drive"] += 1
                    continue
            out.append({"channel": "drive", "message_id": _drive_id(fn), "date": datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M"),
                        "filename": telegram_report.nfc(fn), "text_preview": "", "matched": ["drive"], "broker": guess_broker(fn, ""), "drive_src": p, "drive": p})
            stats["drive"] += 1
    out.sort(key=lambda x: x.get("date") or "", reverse=True)
    return out, stats


def fetch_single(market, code, name, channel, message_id):
    """(채널, message_id) 1건만 받아 로컬 사본 + Drive 종목 폴더에 저장 (AI 요약 없음). 반환 (로컬 pdf 경로 또는 None, 오류)"""
    try:
        mid = int(message_id)
    except (TypeError, ValueError):
        return None, "message_id 형식 오류"
    with _state_lock:
        st = _load_state(market, code, name)
        it = st["items"].get(str(mid))
    if not it:
        db = os.path.join(_BD, telegram_report.REPORT_DB) if not os.path.isabs(telegram_report.REPORT_DB) else telegram_report.REPORT_DB
        row = None
        if os.path.exists(db):
            conn = sqlite3.connect(db)
            try:
                row = conn.execute("SELECT channel, message_id, date, filename, text_preview FROM messages WHERE channel=? AND message_id=?",
                                   (channel, mid)).fetchone()
            finally:
                conn.close()
        if not row:
            return None, "리포트 DB 에 없는 메시지"
        it = {"channel": row[0], "message_id": row[1], "date": row[2], "filename": row[3] or "", "text_preview": row[4] or "",
              "matched": [], "broker": guess_broker(row[3], row[4])}
    if it.get("pdf") and os.path.isfile(os.path.join(_BD, it["pdf"])):
        return os.path.join(_BD, it["pdf"]), None
    p, err = download_one(market, code, name, it)
    if not p:
        return None, err
    it["pdf"] = os.path.relpath(p, _BD)
    it["size"] = os.path.getsize(p)
    if not (it.get("from_drive") and it.get("drive")):
        dst, derr = _copy_to_drive(p, name, code, it, market)
        it["drive"], it["drive_error"] = dst, derr
    with _state_lock:
        st = _load_state(market, code, name)
        cur = st["items"].get(str(mid), {})
        cur.update(it)
        st["items"][str(mid)] = cur
        st["updated_at"] = _now()
        _save_json(_state_path(market, code), st)
    return p, None


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
            found, stats = _add_drive_candidates(market, code, name, found, stats,
                                                 extra_names=[it.get("filename") for it in st["items"].values()]
                                                 + [os.path.basename(it["drive"]) for it in st["items"].values() if it.get("drive")],
                                                 extra_mids=list(st["items"].keys()))   # 기존 기능으로 받아둔 Drive PDF 도 후보에
            for it in found:
                k = str(it["message_id"])
                cur = st["items"].get(k, {})
                cur.update(it)
                st["items"][k] = cur
            st["last_search"] = _now()
            st["search_stats"] = stats
            st["updated_at"] = _now()
            _save_json(_state_path(market, code), st)
        # 이미 받은 로컬 사본인데 Drive 사본이 없거나(옛 폴더가 지워진 경우 등) 경로가 사라졌으면 Drive 종목 폴더로 다시 복사 (다운로드 없음)
        for it in st["items"].values():
            lp = os.path.join(_BD, it["pdf"]) if it.get("pdf") else None
            if lp and os.path.isfile(lp) and not (it.get("drive") and os.path.isfile(it["drive"])):
                dst, derr = _copy_to_drive(lp, name, code, it, market)
                it["drive"], it["drive_error"] = dst, derr
        todo = [it for it in st["items"].values() if not it.get("pdf")]
        job.update(stage="downloading", done=0, total=len(todo))
        for n, it in enumerate(todo, 1):
            p, err = download_one(market, code, name, it)
            if p:
                it["pdf"] = os.path.relpath(p, _BD)
                it["size"] = os.path.getsize(p)
                if it.get("from_drive") and it.get("drive"):
                    it["drive_error"] = None
                else:
                    dst, derr = _copy_to_drive(p, name, code, it, market)
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
            if res.get("status") == "ok" and res.get("broker"):
                rename_if_broker_found(market, code, name, it, res["broker"])      # '미확인' → 증권사 이름으로 (4-D)
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
            "file": f"{it['message_id']}.pdf" if it.get("pdf") else None,
            "file_url": f"/api/stock/reports/file?market={market}&code={code}&name={it['message_id']}.pdf" if it.get("pdf") else None,
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
