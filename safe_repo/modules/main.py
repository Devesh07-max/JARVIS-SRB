# safe_repo

import asyncio
import time
from pyrogram import filters
from safe_repo import app
from config import API_ID, API_HASH, BATCH_SIZE, BATCH_DELAY, FREE_TRIAL_USES, PLAN_CONFIG, MEDIA_FOOTER, FILE_TASK_TIMEOUT, UPLOAD_TOTAL_TIMEOUT
from safe_repo.core.get_func import (
    get_target_chat_ids, get_msg, upload_video_resilient, upload_document_resilient,
    upload_audio_resilient, upload_photo_resilient, _copy_uploaded_message_to_targets,
    with_media_footer, PRE_UPLOAD_TIMEOUT,
)
from safe_repo.core.download_utils import download_media_resilient
from safe_repo.core.func import *
from safe_repo.core.mongo import db
from safe_repo.core.mongo import users_db
from safe_repo.core.log_utils import log_event, user_label
from safe_repo.core.func import is_premium_user, trial_remaining, consume_trial, check_batch_access, consume_plan_quota, get_active_plan, plan_daily_remaining
from safe_repo.core.mongo.stats_db import increment
from pyrogram.errors import FloodWait
from pyrogram import Client
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

@app.on_message(filters.text & filters.regex(r'(?i)(?:https?://)?(?:t\.me|telegram\.me)/[^\s]+'), group=-10)
async def single_link(_, message):
    # Highest-priority text handler for direct Telegram message links.
    # This intentionally reuses the same get_msg() pipeline used by batch.
    user_id = message.chat.id
    link = get_link(message.text or "")
    if not link:
        return

    if user_id in active_tasks:
        await message.reply_text("⏳ **A file or batch is already processing.**\n\nUse /reset to stop it before starting another request.")
        return
    active_tasks[user_id] = asyncio.current_task()
    _register_task_state_cleanup(user_id)
    cancel_requested.discard(user_id)

    if await chk_user(message, user_id) == 1:
        active_tasks.pop(user_id, None)
        cancel_requested.discard(user_id)
        return

    join = await subscribe(_, message)
    if join == 1:
        active_tasks.pop(user_id, None)
        cancel_requested.discard(user_id)
        return

    msg = await message.reply("⏳ **Processing your request...**\n\n🔎 Checking the message and preparing the file.")
    try:
        await log_event(
            f"👤 USER REQUEST\n{user_label(message.from_user)}\n🆔 `{user_id}`\n🔗 `{link}`"
        )
    except Exception as e:
        print(f"Request logging error for {user_id}: {e}")
    link_route = telegram_link_route(link)
    if link_route == "invalid":
        await msg.edit_text("❌ **Invalid Telegram message link.**\n\nPlease check the chat and message ID.")
        return

    userbot = app if link_route == "public" else None
    owns_userbot = False
    if link_route != "public":
        data = await db.get_data(user_id)
        if not data or not data.get("session"):
            await msg.edit_text("🔐 **Login required**\n\nPlease use /login first, then send the link again.")
            return
        userbot = Client(f":userbot_{user_id}:", api_id=API_ID, api_hash=API_HASH, session_string=data["session"])
        owns_userbot = True

    try:
        if owns_userbot:
            try:
                await userbot.start()
            except Exception:
                await msg.edit_text("🔐 **Session expired**\n\nPlease use /login again and then retry your link.")
                return

        if 't.me/+' in link:
            q = await userbot_join(userbot, link)
            await msg.edit_text(q)
            return

        if link_route in ("public", "private"):
            success = await asyncio.wait_for(
                    get_msg(userbot, user_id, msg.id, link, 0, message),
                    timeout=FILE_TASK_TIMEOUT,
                )
            await increment("downloads")
            if success:
                await _log_file_result(user_id, f"message_{link.rsplit('/', 1)[-1]}", "download/upload", "SUCCESS", link=link)
                await increment("download_success")
                if await is_premium_user(user_id):
                    await consume_plan_quota(user_id, 1)
            else:
                await _log_file_result(
                    user_id,
                    f"message_{link.rsplit('/', 1)[-1]}",
                    "download/upload",
                    "FAILED",
                    "Telegram media transfer failed after retry",
                    link,
                )
                await increment("download_failed")
            if success and not await is_premium_user(user_id):
                used = await consume_trial(user_id)
                trial_limit = await users_db.get_trial_limit(user_id)
                remaining = max(0, trial_limit - used)
                await log_event(
                    f"🆓 FREE TRIAL USE\n👤 {user_label(message.from_user)}\n🆔 `{user_id}`\n"
                    f"📊 Used: `{used}/{trial_limit}`\n🔋 Remaining: `{remaining}`"
                )
                if remaining == 0:
                    await app.send_message(user_id, "╭━━━〔 🎁 𝗧𝗥𝗜𝗔𝗟 𝗙𝗜𝗡𝗜𝗦𝗛𝗘𝗗 〕━━━╮\n\nYou have used all your free trial credits.\n\n💎 Use /buypremium to continue with Premium.\n╰━━━━━━━━━━━━━━━━━━━━╯")
    except FloodWait as fw:
        await msg.edit_text(f'⏳ **Telegram rate limit**\n\nPlease wait `{fw.x}` seconds, then try again.')
    except Exception as e:
        await _log_file_result(
            user_id,
            f"message_{link.rsplit('/', 1)[-1]}",
            "download/upload",
            "FAILED",
            str(e),
            link,
        )
        await increment("download_failed")
        await msg.edit_text(f"❌ **Unable to process this link**\n\n🔗 `{link}`\n\n<blockquote>{e}</blockquote>")
    except asyncio.CancelledError:
        cancel_requested.add(user_id)
        await _log_file_result(
            user_id,
            f"message_{link.rsplit('/', 1)[-1]}",
            "download/upload",
            "CANCELLED",
            "Operation cancelled",
            link,
        )
        try:
            await app.send_message(user_id, "╭━━━〔 ⛔ 𝗖𝗔𝗡𝗖𝗘𝗟𝗟𝗘𝗗 〕━━━╮\n\nThe current file processing has been cancelled.\n╰━━━━━━━━━━━━━━━━━━━━╯")
        except Exception:
            pass
        raise
    finally:
        active_tasks.pop(user_id, None)
        cancel_requested.discard(user_id)
        if owns_userbot and userbot is not None:
            try:
                await userbot.stop()
            except Exception:
                pass


# Direct media/file intake:
# Users can send a video/document/photo directly to the bot without a t.me message link.
# This is useful for files received from file-store bots or other chats. The bot downloads
# the incoming media to local storage first, then uploads that local file to the configured
# destination. No source message link is required.
@app.on_message(filters.media & ~filters.command([
    "start", "help", "id", "login", "logout", "myplan", "premium", "buypremium",
    "batch", "cancel", "reset", "earntokens", "add", "rem", "check", "stats", "gcast", "announce", "speedtest", "admincommands", "media", "dlbots", "autoupdate", "redeempremium"
]))
async def direct_media(_, message):
    # Direct-media intake is user-driven; ignore channel/anonymous-admin posts.
    if not getattr(message, "from_user", None):
        return
    user_id = message.chat.id
    try:
        await users_db.add_user(message.from_user)
    except Exception as e:
        print(f"User registration error: {e}")

    if user_id in active_tasks:
        return
    active_tasks[user_id] = asyncio.current_task()
    _register_task_state_cleanup(user_id)
    cancel_requested.discard(user_id)

    if await chk_user(message, user_id) == 1:
        active_tasks.pop(user_id, None)
        cancel_requested.discard(user_id)
        return
    join = await subscribe(_, message)
    if join == 1:
        active_tasks.pop(user_id, None)
        cancel_requested.discard(user_id)
        return

    if not (message.video or message.document or message.photo or message.audio):
        active_tasks.pop(user_id, None)
        cancel_requested.discard(user_id)
        return

    status = await message.reply(
        "╭━━━〔 📥 𝗗𝗜𝗥𝗘𝗖𝗧 𝗙𝗜𝗟𝗘 〕━━━╮\n\n"
        "⬇️ Downloading the file...\n"
        "🔗 No Telegram link is required.\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯"
    )

    data = await db.get_data(user_id)
    if not data or not data.get("session"):
        await status.edit_text(
            "╭━━━〔 🔐 𝗟𝗢𝗚𝗜𝗡 𝗥𝗘𝗤𝗨𝗜𝗥𝗘𝗗 〕━━━╮\n\n"
            "Please use /login first to enable file processing.\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯"
        )
        return

    # Download through the bot's own accessible incoming message.
    local_file = None
    requested_filename = (
        getattr(getattr(message, "document", None), "file_name", None)
        or getattr(getattr(message, "audio", None), "file_name", None)
        or getattr(getattr(message, "video", None), "file_name", None)
        or f"message_{message.id}"
    )
    try:
        local_file = await message.download(progress=progress_bar, progress_args=("**DOWNLOADING:**\\n", status, time.time()))
        if not local_file or not os.path.exists(local_file):
            raise RuntimeError("Telegram did not provide a downloadable local file.")

        target_chat_ids = get_target_chat_ids(user_id)
        caption = message.caption or ""
        custom_caption = get_user_caption_preference(user_id)
        delete_words = load_delete_words(user_id)
        replacements = load_replacement_words(user_id)
        for word in delete_words:
            caption = caption.replace(word, "")
        for word, replace_word in replacements.items():
            caption = caption.replace(word, replace_word)
        if custom_caption:
            caption = f"{caption}\n\n__**{custom_caption}**__" if caption else f"__**{custom_caption}**__"
        caption = with_media_footer(
            caption,
            include_footer=(await get_active_plan(user_id) != "elite"),
        )

        await status.edit_text(
            "╭━━━〔 📤 𝗨𝗣𝗟𝗢𝗔𝗗𝗜𝗡𝗚 〕━━━╮\n\n"
            "🚀 Uploading your file...\n"
            "🖼️ Thumbnail is optional.\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯"
        )

        sent = None
        if message.video:
            metadata = await asyncio.wait_for(
                video_metadata_async(
                    local_file,
                    timeout=PRE_UPLOAD_TIMEOUT,
                    fallback={
                        "width": getattr(message.video, "width", 0) or 0,
                        "height": getattr(message.video, "height", 0) or 0,
                        "duration": getattr(message.video, "duration", 0) or 0,
                    },
                ),
                timeout=PRE_UPLOAD_TIMEOUT,
            )
            sent = await upload_video_resilient(
                target_chat_ids, local_file, caption, status, user_id,
                metadata.get("width") or 0, metadata.get("height") or 0, metadata.get("duration") or 0
            )
        elif message.audio:
            sent = await upload_audio_resilient(
                target_chat_ids,
                local_file,
                caption,
                status,
                user_id,
                duration=getattr(message.audio, "duration", 0),
                performer=getattr(message.audio, "performer", None),
                title=getattr(message.audio, "title", None),
            )
        elif message.document:
            sent = await upload_document_resilient(target_chat_ids, local_file, caption, status, user_id)
        elif message.photo:
            sent = await asyncio.wait_for(
                upload_photo_resilient(
                    target_chat_ids, local_file, caption, status, user_id
                ),
                timeout=UPLOAD_TOTAL_TIMEOUT,
            )

        if sent:
            await _log_file_result(
                user_id, requested_filename, "direct download/upload", "SUCCESS"
            )
            await increment("downloads")
            await increment("download_success")
            await status.delete()
            if not await is_premium_user(user_id):
                used = await consume_trial(user_id)
                trial_limit = await users_db.get_trial_limit(user_id)
                remaining = max(0, trial_limit - used)
                await log_event(
                    f"📥 DIRECT FILE SAVED\\n👤 {user_label(message.from_user)}\\n"
                    f"🆔 `{user_id}`\\n📊 Trial: `{used}/{trial_limit}`"
                )
                if remaining == 0:
                    await app.send_message(
                        user_id,
                        "╭━━━〔 🎁 𝗧𝗥𝗜𝗔𝗟 𝗙𝗜𝗡𝗜𝗦𝗛𝗘𝗗 〕━━━╮\\n\\n"
                        "You have used all your free trial credits.\\n\\n"
                        "💎 Use /buypremium to continue with Premium.\\n"
                        "╰━━━━━━━━━━━━━━━━━━━━╯"
                    )
    except Exception as e:
        await _log_file_result(
            user_id,
            requested_filename,
            "direct download/upload",
            "FAILED",
            str(e),
        )
        await increment("download_failed")
        try:
            await status.edit_text(
                f"╭━━━〔 ❌ 𝗙𝗜𝗟𝗘 𝗙𝗔𝗜𝗟𝗘𝗗 〕━━━╮\\n\\n"
                f"Unable to process this file.\\n\\n<blockquote>{e}</blockquote>\\n"
                f"╰━━━━━━━━━━━━━━━━━━━━╯"
            )
        except Exception:
            pass
    except asyncio.CancelledError:
        cancel_requested.add(user_id)
        await _log_file_result(
            user_id,
            requested_filename,
            "direct download/upload",
            "CANCELLED",
            "Operation cancelled",
        )
        try:
            await app.send_message(user_id, "╭━━━〔 ⛔ 𝗖𝗔𝗡𝗖𝗘𝗟𝗟𝗘𝗗 〕━━━╮\n\nThe current file processing has been cancelled.\n╰━━━━━━━━━━━━━━━━━━━━╯")
        except Exception:
            pass
        raise
    finally:
        active_tasks.pop(user_id, None)
        cancel_requested.discard(user_id)
        if local_file:
            try:
                os.remove(local_file)
            except OSError:
                pass


users_loop = {}
active_tasks = {}

cancel_requested = set()

def _release_user_task_state(user_id, task=None):
    """Release only the completed task's state, never a newer user's task."""
    if task is None or active_tasks.get(user_id) is task:
        active_tasks.pop(user_id, None)
    users_loop.pop(user_id, None)
    cancel_requested.discard(user_id)


def _register_task_state_cleanup(user_id):
    task = asyncio.current_task()
    if task is not None:
        task.add_done_callback(
            lambda completed: _release_user_task_state(user_id, completed)
        )


async def _log_file_result(user, filename, stage, status, reason="", link=""):
    """Best-effort per-item audit log with a hard bound."""
    details = (
        f"📄 File: `{filename}`\n👤 User ID: `{user}`\n"
        f"🔧 Stage: `{stage}`\n📊 Status: `{status}`"
    )
    if link:
        details += f"\n🔗 `{link}`"
    if reason:
        details += f"\n⚠️ Reason: `{str(reason)[:500]}`"
    try:
        await asyncio.wait_for(log_event(f"🧾 FILE RESULT\n{details}"), timeout=10)
    except Exception as exc:
        print(f"Per-file logging error for {user}: {exc}")


async def _send_batch_issue_links(chat_id, failed_links, skipped_links):
    """Send one final failed/skipped report after the batch finishes.

    Keep failed and skipped links together so the user does not receive a
    stream of per-item error messages while the batch is running. Very large
    reports are split only when Telegram's message-size limit requires it.
    """
    if not failed_links and not skipped_links:
        return

    header = "╭━━━〔 ⚠️ 𝗕𝗔𝗧𝗖𝗛 𝗜𝗦𝗦𝗨𝗘𝗦 〕━━━╮\n\n"
    footer = "╰━━━━━━━━━━━━━━━━━━━━╯"
    sections = []
    if failed_links:
        sections.append(
            "❌ **Failed files**\n"
            + "\n".join(f"`{message_id}` — {link}" for message_id, link in failed_links)
        )
    if skipped_links:
        sections.append(
            "⏭️ **Skipped files**\n"
            + "\n".join(f"`{message_id}` — {link}" for message_id, link in skipped_links)
        )

    body = "\n\n".join(sections)
    # Keep a little room below Telegram's hard message limit.
    if len(header) + len(body) + len(footer) <= 3800:
        try:
            await app.send_message(chat_id, f"{header}{body}\n\n{footer}")
        except Exception as exc:
            print(f"Batch issue report error: {exc}")
        return

    # If the link list itself is very large, split it into chunks. This is
    # unavoidable for Telegram's message-size limit; normal batches use one
    # final issue message.
    current = header
    chunks = []
    for section in sections:
        for line in section.splitlines(True):
            if len(current) + len(line) + len(footer) > 3800 and current != header:
                chunks.append(current.rstrip())
                current = header
            current += line
        current += "\n"
    if current.strip() != header.strip():
        chunks.append(current.rstrip())

    for index, chunk in enumerate(chunks):
        suffix = f"\n\n{footer}"
        try:
            await app.send_message(chat_id, chunk + suffix)
        except Exception as exc:
            print(f"Batch issue report chunk {index + 1} error: {exc}")


async def _move_batch_progress_to_bottom(chat_id, progress_msg, text):
    """Move the single batch progress message below the latest delivered file.

    Telegram message edits do not move a message in chat history. After each
    sequential file finishes, delete the old progress message and create the
    next one at the bottom. The batch remains strictly sequential; this helper
    never starts another file. If deletion/sending is temporarily unavailable,
    fall back to editing the existing message so the batch itself is not lost.
    """
    if progress_msg is not None:
        try:
            await progress_msg.delete()
        except Exception as exc:
            print(f"Batch progress delete error for {chat_id}: {exc}")
    try:
        return await app.send_message(chat_id, text)
    except Exception as exc:
        print(f"Batch progress relocate error for {chat_id}: {exc}")
        if progress_msg is not None:
            try:
                await progress_msg.edit_text(text)
                return progress_msg
            except Exception as edit_exc:
                print(f"Batch progress fallback edit error for {chat_id}: {edit_exc}")
        return progress_msg


async def _pause_batch_for_floodwait(user_id, chat_id, progress_msg, wait_s, current_link, current_index, batch_end):
    """Persist and automatically resume the current batch item after FloodWait."""
    wait_s = max(0, int(wait_s or 0))
    try:
        await db.update_one(
            {"_id": user_id},
            {"$set": {"paused_batch": {
                "current_link": current_link,
                "current_index": int(current_index),
                "batch_end": int(batch_end),
                "wait_until": int(time.time()) + wait_s,
                "saved_at": int(time.time()),
            }}},
            upsert=True,
        )
    except Exception as exc:
        print(f"Could not persist paused batch state for {user_id}: {exc}")

    try:
        await progress_msg.edit_text(
            "⏸️ **Batch automatically paused by Telegram FloodWait**\n\n"
            f"📄 Current file: `{current_index}`\n"
            f"⏳ Telegram has asked the bot to wait `{wait_s}` seconds.\n\n"
            "The current file is retained. The bot will automatically retry it "
            "after the wait and continue the remaining batch."
        )
    except Exception:
        pass

    # Keep the same file index/link and resume without a manual button.
    await asyncio.sleep(wait_s + 1)
    try:
        await progress_msg.edit_text(
            "▶️ **FloodWait finished — automatically resuming**\n\n"
            f"📄 Retrying current file `{current_index}` now..."
        )
    except Exception:
        pass
    try:
        await db.update_one({"_id": user_id}, {"$unset": {"paused_batch": ""}})
    except Exception as exc:
        print(f"Could not clear paused batch state for {user_id}: {exc}")
    return True


@app.on_callback_query(filters.regex(r"^batchresume$"))
async def resume_paused_batch(_, query):
    # Compatibility for old messages containing the former manual-resume button.
    await query.answer("Automatic resume is enabled. No action is needed.", show_alert=True)


async def recover_interrupted_batches():
    """Report every remaining link if Render restarts during an active batch.

    A Telegram upload may have reached the destination just before a process
    restart, so the bot does not blindly replay the in-flight item and risk a
    duplicate. It reports the in-flight item plus all later links instead.
    """
    try:
        cursor = db.find({"paused_batch": {"$exists": True}})
        async for record in cursor:
            user_id = record.get("_id")
            state = record.get("paused_batch") or {}
            link = state.get("current_link") or ""
            current_index = int(state.get("current_index") or 0)
            batch_end = int(state.get("batch_end") or current_index)
            pending_links = []
            if link and current_index:
                base = link.rsplit("/", 1)[0]
                for message_id in range(current_index, batch_end + 1):
                    raw = f"{base}/{message_id}"
                    pending_links.append(get_link(raw) or raw)
            lines = "\n".join(f"• {item}" for item in pending_links[:60])
            extra = f"\n…and `{len(pending_links) - 60}` more links." if len(pending_links) > 60 else ""
            try:
                await app.send_message(
                    user_id,
                    "⚠️ **Batch interrupted by a server restart.**\n\n"
                    "The active transfer cannot be safely replayed automatically because "
                    "Telegram may have accepted the last upload before the process stopped. "
                    "To avoid duplicate uploads, the bot has not replayed it.\n\n"
                    f"❌ Current and remaining links (`{len(pending_links)}`):\n"
                    f"{lines or 'The saved current link is unavailable.'}{extra}\n\n"
                    "You can resend only the links that still need processing."
                )
            except Exception as exc:
                print(f"Could not notify interrupted batch user {user_id}: {exc}")
            await db.update_one({"_id": user_id}, {"$unset": {"paused_batch": ""}})
    except Exception as exc:
        print(f"Interrupted batch recovery scan failed: {exc}")



@app.on_message(filters.command("batch"))
async def batch_link(_, message):
    user_id = message.chat.id
    if user_id in active_tasks:
        await message.reply_text("⏳ **A file or batch is already processing.**\n\nUse /reset to stop it before starting another request.")
        return
    active_tasks[user_id] = asyncio.current_task()
    _register_task_state_cleanup(user_id)
    users_loop[user_id] = True
    cancel_requested.discard(user_id)
    batch_started_at = time.monotonic()
    processed = failed = skipped = attempted = 0
    total = 0
    failed_links = []
    skipped_links = []
    if await chk_user(message, user_id) == 1:
        active_tasks.pop(user_id, None)
        users_loop.pop(user_id, None)
        return

    userbot = None
    current_index = None
    current_link = ""
    completed_index = 0
    start_id = None
    cs = None
    cl = None

    def add_remaining_batch_links():
        """Add the in-flight and not-yet-started links to the final issue report."""
        if cs is None or cl is None or not start_id:
            return
        if current_index is not None and current_index > completed_index and current_link:
            already_ids = {mid for mid, _ in failed_links + skipped_links}
            if current_index not in already_ids:
                failed_links.append((current_index, current_link))
            next_unprocessed = current_index + 1
        else:
            next_unprocessed = completed_index + 1
        base_for_report = start_id.rsplit("/", 1)[0]
        already_reported = {mid for mid, _ in failed_links + skipped_links}
        for pending_id in range(max(cs, next_unprocessed), cl + 1):
            if pending_id in already_reported:
                continue
            raw = f"{base_for_report}/{pending_id}"
            skipped_links.append((pending_id, get_link(raw) or raw))

    start = await app.ask(message.chat.id, text="╭━━━〔 1️⃣ 𝗦𝗧𝗔𝗥𝗧 𝗟𝗜𝗡𝗞 〕━━━╮\n\nSend the first Telegram message link.\n\n💡 Example: `https://t.me/channel/100`\n╰━━━━━━━━━━━━━━━━━━━━╯")
    start_id = get_link(start.text or "")
    last = await app.ask(message.chat.id, text="╭━━━〔 2️⃣ 𝗘𝗡𝗗 𝗟𝗜𝗡𝗞 〕━━━╮\n\nSend the last Telegram message link.\n\n💡 Example: `https://t.me/channel/110`\n╰━━━━━━━━━━━━━━━━━━━━╯")
    last_id = get_link(last.text or "")

    if not start_id or not last_id:
        await app.send_message(message.chat.id, "❌ **Invalid start or end link.**\n\nPlease send valid Telegram message links and try /batch again.")
        return

    try:
        cs = int(start_id.rstrip('/').split('/')[-1])
        cl = int(last_id.rstrip('/').split('/')[-1])
    except ValueError:
        await app.send_message(message.chat.id, "❌ **Invalid message IDs.**\n\nPlease check both Telegram links and try again.")
        return

    if cl < cs:
        await app.send_message(message.chat.id, "❌ **Invalid range.**\n\nThe end message ID must be greater than or equal to the start message ID.")
        return

    total = cl - cs + 1
    allowed, reason = await check_batch_access(user_id, total)
    if not allowed:
        await app.send_message(
            message.chat.id,
            f"🚫 **/batch access denied**\n\n{reason}\n\n💎 Use /buypremium to view available plans."
        )
        return
    if total > BATCH_SIZE:
        await app.send_message(
            message.chat.id,
            f"📦 **Batch limit reached**\n\nMaximum allowed: **{BATCH_SIZE} messages**.\nPlease reduce the range and try again."
        )
        return

    join = await subscribe(_, message)
    if join == 1:
        return

    data = await db.get_data(user_id)
    if not data or not data.get("session"):
        await app.send_message(message.chat.id, "🔐 **Login required**\n\nPlease use /login first, then send the link again.")
        return

    userbot = Client(f":userbot_{user_id}:", api_id=API_ID, api_hash=API_HASH, session_string=data["session"])
    try:
        try:
            await userbot.start()
        except Exception:
            await app.send_message(message.chat.id, "🔐 **Session expired**\n\nPlease use /login again before starting the batch.")
            return

        users_loop[user_id] = True
        await increment("batches")
        await log_event(
            f"📦 BATCH STARTED\n👤 {user_label(message.from_user)}\n"
            f"🆔 `{user_id}`\n🔢 `{total}` messages\n"
            f"🔗 `{start_id}` → `{last_id}`"
        )

        # Counters are initialized before the interactive prompts so /cancel can always produce a report.
        started_at = batch_started_at
        current_index = None
        current_link = ""
        progress_msg = await app.send_message(
            message.chat.id,
            f"📦 **Batch Progress**\n\n🔄 Preparing file `1/{total}`..."
        )

        for i in range(cs, cl + 1):
            if not users_loop.get(user_id, False) or user_id in cancel_requested:
                break

            if await chk_user(message, user_id) == 1:
                cancel_requested.add(user_id)
                break

            current_index = i
            attempted += 1
            try:
                await progress_msg.edit_text(
                    f"📦 **Batch Progress**\n\n🔄 Processing file `{i - cs + 1}/{total}`..."
                )
            except Exception as exc:
                print(f"Batch progress edit error for {user_id}: {exc}")
            msg = progress_msg
            try:
                base = start_id.rsplit('/', 1)[0]
                url = f"{base}/{i}"
                link = get_link(url) or url
                current_link = link
                while True:
                    try:
                        success = await get_msg(userbot, user_id, msg.id, link, 0, message, batch_position=(i - cs + 1), batch_total=total)
                        break
                    except FloodWait as fw:
                        wait_s = int(getattr(fw, "value", 0) or 0)
                        resumed = await _pause_batch_for_floodwait(
                            user_id, message.chat.id, progress_msg, wait_s, link, i, cl
                        )
                        if not resumed:
                            failed += 1
                            failed_links.append((i, link))
                            await increment("download_failed")
                            cancel_requested.add(user_id)
                            break
                        if not users_loop.get(user_id, False) or user_id in cancel_requested:
                            break
                if user_id in cancel_requested:
                    break
                await increment("downloads")
                if success is True:
                    await increment("download_success")
                    processed += 1
                    if await is_premium_user(user_id):
                        await consume_plan_quota(user_id, 1)
                    if not await is_premium_user(user_id):
                        used = await consume_trial(user_id)
                        trial_limit = await users_db.get_trial_limit(user_id)
                        remaining = max(0, trial_limit - used)
                        if remaining == 0:
                            await app.send_message(
                                user_id,
                                "╭━━━〔 🎁 𝗧𝗥𝗜𝗔𝗟 𝗙𝗜𝗡𝗜𝗦𝗛𝗘𝗗 〕━━━╮\n\n"
                                "You have used all your free trial credits.\n\n"
                                "💎 Use /buypremium to continue with Premium.\n"
                                "╰━━━━━━━━━━━━━━━━━━━━╯"
                            )
                            cancel_requested.add(user_id)
                            break
                elif success is False:
                    await increment("download_failed")
                    failed += 1
                    failed_links.append((i, link))
                else:
                    skipped += 1
                    skipped_links.append((i, link))
                completed_index = i
                # The current file has completely finished (or was skipped/failed).
                # Move the progress message to the bottom before starting the next
                # file. This keeps the batch strictly sequential while avoiding
                # hundreds of simultaneous downloads/uploads.
                if i < cl and users_loop.get(user_id, False) and user_id not in cancel_requested:
                    progress_msg = await _move_batch_progress_to_bottom(
                        message.chat.id,
                        progress_msg,
                        f"📦 **Batch Progress**\n\n🔄 Processing file `{i - cs + 2}/{total}`..."
                    )
                if BATCH_DELAY:
                    await asyncio.sleep(BATCH_DELAY)
            except FloodWait as fw:
                wait_s = int(getattr(fw, "value", 0) or 0)
                resumed = await _pause_batch_for_floodwait(
                    user_id, message.chat.id, progress_msg, wait_s, link, i, cl
                )
                if not resumed:
                    failed += 1
                    await increment("download_failed")
                    failed_links.append((i, link))
                    cancel_requested.add(user_id)
                    break
                # A FloodWait outside get_msg means this file may have reached
                # a later stage. Report it rather than risk duplicate delivery.
                failed += 1
                await increment("download_failed")
                failed_links.append((i, link))
                completed_index = i
                if i < cl and users_loop.get(user_id, False) and user_id not in cancel_requested:
                    progress_msg = await _move_batch_progress_to_bottom(
                        message.chat.id, progress_msg,
                        f"📦 **Batch Progress**\n\n🔄 Processing file `{i - cs + 2}/{total}`..."
                    )
                continue
            except asyncio.TimeoutError:
                skipped += 1
                skipped_links.append((i, link))
                completed_index = i
                try:
                    await progress_msg.edit_text(
                        f"⏭️ **Message `{i}` timed out** after `{FILE_TASK_TIMEOUT}` seconds.\n\nSkipping it and continuing with the next file."
                    )
                except Exception:
                    pass
                if i < cl and users_loop.get(user_id, False) and user_id not in cancel_requested:
                    progress_msg = await _move_batch_progress_to_bottom(
                        message.chat.id, progress_msg,
                        f"📦 **Batch Progress**\n\n🔄 Processing file `{i - cs + 2}/{total}`..."
                    )
                continue
            except asyncio.CancelledError:
                cancel_requested.add(user_id)
                raise
            except Exception as e:
                failed += 1
                await increment("download_failed")
                failed_links.append((i, link))
                completed_index = i
                try:
                    await progress_msg.edit_text(
                        f"⚠️ **Message `{i}` failed.**\n\nSkipping it and continuing with the next file.\n\n"
                        "The technical error has been recorded in the server logs."
                    )
                except Exception:
                    pass
                if i < cl and users_loop.get(user_id, False) and user_id not in cancel_requested:
                    progress_msg = await _move_batch_progress_to_bottom(
                        message.chat.id, progress_msg,
                        f"📦 **Batch Progress**\n\n🔄 Processing file `{i - cs + 2}/{total}`..."
                    )
                continue

        stopped = not users_loop.get(user_id, False) or user_id in cancel_requested
        elapsed = max(0, int(time.monotonic() - started_at))
        mins, secs = divmod(elapsed, 60)
        hours, mins = divmod(mins, 60)
        duration_text = f"{hours}h {mins}m {secs}s" if hours else f"{mins}m {secs}s"
        if stopped:
            await increment("batch_cancelled")
        else:
            await increment("batch_completed")
        await log_event(
            f"📦 BATCH FINISHED\n👤 {user_label(message.from_user)}\n"
            f"🆔 `{user_id}`\n✅ Success: `{processed}`\n❌ Failed: `{failed}`\n"
            f"⏭️ Skipped: `{skipped}`\n⏱️ Duration: `{duration_text}`\n🛑 Cancelled: `{stopped}`"
        )
        report_title = "⛔ 𝗕𝗔𝗧𝗖𝗛 𝗖𝗔𝗡𝗖𝗘𝗟𝗟𝗘𝗗" if stopped else "✅ 𝗕𝗔𝗧𝗖𝗛 𝗖𝗢𝗠𝗣𝗟𝗘𝗧𝗘"
        report = (
            f"╭━━━〔 {report_title} 〕━━━╮\n\n"
            f"📦 Requested: `{total}` files\n"
            f"✅ Successful: `{processed}`\n"
            f"❌ Failed: `{failed}`\n"
            f"⏭️ Skipped: `{skipped}`\n"
            f"🧮 Attempted: `{attempted}`\n"
            f"⏱️ Time taken: `{duration_text}`\n\n"
            f"📊 Remaining: `{max(0, total - processed - failed - skipped)}`\n"
            f"╰━━━━━━━━━━━━━━━━━━━━╯"
        )
        try:
            await progress_msg.edit_text(report)
        except Exception:
            await app.send_message(message.chat.id, report)
        await _send_batch_issue_links(message.chat.id, failed_links, skipped_links)
    except asyncio.CancelledError:
        cancel_requested.add(user_id)
        elapsed = max(0, int(time.monotonic() - batch_started_at))
        mins, secs = divmod(elapsed, 60)
        hours, mins = divmod(mins, 60)
        duration_text = f"{hours}h {mins}m {secs}s" if hours else f"{mins}m {secs}s"
        try:
            await app.send_message(
                message.chat.id,
                "╭━━━〔 ⛔ 𝗕𝗔𝗧𝗖𝗛 𝗖𝗔𝗡𝗖𝗘𝗟𝗟𝗘𝗗 〕━━━╮\n\n"
                "The current download/upload operation was cancelled.\n\n"
                f"✅ Successful: `{processed}`\n❌ Failed: `{failed}`\n⏭️ Skipped: `{skipped}`\n"
                f"🧮 Attempted: `{attempted}`\n⏱️ Time taken: `{duration_text}`\n"
                "╰━━━━━━━━━━━━━━━━━━━━╯"
            )
        except Exception:
            pass
        # Include the in-flight item and every item that had not started yet.
        add_remaining_batch_links()
        await _send_batch_issue_links(message.chat.id, failed_links, skipped_links)
        raise
    except FloodWait as fw:
        wait_s = int(getattr(fw, "value", 0) or 0)
        await app.send_message(
            message.chat.id,
            f"⏸️ **Telegram temporarily rate-limited the bot.**\n\n"
            f"The batch was stopped safely instead of waiting for `{wait_s}` seconds.\n\n"
            "Please try again later."
        )
        print(f"Batch FloodWait for user {user_id}: {wait_s}s")
        add_remaining_batch_links()
        await _send_batch_issue_links(message.chat.id, failed_links, skipped_links)
    except Exception as e:
        print(f"Batch error for user {user_id}: {e}")
        await app.send_message(
            message.chat.id,
            "❌ **Batch could not continue.**\n\n"
            "The technical error was recorded in the server logs. Please try again."
        )
        add_remaining_batch_links()
        await _send_batch_issue_links(message.chat.id, failed_links, skipped_links)
    finally:
        users_loop.pop(user_id, None)
        active_tasks.pop(user_id, None)
        cancel_requested.discard(user_id)
        if userbot is not None:
            try:
                await userbot.stop()
            except Exception:
                pass


@app.on_message(filters.command("reset"))
async def reset_current_state(_, message):
    """Emergency reset for a user's stuck/pending task without deleting saved settings."""
    user_id = message.chat.id

    # Clear interactive states first so a stray reply cannot be consumed by an old flow.
    try:
        from safe_repo.core.get_func import sessions, pending_photos
        sessions.pop(user_id, None)
        pending_photos.pop(user_id, None)
    except Exception:
        pass
    cancel_requested.add(user_id)
    users_loop.pop(user_id, None)
    task = active_tasks.get(user_id)

    if task and task is not asyncio.current_task() and not task.done():
        task.cancel()
        await app.send_message(
            user_id,
            "╭━━━〔 🔄 𝗥𝗘𝗦𝗘𝗧 𝗖𝗢𝗠𝗣𝗟𝗘𝗧𝗘 〕━━━╮\n\n"
            "🛑 The current download/upload/batch task has been force-stopped.\n"
            "🧹 Any pending input state was cleared.\n\n"
            "✅ Your saved login, Premium and customisation settings are unchanged.\n\n"
            "You can start a fresh /batch or send a new link now.\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯"
        )
        return

    active_tasks.pop(user_id, None)
    cancel_requested.discard(user_id)
    await app.send_message(
        user_id,
        "╭━━━〔 🔄 𝗥𝗘𝗦𝗘𝗧 𝗖𝗢𝗠𝗣𝗟𝗘𝗧𝗘 〕━━━╮\n\n"
        "🧹 No running task was found, but all pending input state was cleared.\n\n"
        "✅ Your saved login, Premium and customisation settings are unchanged.\n\n"
        "You can start a fresh operation now.\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯"
    )


@app.on_message(filters.command("cancel"))
async def stop_batch(_, message):
    user_id = message.chat.id
    cleared = []

    # Clear pending Telegram-link/batch interaction state.
    try:
        from safe_repo.core.get_func import sessions as sessions_obj
        sessions_obj.pop(user_id, None)
    except Exception:
        pass
    cancel_requested.add(user_id)
    users_loop.pop(user_id, None)

    # Stop the normal Telegram link/direct-media task, if one is active.
    normal_task = active_tasks.get(user_id)
    if normal_task and normal_task is not asyncio.current_task() and not normal_task.done():
        normal_task.cancel()
        cleared.append("link/batch")

    # Stop social-media downloader work and clear its active selection.
    try:
        from safe_repo.modules.media_downloader import MEDIA_TASKS, ACTIVE_MEDIA_SESSIONS, SESSIONS as MEDIA_SESSIONS
        media_session = ACTIVE_MEDIA_SESSIONS.get(user_id)
        if media_session is not None:
            media_session["cancel_requested"] = True
        removed_media_selection = False
        for key, media_session_item in list(MEDIA_SESSIONS.items()):
            if media_session_item.get("user_id") == user_id:
                media_session_item["cancel_requested"] = True
                MEDIA_SESSIONS.pop(key, None)
                removed_media_selection = True
        if removed_media_selection and not media_session:
            cleared.append("Media selection")
        media_task = MEDIA_TASKS.get(user_id)
        if media_task and media_task is not asyncio.current_task() and not media_task.done():
            media_task.cancel()
            cleared.append("Media Downloader")
    except Exception as exc:
        print(f"/cancel media cleanup error: {exc}")

    # Stop DLBots transfer and clear its selected-file queue without closing login.
    try:
        from safe_repo.modules.dl_bots import DLBOT_TASKS, DLBOT_SESSIONS
        dl_task = DLBOT_TASKS.get(user_id)
        if dl_task and dl_task is not asyncio.current_task() and not dl_task.done():
            dl_task.cancel()
            cleared.append("DLBots")
        dl_state = DLBOT_SESSIONS.get(user_id)
        if dl_state:
            if dl_state.get("selected_files") and not dl_task:
                cleared.append("DLBots selection")
            dl_state["selected_files"] = []
            dl_state["busy"] = False
    except Exception as exc:
        print(f"/cancel DLBots cleanup error: {exc}")

    # Cancel the currently-running Auto Update pair transfer for this owner only.
    try:
        from safe_repo.modules.auto_update import AUTO_UPDATE_TASKS
        au_task = AUTO_UPDATE_TASKS.get(user_id)
        if au_task and au_task is not asyncio.current_task() and not au_task.done():
            au_task.cancel()
            cleared.append("Auto Update transfer")
    except Exception as exc:
        print(f"/cancel Auto Update cleanup error: {exc}")

    active_tasks.pop(user_id, None)
    cancel_requested.discard(user_id)
    if cleared:
        await app.send_message(
            message.chat.id,
            "╭━━━〔 ⛔ 𝗖𝗔𝗡𝗖𝗘𝗟 𝗖𝗢𝗠𝗣𝗟𝗘𝗧𝗘 〕━━━╮\n\n"
            "🛑 Stopped: " + ", ".join(cleared) + ".\n"
            "🧹 Pending selections/queues cleared.\n\n"
            "✅ Saved login, Premium and customisations remain unchanged.\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯"
        )
    else:
        await app.send_message(
            message.chat.id,
            "╭━━━〔 ℹ️ 𝗡𝗢 𝗔𝗖𝗧𝗜𝗩𝗘 𝗧𝗔𝗦𝗞 〕━━━╮\n\n"
            "There is no active download, upload, batch, Media Downloader, DLBots or Auto Update transfer to cancel.\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯"
        )
