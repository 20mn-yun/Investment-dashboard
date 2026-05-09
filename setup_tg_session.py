import os
import asyncio
from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

API_ID = int(os.environ.get("TG_API_ID", "0"))
API_HASH = os.environ.get("TG_API_HASH", "")

SESSION_DIR = "sessions"
SESSION_PATH = os.path.join(SESSION_DIR, "tg_report")


async def main():
    os.makedirs(SESSION_DIR, exist_ok=True)
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    await client.start()
    me = await client.get_me()
    print(f"로그인 성공: {me.first_name} ({me.phone})")
    print(f"세션 파일: {SESSION_PATH}.session")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
