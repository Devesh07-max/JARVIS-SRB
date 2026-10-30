import asyncio
import datetime
import pytz
from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait
from safe_repo import app
from config import OWNER_ID, PLAN_CONFIG
from safe_repo.core.func import get_seconds, get_active_plan, plan_daily_remaining
from safe_repo.core.mongo import plans_db
from safe_repo.core.mongo.users_db import get_users
from safe_repo.core.log_utils import log_event, user_label
from safe_repo.modules.gcast import send_broadcast_message
from safe_repo.modules.stats import build_stats
from safe_repo.modules.speedtest import run_admin_speedtest


def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Premium", callback_data="admin_add"), InlineKeyboardButton("➖ Remove Premium", callback_data="admin_rem")],
        [InlineKeyboardButton("🔎 Check Premium", callback_data="admin_check"), InlineKeyboardButton("👑 Premium List", callback_data="admin_list")],
        [InlineKeyboardButton("📊 Statistics", callback_data="admin_stats"), InlineKeyboardButton("⚡ Speed Test", callback_data="admin_speed")],
        [InlineKeyboardButton("🧾 Plan Guide", callback_data="admin_plans"), InlineKeyboardButton("💳 Payment History", callback_data="admin_payments")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"), InlineKeyboardButton("📣 Announcement", callback_data="admin_announce")],
        [InlineKeyboardButton("💬 Message User", callback_data="admin_pm")],
    ])


async def add_premium_flow(client, admin_id):
    answer = await app.ask(
        admin_id,
        "➕ **Add Premium**\n\n"
        "Send: `USER_ID PLAN DURATION`\n\n"
        "Plans: `basic`, `standard`, `premium`, `ultimate`, `elite`\n\n"
        "Examples:\n"
        "`123456789 standard 7 days`\n"
        "`123456789 premium 1 month`\n"
        "`123456789 ultimate 1 year`\n"
        "`123456789 elite 1 month`"
    )
    parts = (answer.text or '').strip().split()
    if len(parts) < 3:
        return await app.send_message(admin_id, "❌ Invalid format. Example: `123456789 standard 30 days`")
    try:
        user_id = int(parts[0])
    except ValueError:
        return await app.send_message(admin_id, "❌ Invalid User ID.")
    plan = parts[1].lower()
    if plan not in PLAN_CONFIG:
        return await app.send_message(admin_id, "❌ Invalid plan. Use `basic`, `standard`, `premium`, `ultimate` or `elite`.")
    duration = ' '.join(parts[2:])
    seconds = await get_seconds(duration)
    if seconds <= 0:
        return await app.send_message(
            admin_id,
            "❌ Invalid duration. Use `7 days`, `1 week`, `1 month`, `1 year`, etc."
        )
    try:
        user = await client.get_users(user_id)
        expiry = datetime.datetime.utcnow() + datetime.timedelta(seconds=seconds)
        await plans_db.add_premium(user_id, expiry, plan=plan)
        expiry_ist = expiry.replace(tzinfo=pytz.UTC).astimezone(pytz.timezone('Asia/Kolkata'))
        expiry_text = expiry_ist.strftime('%d-%m-%Y %I:%M:%S %p')
        await app.send_message(
            admin_id,
            f"╭━━━〔 ✅ 𝗣𝗥𝗘𝗠𝗜𝗨𝗠 𝗔𝗖𝗧𝗜𝗩𝗔𝗧𝗘𝗗 〕━━━╮\n\n"
            f"👤 {user.mention}\n🆔 `{user_id}`\n💎 Plan: `{duration}`\n⌛ Expires: `{expiry_text}`\n"
            f"╰━━━━━━━━━━━━━━━━━━━━╯"
        )
        try:
            await client.send_message(
                user_id,
                f"╭━━━〔 🎉 𝗣𝗥𝗘𝗠𝗜𝗨𝗠 𝗔𝗖𝗧𝗜𝗩𝗘 〕━━━╮\n\n"
                f"💎 Plan: `{PLAN_CONFIG[plan]['label']}`\n⏳ Duration: `{duration}`\n⌛ Expires: `{expiry_text}`\n\nEnjoy Premium access! 🚀\n"
                f"╰━━━━━━━━━━━━━━━━━━━━╯"
            )
        except Exception:
            pass
        await log_event(
            f"💎 PREMIUM ACTIVATED\n👤 {user_label(user)}\n🆔 `{user_id}`\n"
            f"⏳ Plan: `{duration}`\n⌛ Expiry: `{expiry_text}`"
        )
    except Exception as e:
        await app.send_message(admin_id, f"❌ Could not add Premium.\n\n<blockquote>{e}</blockquote>")


async def remove_premium_flow(client, admin_id):
    answer = await app.ask(admin_id, "➖ Send the User ID whose Premium should be removed.")
    try:
        user_id = int((answer.text or '').strip())
    except ValueError:
        return await app.send_message(admin_id, "❌ Invalid User ID.")
    if not await plans_db.check_premium(user_id):
        return await app.send_message(admin_id, "📭 No active Premium record found.")
    await plans_db.remove_premium(user_id)
    await app.send_message(admin_id, f"✅ Premium removed from `{user_id}`.")
    try:
        await client.send_message(user_id, "⚠️ Your Premium access has been removed by the administrator.")
    except Exception:
        pass


async def check_premium_flow(client, admin_id):
    answer = await app.ask(admin_id, "🔎 Send the User ID to check.")
    try:
        user_id = int((answer.text or '').strip())
    except ValueError:
        return await app.send_message(admin_id, "❌ Invalid User ID.")
    data = await plans_db.check_premium(user_id)
    if not data or not data.get('expire_date'):
        return await app.send_message(admin_id, "📭 No active Premium record found.")
    expiry = data['expire_date']
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=pytz.UTC)
    expiry_ist = expiry.astimezone(pytz.timezone('Asia/Kolkata'))
    remaining = expiry_ist - datetime.datetime.now(pytz.timezone('Asia/Kolkata'))
    if remaining.total_seconds() <= 0:
        await plans_db.remove_premium(user_id)
        return await app.send_message(admin_id, "⌛ Premium has already expired.")
    try:
        name = (await client.get_users(user_id)).mention
    except Exception:
        name = str(user_id)
    plan = await get_active_plan(user_id) or data.get("plan") or "unknown"
    plan_label = PLAN_CONFIG.get(plan, {}).get("label", str(plan).title())
    await app.send_message(
        admin_id,
        f"╭━━━〔 💎 𝗣𝗥𝗘𝗠𝗜𝗨𝗠 𝗦𝗧𝗔𝗧𝗨𝗦 〕━━━╮\n\n"
        f"👤 {name}\n🆔 `{user_id}`\n"
        f"💎 `{plan_label}`\n"
        f"⏳ `{remaining.days}d {remaining.seconds//3600}h {(remaining.seconds%3600)//60}m`\n"
        f"📅 `{expiry_ist.strftime('%d-%m-%Y %I:%M:%S %p')}`\n"
        f"╰━━━━━━━━━━━━━━━━━━━━╯"
    )


async def premium_list_flow(client, admin_id):
    ids = await plans_db.premium_users()
    if not ids:
        return await app.send_message(
            admin_id,
            "╭━━━〔 👑 𝗣𝗥𝗘𝗠𝗜𝗨𝗠 𝗨𝗦𝗘𝗥𝗦 〕━━━╮\n\n"
            "📭 No active Premium users found.\n╰━━━━━━━━━━━━━━━━━━━━╯"
        )
    lines = [
        "╭━━━〔 👑 𝗣𝗥𝗘𝗠𝗜𝗨𝗠 𝗨𝗦𝗘𝗥𝗦 〕━━━╮",
        "",
        f"👥 Active users: `{len(ids)}`",
        "",
    ]
    for idx, uid in enumerate(ids, 1):
        data = await plans_db.check_premium(uid)
        expiry = data.get('expire_date') if data else None
        if expiry and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=pytz.UTC)
        expiry_text = expiry.astimezone(pytz.timezone('Asia/Kolkata')).strftime('%d-%m-%Y %I:%M %p') if expiry else 'Unknown'
        try:
            name = (await client.get_users(uid)).mention
        except Exception:
            name = str(uid)
        lines.append(f"{idx}. {name}\n   🆔 `{uid}`\n   ⏳ `{expiry_text}`")
        if len('\n'.join(lines)) > 3800:
            await app.send_message(admin_id, '\n'.join(lines) + '\n╰━━━━━━━━━━━━━━━━━━━━╯')
            lines = ["╭━━━〔 👑 𝗣𝗥𝗘𝗠𝗜𝗨𝗠 𝗨𝗦𝗘𝗥𝗦 — CONTINUED 〕━━━╮", ""]
    lines.append("╰━━━━━━━━━━━━━━━━━━━━╯")
    await app.send_message(admin_id, '\n'.join(lines))


async def personal_message_flow(client, admin_id):
    """Admin tool for sending a message to one user who has already contacted the bot."""
    answer = await app.ask(admin_id, "💬 **Message One User**\n\nSend the User ID:")
    try:
        user_id = int((answer.text or "").strip())
    except ValueError:
        return await app.send_message(admin_id, "❌ Invalid User ID.")

    try:
        user = await client.get_users(user_id)
        name = user.mention
    except Exception:
        name = str(user_id)

    content = await app.ask(admin_id, f"✉️ Send the message/media for {name}.")
    try:
        await content.copy(user_id)
    except FloodWait as fw:
        await asyncio.sleep(int(fw.value))
        try:
            await content.copy(user_id)
        except Exception as e:
            return await app.send_message(admin_id, f"❌ Delivery failed for `{user_id}`.\n\n<blockquote>{e}</blockquote>")
    except Exception as e:
        return await app.send_message(admin_id, f"❌ Delivery failed for `{user_id}`.\n\n<blockquote>{e}</blockquote>")

    await app.send_message(admin_id, f"✅ **Message sent successfully.**\n\n👤 {name}\n🆔 `{user_id}`")


async def distribution_flow(client, admin_id, announcement=False):
    title = "📣 𝗔𝗡𝗡𝗢𝗨𝗡𝗖𝗘𝗠𝗘𝗡𝗧" if announcement else "📢 𝗕𝗥𝗢𝗔𝗗𝗖𝗔𝗦𝗧"
    await app.send_message(
        admin_id,
        f"╭━━━〔 {title} 〕━━━╮\n\n"
        "Send the message/media you want to distribute.\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯"
    )
    content = await app.ask(admin_id, "Send it now.")
    users = await get_users()
    if not announcement:
        sent, failed = await send_broadcast_message(content, users)
    else:
        sent = failed = 0
        for dest in users:
            try:
                if content.text:
                    await client.send_message(dest, f"📣 **OFFICIAL ANNOUNCEMENT**\n\n{content.text}")
                else:
                    await content.copy(dest, caption=f"📣 **OFFICIAL ANNOUNCEMENT**\n\n{content.caption or ''}")
                sent += 1
            except FloodWait as fw:
                await asyncio.sleep(int(fw.value))
                try:
                    await content.copy(dest)
                    sent += 1
                except Exception:
                    failed += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.1)
    await app.send_message(
        admin_id,
        f"✅ **{title} COMPLETE**\n\n📨 Delivered: `{sent}`\n⚠️ Failed: `{failed}`"
    )


@app.on_message(filters.command("pm") & filters.user(OWNER_ID))
async def pm_user(client, message):
    """Quick shortcut for the same single-user messaging flow."""
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2:
        try:
            user_id = int(parts[1].strip())
        except ValueError:
            return await message.reply_text("❌ Invalid User ID.")
        try:
            user = await client.get_users(user_id)
            name = user.mention
        except Exception:
            name = str(user_id)
        content = await app.ask(message.chat.id, f"✉️ Send the message/media for {name}.")
        try:
            await content.copy(user_id)
            return await message.reply_text(f"✅ **Message sent.**\n\n👤 {name}\n🆔 `{user_id}`")
        except FloodWait as fw:
            await asyncio.sleep(int(fw.value))
            try:
                await content.copy(user_id)
                return await message.reply_text(f"✅ **Message sent after retry.**\n\n👤 {name}\n🆔 `{user_id}`")
            except Exception as e:
                return await message.reply_text(f"❌ Delivery failed.\n\n<blockquote>{e}</blockquote>")
        except Exception as e:
            return await message.reply_text(f"❌ Delivery failed.\n\n<blockquote>{e}</blockquote>")
    await personal_message_flow(client, message.chat.id)


@app.on_message(filters.command("ban") & filters.user(OWNER_ID))
async def ban_user_command(_, message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip().lstrip("-").isdigit():
        return await message.reply_text("Usage: `/ban USER_ID`", quote=True)
    user_id = int(parts[1].strip())
    from motor.motor_asyncio import AsyncIOMotorClient
    from config import MONGO_DB, MONGO_DATABASE_NAME
    bans = AsyncIOMotorClient(MONGO_DB)[MONGO_DATABASE_NAME]["banned_users"]
    if user_id in OWNER_ID:
        return await message.reply_text("❌ You cannot ban a configured bot owner.")
    await bans.update_one({"_id": user_id}, {"$set": {"banned_at": datetime.datetime.now(datetime.timezone.utc), "banned_by": message.from_user.id}}, upsert=True)
    await message.reply_text(f"⛔ User `{user_id}` has been banned.")


@app.on_message(filters.command("unban") & filters.user(OWNER_ID))
async def unban_user_command(_, message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip().lstrip("-").isdigit():
        return await message.reply_text("Usage: `/unban USER_ID`", quote=True)
    user_id = int(parts[1].strip())
    from motor.motor_asyncio import AsyncIOMotorClient
    from config import MONGO_DB, MONGO_DATABASE_NAME
    result = await AsyncIOMotorClient(MONGO_DB)[MONGO_DATABASE_NAME]["banned_users"].delete_one({"_id": user_id})
    await message.reply_text("✅ User unbanned." if result.deleted_count else "ℹ️ This user was not banned.")


@app.on_message(filters.command('admincommands') & filters.user(OWNER_ID))
async def admincommands(_, message):
    await message.reply_text(
        "╭━━━〔 👑 𝗔𝗗𝗠𝗜𝗡 𝗖𝗢𝗡𝗧𝗥𝗢𝗟 〕━━━╮\n\n"
        "Choose an action below.\n╰━━━━━━━━━━━━━━━━━━━━╯",
        reply_markup=admin_keyboard(),
    )


@app.on_callback_query(filters.regex(r'^admin_(add|rem|check|list|stats|speed|plans|payments|broadcast|announce|pm)$'))
async def admin_callback(client, query):
    if query.from_user.id not in OWNER_ID:
        await query.answer('Admin only.', show_alert=True)
        return
    await query.answer()
    admin_id = query.from_user.id
    action = query.data.split('_', 1)[1]
    try:
        if action == 'add':
            await add_premium_flow(client, admin_id)
        elif action == 'rem':
            await remove_premium_flow(client, admin_id)
        elif action == 'check':
            await check_premium_flow(client, admin_id)
        elif action == 'list':
            await premium_list_flow(client, admin_id)
        elif action == 'stats':
            await app.send_message(admin_id, await build_stats(client))
        elif action == 'plans':
            lines = ['╭━━━〔 🧾 PLAN GUIDE 〕━━━╮', '']
            for key, info in PLAN_CONFIG.items():
                limit = 'Unlimited' if info.get('daily_limit') is None else str(info.get('daily_limit'))
                batch = 'Yes' if info.get('batch') else 'No'
                prices = " | ".join(
                    f"{duration.title()}: ₹{info['inr'][duration]} / ${info['usd'][duration]:g}"
                    for duration in ("weekly", "monthly", "yearly")
                )
                feature = " | No footer" if info.get("no_footer") else ""
                lines.append(f"{info['label']}\n   Daily: `{limit}` | /batch: `{batch}`{feature}\n   Price: `{prices}`")
            lines.append('╰━━━━━━━━━━━━━━━━━━━━╯')
            await app.send_message(admin_id, '\n'.join(lines))
        elif action == 'payments':
            from safe_repo.core.payment_service import payment_collection
            records = list(payment_collection.find({}, {"_id": 1, "user_id": 1, "plan": 1, "duration": 1, "currency": 1, "amount": 1, "status": 1, "created_at": 1}).sort("created_at", -1).limit(15))
            if not records:
                await app.send_message(admin_id, "📭 No payment records found yet.")
            else:
                lines = ["╭━━━〔 💳 PAYMENT HISTORY 〕━━━╮", ""]
                for item in records:
                    amount = item.get("amount", 0) / 100
                    lines.append(f"🆔 `{item.get('_id')}`\n👤 `{item.get('user_id')}` | {item.get('plan')} {item.get('duration')}\n💰 {item.get('currency')} {amount:g} | `{item.get('status')}`")
                lines.append("╰━━━━━━━━━━━━━━━━━━━━╯")
                await app.send_message(admin_id, "\n\n".join(lines))
        elif action == 'speed':
            msg = await app.send_message(
                admin_id,
                "╭━━━〔 ⚡ 𝗦𝗣𝗘𝗘𝗗 𝗧𝗘𝗦𝗧 〕━━━╮\n\n"
                "🔄 Testing server speed...\n╰━━━━━━━━━━━━━━━━━━━━╯"
            )
            ping, download, upload = await run_admin_speedtest(timeout=90)
            await msg.edit_text(
                f"╭━━━〔 ⚡ 𝗦𝗣𝗘𝗘𝗗 𝗧𝗘𝗦𝗧 〕━━━╮\n\n"
                f"🏓 Ping: `{ping:.0f} ms`\n"
                f"⬇️ Download: `{download:.2f} Mbps`\n"
                f"⬆️ Upload: `{upload:.2f} Mbps`\n"
                f"╰━━━━━━━━━━━━━━━━━━━━╯"
            )
        elif action == 'broadcast':
            await distribution_flow(client, admin_id, False)
        elif action == 'announce':
            await distribution_flow(client, admin_id, True)
        elif action == 'pm':
            await personal_message_flow(client, admin_id)
    except Exception as e:
        await app.send_message(admin_id, f"❌ **Admin action failed**\n\n<blockquote>{e}</blockquote>")
