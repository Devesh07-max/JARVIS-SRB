"""Browse media already visible in a user's other Telegram bot chats.

This uses the user's own saved Telegram session. It does not attempt to bypass
protected-content restrictions or access conversations unavailable to that account.
"""
import asyncio
import os
import tempfile
import shutil
import uuid
from pathlib import Path

from pyrogram import filters, Client
from pyrogram.enums import ChatType
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import FloodWait

from safe_repo import app
from config import API_ID, API_HASH
from safe_repo.core.mongo import db

DLBOT_SESSIONS = {}


def _media_kind(message):
    if getattr(message, "video", None) or getattr(message, "animation", None) or getattr(message, "video_note", None):
        return "video"
    if getattr(message, "audio", None):
        return "audio"
    if getattr(message, "photo", None):
        return "photo"
    if getattr(message, "document", None) or getattr(message, "voice", None):
        return "document"
    return None


def _chat_label(chat):
    return (getattr(chat, "title", None) or getattr(chat, "first_name", None)
            or getattr(chat, "username", None) or str(chat.id))[:55]


async def _get_session(user_id, key=None):
    state = DLBOT_SESSIONS.get(user_id)
    if not state or (key and state.get("key") != key):
        return None
    return state


@app.on_message(filters.private & filters.command("dlbots"))
async def list_other_bots(_, message):
    user_id = message.from_user.id
    data = await db.get_data(user_id)
    if not data or not data.get("session"):
        return await message.reply_text(
            "🔐 **Telegram login required**\n\nUse /login first. /dlbots reads only the bot conversations visible to your own logged-in Telegram account."
        )
    status = await message.reply_text("🔎 **Finding your Telegram bot chats...**")
    old = DLBOT_SESSIONS.pop(user_id, None)
    if old:
        try:
            await old["client"].stop()
        except Exception:
            pass
    client = Client(f":dlbots_{user_id}_{uuid.uuid4().hex[:6]}:", api_id=API_ID, api_hash=API_HASH, session_string=data["session"])
    try:
        await client.start()
        bots = []
        async for dialog in client.get_dialogs(limit=150):
            chat = dialog.chat
            if getattr(chat, "type", None) == ChatType.BOT or getattr(chat, "is_bot", False):
                bots.append(chat)
        if not bots:
            await client.stop()
            return await status.edit_text(
                "📭 **No other bot chats found.**\n\nOpen the required bot in Telegram, start it and receive its media first. Then try /dlbots again."
            )
        key = uuid.uuid4().hex[:8]
        DLBOT_SESSIONS[user_id] = {"client": client, "key": key, "bots": {str(x.id): x for x in bots}}
        rows = [[InlineKeyboardButton("🤖 " + _chat_label(chat), callback_data=f"dlbot:{key}:{chat.id}")] for chat in bots[:30]]
        rows.append([InlineKeyboardButton("✖️ Close", callback_data=f"dlclose:{key}")])
        await status.edit_text(
            "╭━━━〔 🤖 𝗢𝗧𝗛𝗘𝗥 𝗕𝗢𝗧 𝗙𝗜𝗟𝗘𝗦 〕━━━╮\n\n"
            f"Found `{len(bots)}` bot chat(s) in your Telegram account.\n\n"
            "Select the bot from which you want to retrieve a file. I will show its recent media messages.\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯",
            reply_markup=InlineKeyboardMarkup(rows),
        )
    except Exception as exc:
        try:
            await client.stop()
        except Exception:
            pass
        await status.edit_text(
            "❌ **Could not read your Telegram bot chats.**\n\n"
            f"{str(exc)[:500]}\n\nPlease check /login and try again."
        )


@app.on_callback_query(filters.regex(r"^dlclose:[a-f0-9]{8}$"))
async def close_dl_bots(_, query):
    key = query.data.split(":", 1)[1]
    state = await _get_session(query.from_user.id, key)
    if state:
        DLBOT_SESSIONS.pop(query.from_user.id, None)
        try:
            await state["client"].stop()
        except Exception:
            pass
    await query.answer("Closed")
    await query.message.edit_text("❎ **/dlbots closed.**")


@app.on_callback_query(filters.regex(r"^dlbot:[a-f0-9]{8}:-?\d+$"))
async def show_bot_media(_, query):
    _, key, chat_id = query.data.split(":", 2)
    state = await _get_session(query.from_user.id, key)
    if not state:
        return await query.answer("This menu expired. Send /dlbots again.", show_alert=True)
    chat_id = int(chat_id)
    await query.answer("Loading recent media...")
    try:
        items = []
        async for msg in state["client"].get_chat_history(chat_id, limit=50):
            kind = _media_kind(msg)
            if kind:
                title = (msg.caption or "").strip().splitlines()[0][:45] if msg.caption else f"{kind.title()} file"
                items.append((msg.id, kind, title))
            if len(items) >= 15:
                break
        if not items:
            return await query.message.edit_text(
                "📭 **No recent media found in this bot chat.**\n\nMake sure you have received the file from that bot, then try again.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to bots", callback_data=f"dlback:{key}")]]),
            )
        state["selected_bot"] = chat_id
        rows = []
        for msg_id, kind, title in items:
            rows.append([InlineKeyboardButton(f"{kind.upper()} · {msg_id} · {title}"[:60], callback_data=f"dlfile:{key}:{msg_id}")])
        rows.append([InlineKeyboardButton("⬅️ Back to bots", callback_data=f"dlback:{key}")])
        await query.message.edit_text(
            "📂 **Recent media from selected bot**\n\nSelect the file you want JARVIS SRB to download and send to you.\n\n"
            "Only media visible to your logged-in account can be accessed.",
            reply_markup=InlineKeyboardMarkup(rows),
        )
    except FloodWait as fw:
        await query.message.edit_text(f"⏳ Telegram asked us to wait `{int(getattr(fw, 'value', 0) or 0)}` seconds. Please try again later.")
    except Exception as exc:
        await query.message.edit_text(f"❌ Could not read this bot's recent messages.\n\n{str(exc)[:500]}")


@app.on_callback_query(filters.regex(r"^dlback:[a-f0-9]{8}$"))
async def back_to_bot_list(_, query):
    key = query.data.split(":", 1)[1]
    state = await _get_session(query.from_user.id, key)
    if not state:
        return await query.answer("Menu expired. Send /dlbots again.", show_alert=True)
    rows = [[InlineKeyboardButton("🤖 " + _chat_label(chat), callback_data=f"dlbot:{key}:{chat.id}")] for chat in state["bots"].values()][:30]
    rows.append([InlineKeyboardButton("✖️ Close", callback_data=f"dlclose:{key}")])
    await query.answer()
    await query.message.edit_text("🤖 **Select another bot chat:**", reply_markup=InlineKeyboardMarkup(rows))


@app.on_callback_query(filters.regex(r"^dlfile:[a-f0-9]{8}:\d+$"))
async def download_from_other_bot(_, query):
    _, key, message_id = query.data.split(":", 2)
    state = await _get_session(query.from_user.id, key)
    if not state or not state.get("selected_bot"):
        return await query.answer("This selection expired. Send /dlbots again.", show_alert=True)
    await query.answer("Downloading selected file...")
    status = query.message
    temp_dir = tempfile.mkdtemp(prefix=f"jarvis_dlbot_{query.from_user.id}_")
    try:
        source = await state["client"].get_messages(state["selected_bot"], int(message_id))
        if not source or not _media_kind(source):
            raise RuntimeError("The selected message no longer contains accessible media.")
        await status.edit_text("⬇️ **Downloading from the selected bot chat...**\n\nPlease wait.")
        path = await asyncio.wait_for(
            state["client"].download_media(source, file_name=str(Path(temp_dir) / "media")),
            timeout=900,
        )
        if not path or not os.path.isfile(path) or os.path.getsize(path) <= 0:
            raise RuntimeError("Telegram did not return a valid media file.")
        if os.path.getsize(path) > 1_950 * 1024 * 1024:
            raise RuntimeError("This file is too large for the current Telegram upload route.")
        await status.edit_text("📤 **Sending file to you...**")
        kind = _media_kind(source)
        caption = "📥 **File retrieved from your selected Telegram bot chat.**\n\n⚡ JARVIS SRB"
        if kind == "video":
            await app.send_video(query.from_user.id, path, caption=caption, supports_streaming=True)
        elif kind == "audio":
            await app.send_audio(query.from_user.id, path, caption=caption)
        else:
            await app.send_document(query.from_user.id, path, caption=caption)
        await status.edit_text("✅ **File sent successfully.**\n\nUse /dlbots to retrieve another file.")
    except asyncio.TimeoutError:
        await status.edit_text("⌛ **Download timed out.** Please try the file again later.")
    except FloodWait as fw:
        await status.edit_text(f"⏳ **Telegram FloodWait**\n\nPlease retry after `{int(getattr(fw, 'value', 0) or 0)}` seconds.")
    except Exception as exc:
        await status.edit_text(
            "❌ **Could not download this file.**\n\n"
            f"{str(exc)[:700]}\n\n"
            "The source bot may have deleted the file, or Telegram may restrict downloading this media. Protected-content restrictions are not bypassed."
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
