"""Public social-media downloader for supported yt-dlp URLs.

Telegram message links are intentionally handled by modules.main.py. This
module only accepts non-Telegram HTTP(S) URLs and keeps yt-dlp work off the
async event loop.
"""
import asyncio
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from pyrogram import filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import FloodWait

from safe_repo import app
from config import MEDIA_DOWNLOAD_TIMEOUT
from safe_repo.core.func import chk_user, consume_trial, consume_plan_quota, is_premium_user, trial_remaining, plan_daily_remaining
from safe_repo.core.get_func import (
    video_metadata_async, upload_video_resilient, upload_audio_resilient,
    upload_document_resilient, upload_photo_resilient, with_media_footer, PRE_UPLOAD_TIMEOUT,
)
from safe_repo.core.log_utils import log_event, user_label
from safe_repo.core.mongo import users_db

URL_RE = re.compile(r"https?://[^\s<>]+", re.I)
TELEGRAM_HOSTS = {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}
SESSIONS = {}
MEDIA_TASKS = {}
ACTIVE_MEDIA_SESSIONS = {}
SESSION_TTL_SECONDS = 20 * 60


def _yt_dlp_base_options():
    """Return stable yt-dlp options without inheriting host-level config.

    The Docker image installs Deno; when available, yt-dlp can use it for
    supported JavaScript-dependent extractors such as YouTube.
    """
    options = {
        "quiet": True,
        "no_warnings": True,
        "ignoreconfig": True,
    }
    deno_path = shutil.which("deno")
    if deno_path:
        options["js_runtimes"] = {"deno": {"path": deno_path}}
    return options


def _friendly_media_error(error):
    """Turn common platform authentication/JS errors into actionable text."""
    detail = str(error)
    lowered = detail.lower()
    if (
        "confirm you're not a bot" in lowered
        or "sign in to confirm" in lowered
        or "not a bot" in lowered
    ):
        return (
            "YouTube is currently refusing this automated server request with "
            "its bot-verification check. This can happen even for a public video; "
            "it does not by itself mean the video is private. JARVIS cannot bypass "
            "YouTube verification or import browser sign-in sessions. The "
            "downloader now uses the updated yt-dlp release and configured Deno "
            "runtime; if YouTube continues to block the server, this video cannot "
            "be fetched from this deployment at the moment. Please try again later."
        )
    if any(term in lowered for term in (
        "cookies", "sign in", "login required", "age-restricted",
        "age restricted", "members-only", "private video",
    )):
        return (
            "This media link requires sign-in, cookies, or additional access "
            "verification. JARVIS supports publicly accessible media only and "
            "does not import private browser sessions or bypass login, age, or "
            "platform verification checks."
        )
    if any(term in lowered for term in (
        "javascript", "js runtime", "deno", "signature extraction",
    )):
        return (
            "The platform's media page needs a JavaScript runtime or an updated "
            "extractor. The Docker deployment includes Deno; rebuild/redeploy "
            "the latest image and try again."
        )
    return detail[:700]


def _is_telegram_url(url):
    try:
        return (urlparse(url).hostname or "").lower() in TELEGRAM_HOSTS
    except Exception:
        return False


def _clean_url(text):
    for raw_url in URL_RE.findall(text or ""):
        url = raw_url.rstrip(".,!?)]}>")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            continue
        if _is_telegram_url(url):
            continue
        return url
    return None


def _extract_info(url):
    opts = {
        **_yt_dlp_base_options(),
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "extractor_retries": 2,
        "ignoreerrors": False,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError("This link did not return downloadable media.")
    if info.get("_type") in ("playlist", "multi_video"):
        raise RuntimeError("Playlist links are not supported yet. Please send one video/post link at a time.")
    formats = info.get("formats") or []
    video_formats = [item for item in formats if item.get("vcodec") != "none"]
    best_video = max(video_formats, key=lambda item: int(item.get("height") or 0), default={})
    return {
        "title": str(info.get("title") or "Downloaded media")[:180],
        "duration": info.get("duration"),
        "width": info.get("width") or best_video.get("width") or 0,
        "height": info.get("height") or best_video.get("height") or 0,
        "extractor": str(info.get("extractor_key") or info.get("extractor") or "Media platform"),
        "formats": formats,
        "webpage_url": info.get("webpage_url") or url,
        "thumbnail": info.get("thumbnail"),
    }


def _format_options(info):
    formats = info.get("formats") or []
    heights = sorted({
        int(f.get("height")) for f in formats
        if f.get("height") and f.get("vcodec") != "none"
    }, reverse=True)
    options = []
    for height in heights[:10]:
        code = f"mp4:{height}"
        label = f"🎬 MP4 · {height}p"
        if shutil.which("ffmpeg"):
            fmt = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"
        else:
            fmt = f"best[height<={height}][ext=mp4]/best[height<={height}]/best"
        options.append((code, label, fmt))
    if not options and formats:
        options.append(("mp4:best", "🎬 MP4 · Best available", "best[ext=mp4]/best"))
    # Audio conversion requires ffmpeg. Offer it only when the binary is present.
    if shutil.which("ffmpeg"):
        options.append(("mp3:audio", "🎵 MP3 · Audio", "bestaudio/best"))
    return options


def _download(url, fmt, output_dir, audio=False, progress_hook=None):
    template = str(Path(output_dir) / "%(title).120B-%(id)s.%(ext)s")
    opts = {
        **_yt_dlp_base_options(),
        "format": fmt,
        "outtmpl": template,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "continuedl": True,
        "overwrites": True,
        "progress_hooks": [progress_hook] if progress_hook else [],
    }
    if audio:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    else:
        # Avoid a hard dependency on ffmpeg: use a single-file MP4 when possible.
        opts["format"] = fmt
        opts["merge_output_format"] = "mp4"
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    files = [p for p in Path(output_dir).iterdir() if p.is_file() and p.stat().st_size > 0]
    if not files:
        raise RuntimeError("The platform did not produce a downloadable file.")
    return max(files, key=lambda p: p.stat().st_mtime)


def _remux_video_to_mp4(path):
    """Remux a downloaded video into an MP4 container without re-encoding.

    This preserves the original video/audio quality while improving Telegram
    video playback, duration probing and thumbnail support. If the source
    codecs cannot be placed in MP4, keep the original file unchanged.
    """
    source = Path(path)
    # Remux MP4/M4V too: social platforms may provide a container whose index
    # or metadata is not accepted by gallery players. Stream-copy avoids re-encode.
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        try:
            import imageio_ffmpeg
            ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return source
    output = source.with_name(source.stem + ".jarvis.mp4")
    command = [
        ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-map", "0:v:0", "-map", "0:a?",
        "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(output),
    ]
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
        if result.returncode == 0 and output.is_file() and output.stat().st_size > 0:
            return output
    except Exception as exc:
        print(f"Video MP4 remux skipped: {exc}")
    try:
        output.unlink(missing_ok=True)
    except OSError:
        pass

    # If stream-copy cannot produce a gallery-compatible MP4 (e.g. unsupported
    # codec/container combination), use a compatibility encode as a last resort.
    fallback_command = [
        ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", "-f", "mp4", str(output),
    ]
    try:
        result = subprocess.run(fallback_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        if result.returncode == 0 and output.is_file() and output.stat().st_size > 0:
            return output
    except Exception as exc:
        print(f"Video compatibility encode skipped: {exc}")
    try:
        output.unlink(missing_ok=True)
    except OSError:
        pass
    return source


def _session_for(user_id, key):
    session = SESSIONS.get(key)
    if not session or session.get("user_id") != user_id:
        return None
    return session


@app.on_message(filters.private & filters.command("media"))
async def media_command(_, message):
    if await chk_user(message, message.from_user.id):
        return
    await message.reply_text(
        "╭━━━〔 🎬 𝗠𝗘𝗗𝗜𝗔 𝗗𝗢𝗪𝗡𝗟𝗢𝗔𝗗𝗘𝗥 〕━━━╮\n\n"
        "Send me a supported YouTube, Instagram or other platform video link.\n\n"
        "⚙️ I will check available formats and show quality options.\n"
        "📥 Choose a format and I will download it and send the media here.\n"
        "📊 Download and upload progress will be shown.\n\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯"
    )


@app.on_message(filters.private & filters.text & filters.regex(r"https?://"), group=-9)
async def external_media_link(_, message):
    # Keep Telegram links exclusively on the established Telegram pipeline.
    url = _clean_url(message.text or "")
    if not url:
        return
    if message.text and message.text.strip().split()[0].startswith("/"):
        return
    if await chk_user(message, message.from_user.id):
        return

    status = await message.reply_text("🔎 **Checking media link...**\n\nPlease wait while I check the available formats.")
    try:
        info = await asyncio.wait_for(asyncio.to_thread(_extract_info, url), timeout=90)
        options = _format_options(info)
        if not options:
            raise RuntimeError("No supported video or audio formats were found for this link.")
        key = uuid.uuid4().hex[:10]
        SESSIONS[key] = {"user_id": message.from_user.id, "url": url, "info": info, "options": {x[0]: x[2] for x in options}, "created": asyncio.get_running_loop().time()}
        # Opportunistically remove expired in-memory selections.
        now = asyncio.get_running_loop().time()
        for old_key, old in list(SESSIONS.items()):
            if now - old.get("created", now) > SESSION_TTL_SECONDS:
                SESSIONS.pop(old_key, None)
        buttons = [[InlineKeyboardButton(label, callback_data=f"smd:{key}:{code}")] for code, label, _ in options]
        buttons.append([InlineKeyboardButton("✖️ Cancel", callback_data=f"smdcancel:{key}")])
        duration = info.get("duration")
        duration_text = f"\n⏱️ Duration: `{int(duration)} sec`" if duration else ""
        text = (
            "╭━━━〔 🎬 𝗠𝗘𝗗𝗜𝗔 𝗗𝗢𝗪𝗡𝗟𝗢𝗔𝗗𝗘𝗥 〕━━━╮\n\n"
            f"📌 **Title:** {info['title']}\n"
            f"🌐 **Platform:** {info['extractor']}{duration_text}\n\n"
            "👇 Choose your preferred quality / format:\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯"
        )
        await status.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons), disable_web_page_preview=True)
    except asyncio.TimeoutError:
        await status.edit_text("⌛ **Media information timed out.**\n\nThe platform took too long to respond. Please try again.")
    except Exception as exc:
        await status.edit_text(
            "❌ **Unable to read this media link.**\n\n"
            f"{_friendly_media_error(exc)}\n\n"
            "The link may be private, unavailable, or unsupported by the current platform extractor."
        )


@app.on_callback_query(filters.regex(r"^smdcancel:[a-f0-9]{10}$"))
async def cancel_media_selection(_, query):
    key = query.data.split(":", 1)[1]
    session = _session_for(query.from_user.id, key)
    if not session:
        return await query.answer("This selection expired. Send the link again.", show_alert=True)
    SESSIONS.pop(key, None)
    await query.answer("Cancelled")
    await query.message.edit_text("❎ **Media download cancelled.**")


@app.on_callback_query(filters.regex(r"^smd:[a-f0-9]{10}:(?:mp4:\d+|mp4:best|mp3:audio)$"))
async def download_selected_media(_, query):
    parts = query.data.split(":")
    key, code = parts[1], ":".join(parts[2:])
    session = _session_for(query.from_user.id, key)
    if not session:
        return await query.answer("This selection expired. Send the link again.", show_alert=True)
    fmt = session["options"].get(code)
    if not fmt:
        return await query.answer("Selected format is no longer available.", show_alert=True)
    if await is_premium_user(query.from_user.id):
        daily_left = await plan_daily_remaining(query.from_user.id)
        if daily_left is not None and daily_left <= 0:
            return await query.answer("Your daily plan quota is finished. Use /buypremium for an upgrade.", show_alert=True)
    elif await trial_remaining(query.from_user.id) == 0:
        return await query.answer("Your shared free trials are finished. Use /buypremium.", show_alert=True)
    await query.answer("Download started")
    status = await query.message.reply_text("⬇️ **Preparing download...**")
    work_dir = tempfile.mkdtemp(prefix=f"jarvis_media_{query.from_user.id}_")
    last_edit = {"time": 0.0}
    progress_state = {"uploading": False}
    loop = asyncio.get_running_loop()

    async def safe_progress_edit(text):
        if progress_state["uploading"]:
            return
        try:
            await status.edit_text(text)
        except Exception:
            pass

    def hook(data):
        if session.get("cancel_requested"):
            raise yt_dlp.utils.DownloadError("Download cancelled by /cancel")
        if not status or data.get("status") not in ("downloading", "finished"):
            return
        now = time.monotonic()
        if now - last_edit["time"] < 2.0 and data.get("status") != "finished":
            return
        last_edit["time"] = now
        downloaded = int(data.get("downloaded_bytes") or 0)
        total = int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0)
        if total > 0:
            pct = min(100, int(downloaded * 100 / total))
            bar = "▰" * (pct // 10) + "▱" * (10 - pct // 10)
            text = f"⬇️ **Downloading media**\n\n`{bar}` `{pct}%`\n{downloaded / 1048576:.1f} MB / {total / 1048576:.1f} MB"
        else:
            text = f"⬇️ **Downloading media**\n\n`{downloaded / 1048576:.1f} MB` received"
        try:
            loop.call_soon_threadsafe(lambda: asyncio.create_task(safe_progress_edit(text)))
        except RuntimeError:
            pass

    task = asyncio.current_task()
    MEDIA_TASKS[query.from_user.id] = task
    ACTIVE_MEDIA_SESSIONS[query.from_user.id] = session
    session["cancel_requested"] = False
    try:
        audio = code.startswith("mp3:")
        path = await asyncio.wait_for(
            asyncio.to_thread(_download, session["url"], fmt, work_dir, audio, hook),
            timeout=MEDIA_DOWNLOAD_TIMEOUT,
        )
        if not audio:
            path = await asyncio.to_thread(_remux_video_to_mp4, path)
        size = path.stat().st_size
        if size > 1_950 * 1024 * 1024:
            raise RuntimeError("This file is too large to send through the current Telegram bot upload route.")
        caption = with_media_footer(
            f"📥 **{session['info']['title'][:700]}**\n\n⚡ Downloaded by JARVIS SRB",
            include_footer=True,
        )
        progress_state["uploading"] = True
        await status.edit_text("📤 **Uploading to Telegram...**\n\nUpload progress will appear here.")
        if audio:
            await upload_audio_resilient([], str(path), caption, status, query.from_user.id,
                duration=int(session["info"].get("duration") or 0), title=session["info"]["title"][:64],
                upload_chat_id=query.from_user.id)
        elif path.suffix.lower() in (".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".flv", ".ts"):
            metadata = await asyncio.wait_for(
                video_metadata_async(str(path), timeout=PRE_UPLOAD_TIMEOUT, fallback={
                    "width": int(session["info"].get("width") or 0),
                    "height": int(session["info"].get("height") or 0),
                    "duration": int(round(float(session["info"].get("duration") or 0))),
                }), timeout=PRE_UPLOAD_TIMEOUT,
            )
            await upload_video_resilient([], str(path), caption, status, query.from_user.id,
                metadata.get("width") or 0, metadata.get("height") or 0, metadata.get("duration") or 0,
                upload_chat_id=query.from_user.id, use_saved_thumbnail=False)
        elif path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
            await upload_photo_resilient([], str(path), caption, status, query.from_user.id,
                upload_chat_id=query.from_user.id)
        else:
            await upload_document_resilient([], str(path), caption, status, query.from_user.id,
                upload_chat_id=query.from_user.id)
        if await is_premium_user(query.from_user.id):
            await consume_plan_quota(query.from_user.id, 1)
        else:
            used = await consume_trial(query.from_user.id)
            limit = await users_db.get_trial_limit(query.from_user.id)
            remaining = max(0, limit - used)
            await log_event(
                f"🎬 MEDIA DOWNLOAD SUCCESS\n👤 {user_label(query.from_user)}\n"
                f"🆔 `{query.from_user.id}`\n📊 Trial: `{used}/{limit}` · Remaining: `{remaining}`"
            )
            if remaining == 0:
                await app.send_message(query.from_user.id, "🎁 **Your shared free uses are finished.**\nUse /buypremium to continue.")
        await status.edit_text("✅ **Media sent successfully.**")
    except asyncio.CancelledError:
        session["cancel_requested"] = True
        try:
            await status.edit_text("⛔ **Media download cancelled by /cancel.**")
        except Exception:
            pass
        raise
    except asyncio.TimeoutError:
        await status.edit_text("⌛ **Download timed out.**\n\nTry a lower quality or another link.")
    except FloodWait as fw:
        wait = int(getattr(fw, "value", 0) or 0)
        await status.edit_text(f"⏳ **Telegram FloodWait**\n\nPaused for `{wait}` seconds. Please retry after the wait.")
    except Exception as exc:
        await status.edit_text(f"❌ **Download failed.**\n\n{_friendly_media_error(exc)}")
    finally:
        session["cancel_requested"] = True
        SESSIONS.pop(key, None)
        if MEDIA_TASKS.get(query.from_user.id) is task:
            MEDIA_TASKS.pop(query.from_user.id, None)
        if ACTIVE_MEDIA_SESSIONS.get(query.from_user.id) is session:
            ACTIVE_MEDIA_SESSIONS.pop(query.from_user.id, None)
        shutil.rmtree(work_dir, ignore_errors=True)
