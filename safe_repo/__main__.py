#safe_repo

import asyncio
import importlib
import threading
import pyrogram.utils
from pyrogram import idle
from safe_repo.modules import ALL_MODULES
from aiojobs import create_scheduler
pyrogram.utils.MIN_CHANNEL_ID = -1009999999999
from safe_repo.core.mongo.plans_db import check_and_remove_expired_users
from app import run_web_server

loop = asyncio.get_event_loop()

async def schedule_expiry_check():
    scheduler = await create_scheduler()
    while True:
        await scheduler.spawn(check_and_remove_expired_users())
        await asyncio.sleep(3600)  # Check every hour

async def safe_repo_boot():
    # The hosting platform expects one long-running process. Start Flask in a
    # daemon thread inside this same process instead of spawning a second
    # background shell/process from Docker.
    web_thread = threading.Thread(
        target=run_web_server,
        name="health-web",
        daemon=True,
    )
    web_thread.start()

    for all_module in ALL_MODULES:
        importlib.import_module("safe_repo.modules." + all_module)
    try:
        from safe_repo.modules.main import recover_interrupted_batches
        await recover_interrupted_batches()
    except Exception as exc:
        print(f"Interrupted batch recovery startup error: {exc}")
    print("»»»» ʙᴏᴛ ᴅᴇᴘʟᴏʏ sᴜᴄᴄᴇssғᴜʟʟʏ ✨ 🎉")

    # Keep the reference Telethon client alive so its callback/input handlers
    # continue receiving updates.
    from safe_repo import sex
    asyncio.create_task(sex.run_until_disconnected())

    # Start the background task for checking expired users
    asyncio.create_task(schedule_expiry_check())

    await idle()
    print("»» ɢᴏᴏᴅ ʙʏᴇ ! sᴛᴏᴘᴘɪɴɢ ʙᴏᴛ.")

if __name__ == "__main__":
    loop.run_until_complete(safe_repo_boot())
