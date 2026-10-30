from pyrogram import filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from urllib.parse import quote

from safe_repo import app
from safe_repo.core.mongo import users_db


@app.on_message(filters.command("earntokens"))
async def earn_tokens(_, message):
    user_id = message.from_user.id
    try:
        bot = await app.get_me()
        username = bot.username
        stats = await users_db.get_referral_stats(user_id)
        link = f"https://t.me/{username}?start=ref_{user_id}"
        share_url = f"https://t.me/share/url?url={quote(link, safe='')}"
        await message.reply_text(
            "╭━━━〔 🎁 𝗘𝗔𝗥𝗡 𝗧𝗢𝗞𝗘𝗡𝗦 〕━━━╮\n\n"
            "Invite new users and earn **+2 trial tokens** for every valid referral.\n\n"
            f"🔗 **Your personal referral link:**\n`{link}`\n\n"
            "Share the link with new Telegram users. A referral counts only "
            "when that user actually starts the bot through your link.\n\n"
            f"👥 Successful referrals: `{stats['successful_referrals']}`\n"
            f"➕ Tokens earned: `{stats['tokens_earned']}`\n"
            f"➖ Tokens used: `{stats['tokens_used']}`\n"
            f"🔋 Tokens remaining: `{stats['tokens_remaining']}`\n\n"
            "Each successful referral adds **2 trial uses**. Premium plans "
            "are not changed by referrals.\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📤 Share Referral Link", url=share_url)],
            ]),
            disable_web_page_preview=True,
        )
    except Exception as exc:
        await message.reply_text(f"❌ Could not load referral details.\n\n`{str(exc)[:300]}`")