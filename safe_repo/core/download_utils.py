"""Robust download helpers for authorized Telegram media.

The helper keeps temporary files inside a guaranteed per-user directory, removes
stale partial files before retrying, and retries transient transfer failures.
"""
import asyncio
import copy
import os
import time
import uuid
from pathlib import Path

from safe_repo.core.func import run_transfer_with_watchdog
from config import TRANSFER_RETRIES, TRANSFER_IDLE_TIMEOUT, UPLOAD_TOTAL_TIMEOUT


def _download_root() -> Path:
    root = Path(os.getenv("DOWNLOAD_DIR", str(Path.cwd() / "downloads")))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cleanup_partial(folder: Path, stem: str) -> None:
    if not folder.exists():
        return
    for path in folder.glob(f"{stem}*"):
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass


def _media_only_message(message):
    """Return a shallow copy containing the same Telegram media but no caption/text.

    Some Telegram/Pyrogram message combinations can fail during media resolution
    when caption entities contain quote characters. The caption is not needed to
    download the file, so keep the original media object and remove only text
    metadata from the temporary download view. The original message is untouched
    and is still used later for the real caption.
    """
    try:
        clean = copy.copy(message)
    except Exception:
        return message

    for attr in ("caption", "caption_entities", "text", "entities"):
        try:
            setattr(clean, attr, None)
        except Exception:
            pass
    return clean


async def download_media_resilient(client, message, *, progress=None, progress_args=(),
                                   progress_message=None, user_id=None, retries=None):
    """Download media with safe temp handling and bounded retries.

    This is intended for media the bot is authorized to process. A completed
    download must exist on disk and have a non-zero size before it is returned.
    """
    retries = max(1, int(retries or TRANSFER_RETRIES))
    user_dir = _download_root() / str(user_id or "anonymous")
    user_dir.mkdir(parents=True, exist_ok=True)
    stem = f"download_{int(time.time() * 1000)}_{uuid.uuid4().hex[:10]}"
    target = user_dir / stem
    last_error = None

    for attempt in range(1, retries + 1):
        _cleanup_partial(user_dir, stem)
        try:
            # Caption text is not required to resolve/download Telegram media.
            # For quote-bearing captions, use a media-only view so caption parser
            # or entity handling can never affect the actual media download.
            # The original message remains untouched for caption preservation.
            download_message = message
            caption = getattr(message, "caption", None)
            if caption:
                quote_chars = {
                    '"', "'", '“', '”', '‘', '’', '„', '‟',
                    '«', '»', '‹', '›', '`',
                }
                if any(ch in quote_chars for ch in str(caption)):
                    download_message = _media_only_message(message)

            result = await run_transfer_with_watchdog(
                client.download_media(
                    download_message,
                    file_name=str(target),
                    progress=progress,
                    progress_args=progress_args,
                ),
                progress_message=progress_message,
                idle_timeout=TRANSFER_IDLE_TIMEOUT,
                total_timeout=UPLOAD_TOTAL_TIMEOUT,
            )
            if result and os.path.isfile(result) and os.path.getsize(result) > 0:
                return result
            raise RuntimeError("Download completed without producing a valid local file.")
        except asyncio.CancelledError:
            _cleanup_partial(user_dir, stem)
            raise
        except Exception as exc:
            last_error = exc
            _cleanup_partial(user_dir, stem)
            if attempt < retries:
                await asyncio.sleep(min(2 ** (attempt - 1), 8))

    raise RuntimeError(f"Download failed after {retries} attempts: {last_error}")
