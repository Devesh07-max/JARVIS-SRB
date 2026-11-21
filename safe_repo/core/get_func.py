#safe_repo

import asyncio
import time
import os
import subprocess
import shutil
import tempfile
from urllib.parse import urlparse
import requests
from safe_repo import app
from safe_repo import sex as gf
import pymongo
from pyrogram import filters
from pyrogram.errors import (ChannelBanned, ChannelInvalid, ChannelPrivate, ChatIdInvalid, ChatInvalid, PeerIdInvalid, FloodWait, ChatAdminRequired, ChatWriteForbidden, UserNotParticipant, ChatForbidden)
from pyrogram.enums import MessageMediaType
from safe_repo.core.func import (
    progress_bar, video_metadata, video_metadata_async, screenshot,
    run_transfer_with_watchdog, get_active_plan,
)
from safe_repo.core.download_utils import download_media_resilient
from safe_repo.core.mongo import db
from pyrogram.types import Message
from config import MONGO_DB as MONGODB_CONNECTION_STRING, MONGO_DATABASE_NAME, LOG_GROUP, OWNER_ID, SUPPORT_URL, TRANSFER_IDLE_TIMEOUT, TRANSFER_RETRIES, MEDIA_FOOTER, FILE_TASK_TIMEOUT

UPLOAD_TOTAL_TIMEOUT = int(os.getenv("UPLOAD_TOTAL_TIMEOUT", "10800"))
POST_UPLOAD_TIMEOUT = max(15, int(os.getenv("POST_UPLOAD_TIMEOUT", "60")))
FORWARD_TIMEOUT = max(15, int(os.getenv("FORWARD_TIMEOUT", "90")))
# This covers only the work after the download is complete and before the
# upload coroutine is allowed to start.  It is intentionally separate from
# FILE_TASK_TIMEOUT: a blocked preparation step must not consume the whole
# per-file transfer budget or park the batch.
PRE_UPLOAD_TIMEOUT = max(30, int(os.getenv("PRE_UPLOAD_TIMEOUT", "120")))
# Message retrieval is a separate bounded stage.  Without this bound a stalled
# Pyrogram RPC can leave a batch parked before the first download progress update.
MESSAGE_FETCH_TIMEOUT = max(30, int(os.getenv("MESSAGE_FETCH_TIMEOUT", "90")))


def with_media_footer(caption, include_footer=True):
    """Append the configured footer exactly once unless the active plan opts out."""
    text = (caption or "").rstrip()
    if not include_footer:
        return text
    if MEDIA_FOOTER in text:
        return text
    return f"{text}\n\n{MEDIA_FOOTER}" if text else MEDIA_FOOTER


_CAPTION_QUOTE_CHARS = frozenset({
    '"', "'", '“', '”', '‘', '’', '„', '‟', '«', '»', '‹', '›',
})


def _caption_send_kwargs(caption):
    """Build caption arguments without letting quote characters enter Pyrogram's
    default caption parser.

    The source caption is deliberately returned unchanged.  For captions that
    contain quotation characters we disable Pyrogram's implicit Markdown parser
    and send the caption as literal text.  This keeps quote-containing captions
    from being misparsed and incorrectly turning a valid media transfer into a
    retry/skip, while preserving every character in the caption itself.
    """
    text = caption or ""
    kwargs = {"caption": text}
    if any(char in _CAPTION_QUOTE_CHARS for char in text):
        kwargs["parse_mode"] = None
    return kwargs


def message_media_kind(msg):
    """Return the actual downloadable Telegram media kind.

    Do not infer this from captions or from ``Message.media`` alone.  Pyrogram
    exposes the concrete media objects on the message and those are the source
    of truth for decorated-caption messages.
    """
    if getattr(msg, "photo", None) is not None:
        return "photo"
    if getattr(msg, "video", None) is not None:
        return "video"
    if getattr(msg, "audio", None) is not None:
        return "audio"
    if getattr(msg, "document", None) is not None:
        return "document"
    media = getattr(msg, "media", None)
    if media == MessageMediaType.PHOTO:
        return "photo"
    if media == MessageMediaType.VIDEO:
        return "video"
    if media == MessageMediaType.DOCUMENT:
        return "document"
    if media == MessageMediaType.AUDIO:
        return "audio"
    return None


def detect_image_extension(path):
    """Detect common image bytes without changing the file contents."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(32)
        if header.startswith(b"\xff\xd8\xff"):
            return "jpg"
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        if header.startswith((b"GIF87a", b"GIF89a")):
            return "gif"
        if header.startswith(b"BM"):
            return "bmp"
        if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
            return "webp"
        if header.startswith(b"\x00\x00\x01\x00"):
            return "ico"
    except (OSError, TypeError):
        pass
    return None


def _photo_upload_path(path):
    """Give a photo a truthful extension, preserving the original bytes."""
    detected = detect_image_extension(path)
    suffix = os.path.splitext(str(path))[1].lower().lstrip(".")
    if detected and suffix == detected:
        return path, False
    if detected:
        staged = f"{path}.{detected}"
        try:
            shutil.copyfile(path, staged)
            return staged, True
        except OSError:
            pass
    return path, False
import cv2
from telethon import events, Button
from safe_repo.core.speedtest_utils import run_speedtest
from safe_repo.core.log_utils import log_event, user_label
    



def thumbnail(sender):
    local = f'{sender}.jpg'
    if os.path.exists(local):
        return local
    try:
        data = collection.find_one({"_id": sender}, {"thumbnail_data": 1})
        blob = data.get("thumbnail_data") if data else None
        if blob:
            with open(local, "wb") as f:
                f.write(bytes(blob))
            return local
    except Exception as e:
        print(f"Thumbnail load error: {e}")
    return None


def _thumb_field(kind):
    return "video_thumbnail_data" if kind == "video" else "media_thumbnail_data"


def load_saved_thumbnail(user_id, kind):
    """Return a saved thumbnail path for video or audio/document, or None."""
    field = _thumb_field(kind)
    local = f"{user_id}_{kind}_thumb.jpg"
    try:
        if os.path.isfile(local) and os.path.getsize(local) > 0:
            return local
        data = collection.find_one({"_id": user_id}, {field: 1})
        blob = data.get(field) if data else None
        if blob:
            with open(local, "wb") as fh:
                fh.write(bytes(blob))
            return local
    except Exception as e:
        print(f"Thumbnail load error ({kind}): {e}")
    return None


async def _normalize_thumbnail(src_path, user_id):
    """Normalize a thumbnail to Telegram's JPEG/size constraints."""
    if not src_path or not os.path.isfile(src_path):
        return None
    out = os.path.join(tempfile.gettempdir(), f"vjbot_norm_thumb_{user_id}_{int(time.time()*1000)}.jpg")
    try:
        img = cv2.imread(src_path, cv2.IMREAD_COLOR)
        if img is None:
            return None
        h, w = img.shape[:2]
        if not w or not h:
            return None
        scale = min(1.0, 320.0 / max(w, h))
        nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
        if (nw, nh) != (w, h):
            img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
        for quality in (90, 80, 70, 60, 50):
            ok, encoded = cv2.imencode('.jpg', img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
            if ok and len(encoded) < 190 * 1024:
                encoded.tofile(out)
                if os.path.isfile(out) and os.path.getsize(out) > 0:
                    return out
    except Exception as e:
        print(f"Thumbnail normalization error: {e}")
    try:
        if os.path.exists(out): os.remove(out)
    except OSError: pass
    return None


async def _download_source_video_thumbnail(userbot, msg, user_id):
    """Download the real Telegram source thumbnail from Pyrogram's Video/Document thumbs."""
    thumbs = getattr(getattr(msg, "video", None), "thumbs", None) or []
    if not thumbs:
        thumbs = getattr(getattr(msg, "document", None), "thumbs", None) or []
    if not thumbs:
        return None
    thumb = max(thumbs, key=lambda x: (getattr(x, "width", 0) or getattr(x, "w", 0) or 0) * (getattr(x, "height", 0) or getattr(x, "h", 0) or 0))
    file_id = getattr(thumb, "file_id", None)
    if not file_id:
        return None
    raw = os.path.join(tempfile.gettempdir(), f"vjbot_src_thumb_{user_id}_{int(time.time()*1000)}")
    downloaded = None
    try:
        downloaded = await run_transfer_with_watchdog(
            userbot.download_media(file_id, file_name=raw),
            idle_timeout=TRANSFER_IDLE_TIMEOUT,
            total_timeout=PRE_UPLOAD_TIMEOUT,
        )
        normalized = await _normalize_thumbnail(downloaded or raw, user_id)
        if normalized:
            if downloaded and os.path.abspath(downloaded) != os.path.abspath(normalized):
                try: os.remove(downloaded)
                except OSError: pass
            return normalized
    except asyncio.CancelledError:
        for p in (raw, downloaded):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        raise
    except Exception as e:
        print(f"Source thumbnail download error: {e}")
    for p in (raw, downloaded):
        try:
            if p and os.path.exists(p): os.remove(p)
        except OSError: pass
    return None


async def _generate_auto_video_thumbnail(video, duration, user_id, use_saved_thumbnail=True):
    """Generate a Telegram-friendly JPEG preview without requiring a user thumbnail."""
    proc = None
    out = None
    thumbnail_created = False
    try:
        # Prefer a user-saved video thumbnail when one exists.
        saved = load_saved_thumbnail(user_id, "video") if use_saved_thumbnail else None
        if saved and os.path.isfile(saved) and os.path.getsize(saved) > 0:
            return saved, False

        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            try:
                import imageio_ffmpeg
                ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                return None, False

        out = os.path.join(tempfile.gettempdir(),
                           f"vjbot_thumb_{user_id}_{int(time.time()*1000)}.jpg")
        # Pick the middle frame and keep both dimensions <= 320px.
        dur = max(0, int(duration or 0))
        seek = dur // 2
        cmd = [
            ffmpeg_bin, "-hide_banner", "-loglevel", "error",
            "-ss", str(seek), "-i", str(video),
            "-frames:v", "1",
            "-vf", "scale=if(gt(iw,ih),320,-2):if(gt(ih,iw),320,-2)",
            "-q:v", "5", "-y", out
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
        if proc.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0:
            thumbnail_created = True
            return out, True
        if stderr:
            print(f"Auto-thumbnail ffmpeg error: {stderr.decode(errors='ignore')[-500:]}")
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                await proc.communicate()
            except BaseException:
                pass
        raise
    except asyncio.TimeoutError:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                await proc.communicate()
            except BaseException:
                pass
        print("Auto-thumbnail error: ffmpeg timed out")
    except Exception as e:
        print(f"Auto-thumbnail error: {e}")
    finally:
        # A failed or cancelled probe must not leave ffmpeg's output around.
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                await proc.communicate()
            except BaseException:
                pass
        if out and not thumbnail_created:
            try:
                if os.path.exists(out):
                    os.remove(out)
            except OSError:
                pass
    return None, False


async def _prepare_video_upload(userbot, msg, file, user_id):
    """Finish bounded video preparation before the first upload request.

    Keep this as one stage so a retry always gets a fresh metadata probe and a
    fresh source-thumbnail task.  In particular, never call the synchronous
    OpenCV metadata reader from the event-loop thread for a large file.
    """
    source_thumb = None
    try:
        source_video = getattr(msg, "video", None)
        source_metadata = {
            "width": getattr(source_video, "width", 0) or 0,
            "height": getattr(source_video, "height", 0) or 0,
            "duration": getattr(source_video, "duration", 0) or 0,
        }
        metadata = await video_metadata_async(
            file,
            timeout=PRE_UPLOAD_TIMEOUT,
            fallback=source_metadata,
        )
        source_thumb = await _download_source_video_thumbnail(userbot, msg, user_id)
        return metadata, source_thumb
    except asyncio.CancelledError:
        # If cancellation lands after thumbnail download but before this
        # helper returns, _get_msg_once has not received the path yet.
        if source_thumb:
            try:
                if os.path.isfile(source_thumb):
                    os.remove(source_thumb)
            except OSError:
                pass
        raise


async def _transfer_with_retries(factory, edit, label, retries=None):
    last_error = None
    retries = TRANSFER_RETRIES if retries is None else retries
    for attempt in range(1, max(1, retries) + 1):
        try:
            return await run_transfer_with_watchdog(factory(), progress_message=edit)
        except FloodWait as fw:
            last_error = fw
            if attempt >= retries:
                raise
            await edit.edit(f"⏳ **Telegram rate limit**\n\nPausing for `{fw.value}` seconds...\n🔁 Retry `{attempt}/{retries - 1}`")
            await asyncio.sleep(fw.value)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            # A stalled transfer must immediately bubble up so get_msg() can
            # restart the ENTIRE item from the downloading stage. Do not spend
            # the internal upload retries on a dead transfer.
            raise
        except Exception as e:
            last_error = e
            if attempt >= retries:
                raise
            await asyncio.sleep(min(5 * attempt, 15))
            try:
                await edit.edit(f"🔄 **Retrying {label}...**\n\nAttempt `{attempt + 1}/{retries}`")
            except Exception:
                pass
    raise last_error or RuntimeError(f"{label} failed")


async def _copy_uploaded_message_to_targets(sent_message, targets):
    """Forward the bot's uploaded message to the configured destination.

    Uses copy_message first, then forward_messages, then a direct media resend
    for media types supported by the uploaded message. All methods still obey
    Telegram's destination permissions; this only handles API differences and
    transient copy failures.
    """
    if not sent_message:
        return []
    source_chat_id = getattr(getattr(sent_message, "chat", None), "id", None)
    source_message_id = getattr(sent_message, "id", None)
    if source_chat_id is None or source_message_id is None:
        return ["The uploaded message did not have a valid source ID."]

    errors = []
    for target in [x for x in (targets or []) if x is not None and x != source_chat_id]:
        last_error = None
        for attempt in range(3):
            try:
                await app.copy_message(
                    chat_id=target,
                    from_chat_id=source_chat_id,
                    message_id=source_message_id,
                )
                last_error = None
                break
            except FloodWait as e:
                wait_s = int(getattr(e, "value", 0) or 0)
                if wait_s > 0 and attempt < 2:
                    await asyncio.sleep(wait_s + 1)
                    continue
                last_error = f"rate limited for {wait_s} seconds"
                break
            except Exception as e:
                last_error = str(e)
                break

        if last_error is None:
            continue

        # Some Telegram/API combinations reject copy_message while allowing a
        # normal forward. Try that before reporting failure.
        try:
            await app.forward_messages(
                chat_id=target,
                from_chat_id=source_chat_id,
                message_ids=source_message_id,
            )
            continue
        except FloodWait as e:
            wait_s = int(getattr(e, "value", 0) or 0)
            if wait_s > 0:
                await asyncio.sleep(wait_s + 1)
                try:
                    await app.forward_messages(
                        chat_id=target,
                        from_chat_id=source_chat_id,
                        message_ids=source_message_id,
                    )
                    continue
                except Exception as e2:
                    last_error = str(e2)
        except Exception as e:
            last_error = str(e)

        # Final safe fallback: resend the already-uploaded media using its file_id.
        # This does not bypass Telegram permissions; it only avoids copy restrictions.
        try:
            caption = getattr(sent_message, "caption", None)
            media = getattr(sent_message, "media", None)
            if getattr(sent_message, "video", None):
                await app.send_video(target, sent_message.video.file_id, caption=caption)
                continue
            if getattr(sent_message, "document", None):
                await app.send_document(target, sent_message.document.file_id, caption=caption)
                continue
            if getattr(sent_message, "audio", None):
                await app.send_audio(target, sent_message.audio.file_id, caption=caption)
                continue
            if getattr(sent_message, "photo", None):
                await app.send_photo(target, sent_message.photo.file_id, caption=caption)
                continue
            if getattr(sent_message, "animation", None):
                await app.send_animation(target, sent_message.animation.file_id, caption=caption)
                continue
            if getattr(sent_message, "text", None):
                await app.send_message(target, sent_message.text)
                continue
        except Exception as e:
            last_error = str(e)

        errors.append(f"Forwarding to `{target}` failed: {str(last_error or 'unknown error')[:300]}")
    return errors


async def _forward_uploaded_safely(sent_message, targets, edit, label="file"):
    """Forward an already-uploaded item without allowing forwarding to freeze the batch.

    The primary upload is considered successful once Telegram accepts it. Forwarding
    is a secondary operation, so a stalled destination must not hold the per-file
    transfer task or force the file to be downloaded/uploaded again.
    """
    try:
        errors = await asyncio.wait_for(
            _copy_uploaded_message_to_targets(sent_message, targets),
            timeout=min(90, max(1, UPLOAD_TOTAL_TIMEOUT)),
        )
        if errors:
            try:
                await edit.edit(
                    f"⚠️ **{label.capitalize()} uploaded, but automatic forwarding had an issue.**\\n\\n"
                    + "\\n".join(errors)
                )
            except Exception:
                pass
        return errors
    except asyncio.TimeoutError:
        try:
            await edit.edit(
                f"⚠️ **{label.capitalize()} uploaded successfully.**\\n\\n"
                "Automatic forwarding timed out, so the batch is continuing with the next file."
            )
        except Exception:
            pass
        return [f"Forwarding timed out for {label}; upload was successful."]



async def _copy_to_log_group(sent_message, user_id, source_link=None, user_display=None):
    """Copy the delivered file to LOG_GROUP with compact user attribution.

    The file itself remains in the log channel, but the attribution is placed
    in the copied media caption so we do not need a second sendMessage log for
    every batch item. This materially reduces API traffic while preserving the
    owner's per-file audit trail.
    """
    if not sent_message or not LOG_GROUP:
        return None
    try:
        source_caption = getattr(sent_message, "caption", None) or ""
        who = user_display or str(user_id)
        header = f"👤 User: {who}\n🆔 `{user_id}`"
        if source_link:
            header += f"\n🔗 `{source_link}`"
        caption = f"{header}\n\n{source_caption}" if source_caption else header
        # Telegram media captions are limited; preserve the beginning of the
        # original caption after the audit header.
        caption = caption[:1024]
        return await app.copy_message(
            chat_id=LOG_GROUP,
            from_chat_id=sent_message.chat.id,
            message_id=sent_message.id,
            caption=caption,
            parse_mode=None,
        )
    except Exception as exc:
        print(f"Log file copy error for {user_id}: {exc}")
        return None


async def _safe_post_upload(action, label="post-upload", timeout=None):
    """Run non-essential post-upload work with a short hard timeout.

    Once Telegram has accepted the media, logging/forwarding/pinning must never
    keep the batch parked in the UPLOADING state. A timeout here is intentionally
    treated as a secondary failure; the uploaded file remains a success.
    """
    try:
        return await asyncio.wait_for(action(), timeout=(POST_UPLOAD_TIMEOUT if timeout is None else timeout))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"{label} timed out/failed: {exc}")
        return None


async def upload_video_resilient(target_chat_id, file, caption, edit, user_id, width, height, duration, source_thumb=None, upload_chat_id=None, use_saved_thumbnail=True):
    """Upload to the requester (or explicit chat), then copy to saved destinations.

    ``user_id`` always identifies whose saved thumbnail/settings are used.
    ``upload_chat_id`` lets Auto Update publish directly to its pair target
    without confusing that target ID with the owner's customization identity.
    """
    forward_targets = target_chat_id if isinstance(target_chat_id, (list, tuple, set)) else ([target_chat_id] if target_chat_id is not None else [])
    errors=[]; first_sent=None; auto_thumb=None
    saved_thumb=load_saved_thumbnail(user_id,"video") if use_saved_thumbnail else None
    if saved_thumb and os.path.isfile(saved_thumb) and os.path.getsize(saved_thumb)>0:
        normalized=await _normalize_thumbnail(saved_thumb,user_id); thumb=normalized or saved_thumb; is_temp=bool(normalized)
    elif source_thumb and os.path.isfile(source_thumb) and os.path.getsize(source_thumb)>0:
        thumb=source_thumb; is_temp=True
    else:
        thumb,is_temp=await _generate_auto_video_thumbnail(file,duration,user_id,use_saved_thumbnail=use_saved_thumbnail)
    auto_thumb=thumb if is_temp else None
    try:
        base=dict(chat_id=(upload_chat_id if upload_chat_id is not None else user_id),video=file,supports_streaming=True,progress=progress_bar,progress_args=("**UPLOADING:**\n",edit,time.time()),**_caption_send_kwargs(caption))
        if width and int(width) > 0:
            base["width"] = int(width)
        if height and int(height) > 0:
            base["height"] = int(height)
        if duration and int(duration) > 0:
            base["duration"] = int(duration)
        sent=None
        if thumb and os.path.isfile(thumb):
            try:
                kw=dict(base); kw["thumb"]=thumb
                sent=await _transfer_with_retries(lambda kw=kw: app.send_video(**kw),edit,"video with thumbnail",retries=3)
            except asyncio.CancelledError: raise
            except asyncio.TimeoutError:
                raise
            except Exception as e: errors.append(f"thumbnail upload: {e}")
        if sent is None:
            try: sent=await _transfer_with_retries(lambda base=base: app.send_video(**base),edit,"video upload",retries=3)
            except asyncio.CancelledError: raise
            except asyncio.TimeoutError:
                raise
            except Exception as e: errors.append(f"video upload: {e}")
        if sent is None:
            try:
                fallback_chat_id = upload_chat_id if upload_chat_id is not None else user_id
                sent=await _transfer_with_retries(lambda: app.send_document(chat_id=fallback_chat_id,document=file,progress=progress_bar,progress_args=("**UPLOADING FILE:**\n",edit,time.time()),**_caption_send_kwargs(caption)),edit,"document fallback",retries=2)
            except asyncio.CancelledError: raise
            except asyncio.TimeoutError:
                raise
            except Exception as e: errors.append(f"document fallback: {e}")
        if sent is not None:
            first_sent=sent
            forward_errors = await _safe_post_upload(
                lambda: _copy_uploaded_message_to_targets(sent, forward_targets),
                "video forwarding", FORWARD_TIMEOUT,
            )
            if forward_errors:
                try:
                    await edit.edit("⚠️ **File uploaded successfully, but automatic forwarding failed.**\n\n" + "\n".join(forward_errors))
                except Exception:
                    pass
        if first_sent is None: raise RuntimeError("; ".join(errors[-6:]) or "Upload failed")
        return first_sent
    finally:
        if auto_thumb:
            try: os.remove(auto_thumb)
            except OSError: pass


async def upload_audio_resilient(target_chat_id, file, caption, edit, user_id, duration=0, performer=None, title=None, upload_chat_id=None):
    """Upload to the requester (or explicit chat), then copy to saved destinations."""
    forward_targets=target_chat_id if isinstance(target_chat_id,(list,tuple,set)) else ([target_chat_id] if target_chat_id is not None else [])
    errors=[]; thumb=load_saved_thumbnail(user_id,"media")
    base=dict(chat_id=(upload_chat_id if upload_chat_id is not None else user_id),audio=file,duration=max(0,int(duration or 0)),performer=performer,title=title,progress=progress_bar,progress_args=("**UPLOADING:**\n",edit,time.time()),**_caption_send_kwargs(caption))
    base={k:v for k,v in base.items() if v is not None}
    sent=None
    try: sent=await _transfer_with_retries(lambda base=base: app.send_audio(**base),edit,"audio upload",retries=3)
    except asyncio.CancelledError: raise
    except asyncio.TimeoutError:
        raise
    except Exception as e: errors.append(f"audio: {e}")
    if sent is None and thumb and os.path.isfile(thumb):
        try:
            kw=dict(base); kw["thumb"]=thumb
            sent=await _transfer_with_retries(lambda kw=kw: app.send_audio(**kw),edit,"audio with thumbnail",retries=2)
        except asyncio.CancelledError: raise
        except asyncio.TimeoutError:
            raise
        except Exception as e: errors.append(f"audio thumb: {e}")
    if sent is None: raise RuntimeError("; ".join(errors[-6:]) or "Audio upload failed")
    forward_errors = await _safe_post_upload(
        lambda: _copy_uploaded_message_to_targets(sent, forward_targets),
        "audio forwarding", FORWARD_TIMEOUT,
    )
    if forward_errors:
        try:
            await edit.edit("⚠️ **Audio uploaded successfully, but automatic forwarding failed.**\n\n" + "\n".join(forward_errors))
        except Exception:
            pass
    return sent


async def upload_document_resilient(target_chat_id, file, caption, edit, user_id, upload_chat_id=None):
    """Upload to the requester (or explicit chat), then copy to saved destinations."""
    forward_targets=target_chat_id if isinstance(target_chat_id,(list,tuple,set)) else ([target_chat_id] if target_chat_id is not None else [])
    errors=[]; thumb=load_saved_thumbnail(user_id,"media")
    base=dict(chat_id=(upload_chat_id if upload_chat_id is not None else user_id),document=file,progress=progress_bar,progress_args=("**UPLOADING:**\n",edit,time.time()),**_caption_send_kwargs(caption))
    sent=None
    if thumb and os.path.isfile(thumb):
        try:
            kw=dict(base); kw["thumb"]=thumb
            sent=await _transfer_with_retries(lambda kw=kw: app.send_document(**kw),edit,"document with thumbnail",retries=2)
        except asyncio.CancelledError: raise
        except asyncio.TimeoutError:
            raise
        except Exception as e: errors.append(f"thumb: {e}")
    if sent is None:
        try: sent=await _transfer_with_retries(lambda base=base: app.send_document(**base),edit,"document upload",retries=3)
        except asyncio.CancelledError: raise
        except asyncio.TimeoutError:
            raise
        except Exception as e: errors.append(f"document: {e}")
    if sent is None: raise RuntimeError("; ".join(errors[-6:]) or "Document upload failed")
    forward_errors = await _safe_post_upload(
        lambda: _copy_uploaded_message_to_targets(sent, forward_targets),
        "document forwarding", FORWARD_TIMEOUT,
    )
    if forward_errors:
        try:
            await edit.edit("⚠️ **Document uploaded successfully, but automatic forwarding failed.**\n\n" + "\n".join(forward_errors))
        except Exception:
            pass
    return sent


async def upload_photo_resilient(target_chat_id, file, caption, edit, user_id, upload_chat_id=None):
    """Upload an image as a photo, falling back to a document safely.

    Telegram validates the local filename extension for ``send_photo``.  The
    staging copy below only fixes the name; it never converts the image or any
    other media.  If Telegram still rejects the image as a photo, the exact
    bytes are sent as a document instead of being skipped.
    """
    forward_targets = target_chat_id if isinstance(target_chat_id, (list, tuple, set)) else (
        [target_chat_id] if target_chat_id is not None else []
    )
    upload_path, temporary = _photo_upload_path(file)
    try:
        try:
            sent = await _transfer_with_retries(
                lambda: app.send_photo(chat_id=(upload_chat_id if upload_chat_id is not None else user_id), photo=upload_path, **_caption_send_kwargs(caption)),
                edit,
                "photo upload",
                retries=2,
            )
            await _forward_uploaded_safely(sent, forward_targets, edit, "photo")
            return sent
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            raise
        except FloodWait:
            raise
        except Exception as photo_error:
            # A valid image can still be unsupported by Telegram's photo
            # endpoint (for example some WEBP/GIF variants).  Keep the bytes
            # intact and use the normal document uploader as the fallback.
            try:
                await edit.edit("ℹ️ **Photo format is not accepted as a photo.**\n\nSending it as a document...")
            except Exception:
                pass
            try:
                return await upload_document_resilient(
                    forward_targets, upload_path, caption, edit, user_id,
                    upload_chat_id=upload_chat_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as document_error:
                raise RuntimeError(
                    f"Photo upload failed: {photo_error}; document fallback failed: {document_error}"
                ) from document_error
    finally:
        if temporary:
            try:
                os.remove(upload_path)
            except OSError:
                pass


class TransferRetryRequired(Exception):
    """Media transfer failed or stalled; retry the complete item from download stage."""


async def _fetch_source_message(userbot, chat, msg_id):
    """Fetch one source message without allowing a stuck RPC to park a batch.

    ``get_messages`` is an independent network stage from media transfer.  The
    old pipeline awaited it without a timeout, so a Pyrogram connection that
    stopped completing an RPC left the batch on its initial "Checking" text
    forever.  ``wait_for`` cancels and awaits the RPC coroutine before the item
    is retried/skipped by ``get_msg``.
    """
    try:
        return await asyncio.wait_for(
            userbot.get_messages(chat, msg_id),
            timeout=MESSAGE_FETCH_TIMEOUT,
        )
    except asyncio.TimeoutError as exc:
        raise TransferRetryRequired(
            f"Telegram message lookup timed out after {MESSAGE_FETCH_TIMEOUT} seconds."
        ) from exc


async def _get_msg_once(userbot, sender, edit_id, msg_link, i, message, batch_position=None, batch_total=None):
    edit = ""
    chat = ""
    round_message = False
    media_pipeline_started = False
    if "?single" in msg_link:
        msg_link = msg_link.split("?single")[0]
    msg_id = int(msg_link.split("/")[-1]) + int(i)

    
    if 't.me/' in msg_link or 'telegram.me/' in msg_link:
        try:
            clean_link = msg_link.split('?', 1)[0].split('#', 1)[0]
            parsed = urlparse(clean_link if '://' in clean_link else 'https://' + clean_link)
            path = [p for p in parsed.path.split('/') if p]
            if len(path) < 2:
                raise ValueError('Invalid Telegram link')
            msg_id = int(path[-1]) + int(i)
            kind = path[0].lower()
            if kind == 'c':
                if len(path) < 3:
                    raise ValueError('Invalid /c/ link')
                chat = int('-100' + str(path[1]))
            elif kind == 'b':
                if len(path) < 3:
                    raise ValueError('Invalid /b/ link')
                chat = path[1]
            elif kind == 's':
                if len(path) < 3:
                    raise ValueError('Invalid /s/ link')
                chat = path[1]
            else:
                chat = path[0]
        except (ValueError, IndexError):
            await app.edit_message_text(sender, edit_id, "❌ **Invalid Telegram message link.**\n\nPlease check the chat and message ID.")
            return False
        file = ""
        source_thumb = None
        try:
            chatx = message.chat.id
            # Do not resolve the forwarding destination before every download.
            # That repeated app.get_chat() RPC was unrelated to source-message
            # retrieval and could park the batch before the download stage after
            # enough sequential files.  Forwarding still performs its real
            # permission/API operation after upload, with bounded post-upload
            # handling.
            msg = await _fetch_source_message(userbot, chat, msg_id)
            caption = None

            if msg.service is not None:
                return None 
            if msg.empty is not None:
                return None                          
            media_kind = message_media_kind(msg)
            if msg.media and media_kind is None:
                if msg.media == MessageMediaType.WEB_PAGE:
                    target_chat_ids = get_target_chat_ids(chatx)
                    edit = await app.edit_message_text(sender, edit_id, "🔄 **Preparing message...**")
                    safe_repo = None
                    for _target in target_chat_ids:
                        safe_repo = await app.send_message(_target, msg.text.markdown)
                    if msg.pinned_message:
                        try:
                            await safe_repo.pin(both_sides=True)
                        except Exception as e:
                            await safe_repo.pin()
                    await _copy_to_log_group(safe_repo, sender, msg_link, user_label(message.from_user))                  
                    await edit.delete()
                    return True
            if media_kind is None and not msg.media:
                if msg.text:
                    target_chat_ids = get_target_chat_ids(chatx)
                    edit = await app.edit_message_text(sender, edit_id, "🔄 **Preparing message...**")
                    safe_repo = None
                    for _target in target_chat_ids:
                        safe_repo = await app.send_message(_target, msg.text.markdown)
                    if msg.pinned_message:
                        try:
                            await safe_repo.pin(both_sides=True)
                        except Exception as e:
                            await safe_repo.pin()
                    await _copy_to_log_group(safe_repo, sender, msg_link, user_label(message.from_user))
                    await edit.delete()
                    return True
            
            media_pipeline_started = True
            edit = await app.edit_message_text(sender, edit_id, "╭━━━〔 📥 𝗗𝗢𝗪𝗡𝗟𝗢𝗔𝗗𝗜𝗡𝗚 〕━━━╮\n\n🔄 Preparing your file...\n╰━━━━━━━━━━━━━━━━━━━━╯")
            file = await download_media_resilient(
                userbot, msg,
                progress=progress_bar,
                progress_args=((f"**__Downloading file {batch_position}/{batch_total}: __**\n" if batch_position and batch_total else "**__Downloading: __**\n"), edit, time.time()),
                progress_message=edit,
                user_id=sender,
                retries=1,
            )
            if not file or not os.path.exists(file):
                raise RuntimeError("Telegram download returned no local file")

            custom_rename_tag = get_user_rename_preference(chatx)

            # The resilient downloader uses extension-less temporary filenames.
            # For documents, restore the original Telegram filename/extension so
            # PDFs remain PDFs (and other document types keep their real format).
            source_name = None
            if getattr(msg, "document", None):
                source_name = getattr(msg.document, "file_name", None)
            elif getattr(msg, "audio", None):
                source_name = getattr(msg.audio, "file_name", None)
            elif getattr(msg, "video", None):
                source_name = getattr(msg.video, "file_name", None)

            if source_name and os.path.splitext(str(source_name))[1]:
                source_name = os.path.basename(str(source_name))
                original_file_name, file_extension = os.path.splitext(source_name)
                file_extension = file_extension.lstrip(".")
            else:
                last_dot_index = str(file).rfind('.')
                if last_dot_index != -1 and last_dot_index != 0:
                    safe_repo_ext = str(file)[last_dot_index + 1:]
                    if safe_repo_ext.isalpha() and len(safe_repo_ext) <= 5:
                        original_file_name = str(file)[:last_dot_index]
                        file_extension = safe_repo_ext
                    else:
                        original_file_name = str(file)
                        file_extension = 'mp4' if media_kind == "video" else (
                            detect_image_extension(file) if media_kind == "photo" else "bin"
                        ) or 'bin'
                else:
                    original_file_name = str(file)
                    file_extension = 'mp4' if media_kind == "video" else (
                        detect_image_extension(file) if media_kind == "photo" else "bin"
                    ) or 'bin'

            delete_words = load_delete_words(chatx)
            for word in delete_words:
                original_file_name = original_file_name.replace(word, "")
            video_file_name = original_file_name + " " + custom_rename_tag    
            new_file_name = original_file_name + " " + custom_rename_tag + "." + file_extension
            if os.path.abspath(str(file)) != os.path.abspath(new_file_name):
                if os.path.exists(new_file_name):
                    base, ext = os.path.splitext(new_file_name)
                    new_file_name = f"{base}_{int(time.time() * 1000)}{ext}"
                os.rename(file, new_file_name)
                file = new_file_name

            # CODES are hidden             

            upload_label = f"file {batch_position}/{batch_total}" if batch_position and batch_total else "your file"
            await edit.edit(f'╭━━━〔 📤 𝗨𝗣𝗟𝗢𝗔𝗗𝗜𝗡𝗚 〕━━━╮\n\n🚀 Sending {upload_label} to Telegram...\n╰━━━━━━━━━━━━━━━━━━━━╯')

            custom_caption = get_user_caption_preference(sender)
            original_caption = msg.caption if msg.caption else ''
            delete_words = load_delete_words(sender)
            final_caption = original_caption
            for word in delete_words:
                final_caption = final_caption.replace(word, '')
            replacements = load_replacement_words(sender)
            for word, replace_word in replacements.items():
                final_caption = final_caption.replace(word, replace_word)
            caption = f"{final_caption}\n\n__**{custom_caption}**__" if custom_caption else final_caption
            caption = with_media_footer(
                caption,
                include_footer=(await get_active_plan(sender) != "elite"),
            )

            if media_kind == "video":
                # OpenCV's synchronous VideoCapture can block the event loop
                # while probing a large container.  Keep metadata probing and
                # source-thumbnail preparation behind a cancellable, bounded
                # stage so the upload watchdog can either start or restart
                # this item cleanly.
                metadata, source_thumb = await asyncio.wait_for(
                    _prepare_video_upload(userbot, msg, file, chatx),
                    timeout=PRE_UPLOAD_TIMEOUT,
                )
                width = metadata.get('width') or 0
                height = metadata.get('height') or 0
                duration = metadata.get('duration') or 0
                target_chat_ids = get_target_chat_ids(chatx)
                try:
                    safe_repo = await upload_video_resilient(
                        target_chat_ids, file, caption, edit, chatx, width, height, duration, source_thumb=source_thumb
                    )
                except FloodWait:
                    raise
                except Exception as e:
                    try:
                        await edit.edit("⚠️ **Video upload failed.**\n\n🔄 Retrying the complete file from downloading...")
                    except Exception:
                        pass
                    try: os.remove(file)
                    except OSError: pass
                    if source_thumb:
                        try: os.remove(source_thumb)
                        except OSError: pass
                    raise TransferRetryRequired(f"Video upload failed: {e}") from e

                if msg.pinned_message:
                    await _safe_post_upload(lambda: safe_repo.pin(both_sides=True), "video pin")
                await _safe_post_upload(lambda: _copy_to_log_group(safe_repo, sender, msg_link, user_label(message.from_user)), "video log copy")
                if not (batch_position and batch_total):
                    try: await edit.delete()
                    except Exception: pass
                try: os.remove(file)
                except OSError: pass
                if source_thumb:
                    try: os.remove(source_thumb)
                    except OSError: pass
                return True

            elif media_kind == "photo":
                await edit.edit("╭━━━〔 🖼️ 𝗨𝗣𝗟𝗢𝗔𝗗𝗜𝗡𝗚 〕━━━╮\n\n📤 Sending photo...\n╰━━━━━━━━━━━━━━━━━━━━╯")
                delete_words = load_delete_words(sender)
                custom_caption = get_user_caption_preference(sender)
                original_caption = msg.caption if msg.caption else ''
                final_caption = f"{original_caption}" if custom_caption else f"{original_caption}"
                lines = final_caption.split('\n')
                processed_lines = []
                for line in lines:
                    for word in delete_words:
                        line = line.replace(word, '')
                    if line.strip():
                        processed_lines.append(line.strip())
                final_caption = '\n'.join(processed_lines)
                replacements = load_replacement_words(sender)
                for word, replace_word in replacements.items():
                    final_caption = final_caption.replace(word, replace_word)
                caption = f"{final_caption}\n\n__**{custom_caption}**__" if custom_caption else f"{final_caption}"
                caption = with_media_footer(
                    caption,
                    include_footer=(await get_active_plan(sender) != "elite"),
                )

                target_chat_ids = get_target_chat_ids(sender)
                try:
                    safe_repo = await upload_photo_resilient(
                        target_chat_ids, file, caption, edit, sender
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    try: os.remove(file)
                    except OSError: pass
                    raise TransferRetryRequired(f"Photo upload failed: {e}") from e
                if msg.pinned_message:
                    await _safe_post_upload(lambda: safe_repo.pin(both_sides=True), "photo pin")
                await _safe_post_upload(lambda: _copy_to_log_group(safe_repo, sender, msg_link, user_label(message.from_user)), "photo log copy")
                if not (batch_position and batch_total):
                    try:
                        await edit.delete()
                    except Exception:
                        pass
                try:
                    os.remove(file)
                except OSError:
                    pass
                return True
            else:
                target_chat_ids = get_target_chat_ids(chatx)
                try:
                    if getattr(msg, "audio", None):
                        duration = int(getattr(msg.audio, "duration", 0) or 0)
                        performer = getattr(msg.audio, "performer", None)
                        title = getattr(msg.audio, "title", None)
                        safe_repo = await upload_audio_resilient(target_chat_ids, file, caption, edit, chatx, duration, performer, title)
                    else:
                        safe_repo = await upload_document_resilient(target_chat_ids, file, caption, edit, chatx)
                    if msg.pinned_message:
                        await _safe_post_upload(lambda: safe_repo.pin(both_sides=True), "document pin")
                    await _safe_post_upload(lambda: _copy_to_log_group(safe_repo, sender, msg_link, user_label(message.from_user)), "document log copy")
                    if not (batch_position and batch_total):
                        try: await edit.delete()
                        except Exception: pass
                    try: os.remove(file)
                    except OSError: pass
                    return True
                except FloodWait:
                    raise
                except Exception as e:
                    try:
                        await edit.edit("⚠️ **Upload failed.**\n\n🔄 Retrying the complete file from downloading...")
                    except Exception:
                        pass
                    try: os.remove(file)
                    except OSError: pass
                    raise TransferRetryRequired(f"Upload failed: {e}") from e

            await edit.delete()
            return True
        
        except (ChannelBanned, ChannelInvalid, ChannelPrivate, ChatIdInvalid, ChatInvalid):
            if userbot is app:
                error_text = "❌ Could not access this public Telegram message. The bot may not be permitted to view it, or the message may be protected."
            else:
                error_text = "❌ Could not access the source channel/message. Make sure the account you logged in with is a member of that private channel/group, then /login again and retry."
            await app.edit_message_text(sender, edit_id, error_text)
            return False
        except TransferRetryRequired:
            raise
        except asyncio.TimeoutError as e:
            if media_pipeline_started:
                raise TransferRetryRequired(str(e) or "Transfer timed out") from e
            print(f"Failed to save {msg_link} for {sender}: {e}")
            await app.edit_message_text(sender, edit_id, f'❌ **Could not process this Telegram message.**\n\nThe technical error has been recorded in the server logs.')
            return False
        except Exception as e:
            if media_pipeline_started:
                raise TransferRetryRequired(str(e)) from e
            print(f"Failed to save {msg_link} for {sender}: {e}")
            await app.edit_message_text(sender, edit_id, f'❌ **Could not process this Telegram message.**\n\nThe technical error has been recorded in the server logs.')
            return False
        finally:
            # A cancelled/timed-out attempt must never leave a partial file behind.
            # This is especially important before the complete-file retry.
            for _path in (file, source_thumb):
                try:
                    if _path and os.path.isfile(_path):
                        os.remove(_path)
                except OSError:
                    pass
        

async def get_msg(userbot, sender, edit_id, msg_link, i, message, batch_position=None, batch_total=None):
    """Process one item with exactly one full retry from the download stage."""
    for attempt in range(1, 3):
        try:
            return await asyncio.wait_for(
                _get_msg_once(
                    userbot, sender, edit_id, msg_link, i, message,
                    batch_position=batch_position, batch_total=batch_total,
                ),
                timeout=FILE_TASK_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except FloodWait:
            raise
        except (TransferRetryRequired, asyncio.TimeoutError) as e:
            if attempt == 1:
                try:
                    label = f"file `{batch_position}/{batch_total}`" if batch_position and batch_total else "this file"
                    await app.edit_message_text(
                        sender, edit_id,
                        f"🔄 **Transfer stalled/failed.**\n\nRetrying {label} from the downloading stage...\n\nAttempt `2/2`."
                    )
                except Exception:
                    pass
                await asyncio.sleep(2)
                continue
            try:
                label = f"File `{batch_position}/{batch_total}`" if batch_position and batch_total else "File"
                await app.edit_message_text(
                    sender, edit_id,
                    f"⏭️ **{label} skipped.**\n\nDownload/upload failed again after one complete retry.\n"
                    "The technical error has been recorded in the server logs."
                )
            except Exception:
                pass
            print(f"Transfer failed for {sender} ({msg_link}): {e}")
            return False
    return False


async def copy_message_with_chat_id(client, sender, chat_id, message_id):
    # Get the user's set chat ID, if available; otherwise, use the original sender ID
    target_chat_ids = get_target_chat_ids(sender)
    
    try:
        # Fetch the message using get_message
        msg = await asyncio.wait_for(
            client.get_messages(chat_id, message_id),
            timeout=MESSAGE_FETCH_TIMEOUT,
        )
        
        # Modify the caption based on user's custom caption preference
        custom_caption = get_user_caption_preference(sender)
        original_caption = msg.caption if msg.caption else ''
        final_caption = f"{original_caption}" if custom_caption else f"{original_caption}"
        
        delete_words = load_delete_words(sender)
        for word in delete_words:
            final_caption = final_caption.replace(word, '  ')
        
        replacements = load_replacement_words(sender)
        for word, replace_word in replacements.items():
            final_caption = final_caption.replace(word, replace_word)
        
        caption = f"{final_caption}\n\n__**{custom_caption}**__" if custom_caption else f"{final_caption}"
        caption = with_media_footer(
            caption,
            include_footer=(await get_active_plan(sender) != "elite"),
        )
        
        if msg.media:
            if msg.media == MessageMediaType.VIDEO:
                result = None
                for _target in target_chat_ids:
                    result = await client.send_video(_target, msg.video.file_id, caption=caption)
            elif msg.media == MessageMediaType.DOCUMENT:
                result = None
                for _target in target_chat_ids:
                    result = await client.send_document(_target, msg.document.file_id, caption=caption)
            elif msg.media == MessageMediaType.PHOTO:
                result = None
                for _target in target_chat_ids:
                    result = await client.send_photo(_target, msg.photo.file_id, caption=caption)
            else:
                # Use copy_message for any other media types
                result = None
            for _target in target_chat_ids:
                result = await client.copy_message(_target, chat_id, message_id)
        else:
            # Use copy_message if there is no media
            result = None
            for _target in target_chat_ids:
                result = await client.copy_message(_target, chat_id, message_id)

        # Attempt to copy the result to the LOG_GROUP
        try:
            await _copy_to_log_group(result, sender, None, None)
        except Exception:
            pass
            
        if msg.pinned_message:
            try:
                await result.pin(both_sides=True)
            except Exception as e:
                await result.pin()
        return True

    except Exception as e:
        error_message = f"Error occurred while sending message to chat IDs {target_chat_ids}: {str(e)}"
        await client.send_message(sender, error_message)
        await client.send_message(sender, f"Make Bot admin in your Channel(s) - {target_chat_ids} and restart the process after /cancel")
        return False

# -------------- FFMPEG CODES ---------------

# ------------------------ Button Mode Editz FOR SETTINGS ----------------------------

# MongoDB database name and collection name
DB_NAME = MONGO_DATABASE_NAME
COLLECTION_NAME = "super_user"

# Establish a connection to MongoDB
mongo_client = pymongo.MongoClient(MONGODB_CONNECTION_STRING)
db = mongo_client[DB_NAME]
collection = db[COLLECTION_NAME]

def load_authorized_users():
    """
    Load authorized user IDs from the MongoDB collection
    """
    authorized_users = set()
    for user_doc in collection.find():
        if "user_id" in user_doc:
            authorized_users.add(user_doc["user_id"])
    return authorized_users

def save_authorized_users(authorized_users):
    """
    Save authorized user IDs to the MongoDB collection
    """
    collection.delete_many({})
    for user_id in authorized_users:
        collection.insert_one({"user_id": user_id})

SUPER_USERS = load_authorized_users()

# Saved forwarding destinations and the currently selected destination are kept
# separately. Reset Customisation must never erase this independent setting.
user_forward_destinations = {}
user_chat_ids = {}  # active destination only, retained for existing upload callers
try:
    for _doc in collection.find({"$or": [
        {"forward_destinations": {"$exists": True}},
        {"chat_ids": {"$exists": True}},
        {"chat_id": {"$exists": True}},
    ]}):
        raw_destinations = _doc.get("forward_destinations") or []
        destinations = []
        for item in raw_destinations:
            if isinstance(item, dict) and item.get("chat_id") is not None:
                try:
                    cid = int(item["chat_id"])
                    destinations.append({"chat_id": cid, "title": str(item.get("title") or cid)[:100]})
                except (TypeError, ValueError):
                    continue
        if not destinations:
            legacy = _doc.get("chat_ids")
            if not legacy:
                legacy = [_doc.get("chat_id")] if _doc.get("chat_id") is not None else []
            if isinstance(legacy, (int, str)):
                legacy = [legacy]
            for value in legacy or []:
                try:
                    cid = int(value)
                    if cid and all(x["chat_id"] != cid for x in destinations):
                        destinations.append({"chat_id": cid, "title": str(cid)})
                except (TypeError, ValueError):
                    continue
        if destinations:
            uid = _doc["_id"]
            user_forward_destinations[uid] = destinations
            selected = _doc.get("active_forward_chat_id", _doc.get("chat_id"))
            try:
                selected = int(selected) if selected is not None else None
            except (TypeError, ValueError):
                selected = None
            if selected is None and "active_forward_chat_id" not in _doc and _doc.get("chat_id") is None:
                selected = destinations[0]["chat_id"]
            if selected and any(x["chat_id"] == selected for x in destinations):
                user_chat_ids[uid] = [selected]
except Exception as e:
    print(f"Chat ID preference load error: {e}")

def _parse_chat_id_list(raw_text):
    """Parse one Telegram destination ID."""
    raw = str(raw_text or "").strip().split()[0] if str(raw_text or "").strip() else ""
    if not raw:
        return []
    try:
        return [int(raw)]
    except (TypeError, ValueError):
        return []

def _resolve_forward_targets(user_id=None):
    """Resolve per-user forwarding destinations; fall back to the requester."""
    if user_id is not None:
        targets = list(user_chat_ids.get(user_id, []))
        if targets:
            return targets
    return [user_id] if user_id is not None else []

def get_target_chat_ids(user_id):
    return _resolve_forward_targets(user_id)

async def validate_destination_chats(user_id):
    """Resolve saved destinations before processing a file.

    Permission checks are deliberately non-blocking here because Telegram can
    return stale/incomplete administrator data for channels. The actual forward
    operation performs the real permission check.
    """
    targets = get_target_chat_ids(user_id)
    if not user_chat_ids.get(user_id):
        return targets
    for target in targets:
        try:
            await asyncio.wait_for(
                app.get_chat(target),
                timeout=MESSAGE_FETCH_TIMEOUT,
            )
        except Exception as e:
            raise RuntimeError(f"Forward destination cannot be resolved: {target} — {str(e)[:180]}")
    return targets

# MongoDB database name and collection name
MDB_NAME = MONGO_DATABASE_NAME
MCOLLECTION_NAME = "stringsession"

# Establish a connection to MongoDB
m_client = pymongo.MongoClient(MONGODB_CONNECTION_STRING)
mdb = m_client[MDB_NAME]
mcollection = mdb[MCOLLECTION_NAME]

def load_delete_words(user_id):
    """
    Load delete words for a specific user from MongoDB
    """
    try:
        words_data = collection.find_one({"_id": user_id})
        if words_data:
            return set(words_data.get("delete_words", []))
        else:
            return set()
    except Exception as e:
        print(f"Error loading delete words: {e}")
        return set()

def save_delete_words(user_id, delete_words):
    """
    Save delete words for a specific user to MongoDB
    """
    try:
        collection.update_one(
            {"_id": user_id},
            {"$set": {"delete_words": list(delete_words)}},
            upsert=True
        )
    except Exception as e:
        print(f"Error saving delete words: {e}")

def load_replacement_words(user_id):
    try:
        words_data = collection.find_one({"_id": user_id})
        if words_data:
            return words_data.get("replacement_words", {})
        else:
            return {}
    except Exception as e:
        print(f"Error loading replacement words: {e}")
        return {}

def save_replacement_words(user_id, replacements):
    try:
        collection.update_one(
            {"_id": user_id},
            {"$set": {"replacement_words": replacements}},
            upsert=True
        )
    except Exception as e:
        print(f"Error saving replacement words: {e}")

# Initialize the dictionary to store user preferences for renaming
user_rename_preferences = {}

# Initialize the dictionary to store user caption
user_caption_preferences = {}

# Function to load user session from MongoDB
def load_user_session(sender_id):
    user_data = collection.find_one({"user_id": sender_id})
    if user_data:
        return user_data.get("session")
    else:
        return None  # Or handle accordingly if session doesn't exist

# Function to handle the /setrename command
async def set_rename_command(user_id, custom_rename_tag):
    user_rename_preferences[str(user_id)] = custom_rename_tag
    collection.update_one({"_id": user_id}, {"$set": {"rename_tag": custom_rename_tag}}, upsert=True)

def get_user_rename_preference(user_id):
    key = str(user_id)
    if key in user_rename_preferences:
        return user_rename_preferences[key]
    try:
        data = collection.find_one({"_id": user_id}, {"rename_tag": 1})
        value = data.get("rename_tag") if data else None
        if value is not None:
            user_rename_preferences[key] = value
            return value
    except Exception as e:
        print(f"Rename preference load error: {e}")
    return 'safe_repo'

async def set_caption_command(user_id, custom_caption):
    user_caption_preferences[str(user_id)] = custom_caption
    collection.update_one({"_id": user_id}, {"$set": {"caption": custom_caption}}, upsert=True)

def get_user_caption_preference(user_id):
    key = str(user_id)
    if key in user_caption_preferences:
        return user_caption_preferences[key]
    try:
        data = collection.find_one({"_id": user_id}, {"caption": 1})
        value = data.get("caption") if data else None
        if value is not None:
            user_caption_preferences[key] = value
            return value
    except Exception as e:
        print(f"Caption preference load error: {e}")
    return ''

# Initialize the dictionary to store user sessions

sessions = {}

SET_PIC = "customisations.jpg"
MESS = "╭━━━〔 🎨 𝗖𝗨𝗦𝗧𝗢𝗠𝗜𝗦𝗔𝗧𝗜𝗢𝗡𝗦 〕━━━╮\n\n🛠️ Personalize how your saved files are renamed, captioned and uploaded.\n\n💡 Changes are saved automatically.\n╰━━━━━━━━━━━━━━━━━━━━╯"

@gf.on(events.NewMessage(incoming=True, pattern='/customisations'))
async def customisations_command(event):
    user_id = event.sender_id
    buttons = [
        [Button.inline("🏷️ Rename Tag", b'setrename')],
        [Button.inline("📝 Caption", b'setcaption'), Button.inline("🔤 Replace Words", b'setreplacement')],
        [Button.inline("🗑️ Remove Words", b'delete'), Button.inline("📋 My Replacements", b'listreplace')],
        [Button.inline("🎬 Video Thumbnail", b'videothumb'), Button.inline("🎵📄 Media Thumbnail", b'mediathumb')],
        [Button.inline("👀 Video Preview", b'videopreview'), Button.inline("👀 Media Preview", b'mediapreview')],
        [Button.inline("🗑️ Delete Video Thumb", b'delvideothumb'), Button.inline("🗑️ Delete Media Thumb", b'delmediathumb')],
        [Button.inline("🆔 Forward Chat ID", b'setforwardchat')],
        [Button.inline("🔄 Auto Update", b"au:list")],
        [Button.inline("🔐 Login", b'addsession'), Button.inline("🚪 Logout", b'logout')],
        [Button.inline("🧹 Reset Customisation", b'reset_customisation')],
    ]
    if user_id in OWNER_ID:
        buttons.append([Button.inline("⚡ Speed Test", b'speedtest')])
    buttons.append([Button.url("🆘 Report Errors", SUPPORT_URL)])
    await gf.send_message(event.chat_id, message=MESS, buttons=buttons)

pending_photos = {}

async def _send_thumb_preview(event, user_id, kind):
    path = load_saved_thumbnail(user_id, kind)
    if not path or not os.path.isfile(path):
        await event.respond("ℹ️ **No thumbnail saved**\n\nSet one first from Customisations.")
        return
    try:
        await event.respond(file=path, message=f"👀 **{kind.title()} Thumbnail Preview**")
    except Exception as e:
        await event.respond(f"❌ Preview failed: `{e}`")





async def show_forward_destinations(event, user_id):
    destinations = user_forward_destinations.get(user_id, [])
    selected = (user_chat_ids.get(user_id) or [None])[0]
    rows = []
    for item in destinations:
        cid = item["chat_id"]
        title = item.get("title") or str(cid)
        prefix = "✅ " if cid == selected else "▫️ "
        rows.append([Button.inline((prefix + title)[:60], f"fwdsel:{cid}".encode())])
    rows.append([Button.inline("➕ Add New Channel", b"fwdadd")])
    if destinations:
        rows.append([Button.inline("🗑️ Delete Channel", b"fwddelmenu")])
    if selected:
        rows.append([Button.inline("🚫 Deselect Forward Chat ID", b"fwdoff")])
    rows.append([Button.inline("⬅️ Close", b"fwdclose")])
    selected_item = next((x for x in destinations if x.get("chat_id") == selected), None)
    selected_label = (
        f"{selected_item.get('title') or selected} ({selected})"
        if selected_item else "Forwarding OFF"
    )
    await event.respond(
        "╭━━━〔 🆔 𝗙𝗢𝗥𝗪𝗔𝗥𝗗 𝗗𝗘𝗦𝗧𝗜𝗡𝗔𝗧𝗜𝗢𝗡𝗦 〕━━━╮\n\n"
        "Choose a saved channel to enable forwarding.\n\n"
        f"📌 Saved channels: `{len(destinations)}`\n"
        f"🎯 Active destination: **{selected_label}**\n\n"
        "Use Add New Channel to save another destination.\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯",
        buttons=rows,
    )


async def reset_user_customisations(user_id):
    """Clear user customisation preferences while preserving thumbnails and login/premium data."""
    key = str(user_id)
    user_rename_preferences.pop(key, None)
    user_caption_preferences.pop(key, None)
    # Keep thumbnail fields untouched. Only clear the customisation fields.
    collection.update_one(
        {"_id": user_id},
        {"$unset": {
            "rename_tag": "",
            "caption": "",
            "replacement_words": "",
            "delete_words": ""
        }},
        upsert=True,
    )
    # Forward destinations are managed independently from customisation reset.
    # Preserve both the saved destination list and the currently selected target.
    sessions.pop(user_id, None)
    return True


@gf.on(events.CallbackQuery)
async def callback_query_handler(event):
    user_id = event.sender_id

    if event.data == b'setrename':
        await event.respond("╭━━━〔 🏷️ 𝗥𝗘𝗡𝗔𝗠𝗘 𝗧𝗔𝗚 〕━━━╮\n\nSend the text to append to filenames.\n\n💡 Example: `@MyBot`\n╰━━━━━━━━━━━━━━━━━━━━╯")
        sessions[user_id]='setrename'
    elif event.data == b'setcaption':
        await event.respond("╭━━━〔 📝 𝗖𝗔𝗣𝗧𝗜𝗢𝗡 〕━━━╮\n\nSend the caption you want added to uploaded files.\n╰━━━━━━━━━━━━━━━━━━━━╯")
        sessions[user_id]='setcaption'
    elif event.data == b'setreplacement':
        await event.respond("╭━━━〔 🔤 𝗥𝗘𝗣𝗟𝗔𝗖𝗘 𝗪𝗢𝗥𝗗𝗦 〕━━━╮\n\nUse this simple format:\n\n`OLD_WORD -> NEW_WORD`\n\nExample:\n`@oldbot -> Yash`\n\n💡 Add one replacement per message.\n╰━━━━━━━━━━━━━━━━━━━━╯")
        sessions[user_id]='setreplacement'
    elif event.data == b'listreplace':
        replacements=load_replacement_words(user_id)
        if not replacements:
            await event.respond("📋 **No replacement rules saved.**\n\nUse **Replace Words** to add one.")
        else:
            lines=["📋 **SAVED REPLACEMENTS**\n"]+[f"• `{a}` ➜ `{b}`" for a,b in replacements.items()]
            await event.respond("\n".join(lines), buttons=[[Button.inline("🗑️ Clear All Replacements", b'clearreplace')]])
    elif event.data == b'clearreplace':
        save_replacement_words(user_id,{})
        await event.respond("╭━━━〔 🗑️ 𝗥𝗘𝗣𝗟𝗔𝗖𝗘𝗠𝗘𝗡𝗧𝗦 𝗖𝗟𝗘𝗔𝗥𝗘𝗗 〕━━━╮\n\nAll replacement rules have been removed.\n╰━━━━━━━━━━━━━━━━━━━━╯")
    elif event.data == b'setforwardchat':
        await show_forward_destinations(event, user_id)
    elif event.data == b'fwdadd':
        await event.respond(
            "➕ **Add New Forward Channel**\n\n"
            "Send the channel Chat ID (example: `-1001234567890`).\n"
            "Make sure the JARVIS bot is an administrator and can post messages there."
        )
        sessions[user_id] = 'setforwardchat'
    elif event.data == b'fwdoff':
        user_chat_ids.pop(user_id, None)
        collection.update_one({"_id": user_id}, {"$set": {"active_forward_chat_id": None}, "$unset": {"chat_id": "", "chat_ids": ""}}, upsert=True)
        await event.respond("🚫 **Forwarding disabled.**\n\nYour saved channel list has been preserved.")
        await show_forward_destinations(event, user_id)
    elif event.data == b'fwddelmenu':
        rows = [[Button.inline("🗑️ " + str(x.get("title") or x["chat_id"])[:48], f"fwddel:{x['chat_id']}".encode())] for x in user_forward_destinations.get(user_id, [])]
        rows.append([Button.inline("⬅️ Back", b"setforwardchat")])
        await event.respond("Select the saved channel you want to delete:", buttons=rows)
    elif event.data.startswith(b'fwdsel:'):
        try:
            cid = int(event.data.decode().split(":", 1)[1])
            if not any(x["chat_id"] == cid for x in user_forward_destinations.get(user_id, [])):
                raise ValueError("not saved")
            user_chat_ids[user_id] = [cid]
            collection.update_one({"_id": user_id}, {"$set": {"active_forward_chat_id": cid, "chat_id": cid, "chat_ids": [cid]}}, upsert=True)
            await event.respond(f"✅ **Forwarding enabled.**\n\nSelected destination: `{cid}`")
            await show_forward_destinations(event, user_id)
        except Exception:
            await event.respond("❌ This saved channel could not be selected. Please refresh the list.")
    elif event.data.startswith(b'fwddel:'):
        try:
            cid = int(event.data.decode().split(":", 1)[1])
            destinations = [x for x in user_forward_destinations.get(user_id, []) if x["chat_id"] != cid]
            user_forward_destinations[user_id] = destinations
            selected = (user_chat_ids.get(user_id) or [None])[0]
            if selected == cid:
                user_chat_ids.pop(user_id, None)
            update = {"$set": {"forward_destinations": destinations}}
            if selected == cid:
                update["$set"]["active_forward_chat_id"] = None
                update["$unset"] = {"chat_id": "", "chat_ids": ""}
            collection.update_one({"_id": user_id}, update, upsert=True)
            await event.respond("🗑️ **Saved channel deleted.**")
            await show_forward_destinations(event, user_id)
        except Exception:
            await event.respond("❌ Could not delete that channel.")
    elif event.data == b'fwdclose':
        await event.respond("Forward destination menu closed.")
    elif event.data == b'addsession':
        await event.respond("ℹ️ **Use /login** to connect your Telegram account.")
    elif event.data == b'delete':
        await event.respond("╭━━━〔 🗑️ 𝗥𝗘𝗠𝗢𝗩𝗘 𝗪𝗢𝗥𝗗𝗦 〕━━━╮\n\nSend words separated by spaces.\n\nExample: `watermark demo`\n╰━━━━━━━━━━━━━━━━━━━━╯")
        sessions[user_id]='deleteword'
    elif event.data == b'logout':
        data=await db.get_data(user_id)
        if data and data.get('session'):
            await db.delete_session(user_id)
            await event.respond("╭━━━〔 ✅ 𝗟𝗢𝗚𝗚𝗘𝗗 𝗢𝗨𝗧 〕━━━╮\n\nYour saved Telegram session has been removed.\n╰━━━━━━━━━━━━━━━━━━━━╯")
        else: await event.respond("ℹ️ **No active session found.**\n\nUse /login first.")
    elif event.data in (b'videothumb', b'mediathumb'):
        kind='video' if event.data == b'videothumb' else 'media'
        pending_photos[user_id]=kind
        title='VIDEO' if kind=='video' else 'AUDIO / DOCUMENT'
        await event.respond(f"╭━━━〔 🖼️ 𝗦𝗘𝗧 {title} 𝗧𝗛𝗨𝗠𝗕𝗡𝗔𝗜𝗟 〕━━━╮\n\nSend a photo now.\n\n💡 This thumbnail is stored separately from the other type.\n╰━━━━━━━━━━━━━━━━━━━━╯")
    elif event.data in (b'videopreview', b'mediapreview'):
        await _send_thumb_preview(event,user_id,'video' if event.data==b'videopreview' else 'media')
    elif event.data in (b'delvideothumb', b'delmediathumb'):
        kind='video' if event.data==b'delvideothumb' else 'media'; field=_thumb_field(kind); path=f'{user_id}_{kind}_thumb.jpg'
        try: os.remove(path)
        except OSError: pass
        collection.update_one({'_id':user_id},{'$unset':{field:''}},upsert=True)
        await event.respond(f"🗑️ **{kind.title()} thumbnail deleted.**")
    elif event.data == b'reset_customisation':
        try:
            await reset_user_customisations(user_id)
            await event.respond(
                "╭━━━〔 🧹 𝗖𝗨𝗦𝗧𝗢𝗠𝗜𝗦𝗔𝗧𝗜𝗢𝗡 𝗥𝗘𝗦𝗘𝗧 〕━━━╮\n\n"
                "✅ Caption removed\n"
                "✅ Rename tag removed\n"
                "✅ Replacement rules removed\n"
                "✅ Remove-word rules removed\n"
                "✅ Forward Chat ID settings are unchanged\n\n"
                "🖼️ Saved thumbnails are unchanged.\n"
                "🔐 Login and Premium are unchanged.\n"
                "╰━━━━━━━━━━━━━━━━━━━━╯"
            )
        except Exception as e:
            await event.respond(f"❌ Customisation reset failed: `{e}`")
    elif event.data == b'speedtest':
        if user_id not in OWNER_ID:
            await event.answer("Admin only.", alert=True)
            return
        await event.edit("╭━━━〔 ⚡ 𝗦𝗣𝗘𝗘𝗗 𝗧𝗘𝗦𝗧 〕━━━╮\n\n🔄 Testing server speed...\n\n⏱️ This can take up to 90 seconds.\n╰━━━━━━━━━━━━━━━━━━━━╯")
        try:
            ping, download, upload = await run_speedtest(timeout=120)
            await event.edit(
                f"╭━━━〔 ⚡ 𝗦𝗣𝗘𝗘𝗗 𝗧𝗘𝗦𝗧 〕━━━╮\n\n"
                f"🏓 Ping: `{ping:.0f} ms`\n"
                f"⬇️ Download: `{download:.2f} Mbps`\n"
                f"⬆️ Upload: `{upload:.2f} Mbps`\n"
                f"╰━━━━━━━━━━━━━━━━━━━━╯"
            )
        except asyncio.TimeoutError:
            await event.edit("❌ **Speed test timed out.**\n\nThe speed-test server did not respond within 120 seconds. Please try again.")
        except Exception as e:
            await event.edit(f"❌ **Speed test failed.**\n\n`{str(e)[:500]}`")

@gf.on(events.NewMessage(func=lambda e: e.sender_id in pending_photos))
async def save_thumbnail(event):
    user_id=event.sender_id; kind=pending_photos.get(user_id)
    if not kind: return
    if not event.photo:
        await event.respond("🖼️ **Please send a photo.**"); pending_photos.pop(user_id,None); return
    temp=await event.download_media(); path=f'{user_id}_{kind}_thumb.jpg'
    try:
        if os.path.exists(path): os.remove(path)
        os.rename(temp,path)
        with open(path,'rb') as f: collection.update_one({'_id':user_id},{'$set':{_thumb_field(kind):pymongo.Binary(f.read())}},upsert=True)
        await event.respond(f"╭━━━〔 ✅ 𝗧𝗛𝗨𝗠𝗕𝗡𝗔𝗜𝗟 𝗦𝗔𝗩𝗘𝗗 〕━━━╮\n\n{kind.title()} thumbnail saved successfully.\n\n👀 Use Preview anytime from Customisations.\n╰━━━━━━━━━━━━━━━━━━━━╯")
    except Exception as e: await event.respond(f"❌ Could not save thumbnail: `{e}`")
    finally: pending_photos.pop(user_id,None)


async def resolve_forward_chat_id(raw: str):
    """Resolve a numeric Telegram chat ID exactly like the reference helper."""
    raw = str(raw).strip()
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw


async def check_forward_permission(bot, chat_id):
    """Resolve a destination without rejecting valid admin channels because of a
    transient/stale member-permission response. Actual send/copy operations remain
    subject to Telegram's permissions and are retried with safe fallbacks."""
    try:
        chat = await bot.get_chat(chat_id)
    except (PeerIdInvalid, ChannelPrivate, ChatForbidden, ValueError):
        return False, "Bot cannot access this chat ID or the chat ID is invalid."
    except Exception as e:
        return False, f"Could not resolve forward chat: {e}"

    # A channel can be resolved even when get_chat_member() temporarily returns
    # incomplete permission data. Do not block saving the ID in that case.
    try:
        member = await bot.get_chat_member(chat_id, "me")
        status = str(getattr(member, "status", "")).lower()
        if status in {"banned", "kicked", "left"}:
            return False, "Bot is not a member of the configured forward chat."
    except UserNotParticipant:
        return False, "Bot is not a member of the configured forward chat."
    except Exception:
        pass

    return True, ""


async def _validate_and_save_forward_chat_ids(user_id, text):
    """Validate and save one or more per-user forwarding Chat IDs."""
    parsed = _parse_chat_id_list(text)
    if not parsed:
        return False, [], ["No valid numeric Chat ID was detected."]

    valid = []
    invalid = []
    for raw_target in parsed:
        target = await resolve_forward_chat_id(str(raw_target))
        ok, err_msg = await check_forward_permission(app, target)
        if ok:
            valid.append(int(target) if isinstance(target, int) else target)
        else:
            invalid.append(f"{raw_target} — {err_msg}")

    if valid:
        target = valid[0]
        title = None
        # First ask the bot client (the bot must still have destination access).
        try:
            chat = await app.get_chat(target)
            title = getattr(chat, "title", None) or getattr(chat, "first_name", None)
        except Exception:
            pass
        # Private channels may be visible to the owner's logged-in account
        # even when the bot's peer cache does not contain their title.
        if not title:
            try:
                from safe_repo.modules.auto_update import _get_user_client
                owner_client = await _get_user_client(user_id)
                owner_chat = await owner_client.get_chat(target)
                title = getattr(owner_chat, "title", None) or getattr(owner_chat, "first_name", None)
            except Exception:
                pass
        title = str(title or f"Private/unknown channel {target}")[:100]
        destinations = list(user_forward_destinations.get(user_id, []))
        existing_destination = next((x for x in destinations if x["chat_id"] == target), None)
        if existing_destination is None:
            destinations.append({"chat_id": target, "title": str(title)[:100]})
        elif not existing_destination.get("title") or str(existing_destination.get("title")) == str(target):
            existing_destination["title"] = str(title)[:100]
        user_forward_destinations[user_id] = destinations
        user_chat_ids[user_id] = [target]
        collection.update_one(
            {"_id": user_id},
            {"$set": {"forward_destinations": destinations, "active_forward_chat_id": target,
                      "chat_ids": [target], "chat_id": target}},
            upsert=True,
        )
        return True, valid, invalid
    return False, [], invalid


@gf.on(events.NewMessage)
async def handle_user_input(event):
    user_id=event.sender_id
    if user_id not in sessions: return
    session_type=sessions[user_id]
    text=(event.text or '').strip()

    # Never swallow normal bot commands while a customisation prompt is open.
    # This keeps /start, /myplan, /buypremium, /reset, /batch and /cancel responsive.
    if text.startswith('/'):
        return

    if session_type=='setrename':
        await set_rename_command(user_id,text); await event.respond(f"🏷️ **Rename tag saved:** `{text}`")
    elif session_type=='setcaption':
        await set_caption_command(user_id,text); await event.respond(f"📝 **Caption saved.**")
    elif session_type=='setreplacement':
        if '->' not in text:
            await event.respond("❌ Wrong format. Use:\n`OLD_WORD -> NEW_WORD`\n\nExample: `@oldbot -> Yash`")
        else:
            old,new=[x.strip() for x in text.split('->',1)]
            if not old or not new: await event.respond("❌ Both OLD_WORD and NEW_WORD are required.")
            else:
                replacements=load_replacement_words(user_id); replacements[old]=new; save_replacement_words(user_id,replacements)
                await event.respond(f"✅ **Replacement saved**\n\n`{old}` ➜ `{new}`")
    elif session_type=='deleteword':
        words=text.split(); delete_words=load_delete_words(user_id); delete_words.update(words); save_delete_words(user_id,delete_words)
        await event.respond(f"🗑️ **Words added to remove list:** {', '.join(words)}")
    elif session_type=='setforwardchat':
        ok, valid, invalid = await _validate_and_save_forward_chat_ids(user_id, text)
        if ok:
            lines = [
                "╭━━━〔 ✅ 𝗙𝗢𝗥𝗪𝗔𝗥𝗗 𝗖𝗛𝗔𝗧 𝗜𝗗 𝗦𝗔𝗩𝗘𝗗 〕━━━╮",
                "",
                f"📤 Saved destinations: `{len(user_forward_destinations.get(user_id, []))}`",
                "",
            ]
            saved = user_forward_destinations.get(user_id, [])
            for chat_id in valid[:1]:
                saved_item = next((item for item in saved if item.get("chat_id") == chat_id), {})
                display_name = saved_item.get("title") or f"Channel {chat_id}"
                lines.append(f"• **{display_name}** — `{chat_id}`")
            if invalid:
                lines += ["", "⚠️ Not saved:"]
                lines.extend(f"• {item}" for item in invalid)
            lines += [
                "",
                "🚀 Forwarding is now enabled for the selected channel.",
                "╰━━━━━━━━━━━━━━━━━━━━╯",
            ]
            await event.respond("\n".join(lines))
        else:
            reason_lines = invalid or ["The Chat ID could not be validated."]
            await event.respond(
                "╭━━━〔 ❌ 𝗙𝗢𝗥𝗪𝗔𝗥𝗗 𝗖𝗛𝗔𝗧 𝗜𝗗 𝗙𝗔𝗜𝗟𝗘𝗗 〕━━━╮\n\n"
                "No destination was saved.\n\n"
                + "\n".join(f"• {item}" for item in reason_lines)
                + "\n\nMake sure the bot is a member/admin and can send messages to the destination.\n"
                "╰━━━━━━━━━━━━━━━━━━━━╯"
            )
    sessions.pop(user_id,None)

