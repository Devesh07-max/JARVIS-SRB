"""Browse media in Telegram bot chats visible to the user's own session.

This module never bypasses protected-content restrictions or reads chats the
logged-in account cannot access. Menus remain persistent while files are sent
as separate messages, and every visible file can be downloaded individually
or through Download All Above.
"""
import asyncio
import os
import tempfile
import shutil
import uuid
import time
from pathlib import Path

from pyrogram import filters, Client
from pyrogram.enums import ChatType
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import FloodWait

from safe_repo import app
from config import API_ID, API_HASH
from safe_repo.core.mongo import db, users_db
from safe_repo.core.func import chk_user, consume_trial, consume_plan_quota, is_premium_user, progress_bar, trial_remaining, plan_daily_remaining
from safe_repo.core.get_func import (
    _prepare_video_upload, video_metadata_async, upload_video_resilient,
    upload_audio_resilient, upload_document_resilient, upload_photo_resilient,
    PRE_UPLOAD_TIMEOUT,
)
from safe_repo.core.download_utils import download_media_resilient
from safe_repo.core.log_utils import log_event, user_label

DLBOT_SESSIONS = {}
BOT_PAGE_SIZE = 8
FILE_PAGE_SIZE = 8


def _media_kind(message):
    if getattr(message, "video", None) or getattr(message, "animation", None) or getattr(message, "video_note", None):
        return "video"
    if getattr(message, "audio", None) or getattr(message, "voice", None):
        return "audio"
    if getattr(message, "photo", None):
        return "photo"
    if getattr(message, "document", None):
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


def _bot_keyboard(state, page=0):
    bots = list(state["bots"].values())
    start = page * BOT_PAGE_SIZE
    rows = [[InlineKeyboardButton("🤖 " + _chat_label(chat), callback_data=f"dlbot:{state['key']}:{chat.id}:0")]
            for chat in bots[start:start + BOT_PAGE_SIZE]]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"dlbots:{state['key']}:{page-1}"))
    if start + BOT_PAGE_SIZE < len(bots):
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"dlbots:{state['key']}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("✖️ Close", callback_data=f"dlclose:{state['key']}")])
    return rows


def _files_keyboard(state, page=0):
    files = state.get("selected_files") or []
    start = page * FILE_PAGE_SIZE
    rows = []
    for item in files[start:start + FILE_PAGE_SIZE]:
        label = f"{item['kind'].upper()} · {item['id']} · {item['title']}"[:60]
        rows.append([InlineKeyboardButton(label, callback_data=f"dlfile:{state['key']}:{item['id']}")])
    if files:
        rows.append([InlineKeyboardButton(f"⬇️ Download All Above ({len(files)})", callback_data=f"dlall:{state['key']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"dlpage:{state['key']}:{page-1}"))
    if start + FILE_PAGE_SIZE < len(files):
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"dlpage:{state['key']}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("⬅️ Back to bots", callback_data=f"dlback:{state['key']}")])
    rows.append([InlineKeyboardButton("✖️ Close", callback_data=f"dlclose:{state['key']}")])
    return rows


@app.on_message(filters.private & filters.command("dlbots"))
async def list_other_bots(_, message):
    user_id = message.from_user.id
    if await chk_user(message, user_id):
        return
    data = await db.get_data(user_id)
    if not data or not data.get("session"):
        return await message.reply_text(
            "🔐 **Telegram login required**\n\nUse /login first. /dlbots only lists bot chats visible to your own logged-in Telegram account."
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
        bots = {}
        # Scan all dialogs returned by Telegram rather than only the first 150.
        async for dialog in client.get_dialogs(limit=0):
            chat = dialog.chat
            if getattr(chat, "type", None) == ChatType.BOT or getattr(chat, "is_bot", False):
                bots[str(chat.id)] = chat
        if not bots:
            await client.stop()
            return await status.edit_text(
                "📭 **No other bot chats found.**\n\nOpen the required bot, press Start and receive media first. Then try /dlbots again."
            )
        key = uuid.uuid4().hex[:8]
        state = {"client": client, "key": key, "bots": bots, "bot_page": 0, "selected_bot": None, "selected_files": [], "file_page": 0, "user": message.from_user}
        DLBOT_SESSIONS[user_id] = state
        await status.edit_text(
            "╭━━━〔 🤖 𝗢𝗧𝗛𝗘𝗥 𝗕𝗢𝗧 𝗙𝗜𝗟𝗘𝗦 〕━━━╮\n\n"
            f"Found `{len(bots)}` bot chat(s) in your Telegram dialogs.\n\n"
            "Select a bot to scan its available media history.\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯",
            reply_markup=InlineKeyboardMarkup(_bot_keyboard(state, 0)),
        )
    except Exception as exc:
        try:
            await client.stop()
        except Exception:
            pass
        await status.edit_text(f"❌ **Could not read your Telegram bot chats.**\n\n{str(exc)[:500]}\n\nPlease check /login and try again.")


@app.on_callback_query(filters.regex(r"^dlbots:[a-f0-9]{8}:\d+$"))
async def page_bot_list(_, query):
    _, key, page = query.data.split(":")
    state = await _get_session(query.from_user.id, key)
    if not state:
        return await query.answer("Menu expired. Send /dlbots again.", show_alert=True)
    state["bot_page"] = int(page)
    await query.answer()
    await query.message.edit_reply_markup(InlineKeyboardMarkup(_bot_keyboard(state, int(page))))


@app.on_callback_query(filters.regex(r"^dlclose:[a-f0-9]{8}$"))
async def close_dl_bots(_, query):
    key = query.data.split(":", 1)[1]
    state = await _get_session(query.from_user.id, key)
    if state and state.get("busy"):
        return await query.answer("A download is running. Close the menu after it finishes.", show_alert=True)
    if state:
        DLBOT_SESSIONS.pop(query.from_user.id, None)
        try:
            await state["client"].stop()
        except Exception:
            pass
    await query.answer("Closed")
    await query.message.edit_text("❎ **/dlbots closed.**")


@app.on_callback_query(filters.regex(r"^dlbot:[a-f0-9]{8}:-?\d+:\d+$"))
async def show_bot_media(_, query):
    _, key, chat_id, page = query.data.split(":")
    state = await _get_session(query.from_user.id, key)
    if not state:
        return await query.answer("This menu expired. Send /dlbots again.", show_alert=True)
    chat_id = int(chat_id)
    if state.get("busy"):
        return await query.answer("Please wait until the current download finishes.", show_alert=True)
    await query.answer("Scanning media history...")
    try:
        items = []
        scanned = 0
        async for msg in state["client"].get_chat_history(chat_id, limit=0):
            scanned += 1
            kind = _media_kind(msg)
            if kind:
                title = (msg.caption or "").strip().splitlines()[0][:45] if msg.caption else f"{kind.title()} file"
                items.append({"id": int(msg.id), "kind": kind, "title": title})
        if not items:
            return await query.message.edit_text(
                "📭 **No media found in this bot chat's scanned history.**\n\nOnly media visible to your logged-in account can be retrieved.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to bots", callback_data=f"dlback:{key}")]]),
            )
        state["selected_bot"] = chat_id
        state["selected_files"] = items
        state["file_page"] = int(page)
        chat = state["bots"].get(str(chat_id))
        await query.message.edit_text(
            "╭━━━〔 📂 𝗕𝗢𝗧 𝗠𝗘𝗗𝗜𝗔 〕━━━╮\n\n"
            f"🤖 **Bot:** {_chat_label(chat) if chat else chat_id}\n"
            f"📁 **Media found:** `{len(items)}` (scanned all history available to your account)\n\n"
            "Tap a file to download it. The menu stays available after each download.\n"
            "Use **Download All Above** to process the complete list.\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯",
            reply_markup=InlineKeyboardMarkup(_files_keyboard(state, int(page))),
        )
    except FloodWait as fw:
        wait = int(getattr(fw, "value", 0) or 0)
        await query.message.reply_text(f"⏳ Telegram asked to pause for `{wait}` seconds. Try /dlbots again after the wait.")
    except Exception as exc:
        await query.message.reply_text(f"❌ Could not read this bot's history.\n\n{str(exc)[:500]}")


@app.on_callback_query(filters.regex(r"^dlpage:[a-f0-9]{8}:\d+$"))
async def page_file_list(_, query):
    _, key, page = query.data.split(":")
    state = await _get_session(query.from_user.id, key)
    if not state:
        return await query.answer("Menu expired. Send /dlbots again.", show_alert=True)
    state["file_page"] = int(page)
    await query.answer()
    await query.message.edit_reply_markup(InlineKeyboardMarkup(_files_keyboard(state, int(page))))


@app.on_callback_query(filters.regex(r"^dlback:[a-f0-9]{8}$"))
async def back_to_bot_list(_, query):
    key = query.data.split(":", 1)[1]
    state = await _get_session(query.from_user.id, key)
    if not state:
        return await query.answer("Menu expired. Send /dlbots again.", show_alert=True)
    await query.answer()
    await query.message.edit_text("🤖 **Select another bot chat:**", reply_markup=InlineKeyboardMarkup(_bot_keyboard(state, state.get("bot_page", 0))))


async def _retrieve_one(user_id, state, item, status):
    source = await state["client"].get_messages(state["selected_bot"], int(item["id"]))
    if not source or not _media_kind(source):
        raise RuntimeError("The selected message no longer contains accessible media.")
    temp_dir = tempfile.mkdtemp(prefix=f"jarvis_dlbot_{user_id}_")
    try:
        path = await download_media_resilient(
            state["client"], source, user_id=user_id, retries=3,
            progress=progress_bar, progress_args=("**DOWNLOADING:**\n", status, time.time()),
            progress_message=status,
        )
        if not path or not os.path.isfile(path) or os.path.getsize(path) <= 0:
            raise RuntimeError("Telegram did not return a valid media file.")
        if os.path.getsize(path) > 1_950 * 1024 * 1024:
            raise RuntimeError("This file is too large for the current Telegram upload route.")
        caption = (source.caption or "📥 Retrieved from your selected Telegram bot chat.")[:900]
        kind = _media_kind(source)
        await status.edit_text("📤 **Uploading to your chat...**")
        if getattr(source, "video", None):
            metadata, source_thumb = await _prepare_video_upload(state["client"], source, path, user_id)
            try:
                await upload_video_resilient([], path, caption, status, user_id,
                    metadata.get("width") or 0, metadata.get("height") or 0,
                    metadata.get("duration") or 0, source_thumb=source_thumb,
                    upload_chat_id=user_id)
            finally:
                if source_thumb and os.path.isfile(source_thumb):
                    os.remove(source_thumb)
        elif getattr(source, "audio", None):
            await upload_audio_resilient([], path, caption, status, user_id,
                duration=getattr(source.audio, "duration", 0) or 0,
                performer=getattr(source.audio, "performer", None), title=getattr(source.audio, "title", None),
                upload_chat_id=user_id)
        elif getattr(source, "photo", None):
            await upload_photo_resilient([], path, caption, status, user_id, upload_chat_id=user_id)
        elif getattr(source, "video_note", None):
            await app.send_video_note(user_id, path, progress=progress_bar,
                progress_args=("**UPLOADING:**\n", status, time.time()))
        elif getattr(source, "voice", None):
            await app.send_voice(user_id, path, caption=caption, progress=progress_bar,
                progress_args=("**UPLOADING:**\n", status, time.time()))
        elif getattr(source, "animation", None):
            await app.send_animation(user_id, path, caption=caption, progress=progress_bar,
                progress_args=("**UPLOADING:**\n", status, time.time()))
        else:
            await upload_document_resilient([], path, caption, status, user_id, upload_chat_id=user_id)
        if await is_premium_user(user_id):
            await consume_plan_quota(user_id, 1)
        else:
            used = await consume_trial(user_id)
            limit = await users_db.get_trial_limit(user_id)
            await log_event(f"🤖 DLBOTS FILE SUCCESS\n👤 {user_label(user_id)}\n🆔 `{user_id}`\n📊 Trial: `{used}/{limit}`")
        return True
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        try:
            if 'path' in locals() and path and os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass


async def _run_downloads(user_id, state, items):
    if state.get("busy"):
        await app.send_message(user_id, "⏳ A DLBots download is already running. Please wait for it to finish.")
        return
    state["busy"] = True
    successes, failures = 0, []
    try:
        for index, item in enumerate(items, 1):
            # Shared quota is checked for every file in Download All Above.
            if await is_premium_user(user_id):
                daily_left = await plan_daily_remaining(user_id)
                if daily_left is not None and daily_left <= 0:
                    await app.send_message(user_id, "🚫 **Your daily plan quota is finished.**\nUse /buypremium for an upgrade.")
                    failures.append(f"{item['id']}: daily quota exhausted")
                    break
            elif await trial_remaining(user_id) == 0:
                await app.send_message(user_id, "🚫 **Your shared free trials are finished.**\nUse /buypremium to continue.")
                failures.append(f"{item['id']}: free uses exhausted")
                break
            status = await app.send_message(user_id, f"⬇️ **DLBots · {index}/{len(items)}**\n\nPreparing `{item['title'][:80]}`...")
            try:
                await _retrieve_one(user_id, state, item, status)
                await status.edit_text(f"✅ **Sent:** {item['title'][:100]}")
                successes += 1
            except FloodWait as fw:
                wait = int(getattr(fw, "value", 0) or 0)
                await status.edit_text(f"⏳ **FloodWait:** pausing for `{wait}` seconds, then resuming this file.")
                await asyncio.sleep(wait + 1)
                try:
                    await _retrieve_one(user_id, state, item, status)
                    await status.edit_text(f"✅ **Sent after FloodWait:** {item['title'][:100]}")
                    successes += 1
                except Exception as exc:
                    failures.append(f"{item['id']}: {str(exc)[:140]}")
                    await status.edit_text(f"❌ **Failed:** {item['title'][:80]}\n\n{str(exc)[:500]}")
            except Exception as exc:
                failures.append(f"{item['id']}: {str(exc)[:140]}")
                await status.edit_text(f"❌ **Failed:** {item['title'][:80]}\n\n{str(exc)[:500]}")
        summary = f"📦 **DLBots complete**\n\n✅ Sent: `{successes}`\n❌ Failed: `{len(failures)}`"
        if failures:
            summary += "\n\n" + "\n".join(f"• `{x}`" for x in failures[:20])
        await app.send_message(user_id, summary)
    finally:
        state["busy"] = False


@app.on_callback_query(filters.regex(r"^dlfile:[a-f0-9]{8}:\d+$"))
async def download_from_other_bot(_, query):
    _, key, message_id = query.data.split(":")
    state = await _get_session(query.from_user.id, key)
    if not state or not state.get("selected_bot"):
        return await query.answer("This selection expired. Send /dlbots again.", show_alert=True)
    item = next((x for x in state.get("selected_files", []) if int(x["id"]) == int(message_id)), None)
    if not item:
        return await query.answer("This file is no longer in the menu.", show_alert=True)
    if state.get("busy"):
        return await query.answer("A download is already running.", show_alert=True)
    await query.answer("Download started")
    asyncio.create_task(_run_downloads(query.from_user.id, state, [item]))


@app.on_callback_query(filters.regex(r"^dlall:[a-f0-9]{8}$"))
async def download_all_bot_media(_, query):
    key = query.data.split(":", 1)[1]
    state = await _get_session(query.from_user.id, key)
    if not state or not state.get("selected_files"):
        return await query.answer("This menu expired. Select a bot again.", show_alert=True)
    if state.get("busy"):
        return await query.answer("A download is already running.", show_alert=True)
    await query.answer("Download All started")
    asyncio.create_task(_run_downloads(query.from_user.id, state, list(state["selected_files"])))
