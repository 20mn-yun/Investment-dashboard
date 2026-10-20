import json
import unicodedata
import re
import os
import time
import threading
import asyncio
import uuid
import sqlite3
import shutil
import subprocess
import requests
from datetime import datetime, timezone

from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaDocument, DocumentAttributeFilename
import logging
import llm_client

logger = logging.getLogger("telegram_report")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

CONFIG_PATH = "report_config.json"
REPORT_DB = "report_index.db"

_DRIVE_ROOT_NAMES = ("내 드라이브", "My Drive")


def find_drive_root():
    """구글 드라이브 데스크톱 마운트의 루트 폴더 경로를 탐색한다.

    ~/Library/CloudStorage/GoogleDrive-*/ 중 첫 마운트 아래 '내 드라이브' 또는
    'My Drive' 중 실재하는 쪽을 반환. 못 찾으면 None. (계정 이메일 하드코딩 방지)
    """
    import glob
    for mount in sorted(glob.glob(os.path.expanduser("~/Library/CloudStorage/GoogleDrive-*"))):
        for root in _DRIVE_ROOT_NAMES:
            base = os.path.join(mount, root)
            if os.path.isdir(base):
                return base
    return None


def drive_path(*parts):
    """드라이브 루트 아래 상대 경로를 절대 경로로. 마운트 없으면 None."""
    root = find_drive_root()
    return os.path.join(root, *parts) if root else None


DEFAULT_CONFIG = {
    "channels": [],
    "download_base": drive_path("Analysis") or "~/Downloads/Analysis",
    "watchlist": [],
    "personal_channels": [],
    "forward_enabled": True,
}

BROKERAGES = [
    "현대차증권", "미래에셋증권", "한국투자증권", "삼성증권", "NH투자증권",
    "KB증권", "신한투자증권", "키움증권", "하나증권", "메리츠증권",
    "대신증권", "유안타증권", "한화투자증권", "교보증권", "하이투자증권",
    "DB금융투자", "신영증권", "IBK투자증권", "SK증권", "유진투자증권",
    "다올투자증권", "LS증권", "이베스트투자증권", "부국증권", "BNK투자증권",
    "상상인증권", "흥국증권", "케이프투자증권",
]


def _mask_brokerages(text, watchlist):
    wl_lower = [w.lower() for w in watchlist]
    for b in BROKERAGES:
        bl = b.lower()
        if bl in wl_lower:
            continue
        text = text.replace(bl, " ")
    return text


def _gemini_yes_no(prompt):
    last = ""
    for attempt in range(3):
        try:
            res = requests.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent"
                f"?key={GEMINI_API_KEY}",
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=30,
            )
            if res.status_code == 200:
                text = res.json()["candidates"][0]["content"]["parts"][0]["text"]
                return text.strip().upper()
            last = f"status={res.status_code} {res.text[:120]}"
            print(f"[Report AI필터] gemini 실패 status={res.status_code} 시도={attempt+1}/3", flush=True)
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            print(f"[Report AI필터] gemini 예외 {type(e).__name__} 시도={attempt+1}/3", flush=True)
        time.sleep(2)
    logger.warning("[gemini] _gemini_yes_no 실패: 3회 시도 후 포기 — %s", last[:200])
    return ""


def _ai_relevance(filename, caption, watchlist):
    return _ai_relevance_detail(filename, caption, watchlist)[0]


def _relevance_prompt(filename, caption, watchlist):
    wl = ", ".join(watchlist)
    prompt = (
        "너는 증권사 리포트 필터다. 아래는 텔레그램 채널에 올라온 리포트의 파일명과 메시지 본문 일부다.\n\n"
        f"관심 종목 목록: {wl}\n\n"
        f"파일명: {filename}\n"
        f"본문: {caption[:500]}\n\n"
        "판정 규칙을 순서대로 적용해라.\n\n"
        "규칙 1 (최우선, 무조건 NO): 다음 유형의 리포트는 관심 종목이나 그 산업이 제목·본문에 아무리 비중 있게 나와도 무조건 NO 다.\n"
        "- 시황·마감시황·데일리·모닝브리프 등 정기 시장 요약\n"
        "- 위클리·주간전망·월간전망·연간전망 등 정기 전망\n"
        "- 투자전략·자산배분·포트폴리오 전략·퀀트 전략\n"
        "- 파생상품·ETF·ETP·채권·환율·매크로 리포트\n\n"
        "규칙 2 (YES): 규칙 1에 해당하지 않고, 다음 중 하나면 YES 다.\n"
        "- 관심 종목 중 하나를 주요 분석 대상으로 하는 개별 기업 리포트\n"
        "- 관심 종목들이 속한 산업(예: 반도체, 반도체 장비, 전력기기)을 전문적으로 다루는 산업 리포트\n\n"
        "규칙 3: 그 외는 전부 NO 다. 관심 종목과 무관한 기업·산업 리포트, 이름만 비슷한 다른 회사(예: 삼성E&A는 삼성전자가 아니다)도 NO 다.\n\n"
        "판정 예시:\n"
        "- 국내 마감시황 → NO (규칙 1)\n"
        "- 주간 아메리카 → NO (규칙 1)\n"
        "- 마켓뷰: 관심은 결국, 실적 → NO (규칙 1)\n"
        "- AI 주도장의 완충 포트폴리오: 밸류에이션 매력 중심의 단기 모멘텀 전략 → NO (규칙 1)\n"
        "- 개별 주식 레버리지 리밸런싱 효과 → NO (규칙 1)\n"
        "- 삼성E&A 기업분석 → NO (규칙 3)\n"
        "- 반도체의 시간 → YES (규칙 2, 산업)\n"
        "- 삼성전자 2Q26 프리뷰 → YES (규칙 2, 개별 기업)\n\n"
        "답은 YES 또는 NO 한 단어만 출력해라."
    )
    return prompt


def _yes_no(ans):
    ans = (ans or "").strip().upper()
    if ans.startswith("YES"):
        return True
    if ans.startswith("NO"):
        return False
    return None


def _ai_relevance_detail_from_prompt(prompt):
    """관련성 판정 (Sonnet 단일). 반환 (최종, sonnet 판정, gemini 판정). 판정값은 True/False/None(실패·미호출).
    순서: llm_client report_relevance(Sonnet) → 실패·해석 불가면 Gemini(_gemini_yes_no) 1회 → 그것도 실패면 None(호출부 키워드 규칙 폴백)."""
    sonnet = None
    try:
        r = llm_client.complete("report_relevance", prompt)
        if r.ok:
            sonnet = _yes_no(r.text)
            if sonnet is None:
                logger.warning("[llm] report_relevance 응답 해석 불가: %r — Gemini 로 폴백", (r.text or "")[:80])
        else:
            logger.warning("[llm] report_relevance 실패: %s%s — Gemini 로 폴백", (r.error or "unknown")[:200], f" (fallback: {r.fallback_reason})" if r.fallback else "")
    except Exception as e:
        logger.warning("[llm] report_relevance 호출 예외: %s — Gemini 로 폴백", f"{type(e).__name__}: {e}"[:200])
    if sonnet is not None:
        return sonnet, sonnet, None
    gemini = _yes_no(_gemini_yes_no(prompt)) if GEMINI_API_KEY else None
    if gemini is None:
        logger.warning("[gemini] 관련성 폴백도 실패%s — 키워드 규칙으로 폴백", "" if GEMINI_API_KEY else "(GEMINI_API_KEY 없음)")
    return gemini, None, gemini


def _ai_relevance_detail(filename, caption, watchlist):
    """_ai_relevance 와 같은 입력·프롬프트로 (최종, sonnet, gemini) 을 돌려준다 (로그용)."""
    return _ai_relevance_detail_from_prompt(_relevance_prompt(filename, caption, watchlist))


def _relevance_log_label(sonnet, gemini):
    lab = lambda v: "YES" if v is True else "NO" if v is False else "FAIL"
    s = f"sonnet={lab(sonnet)}"
    if sonnet is None:
        s += f" gemini={lab(gemini)}"
    return s


_jobs = {}
_shared_client = None
_shared_loop = None
_shared_thread = None


def get_config():
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def get_job(job_id):
    return _jobs.get(job_id)


# ===== DB =====

def init_db():
    conn = sqlite3.connect(REPORT_DB)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY,
        channel TEXT,
        message_id INTEGER,
        date TEXT,
        filename TEXT,
        text_preview TEXT,
        UNIQUE(channel, message_id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS meta (
        channel TEXT PRIMARY KEY,
        last_message_id INTEGER,
        last_updated TEXT,
        total_count INTEGER
    )""")
    conn.commit()
    conn.close()


async def build_full_index(channel_username, api_id, api_hash, session_path):
    client = _shared_client
    if not client:
        return

    conn = sqlite3.connect(REPORT_DB)
    c = conn.cursor()

    channel = await client.get_entity(channel_username)
    count = 0
    max_id = 0

    async for message in client.iter_messages(channel, reverse=True):
        if not isinstance(getattr(message, "media", None), MessageMediaDocument):
            continue

        doc = message.media.document
        filename = None
        for attr in doc.attributes:
            if isinstance(attr, DocumentAttributeFilename):
                filename = attr.file_name
                break
        if not filename:
            continue

        text_preview = (message.text or "")[:100]
        c.execute(
            "INSERT OR IGNORE INTO messages (channel, message_id, date, filename, text_preview) VALUES (?,?,?,?,?)",
            (channel_username, message.id, message.date.strftime("%Y-%m-%d %H:%M"), filename, text_preview),
        )
        count += 1
        if message.id > max_id:
            max_id = message.id
        if count % 500 == 0:
            conn.commit()

    conn.commit()
    c.execute(
        "INSERT OR REPLACE INTO meta (channel, last_message_id, last_updated, total_count) VALUES (?,?,?,?)",
        (channel_username, max_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), count),
    )
    conn.commit()
    conn.close()


async def update_index(channel_username, api_id, api_hash, session_path):
    client = _shared_client
    if not client:
        return 0

    conn = sqlite3.connect(REPORT_DB)
    c = conn.cursor()
    c.execute("SELECT last_message_id FROM meta WHERE channel=?", (channel_username,))
    row = c.fetchone()
    last_id = row[0] if row else 0

    channel = await client.get_entity(channel_username)
    new_count = 0
    max_id = last_id

    async for message in client.iter_messages(channel, min_id=last_id):
        if not isinstance(getattr(message, "media", None), MessageMediaDocument):
            continue

        doc = message.media.document
        filename = None
        for attr in doc.attributes:
            if isinstance(attr, DocumentAttributeFilename):
                filename = attr.file_name
                break
        if not filename:
            continue

        text_preview = (message.text or "")[:100]
        c.execute(
            "INSERT OR IGNORE INTO messages (channel, message_id, date, filename, text_preview) VALUES (?,?,?,?,?)",
            (channel_username, message.id, message.date.strftime("%Y-%m-%d %H:%M"), filename, text_preview),
        )
        new_count += 1
        if message.id > max_id:
            max_id = message.id

    conn.commit()
    c.execute("SELECT COUNT(*) FROM messages WHERE channel=?", (channel_username,))
    total = c.fetchone()[0]
    c.execute(
        "INSERT OR REPLACE INTO meta (channel, last_message_id, last_updated, total_count) VALUES (?,?,?,?)",
        (channel_username, max_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), total),
    )
    conn.commit()
    conn.close()
    return new_count


def search_index(keyword, date_from_str, date_to_str, channel=None):
    if not os.path.exists(REPORT_DB):
        return []
    conn = sqlite3.connect(REPORT_DB)
    c = conn.cursor()
    kw = f"%{keyword}%"
    if channel:
        c.execute(
            "SELECT message_id, date, filename, text_preview FROM messages "
            "WHERE channel=? AND date>=? AND date<=? AND (filename LIKE ? OR text_preview LIKE ?) ORDER BY date DESC",
            (channel, date_from_str, date_to_str + " 23:59", kw, kw),
        )
    else:
        c.execute(
            "SELECT message_id, date, filename, text_preview FROM messages "
            "WHERE date>=? AND date<=? AND (filename LIKE ? OR text_preview LIKE ?) ORDER BY date DESC",
            (date_from_str, date_to_str + " 23:59", kw, kw),
        )
    rows = c.fetchall()
    conn.close()
    return [{"message_id": r[0], "date": r[1], "filename": r[2], "text_preview": r[3]} for r in rows]


def get_index_status(channel=None):
    try:
        if not os.path.exists(REPORT_DB):
            if channel:
                return {"channel": channel, "total_count": 0, "last_updated": None}
            return {}
        conn = sqlite3.connect(REPORT_DB)
        c = conn.cursor()
        if channel:
            c.execute("SELECT last_message_id, last_updated, total_count FROM meta WHERE channel=?", (channel,))
            row = c.fetchone()
            conn.close()
            if not row:
                return {"channel": channel, "total_count": 0, "last_updated": None}
            return {"channel": channel, "last_message_id": row[0], "last_updated": row[1], "total_count": row[2]}
        else:
            c.execute("SELECT channel, last_message_id, last_updated, total_count FROM meta")
            rows = c.fetchall()
            conn.close()
            return {r[0]: {"last_message_id": r[1], "last_updated": r[2], "total_count": r[3]} for r in rows}
    except Exception:
        if channel:
            return {"channel": channel, "total_count": 0, "last_updated": None}
        return {}


# ===== Realtime Watcher =====

def start_realtime_watcher(api_id, api_hash, session_path):
    global _shared_client, _shared_loop, _shared_thread

    async def _run():
        global _shared_client
        _shared_client = TelegramClient(session_path, api_id, api_hash)
        await _shared_client.start()
        cfg = get_config()
        channels = cfg.get("channels", [])

        @_shared_client.on(events.NewMessage(chats=channels if channels else None))
        async def handler(event):
            message = event.message
            if not message.media:
                return
            cfg = get_config()
            watchlist = cfg.get("watchlist", [])
            personal_channels = cfg.get("personal_channels", [])
            if not watchlist or not personal_channels:
                return
            filename = ""
            for attr in getattr(getattr(message.media, "document", None), "attributes", []):
                if isinstance(attr, DocumentAttributeFilename):
                    filename = attr.file_name
                    break
            if not filename:
                return
            text = message.message or ""
            should_forward = None
            if cfg.get("ai_filter_enabled", True):
                loop = asyncio.get_event_loop()
                should_forward, _s, _g = await loop.run_in_executor(
                    None, _ai_relevance_detail, filename, text, watchlist
                )
                print(f"[Report AI필터] {filename[:60]} {_relevance_log_label(_s, _g)} → {should_forward}", flush=True)
            if should_forward is None:
                cleaned_filename = _mask_brokerages(filename.lower(), watchlist)
                should_forward = False
                for stock in watchlist:
                    if stock.lower() in cleaned_filename:
                        should_forward = True
                        break
                print(f"[Report AI필터] 폴백(파일명 매칭) {filename[:60]} -> {should_forward}", flush=True)
            if should_forward:
                for ch in personal_channels:
                    await _shared_client.forward_messages(ch, [message.id], event.chat_id)

        await _shared_client.run_until_disconnected()

    def _thread_main():
        global _shared_loop
        import time as _time
        _shared_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_shared_loop)

        async def _reconnect_and_wait():
            # 기존 _shared_client 객체를 새로 만들지 않고 그대로 재접속한다
            # (tg_inbox 등 다른 모듈이 telegram_report._shared_client 참조를 사용하므로).
            # Telethon은 같은 클라이언트 객체에 connect()로 재접속 가능하며 등록된 핸들러도 유지된다.
            if not _shared_client.is_connected():
                await _shared_client.connect()
            await _shared_client.run_until_disconnected()

        backoff = 60
        while True:
            try:
                if _shared_client is None:
                    # 최초 1회: 클라이언트 생성·start·핸들러 등록 후 run_until_disconnected
                    _shared_loop.run_until_complete(_run())
                else:
                    _shared_loop.run_until_complete(_reconnect_and_wait())
                # 예외 없이 반환 = 연결이 정상적으로 끊긴 것 → 즉시 재접속 시도, 백오프 리셋
                print("[telegram] shared client disconnected (normal return), reconnecting...", flush=True)
                backoff = 60
                _time.sleep(3)
            except (KeyboardInterrupt, SystemExit):
                print("[telegram] shared client thread stopping (interrupt/exit)", flush=True)
                break
            except Exception as e:
                print(f"[telegram] shared client connection lost: {type(e).__name__}: {e} "
                      f"— {backoff}s 후 재접속 시도", flush=True)
                _time.sleep(backoff)
                backoff = 120 if backoff == 60 else 300   # 60 → 120 → 300 (최대 300)
                continue

    _shared_thread = threading.Thread(target=_thread_main, daemon=True)
    _shared_thread.start()


# ===== Scheduler =====

def start_index_scheduler(api_id, api_hash, session_path):
    def _scheduler():
        import time as _time

        for _ in range(30):
            if _shared_client is not None:
                break
            _time.sleep(1)

        if _shared_client is None:
            return

        init_db()

        cfg = get_config()
        for ch in cfg.get("channels", []):
            status = get_index_status(ch)
            if status["total_count"] == 0:
                asyncio.run_coroutine_threadsafe(
                    build_full_index(ch, api_id, api_hash, session_path), _shared_loop
                ).result()

        last_run_date = None
        while True:
            now = datetime.now()
            utc_now = datetime.now(timezone.utc)
            kst_hour = (utc_now.hour + 9) % 24
            today_str = now.strftime("%Y-%m-%d")

            if kst_hour == 20 and last_run_date != today_str:
                last_run_date = today_str
                cfg = get_config()
                for ch in cfg.get("channels", []):
                    try:
                        asyncio.run_coroutine_threadsafe(
                            update_index(ch, api_id, api_hash, session_path), _shared_loop
                        ).result()
                    except Exception:
                        pass

            _time.sleep(60)

    t = threading.Thread(target=_scheduler, daemon=True)
    t.start()


# ===== Search & Download =====

def start_search_job(channel_username, keyword, date_from, date_to,
                     api_id, api_hash, session_path, download_base):
    job_id = uuid.uuid4().hex[:8]
    _jobs[job_id] = {
        "status": "searching",
        "msg_count": 0,
        "found": 0,
        "downloaded": 0,
        "total_found": 0,
        "error": None,
        "files": [],
        "download_path": "",
        "stop_requested": False,
    }

    if _shared_client is None or _shared_loop is None:
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = "Telethon 클라이언트 준비 중입니다. 잠시 후 다시 시도하세요."
        return job_id

    asyncio.run_coroutine_threadsafe(
        _search_and_download(
            job_id, channel_username, keyword, date_from, date_to,
            api_id, api_hash, session_path, download_base,
        ),
        _shared_loop,
    )
    return job_id


async def download_message(channel_username, message_id, dest_path):
    """(채널, message_id) 메시지 1건의 첨부 문서를 dest_path 에 저장 (종목 리포트 수집용, stock_reports 가 _shared_loop 에서 호출).
    기존 _search_and_download 의 get_messages/download_media 흐름을 그대로 쓴다. 반환: (원본 파일명, 크기 bytes) / 첨부 없으면 (None, 0)"""
    client = _shared_client
    channel = await client.get_entity(channel_username)
    msg = await client.get_messages(channel, ids=message_id)
    if not msg or not getattr(msg, "media", None) or not getattr(msg.media, "document", None):
        return None, 0
    filename = None
    for attr in msg.media.document.attributes:
        if isinstance(attr, DocumentAttributeFilename):
            filename = attr.file_name
            break
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    tmp = dest_path + ".part"
    await client.download_media(msg, file=tmp)
    os.replace(tmp, dest_path)
    return filename, os.path.getsize(dest_path)


def report_download_base():
    """기존 증권사 리포트 기능의 저장 루트 (report_config.json 의 download_base, 기본 Drive Analysis/)"""
    cfg = get_config()
    return cfg.get("download_base", DEFAULT_CONFIG["download_base"])


def stock_folder(download_base, keyword):
    """종목 폴더 규칙 (기존 _search_and_download 의 stock_root 와 같음): <download_base>/<keyword>"""
    return os.path.join(os.path.expanduser(download_base), keyword)


# --- Drive(File Provider) 폴더 나열: EDEADLK(errno 11) = 클라우드 전용(dataless) 폴더. launchd 프로세스는 실체화를 못 하므로
#     Finder 로 폴더를 열어(open -g) 실체화를 유도한 뒤 재시도한다. 폴더별 open 은 하루 1회, 실패 로그도 하루 1회. ---
_EDEADLK = 11
_drive_open_at = {}          # 경로 → 마지막 open 실행 시각(datetime)
_drive_folder_state = {}     # 경로 → {"last_fail": iso, "errno": int, "recovered": bool, "logged_day": "YYYY-MM-DD"}
_drive_state_lock = threading.Lock()


def _finder_open(path):
    """Finder 에 폴더를 (비활성으로) 열어 File Provider 실체화를 트리거. 반환 (실행 여부, 설명)"""
    try:
        r = subprocess.run(["open", "-g", path], capture_output=True, text=True, timeout=10)
        return r.returncode == 0, f"rc={r.returncode} {(r.stderr or '').strip()[:80]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"


def _listdir_retry(path, attempts=2, wait=1.0):
    today = datetime.now().date().isoformat()
    last_err = None
    res = None
    for i in range(attempts):
        try:
            res = os.listdir(path)
            break
        except OSError as e:
            last_err = e
            if e.errno == _EDEADLK:
                break                                   # 일반 재시도는 의미 없음 → 아래 실체화 경로로
            if i < attempts - 1:
                time.sleep(wait)
    if last_err is not None and getattr(last_err, "errno", None) == _EDEADLK and res is None:
        res = _listdir_materialize(path, today)
    if res is not None:
        if last_err is not None and getattr(last_err, "errno", None) == _EDEADLK:   # 실체화 재시도로 살아난 경우
            with _drive_state_lock:
                st = _drive_folder_state.setdefault(path, {})
                st.update(last_fail=datetime.now().isoformat(timespec="seconds"), errno=_EDEADLK, recovered=True)
            print(f"[report] 폴더 실체화 복구: {path}", flush=True)
        return res
    with _drive_state_lock:
        st = _drive_folder_state.setdefault(path, {})
        st.update(last_fail=datetime.now().isoformat(timespec="seconds"), errno=getattr(last_err, "errno", None), recovered=False)
        should_log = st.get("logged_day") != today
        st["logged_day"] = today
    if should_log:
        print(f"[report] 폴더 나열 실패(건너뜀): {path} — {type(last_err).__name__}: {last_err}", flush=True)
    return None


def _listdir_materialize(path, today):
    """EDEADLK 폴더: open -g 로 Finder 에 열고 5초 뒤 재시도, 또 실패면 10초 더 기다려 마지막 재시도. open 은 폴더당 하루 1회."""
    with _drive_state_lock:
        opened_at = _drive_open_at.get(path)
        already_today = opened_at is not None and opened_at.date().isoformat() == today
        if not already_today:
            _drive_open_at[path] = datetime.now()
    if already_today:
        return None                                     # 오늘 이미 열었는데 또 실패 → 바로 포기
    ok, desc = _finder_open(path)
    print(f"[report] dataless 폴더 Finder 열기({desc}): {path}", flush=True)
    for delay in (5, 10):
        time.sleep(delay)
        try:
            return os.listdir(path)
        except OSError as e:
            if e.errno != _EDEADLK:
                break
    return None


def drive_folder_status(prefix=None, day=None):
    """당일 Drive 폴더 상태 요약: {"skipped_folders": [최종 실패 경로...], "recovered_folders": 복구 수}. prefix 로 종목 폴더만 걸러냄."""
    day = day or datetime.now().date().isoformat()
    skipped, recovered = [], 0
    with _drive_state_lock:
        for p, st in _drive_folder_state.items():
            if prefix and not p.startswith(prefix):
                continue
            if (st.get("last_fail") or "")[:10] != day and not st.get("recovered"):
                continue
            if st.get("recovered"):
                if (st.get("last_fail") or "")[:10] == day:
                    recovered += 1
            else:
                skipped.append(p)
    return {"skipped_folders": sorted(skipped), "recovered_folders": recovered}


def existing_report_files(stock_root):
    """종목 폴더의 모든 하위 폴더에 있는 파일 {파일명: 경로} (기존 중복 검사 규칙: 하위 폴더 1단계만, 루트 바로 아래 파일은 제외).
    기존 _search_and_download 와 stock_reports 가 같은 기준으로 중복을 거른다."""
    existing = {}
    if not os.path.exists(stock_root):
        return existing
    subs = _listdir_retry(stock_root)
    if subs is None:
        print(f"[report] 루트 나열 실패로 중복검사 생략: {stock_root}", flush=True)
        return existing
    for sub in subs:
        sub_path = os.path.join(stock_root, sub)
        try:
            is_dir = os.path.isdir(sub_path)
        except OSError:
            is_dir = False
        if not is_dir:
            continue
        files = _listdir_retry(sub_path)
        if files is None:
            continue
        for f in files:
            existing.setdefault(nfc(f), os.path.join(sub_path, f))
    return existing


# ===== 종목 판별 · 폴더 키워드 · 파일명 규칙 (stock_reports 와 공유, 4-D) =====
_BD = os.path.dirname(os.path.abspath(__file__))
_maps_cache = {}
REPORT_SUBFOLDER = "리포트"
REPORT_INDEX = ".index.json"          # 종목 폴더/리포트/.index.json (두 기능이 공유하는 중복 기록)
# 구형: {message_id: "파일명"}  /  신형: {message_id: {"filename","sha256","size","text_fp","pages","channel","original_name","saved_at"}}
# 키는 message_id 문자열, 또는 index 에 없던 파일을 백필한 "file:<sha256 앞 12자>"
_TAIL_RE = re.compile(r"^(?P<name>.+?)(?P<tail>[\s\d.,\-_]+)$")
ISSUER_EXTRAS = ("KB증권", "삼성증권", "한국투자증권", "NH투자증권", "현대차증권", "SK증권", "LS증권", "iM증권", "DS투자증권", "리딩투자증권",
                 "한양증권", "유화증권", "KR투자증권", "코리아에셋투자증권", "삼성선물", "토스증권", "대신증권", "메리츠증권", "하나증권")


def nfc(s):
    """macOS 파일시스템은 한글을 NFD 로 돌려주므로 비교 전에 NFC 로 맞춘다"""
    return unicodedata.normalize("NFC", s) if isinstance(s, str) else s


def _json_map(name, path):
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return {}
    hit = _maps_cache.get(name)
    if hit and hit[0] == mt:
        return hit[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return (hit[1] if hit else {})          # 읽기 실패(갱신 중 등)는 캐시하지 않음
    if data:
        _maps_cache[name] = (mt, data)
    return data


def kr_map():
    """{종목코드: {corp_code, name}} (dart_corp_map.json)"""
    return _json_map("kr", os.path.join(_BD, "dart_corp_map.json"))


def us_map():
    return _json_map("us", os.path.join(_BD, "us_stock_map.json"))


def us_short_name(code, name=None):
    """'Micron Technology, Inc. - Common Stock' → 'Micron Technology'"""
    name = name if name is not None else (us_map().get(code) or {}).get("name", "")
    short = re.sub(r"\s*-?\s*(Class [A-Z]\s+)?(Common Stock|Common Shares|Ordinary Shares|American Depositary Shares?|Depositary Shares?|ADS|ADR)(\s.*)?$", "", name or "", flags=re.I).strip()
    short = re.sub(r"\s+-\s+.*$", "", short).strip()
    short = re.sub(r",?\s+(Inc\.?|Incorporated|Corporation|Corp\.?|Ltd\.?|Limited|plc|N\.V\.|S\.A\.|Co\.?|Company|Holdings?)\s*$", "", short, flags=re.I).strip()
    return short.rstrip(",. ") or code


def _safe_name(name, limit=80):
    return re.sub(r'[/\\:*?"<>|\n\r]', "_", (name or "").strip())[:limit].strip("._ ") or "종목"


def stock_folder_keyword(market, code, name=None):
    """종목 폴더 키워드: 한국 = 종목명(코드 없음), 미국 = <짧은 이름>_<티커> (예 NVIDIA_NVDA). 모든 기능이 이 함수를 쓴다."""
    if market == "US":
        return _safe_name(f"{us_short_name(code, name)}_{code}")
    if name is None:
        name = (kr_map().get(code) or {}).get("name", "")
    return _safe_name(name) or code


ALIASES = {   # 검색어·폴더명 약칭 → 정식 종목명(한국) 또는 티커(미국). 비교는 소문자·공백 제거 (4-E)
    "현대일렉트릭": "HD현대일렉트릭", "하이닉스": "SK하이닉스", "s-oil": "S-Oil", "lselectric": "엘에스일렉트릭",
    "현대차": "현대자동차", "samg엔터": "SAMG엔터", "brk": "BRK.B",
}
_ALIAS_NORM = {re.sub(r"\s+", "", k.lower()): v for k, v in ALIASES.items()}


def _alias(t):
    return _ALIAS_NORM.get(re.sub(r"\s+", "", (t or "").lower()), t)


def resolve_stock_keyword(keyword):
    """검색어 → {market, code, name, tail} 또는 None.
    종목명 완전 일치 / 6자리 코드 / 미국 티커 / '<종목명><꼬리>' (꼬리 = 공백·숫자·점·쉼표·대시·밑줄만). 쉼표가 든 복수 종목은 None."""
    k = nfc((keyword or "").strip())
    if not k or "," in k:
        return None
    km, um = kr_map(), us_map()
    names = {v.get("name"): c for c, v in km.items() if v.get("name")}

    def exact(t):
        t = _alias(t)
        if t in names:
            return {"market": "KR", "code": names[t], "name": t}
        if re.fullmatch(r"\d{6}", t) and t in km:
            return {"market": "KR", "code": t, "name": km[t].get("name", "")}
        if t.upper() in um and re.fullmatch(r"[A-Za-z.\-]{1,6}", t):
            return {"market": "US", "code": t.upper(), "name": um[t.upper()].get("name", "")}
        return None

    r = exact(k)
    if r:
        return dict(r, tail="")
    m = _TAIL_RE.match(k)
    if m:
        base, tail = m.group("name").rstrip(), m.group("tail")
        if base != k:
            r = exact(base)
            if r:
                return dict(r, tail=tail)
    return None


def split_keywords(keyword):
    """'삼성전자, 하이닉스' → 판별된 종목 목록 (판별 안 되는 조각은 빼고). 쉼표 없으면 1개 또는 빈 목록"""
    out = []
    for part in [p.strip() for p in (keyword or "").split(",") if p.strip()]:
        r = resolve_stock_keyword(part)
        if r:
            out.append(r)
    return out


def guess_broker(filename, preview=""):
    """파일명·본문에서 증권사 이름 추정 (없으면 None)"""
    text = f"{filename or ''} {preview or ''}"
    for b in sorted(set(BROKERAGES) | set(ISSUER_EXTRAS), key=len, reverse=True):
        if b in text:
            return b
    m = re.search(r"([가-힣A-Za-z&]{2,10}(?:투자증권|증권|금융투자))", text)
    return m.group(1) if m else None


def _yymmdd(d):
    """'2026-10-08 11:23' / '2026-10-08T..' / datetime / '20261008' → '261008'. 모르면 None"""
    if not d:
        return None
    if isinstance(d, datetime):
        return d.strftime("%y%m%d")
    s = str(d)
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s) or re.match(r"(\d{4})(\d{2})(\d{2})$", s)
    return (m.group(1)[2:] + m.group(2) + m.group(3)) if m else None


def report_filename(stock_name, msg_date, broker, original_filename, limit=120):
    """저장 파일명 규칙: <종목명>_<yymmdd>_<증권사|미확인>_<레포트명>.pdf
    레포트명 = 원본 stem 에서 종목명·증권사·날짜(6/8자리)·밑줄 중복을 뺀 나머지(비면 원본 stem)."""
    stem = nfc(re.sub(r"\.pdf$", "", os.path.basename(original_filename or ""), flags=re.I))
    stock_name = nfc(stock_name)
    broker = nfc((broker or "").strip()) or "미확인"
    rest = stem
    for tok in (stock_name, broker if broker != "미확인" else ""):
        if tok:
            rest = rest.replace(tok, "_").replace(tok.replace(" ", "_"), "_")
    rest = re.sub(r"(?<!\d)(?:20)?\d{6}(?!\d)", "_", rest)          # 날짜 6/8자리
    rest = re.sub(r'[/\\:*?"<>|\n\r]', "_", rest)
    rest = re.sub(r"[_\s]+", "_", rest).strip("_ .")
    if not rest:
        rest = re.sub(r"[_\s]+", "_", re.sub(r'[/\\:*?"<>|\n\r]', "_", stem)).strip("_ .") or "리포트"
    name = f"{_safe_name(stock_name, 40)}_{_yymmdd(msg_date) or '000000'}_{_safe_name(broker, 20)}_{rest}"
    name = re.sub(r"_+", "_", name)
    if len(name) > limit - 4:
        name = name[:limit - 4].rstrip("_ .")
    return name + ".pdf"


def report_dir(stock_root):
    return os.path.join(stock_root, REPORT_SUBFOLDER)


def _index_entry(v):
    """구형(문자열)·신형(dict) 항목을 모두 dict 로 정규화"""
    if isinstance(v, dict):
        return dict(v, filename=v.get("filename"))
    return {"filename": v if isinstance(v, str) else None}


def load_report_index(stock_root):
    """{키: {"filename", "sha256", "size", "text_fp", "pages", "channel", "original_name", "saved_at"}} — 구형 항목은 filename 만 채워짐"""
    p = os.path.join(report_dir(stock_root), REPORT_INDEX)
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        return {str(k): _index_entry(v) for k, v in d.items()} if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_report_index_raw(stock_root):
    """저장된 그대로(구형 문자열 항목 보존) — record_report 가 다른 항목을 건드리지 않게 쓸 때"""
    p = os.path.join(report_dir(stock_root), REPORT_INDEX)
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def pdf_fingerprint(path):
    """PDF 내용 지문: {"sha256", "size", "pages", "text_fp"}. text_fp = sha256(페이지수|첫 페이지 텍스트(공백·줄바꿈 제거))[:16], 추출 실패면 None"""
    import hashlib
    out = {"sha256": None, "size": None, "pages": None, "text_fp": None}
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        out["sha256"] = h.hexdigest()
        out["size"] = os.path.getsize(path)
    except OSError:
        return out
    try:
        from pypdf import PdfReader
        r = PdfReader(path)
        n = len(r.pages)
        t = (r.pages[0].extract_text() or "") if n else ""
        t = re.sub(r"\s+", "", t)
        out["pages"] = n
        if t:
            out["text_fp"] = hashlib.sha256(f"{n}|{t}".encode("utf-8")).hexdigest()[:16]
    except Exception:
        pass
    return out


def save_report_index(stock_root, index):
    d = report_dir(stock_root)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, REPORT_INDEX)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def record_report(stock_root, message_id, filename, fp=None, channel=None, original_name=None):
    """신형 항목으로 기록. fp 는 pdf_fingerprint 결과(없으면 파일명만). 다른 항목(구형 포함)은 건드리지 않는다."""
    if message_id is None:
        return
    idx = load_report_index_raw(stock_root)
    fp = fp or {}
    idx[str(message_id)] = {"filename": filename, "sha256": fp.get("sha256"), "size": fp.get("size"), "text_fp": fp.get("text_fp"),
                            "pages": fp.get("pages"), "channel": channel, "original_name": original_name,
                            "saved_at": datetime.now().isoformat(timespec="seconds")}
    save_report_index(stock_root, idx)


def find_duplicate_detail(stock_root, message_id=None, new_name=None, original_name=None, existing=None, sha256=None, text_fp=None):
    """중복이면 (기존 파일 경로, 사유), 아니면 (None, None).
    (a) .index.json 의 message_id (b) 새 규칙 파일명 (c) 원본 파일명 (d) sha256 일치 (e) text_fp 일치(None 제외) — 하나라도 맞으면 중복"""
    if not stock_root:
        return None, None
    existing = existing if existing is not None else existing_report_files(stock_root)
    idx = load_report_index(stock_root) if (message_id is not None or sha256 or text_fp) else {}
    if message_id is not None:
        fn = nfc((idx.get(str(message_id)) or {}).get("filename"))
        if fn and fn in existing:
            return existing[fn], "message_id"
    for nm, why in ((new_name, "new_name"), (original_name, "original_name")):
        nm = nfc(nm)
        if nm and nm in existing:
            return existing[nm], why
    for key, why in (("sha256", "sha256"), ("text_fp", "text_fp")):
        want = sha256 if key == "sha256" else text_fp
        if not want:
            continue
        for ent in idx.values():
            if ent.get(key) and ent[key] == want:
                fn = nfc(ent.get("filename"))
                if fn and fn in existing:
                    return existing[fn], why
    return None, None


def find_duplicate(stock_root, message_id=None, new_name=None, original_name=None, existing=None, sha256=None, text_fp=None):
    """중복이면 기존 파일 경로, 아니면 None (find_duplicate_detail 의 호환 래퍼)"""
    return find_duplicate_detail(stock_root, message_id, new_name, original_name, existing, sha256, text_fp)[0]


def _unique_path(directory, filename):
    base, ext = os.path.splitext(filename)
    path = os.path.join(directory, filename)
    counter = 2
    while os.path.exists(path):
        path = os.path.join(directory, f"{base}_{counter}{ext}")
        counter += 1
    return path


async def _search_and_download(job_id, channel_username, keyword, date_from, date_to,
                               api_id, api_hash, session_path, download_base):
    job = _jobs[job_id]
    try:
        client = _shared_client
        if not client:
            job["status"] = "error"
            job["error"] = "Telethon 클라이언트 준비 중입니다. 잠시 후 다시 시도하세요."
            return

        date_from_str = date_from.strftime("%Y-%m-%d")
        date_to_str = date_to.strftime("%Y-%m-%d")
        _one = resolve_stock_keyword(keyword)
        search_term = _one["name"] if (_one and _one["tail"] and _one["market"] == "KR") else keyword   # '삼아알미늄 26.04' 는 종목명으로 검색 (4-D)
        results = search_index(search_term, date_from_str, date_to_str, channel_username)
        if _one or split_keywords(keyword):
            _stocks0 = split_keywords(keyword)
            job["download_path"] = ", ".join(report_dir(stock_folder(download_base, stock_folder_keyword(s0["market"], s0["code"], s0["name"]))) for s0 in _stocks0)
        job["found"] = len(results)
        job["total_found"] = len(results)
        job["msg_count"] = len(results)

        if not results:
            job["status"] = "done"
            return

        stocks = split_keywords(keyword)            # 검색어가 종목(복수 가능)이면 종목 폴더/리포트/ 로 고정 (4-D)
        if stocks:
            roots = {s["code"]: stock_folder(download_base, stock_folder_keyword(s["market"], s["code"], s["name"])) for s in stocks}
            final_path = report_dir(roots[stocks[0]["code"]])
            local_staging = os.path.join("downloads", _safe_name(keyword, 40), "리포트")
            job["download_path"] = final_path if len(stocks) == 1 else ", ".join(report_dir(roots[s["code"]]) for s in stocks)
            job["stock_folders"] = [report_dir(roots[s["code"]]) for s in stocks]
            existing_by_code = {s["code"]: await asyncio.to_thread(existing_report_files, roots[s["code"]]) for s in stocks}
        else:
            stock_root = stock_folder(download_base, keyword)
            subfolder_name = f"{date_from.strftime('%y%m%d')}~{date_to.strftime('%y%m%d')}"
            final_path = os.path.join(stock_root, subfolder_name)
            local_staging = os.path.join("downloads", keyword, subfolder_name)
            job["download_path"] = final_path
            existing_files = set(await asyncio.to_thread(existing_report_files, stock_root))   # 공통 함수 (stock_reports 와 같은 기준)
        os.makedirs(local_staging, exist_ok=True)
        job["skipped"] = 0
        staged = []     # (로컬 경로, 최종 폴더, 종목 루트, message_id, 저장 파일명)

        channel = await client.get_entity(channel_username)

        for r in results:
            if job.get("stop_requested"):
                job["status"] = "stopping"
                break

            msg = await client.get_messages(channel, ids=r["message_id"])
            if not msg:
                continue

            doc = msg.media.document
            filename = None
            for attr in doc.attributes:
                if isinstance(attr, DocumentAttributeFilename):
                    filename = attr.file_name
                    break
            if not filename:
                filename = r["filename"]

            if stocks:
                target = stocks[0]
                if len(stocks) > 1:            # 복수 종목: 파일명·본문에 나오는 종목 폴더로, 없으면 첫 종목
                    hay = f"{filename} {r.get('text_preview') or ''}"
                    target = next((s for s in stocks if s["name"] and s["name"] in hay or s["code"] in hay), stocks[0])
                root = roots[target["code"]]
                new_name = report_filename(target["name"] if target["market"] == "KR" else us_short_name(target["code"], target["name"]),
                                           r["date"], guess_broker(filename, r.get("text_preview")), filename)
                if find_duplicate(root, r["message_id"], new_name, filename, existing_by_code[target["code"]]):
                    job["skipped"] += 1
                    continue
                file_path = _unique_path(local_staging, new_name)
                await client.download_media(msg, file=file_path)
                existing_by_code[target["code"]][new_name] = os.path.join(report_dir(root), new_name)
                staged.append((file_path, report_dir(root), root, r["message_id"], os.path.basename(file_path)))
            else:
                if filename in existing_files:
                    job["skipped"] += 1
                    continue
                file_path = _unique_path(local_staging, filename)
                await client.download_media(msg, file=file_path)

            size_kb = round(os.path.getsize(file_path) / 1024, 1)
            job["downloaded"] += 1
            job["files"].append({
                "filename": os.path.basename(file_path),
                "date": r["date"],
                "size_kb": size_kb,
            })

        staging_files = os.listdir(local_staging) if os.path.exists(local_staging) else []
        if not staging_files:
            shutil.rmtree(local_staging, ignore_errors=True)
            parent = os.path.dirname(local_staging)
            if os.path.isdir(parent) and not os.listdir(parent):
                shutil.rmtree(parent, ignore_errors=True)
            job["status"] = "done"
            job["error"] = None
            if job["skipped"] > 0:
                job["error"] = f"신규 파일 없음 (전부 중복, {job['skipped']}건 스킵)"
            return

        def _copy_to_drive():
            if stocks:                      # 종목 폴더: 파일마다 자기 종목 폴더/리포트/ 로 옮기고 .index.json 에 기록 (내용 지문 중복이면 저장하지 않음)
                for lp, dest_dir, root, mid, fn in staged:
                    fp = pdf_fingerprint(lp)
                    dup, why = find_duplicate_detail(root, None, None, None, None, fp.get("sha256"), fp.get("text_fp"))
                    if dup and why in ("sha256", "text_fp"):
                        print(f"[report] 중복 건너뜀: {fn} = {os.path.basename(dup)} ({why})", flush=True)
                        record_report(root, mid, os.path.basename(dup), fp, channel_username, fn)
                        try:
                            os.remove(lp)
                        except OSError:
                            pass
                        job["skipped"] = job.get("skipped", 0) + 1
                        continue
                    os.makedirs(dest_dir, exist_ok=True)
                    dst = os.path.join(dest_dir, fn)
                    if not os.path.exists(dst):
                        shutil.move(lp, dst)
                    record_report(root, mid, fn, fp, channel_username, fn)
                shutil.rmtree(local_staging, ignore_errors=True)
                parent = os.path.dirname(local_staging)
                if os.path.isdir(parent) and not os.listdir(parent):
                    shutil.rmtree(parent, ignore_errors=True)
                return
            result_mkdir = subprocess.run(["/bin/mkdir", "-p", final_path], capture_output=True, text=True)
            if result_mkdir.returncode != 0:
                raise RuntimeError(f"mkdir 실패: {result_mkdir.stderr}")

            result_cp = subprocess.run(
                ["/bin/cp", "-R", local_staging + "/.", final_path + "/"],
                capture_output=True, text=True,
            )
            if result_cp.returncode != 0:
                raise RuntimeError(f"cp 실패: {result_cp.stderr}")

            subprocess.run(["/bin/rm", "-rf", local_staging], capture_output=True)
            parent = os.path.dirname(local_staging)
            if os.path.isdir(parent) and not os.listdir(parent):
                subprocess.run(["/bin/rm", "-rf", parent], capture_output=True)

        try:
            await asyncio.to_thread(_copy_to_drive)
        except Exception as e:
            job["error"] = f"Google Drive 복사 실패 (로컬 staging에 파일 보존됨: {local_staging}): {e}"
            job["status"] = "error"
            return

        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
