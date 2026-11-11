"""DART 공시 감시 (app.py 에서 옮긴 구획: 감시 루프·요약·텔레그램·엑셀·/api/dart/* 주소).

- app 을 import 하지 않는다(app.py 는 __main__ 으로 실행되므로 그것을 import 하면 두 번째 로드가 된다).
- import 시점에 스레드를 시작하지 않는다. app.py 가 init() → register_blueprint(bp) → _start_monitor() 순서로 호출.
- MARKET_STOCKS, load_dart_corp_map, _sync_dart_to_others 는 app.py 가 init() 으로 주입한다(옮긴 함수 본문이 같은 이름을 쓰도록).
"""
import io
import json
import logging
import os
import re
import threading
import time
import zipfile
from datetime import date, timedelta, datetime

import requests
import yfinance as yf
from flask import Blueprint, jsonify, request

import kr_financials
import llm_client
import telegram_report
import watchlist as watchlist_store

logger = logging.getLogger("app")
DART_API_KEY = os.environ.get("DART_API_KEY")

bp = Blueprint("dart_monitor", __name__)

# app.py 가 init() 으로 넣는 자리
MARKET_STOCKS = None
load_dart_corp_map = None
_sync_dart_to_others = None


def init(market_stocks, load_dart_corp_map_fn, sync_dart_to_others_fn):
    global MARKET_STOCKS, load_dart_corp_map, _sync_dart_to_others
    MARKET_STOCKS = market_stocks
    load_dart_corp_map = load_dart_corp_map_fn
    _sync_dart_to_others = sync_dart_to_others_fn


# ===== DART 공시 모니터링 =====

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_TOKEN_GENERAL = os.environ.get("TELEGRAM_TOKEN_GENERAL")
TELEGRAM_TOKEN_EARNINGS = os.environ.get("TELEGRAM_TOKEN_EARNINGS")


_BD = os.path.dirname(os.path.abspath(__file__))
DART_MON_CFG_FILE = os.path.join(_BD, "dart_monitor_config.json")
DART_SEEN_FILE = os.path.join(_BD, "dart_seen.json")
DART_DAILY_FILE = os.path.join(_BD, "dart_daily.json")
DART_HISTORY_FILE = os.path.join(_BD, "dart_daily_history.json")
DART_SENT_FILE = os.path.join(_BD, "dart_sent.json")
# 드라이브 마운트는 glob 탐색(telegram_report.drive_path) — 계정 이메일 하드코딩 방지.
# 마운트 없으면 None → 아래 copy2 지점에서 예외로 잡혀 로컬 파일만 유지됨.
EXCEL_PATH = telegram_report.drive_path("공시정리", "DART_공시_누적.xlsx")
EXCEL_PATH_LOCAL = os.path.join(_BD, "DART_공시_누적.xlsx")

DEFAULT_DART_MON_CFG = {
    "watchlist": ["005930", "000660", "035420"],
    "earnings_conditions": {
        "revenue_growth": True, "op_profit_growth": True, "condition_type": "or",
    },
    "telegram_chat_ids": {"general": None, "earnings": None},
    "monitor_enabled": True,
    "digest_telegram_enabled": False,
}

_dart_alert_log = []
_monitor_thread = None


def _lj(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _sj(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_dm_cfg():
    cfg = _lj(DART_MON_CFG_FILE, None)
    if cfg is None:
        cfg = DEFAULT_DART_MON_CFG.copy()
        _sj(DART_MON_CFG_FILE, cfg)
    return cfg


def save_dm_cfg(cfg):
    watchlist_store.atomic_write_json(DART_MON_CFG_FILE, cfg)




def _load_seen():
    return set(_lj(DART_SEEN_FILE, []))


def _save_seen(seen):
    # 기존 순서를 유지하면서 최근 항목만 보관 (set은 순서 미보장이므로 리스트 기반)
    existing = _lj(DART_SEEN_FILE, [])
    existing_set = set(existing)
    new_items = [x for x in seen if x not in existing_set]
    ordered = existing + new_items
    _sj(DART_SEEN_FILE, ordered[-3000:])


def _load_daily_history():
    return _lj(DART_HISTORY_FILE, {"days": {}})


def _save_daily_history(h):
    _sj(DART_HISTORY_FILE, h)


def _load_daily():
    h = _load_daily_history()
    today = date.today().isoformat()
    items = h.get("days", {}).get(today, [])
    return {"date": today, "items": items}


def _save_daily(d):
    h = _load_daily_history()
    days = h.get("days", {})
    days[d["date"]] = d["items"]
    cutoff = (date.today() - timedelta(days=14)).isoformat()
    days = {k: v for k, v in days.items() if k >= cutoff}
    h["days"] = days
    _save_daily_history(h)


def _load_sent():
    return _lj(DART_SENT_FILE, {"date": "", "sent": []})


def _save_sent(d):
    _sj(DART_SENT_FILE, d)


def _alog(cat, msg):
    _dart_alert_log.append({"time": datetime.now().strftime("%H:%M"), "cat": cat, "msg": msg})
    if len(_dart_alert_log) > 50:
        _dart_alert_log.pop(0)


# --- Telegram ---
TG_MAX_LEN = 4000


def _tg_split_message(text, max_len=TG_MAX_LEN):
    if len(text) <= max_len:
        return [text]
    chunks = []
    current = ""
    for para in text.split("\n\n"):
        if len(para) > max_len:
            if current:
                chunks.append(current)
                current = ""
            for line in para.split("\n"):
                if len(line) > max_len:
                    while line:
                        space = max_len if not current else max_len - len(current) - 1
                        if current:
                            current += "\n" + line[:space]
                            chunks.append(current)
                            current = ""
                        else:
                            chunks.append(line[:space])
                        line = line[space:]
                elif current and len(current) + 1 + len(line) > max_len:
                    chunks.append(current)
                    current = line
                else:
                    current = current + "\n" + line if current else line
        elif current and len(current) + 2 + len(para) > max_len:
            chunks.append(current)
            current = para
        else:
            current = current + "\n\n" + para if current else para
    if current:
        chunks.append(current)
    return chunks


def _tg_send(token, chat_id, text):
    if not chat_id:
        return False
    try:
        chunks = _tg_split_message(text)
        total = len(chunks)
        if total > 1:
            print(f"[TG] 메시지 분할 전송: {total}개 청크")
        for idx, chunk in enumerate(chunks, 1):
            if total > 1:
                chunk += f"\n\n({idx}/{total})"
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": chunk,
                      "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=10,
            )
            if idx < total:
                time.sleep(0.3)
        return True
    except Exception:
        return False


def _tg_detect(token):
    try:
        res = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10)
        data = res.json()
        if data.get("ok") and data.get("result"):
            return data["result"][-1]["message"]["chat"]["id"]
    except Exception:
        pass
    return None


# --- Gemini (남은 호출: _save_excel 의 1줄 요약만) ---
def _gemini(prompt):
    try:
        res = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent"
            f"?key={GEMINI_API_KEY}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=30,
        )
        return res.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        logger.warning("[gemini] _gemini(_save_excel) 실패: %s", f"{type(e).__name__}: {e}"[:200])
        return ""


# --- LLM (llm_client: 공시 모니터 A묶음 — dart_watch_summary / dart_detail / earnings_extract) ---
def _llm(task, prompt, json_mode=False):
    """llm_client.complete 어댑터. 성공 시 text(json_mode 면 파싱된 data), 실패 시 ""(json_mode 면 None) 반환 + warning 로그.
    기존 _gemini 호출부와 같은 '실패 = 빈 값' 계약을 유지한다."""
    empty = None if json_mode else ""
    try:
        r = llm_client.complete(task, prompt, json_mode=json_mode)
    except Exception as e:
        logger.warning("[llm] %s 호출 예외: %s", task, f"{type(e).__name__}: {e}"[:200])
        return empty
    if not r.ok:
        reason = (r.error or "unknown")[:200]
        if r.fallback:
            reason += f" (fallback: {r.fallback_reason})"
        logger.warning("[llm] %s 실패: %s", task, reason)
        return empty
    if json_mode:
        return r.data if isinstance(r.data, dict) else None
    return r.text or ""


# --- DART 문서 텍스트 추출 ---
EARNINGS_CACHE_DIR = os.path.join(_BD, "cache", "earnings_extract")
_DART_DOC_MAX = 8000
_AMT_UNIT_TO_EOK = {"조원": 10_000.0, "조": 10_000.0, "억원": 1.0, "억": 1.0, "백만원": 0.01, "백만": 0.01, "천원": 0.00001, "천": 0.00001, "원": 0.00000001}


def _dart_doc(rcept_no):
    """공시 원문(document.xml) 텍스트. 반환 (텍스트 ≤8,000자, 사유).
    사유: None(정상) | "DART점검"(점검 중이거나 HTML 안내 페이지 → kr_financials 차단기와 공유) | "원문없음"(아직 준비 안 됨·오류 응답·네트워크)"""
    if kr_financials.dart_is_paused():
        return "", "DART점검"
    try:
        res = requests.get(
            "https://opendart.fss.or.kr/api/document.xml",
            params={"crtfc_key": DART_API_KEY, "rcept_no": rcept_no},
            timeout=30,
        )
    except Exception:
        return "", "원문없음"
    body = res.content or b""
    head = body[:200].lstrip().lower()
    if head.startswith(b"<!doctype") or head.startswith(b"<html") or b"text/html" in (res.headers.get("Content-Type") or "").lower().encode():
        kr_financials.dart_mark_html_response("document.xml")
        return "", "DART점검"
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            for name in z.namelist():
                if name.endswith(".xml"):
                    with z.open(name) as f:
                        raw = f.read().decode("utf-8", errors="ignore")
                        text = re.sub(r"<[^>]+>", " ", raw)
                        text = re.sub(r"\s+", " ", text).strip()[:_DART_DOC_MAX]
                        return (text, None) if text else ("", "원문없음")
    except zipfile.BadZipFile:
        pass                                   # DART 가 JSON 오류({"status":"013",...}) 를 준 경우: 아직 준비 안 됨
    except Exception:
        pass
    return "", "원문없음"


def _mcap_str(stock_code):
    try:
        tc = stock_code + ".KS" if stock_code and "." not in stock_code else stock_code
        mc = yf.Ticker(tc).info.get("marketCap", 0)
        if mc >= 1e12:
            return f"{mc / 1e12:.1f}조원"
        if mc >= 1e8:
            return f"{mc / 1e8:.0f}억원"
    except Exception:
        pass
    return "-"


def _fv(v):
    return f"{v:,.0f}억" if v is not None else "-"


# --- Feature 1: 종목 모니터링 ---
def _ck_watchlist():
    cfg = load_dm_cfg()
    seen = _load_seen()
    cmap = load_dart_corp_map()
    td = date.today().strftime("%Y%m%d")
    cid = cfg["telegram_chat_ids"].get("general")
    if not cid:
        return

    chg = False
    for sc in cfg.get("watchlist", []):
        info = cmap.get(sc)
        if not info:
            continue
        try:
            page_no = 1
            while True:
                res = requests.get(
                    "https://opendart.fss.or.kr/api/list.json",
                    params={"crtfc_key": DART_API_KEY, "corp_code": info["corp_code"],
                            "bgn_de": td, "end_de": td, "page_no": page_no, "page_count": 100},
                    timeout=10,
                )
                data = res.json()
                if data.get("status") != "000":
                    break
                items = data.get("list", [])
                if not items:
                    break
                for it in items:
                    rno = it.get("rcept_no", "")
                    if rno in seen:
                        continue
                    seen.add(rno)
                    chg = True
                    title = it.get("report_nm", "")
                    msg = _watchlist_message(info["name"], title, it.get("rcept_dt", ""), rno)
                    _tg_send(TELEGRAM_TOKEN_GENERAL, cid, msg)
                    _alog("종목", f"{info['name']} - {title}")
                total_page = int(data.get("total_page", 1))
                if page_no >= total_page:
                    break
                page_no += 1
                time.sleep(0.2)
        except Exception:
            continue
    if chg:
        _save_seen(seen)


def _watchlist_message(corp_name, title, rcept_dt, rno):
    """워치리스트 새 공시 텔레그램 메시지 문자열 (전송은 호출부). 원문이 없으면 요약 줄 생략."""
    doc, _derr = _dart_doc(rno)
    summary = _llm("dart_watch_summary", f"다음 DART 공시의 핵심 내용을 3줄 이내로 간결하게 한국어로 요약해줘:\n\n{doc}") if doc else ""
    msg = (f"📢 <b>{corp_name}</b> 새 공시\n\n"
           f"📋 {title}\n📅 {rcept_dt}\n")
    if summary:
        msg += f"\n📝 {summary}\n"
    msg += f"\n🔗 <a href='https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rno}'>공시 원문</a>"
    return msg


def _fmt_amt(v):
    """억원 값 → '1.2조' / '980억' / '-210억'. None 이면 '-'"""
    if v is None:
        return "-"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(n) >= 10000:
        return f"{n/10000:.1f}조"
    return f"{n:,.0f}억"


def _pct_change(cur, prev):
    """증감률 문자열(소수 1자리). 부호가 바뀌면 적자전환/흑자전환, 둘 다 음수면 적자지속. 계산 불가면 ''"""
    try:
        c, p = float(cur), float(prev)
    except (TypeError, ValueError):
        return ""
    if p == 0:
        return ""
    if p < 0 and c < 0:
        return "적자지속"
    if p >= 0 > c:
        return "적자전환"
    if p < 0 <= c:
        return "흑자전환"
    return f"{(c - p) / abs(p) * 100:+.1f}%"


def _num_raw(v):
    """원문 숫자 문자열 → float (쉼표·공백 제거, 괄호·△ 는 음수). 없으면 None"""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).strip().replace(",", "").replace(" ", "")
    neg = t.startswith(("△", "▲", "(", "-")) and not t.startswith("-")
    t = t.strip("()△▲")
    try:
        f = float(t)
    except ValueError:
        return None
    return -abs(f) if neg else f


def _to_eok(raw, unit):
    """원문 숫자 + 단위 → 억원 (코드 환산). 단위를 모르면 None"""
    v = _num_raw(raw)
    if v is None:
        return None
    u = re.sub(r"\s+", "", str(unit or ""))
    for k in sorted(_AMT_UNIT_TO_EOK, key=len, reverse=True):
        if u.startswith(k):
            return v * _AMT_UNIT_TO_EOK[k]
    return None


def _period_label(s):
    """'2026년 3분기' / '2026.3Q' / '26.3Q' / '2026-09' → '26.3Q'. 못 알아보면 원문 앞 12자, 없으면 '-'"""
    t = str(s or "").strip()
    if not t:
        return "-"
    m = re.search(r"(20)?(\d\d)\s*년?\s*[.\-/]?\s*([1-4])\s*(?:분기|Q)", t, flags=re.I)
    if m:
        return f"{m.group(2)}.{m.group(3)}Q"
    m = re.search(r"(20)?(\d\d)[.\-/](\d{1,2})", t)
    if m:
        q = (int(m.group(3)) - 1) // 3 + 1
        return f"{m.group(2)}.{q}Q"
    m = re.search(r"(20\d\d)", t)
    if m:
        return f"FY{m.group(1)}"
    return t[:12]


def _llm_json(task, prompt):
    """JSON 모드 LLM 호출 1회 + JSON 실패 시 같은 프롬프트로 1회 재시도. 반환 (dict|None, err). err: None | 'JSON실패' | 'LLM실패:<사유>'"""
    last = "JSON실패"
    for attempt in range(2):
        try:
            r = llm_client.complete(task, prompt, json_mode=True)
        except Exception as e:
            return None, f"LLM실패:{(type(e).__name__ + ': ' + str(e))[:60]}"
        if r.ok and isinstance(r.data, dict):
            return r.data, None
        if r.error == "json_parse_failed":
            last = "JSON실패"
            continue
        return None, f"LLM실패:{(r.error or 'unknown')[:60]}"
    return None, last


def _fin_triplet(label, cur, prev, prev_year=None):
    """'매출 1.2조 (YoY +12.0%, QoQ -3.0%)' — prev_year 없으면 YoY 생략, prev 없으면 QoQ 생략"""
    parts = []
    if prev_year is not None and cur is not None:
        parts.append(f"YoY {_pct_change(cur, prev_year) or '-'}")
    if prev is not None and cur is not None:
        parts.append(f"QoQ {_pct_change(cur, prev) or '-'}")
    return f"{label} {_fmt_amt(cur)}" + (f" ({', '.join(parts)})" if parts else "")


# --- 잠정실적 추출 + 공유 캐시 (1-B) ---
def _earnings_cache_get(rcept_no):
    try:
        with open(os.path.join(EARNINGS_CACHE_DIR, f"{rcept_no}.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _earnings_cache_put(rcept_no, data):
    try:
        os.makedirs(EARNINGS_CACHE_DIR, exist_ok=True)
        tmp = os.path.join(EARNINGS_CACHE_DIR, f"{rcept_no}.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(EARNINGS_CACHE_DIR, f"{rcept_no}.json"))
    except OSError as e:
        logger.warning("[dart] earnings cache 저장 실패 %s: %s", rcept_no, type(e).__name__)


def _earnings_extract(doc, rcept_no=None):
    """잠정실적 공시 본문 → 수치. 모델은 원문 숫자(raw)·단위만 뽑고 환산(억원)은 코드가 한다.
    반환(기존 호환): {"fs_type", "period", "revenue"|"op_profit"|"net_income": {"current","prev","prev_year"}(억원), "reason", "raw_unit", "raw", "err"}.
    실패하면 None. rcept_no 가 있으면 cache/earnings_extract/<rcept_no>.json 에 저장."""
    if not doc:
        return None
    data, err = _llm_json(
        "earnings_extract",
        "다음 DART 잠정실적 공시에서 정보를 추출해. 반드시 JSON만 응답:\n"
        '{"fs_type":"연결 또는 별도",'
        '"period":"당기 열의 머리글 그대로(예: 2026년 3분기)",'
        '"unit":"표에 적힌 금액 단위 그대로(원/천원/백만원/억원/조원)",'
        '"revenue":{"current_raw":당기 매출액 원문 숫자,"prev_raw":직전분기(전기) 매출액 원문 숫자,"prev_year_raw":전년동기 매출액 원문 숫자},'
        '"op_profit":{"current_raw":당기 영업이익 원문 숫자,"prev_raw":전기 영업이익 원문 숫자,"prev_year_raw":전년동기 영업이익 원문 숫자},'
        '"net_income":{"current_raw":당기 순이익 원문 숫자,"prev_raw":전기 순이익 원문 숫자,"prev_year_raw":전년동기 순이익 원문 숫자},'
        '"reason":"변동요인 2줄 요약"}\n'
        "규칙: 숫자는 표에 적힌 그대로(쉼표만 제거, 환산 금지), 음수는 -로. '당해실적' 열만 current, '전기실적' 열은 prev, '전년동기실적' 열은 prev_year. "
        "누계실적 열은 쓰지 않는다. 없는 값은 null.\n\n"
        f"{doc}",
    )
    if not data:
        return None
    unit = data.get("unit") or ""
    out = {"fs_type": data.get("fs_type") or "-", "period": _period_label(data.get("period")), "reason": data.get("reason") or "",
           "unit": unit, "raw": {}, "raw_unit": {"unit": unit}}
    for key in ("revenue", "op_profit", "net_income"):
        node = data.get(key) or {}
        out["raw"][key] = {k: node.get(k) for k in ("current_raw", "prev_raw", "prev_year_raw")}
        out[key] = {"current": _to_eok(node.get("current_raw"), unit), "prev": _to_eok(node.get("prev_raw"), unit),
                    "prev_year": _to_eok(node.get("prev_year_raw"), unit)}
    out["raw_unit"]["revenue_current"] = (data.get("revenue") or {}).get("current_raw")
    if rcept_no:
        out["rcept_no"] = rcept_no
        out["at"] = datetime.now().isoformat(timespec="seconds")
        _earnings_cache_put(rcept_no, out)
    return out


def _earnings_data(rcept_no, doc=None):
    """캐시 우선, 없으면 (doc 받아) 추출해 캐시. 반환 (data|None, err)"""
    cached = _earnings_cache_get(rcept_no) if rcept_no else None
    if cached:
        return cached, None
    derr = None
    if doc is None:
        doc, derr = _dart_doc(rcept_no)
    if not doc:
        return None, derr or "원문없음"
    fd = _earnings_extract(doc, rcept_no)
    return (fd, None) if fd else (None, "JSON실패")


# --- 대량보유: DART majorstock.json 1순위 ---
def _majorstock_summary(corp_code, rno):
    """DART majorstock.json 에서 rcept_no 를 찾아 dict {reporter, prev, curr, reason, report_tp} 반환. 없으면 None"""
    if not corp_code or kr_financials.dart_is_paused():
        return None
    try:
        res = requests.get(
            "https://opendart.fss.or.kr/api/majorstock.json",
            params={"crtfc_key": DART_API_KEY, "corp_code": corp_code},
            timeout=15,
        )
        data = res.json()
        if data.get("status") != "000":
            return None
        hit = next((x for x in data.get("list", []) if x.get("rcept_no") == rno), None)
        if not hit:
            return None
        curr = float(hit.get("stkrt", "0"))
        irds = float(hit.get("stkrt_irds", "0"))
        return {"reporter": hit.get("repror", ""), "prev": round(curr - irds, 2), "curr": curr,
                "reason": (hit.get("report_resn") or "").lstrip("- ").strip()[:50], "purpose": "", "report_tp": hit.get("report_tp", "")}
    except Exception:
        return None


_MAJOR_PURPOSES = (("경영참가", ("경영참가", "경영권", "경영에 영향", "경영참여")), ("일반투자", ("일반투자",)), ("단순투자", ("단순투자",)))


def _major_purpose(*texts):
    """사유·목적 문구에서 보유목적 3분류 추출. 우선순위 경영참가 > 일반투자 > 단순투자, 없으면 '-'"""
    hay = " ".join(str(t or "") for t in texts)
    for label, kws in _MAJOR_PURPOSES:
        if any(k in hay for k in kws):
            return label
    return "-"


def _major_reason_clean(reason):
    """사유 문구 정제(LLM 없음): '<목적>목적(으로|의|)' 제거, 장내에서 매수→장내매수, 장외에서→장외, 공백 정리, 50자"""
    s = str(reason or "")
    s = re.sub(r"(단순|일반|경영참가|경영참여|경영권\s*(?:행사|확보)?)\s*투자?\s*목적\s*(?:으로|의|을|를|에서)?", " ", s)
    s = re.sub(r"(경영\s*참가|경영\s*참여|경영권\s*행사)\s*(?:목적)?\s*(?:으로|의)?", " ", s)
    s = re.sub(r"장내\s*에서\s*(의\s*)?매수", "장내매수", s)
    s = re.sub(r"장내\s*에서\s*(의\s*)?매도", "장내매도", s)
    s = re.sub(r"장외\s*에서", "장외", s)
    s = re.sub(r"\s+", " ", s).strip(" -·,.")
    return s[:50] if s else "-"


_MAJOR_PURPOSE_RE = re.compile(r"보유목적\s*[:：]?\s*(경영권에\s*영향을\s*주기\s*위한\s*목적|경영권\s*영향|경영\s*참가|경영\s*참여|일반\s*투자|단순\s*투자)")
_MAJOR_REPORTER_RE = re.compile(r"보고자\s*[:：]\s*(.+?)\s*(?:위\s*대리인|요약정보|보고특례)")
_MAJOR_REPORTER_RE2 = re.compile(r"성명\(명칭\)\s*한글\s*(.+?)\s*한자\(영문\)")


def _major_from_doc(doc):
    """공시 원문(태그 제거·공백 압축 텍스트)에서 보유목적·보고자를 정규식으로 추출. 반환 {"purpose", "reporter"} (없는 키는 생략). LLM 없음.
    '보유목적이 발행인의 경영권…' 같은 안내문은 '보유목적' 뒤에 조사가 붙어 패턴에 걸리지 않는다."""
    out = {}
    if not doc:
        return out
    m = _MAJOR_PURPOSE_RE.search(doc)
    if m:
        out["purpose"] = _major_purpose(m.group(1))
    m = _MAJOR_REPORTER_RE.search(doc)
    name = re.sub(r"\s+", " ", m.group(1)).strip(" -:") if m else ""
    if not name or len(name) > 80:
        m2 = _MAJOR_REPORTER_RE2.search(doc)
        name = re.sub(r"\s+", " ", m2.group(1)).strip(" -:") if m2 else ""
    if name and len(name) <= 80:
        out["reporter"] = name
    return out


def _major_needs_purpose(item):
    """대량보유 요약이 있는데 목적이 '-' 로 저장된 건(원문 지연 등) — 6시간 창 안에서 다시 시도"""
    s = item.get("summary") or ""
    return s.startswith("[대량보유]") and "목적: -" in s


def _fmt_major(d):
    pv, cv = d.get("prev"), d.get("curr")
    ratio = "-"
    if pv is not None and cv is not None:
        ratio = f"{float(pv):.2f}% → {float(cv):.2f}% ({float(cv) - float(pv):+.2f}%p)"
    elif cv is not None:
        ratio = f"{float(cv):.2f}%"
    purpose = _major_purpose(d.get("purpose"), d.get("reason"))
    s = f"[대량보유] 보고자: {d.get('reporter') or '-'} | {ratio} | 사유: {_major_reason_clean(d.get('reason'))} | 목적: {purpose}"
    if d.get("report_tp"):
        s += f" ({d['report_tp']})"
    return s


def _dart_kind(title):
    t = title or ""
    if "잠정실적" in t or "영업(잠정)" in t:
        return "잠정실적"
    if "공급계약" in t:
        return "공급계약"
    if "대량보유" in t:
        return "대량보유"
    if "손익구조" in t:
        return "손익구조변경"
    if "전망" in t:
        return "전망"
    return "기타"


def _dart_summary(title, rno, corp_code=""):
    """공시 1건 → (요약, 사유). 요약은 '[종류] 항목 | 항목 | …' 고정 틀, 값이 없으면 '-'.
    실패하면 ("", err) — err ∈ "원문없음" / "DART점검" / "LLM실패:<사유>" / "JSON실패". 자유 텍스트 폴백 없음."""
    kind = _dart_kind(title)

    if kind == "대량보유":
        d = _majorstock_summary(corp_code, rno)
        doc, derr = _dart_doc(rno)                       # 보유목적·보고자(공백 복원)는 원문 정규식이 1순위
        extra = _major_from_doc(doc)
        if d:
            if extra.get("purpose") and extra["purpose"] != "-":
                d["purpose"] = extra["purpose"]
            if extra.get("reporter"):
                d["reporter"] = extra["reporter"]
            return _fmt_major(d), None                    # 원문이 없어도 majorstock 만으로 요약 (summary_err 없음)
        if not doc:
            return "", derr or "원문없음"
        data, err = _llm_json(
            "dart_detail",
            "다음 DART 대량보유 공시에서 정보를 추출해. 반드시 JSON만 응답:\n"
            '{"reporter":"보고자","prev_ratio":직전보유비율숫자,"curr_ratio":이번보유비율숫자,"reason":"보고사유(30자 이내)","purpose":"보유목적","report_tp":"약식 또는 일반 또는 null"}\n'
            f"숫자는 퍼센트, 없으면 null.\n\n{doc}",
        )
        if not data:
            return "", err
        return _fmt_major({"reporter": extra.get("reporter") or data.get("reporter"), "prev": _num_raw(data.get("prev_ratio")), "curr": _num_raw(data.get("curr_ratio")),
                           "reason": data.get("reason"), "purpose": extra.get("purpose") if extra.get("purpose") not in (None, "-") else data.get("purpose"),
                           "report_tp": data.get("report_tp") or ""}), None

    if kind == "잠정실적":
        fd, err = _earnings_data(rno)
        if not fd:
            return "", err
        parts = [f"[잠정실적] {fd.get('period') or '-'} {fd.get('fs_type') or '-'}"]
        for label, key in (("매출", "revenue"), ("영업익", "op_profit"), ("순익", "net_income")):
            n = fd.get(key) or {}
            parts.append(_fin_triplet(label, n.get("current"), n.get("prev"), n.get("prev_year")))
        return " | ".join(parts), None

    doc, derr = _dart_doc(rno)
    if not doc:
        return "", derr or "원문없음"

    if kind == "공급계약":
        data, err = _llm_json(
            "dart_detail",
            "다음 DART 단일판매·공급계약 공시에서 정보를 추출해. 반드시 JSON만 응답:\n"
            '{"counterparty":"계약상대방(비공개면 \\"비공개\\")","amount_raw":계약금액 원문 숫자,"amount_unit":"계약금액 단위(원/천원/백만원/억원)",'
            '"recent_revenue_raw":최근 매출액 원문 숫자,"recent_revenue_unit":"단위","pct":원문 표의 매출액 대비 비율 숫자(없으면 null),'
            '"start_date":"계약 시작일 YYYY-MM-DD","end_date":"계약 종료일 YYYY-MM-DD","subject":"계약 내용(20자 이내)"}\n'
            f"숫자는 표에 적힌 그대로(쉼표만 제거, 환산 금지). 없는 값은 null.\n\n{doc}",
        )
        if not data:
            return "", err
        amt = _to_eok(data.get("amount_raw"), data.get("amount_unit"))
        rev = _to_eok(data.get("recent_revenue_raw"), data.get("recent_revenue_unit"))
        pct = _num_raw(data.get("pct"))
        if pct is None and amt is not None and rev:
            pct = amt / rev * 100
        sd, ed = str(data.get("start_date") or "")[2:10].replace("-", "-"), str(data.get("end_date") or "")[2:10]
        period = f"{sd}~{ed}" if sd and ed else (sd or ed or "-")
        return (f"[공급계약] 상대: {data.get('counterparty') or '-'} | {_fmt_amt(amt)}"
                + (f" (최근매출 대비 {pct:.1f}%)" if pct is not None else " (최근매출 대비 -)")
                + f" | 기간 {period} | 내용: {(data.get('subject') or '-')[:20]}"), None

    if kind == "손익구조변경":
        data, err = _llm_json(
            "dart_detail",
            "다음 DART 매출액 또는 손익구조 30% 이상 변경 공시에서 정보를 추출해. 반드시 JSON만 응답:\n"
            '{"fs_type":"연결 또는 별도","fiscal_year":"해당 사업연도(예: 2025)","unit":"표의 금액 단위(원/천원/백만원/억원)",'
            '"revenue":{"current_raw":당해 매출액 원문 숫자,"prev_raw":직전 사업연도 매출액 원문 숫자},'
            '"op_profit":{"current_raw":당해 영업이익 원문 숫자,"prev_raw":직전 영업이익 원문 숫자},'
            '"net_income":{"current_raw":당해 당기순이익 원문 숫자,"prev_raw":직전 당기순이익 원문 숫자},'
            '"reason":"변동 주요원인(30자 이내)"}\n'
            f"숫자는 표에 적힌 그대로(쉼표만 제거, 환산 금지), 음수는 -로. 없는 값은 null.\n\n{doc}",
        )
        if not data:
            return "", err
        unit = data.get("unit") or ""
        fy = str(data.get("fiscal_year") or "").strip()
        fy = f"FY{fy}" if fy and not fy.upper().startswith("FY") else (fy or "-")
        parts = [f"[손익구조변경] {fy} {data.get('fs_type') or '-'}"]
        for label, key in (("매출", "revenue"), ("영업익", "op_profit"), ("순익", "net_income")):
            node = data.get(key) or {}
            cur, prev = _to_eok(node.get("current_raw"), unit), _to_eok(node.get("prev_raw"), unit)
            chg = _pct_change(cur, prev) if cur is not None and prev is not None else ""
            parts.append(f"{label} {_fmt_amt(cur)}" + (f" ({chg})" if chg else ""))
        parts.append(f"원인: {(data.get('reason') or '-')[:30]}")
        return " | ".join(parts), None

    # 전망 / 기타: 자유 1줄 요약 + 접두
    text = _llm("dart_detail", f"다음 DART 공시의 핵심 내용을 1줄로 요약(숫자는 원문 표기 그대로):\n{doc}")
    if not text:
        return "", "LLM실패:빈 응답"
    return f"[{'전망' if kind == '전망' else '기타'}] {text.strip()}", None


# --- Feature 2: 잠정실적 ---
def _earnings_message(it, fd):
    """잠정실적 텔레그램 메시지 문자열 (전송은 호출부)"""
    corp_name, stock_code, title, rno = it.get("corp_name", ""), it.get("stock_code", ""), it.get("report_nm", ""), it.get("rcept_no", "")
    mc = _mcap_str(stock_code)
    msg = "📊 <b>잠정실적 공시 알림</b>\n\n"
    msg += f"📅 공시일시: {it.get('rcept_dt', '')}\n"
    msg += f"🏢 기업명: <b>{corp_name}</b> (시총: {mc})\n"
    msg += f"📋 보고서: {title}\n"
    if fd:
        msg += f"📑 재무제표: {fd.get('fs_type', '-')}" + (f" · {fd.get('period')}" if fd.get("period") and fd.get("period") != "-" else "") + "\n\n"
        for label, key in (("매출액", "revenue"), ("영업이익", "op_profit"), ("순이익", "net_income")):
            n = fd.get(key) or {}
            msg += f"💰 {label}: {_fv(n.get('current'))} (전기: {_fv(n.get('prev'))}" + (f", 전년동기: {_fv(n.get('prev_year'))}" if n.get("prev_year") is not None else "") + ")\n"
        reason = fd.get("reason", "")
        if reason:
            msg += f"\n📝 변동요인: {reason}\n"
    msg += f"\n🔗 <a href='https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rno}'>DART 공시 원문</a>"
    if stock_code:
        msg += f"\n🔗 <a href='https://finance.naver.com/item/main.naver?code={stock_code}'>네이버 회사정보</a>"
    return msg


def _ck_earnings():
    cfg = load_dm_cfg()
    seen = _load_seen()
    td = date.today().strftime("%Y%m%d")
    cid = cfg["telegram_chat_ids"].get("earnings")
    if not cid:
        return
    conds = cfg.get("earnings_conditions", {})

    chg = False
    try:
        page_no = 1
        while True:
            res = requests.get(
                "https://opendart.fss.or.kr/api/list.json",
                params={"crtfc_key": DART_API_KEY, "bgn_de": td, "end_de": td,
                        "pblntf_ty": "B", "page_no": page_no, "page_count": 100},
                timeout=10,
            )
            data = res.json()
            if data.get("status") != "000":
                break
            items = data.get("list", [])
            if not items:
                break
            for it in items:
                title = it.get("report_nm", "")
                if "잠정실적" not in title and "영업(잠정)" not in title:
                    continue
                rno = it.get("rcept_no", "")
                key = f"e_{rno}"
                if key in seen:
                    continue
                seen.add(key)
                chg = True

                fd, _err = _earnings_data(rno)        # 캐시 우선, 없으면 추출 후 캐시 (1-B)

                if fd:
                    rev = fd.get("revenue") or {}
                    op = fd.get("op_profit") or {}
                    rg = (rev.get("current") or 0) > (rev.get("prev") or 0) if rev.get("current") and rev.get("prev") else False
                    og = (op.get("current") or 0) > (op.get("prev") or 0) if op.get("current") and op.get("prev") else False
                    ct = conds.get("condition_type", "or")
                    cr = conds.get("revenue_growth", True)
                    co = conds.get("op_profit_growth", True)
                    passed = ((cr and rg) or (co and og)) if ct == "or" else ((not cr or rg) and (not co or og))
                    if not passed:
                        continue

                _tg_send(TELEGRAM_TOKEN_EARNINGS, cid, _earnings_message(it, fd))
                _alog("잠정", f"{it.get('corp_name', '')} - {title}")
            total_page = int(data.get("total_page", 1))
            if page_no >= total_page:
                break
            page_no += 1
            time.sleep(0.2)
    except Exception:
        pass
    if chg:
        _save_seen(seen)


# --- Feature 3: 주요 공시 ---
_MAJOR_KW = ["대량보유", "실적", "매출액", "공급계약"]
_MAJOR_EXCLUDE = ["증권발행실적보고서", "결산실적공시예고"]   # 대량보유 일반보고서는 경영참가 목적 5% 신고가 담겨 수집 대상 (2026-10-29)


def _major_excluded(title):
    """제목이 _MAJOR_EXCLUDE 에 걸리면 True — 수집(_ck_major)·표시(/api/dart/daily)·슬롯·엑셀이 같은 목록을 쓴다"""
    t = title or ""
    return any(ex in t for ex in _MAJOR_EXCLUDE)


MAJOR_MIN_MCAP_WON = 5_000 * 1e8      # 기본 5,000억원. dart_monitor_config.json 의 major_min_mcap_eok 로 변경 가능


def _major_min_mcap_won():
    try:
        v = load_dm_cfg().get("major_min_mcap_eok")
        return float(v) * 1e8 if v is not None else MAJOR_MIN_MCAP_WON
    except (TypeError, ValueError):
        return MAJOR_MIN_MCAP_WON


def _major_mcap_reason(stock_code):
    """시총 판정 사유: "ok" | "비상장" | "마스터 없음" | "기준 미달" | "마스터 장애"(필터 건너뜀)"""
    code = (stock_code or "").strip()
    if not code:
        return "비상장"
    try:
        import stock_profile
        master = stock_profile._kr_master()
    except Exception as e:
        print(f"[dart] 시총 마스터 조회 실패({type(e).__name__}) — 시총 필터 건너뜀", flush=True)
        return "마스터 장애"
    if not master:
        print("[dart] 시총 마스터 비어 있음 — 시총 필터 건너뜀", flush=True)
        return "마스터 장애"
    m = master.get(code)
    if not m:
        return "마스터 없음"
    cap = m.get("prev_cap")
    if cap is None:
        return "마스터 없음"
    return "ok" if cap >= _major_min_mcap_won() else "기준 미달"


def _major_mcap_ok(stock_code):
    """시가총액 기준(전일, KIS 종목 마스터) 통과 여부. 마스터 장애면 True(필터 건너뛰고 수집 계속)."""
    return _major_mcap_reason(stock_code) in ("ok", "마스터 장애")


def _major_visible(items):
    """표시 필터(/api/dart/daily·텔레그램 슬롯·엑셀 공용): 제외어 + 시총 기준. history 원본은 지우지 않음"""
    return [it for it in (items or []) if not _major_excluded(it.get("title")) and _major_mcap_ok(it.get("stock_code"))]


_MAJOR_RETRY_PER_LOOP = 30
_MAJOR_RETRY_HOURS = 6


def _major_retry_due(item, day=None):
    """접수(rcept_dt, 당일 공시는 detected_at) 후 6시간 안이면 재시도 대상"""
    day = day or date.today().isoformat()
    try:
        rd = str(item.get("rcept_dt") or "")
        if len(rd) >= 8 and rd[:8] != date.today().strftime("%Y%m%d"):
            base = datetime.strptime(rd[:8], "%Y%m%d").replace(hour=18)       # 지난 날짜 공시: 그날 18시 접수로 간주
        else:
            base = datetime.strptime(f"{day} {item.get('detected_at') or '00:00'}", "%Y-%m-%d %H:%M")
    except ValueError:
        return True
    return datetime.now() - base < timedelta(hours=_MAJOR_RETRY_HOURS)


_resummarize_lock = threading.Lock()
_resummarize_state = {"running": False, "started_at": None, "done": 0, "total": 0, "ok": 0, "last_finished": None}


def resummarize_item(item):
    """history 항목 1건 요약 재생성(덮어쓰기). 반환 (summary, err)"""
    cc = item.get("corp_code", "")
    if not cc and item.get("stock_code"):
        entry = load_dart_corp_map().get(item["stock_code"], {})
        cc = entry.get("corp_code", "") if isinstance(entry, dict) else str(entry)
        item["corp_code"] = cc
    s, err = _dart_summary(item.get("title", ""), item.get("rcept_no", ""), corp_code=cc)
    item["summary"] = s
    item["summary_err"] = None if s else (err or "원문없음")
    item["summary_last_try"] = datetime.now().strftime("%m-%d %H:%M")
    item["summary_tries"] = int(item.get("summary_tries") or 0) + 1
    return s, err


def _resummarize_days(n_days):
    """최근 n_days 의 history 전체를 다시 요약(백그라운드). 하루 단위로 저장."""
    try:
        h = _load_daily_history()
        cutoff = (date.today() - timedelta(days=n_days - 1)).isoformat()
        targets = [(d, it) for d, its in sorted(h.get("days", {}).items()) if d >= cutoff for it in its]
        _resummarize_state.update(total=len(targets), done=0, ok=0)
        for d, it in targets:
            try:
                s, _e = resummarize_item(it)
                _resummarize_state["ok"] += 1 if s else 0
            except Exception as e:
                it["summary_err"] = f"LLM실패:{type(e).__name__}"[:70]
            _resummarize_state["done"] += 1
            if _resummarize_state["done"] % 10 == 0:
                _save_daily_history(h)
            time.sleep(0.3)
        _save_daily_history(h)
        print(f"[DART] 요약 재생성 완료: {_resummarize_state['ok']}/{_resummarize_state['total']}건 (days={n_days})", flush=True)
    finally:
        _resummarize_state.update(running=False, last_finished=datetime.now().isoformat(timespec="seconds"))


def _ck_major():
    seen = _load_seen()
    daily = _load_daily()
    td = date.today().strftime("%Y%m%d")
    ti = date.today().isoformat()
    if daily.get("date") != ti:
        daily = {"date": ti, "items": []}

    chg = False
    try:
        page_no = 1
        while True:
            res = requests.get(
                "https://opendart.fss.or.kr/api/list.json",
                params={"crtfc_key": DART_API_KEY, "bgn_de": td, "end_de": td,
                        "page_no": page_no, "page_count": 100},
                timeout=10,
            )
            data = res.json()
            if data.get("status") != "000":
                break
            items = data.get("list", [])
            if not items:
                break
            for it in items:
                title = it.get("report_nm", "")
                if not any(k in title for k in _MAJOR_KW):
                    continue
                if _major_excluded(title):
                    continue
                rno = it.get("rcept_no", "")
                key = f"m_{rno}"
                if key in seen:
                    continue
                stock_code = it.get("stock_code", "")
                if not _major_mcap_ok(stock_code):      # 시총 기준 미달·비상장: seen 에도 넣지 않음 (기준을 낮추면 다시 잡히도록), 요약 LLM 호출 전
                    continue
                seen.add(key)
                chg = True
                corp_name = it.get("corp_name", "")
                it_corp_code = it.get("corp_code", "")
                if not it_corp_code and stock_code:
                    cmap = load_dart_corp_map()
                    entry = cmap.get(stock_code, {})
                    it_corp_code = entry.get("corp_code", "") if isinstance(entry, dict) else str(entry)
                summary, serr = _dart_summary(title, rno, corp_code=it_corp_code)
                daily["items"].append({
                    "corp_name": corp_name,
                    "stock_code": stock_code,
                    "corp_code": it_corp_code,
                    "title": title, "rcept_no": rno,
                    "rcept_dt": it.get("rcept_dt", ""),
                    "detected_at": datetime.now().strftime("%H:%M"),
                    "summary": summary,
                    "summary_err": None if summary else (serr or "원문없음"),
                    "summary_tries": 0 if summary or serr == "DART점검" else 1,
                })
                _alog("주요", f"{corp_name} - {title}")
            total_page = int(data.get("total_page", 1))
            if page_no >= total_page:
                break
            page_no += 1
            time.sleep(0.2)
    except Exception:
        pass

    retried = 0
    for item in daily.get("items", []):
        if retried >= _MAJOR_RETRY_PER_LOOP:
            break
        if item.get("summary") and not _major_needs_purpose(item):
            continue
        if not _major_retry_due(item, daily.get("date")):
            continue                                   # 접수 후 6시간이 지난 건은 포기 (수동 재생성 API 로만)
        if kr_financials.dart_is_paused():
            item["summary_err"] = "DART점검"            # 점검 중엔 시도도, tries 증가도 하지 않는다
            chg = True
            continue
        cc = item.get("corp_code", "")
        if not cc and item.get("stock_code"):
            cmap = load_dart_corp_map()
            entry = cmap.get(item["stock_code"], {})
            cc = entry.get("corp_code", "") if isinstance(entry, dict) else str(entry)
            item["corp_code"] = cc
        tries = int(item.get("summary_tries") or 0)
        try:
            s, serr = _dart_summary(item.get("title", ""), item.get("rcept_no", ""), corp_code=cc)
            if s:
                item["summary"], item["summary_err"] = s, None
                if _major_needs_purpose(item):
                    item["summary_tries"] = tries + 1      # 목적 '-' 그대로면 시도 횟수만 올리고 다음 루프에 재시도
            else:
                item["summary_err"] = serr or "원문없음"
                if serr != "DART점검":
                    item["summary_tries"] = tries + 1
        except Exception as e:
            item["summary_err"] = f"LLM실패:{type(e).__name__}"[:70]
            item["summary_tries"] = tries + 1
        item["summary_last_try"] = datetime.now().strftime("%H:%M")
        chg = True
        retried += 1
        time.sleep(0.5)

    if retried:
        chg = True

    if chg:
        _save_seen(seen)
        _save_daily(daily)


_DAILY_SLOTS = [
    {"hour": 11, "key": "11", "start": "00:00", "end": "10:59",
     "header": "📋 <b>오전 공시 정리 (00:00~11:00)</b>"},
    {"hour": 14, "key": "14", "start": "11:00", "end": "13:59",
     "header": "📋 <b>오후 공시 정리 (11:00~14:00)</b>"},
    {"hour": 18, "key": "18", "start": "14:00", "end": "17:59",
     "header": "📋 <b>오후 공시 정리 (14:00~18:00)</b>"},
    {"hour": 22, "key": "22", "start": None, "end": None,
     "header": None},
]


def _send_daily_slot(slot):
    cfg = load_dm_cfg()
    daily = _load_daily()
    cid = cfg["telegram_chat_ids"].get("general")
    if not cid or not daily.get("items"):
        return
    if slot["start"] is not None:
        filtered = [it for it in _major_visible(daily["items"])
                    if slot["start"] <= it.get("detected_at", "00:00") <= slot["end"]]
        if not filtered:
            return
        header = slot["header"]
    else:
        filtered = _major_visible(daily["items"])
        if not filtered:
            return
        header = f"📋 <b>오늘의 전체 공시 정리 ({date.today().isoformat()})</b>"
    msg = f"{header}\n\n"
    msg += _daily_slot_body(filtered)
    _tg_send(TELEGRAM_TOKEN_GENERAL, cid, msg)
    _alog("일일", f"{slot['key']}시 공시 {len(filtered)}건 전송")


def _daily_slot_body(items):
    """슬롯 메시지 본문: 번호·종목·제목·(요약)·링크. 요약이 비어 있으면 그 줄 생략. 분할은 _tg_send 가 처리."""
    import html as _html
    out = ""
    for i, it in enumerate(items, 1):
        out += f"{i}. <b>{_html.escape(it.get('corp_name') or '')}</b>\n   📋 {_html.escape(it.get('title') or '')}\n"
        if (it.get("summary") or "").strip():
            out += f"   📝 {_html.escape(it['summary'].strip())}\n"
        out += f"   🔗 https://dart.fss.or.kr/dsaf001/main.do?rcpNo={it.get('rcept_no', '')}\n\n"
    return out


def _save_excel():
    import shutil
    from openpyxl import Workbook, load_workbook
    daily = _load_daily()
    if not daily.get("items"):
        return

    # 항상 로컬에 저장
    try:
        wb = load_workbook(EXCEL_PATH_LOCAL)
        ws = wb.active
    except Exception:
        wb = Workbook()
        ws = wb.active
        ws.title = "DART 공시"
        ws.append(["날짜", "종목명", "공시사유", "섹터", "시총", "매출액", "영업이익", "공시내용", "영익대비시총"])

    for it in _major_visible(daily["items"]):
        sc = it.get("stock_code", "")
        mc_s = rev_s = op_s = sector = ""
        try:
            tc = sc + ".KS" if sc and "." not in sc else sc
            if tc:
                inf = yf.Ticker(tc).info
                mc = inf.get("marketCap", 0)
                mc_s = f"{mc / 1e8:.0f}억" if mc else ""
                rv = inf.get("totalRevenue", 0)
                rev_s = f"{rv / 1e8:.0f}억" if rv else ""
                oi = inf.get("operatingIncome") or inf.get("ebitda", 0)
                op_s = f"{oi / 1e8:.0f}억" if oi else ""
                sector = inf.get("sector", "")
        except Exception:
            pass
        if not sector:
            for s in MARKET_STOCKS.get("kr", []):
                if sc and sc in s[0]:
                    sector = s[2]
                    break
        doc = _dart_doc(it.get("rcept_no", ""))
        summary = ""
        if doc:
            _p = f"다음 DART 공시의 핵심 내용을 1줄로 요약:\n{doc[:3000]}"
            summary = _llm("dart_excel_line", _p)                       # Codex(gpt-6-luna) 1차
            if not summary:
                logger.warning("[llm] dart_excel_line 실패 → gemini 폴백: %s", it.get("corp_name", ""))
                summary = _gemini(_p)
        ratio = ""
        try:
            if mc_s and op_s:
                mcv = float(mc_s.replace("억", "").replace(",", ""))
                opv = float(op_s.replace("억", "").replace(",", ""))
                if opv > 0:
                    ratio = f"{mcv / opv:.1f}배"
        except Exception:
            pass
        ws.append([it.get("rcept_dt", ""), it.get("corp_name", ""), it.get("title", ""),
                   sector, mc_s, rev_s, op_s, summary[:200], ratio])

    try:
        wb.save(EXCEL_PATH_LOCAL)
        _alog("엑셀", f"공시 {len(daily['items'])}건 로컬 저장 완료")
    except Exception as e:
        _alog("엑셀", f"로컬 저장 실패: {e}")
        return

    # Google Drive로 복사
    try:
        if not EXCEL_PATH:
            raise FileNotFoundError("drive mount not found")
        os.makedirs(os.path.dirname(EXCEL_PATH), exist_ok=True)
        shutil.copy2(EXCEL_PATH_LOCAL, EXCEL_PATH)
        _alog("엑셀", "Google Drive 복사 완료")
    except Exception as e:
        _alog("엑셀", f"Google Drive 복사 실패 (로컬 저장은 완료): {e}")


# --- Background monitor ---
def _dart_loop():
    time.sleep(10)
    while True:
        if datetime.now().weekday() >= 5:
            time.sleep(300)
            continue

        try:
            cfg = load_dm_cfg()
            if cfg.get("monitor_enabled", True):
                _ck_watchlist()
                _ck_earnings()
                _ck_major()
                now = datetime.now()
                today_iso = now.date().isoformat()
                sent_state = _load_sent()
                if sent_state.get("date") != today_iso:
                    sent_state = {"date": today_iso, "sent": []}
                cfg = load_dm_cfg()
                tg_on = cfg.get("digest_telegram_enabled", False)
                for slot in _DAILY_SLOTS:
                    if now.hour == slot["hour"] and slot["key"] not in sent_state["sent"]:
                        if tg_on:
                            _send_daily_slot(slot)
                        sent_state["sent"].append(slot["key"])
                        _save_sent(sent_state)
                        if slot["key"] == "22":
                            _save_excel()
        except Exception as e:
            print(f"[DART Monitor] {e}")
        time.sleep(300)


def _start_monitor():
    global _monitor_thread
    if _monitor_thread and _monitor_thread.is_alive():
        return
    _monitor_thread = threading.Thread(target=_dart_loop, daemon=True)
    _monitor_thread.start()




# --- DART Daily API ---
@bp.route("/api/dart/daily", methods=["GET"])
def get_dart_daily():
    days_param = request.args.get("days")
    if days_param:
        try:
            n = int(days_param)
        except ValueError:
            n = 1
        h = _load_daily_history()
        all_days = h.get("days", {})
        cutoff = (date.today() - timedelta(days=n - 1)).isoformat()
        items = []
        for d, d_items in all_days.items():
            if d >= cutoff:
                for it in _major_visible(d_items):
                    it_copy = dict(it)
                    it_copy["date"] = d
                    items.append(it_copy)
        return jsonify({"days": n, "items": items})
    dt = request.args.get("date", date.today().isoformat())
    h = _load_daily_history()
    items = _major_visible(h.get("days", {}).get(dt, []))
    return jsonify({"date": dt, "items": items})


@bp.route("/api/dart/dates", methods=["GET"])
def get_dart_dates():
    h = _load_daily_history()
    dates = sorted(h.get("days", {}).keys(), reverse=True)
    return jsonify({"dates": dates})


# --- DART Monitor API ---
@bp.route("/api/dart/monitor/config", methods=["GET"])
def get_dm_cfg():
    return jsonify(load_dm_cfg())


@bp.route("/api/dart/monitor/config", methods=["POST"])
def set_dm_cfg():
    old_wl = load_dm_cfg().get("watchlist", [])
    new_cfg = request.get_json()
    new_wl = new_cfg.get("watchlist", [])
    save_dm_cfg(new_cfg)
    added = [c for c in new_wl if c not in old_wl]
    removed = [c for c in old_wl if c not in new_wl]
    # 워치리스트는 통합본에서 고친 뒤 세 파일에 반영 (DART 파일의 watchlist 도 통합본 기준으로 다시 맞춰짐)
    _sync_dart_to_others(added, removed)
    return jsonify({"ok": True})


@bp.route("/api/dart/monitor/status", methods=["GET"])
def get_dm_status():
    cfg = load_dm_cfg()
    return jsonify({
        "running": _monitor_thread is not None and _monitor_thread.is_alive(),
        "enabled": cfg.get("monitor_enabled", True),
    })


@bp.route("/api/dart/monitor/log", methods=["GET"])
def get_dm_log():
    return jsonify(_dart_alert_log[-20:])


@bp.route("/api/dart/telegram/test", methods=["POST"])
def test_dm_tg():
    body = request.get_json()
    tt = body.get("type", "general")
    token = TELEGRAM_TOKEN_GENERAL if tt == "general" else TELEGRAM_TOKEN_EARNINGS
    cid = _tg_detect(token)
    if not cid:
        return jsonify({"ok": False, "error": "봇에 먼저 /start 메시지를 보내주세요"})
    cfg = load_dm_cfg()
    cfg["telegram_chat_ids"][tt] = cid
    save_dm_cfg(cfg)
    ok = _tg_send(token, cid, "✅ 텔레그램 연결 성공! DART 공시 알림이 활성화됩니다.")
    return jsonify({"ok": ok, "chat_id": cid})


@bp.route("/api/dart/monitor/toggle", methods=["POST"])
def toggle_dm():
    cfg = load_dm_cfg()
    cfg["monitor_enabled"] = not cfg.get("monitor_enabled", True)
    save_dm_cfg(cfg)
    if cfg["monitor_enabled"]:
        _start_monitor()
    return jsonify({"ok": True, "enabled": cfg["monitor_enabled"]})


@bp.route("/api/dart/daily/resummarize", methods=["POST"])
def dart_daily_resummarize():
    """공시 요약 재생성. body {"rcept_no": "…"} → 그 건만 동기 처리 후 결과 반환, {"days": N} → 기간 전체를 백그라운드로(즉시 {"ok","queued"}). 기존 요약도 덮어쓴다."""
    body = request.get_json(silent=True) or {}
    rno = str(body.get("rcept_no") or "").strip()
    if rno:
        h = _load_daily_history()
        for d, its in h.get("days", {}).items():
            for it in its:
                if str(it.get("rcept_no")) == rno:
                    s, err = resummarize_item(it)
                    _save_daily_history(h)
                    return jsonify({"ok": bool(s), "rcept_no": rno, "date": d, "summary": s, "summary_err": None if s else err, "title": it.get("title")})
        return jsonify({"ok": False, "error": "not_found", "rcept_no": rno}), 404
    try:
        n = max(1, min(int(body.get("days") or 1), 14))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "days 는 정수"}), 400
    with _resummarize_lock:
        if _resummarize_state["running"]:
            return jsonify({"ok": False, "error": "already_running", "state": _resummarize_state}), 409
        h = _load_daily_history()
        cutoff = (date.today() - timedelta(days=n - 1)).isoformat()
        queued = sum(len(its) for d, its in h.get("days", {}).items() if d >= cutoff)
        _resummarize_state.update(running=True, started_at=datetime.now().isoformat(timespec="seconds"), done=0, total=queued, ok=0)
        threading.Thread(target=_resummarize_days, args=(n,), daemon=True).start()
    return jsonify({"ok": True, "queued": queued, "days": n})


@bp.route("/api/dart/daily/resummarize", methods=["GET"])
def dart_daily_resummarize_state():
    return jsonify(_resummarize_state)


@bp.route("/api/dart/daily/trigger", methods=["POST"])
def trigger_daily():
    """수동으로 전체 공시 정리 + 엑셀 저장을 실행"""
    try:
        full_slot = _DAILY_SLOTS[-1]
        _send_daily_slot(full_slot)
        _save_excel()
        return jsonify({"ok": True, "msg": "전체 공시 정리 및 엑셀 저장 완료"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
