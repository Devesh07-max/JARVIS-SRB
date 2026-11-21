import asyncio
from pyrogram.errors import FloodWait, InputUserDeactivated, UserIsBlocked, PeerIdInvalid

async def send_broadcast_message(message, users):
    sent = failed = 0
    for uid in users:
        try:
            await message.copy(chat_id=uid); sent += 1
        except FloodWait as e:
            await asyncio.sleep(int(e.value))
            try: await message.copy(chat_id=uid); sent += 1
            except Exception: failed += 1
        except (InputUserDeactivated, UserIsBlocked, PeerIdInvalid): failed += 1
        except Exception: failed += 1
        await asyncio.sleep(0.1)
    return sent, failed
