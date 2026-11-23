"""Reliable Auto Update worker for JARVIS-SRB.

Source messages are read with each owner's existing saved Pyrogram user session;
files are published with the already-running JARVIS bot client.  Each pair has
its own durable cursor, retry schedule and FloodWait pause state.
"""

import asyncio
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone

from pyrogram import Client
from pyrogram.enums import ChatMemberStatus, ParseMode
from pyrogram.errors import FloodWait
from pyrogram.types import (
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)
from telethon import events
from telethon.tl.custom import Button
from motor.motor_asyncio import AsyncIOMotorClient

from config import API_ID, API_HASH, MONGO_DB, MONGO_DATABASE_NAME, TRANSFER_RETRIES
from safe_repo import app, sex as gf
from safe_repo.core import get_func as srb_pipeline
from safe_repo.core.download_utils import download_media_resilient
from safe_repo.core.mongo import db as users_db


_mongo = AsyncIOMotorClient(MONGO_DB)
_database = _mongo[MONGO_DATABASE_NAME]
pairs_col = _database["auto_update_pairs"]
logs_col = _database["auto_update_logs"]
transfers_col = _database["auto_update_transfers"]

DEFAULT_FILTERS = {
    "videos": True,
    "documents": True,
    "photos": True,
    "audio": True,
    "apk": True,
}
POLL_INTERVAL = max(5, int(os.getenv("AUTO_UPDATE_POLL_INTERVAL", "10")))
INTER_FILE_DELAY = 0.5
MAX_MESSAGES_PER_PAIR_PER_CYCLE = 25
RETRY_DELAYS = (5, 15, 30, 60, 120, 300, 600)
PROGRESS_EDIT_INTERVAL = 1.25
PROGRESS_STEP = 4

PROMPTS = {}
PAIR_LOCKS = {}
USER_CLIENTS = {}
USER_CLIENT_LOCKS = {}
_worker_task = None
_started = False
_indexes_ready = False


def _now():
    return datetime.now(timezone.utc)


def _as_utc(value):
    if not value:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _human_bytes(value):
    value = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _pair_status(pair):
    if not pair.get("enabled", True):
        return "⏸ Paused"
    until = _as_utc(pair.get("auto_paused_until"))
    if until and until > _now():
        return f"⏳ Auto-paused until {until.strftime('%H:%M:%S UTC')}"
    return "🟢 Running"


class TransferRetry(Exception):
    """A message must remain at the cursor and be retried later."""


def _pair_keyboard(pair):
    pid = pair["pair_id"]
    toggle = "⏸ Pause" if pair.get("enabled", True) else "▶️ Resume"
    return [
        [Button.inline(toggle, f"au:toggle:{pid}".encode())],
        [Button.inline("⚙️ Pair Settings", f"au:settings:{pid}".encode()),
         Button.inline("📜 Activity / Logs", f"au:logs:{pid}".encode())],
        [Button.inline("✏️ Edit Pair", f"au:edit:{pid}".encode()),
         Button.inline("🗑 Delete Pair", f"au:delete:{pid}".encode())],
        [Button.inline("⬅️ All Pairs", b"au:list")],
    ]


def _settings_keyboard(pair):
    filters = {**DEFAULT_FILTERS, **(pair.get("filters") or {})}
    rows = []
    for key, label in (
        ("videos", "Videos"), ("documents", "Documents"),
        ("photos", "Photos"), ("audio", "Audio"), ("apk", "APK files"),
    ):
        rows.append([Button.inline(
            f"{'✅' if filters.get(key, True) else '❌'} {label}",
            f"au:filter:{pair['pair_id']}:{key}".encode(),
        )])
    rows.extend([
        [Button.inline("🚫 Ignore Rules", f"au:ignore:{pair['pair_id']}".encode())],
        [Button.inline("⬅️ Pair Menu", f"au:pair:{pair['pair_id']}".encode())],
    ])
    return rows


async def _log(pair, kind, message_id=None, detail=""):
    now = _now()
    await logs_col.insert_one({
        "pair_id": pair["pair_id"],
        "owner_id": int(pair["owner_id"]),
        "kind": kind,
        "source_message_id": int(message_id) if message_id is not None else None,
        "detail": str(detail)[:1500],
        "created_at": now,
    })
    update = {"last_activity_at": now}
    if kind == "success":
        update.update({"last_success_at": now, "last_error": None})
    elif kind in ("failed", "download_failed", "upload_failed", "floodwait"):
        update["last_error"] = str(detail)[:1000]
    await pairs_col.update_one({"pair_id": pair["pair_id"]}, {"$set": update})


async def _ensure_indexes():
    global _indexes_ready
    if _indexes_ready:
        return
    await pairs_col.create_index("pair_id", unique=True)
    await pairs_col.create_index([("owner_id", 1), ("created_at", -1)])
    await logs_col.create_index([("pair_id", 1), ("created_at", -1)])
    await transfers_col.create_index(
        [("pair_id", 1), ("source_message_id", 1)], unique=True
    )
    _indexes_ready = True


async def _get_user_client(owner_id):
    """Return one reusable Pyrogram client for this owner's saved session."""
    owner_id = int(owner_id)
    lock = USER_CLIENT_LOCKS.setdefault(owner_id, asyncio.Lock())
    async with lock:
        cached = USER_CLIENTS.get(owner_id)
        if cached and getattr(cached, "is_connected", False):
            return cached
        if cached:
            USER_CLIENTS.pop(owner_id, None)
            try:
                await cached.stop()
            except Exception:
                pass

        data = await users_db.get_data(owner_id)
        if not data or not data.get("session"):
            raise RuntimeError("Telegram login is missing. Use /login first.")

        client = Client(
            f":jarvis_auto_update_{owner_id}:",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=data["session"],
            no_updates=True,
            sleep_threshold=60,
        )
        try:
            await client.start()
        except Exception:
            try:
                await client.stop()
            except Exception:
                pass
            raise
        USER_CLIENTS[owner_id] = client
        return client


def _chat_name(chat):
    return (
        getattr(chat, "title", None)
        or " ".join(x for x in (getattr(chat, "first_name", None), getattr(chat, "last_name", None)) if x)
        or getattr(chat, "username", None)
        or str(chat.id)
    )


async def _resolve_pair_channels(owner_id, source_id, target_id):
    user = await _get_user_client(owner_id)
    source = await user.get_chat(source_id)
    target = await app.get_chat(target_id)
    bot_me = await app.get_me()
    member = await app.get_chat_member(target.id, bot_me.id)
    if getattr(member, "status", None) not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        raise ValueError("JARVIS bot must be an administrator in the target channel.")
    privileges = getattr(member, "privileges", None)
    if privileges is not None and not getattr(privileges, "can_post_messages", False):
        raise ValueError("JARVIS bot needs permission to post messages in the target channel.")
    return source, target


async def _latest_message_id(client, chat_id):
    async for message in client.get_chat_history(chat_id, limit=1):
        return int(message.id)
    return 0


async def _pair_view(event, pair):
    text = (
        "🔄 **AUTO UPDATE PAIR**\n\n"
        f"📥 Source: **{pair.get('source_name', pair['source_id'])}**\n"
        f"📤 Target: **{pair.get('target_name', pair['target_id'])}**\n"
        f"📌 Status: **{_pair_status(pair)}**\n"
        f"📦 Uploaded: `{pair.get('success_count', 0)}` · "
        f"Skipped: `{pair.get('skipped_count', 0)}` · "
        f"Failed attempts: `{pair.get('failed_count', 0)}`\n"
        f"Retries: `{pair.get('retry_count', 0)}`\n"
        f"🕒 Last successful update: `{pair.get('last_success_at') or 'Never'}`\n"
        f"⚠️ Last error: `{pair.get('last_error') or 'None'}`"
    )
    await event.edit(text, buttons=_pair_keyboard(pair))


async def _list_pairs(event, owner_id):
    pairs = await pairs_col.find({"owner_id": int(owner_id)}).sort("created_at", -1).to_list(length=100)
    rows, summaries = [], []
    for pair in pairs:
        label = f"{pair.get('source_name', pair['source_id'])} → {pair.get('target_name', pair['target_id'])}"
        summaries.append(
            f"**{label}**\nStatus: `{_pair_status(pair)}` · Uploaded: `{pair.get('success_count', 0)}`\n"
            f"Last success: `{pair.get('last_success_at') or 'Never'}`\n"
            f"Last error: `{str(pair.get('last_error') or 'None')[:100]}`"
        )
        rows.append([Button.inline(
            ("🟢 " if pair.get("enabled", True) else "⏸ ") + label[:55],
            f"au:pair:{pair['pair_id']}".encode(),
        )])
    rows.append([Button.inline("➕ Add New Pair", b"au:add")])
    await event.edit(
        "🔄 **AUTO UPDATE MANAGEMENT**\n\n"
        + ("\n\n".join(summaries) if summaries else "No pairs saved yet.")
        + "\n\nSelect a pair or add a new source → target pair.",
        buttons=rows,
    )


async def _prompt_add(event, owner_id):
    PROMPTS[int(owner_id)] = {"action": "add"}
    await event.respond(
        "➕ **Add New Pair**\n\nSend both channel IDs in one message:\n"
        "`SOURCE_CHANNEL_ID TARGET_CHANNEL_ID`\n\n"
        "Your logged-in Telegram account must access the source. "
        "JARVIS bot must be admin in the target."
    )


async def _prompt_edit(event, owner_id, pair):
    PROMPTS[int(owner_id)] = {"action": "edit", "pair_id": pair["pair_id"]}
    await event.respond("✏️ Send the new source and target channel IDs:\n`SOURCE_CHANNEL_ID TARGET_CHANNEL_ID`")


async def _prompt_ignore(event, owner_id, pair, rule_type):
    PROMPTS[int(owner_id)] = {"action": "ignore", "pair_id": pair["pair_id"], "rule_type": rule_type}
    await event.respond(f"Send the {rule_type.replace('_', ' ')} to ignore. Matching is case-insensitive.")


async def _process_prompt(event):
    owner_id = int(event.sender_id)
    prompt = PROMPTS.get(owner_id)
    if not prompt or not event.raw_text or event.raw_text.startswith("/"):
        return
    raw = event.raw_text.strip()

    if prompt["action"] in ("add", "edit"):
        values = raw.split()
        if len(values) != 2:
            await event.respond("❌ Send exactly two channel IDs separated by a space.")
            return
        try:
            source_id, target_id = int(values[0]), int(values[1])
            source, target = await _resolve_pair_channels(owner_id, source_id, target_id)
            user = await _get_user_client(owner_id)
            latest_id = await _latest_message_id(user, source.id)
            if prompt["action"] == "add":
                pair = {
                    "pair_id": uuid.uuid4().hex[:12], "owner_id": owner_id,
                    "source_id": int(source.id), "target_id": int(target.id),
                    "source_name": _chat_name(source), "target_name": _chat_name(target),
                    "pair_name": f"{_chat_name(source)} → {_chat_name(target)}",
                    "enabled": True, "filters": dict(DEFAULT_FILTERS), "ignore_rules": [],
                    "last_processed_message_id": latest_id,
                    "created_at": _now(), "last_activity_at": _now(),
                    "last_success_at": None, "last_error": None,
                    "success_count": 0, "skipped_count": 0, "retry_count": 0,
                }
                await pairs_col.insert_one(pair)
            else:
                pair = await pairs_col.find_one({"pair_id": prompt["pair_id"], "owner_id": owner_id})
                if not pair:
                    raise ValueError("Pair not found.")
                await pairs_col.update_one({"pair_id": pair["pair_id"]}, {"$set": {
                    "source_id": int(source.id), "target_id": int(target.id),
                    "source_name": _chat_name(source), "target_name": _chat_name(target),
                    "pair_name": f"{_chat_name(source)} → {_chat_name(target)}",
                    "last_processed_message_id": latest_id,
                    "auto_paused_until": None, "next_retry_at": None,
                }})
                await transfers_col.delete_many({"pair_id": pair["pair_id"]})
            PROMPTS.pop(owner_id, None)
            await event.respond(f"✅ Pair saved:\n**{_chat_name(source)} → {_chat_name(target)}**")
        except Exception as exc:
            await event.respond(f"❌ Could not save pair: `{str(exc)[:700]}`")
        return

    if prompt["action"] == "ignore":
        pair = await pairs_col.find_one({"pair_id": prompt["pair_id"], "owner_id": owner_id})
        if not pair:
            PROMPTS.pop(owner_id, None)
            return
        rule = {"type": prompt["rule_type"], "value": raw}
        await pairs_col.update_one({"pair_id": pair["pair_id"]}, {"$addToSet": {"ignore_rules": rule}})
        PROMPTS.pop(owner_id, None)
        await event.respond(f"✅ Ignore rule added: **{rule['type']}** = `{raw}`")


@gf.on(events.NewMessage(pattern=r"^/autoupdate(?:@\w+)?$"))
async def auto_update_command(event):
    if not event.is_private:
        return
    await _list_pairs(event, int(event.sender_id))


@gf.on(events.NewMessage(incoming=True))
async def _prompt_listener(event):
    await _process_prompt(event)


@gf.on(events.CallbackQuery(data=re.compile(rb"^au:")))
async def _callback(event):
    owner_id = int(event.sender_id)
    parts = event.data.decode().split(":")
    action = parts[1] if len(parts) > 1 else ""
    await event.answer()

    if action == "list":
        await _list_pairs(event, owner_id)
        return
    if action == "add":
        await _prompt_add(event, owner_id)
        return

    pair_id = parts[2] if len(parts) > 2 else ""
    pair = await pairs_col.find_one({"pair_id": pair_id, "owner_id": owner_id}) if pair_id else None

    if action in ("skip", "retry") and pair:
        try:
            message_id = int(parts[3])
        except (IndexError, ValueError):
            await event.answer("Invalid file reference.", alert=True)
            return
        transfer = await transfers_col.find_one({
            "pair_id": pair_id, "source_message_id": message_id,
            "owner_id": owner_id, "decision_required": True,
        })
        if not transfer:
            await event.edit("This file has already been handled or is no longer waiting for a decision.")
            return
        if action == "skip":
            await _record_transfer(pair, message_id, "skipped", "Skipped by owner after retry limit", decision_required=False)
            if transfer.get("media_group_id"):
                # Rewind to the beginning of this album so unskipped siblings
                # are still picked up on the next poll, even if the failed item
                # was the album's final message.
                try:
                    user = await _get_user_client(owner_id)
                    album = await user.get_media_group(pair["source_id"], message_id)
                    first_album_id = min(int(item.id) for item in album)
                    await pairs_col.update_one({"pair_id": pair_id}, {"$set": {
                        "last_processed_message_id": max(0, first_album_id - 1),
                        "next_retry_at": None,
                    }})
                except Exception:
                    await _mark_cursor(pair, message_id, "skipped")
            else:
                await _mark_cursor(pair, message_id, "skipped")
            await _log(pair, "skipped", message_id, "Owner chose Skip File after 2 automatic retries")
            await pairs_col.update_one({"pair_id": pair_id}, {"$set": {"next_retry_at": None, "last_error": None}})
            await event.edit(f"⏭ **File skipped**\nMessage `{message_id}` skipped. Auto Update will continue with the next file.")
        else:
            await _record_transfer(pair, message_id, "pending", "Owner requested another retry", auto_retry_count=0,
                                   decision_required=False, retry_at=None)
            await pairs_col.update_one({"pair_id": pair_id}, {"$set": {"next_retry_at": None, "last_error": None}})
            await _log(pair, "info", message_id, "Owner chose Retry Again")
            await event.edit(f"🔄 **Retry queued**\nMessage `{message_id}` will be tried again.")
        return

    if action == "pair" and pair:
        await _pair_view(event, pair)
    elif action == "toggle" and pair:
        enabled = not pair.get("enabled", True)
        await pairs_col.update_one({"pair_id": pair_id}, {"$set": {
            "enabled": enabled,
            "auto_paused_until": None if enabled else pair.get("auto_paused_until"),
        }})
        await _pair_view(event, await pairs_col.find_one({"pair_id": pair_id}))
    elif action == "settings" and pair:
        await event.edit("⚙️ **PAIR SETTINGS**\n\nToggle each media type independently.", buttons=_settings_keyboard(pair))
    elif action == "filter" and pair:
        key = parts[3] if len(parts) > 3 else ""
        if key in DEFAULT_FILTERS:
            filters = {**DEFAULT_FILTERS, **(pair.get("filters") or {})}
            filters[key] = not filters.get(key, True)
            await pairs_col.update_one({"pair_id": pair_id}, {"$set": {"filters": filters}})
            pair["filters"] = filters
            await event.edit("⚙️ **PAIR SETTINGS**\n\nToggle each media type independently.", buttons=_settings_keyboard(pair))
    elif action == "ignore" and pair:
        rules = pair.get("ignore_rules", [])
        rows = [[Button.inline(f"🗑 {r['type']}: {r['value']}"[:55], f"au:delrule:{pair_id}:{i}".encode())] for i, r in enumerate(rules)]
        rows.extend([
            [Button.inline("➕ Exact filename", f"au:addrule:{pair_id}:exact_filename".encode())],
            [Button.inline("➕ Filename keyword", f"au:addrule:{pair_id}:filename_keyword".encode())],
            [Button.inline("➕ Caption keyword", f"au:addrule:{pair_id}:caption_keyword".encode())],
            [Button.inline("⬅️ Settings", f"au:settings:{pair_id}".encode())],
        ])
        await event.edit("🚫 **IGNORE RULES**\n\nTap a rule to delete it.", buttons=rows)
    elif action == "addrule" and pair:
        await _prompt_ignore(event, owner_id, pair, parts[3])
    elif action == "delrule" and pair:
        try:
            index = int(parts[3])
        except (IndexError, ValueError):
            index = -1
        rules = pair.get("ignore_rules", [])
        if 0 <= index < len(rules):
            rules.pop(index)
            await pairs_col.update_one({"pair_id": pair_id}, {"$set": {"ignore_rules": rules}})
        pair["ignore_rules"] = rules
        rows = [[Button.inline(f"🗑 {r['type']}: {r['value']}"[:55], f"au:delrule:{pair_id}:{i}".encode())] for i, r in enumerate(rules)]
        rows.extend([
            [Button.inline("➕ Exact filename", f"au:addrule:{pair_id}:exact_filename".encode())],
            [Button.inline("➕ Filename keyword", f"au:addrule:{pair_id}:filename_keyword".encode())],
            [Button.inline("➕ Caption keyword", f"au:addrule:{pair_id}:caption_keyword".encode())],
            [Button.inline("⬅️ Settings", f"au:settings:{pair_id}".encode())],
        ])
        await event.edit("🚫 **IGNORE RULES**", buttons=rows)
    elif action == "edit" and pair:
        await _prompt_edit(event, owner_id, pair)
    elif action == "delete" and pair:
        await pairs_col.delete_one({"pair_id": pair_id, "owner_id": owner_id})
        await logs_col.delete_many({"pair_id": pair_id, "owner_id": owner_id})
        await transfers_col.delete_many({"pair_id": pair_id})
        await event.edit("🗑 Pair deleted.", buttons=[[Button.inline("⬅️ All Pairs", b"au:list")]])
    elif action == "logs" and pair:
        logs = await logs_col.find({"pair_id": pair_id, "owner_id": owner_id}).sort("created_at", -1).to_list(length=15)
        lines = [f"• `{x.get('created_at')}` **{x['kind']}** — {x.get('detail', '')}" for x in logs]
        rows = [
            [Button.inline("🔄 Refresh", f"au:logs:{pair_id}".encode())],
            [Button.inline("⬅️ Pair Menu", f"au:pair:{pair_id}".encode())],
        ]
        await event.edit("📜 **RECENT ACTIVITY**\n\n" + ("\n".join(lines) if lines else "No activity yet."), buttons=rows)


def _filename(message):
    document = getattr(message, "document", None)
    if document:
        return getattr(document, "file_name", None) or f"document_{message.id}.bin"
    video = getattr(message, "video", None)
    if video:
        return getattr(video, "file_name", None) or f"video_{message.id}.mp4"
    audio = getattr(message, "audio", None)
    if audio:
        return getattr(audio, "file_name", None) or f"audio_{message.id}.m4a"
    animation = getattr(message, "animation", None)
    if animation:
        return getattr(animation, "file_name", None) or f"animation_{message.id}.mp4"
    if getattr(message, "photo", None):
        return f"photo_{message.id}.jpg"
    if getattr(message, "video_note", None):
        return f"video_note_{message.id}.mp4"
    if getattr(message, "voice", None):
        return f"voice_{message.id}.ogg"
    return f"message_{message.id}.txt"


async def _customized_payload(owner_id, message):
    """Apply the owner's normal Save Restricted customisations to Auto Update.

    Forward Chat ID is intentionally not read here: the Auto Update pair's
    target channel is the sole destination for this transfer.
    """
    try:
        from safe_repo.core.get_func import (
            get_user_rename_preference, get_user_caption_preference,
            load_delete_words, load_replacement_words,
        )
        from safe_repo.core.func import get_active_plan
        from safe_repo.core.get_func import with_media_footer
    except Exception as exc:
        raise RuntimeError(f"Could not load saved customisation settings: {exc}") from exc

    filename = _filename(message)
    caption = getattr(message, "caption", None) or getattr(message, "text", None) or ""
    delete_words = load_delete_words(int(owner_id))
    replacements = load_replacement_words(int(owner_id))

    # Match the regular Save Restricted pipeline's filename rules.
    stem, extension = os.path.splitext(os.path.basename(filename))
    for word in delete_words:
        stem = stem.replace(str(word), "")
    rename_tag = get_user_rename_preference(int(owner_id))
    if rename_tag:
        stem = f"{stem} {rename_tag}".strip()
    stem = re.sub(r'[\\/:*?"<>|\x00]+', "_", stem).strip(" ._")
    filename = (stem or f"message_{message.id}") + extension

    # Match normal caption order: remove words, then apply replacements,
    # then append the user's custom caption and plan-dependent media footer.
    for word in delete_words:
        caption = caption.replace(str(word), "")
    for old, new in replacements.items():
        caption = caption.replace(str(old), str(new))
    custom_caption = get_user_caption_preference(int(owner_id))
    if custom_caption:
        caption = f"{caption.rstrip()}\n\n{custom_caption}" if caption.strip() else str(custom_caption)
    plan = await get_active_plan(int(owner_id))
    caption = with_media_footer(caption, include_footer=(plan != "elite"))
    return filename, caption


def _is_video_document(message):
    document = getattr(message, "document", None)
    if not document:
        return False
    mime = (getattr(document, "mime_type", None) or "").casefold()
    filename = (getattr(document, "file_name", None) or "").casefold()
    return mime.startswith("video/") or filename.endswith((".mp4", ".m4v", ".mov", ".webm", ".mkv"))


def _media_kind(message):
    if getattr(message, "photo", None):
        return "photos"
    if getattr(message, "video", None) or getattr(message, "video_note", None) or getattr(message, "animation", None):
        return "videos"
    if getattr(message, "audio", None) or getattr(message, "voice", None):
        return "audio"
    if getattr(message, "document", None):
        doc = message.document
        filename = (getattr(doc, "file_name", None) or "").casefold()
        mime = (getattr(doc, "mime_type", None) or "").casefold()
        if filename.endswith(".apk") or "android.package-archive" in mime:
            return "apk"
        if mime.startswith("video/"):
            return "videos"
        if mime.startswith("audio/"):
            return "audio"
        return "documents"
    if getattr(message, "text", None) or getattr(message, "caption", None):
        return "text"
    return "other"


def _ignored(pair, message):
    filename = _filename(message).casefold()
    caption = (getattr(message, "caption", None) or getattr(message, "text", None) or "").casefold()
    for rule in pair.get("ignore_rules", []):
        value = str(rule.get("value", "")).casefold()
        rule_type = rule.get("type")
        if rule_type == "exact_filename" and filename == value:
            return f"exact filename: {rule.get('value')}"
        if rule_type == "filename_keyword" and value and value in filename:
            return f"filename keyword: {rule.get('value')}"
        if rule_type == "caption_keyword" and value and value in caption:
            return f"caption keyword: {rule.get('value')}"
    return None


def _filter_reason(pair, message):
    kind = _media_kind(message)
    if kind in DEFAULT_FILTERS:
        filters = {**DEFAULT_FILTERS, **(pair.get("filters") or {})}
        if not filters.get(kind, True):
            return f"{kind} filter is disabled"
    return _ignored(pair, message)


async def _create_progress(owner_id, pair, position, total, message):
    filename = _filename(message)
    text = (
        f"🔄 JARVIS Auto Update\n"
        f"📥 {pair.get('source_name', pair['source_id'])} → {pair.get('target_name', pair['target_id'])}\n\n"
        f"📦 Queue: {position}/{max(total, 1)}\n"
        f"📄 File: {filename[:90]}\n"
        "⏳ Preparing transfer..."
    )
    try:
        sent = await app.send_message(int(owner_id), text, parse_mode=ParseMode.DISABLED)
        return {"message": sent, "position": position, "total": total, "filename": filename,
                "last_edit": 0.0, "last_percent": -1, "stage": "Preparing"}
    except Exception:
        return None


async def _reuse_progress(ctx, position, total, message):
    if not ctx:
        return
    ctx.update({"position": position, "total": total, "filename": _filename(message),
                "last_edit": 0.0, "last_percent": -1, "stage": "Preparing"})
    await _set_progress(ctx, "⏳ Preparing transfer...", force=True)


async def _set_progress(ctx, stage, current=None, total=None, force=False):
    if not ctx:
        return
    now = time.monotonic()
    percent = None
    if total and total > 0:
        percent = min(100, int(current * 100 / total))
    if not force:
        if now - ctx["last_edit"] < PROGRESS_EDIT_INTERVAL and (
            percent is None or percent - ctx["last_percent"] < PROGRESS_STEP
        ):
            return
    if percent is None:
        detail = stage
    else:
        detail = f"{stage}: {percent}% ({_human_bytes(current)} / {_human_bytes(total)})"
    text = (
        f"🔄 JARVIS Auto Update\n"
        f"📦 Queue: {ctx['position']}/{max(ctx['total'], 1)}\n"
        f"📄 File: {ctx['filename'][:90]}\n\n"
        f"{detail}"
    )
    try:
        await ctx["message"].edit_text(text, parse_mode=ParseMode.DISABLED)
        ctx["last_edit"] = now
        if percent is not None:
            ctx["last_percent"] = percent
        ctx["stage"] = stage
    except Exception:
        pass


async def _transfer_progress(current, total, ctx, stage):
    await _set_progress(ctx, stage, current, total)


async def _finish_progress(ctx, result):
    if not ctx:
        return
    await _set_progress(ctx, result, force=True)


async def _record_transfer(pair, message_id, status, detail="", **extra):
    now = _now()
    await transfers_col.update_one(
        {"pair_id": pair["pair_id"], "source_message_id": int(message_id)},
        {"$set": {"owner_id": int(pair["owner_id"]), "status": status,
                   "detail": str(detail)[:1500], "updated_at": now, **extra},
         "$setOnInsert": {"created_at": now}},
        upsert=True,
    )


async def _mark_cursor(pair, message_id, status):
    update = {
        "$max": {"last_processed_message_id": int(message_id)},
        "$set": {"last_activity_at": _now(), "next_retry_at": None},
    }
    if status == "success":
        update["$inc"] = {"success_count": 1}
        update["$set"].update({"last_success_at": _now(), "last_error": None, "retry_count": 0})
    elif status == "skipped":
        update["$inc"] = {"skipped_count": 1}
    await pairs_col.update_one({"pair_id": pair["pair_id"]}, update)


async def _auto_pause(pair, seconds, message_id, detail):
    seconds = max(1, int(seconds))
    until = _now() + timedelta(seconds=seconds + 2)
    await pairs_col.update_one({"pair_id": pair["pair_id"]}, {"$set": {
        "auto_paused_until": until,
        "auto_pause_reason": "Telegram FloodWait",
        "next_retry_at": until,
        "last_error": str(detail)[:1000],
        "last_activity_at": _now(),
    }, "$inc": {"retry_count": 1}})
    if message_id is not None:
        await _record_transfer(pair, message_id, "pending", f"FloodWait; retry after {seconds}s", retry_at=until)
    await _log(pair, "floodwait", message_id, f"Auto-paused this pair for {seconds}s. Same message will retry automatically.")


async def _retry_later(pair, message_id, detail, kind="failed"):
    current = await pairs_col.find_one({"pair_id": pair["pair_id"]}) or pair
    attempt = int(current.get("retry_count", 0)) + 1
    delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
    retry_at = _now() + timedelta(seconds=delay)
    if message_id is not None:
        await _record_transfer(pair, message_id, "pending", detail, retry_at=retry_at, attempts=attempt)
    await pairs_col.update_one({"pair_id": pair["pair_id"]}, {"$set": {
        "next_retry_at": retry_at, "last_error": str(detail)[:1000], "last_activity_at": _now(),
    }, "$inc": {"retry_count": 1, "failed_count": 1}})
    await _log(pair, kind, message_id, f"Retry {attempt} scheduled in {delay}s: {detail}")


async def _handle_file_failure(pair, message, detail, ctx=None, kind="download_failed"):
    """Retry twice automatically, then mark the file skipped and continue."""
    message_id = int(message.id)
    current = await transfers_col.find_one({
        "pair_id": pair["pair_id"], "source_message_id": message_id
    }) or {}
    failures = int(current.get("auto_retry_count", 0)) + 1
    now = _now()

    if failures <= 2:
        delay = RETRY_DELAYS[min(failures - 1, 1)]
        retry_at = now + timedelta(seconds=delay)
        await _record_transfer(
            pair, message_id, "pending", str(detail), auto_retry_count=failures,
            retry_at=retry_at, decision_required=False,
            media_group_id=getattr(message, "media_group_id", None),
        )
        await pairs_col.update_one({"pair_id": pair["pair_id"]}, {"$set": {
            "next_retry_at": retry_at, "last_error": str(detail)[:1000],
            "last_activity_at": now,
        }, "$inc": {"failed_count": 1}})
        await _log(pair, kind, message_id,
                   f"Automatic retry {failures}/2 scheduled in {delay}s: {detail}")
        await _finish_progress(ctx, f"⚠ Failed · automatic retry {failures}/2 in {delay}s")
        return

    reason = f"Failed after 2 automatic retries: {detail}"
    await _record_transfer(
        pair, message_id, "skipped", reason, auto_retry_count=2,
        decision_required=False, retry_at=None, failure_auto_skipped=True,
        failure_detail=str(detail)[:1200],
        media_group_id=getattr(message, "media_group_id", None),
    )
    await _mark_cursor(pair, message_id, "skipped")
    await pairs_col.update_one({"pair_id": pair["pair_id"]}, {"$set": {
        "next_retry_at": None, "last_error": None, "last_activity_at": now,
    }, "$inc": {"failed_count": 1}})
    await _log(pair, "skipped", message_id, reason)
    await _finish_progress(ctx, "⏭ Failed after 2 retries · skipped; moving to next file")


async def _upload_media(user, pair, message, ctx):
    kind = _media_kind(message)
    reason = _filter_reason(pair, message)
    if reason:
        await _log(pair, "skipped", message.id, reason)
        await _record_transfer(pair, message.id, "skipped", reason)
        await _mark_cursor(pair, message.id, "skipped")
        await _finish_progress(ctx, f"⏭ Skipped: {reason}")
        return "skipped"
    if kind == "other":
        reason = "Unsupported Telegram message type"
        await _log(pair, "skipped", message.id, reason)
        await _record_transfer(pair, message.id, "skipped", reason)
        await _mark_cursor(pair, message.id, "skipped")
        await _finish_progress(ctx, f"⏭ Skipped: {reason}")
        return "skipped"

    existing = await transfers_col.find_one({
        "pair_id": pair["pair_id"], "source_message_id": int(message.id),
        "status": {"$in": ["success", "skipped"]},
    })
    if existing:
        await _mark_cursor(pair, message.id, "already_done" if existing.get("status") == "success" else "skipped")
        await _finish_progress(ctx, "✅ Already transferred; duplicate prevented" if existing.get("status") == "success" else "⏭ Previously skipped")
        return "success" if existing.get("status") == "success" else "skipped"

    await _record_transfer(pair, message.id, "processing", "Transfer started", started_at=_now())
    filename, caption = await _customized_payload(pair["owner_id"], message)
    if kind == "text":
        await _set_progress(ctx, "Uploading text post", force=True)
        await app.send_message(pair["target_id"], caption, disable_web_page_preview=True, parse_mode=ParseMode.DISABLED)
        await _record_transfer(pair, message.id, "success", "Text post uploaded")
        await _mark_cursor(pair, message.id, "success")
        await _log(pair, "success", message.id, "Text post uploaded")
        await _finish_progress(ctx, "✅ Text post uploaded")
        return "success"

    with tempfile.TemporaryDirectory(prefix="jarvis_auto_update_") as tmp:
        path_hint = os.path.join(tmp, filename)
        await _set_progress(ctx, "⬇ Downloading", force=True)
        try:
            # Keep the same retry/watchdog downloader used by normal Save Restricted.
            downloaded_path = await download_media_resilient(
                user, message,
                progress=_transfer_progress,
                progress_args=(ctx, "⬇ Downloading"),
                progress_message=ctx.get("message") if ctx else None,
                user_id=pair.get("owner_id"),
                retries=TRANSFER_RETRIES,
            )
            if not downloaded_path or not os.path.isfile(downloaded_path):
                raise RuntimeError("Telegram returned no downloaded file")
            shutil.move(downloaded_path, path_hint)
            path = path_hint
            if not os.path.isfile(path) or os.path.getsize(path) <= 0:
                raise RuntimeError("Telegram returned an empty downloaded file")
        except FloodWait:
            raise
        except Exception as exc:
            await _handle_file_failure(pair, message, f"Download failed: {exc}", ctx, "download_failed")
            raise TransferRetry(str(exc))

        await _set_progress(ctx, "⬆ Uploading", force=True)
        upload_progress = {"progress": _transfer_progress, "progress_args": (ctx, "⬆ Uploading")}
        try:
            if getattr(message, "video_note", None):
                sent = await app.send_video_note(pair["target_id"], path, **upload_progress)
            elif getattr(message, "voice", None):
                sent = await app.send_voice(pair["target_id"], path, caption=caption,
                    parse_mode=ParseMode.DISABLED, **upload_progress)
            elif getattr(message, "photo", None):
                sent = await srb_pipeline.upload_photo_resilient(
                    [], path, caption, ctx["message"], int(pair["owner_id"]),
                    upload_chat_id=pair["target_id"],
                )
            elif getattr(message, "animation", None):
                sent = await app.send_animation(pair["target_id"], path, caption=caption,
                    parse_mode=ParseMode.DISABLED, **upload_progress)
            elif getattr(message, "video", None) or _is_video_document(message):
                # Video documents must use send_video (not send_document) too,
                # otherwise Telegram displays them as files without a timeline.
                # Match normal Save Restricted processing: ffprobe metadata,
                # source/saved/generated thumbnail, retry/watchdog upload and
                # document fallback. Upload directly to this Auto Update pair's target.
                metadata, source_thumb = await srb_pipeline._prepare_video_upload(
                    user, message, path, int(pair["owner_id"])
                )
                try:
                    sent = await srb_pipeline.upload_video_resilient(
                        [], path, caption, ctx["message"], int(pair["owner_id"]),
                        metadata.get("width") or 0,
                        metadata.get("height") or 0,
                        metadata.get("duration") or 0,
                        source_thumb=source_thumb,
                        upload_chat_id=pair["target_id"],
                    )
                finally:
                    if source_thumb:
                        try:
                            if os.path.isfile(source_thumb):
                                os.remove(source_thumb)
                        except OSError:
                            pass
            elif getattr(message, "audio", None):
                sent = await srb_pipeline.upload_audio_resilient(
                    [], path, caption, ctx["message"], int(pair["owner_id"]),
                    duration=getattr(message.audio, "duration", 0) or 0,
                    performer=getattr(message.audio, "performer", None),
                    title=getattr(message.audio, "title", None),
                    upload_chat_id=pair["target_id"],
                )
            else:
                sent = await srb_pipeline.upload_document_resilient(
                    [], path, caption, ctx["message"], int(pair["owner_id"]),
                    upload_chat_id=pair["target_id"],
                )
            if sent is None:
                raise RuntimeError("Telegram upload was stopped before completion")
        except FloodWait:
            raise
        except Exception as exc:
            await _handle_file_failure(pair, message, f"Upload failed: {exc}", ctx, "upload_failed")
            raise TransferRetry(str(exc))

    await _record_transfer(pair, message.id, "success", f"{kind} uploaded", target_message_id=getattr(sent, "id", None))
    await _mark_cursor(pair, message.id, "success")
    await _log(pair, "success", message.id, f"{kind} uploaded successfully")
    await _finish_progress(ctx, "✅ Upload completed · 100%")
    return "success"


async def _upload_pre_downloaded_album_item(pair, item, path, caption, ctx):
    """Publish an already-downloaded album item without downloading it again."""
    owner_id = int(pair["owner_id"])
    kind = _media_kind(item)
    try:
        if kind == "videos":
            source_video = getattr(item, "video", None)
            fallback = {
                "width": getattr(source_video, "width", 0) or 0,
                "height": getattr(source_video, "height", 0) or 0,
                "duration": getattr(source_video, "duration", 0) or 0,
            }
            if not any(fallback.values()):
                for attribute in (getattr(getattr(item, "document", None), "attributes", None) or []):
                    width = getattr(attribute, "w", None) or 0
                    height = getattr(attribute, "h", None) or 0
                    duration = getattr(attribute, "duration", None) or 0
                    if width or height or duration:
                        fallback = {"width": width, "height": height, "duration": duration}
                        break
            metadata = await srb_pipeline.video_metadata_async(
                str(path), timeout=srb_pipeline.PRE_UPLOAD_TIMEOUT, fallback=fallback
            )
            sent = await srb_pipeline.upload_video_resilient(
                [], str(path), caption, ctx["message"], owner_id,
                metadata.get("width") or 0, metadata.get("height") or 0,
                metadata.get("duration") or 0, upload_chat_id=pair["target_id"],
            )
        elif kind == "photos":
            sent = await srb_pipeline.upload_photo_resilient(
                [], str(path), caption, ctx["message"], owner_id,
                upload_chat_id=pair["target_id"],
            )
        elif kind == "audio":
            audio = getattr(item, "audio", None)
            sent = await srb_pipeline.upload_audio_resilient(
                [], str(path), caption, ctx["message"], owner_id,
                duration=getattr(audio, "duration", 0) or 0,
                performer=getattr(audio, "performer", None),
                title=getattr(audio, "title", None),
                upload_chat_id=pair["target_id"],
            )
        else:
            sent = await srb_pipeline.upload_document_resilient(
                [], str(path), caption, ctx["message"], owner_id,
                upload_chat_id=pair["target_id"],
            )
        if sent is None:
            raise RuntimeError("Telegram did not confirm the album item upload")
        await _record_transfer(pair, item.id, "success", f"{kind} uploaded from cached album download",
                               target_message_id=getattr(sent, "id", None))
        await _mark_cursor(pair, item.id, "success")
        await _log(pair, "success", item.id, f"{kind} uploaded using cached album download")
        await _finish_progress(ctx, "✅ Upload completed · 100%")
        return True
    except FloodWait:
        raise
    except Exception as exc:
        await _handle_file_failure(pair, item, f"Cached album upload failed: {exc}", ctx, "upload_failed")
        row = await transfers_col.find_one({
            "pair_id": pair["pair_id"], "source_message_id": int(item.id)
        }) or {}
        return row.get("status") == "skipped"


async def _upload_album(user, pair, album, queue_start, queue_total, ctx=None):
    """Keep supported albums grouped; Pyrogram's send_media_group has no upload callback."""
    allowed = [(_filter_reason(pair, item) is None and _media_kind(item) in DEFAULT_FILTERS) for item in album]
    if not all(allowed) or len(album) < 2 or len(album) > 10:
        for index, item in enumerate(album):
            if ctx is None:
                ctx = await _create_progress(pair["owner_id"], pair, queue_start + index, queue_total, item)
            else:
                await _reuse_progress(ctx, queue_start + index, queue_total, item)
            try:
                await _upload_media(user, pair, item, ctx)
            except TransferRetry:
                row = await transfers_col.find_one({"pair_id": pair["pair_id"], "source_message_id": int(item.id)}) or {}
                if row.get("status") != "skipped":
                    return False
            except FloodWait:
                raise
            await asyncio.sleep(INTER_FILE_DELAY)
        return True

    def _album_api_kind(item):
        if getattr(item, "photo", None) or getattr(item, "video", None) or _is_video_document(item):
            return "visual"
        if getattr(item, "audio", None):
            return "audio"
        if getattr(item, "document", None):
            return "document"
        return None

    # Telegram media groups cannot mix arbitrary media classes. Split mixed
    # albums into individual transfers instead of issuing a doomed group upload.
    album_api_kinds = [_album_api_kind(item) for item in album]
    album_kind_ok = all(album_api_kinds) and len(set(album_api_kinds)) == 1
    if not album_kind_ok:
        for index, item in enumerate(album):
            if ctx is None:
                ctx = await _create_progress(pair["owner_id"], pair, queue_start + index, queue_total, item)
            else:
                await _reuse_progress(ctx, queue_start + index, queue_total, item)
            try:
                await _upload_media(user, pair, item, ctx)
            except TransferRetry:
                row = await transfers_col.find_one({"pair_id": pair["pair_id"], "source_message_id": int(item.id)}) or {}
                if row.get("status") != "skipped":
                    return False
            await asyncio.sleep(INTER_FILE_DELAY)
        return True

    album_id = str(getattr(album[0], "media_group_id", "") or album[0].id)
    already_sent = set()
    for item in album:
        existing = await transfers_col.find_one({
            "pair_id": pair["pair_id"], "source_message_id": int(item.id),
            "status": {"$in": ["success", "skipped"]}
        })
        if existing:
            already_sent.add(int(item.id))
    if len(already_sent) == len(album):
        for item in album:
            row = await transfers_col.find_one({"pair_id": pair["pair_id"], "source_message_id": int(item.id)})
            await _mark_cursor(pair, item.id, "skipped" if row and row.get("status") == "skipped" else "already_done")
        return True
    if already_sent:
        # A process may have stopped while recording an album's individual
        # success rows. Never resend items already confirmed by Telegram.
        for index, item in enumerate(album):
            if int(item.id) in already_sent:
                row = await transfers_col.find_one({"pair_id": pair["pair_id"], "source_message_id": int(item.id)})
                await _mark_cursor(pair, item.id, "skipped" if row and row.get("status") == "skipped" else "already_done")
                continue
            if ctx is None:
                ctx = await _create_progress(pair["owner_id"], pair, queue_start + index, queue_total, item)
            else:
                await _reuse_progress(ctx, queue_start + index, queue_total, item)
            try:
                await _upload_media(user, pair, item, ctx)
            except TransferRetry:
                row = await transfers_col.find_one({"pair_id": pair["pair_id"], "source_message_id": int(item.id)}) or {}
                if row.get("status") != "skipped":
                    return False
            except FloodWait:
                raise
        return True
    for item in album:
        await _record_transfer(pair, item.id, "processing", "Album transfer started", album_id=album_id)

    with tempfile.TemporaryDirectory(prefix="jarvis_auto_album_") as tmp:
        media, source_paths, captions_by_id = [], [], {}
        for index, item in enumerate(album):
            if ctx is None:
                ctx = await _create_progress(pair["owner_id"], pair, queue_start + index, queue_total, item)
            else:
                await _reuse_progress(ctx, queue_start + index, queue_total, item)
            try:
                await _set_progress(ctx, f"⬇ Downloading album item {index + 1}/{len(album)}", force=True)
                item_filename, item_caption = await _customized_payload(pair["owner_id"], item)
                captions_by_id[int(item.id)] = item_caption
                path = await download_media_resilient(
                    user, item,
                    progress=_transfer_progress,
                    progress_args=(ctx, "⬇ Downloading"),
                    progress_message=ctx.get("message") if ctx else None,
                    user_id=pair.get("owner_id"),
                    retries=TRANSFER_RETRIES,
                )
                if not path or not os.path.isfile(path) or os.path.getsize(path) <= 0:
                    raise RuntimeError(f"Download returned no valid file for message {item.id}")
                target_path = os.path.join(tmp, os.path.basename(item_filename))
                shutil.move(path, target_path)
                path = target_path
                source_paths.append(path)
            except FloodWait:
                raise
            except Exception as exc:
                await _handle_file_failure(pair, item, f"Album download failed: {exc}", ctx, "download_failed")
                failed_row = await transfers_col.find_one({
                    "pair_id": pair["pair_id"], "source_message_id": int(item.id)
                }) or {}
                if failed_row.get("status") != "skipped":
                    return False
                # The failed album item reached its retry limit. Publish the
                # remaining/unprocessed siblings individually so the queue can
                # continue without waiting for another polling cycle.
                for fallback_index, fallback_item in enumerate(album):
                    try:
                        await _upload_media(user, pair, fallback_item, ctx)
                    except TransferRetry:
                        row = await transfers_col.find_one({
                            "pair_id": pair["pair_id"], "source_message_id": int(fallback_item.id)
                        }) or {}
                        if row.get("status") != "skipped":
                            return False
                    except FloodWait:
                        raise
                    await asyncio.sleep(INTER_FILE_DELAY)
                return True
            await _finish_progress(ctx, f"✅ Downloaded album item {index + 1}/{len(album)}")

        for item, path in zip(album, source_paths):
            _, caption = await _customized_payload(pair["owner_id"], item)
            if getattr(item, "photo", None):
                media.append(InputMediaPhoto(path, caption=caption, parse_mode=ParseMode.DISABLED))
            elif getattr(item, "video", None) or _is_video_document(item):
                source_video = getattr(item, "video", None)
                media.append(InputMediaVideo(
                    path, caption=caption, parse_mode=ParseMode.DISABLED,
                    supports_streaming=bool(getattr(source_video, "supports_streaming", True)),
                    width=getattr(source_video, "width", 0) or 0,
                    height=getattr(source_video, "height", 0) or 0,
                    duration=getattr(source_video, "duration", 0) or 0,
                ))
            elif getattr(item, "audio", None):
                media.append(InputMediaAudio(path, caption=caption, parse_mode=ParseMode.DISABLED,
                    title=getattr(item.audio, "title", None), performer=getattr(item.audio, "performer", None)))
            else:
                media.append(InputMediaDocument(path, caption=caption, parse_mode=ParseMode.DISABLED))

        if ctx is None:
            ctx = await _create_progress(pair["owner_id"], pair, queue_start, queue_total, album[0])
        else:
            await _reuse_progress(ctx, queue_start, queue_total, album[0])
        await _set_progress(ctx, "⬆ Uploading album (Telegram album API; upload percentage unavailable)", force=True)
        try:
            sent_messages = await app.send_media_group(pair["target_id"], media)
            if not sent_messages or len(sent_messages) != len(album):
                raise RuntimeError("Telegram did not confirm every album item")
        except FloodWait:
            raise
        except Exception as exc:
            # If Telegram rejects the grouped upload, fall back to the exact
            # per-file Save Restricted upload pipeline. This also gives each
            # failed item its own bounded retries and Skip/Retry choice.
            for index, (item, local_path) in enumerate(zip(album, source_paths)):
                if ctx is None:
                    ctx = await _create_progress(pair["owner_id"], pair, queue_start + index, queue_total, item)
                else:
                    await _reuse_progress(ctx, queue_start + index, queue_total, item)
                ok = await _upload_pre_downloaded_album_item(
                    pair, item, local_path, captions_by_id.get(int(item.id), ""), ctx
                )
                if not ok:
                    return False
                await asyncio.sleep(INTER_FILE_DELAY)
            return True

    for item, sent in zip(album, sent_messages):
        await _record_transfer(pair, item.id, "success", "Album item uploaded", target_message_id=getattr(sent, "id", None), album_id=album_id)
        await _mark_cursor(pair, item.id, "success")
        await _log(pair, "success", item.id, "Album uploaded")
    await _finish_progress(ctx, f"✅ Album uploaded · {len(album)} items")
    return True


async def _process_one(user, pair, message, position, total, ctx=None):
    lock = PAIR_LOCKS.setdefault(pair["pair_id"], asyncio.Lock())
    async with lock:
        latest = await pairs_col.find_one({"pair_id": pair["pair_id"]})
        if not latest or not latest.get("enabled", True):
            return False
        if int(message.id) <= int(latest.get("last_processed_message_id", 0)):
            return True
        success_record = await transfers_col.find_one({
            "pair_id": pair["pair_id"], "source_message_id": int(message.id), "status": "success"
        })
        if success_record:
            await _mark_cursor(pair, message.id, "already_done")
            return True
        if ctx is None:
            ctx = await _create_progress(pair["owner_id"], pair, position, total, message)
        else:
            await _reuse_progress(ctx, position, total, message)
        try:
            await _upload_media(user, pair, message, ctx)
            return True
        except FloodWait as exc:
            wait_seconds = int(getattr(exc, "value", 1) or 1)
            await _auto_pause(pair, wait_seconds, message.id, exc)
            await _finish_progress(ctx, f"⏸ FloodWait · auto-resume in {wait_seconds}s; this file is retained in queue")
            return False
        except TransferRetry:
            record = await transfers_col.find_one({
                "pair_id": pair["pair_id"], "source_message_id": int(message.id)
            }) or {}
            return record.get("status") == "skipped"
        except Exception as exc:
            await _handle_file_failure(pair, message, str(exc), ctx, "failed")
            record = await transfers_col.find_one({
                "pair_id": pair["pair_id"], "source_message_id": int(message.id)
            }) or {}
            return record.get("status") == "skipped"


async def _repair_legacy_failed_messages(pair):
    """Rewind old cursor past messages that the previous version wrongly marked done."""
    if pair.get("legacy_failures_repaired"):
        return
    cursor = int(pair.get("last_processed_message_id", 0))
    failed = await logs_col.find({
        "pair_id": pair["pair_id"],
        "source_message_id": {"$lte": cursor, "$ne": None},
        "kind": {"$in": ["failed", "download_failed", "upload_failed", "floodwait"]},
    }).sort("source_message_id", 1).limit(1).to_list(length=1)
    update = {"legacy_failures_repaired": True}
    if failed:
        first_failed = int(failed[0]["source_message_id"])
        update["last_processed_message_id"] = max(0, first_failed - 1)
        # Import the old version's success logs into the durable transfer ledger
        # before rewinding. This lets us retry old failures without re-uploading
        # later messages that the previous worker had already delivered.
        legacy_logs = await logs_col.find({
            "pair_id": pair["pair_id"],
            "source_message_id": {"$gte": first_failed, "$lte": cursor},
            "kind": {"$in": ["success", "failed", "download_failed", "upload_failed", "floodwait"]},
        }).sort("created_at", 1).to_list(length=10000)
        latest_by_id = {}
        successful_ids = set()
        for item in legacy_logs:
            if item.get("source_message_id") is not None:
                message_id = int(item["source_message_id"])
                latest_by_id[message_id] = item
                if item.get("kind") == "success":
                    successful_ids.add(message_id)
        for message_id, item in latest_by_id.items():
            status = "success" if message_id in successful_ids else "pending"
            await _record_transfer(pair, message_id, status, item.get("detail", "Recovered legacy transfer"))
        await pairs_col.update_one({"pair_id": pair["pair_id"]}, {"$pull": {"processed_ids": {"$gte": first_failed}}})
        await _log(pair, "info", first_failed, "Recovered a message skipped by the previous Auto Update version; retrying it")
    await pairs_col.update_one({"pair_id": pair["pair_id"]}, {"$set": update})


async def _source_message_link(user, source_id, message_id):
    try:
        chat = await user.get_chat(source_id)
        username = getattr(chat, "username", None)
        if username:
            return f"https://t.me/{username}/{int(message_id)}"
    except Exception:
        pass
    raw = str(source_id)
    if raw.startswith("-100"):
        return f"https://t.me/c/{raw[4:]}/{int(message_id)}"
    return None


async def _send_auto_skipped_report(user, pair):
    # Send bounded chunks so a long failure list never exceeds Telegram's message limit.
    while True:
        rows = await transfers_col.find({
            "pair_id": pair["pair_id"], "failure_auto_skipped": True, "report_sent": {"$ne": True}
        }).sort("source_message_id", 1).to_list(length=30)
        if not rows:
            return
        lines = []
        for row in rows:
            mid = int(row.get("source_message_id") or 0)
            link = await _source_message_link(user, pair["source_id"], mid) if mid else None
            lines.append(f"• [Message {mid}]({link})" if link else f"• Message `{mid}`")
        text = (
            "╭━━━〔 ⚠️ 𝗔𝗨𝗧𝗢 𝗨𝗣𝗗𝗔𝗧𝗘 𝗥𝗘𝗣𝗢𝗥𝗧 〕━━━╮\n\n"
            f"Pair: `{pair['pair_id']}`\n"
            f"Files skipped after 2 retries: `{len(rows)}`\n\n"
            + "\n".join(lines)
            + "\n\nOpen these source links to review the files that could not be transferred.\n╰━━━━━━━━━━━━━━━━━━━━╯"
        )
        try:
            await app.send_message(int(pair["owner_id"]), text, disable_web_page_preview=True)
            ids = [int(row["source_message_id"]) for row in rows]
            await transfers_col.update_many(
                {"pair_id": pair["pair_id"], "source_message_id": {"$in": ids}},
                {"$set": {"report_sent": True, "report_sent_at": _now()}},
            )
        except Exception as exc:
            print(f"Auto Update skip report failed for {pair.get('pair_id')}: {exc}")
            return


async def _poll_pair(pair):
    if not pair.get("enabled", True):
        return
    now = _now()
    retry_at = _as_utc(pair.get("auto_paused_until") or pair.get("next_retry_at"))
    if retry_at and retry_at > now:
        return
    if retry_at and retry_at <= now:
        await pairs_col.update_one({"pair_id": pair["pair_id"]}, {"$set": {
            "auto_paused_until": None, "auto_pause_reason": None, "next_retry_at": None,
        }})

    await _repair_legacy_failed_messages(pair)
    pair = await pairs_col.find_one({"pair_id": pair["pair_id"]}) or pair
    owner_id = int(pair["owner_id"])
    user = await _get_user_client(owner_id)
    cursor = int(pair.get("last_processed_message_id", 0))
    pending = []
    # Pyrogram yields newest-first. Walk back only as far as the durable cursor,
    # then process oldest-first so a failed file never gets jumped over.
    async for message in user.get_chat_history(pair["source_id"], limit=0):
        if int(message.id) <= cursor:
            break
        pending.append(message)
    pending.sort(key=lambda item: int(item.id))
    if not pending:
        await _send_auto_skipped_report(user, pair)
        return
    backlog_count = len(pending)
    # Bound each pair's work per pass so a large backlog in one channel cannot
    # starve other configured pairs. The durable cursor makes the next pass
    # continue with the next oldest unprocessed post.
    pending = pending[:MAX_MESSAGES_PER_PAIR_PER_CYCLE]

    handled_groups = set()
    index_by_id = {int(message.id): index + 1 for index, message in enumerate(pending)}
    progress_ctx = await _create_progress(owner_id, pair, 1, backlog_count, pending[0])
    for message in pending:
        current = await pairs_col.find_one({"pair_id": pair["pair_id"]})
        if not current or not current.get("enabled", True):
            return
        wait_until = _as_utc(current.get("auto_paused_until") or current.get("next_retry_at"))
        if wait_until and wait_until > _now():
            return
        group_id = getattr(message, "media_group_id", None)
        if group_id:
            if group_id in handled_groups:
                continue
            handled_groups.add(group_id)
            try:
                album = await user.get_media_group(pair["source_id"], message.id)
                album.sort(key=lambda item: int(item.id))
            except FloodWait:
                raise
            except Exception:
                album = [message]
            if len(album) > 1:
                first_position = min(index_by_id.get(int(item.id), 1) for item in album)
                try:
                    ok = await _upload_album(user, pair, album, first_position, backlog_count, progress_ctx)
                except FloodWait as exc:
                    wait_seconds = int(getattr(exc, "value", 1) or 1)
                    await _auto_pause(pair, wait_seconds, message.id, exc)
                    await _finish_progress(progress_ctx, f"⏸ FloodWait · auto-resume in {wait_seconds}s; album retained in queue")
                    return
                if not ok:
                    return
                await asyncio.sleep(INTER_FILE_DELAY)
                continue
        ok = await _process_one(user, pair, message, index_by_id.get(int(message.id), 1), backlog_count, progress_ctx)
        if not ok:
            return
        await asyncio.sleep(INTER_FILE_DELAY)
    if backlog_count > len(pending):
        await _finish_progress(progress_ctx, f"✅ {len(pending)} posts processed · {backlog_count - len(pending)} remain queued for the next pass")
    else:
        await _finish_progress(progress_ctx, f"✅ Queue complete · {len(pending)} new posts checked")
    await _send_auto_skipped_report(user, pair)


async def auto_update_worker():
    while True:
        try:
            await _ensure_indexes()
            pairs = await pairs_col.find({"enabled": True}).to_list(length=500)
            for pair in pairs:
                try:
                    await _poll_pair(pair)
                except FloodWait as exc:
                    seconds = int(getattr(exc, "value", 1) or 1)
                    # FloodWait can also happen while reading history. Keep the
                    # pair's current cursor unchanged and retry after the wait.
                    await _auto_pause(pair, seconds, None, exc)
                except Exception as exc:
                    await _retry_later(pair, None, exc, "failed")
                    print(f"Auto Update pair {pair.get('pair_id')} error: {exc}")
        except Exception as exc:
            print(f"Auto Update worker error: {exc}")
        await asyncio.sleep(POLL_INTERVAL)


def start_auto_update_worker():
    global _worker_task, _started
    if _started and _worker_task and not _worker_task.done():
        return _worker_task
    _started = True
    _worker_task = asyncio.create_task(auto_update_worker(), name="jarvis-auto-update-worker")
    print("JARVIS Auto Update background worker started.")
    return _worker_task
