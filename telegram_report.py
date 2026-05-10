import json
import os
import threading
import asyncio
import uuid
import sqlite3
import shutil
from datetime import datetime, timezone

from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaDocument, DocumentAttributeFilename

CONFIG_PATH = "report_config.json"
REPORT_DB = "report_index.db"
DEFAULT_CONFIG = {
    "channels": [],
    "download_base": "~/Library/CloudStorage/GoogleDrive-changyun1222@gmail.com/내 드라이브/Analysis",
    "watchlist": [],
    "personal_channels": [],
    "forward_enabled": True,
}

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

    cfg = get_config()
    watchlist = cfg.get("watchlist", [])
    personal_channel = cfg.get("personal_channel", "")
    forward_enabled = cfg.get("forward_enabled", True)

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

        if forward_enabled and personal_channel and watchlist:
            combined = (filename + " " + text_preview).lower()
            for stock in watchlist:
                if stock.lower() in combined:
                    try:
                        await client.forward_messages(personal_channel, [message.id], channel)
                    except Exception:
                        pass
                    break

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
            text = message.message or ""
            for stock in watchlist:
                if stock.lower() in filename.lower() or stock.lower() in text.lower():
                    for ch in personal_channels:
                        await _shared_client.forward_messages(ch, [message.id], event.chat_id)
                    break

        await _shared_client.run_until_disconnected()

    def _thread_main():
        global _shared_loop
        _shared_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_shared_loop)
        _shared_loop.run_until_complete(_run())

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

        now = datetime.now()
        subfolder = f"{keyword} {now.strftime('%y.%m')}"
        local_staging = os.path.join("downloads", subfolder)
        final_path = os.path.join(os.path.expanduser(download_base), subfolder)
        os.makedirs(local_staging, exist_ok=True)
        job["download_path"] = final_path

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

            file_path = _unique_path(local_staging, filename)
            await client.download_media(msg, file=file_path)

            size_kb = round(os.path.getsize(file_path) / 1024, 1)
            job["downloaded"] += 1
            job["files"].append({
                "filename": os.path.basename(file_path),
                "date": r["date"],
                "size_kb": size_kb,
            })

        try:
            os.makedirs(final_path, exist_ok=True)
            for fname in os.listdir(local_staging):
                src = os.path.join(local_staging, fname)
                dst = os.path.join(final_path, fname)
                shutil.copy2(src, dst)
            shutil.rmtree(local_staging)
        except Exception as e:
            job["error"] = f"Google Drive 복사 실패 (로컬 staging에 파일 보존됨: {local_staging}): {e}"
            job["status"] = "error"
            return

        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
