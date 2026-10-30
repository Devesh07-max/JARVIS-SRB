from pyrogram import filters
from safe_repo import app
from safe_repo.core.func import trial_remaining, get_active_plan, plan_daily_remaining
from safe_repo.core.mongo import plans_db
from safe_repo.core.mongo import users_db
from config import FREE_TRIAL_USES, PLAN_CONFIG
import datetime, pytz

@app.on_message(filters.command("myplan"))
async def myplan(client, message):
    user_id = message.from_user.id
    user = message.from_user.mention
    data = await plans_db.check_premium(user_id)
    if data and data.get("expire_date"):
        expiry = data["expire_date"]
        if expiry.tzinfo is None: expiry = expiry.replace(tzinfo=pytz.UTC)
        expiry_ist = expiry.astimezone(pytz.timezone("Asia/Kolkata"))
        remaining = expiry_ist - datetime.datetime.now(pytz.timezone("Asia/Kolkata"))
        if remaining.total_seconds() > 0:
            d = remaining.days; h = remaining.seconds // 3600; m = (remaining.seconds % 3600) // 60
            expiry_text = expiry_ist.strftime('%d-%m-%Y') + "\n⏱️ ᴇxᴘɪʀʏ ᴛɪᴍᴇ : " + expiry_ist.strftime('%I:%M:%S %p')
            plan = await get_active_plan(user_id) or "premium"
            label = PLAN_CONFIG.get(plan, {}).get("label", plan.title())
            remaining_quota = await plan_daily_remaining(user_id)
            quota_text = "Unlimited" if remaining_quota is None else str(remaining_quota)
            feature_text = (
                "\n✨ **Special feature:** No watermark / no auto footer"
                if PLAN_CONFIG.get(plan, {}).get("no_footer")
                else ""
            )
            await message.reply_text(
                f"╭━━━〔 💎 𝗣𝗟𝗔𝗡 𝗦𝗧𝗔𝗧𝗨𝗦 〕━━━╮\n\n👤 **User:** {user}\n🆔 **User ID:** <code>{user_id}</code>\n💎 **Plan:** {label}\n📊 **Daily quota left:** `{quota_text}`{feature_text}\n⏳ **Time left:** {d} days, {h} hours, {m} minutes\n📅 **Expiry:** {expiry_text}\n╰━━━━━━━━━━━━━━━━━━━━╯"
            )
            return
        await plans_db.remove_premium(user_id)
    remaining = await trial_remaining(user_id)
    if remaining >= 0:
        referral = await users_db.get_referral_stats(user_id)
        await message.reply_text(
            f"╭━━━〔 🎁 𝗙𝗥𝗘𝗘 𝗧𝗥𝗜𝗔𝗟 〕━━━╮\n\n👋 **Hello, {user}!**\n\n"
            f"🆓 **Remaining:** `{remaining}` free uses\n"
            f"🎁 **Referral tokens remaining:** `{referral['tokens_remaining']}`\n\n"
            f"💎 Use /buypremium to view Premium plans.\n╰━━━━━━━━━━━━━━━━━━━━╯"
        )
    else:
        await message.reply_text(
            f"╭━━━〔 📋 𝗣𝗟𝗔𝗡 𝗦𝗧𝗔𝗧𝗨𝗦 〕━━━╮\n\n👋 **Hello, {user}!**\n\nYou do not currently have an active Premium plan.\n\n💎 Use /buypremium to view available plans.\n╰━━━━━━━━━━━━━━━━━━━━╯"
        )
