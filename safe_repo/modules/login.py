import os
import time
import random
import string
from pyrogram import filters, Client
from pyrogram.errors import (
    ApiIdInvalid, PhoneNumberInvalid, PhoneCodeInvalid, PhoneCodeExpired,
    SessionPasswordNeeded, PasswordHashInvalid, FloodWait
)
from safe_repo import app
from safe_repo.core.mongo import db
from safe_repo.core.func import subscribe
from config import API_ID as api_id, API_HASH as api_hash
from safe_repo.core.log_utils import log_event, user_label

LOGIN_STATES = {}
LOGIN_TIMEOUT = 600
PASSWORD_TIMEOUT = 300


def _status_text(title, body):
    return f"╭━━━〔 {title} 〕━━━╮\n\n{body}\n╰━━━━━━━━━━━━━━━━━━━━╯"


async def _cleanup_state(user_id, disconnect=True):
    state = LOGIN_STATES.pop(user_id, None)
    if state and disconnect and state.get("client"):
        try:
            await state["client"].disconnect()
        except Exception:
            pass
    return state


async def _edit_status(state, text):
    try:
        await state["status"].edit_text(text)
    except Exception:
        pass


@app.on_message(filters.command("logout"))
async def clear_db(_, message):
    user_id = message.chat.id
    await _cleanup_state(user_id)
    data = await db.get_data(user_id)
    if data and data.get("session"):
        await db.delete_session(user_id)
        await message.reply(
            "╭━━━〔 🚪 𝗟𝗢𝗚𝗚𝗘𝗗 𝗢𝗨𝗧 〕━━━╮\n\n"
            "✅ Your saved Telegram session has been removed.\n\n"
            "You can use /login whenever you want to connect again.\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯"
        )
    else:
        await message.reply("ℹ️ **No saved session found.**\n\nUse /login to connect your Telegram account.")


@app.on_message(filters.command("login"))
async def generate_session(_, message):
    user_id = message.chat.id
    joined = await subscribe(_, message)
    if joined == 1:
        return

    await _cleanup_state(user_id)
    status = await message.reply(
        _status_text(
            "📱 𝗟𝗢𝗚𝗜𝗡",
            "Enter your phone number with the country code.\n\n"
            "💡 Example: `+919876543210`\n\n"
            "🔒 Never share your OTP or Telegram password with anyone."
        )
    )
    LOGIN_STATES[user_id] = {
        "stage": "phone",
        "status": status,
        "created": time.time(),
        "expires": time.time() + LOGIN_TIMEOUT,
        "client": None,
        "user": message.from_user,
    }


@app.on_message(
    filters.private
    & filters.text
    & ~filters.command([
        "start", "help", "id", "login", "logout", "myplan", "premium",
        "buypremium", "batch", "cancel", "reset", "customisations", "add", "rem",
        "check", "premiumlist", "premiumusers", "plist", "stats", "gcast",
        "announce", "speedtest", "admincommands", "redeempremium"
    ])
)
async def login_input(_, message):
    user_id = message.chat.id
    state = LOGIN_STATES.get(user_id)
    if not state:
        return

    if time.time() > state.get("expires", 0):
        await _edit_status(state, _status_text("⏰ 𝗟𝗢𝗚𝗜𝗡 𝗧𝗜𝗠𝗘𝗗 𝗢𝗨𝗧", "The verification window expired.\n\nPlease use /login again."))
        await _cleanup_state(user_id)
        return

    text = (message.text or "").strip()
    stage = state.get("stage")

    if stage == "phone":
        phone_number = text.replace(" ", "")
        if not phone_number.startswith("+") or len(phone_number) < 7:
            await _edit_status(state, _status_text("❌ 𝗜𝗡𝗩𝗔𝗟𝗜𝗗 𝗣𝗛𝗢𝗡𝗘", "Please enter your phone number with country code.\n\nExample: `+919876543210`"))
            state["stage"] = "phone"
            return

        client = Client(f"session_{user_id}", api_id, api_hash)
        state["client"] = client
        state["phone"] = phone_number
        await _edit_status(state, _status_text("📲 𝗟𝗢𝗚𝗜𝗡", "🔄 Sending a secure OTP to your Telegram account..."))
        try:
            await client.connect()
            sent = await client.send_code(phone_number)
            state["phone_code_hash"] = sent.phone_code_hash
            state["stage"] = "otp"
            state["expires"] = time.time() + LOGIN_TIMEOUT
            await _edit_status(
                state,
                _status_text(
                    "🔢 𝗘𝗡𝗧𝗘𝗥 𝗢𝗧𝗣",
                    "Check the OTP in your official Telegram app and send it here.\n\n"
                    "💡 Example: if your OTP is `12345`, send `1 2 3 4 5`."
                )
            )
        except ApiIdInvalid:
            await _edit_status(state, _status_text("❌ 𝗟𝗢𝗚𝗜𝗡 𝗘𝗥𝗥𝗢𝗥", "Invalid API credentials. Please contact the bot owner."))
            await _cleanup_state(user_id)
        except PhoneNumberInvalid:
            await _edit_status(state, _status_text("❌ 𝗜𝗡𝗩𝗔𝗟𝗜𝗗 𝗣𝗛𝗢𝗡𝗘", "Telegram rejected this phone number. Use /login to try again."))
            await _cleanup_state(user_id)
        except FloodWait as e:
            await _edit_status(state, _status_text("⏳ 𝗧𝗢𝗢 𝗙𝗔𝗦𝗧", f"Please wait `{e.value}` seconds and then use /login again."))
            await _cleanup_state(user_id)
        except Exception:
            await _edit_status(state, _status_text("❌ 𝗟𝗢𝗚𝗜𝗡 𝗘𝗥𝗥𝗢𝗥", "Unable to send the OTP. Please use /login again."))
            await _cleanup_state(user_id)
        return

    if stage == "otp":
        otp_code = text.replace(" ", "")
        if not otp_code.isdigit():
            await _edit_status(state, _status_text("🔢 𝗘𝗡𝗧𝗘𝗥 𝗢𝗧𝗣", "Please send only the digits from your Telegram OTP."))
            return
        try:
            client = state["client"]
            await client.sign_in(state["phone"], state["phone_code_hash"], otp_code)
        except SessionPasswordNeeded:
            state["stage"] = "password"
            state["expires"] = time.time() + PASSWORD_TIMEOUT
            await _edit_status(
                state,
                _status_text(
                    "🔐 𝟮-𝗦𝗧𝗘𝗣 𝗩𝗘𝗥𝗜𝗙𝗜𝗖𝗔𝗧𝗜𝗢𝗡",
                    "Your Telegram account has 2-step verification enabled.\n\n"
                    "🔑 Enter your Telegram password to continue.\n\n"
                    "⚠️ This bot does not display your password anywhere."
                )
            )
            return
        except PhoneCodeInvalid:
            await _edit_status(state, _status_text("❌ 𝗜𝗡𝗩𝗔𝗟𝗜𝗗 𝗢𝗧𝗣", "That OTP is incorrect. Please use /login again and enter the latest code."))
            await _cleanup_state(user_id)
            return
        except PhoneCodeExpired:
            await _edit_status(state, _status_text("⏰ 𝗢𝗧𝗣 𝗘𝗫𝗣𝗜𝗥𝗘𝗗", "The OTP expired. Please use /login again."))
            await _cleanup_state(user_id)
            return
        except Exception:
            await _edit_status(state, _status_text("❌ 𝗟𝗢𝗚𝗜𝗡 𝗙𝗔𝗜𝗟𝗘𝗗", "Telegram could not complete the sign-in. Please use /login again."))
            await _cleanup_state(user_id)
            return

        await _finish_login(user_id)
        return

    if stage == "password":
        try:
            client = state["client"]
            await client.check_password(password=text)
        except PasswordHashInvalid:
            await _edit_status(state, _status_text("❌ 𝗜𝗡𝗩𝗔𝗟𝗜𝗗 𝗣𝗔𝗦𝗦𝗪𝗢𝗥𝗗", "The Telegram password is incorrect. Please try the password again."))
            state["expires"] = time.time() + PASSWORD_TIMEOUT
            return
        except Exception:
            await _edit_status(state, _status_text("❌ 𝗩𝗘𝗥𝗜𝗙𝗜𝗖𝗔𝗧𝗜𝗢𝗡 𝗙𝗔𝗜𝗟𝗘𝗗", "Unable to verify the Telegram password. Please use /login again."))
            await _cleanup_state(user_id)
            return

        await _finish_login(user_id)


async def _finish_login(user_id):
    state = LOGIN_STATES.get(user_id)
    if not state:
        return
    try:
        client = state["client"]
        string_session = await client.export_session_string()
        await db.set_session(user_id, string_session)
        await client.disconnect()
        state["client"] = None
        await _edit_status(
            state,
            _status_text(
                "✅ 𝗟𝗢𝗚𝗜𝗡 𝗖𝗢𝗠𝗣𝗟𝗘𝗧𝗘",
                "🔐 Your Telegram session has been connected successfully.\n\n"
                "🚀 You can now send a supported message link."
            )
        )
        await log_event(f"🔐 LOGIN SUCCESS\n👤 {user_label(state.get('user'))}\n🆔 `{user_id}`")
    except Exception:
        await _edit_status(state, _status_text("❌ 𝗟𝗢𝗚𝗜𝗡 𝗘𝗥𝗥𝗢𝗥", "The session could not be saved. Please use /login again."))
    finally:
        LOGIN_STATES.pop(user_id, None)
