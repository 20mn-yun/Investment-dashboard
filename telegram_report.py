import json
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

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

CONFIG_PATH = "report_config.json"
REPORT_DB = "report_index.db"
DEFAULT_CONFIG = {
    "channels": [],
    "download_base": "~/Library/CloudStorage/GoogleDrive-changyun1222@gmail.com/내 드라이브/Analysis",
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
            print(f"[Report AI필터] gemini 실패 status={res.status_code} 시도={attempt+1}/3", flush=True)
        except Exception as e:
            print(f"[Report AI필터] gemini 예외 {type(e).__name__} 시도={attempt+1}/3", flush=True)
        time.sleep(2)
    return ""


def _ai_relevance(filename, caption, watchlist):
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
    ans = _gemini_yes_no(prompt)
    if ans.startswith("YES"):
        return True
    if ans.startswith("NO"):
        return False
    return None


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
            if cfg.get("ai_filter_enabled", True) and GEMINI_API_KEY:
                loop = asyncio.get_event_loop()
                should_forward = await loop.run_in_executor(
                    None, _ai_relevance, filename, text, watchlist
                )
                print(f"[Report AI필터] {filename[:60]} -> {should_forward}", flush=True)
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
        results = search_index(keyword, date_from_str, date_to_str, channel_username)
        job["found"] = len(results)
        job["total_found"] = len(results)
        job["msg_count"] = len(results)

        if not results:
            job["status"] = "done"
            return

        stock_root = os.path.join(os.path.expanduser(download_base), keyword)
        subfolder_name = f"{date_from.strftime('%y%m%d')}~{date_to.strftime('%y%m%d')}"
        final_path = os.path.join(stock_root, subfolder_name)
        local_staging = os.path.join("downloads", keyword, subfolder_name)
        os.makedirs(local_staging, exist_ok=True)
        job["download_path"] = final_path

        def _collect_existing_filenames(root):
            existing = set()
            if not os.path.exists(root):
                return existing
            for sub in os.listdir(root):
                sub_path = os.path.join(root, sub)
                if os.path.isdir(sub_path):
                    for f in os.listdir(sub_path):
                        existing.add(f)
            return existing

        existing_files = _collect_existing_filenames(stock_root)
        job["skipped"] = 0

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
