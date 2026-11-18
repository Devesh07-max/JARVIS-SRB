"""Persistent per-user Telegram channel auto-update pairs.

Source access is performed with the owner's saved Pyrogram user session. Target
publishing is performed with the existing JARVIS bot client.
"""
import asyncio
import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone

from pyrogram import Client
from telethon import events
from telethon.tl.custom import Button
from pyrogram.errors import FloodWait
from pyrogram.enums import ChatMemberStatus, ParseMode

from safe_repo import app, sex as gf
from safe_repo.core.mongo import db as users_db
from config import API_ID, API_HASH, MONGO_DB, MONGO_DATABASE_NAME
from motor.motor_asyncio import AsyncIOMotorClient

_mongo = AsyncIOMotorClient(MONGO_DB)
_database = _mongo[MONGO_DATABASE_NAME]
pairs_col = _database["auto_update_pairs"]
logs_col = _database["auto_update_logs"]

DEFAULT_FILTERS = {"videos": True, "documents": True, "photos": True, "audio": True, "apk": True}
POLL_INTERVAL = max(5, int(os.getenv("AUTO_UPDATE_POLL_INTERVAL", "10")))
PAIR_STATES = {}
PAIR_LOCKS = {}
PROMPTS = {}
_worker_task = None
_started = False


def _now():
    return datetime.now(timezone.utc)


class AutoUpdateTransferError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def _pair_keyboard(pair):
    pid = pair["pair_id"]
    state = "⏸ Pause" if pair.get("enabled", True) else "▶️ Resume"
    return [
        [Button.inline(state, f"au:toggle:{pid}".encode())],
        [Button.inline("⚙️ Pair Settings", f"au:settings:{pid}".encode()),
         Button.inline("📜 Activity / Logs", f"au:logs:{pid}".encode())],
        [Button.inline("✏️ Edit Pair", f"au:edit:{pid}".encode()),
         Button.inline("🗑 Delete Pair", f"au:delete:{pid}".encode())],
        [Button.inline("⬅️ All Pairs", b"au:list")],
    ]


def _settings_keyboard(pair):
    pid = pair["pair_id"]
    f = pair.get("filters") or DEFAULT_FILTERS
    rows = []
    for key, label in [("videos", "Videos"), ("documents", "Documents"), ("photos", "Photos"), ("audio", "Audio"), ("apk", "APK files")]:
        rows.append([Button.inline(f"{'✅' if f.get(key, True) else '❌'} {label}",
                                   f"au:filter:{pid}:{key}".encode())])
    rows += [
        [Button.inline("🚫 Ignore Rules", f"au:ignore:{pid}".encode())],
        [Button.inline("⬅️ Pair Menu", f"au:pair:{pid}".encode())],
    ]
    return rows


async def _log(pair, kind, message_id=None, detail=""):
    await logs_col.insert_one({
        "pair_id": pair["pair_id"], "owner_id": pair["owner_id"],
        "kind": kind, "source_message_id": message_id,
        "detail": str(detail)[:1500], "created_at": _now(),
    })
    update = {"last_activity_at": _now()}
    if kind == "success":
        update["last_success_at"] = _now()
        update["last_error"] = None
    elif kind in ("failed", "download_failed", "upload_failed"):
        update["last_error"] = str(detail)[:1000]
    await pairs_col.update_one({"pair_id": pair["pair_id"]}, {"$set": update})


async def _get_user_client(owner_id):
    data = await users_db.get_data(int(owner_id))
    if not data or not data.get("session"):
        raise RuntimeError("Telegram login is missing. Use /login first.")
    client = Client(
        f"auto_update_{owner_id}", api_id=API_ID, api_hash=API_HASH,
        session_string=data["session"], no_updates=True, sleep_threshold=60,
    )
    await client.start()
    return client


def _chat_name(chat):
    return (getattr(chat, "title", None) or
            " ".join(x for x in [getattr(chat, "first_name", None), getattr(chat, "last_name", None)] if x) or
            getattr(chat, "username", None) or str(chat.id))


async def _resolve_pair_channels(owner_id, source_id, target_id):
    user = await _get_user_client(owner_id)
    try:
        source = await user.get_chat(source_id)
    finally:
        await user.stop()
    target = await app.get_chat(target_id)
    bot_me = await app.get_me()
    member = await app.get_chat_member(target.id, bot_me.id)
    status = getattr(member, "status", None)
    if status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        raise ValueError("JARVIS bot must be an administrator in the target channel.")
    privileges = getattr(member, "privileges", None)
    if privileges is not None and not getattr(privileges, "can_post_messages", False):
        raise ValueError("JARVIS bot needs permission to post messages in the target channel.")
    return source, target


async def _latest_message_id(client, chat_id):
    async for msg in client.get_chat_history(chat_id, limit=1):
        return int(msg.id)
    return 0


async def _pair_view(event, pair):
    text = (
        f"🔄 **AUTO UPDATE PAIR**\n\n"
        f"📥 Source: **{pair.get('source_name', pair['source_id'])}**\n"
        f"📤 Target: **{pair.get('target_name', pair['target_id'])}**\n"
        f"📌 Status: **{'Running' if pair.get('enabled', True) else 'Paused'}**\n"
        f"🕒 Last successful update: `{pair.get('last_success_at') or 'Never'}`\n"
        f"⚠️ Last error: `{pair.get('last_error') or 'None'}`"
    )
    await event.edit(text, buttons=_pair_keyboard(pair))


async def _list_pairs(event, owner_id):
    pairs = await pairs_col.find({"owner_id": int(owner_id)}).sort("created_at", -1).to_list(length=100)
    rows = []
    summaries = []
    for pair in pairs:
        label = f"{pair.get('source_name', pair['source_id'])} → {pair.get('target_name', pair['target_id'])}"
        status = "Running" if pair.get("enabled", True) else "Paused"
        last_ok = pair.get("last_success_at") or "Never"
        last_error = pair.get("last_error") or "None"
        summaries.append(
            f"**{label}**\\n"
            f"Status: `{status}` · Last success: `{last_ok}`\\n"
            f"Last error: `{str(last_error)[:120]}`"
        )
        rows.append([Button.inline(("🟢 " if pair.get("enabled", True) else "⏸ ") + label[:55],
                                   f"au:pair:{pair['pair_id']}".encode())])
    rows.append([Button.inline("➕ Add New Pair", b"au:add")])
    body = "\\n\\n".join(summaries) if summaries else "No pairs saved yet."
    await event.edit("🔄 **AUTO UPDATE MANAGEMENT**\\n\\n" + body + "\\n\\nSelect a pair or add a new source → target pair.",
                     buttons=rows)


async def _prompt_add(event, owner_id):
    PROMPTS[int(owner_id)] = {"action": "add"}
    await event.respond(
        "➕ **Add New Pair**\n\nSend both channel IDs in one message:\n"
        "`SOURCE_CHANNEL_ID TARGET_CHANNEL_ID`\n\n"
        "Example: `-1001234567890 -1009876543210`\n\n"
        "Your logged-in Telegram account must access the source. JARVIS bot must be admin in the target."
    )


async def _prompt_edit(event, owner_id, pair):
    PROMPTS[int(owner_id)] = {"action": "edit", "pair_id": pair["pair_id"]}
    await event.respond("✏️ Send the new source and target channel IDs:\n`SOURCE_CHANNEL_ID TARGET_CHANNEL_ID`")


async def _prompt_ignore(event, owner_id, pair, rule_type):
    PROMPTS[int(owner_id)] = {"action": "ignore", "pair_id": pair["pair_id"], "rule_type": rule_type}
    await event.respond(f"Send the {rule_type.replace('_',' ')} to ignore. Matching is case-insensitive.")


async def _process_prompt(event):
    owner_id = int(event.sender_id)
    prompt = PROMPTS.get(owner_id)
    if not prompt or not event.raw_text:
        return
    raw = event.raw_text.strip()
    if raw.startswith("/"):
        return
    if prompt["action"] in ("add", "edit"):
        values = raw.split()
        if len(values) != 2:
            await event.respond("❌ Send exactly two channel IDs separated by a space.")
            return
        try:
            source_id, target_id = int(values[0]), int(values[1])
            source, target = await _resolve_pair_channels(owner_id, source_id, target_id)
            if prompt["action"] == "add":
                user = await _get_user_client(owner_id)
                try:
                    last_id = await _latest_message_id(user, source.id)
                finally:
                    await user.stop()
                pair = {
                    "pair_id": uuid.uuid4().hex[:12], "owner_id": owner_id,
                    "source_id": int(source.id), "target_id": int(target.id),
                    "source_name": _chat_name(source), "target_name": _chat_name(target),
                    "pair_name": f"{_chat_name(source)} → {_chat_name(target)}",
                    "enabled": True, "filters": dict(DEFAULT_FILTERS), "ignore_rules": [],
                    "last_processed_message_id": last_id, "processed_ids": [],
                    "created_at": _now(), "last_activity_at": _now(),
                    "last_success_at": None, "last_error": None,
                }
                await pairs_col.insert_one(pair)
            else:
                pair = await pairs_col.find_one({"pair_id": prompt["pair_id"], "owner_id": owner_id})
                if not pair:
                    raise ValueError("Pair not found.")
                user = await _get_user_client(owner_id)
                try:
                    latest_id = await _latest_message_id(user, source.id)
                finally:
                    await user.stop()
                await pairs_col.update_one({"pair_id": pair["pair_id"]}, {"$set": {
                    "source_id": int(source.id), "target_id": int(target.id),
                    "source_name": _chat_name(source), "target_name": _chat_name(target),
                    "pair_name": f"{_chat_name(source)} → {_chat_name(target)}",
                    "last_processed_message_id": latest_id, "processed_ids": [],
                }})
                pair = await pairs_col.find_one({"pair_id": pair["pair_id"]})
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

    if action == "pair":
        if pair:
            await _pair_view(event, pair)
    elif action == "toggle":
        if pair:
            await pairs_col.update_one({"pair_id": pair_id}, {"$set": {"enabled": not pair.get("enabled", True)}})
            pair = await pairs_col.find_one({"pair_id": pair_id})
            await _pair_view(event, pair)
    elif action == "settings":
        if pair:
            await event.edit("⚙️ **PAIR SETTINGS**\\n\\nToggle each media type independently.", buttons=_settings_keyboard(pair))
    elif action == "filter":
        key = parts[3] if len(parts) > 3 else ""
        if pair and key in DEFAULT_FILTERS:
            current = pair.get("filters") or dict(DEFAULT_FILTERS)
            current[key] = not current.get(key, True)
            await pairs_col.update_one({"pair_id": pair_id}, {"$set": {"filters": current}})
            pair["filters"] = current
            await event.edit("⚙️ **PAIR SETTINGS**\\n\\nToggle each media type independently.", buttons=_settings_keyboard(pair))
    elif action == "ignore":
        if pair:
            rules = pair.get("ignore_rules", [])
            rows = [[Button.inline(f"🗑 {r['type']}: {r['value']}"[:55],
                                   f"au:delrule:{pair_id}:{i}".encode())] for i, r in enumerate(rules)]
            rows += [
                [Button.inline("➕ Exact filename", f"au:addrule:{pair_id}:exact_filename".encode())],
                [Button.inline("➕ Filename keyword", f"au:addrule:{pair_id}:filename_keyword".encode())],
                [Button.inline("➕ Caption keyword", f"au:addrule:{pair_id}:caption_keyword".encode())],
                [Button.inline("⬅️ Settings", f"au:settings:{pair_id}".encode())],
            ]
            await event.edit("🚫 **IGNORE RULES**\\n\\nTap a rule to delete it.", buttons=rows)
    elif action == "addrule":
        if pair:
            await _prompt_ignore(event, owner_id, pair, parts[3])
    elif action == "delrule":
        index = int(parts[3]) if len(parts) > 3 else -1
        rules = pair.get("ignore_rules", []) if pair else []
        if pair and 0 <= index < len(rules):
            rules.pop(index)
            await pairs_col.update_one({"pair_id": pair_id}, {"$set": {"ignore_rules": rules}})
            rows = [[Button.inline(f"🗑 {r['type']}: {r['value']}"[:55],
                                   f"au:delrule:{pair_id}:{i}".encode())] for i, r in enumerate(rules)]
            rows += [
                [Button.inline("➕ Exact filename", f"au:addrule:{pair_id}:exact_filename".encode())],
                [Button.inline("➕ Filename keyword", f"au:addrule:{pair_id}:filename_keyword".encode())],
                [Button.inline("➕ Caption keyword", f"au:addrule:{pair_id}:caption_keyword".encode())],
                [Button.inline("⬅️ Settings", f"au:settings:{pair_id}".encode())],
            ]
            await event.edit("🚫 **IGNORE RULES**", buttons=rows)
    elif action == "edit":
        if pair:
            await _prompt_edit(event, owner_id, pair)
    elif action == "delete":
        if pair:
            await pairs_col.delete_one({"pair_id": pair_id, "owner_id": owner_id})
            await logs_col.delete_many({"pair_id": pair_id, "owner_id": owner_id})
        await event.edit("🗑 Pair deleted.", buttons=[[Button.inline("⬅️ All Pairs", b"au:list")]])
    elif action == "logs":
        logs = await logs_col.find({"pair_id": pair_id, "owner_id": owner_id}).sort("created_at", -1).to_list(length=15)
        lines = [f"• `{x.get('created_at')}` **{x['kind']}** — {x.get('detail', '')}" for x in logs]
        rows = [
            [Button.inline("🔄 Refresh", f"au:logs:{pair_id}".encode())],
            [Button.inline("⬅️ Pair Menu", f"au:pair:{pair_id}".encode())],
        ]
        await event.edit("📜 **RECENT ACTIVITY**\\n\\n" + ("\\n".join(lines) if lines else "No activity yet."), buttons=rows)


def _media_kind(message):
    if message.photo: return "photos"
    if message.video or message.video_note: return "videos"
    if message.audio or message.voice: return "audio"
    if message.document:
        doc = message.document
        filename = (doc.file_name or "").lower()
        mime = (doc.mime_type or "").lower()
        if filename.endswith(".apk") or "android.package-archive" in mime:
            return "apk"
        if mime.startswith("video/"):
            return "videos"
        if mime.startswith("audio/"):
            return "audio"
        return "documents"
    return "text" if message.text or message.caption else "other"


def _ignored(pair, message):
    filename = ""
    if message.document: filename = message.document.file_name or ""
    elif message.video: filename = message.video.file_name or ""
    elif message.audio: filename = message.audio.file_name or ""
    caption = (message.caption or message.text or "").casefold()
    fname = filename.casefold()
    for rule in pair.get("ignore_rules", []):
        value = str(rule.get("value", "")).casefold()
        typ = rule.get("type")
        if typ == "exact_filename" and fname == value: return f"exact filename: {rule['value']}"
        if typ == "filename_keyword" and value and value in fname: return f"filename keyword: {rule['value']}"
        if typ == "caption_keyword" and value and value in caption: return f"caption keyword: {rule['value']}"
    return None


async def _upload_one(user, pair, msg):
    kind = _media_kind(msg)
    if kind in DEFAULT_FILTERS and not pair.get("filters", {}).get(kind, True):
        await _log(pair, "skipped", msg.id, f"{kind} filter is disabled")
        return False
    reason = _ignored(pair, msg)
    if reason:
        await _log(pair, "skipped", msg.id, reason)
        return False
    caption = msg.caption or ""
    if kind == "text":
        await app.send_message(pair["target_id"], msg.text or "", disable_web_page_preview=True, parse_mode=ParseMode.DISABLED)
        await _log(pair, "success", msg.id, "Text post uploaded")
        return True
    if kind == "other":
        await _log(pair, "skipped", msg.id, "Unsupported message type")
        return False
    with tempfile.TemporaryDirectory(prefix="jarvis_auto_update_") as tmp:
        try:
            path = await user.download_media(msg, file_name=os.path.join(tmp, "media"))
            if not path or not os.path.isfile(path):
                raise RuntimeError("Telegram media download returned no file")
        except FloodWait:
            raise
        except Exception as exc:
            raise AutoUpdateTransferError("download_failed", exc)
        try:
            if kind == "photos":
                await app.send_photo(pair["target_id"], path, caption=caption, parse_mode=ParseMode.DISABLED)
            elif kind == "videos":
                await app.send_video(pair["target_id"], path, caption=caption, parse_mode=ParseMode.DISABLED,
                                     supports_streaming=bool(msg.video and msg.video.supports_streaming))
            elif kind == "audio":
                if msg.voice:
                    await app.send_voice(pair["target_id"], path, caption=caption, parse_mode=ParseMode.DISABLED)
                else:
                    await app.send_audio(pair["target_id"], path, caption=caption, parse_mode=ParseMode.DISABLED,
                                         title=getattr(msg.audio, "title", None),
                                         performer=getattr(msg.audio, "performer", None))
            else:
                await app.send_document(pair["target_id"], path, caption=caption, parse_mode=ParseMode.DISABLED)
        except FloodWait:
            raise
        except Exception as exc:
            raise AutoUpdateTransferError("upload_failed", exc)
    await _log(pair, "success", msg.id, f"{kind} uploaded")
    return True


async def _process_message(user, pair, msg):
    key = (pair["pair_id"], int(msg.id))
    lock = PAIR_LOCKS.setdefault(pair["pair_id"], asyncio.Lock())
    async with lock:
        latest = await pairs_col.find_one({"pair_id": pair["pair_id"]}, {"processed_ids": 1})
        if int(msg.id) in (latest or {}).get("processed_ids", []):
            return
        try:
            await _upload_one(user, pair, msg)
            await pairs_col.update_one({"pair_id": pair["pair_id"]}, {
                "$addToSet": {"processed_ids": int(msg.id)},
                "$max": {"last_processed_message_id": int(msg.id)},
                "$set": {"last_activity_at": _now()},
            })
        except FloodWait as exc:
            # Stop this pair's current pass so later IDs cannot advance the
            # high-water mark past this not-yet-uploaded message.
            await _log(pair, "failed", msg.id, f"FloodWait: retry pending after {getattr(exc, 'value', '?')} seconds")
            raise
        except AutoUpdateTransferError as exc:
            await _log(pair, exc.kind, msg.id, exc)
            await pairs_col.update_one({"pair_id": pair["pair_id"]}, {
                "$addToSet": {"processed_ids": int(msg.id)},
                "$max": {"last_processed_message_id": int(msg.id)},
            })
        except Exception as exc:
            await _log(pair, "failed", msg.id, exc)
            # Mark failed as seen to prevent a poison message blocking the channel;
            # user can inspect the error in logs. It is not recorded as successful.
            await pairs_col.update_one({"pair_id": pair["pair_id"]}, {
                "$addToSet": {"processed_ids": int(msg.id)},
                "$max": {"last_processed_message_id": int(msg.id)},
            })


async def _poll_pair(pair):
    owner_id = int(pair["owner_id"])
    client = await _get_user_client(owner_id)
    try:
        # Newest-first history; only process messages beyond persisted high-water mark.
        pending = []
        async for msg in client.get_chat_history(pair["source_id"], limit=0):
            if int(msg.id) <= int(pair.get("last_processed_message_id", 0)):
                break
            pending.append(msg)
        pending.sort(key=lambda m: m.id)
        handled_groups = set()
        for msg in pending:
            if not await pairs_col.find_one({"pair_id": pair["pair_id"], "enabled": True}):
                break
            group_id = getattr(msg, "media_group_id", None)
            if group_id:
                if group_id in handled_groups:
                    continue
                handled_groups.add(group_id)
                try:
                    album = await client.get_media_group(pair["source_id"], msg.id)
                except Exception:
                    album = [msg]
                # Keep album intact where all entries are allowed and supported.
                album_allowed = all(
                    _media_kind(x) in DEFAULT_FILTERS
                    and pair.get("filters", {}).get(_media_kind(x), True)
                    and not _ignored(pair, x)
                    for x in album
                )
                album_is_documents = all(item.document for item in album)
                album_is_visual = all(item.photo or item.video for item in album)
                if album_allowed and len(album) > 1 and (album_is_documents or album_is_visual):
                    with tempfile.TemporaryDirectory(prefix="jarvis_album_") as tmp:
                        media = []
                        paths = []
                        try:
                            for index, item in enumerate(album):
                                path = await client.download_media(item, file_name=os.path.join(tmp, f"{index}"))
                                if not path or not os.path.isfile(path):
                                    raise RuntimeError(f"Album download failed for message {item.id}")
                                paths.append(path)
                        except FloodWait:
                            raise
                        except Exception as exc:
                            for item in album:
                                await _log(pair, "download_failed", item.id, exc)
                                await pairs_col.update_one({"pair_id": pair["pair_id"]}, {
                                    "$addToSet": {"processed_ids": int(item.id)},
                                    "$max": {"last_processed_message_id": int(item.id)},
                                })
                            continue
                        from pyrogram.types import InputMediaPhoto, InputMediaVideo, InputMediaDocument
                        for item, path in zip(album, paths):
                            cap = item.caption or ""
                            if item.photo:
                                media.append(InputMediaPhoto(path, caption=cap, parse_mode=ParseMode.DISABLED))
                            elif item.video:
                                media.append(InputMediaVideo(path, caption=cap, parse_mode=ParseMode.DISABLED))
                            elif item.document:
                                media.append(InputMediaDocument(path, caption=cap, parse_mode=ParseMode.DISABLED))
                            else:
                                media = []
                                break
                        if media:
                            try:
                                await app.send_media_group(pair["target_id"], media)
                            except FloodWait:
                                raise
                            except Exception as exc:
                                for item in album:
                                    await _log(pair, "upload_failed", item.id, exc)
                                    await pairs_col.update_one({"pair_id": pair["pair_id"]}, {
                                        "$addToSet": {"processed_ids": int(item.id)},
                                        "$max": {"last_processed_message_id": int(item.id)},
                                    })
                                continue
                            for item in album:
                                await _log(pair, "success", item.id, "Album uploaded")
                                await pairs_col.update_one({"pair_id": pair["pair_id"]}, {
                                    "$addToSet": {"processed_ids": int(item.id)},
                                    "$max": {"last_processed_message_id": int(item.id)},
                                })
                            continue
                for item in album:
                    await _process_message(client, pair, item)
                continue
            await _process_message(client, pair, msg)
    finally:
        await client.stop()


async def auto_update_worker():
    while True:
        try:
            pairs = await pairs_col.find({"enabled": True}).to_list(length=250)
            owners = {}
            for pair in pairs:
                owners.setdefault(int(pair["owner_id"]), []).append(pair)
            for owner_id, owner_pairs in owners.items():
                for pair in owner_pairs:
                    try:
                        await _poll_pair(pair)
                    except FloodWait as exc:
                        await asyncio.sleep(int(getattr(exc, "value", 1)) + 1)
                    except Exception as exc:
                        await _log(pair, "failed", None, exc)
        except Exception as exc:
            print(f"Auto Update worker error: {exc}")
        await asyncio.sleep(POLL_INTERVAL)


def start_auto_update_worker():
    global _worker_task, _started
    if _started:
        return
    _started = True
    _worker_task = asyncio.create_task(auto_update_worker(), name="jarvis-auto-update-worker")

