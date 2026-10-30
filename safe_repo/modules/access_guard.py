"""Global ban guard. Loaded before ordinary bot handlers."""
from pyrogram import filters, StopPropagation
from safe_repo import app
from config import OWNER_ID
from motor.motor_asyncio import AsyncIOMotorClient
from config import MONGO_DB

_mongo = AsyncIOMotorClient(MONGO_DB)
_bans = _mongo.users.banned_users


async def _is_banned(user_id):
    if not user_id or user_id in OWNER_ID:
        return False
    return await _bans.find_one({"_id": int(user_id)}, {"_id": 1}) is not None


@app.on_message(filters.incoming, group=-1000)
async def block_banned_messages(_, message):
    user = message.from_user
    if user and await _is_banned(user.id):
        try:
            await message.reply_text("⛔ **You are banned by the admin.**\n\nYou cannot use this bot until the administrator removes your ban.")
        finally:
            raise StopPropagation


@app.on_callback_query(group=-1000)
async def block_banned_callbacks(_, query):
    if await _is_banned(query.from_user.id):
        await query.answer("⛔ You are banned by the admin.", show_alert=True)
        raise StopPropagation
