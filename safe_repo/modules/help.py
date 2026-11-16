from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from safe_repo import app
from config import PREMIUM_URL, SUPPORT_URL, FREE_TRIAL_USES, BATCH_SIZE

HELP_TEXT = f"""╭━━━〔 📚 𝗛𝗘𝗟𝗣 𝗖𝗘𝗡𝗧𝗘𝗥 〕━━━╮

👋 **Welcome!**
Here is everything you need to get started.

╭─〔 👤 𝗨𝗦𝗘𝗥 𝗖𝗢𝗠𝗠𝗔𝗡𝗗𝗦 〕
│
│ 🚀 /start — Open the main menu.
│ 📚 /help — Open this guide.
│ 🆔 /id — Get your Telegram User ID.
│ 🔐 /login — Connect your Telegram account.
│ 🚪 /logout — Remove your saved session.
│ 📋 /myplan — Check trial/Premium status.
│ 🎁 /earntokens — Get your referral link and token balance.
│ 💎 /premium — View Premium information.
│ 🛒 /buypremium — View plans & buying instructions.
│ 📦 /batch — Process a range of messages.
│ 🌐 Send a supported YouTube/Instagram/other media URL to download it.
│ 🤖 /dlbots — Browse media in your other Telegram bot chats (login required).
│ ⛔ /cancel — Stop an active batch/file.
│ 🔄 /reset — Force-reset a stuck/pending task.
│ ⚙️ /customisations — Manage saved preferences.
│
╰────────────────────

╭─〔 🔗 𝗦𝗘𝗡𝗗 𝗔 𝗟𝗜𝗡𝗞 〕
│
│ For one message, simply send its Telegram link.
│ For external media, send the supported platform URL directly.
│
│ **Example:**
│ `https://t.me/channel/123`
│
│ 🔐 For a private source, use /login first and make sure the logged-in account can access that message.
│
╰────────────────────

╭─〔 📦 𝗕𝗔𝗧𝗖𝗛 𝗠𝗢𝗗𝗘 〕
│
│ 1️⃣ Send /batch
│ 2️⃣ Send the **start link**
│    `https://t.me/channel/100`
│ 3️⃣ Send the **end link**
│    `https://t.me/channel/110`
│
│ The bot will process messages **100 → 110**.
│ 📌 Maximum range: **{BATCH_SIZE} messages**.
│ ⛔ Use /cancel to stop it.
│ 🔄 If the bot gets stuck, use /reset to force-clear the current task.
│
╰────────────────────

╭─〔 🎁 𝗙𝗥𝗘𝗘 𝗧𝗥𝗜𝗔𝗟 〕
│
│ 🆓 Every new account receives **{FREE_TRIAL_USES} free uses**.
│ 📋 Check remaining uses with /myplan.
│ 💎 When the trial ends, use /buypremium for continued access.
│
╰────────────────────

╭─〔 💎 𝗣𝗥𝗘𝗠𝗜𝗨𝗠 〕
│
│ Use /buypremium to view available plans, prices and purchase instructions.
│
╰────────────────────

╭─〔 ⚠️ 𝗜𝗠𝗣𝗢𝗥𝗧𝗔𝗡𝗧 〕
│
│ • Send valid Telegram message links.
│ • Private sources require legitimate account access.
│ • Never share your OTP, password or session string.
│ • If something fails, verify the link and try again.
│
╰━━━━━━━━━━━━━━━━━━━━╯

💬 Commands shown above can be tapped directly in Telegram.

💬 Need help or Premium? Use the buttons below.
"""

BUTTONS = InlineKeyboardMarkup([
    [InlineKeyboardButton("💎 Buy Premium", url=PREMIUM_URL)],
    [InlineKeyboardButton("💬 Support", url=SUPPORT_URL)],
])


@app.on_message(filters.command("help"))
async def help_command(_, message):
    await message.reply_text(
        HELP_TEXT,
        reply_markup=BUTTONS,
        disable_web_page_preview=True,
    )

