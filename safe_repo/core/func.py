#safe_repo


import math
import time , re
from pyrogram import enums
from config import CHANNEL_IDS, OWNER_ID, PREMIUM_CONTACT, FREE_TRIAL_USES, TRANSFER_IDLE_TIMEOUT, PLAN_CONFIG, DEFAULT_PLAN 
from safe_repo.core import script
from safe_repo.core.mongo.plans_db import premium_users, check_premium
from safe_repo.core.mongo import users_db
from safe_repo.core.mongo import plans_db
from safe_repo.core.log_utils import log_event, user_label
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
import cv2
from pyrogram.errors import FloodWait, InviteHashInvalid, InviteHashExpired, UserAlreadyParticipant, UserNotParticipant
from datetime import datetime as dt
import asyncio, subprocess, re, os, time, shutil, json



async def get_active_plan(user_id):
    if user_id in OWNER_ID:
        return "elite"
    data = await plans_db.check_premium(user_id)
    if not data:
        return None
    plan = str(data.get("plan") or "premium").lower()
    return plan if plan in PLAN_CONFIG else "premium"

async def is_premium_user(user_id):
    return (await get_active_plan(user_id)) is not None

async def plan_daily_remaining(user_id):
    plan = await get_active_plan(user_id)
    if not plan:
        return 0
    limit = PLAN_CONFIG[plan].get("daily_limit")
    if limit is None or user_id in OWNER_ID:
        return None
    used = await users_db.get_daily_usage(user_id)
    return max(0, int(limit) - used)

async def consume_plan_quota(user_id, amount=1):
    if user_id in OWNER_ID:
        return None
    plan = await get_active_plan(user_id)
    if not plan:
        return 0
    limit = PLAN_CONFIG[plan].get("daily_limit")
    if limit is None:
        return None
    return await users_db.increment_daily_usage(user_id, amount)

async def check_batch_access(user_id, requested):
    plan = await get_active_plan(user_id)
    if not plan:
        return False, "A Standard, Premium, Ultimate or Elite plan is required for /batch."
    info = PLAN_CONFIG[plan]
    if not info.get("batch"):
        return False, "Your current plan does not include /batch. Upgrade to Standard or above."
    requested = int(requested)
    batch_limit = info.get("batch_limit")
    if batch_limit and requested > batch_limit:
        return False, f"Your {info['label']} plan allows up to {batch_limit} messages per batch."
    remaining = await plan_daily_remaining(user_id)
    if remaining is not None and requested > remaining:
        return False, f"Daily quota remaining: {remaining}. Please reduce the batch or wait for the quota to reset."
    return True, ""


async def trial_uses(user_id):
    return await users_db.get_trial_uses(user_id)


async def chk_user(message, user_id):
    """Allow owners/premium users unlimited access, otherwise allow FREE_TRIAL_USES."""
    if await is_premium_user(user_id):
        remaining = await plan_daily_remaining(user_id)
        if remaining is None or remaining > 0:
            return 0
        await message.reply_text("🚫 **Daily plan limit reached**\n\nYour plan's daily quota has been used. Please try again after the daily reset or upgrade your plan.")
        return 1

    used = await trial_uses(user_id)
    trial_limit = await users_db.get_trial_limit(user_id)
    remaining = max(0, trial_limit - used)
    if remaining > 0:
        return 0

    await message.reply_text(
        f"🚫 **Free trial finished**\n\n"
        f"You have used all **{trial_limit} free uses**.\n\n"
        f"💎 Buy Premium with /buypremium to continue using the bot.\n"
        f"💬 Contact: {PREMIUM_CONTACT}"
    )
    await log_event(f"🚫 TRIAL FINISHED\n👤 {user_label(message.from_user)}\n🆔 `{user_id}`")
    return 1


async def consume_trial(user_id, amount=1):
    if await is_premium_user(user_id):
        return await trial_uses(user_id)
    return await users_db.increment_trial_uses(user_id, amount)


async def trial_remaining(user_id):
    if await is_premium_user(user_id):
        return -1
    return max(0, await users_db.get_trial_limit(user_id) - await trial_uses(user_id))



async def gen_link(app,chat_id):
   link = await app.export_chat_invite_link(chat_id)
   return link

async def subscribe(app, message):
    # Owner is never blocked by force-subscribe. This also prevents the old
    # repository channel from appearing for the owner.
    if message.from_user and message.from_user.id in OWNER_ID:
        return 0

    missing = []
    for update_channel in CHANNEL_IDS:
        try:
            member = await app.get_chat_member(update_channel, message.from_user.id)
            if member is None:
                raise RuntimeError("Telegram returned no membership record")
            status_text = str(getattr(member, "status", "")).lower()
            if "kicked" in status_text or "banned" in status_text:

                await message.reply_text("🚫 **Access restricted**\n\nYour account is banned from a required updates channel.\n\n💬 Please contact the bot owner for assistance.")
                return 1
        except UserNotParticipant:
            try:
                url = await gen_link(app, update_channel)
            except Exception:
                # If the bot cannot export an invite, still keep the other configured channels working.
                url = None
            missing.append((update_channel, url))
        except Exception as e:
            # Some Pyrogram versions/providers may return None instead of a member object.
            # Treat that as a membership check failure instead of raising `'NoneType' has no attribute id'`.
            await message.reply_text(
                "❌ **Force-subscribe configuration error**\n\n"
                f"Channel: `{update_channel}`\n"
                "The bot could not verify your membership in this channel. "
                "Please make sure the channel ID is correct and the bot is an administrator.\n\n"
                f"Details: <blockquote>{e}</blockquote>"
            )
            return 1

    if missing:
        buttons = []
        for index, (_, url) in enumerate(missing, start=1):
            if url:
                buttons.append([InlineKeyboardButton(f"Join Channel {index}", url=url)])
        markup = InlineKeyboardMarkup(buttons) if buttons else None
        await message.reply_text(
            script.FORCE_MSG.format(message.from_user.mention),
            reply_markup=markup
        )
        return 1
    return 0



async def get_seconds(time_string):
    """Parse Premium durations like 7 days, 1 week, 30d, 1 month, 1 year."""
    if not isinstance(time_string, str):
        return 0
    match = re.fullmatch(r"\s*(\d+)\s*(seconds?|secs?|s|minutes?|mins?|min|m|hours?|hrs?|h|days?|d|weeks?|w|months?|mo|years?|yrs?|y)\s*", time_string.lower())
    if not match:
        return 0
    value = int(match.group(1)); unit = match.group(2)
    if unit in {"s","sec","secs","second","seconds"}: return value
    if unit in {"m","min","mins","minute","minutes"}: return value * 60
    if unit in {"h","hr","hrs","hour","hours"}: return value * 3600
    if unit in {"d","day","days"}: return value * 86400
    if unit in {"w","week","weeks"}: return value * 7 * 86400
    if unit in {"mo","month","months"}: return value * 30 * 86400
    if unit in {"y","yr","yrs","year","years"}: return value * 365 * 86400
    return 0


PROGRESS_BAR = """╭━━━〔 📥 𝗣𝗥𝗢𝗚𝗥𝗘𝗦𝗦 〕━━━╮

📦 **{0}**

`{1}` **{2}%**

📊 **{3} / {4}**
⚡ **Speed:** {5}/s
⏱️ **ETA:** {6}

╰━━━━━━━━━━━━━━━━━━━━╯"""

_progress_cache = {}
_progress_last_activity = {}
_progress_speed_ema = {}
_progress_edit_tasks = {}
_progress_latest_text = {}

def _consume_task_result(task):
    """Consume a detached task's result so cancellation/errors are not orphaned."""
    try:
        task.result()
    except BaseException:
        pass


async def _cancel_and_drain_task(task, timeout=10):
    """Cancel a child task and drain it without leaking it into the next item."""
    if task is None:
        return
    if not task.done():
        task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=max(1, timeout))
    except asyncio.TimeoutError:
        # A third-party coroutine can ignore cancellation.  Keep the task
        # referenced by its callback until it finally exits instead of leaving
        # an unobserved exception behind.
        task.cancel()
        task.add_done_callback(_consume_task_result)
    except BaseException:
        pass


async def _safe_progress_edit(message, key):
    """Send the newest progress text without blocking the Telegram transfer."""
    try:
        while True:
            text = _progress_latest_text.pop(key, None)
            if text is None:
                return
            try:
                await asyncio.wait_for(message.edit_text(text), timeout=15)
            except Exception as exc:
                # A slow/failed UI edit must not stop the real transfer.  Keep
                # the error visible in logs, but ignore Telegram's harmless
                # duplicate-edit response.
                if "MESSAGE_NOT_MODIFIED" not in str(exc).upper():
                    print(f"Progress message edit error: {exc}")
            if key not in _progress_latest_text:
                return
    finally:
        current_task = asyncio.current_task()
        if _progress_edit_tasks.get(key) is current_task:
            _progress_edit_tasks.pop(key, None)
        # A callback can arrive in the tiny window between the loop's last
        # check and this cleanup.  Start one replacement worker for that
        # already-received real progress value.
        if key in _progress_latest_text and key not in _progress_edit_tasks:
            try:
                _progress_edit_tasks[key] = asyncio.create_task(
                    _safe_progress_edit(message, key)
                )
            except RuntimeError:
                pass

async def run_transfer_with_watchdog(coro, progress_message=None, idle_timeout=None, total_timeout=None):
    """Run a Telegram transfer with an independent hard watchdog.

    Progress-message edits are deliberately decoupled from the transfer, so a slow
    Telegram edit cannot pause Pyrogram's upload/download callback. If the transfer
    stops reporting progress for the idle timeout, its task is cancelled and the
    caller can restart the complete item from the download stage.
    """
    idle_timeout = int(idle_timeout or TRANSFER_IDLE_TIMEOUT)
    total_timeout = int(total_timeout or int(os.getenv("UPLOAD_TOTAL_TIMEOUT", "10800")))
    task = asyncio.create_task(coro)
    key = getattr(progress_message, "id", None)
    now = time.monotonic()
    if key is not None:
        _progress_last_activity[key] = now
    deadline = now + total_timeout
    try:
        while True:
            if task.done():
                return await task
            await asyncio.sleep(1)
            now = time.monotonic()
            if now >= deadline:
                await _cancel_and_drain_task(task)
                raise asyncio.TimeoutError("Transfer exceeded the maximum allowed time.")
            if key is not None:
                last = _progress_last_activity.get(key, now)
                if now - last >= idle_timeout:
                    await _cancel_and_drain_task(task)
                    raise asyncio.TimeoutError(
                        f"Transfer stopped making progress for {idle_timeout} seconds."
                    )
    except asyncio.CancelledError:
        await _cancel_and_drain_task(task)
        raise
    finally:
        if key is not None:
            _progress_last_activity.pop(key, None)
            _progress_cache.pop(key, None)
            _progress_speed_ema.pop(key, None)
            _progress_latest_text.pop(key, None)
            pending = _progress_edit_tasks.pop(key, None)
            if pending:
                await _cancel_and_drain_task(pending)

async def progress_bar(current, total, ud_type, message, start):
    key = getattr(message, "id", None)
    if key is not None:
        # This timestamp is updated immediately when Telegram reports transfer
        # progress; it is NOT dependent on the progress-message edit completing.
        _progress_last_activity[key] = time.monotonic()
    if not total or key is None:
        return
    now = time.time()
    diff = max(now - start, 0.001)
    last = _progress_cache.get(key, 0)
    if current != total and now - last < 1.5:
        return
    _progress_cache[key] = now

    percentage = min(100, current * 100 / total)
    filled = min(10, int(percentage // 10))
    bar = "▰" * filled + "▱" * (10 - filled)
    raw_speed = current / diff
    alpha = 0.30
    previous_speed = _progress_speed_ema.get(message.id)
    speed = raw_speed if previous_speed is None else (alpha * raw_speed + (1 - alpha) * previous_speed)
    _progress_speed_ema[message.id] = speed
    eta = (total - current) / speed if speed > 0 else 0
    eta_text = TimeFormatter(milliseconds=int(eta * 1000)) if eta > 0 else "0s"
    label = str(ud_type or "").replace("**", "").replace("__", "").strip()
    text = PROGRESS_BAR.format(
        label, bar, f"{percentage:.1f}", humanbytes(current), humanbytes(total), humanbytes(speed), eta_text
    )

    # Never await Telegram's UI edit from inside Pyrogram's transfer callback.
    # A slow edit/API response must not stall the actual upload/download.
    _progress_latest_text[key] = text
    if key not in _progress_edit_tasks:
        try:
            _progress_edit_tasks[key] = asyncio.create_task(
                _safe_progress_edit(message, key)
            )
        except RuntimeError:
            pass

    if current == total:
        _progress_cache.pop(key, None)
        _progress_speed_ema.pop(key, None)

def humanbytes(size):
    if not size:
        return "0 B"
    power = 2**10
    n = 0
    Dic_powerN = {0: '', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while size > power:
        size /= power
        n += 1
    return str(round(size, 2)) + " " + Dic_powerN[n] + 'B'

def TimeFormatter(milliseconds: int) -> str:
    seconds, milliseconds = divmod(int(milliseconds), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    tmp = ((str(days) + "d, ") if days else "") + \
        ((str(hours) + "h, ") if hours else "") + \
        ((str(minutes) + "m, ") if minutes else "") + \
        ((str(seconds) + "s, ") if seconds else "") + \
        ((str(milliseconds) + "ms, ") if milliseconds else "")
    return tmp[:-2] 



def convert(seconds):
    seconds = seconds % (24 * 3600)
    hour = seconds // 3600
    seconds %= 3600
    minutes = seconds // 60
    seconds %= 60      
    return "%d:%02d:%02d" % (hour, minutes, seconds)




async def userbot_join(userbot, invite_link):
    try:
        await userbot.join_chat(invite_link)
        return "Successfully joined the Channel"
    except UserAlreadyParticipant:
        return "User is already a participant."
    except (InviteHashInvalid, InviteHashExpired):
        return "Could not join. Maybe your link is expired or Invalid."
    except FloodWait:
        return "Too many requests, try again later."
    except Exception as e:
        print(e)
        return "Could not join, try joining manually."
    


def get_link(string):
    regex = r"(?i)\b((?:https?://|www\d{0,3}[.]|[a-z0-9.\-]+[.][a-z]{2,4}/)(?:[^\s()<>]+|\(([^\s()<>]+|(\([^\s()<>]+\)))*\))+(?:\(([^\s()<>]+|(\([^\s()<>]+\)))*\)|[^\s`!()\[\]{};:'\".,<>?«»“”‘’]))"
    url = re.findall(regex,string)   
    try:
        link = [x[0] for x in url][0]
        if link:
            return link
        else:
            return False
    except Exception:
        return False


def telegram_link_route(link):
    """Classify a Telegram message link before choosing a client.

    Plain username links (``t.me/channel/123``) identify public-message
    addresses and can be attempted with the already-running bot client.
    ``/c/`` links identify private chat IDs and must keep using the user's
    logged-in client.  Invite links and the existing ``/b/`` form also stay
    on the private/session path.

    The function is intentionally only a routing/validation helper; it does
    not claim that a public message is accessible.  Telegram remains the
    authority when the bot client attempts the request.
    """
    try:
        raw = str(link or "").strip()
        parsed = re.match(r"(?i)^(?:https?://)?(?:t\.me|telegram\.me)(?:/|$)", raw)
        if not parsed:
            return "other"

        clean = raw.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        if clean.lower().startswith(("http://", "https://")):
            path_text = clean.split("://", 1)[1]
            path_text = path_text.split("/", 1)[1] if "/" in path_text else ""
        else:
            path_text = clean.split("/", 1)[1] if "/" in clean else ""
        parts = [part for part in path_text.split("/") if part]

        # Preserve the existing login/session handling for join/deep links.
        if parts and (parts[0].startswith("+") or parts[0].lower() == "joinchat"):
            return "private"

        if len(parts) < 2 or not parts[-1].isdigit():
            return "invalid"

        marker = parts[0].lower()
        if marker == "c":
            return "private" if len(parts) in (3, 4) and parts[1].isdigit() else "invalid"
        if marker == "s":
            # Telegram's /s/<username>/<message_id> is the public preview form.
            return "public" if len(parts) in (3, 4) else "invalid"
        if marker == "b":
            return "private" if len(parts) == 3 else "invalid"

        # A normal username-based message link is public in link structure.
        if re.fullmatch(r"[A-Za-z0-9_]{1,64}", parts[0]):
            return "public"
        return "invalid"
    except (AttributeError, TypeError, ValueError):
        return "invalid"


def video_metadata(file):
    default_values = {'width': 0, 'height': 0, 'duration': 0}
    vcap = None
    try:
        vcap = cv2.VideoCapture(file)
        if not vcap.isOpened():
            return default_values  # Return defaults if video cannot be opened

        width = round(vcap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = round(vcap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = vcap.get(cv2.CAP_PROP_FPS)
        frame_count = vcap.get(cv2.CAP_PROP_FRAME_COUNT)

        if fps <= 0:
            return default_values  # Return defaults if FPS value is zero or negative

        duration = round(frame_count / fps)
        if duration <= 0:
            return default_values  # Return defaults if duration is zero or negative

        return {'width': width, 'height': height, 'duration': duration}

    except Exception as e:
        print(f"Error in video_metadata: {e}")
        return default_values
    finally:
        if vcap is not None:
            try:
                vcap.release()
            except Exception:
                pass


async def video_metadata_async(file, timeout=30, fallback=None):
    """Read video metadata without blocking the bot's asyncio event loop.

    ``cv2.VideoCapture`` is synchronous and can spend an unbounded amount of
    time probing a large or malformed container.  That is particularly harmful
    here because it runs after the download message is shown but before
    Pyrogram starts invoking the upload progress callback.  ffprobe runs in a
    cancellable subprocess instead, and a timed-out probe is terminated before
    the caller retries the item.
    """
    fallback = fallback or {}

    def _positive_int(value):
        try:
            parsed = float(value)
            return max(0, int(round(parsed))) if parsed > 0 else 0
        except (TypeError, ValueError):
            return 0

    fallback_values = {
        'width': _positive_int(fallback.get('width')),
        'height': _positive_int(fallback.get('height')),
        'duration': _positive_int(fallback.get('duration')),
    }
    default_values = fallback_values
    def _opencv_probe():
        capture = cv2.VideoCapture(str(file))
        try:
            if not capture.isOpened():
                return {}
            fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
            frames = float(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            duration = int(round(frames / fps)) if fps > 0 and frames > 0 else 0
            return {"width": width, "height": height, "duration": duration}
        finally:
            capture.release()

    ffprobe_bin = shutil.which("ffprobe")
    if not ffprobe_bin:
        try:
            probed = await asyncio.wait_for(asyncio.to_thread(_opencv_probe), timeout=max(1, float(timeout)))
            return {
                "width": probed.get("width") or default_values["width"],
                "height": probed.get("height") or default_values["height"],
                "duration": probed.get("duration") or default_values["duration"],
            }
        except Exception as exc:
            print(f"OpenCV video metadata fallback failed: {exc}")
            return default_values

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe_bin,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,duration:format=duration",
            "-of", "json",
            str(file),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=max(1, float(timeout))
        )
        probe = json.loads(stdout.decode(errors="replace") or "{}")
        streams = probe.get("streams") or []
        stream = streams[0] if streams else {}
        format_info = probe.get("format") or {}

        def _positive_number(value):
            try:
                parsed = float(value)
                return parsed if parsed > 0 else 0
            except (TypeError, ValueError):
                return 0

        width = int(round(_positive_number(stream.get("width"))) or fallback_values["width"])
        height = int(round(_positive_number(stream.get("height"))) or fallback_values["height"])
        durations = [
            _positive_number(stream.get("duration")),
            _positive_number(format_info.get("duration")),
            float(fallback_values["duration"]),
        ]
        duration = int(round(max(durations))) if any(durations) else fallback_values["duration"]
        result = {
            "width": max(0, width),
            "height": max(0, height),
            "duration": max(0, duration),
        }
        if not all(result.values()):
            try:
                probed = await asyncio.wait_for(
                    asyncio.to_thread(_opencv_probe), timeout=max(1, float(timeout))
                )
                result["width"] = result["width"] or probed.get("width", 0) or fallback_values["width"]
                result["height"] = result["height"] or probed.get("height", 0) or fallback_values["height"]
                result["duration"] = result["duration"] or probed.get("duration", 0) or fallback_values["duration"]
            except Exception as exc:
                print(f"OpenCV metadata fallback failed: {exc}")
        return result
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
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
            except ProcessLookupError:
                pass
            except Exception:
                pass
            try:
                await proc.communicate()
            except BaseException:
                pass
        raise asyncio.TimeoutError("Video metadata probe timed out.")
    except Exception as e:
        print(f"Error in async video_metadata: {e}")
        return default_values
    
def hhmmss(seconds):
    return time.strftime('%H:%M:%S',time.gmtime(seconds))

async def screenshot(video, duration, sender):
    if os.path.exists(f'{sender}.jpg'):
        return f'{sender}.jpg'

    time_stamp = hhmmss(int(duration) / 2)
    out = dt.now().isoformat("_", "seconds") + ".jpg"
    try:
        import shutil
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            try:
                import imageio_ffmpeg
                ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                ffmpeg_bin = None

        if not ffmpeg_bin:
            return None

        cmd = [
            ffmpeg_bin, "-ss", f"{time_stamp}", "-i", f"{video}",
            "-frames:v", "1", f"{out}", "-y"
        ]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await process.communicate()
        if os.path.isfile(out):
            return out
        return None
    except (FileNotFoundError, OSError):
        return None
    except Exception as e:
        print(f"Screenshot/FFmpeg error: {e}")
        return None

