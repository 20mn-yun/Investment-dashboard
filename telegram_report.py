import json
import os
import threading
import asyncio
import uuid
from datetime import datetime, timezone

from telethon import TelegramClient
from telethon.tl.types import MessageMediaDocument, DocumentAttributeFilename

CONFIG_PATH = "report_config.json"
DEFAULT_CONFIG = {
    "channels": [],
    "download_base": "~/Library/CloudStorage/GoogleDrive-changyun1222@gmail.com/내 드라이브/증권사레포트",
}

_jobs = {}


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
    }

    def _run():
        asyncio.run(_search_and_download(
            job_id, channel_username, keyword, date_from, date_to,
            api_id, api_hash, session_path, download_base,
        ))

    t = threading.Thread(target=_run, daemon=True)
    t.start()
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
        client = TelegramClient(session_path, api_id, api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            job["status"] = "error"
            job["error"] = "Telegram session not authorized. Run setup_tg_session.py first."
            return

        date_from_utc = date_from.replace(tzinfo=timezone.utc)
        date_to_utc = date_to.replace(tzinfo=timezone.utc)

        now = datetime.now()
        subfolder = f"{keyword} {now.strftime('%y.%m')}"
        download_path = os.path.join(os.path.expanduser(download_base), subfolder)
        os.makedirs(download_path, exist_ok=True)
        job["download_path"] = download_path

        channel = await client.get_entity(channel_username)

        async for message in client.iter_messages(channel, offset_date=date_to_utc, reverse=False):
            if message.date.replace(tzinfo=timezone.utc) < date_from_utc:
                break

            job["msg_count"] += 1

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

            kw = keyword.lower()
            text_match = message.text and kw in message.text.lower()
            name_match = kw in filename.lower()

            if not (text_match or name_match):
                continue

            job["total_found"] += 1
            job["found"] = job["total_found"]

            file_path = _unique_path(download_path, filename)
            await client.download_media(message, file=file_path)

            size_kb = round(os.path.getsize(file_path) / 1024, 1)
            job["downloaded"] += 1
            job["files"].append({
                "filename": os.path.basename(file_path),
                "date": message.date.strftime("%Y-%m-%d %H:%M"),
                "size_kb": size_kb,
            })

        await client.disconnect()
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
