import asyncio
from pyrogram import filters
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from safe_repo import app
from safe_repo.core import script
from safe_repo.core.func import subscribe
from config import PREMIUM_URL, SUPPORT_URL
from safe_repo.core.log_utils import log_event, user_label
from safe_repo.core.mongo.users_db import register_start


def start_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 How to Use", callback_data="start_help")],
        [
            InlineKeyboardButton("🔐 Login", callback_data="start_login"),
            InlineKeyboardButton("📦 Batch", callback_data="start_batch"),
        ],
        [
            InlineKeyboardButton("📋 My Plan", callback_data="start_plan"),
            InlineKeyboardButton("💎 Premium", url=PREMIUM_URL),
        ],
        [InlineKeyboardButton("🛑 Reset Current Task", callback_data="start_reset"),
        ],
        [InlineKeyboardButton("💬 Support", url=SUPPORT_URL)],
    ])


@app.on_message(filters.command("start"))
async def start(_, message):
    # Register/update the user on every /start.  Only a first /start carrying a
    # valid ref_<user_id> argument can create referral credit.
    try:
        command = message.command or []
        referral_id = command[1][4:] if len(command) > 1 and command[1].startswith("ref_") else None
        is_new, rewarded, referrer = await register_start(message.from_user, referral_id)
        if rewarded:
            await log_event(
                f"🎁 REFERRAL SUCCESS\n👤 New user: `{message.from_user.id}`\n"
                f"👥 Referrer: `{referrer}`\n➕ Tokens earned: `2`"
            )
    except Exception as e:
        print(f"User registration error: {e}")

    join = await subscribe(_, message)
    if join == 1:
        return

    await log_event(f"▶️ /START\n👤 {user_label(message.from_user)}\n🆔 `{message.from_user.id}`")
    await message.reply_text(
        script.START_TXT.format(message.from_user.mention),
        reply_markup=start_keyboard(),
        disable_web_page_preview=True,
    )


@app.on_callback_query(filters.regex(r"^start_(help|login|batch|plan|reset)$"))
async def start_menu(_, query: CallbackQuery):
    action = query.matches[0].group(1)
    await query.answer()

    if action == "help":
        await query.message.reply_text(
            "╭━━━〔 📚 𝗤𝗨𝗜𝗖𝗞 𝗛𝗘𝗟𝗣 〕━━━╮\n\n"
            "1️⃣ Use /login for private sources.\n"
            "2️⃣ Send a Telegram link for a single message.\n"
            "3️⃣ Use /batch for multiple messages.\n"
            "4️⃣ Use /cancel to stop a running batch.\n"
            "5️⃣ Use /myplan to check your access.\n\n"
            "💡 For the full guide, use /help.\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯"
        )
    elif action == "login":
        await query.message.reply_text(
            "╭━━━〔 🔐 𝗧𝗘𝗟𝗘𝗚𝗥𝗔𝗠 𝗟𝗢𝗚𝗜𝗡 〕━━━╮\n\n"
            "Use /login and follow the prompts.\n\n"
            "🔒 The logged-in account must have legitimate access to the private source you want to process.\n\n"
            "⚠️ Never share your OTP or password with anyone.\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯"
        )
    elif action == "batch":
        await query.message.reply_text(
            "╭━━━〔 📦 𝗕𝗔𝗧𝗖𝗛 𝗠𝗢𝗗𝗘 〕━━━╮\n\n"
            "Use /batch and follow the prompts.\n\n"
            "1️⃣ Send the **start message link**.\n"
            "2️⃣ Send the **end message link**.\n"
            "3️⃣ The bot processes the selected range.\n\n"
            "⛔ Use /cancel anytime to stop an active batch.\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯"
        )
    elif action == "reset":
        user_id = query.from_user.id
        try:
            from safe_repo.modules.main import active_tasks, users_loop, cancel_requested
            from safe_repo.core.get_func import sessions, pending_photos
            cancel_requested.add(user_id)
            users_loop.pop(user_id, None)
            sessions.pop(user_id, None)
            pending_photos.pop(user_id, None)
            task = active_tasks.get(user_id)
            if task and task is not asyncio.current_task() and not task.done():
                task.cancel()
                await query.message.reply_text(
                    "╭━━━〔 🛑 𝗙𝗢𝗥𝗖𝗘 𝗥𝗘𝗦𝗘𝗧 〕━━━╮\n\n"
                    "The current download/upload/batch task has been force-stopped.\n"
                    "Pending input has also been cleared.\n\n"
                    "✅ Login, Premium and saved customisations are unchanged.\n\n"
                    "You can start a fresh operation now.\n"
                    "╰━━━━━━━━━━━━━━━━━━━━╯"
                )
            else:
                active_tasks.pop(user_id, None)
                cancel_requested.discard(user_id)
                await query.message.reply_text(
                    "╭━━━〔 🔄 𝗥𝗘𝗦𝗘𝗧 𝗖𝗢𝗠𝗣𝗟𝗘𝗧𝗘 〕━━━╮\n\n"
                    "No active task was running. Pending input has been cleared.\n\n"
                    "✅ Login, Premium and saved customisations are unchanged.\n"
                    "╰━━━━━━━━━━━━━━━━━━━━╯"
                )
        except Exception as e:
            await query.message.reply_text(f"❌ Reset failed: `{e}`")
    elif action == "plan":
        await query.message.reply_text(
            "╭━━━〔 📋 𝗬𝗢𝗨𝗥 𝗣𝗟𝗔𝗡 〕━━━╮\n\n"
            "Use /myplan to see your remaining free uses or Premium expiry.\n\n"
            "💎 Need more access? Use /buypremium.\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯"
        )
