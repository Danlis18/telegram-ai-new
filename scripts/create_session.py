import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

async def main():
    api_id = int(input("TELEGRAM_API_ID: ").strip())
    api_hash = input("TELEGRAM_API_HASH: ").strip()
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        print("\nTELEGRAM_SESSION (keep secret; add only to Railway Variables):\n")
        print(client.session.save())

if __name__ == "__main__":
    asyncio.run(main())
