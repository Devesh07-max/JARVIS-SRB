import os
from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from safe_repo import app
from config import PREMIUM_URL, FREE_TRIAL_USES, PLAN_CONFIG, PAYMENT_QR_PATH

PLAN_ORDER = ["basic", "standard", "premium", "ultimate", "elite"]
DURATION_LABELS = {"weekly": "🔹 Weekly", "monthly": "📅 Monthly", "yearly": "🔥 Yearly"}


def _price_text(info, duration):
    return f"₹{info['inr'][duration]} / ${info['usd'][duration]:g}"


def premium_keyboard():
    rows = [[InlineKeyboardButton(PLAN_CONFIG[key]["label"], callback_data=f"buyplan:{key}")] for key in PLAN_ORDER]
    rows.append([InlineKeyboardButton("💬 Contact Admin", url=PREMIUM_URL)])
    return InlineKeyboardMarkup(rows)


def duration_keyboard(key):
    info = PLAN_CONFIG[key]
    rows = [[InlineKeyboardButton(f"{DURATION_LABELS[d]} — {_price_text(info, d)}", callback_data=f"payplan:{key}:{d}")] for d in ("weekly", "monthly", "yearly")]
    rows.append([InlineKeyboardButton("⬅️ Back to Plans", callback_data="buyplans:back")])
    return InlineKeyboardMarkup(rows)


def manual_payment_keyboard(key):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💵 Want to pay in dollars? Contact Admin", url=PREMIUM_URL)],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"buyplan:{key}")],
    ])


def plan_summary(info):
    limit = "Unlimited ♾️" if info.get("daily_limit") is None else f"{info['daily_limit']} links/day"
    batch = "+ /batch" if info.get("batch") else ""
    if info.get("range"):
        batch += " + range"
    footer = "+ no watermark / no auto footer" if info.get("no_footer") else ""
    return f"{limit} {batch} {footer}".strip()


def build_plans_text():
    lines = ["━━━━━━━━━━━━━━━━━━━━━━━━", ""]
    for key in PLAN_ORDER:
        info = PLAN_CONFIG[key]
        lines.append(f"{info['label']} — {plan_summary(info)}")
        lines.append(f"├ 🔹 Weekly: {_price_text(info, 'weekly')}")
        lines.append(f"├ 📅 Monthly: {_price_text(info, 'monthly')}")
        lines.append(f"└ 🔥 Yearly: {_price_text(info, 'yearly')}")
        lines.extend(["", "━━━━━━━━━━━━━━━━━━━━━━━━", ""])
    lines.append(f"🎁 New users get **{FREE_TRIAL_USES} free uses**.")
    lines.append("👇 Select a plan below.")
    return "\n".join(lines)


PREMIUM_TEXT = build_plans_text()


@app.on_message(filters.command(["premium", "buypremium"]))
async def premium_info(_, message):
    await message.reply_text(PREMIUM_TEXT, reply_markup=premium_keyboard(), disable_web_page_preview=True)


@app.on_callback_query(filters.regex(r"^buyplan:(basic|standard|premium|ultimate|elite)$"))
async def choose_plan(_, query):
    key = query.data.split(":", 1)[1]
    info = PLAN_CONFIG[key]
    await query.answer()
    text = (f"╭━━━〔 {info['label']} 〕━━━╮\n\n"
            f"📦 **Access:** {plan_summary(info)}\n\n"
            "👇 Choose your duration:\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯")
    await query.message.edit_text(text, reply_markup=duration_keyboard(key))


@app.on_callback_query(filters.regex(r"^buyplans:back$"))
async def back_to_plans(_, query):
    await query.answer()
    await query.message.edit_text(PREMIUM_TEXT, reply_markup=premium_keyboard(), disable_web_page_preview=True)


@app.on_callback_query(filters.regex(r"^payplan:(basic|standard|premium|ultimate|elite):(weekly|monthly|yearly)$"))
async def payment_plan(_, query):
    _, key, duration = query.data.split(":", 2)
    info = PLAN_CONFIG[key]
    await query.answer()
    user_id = query.from_user.id
    text = (
        f"╭━━━〔 💳 𝗣𝗔𝗬𝗠𝗘𝗡𝗧 〕━━━╮\n\n"
        f"🏷️ **Plan:** {info['label']}\n"
        f"📦 **Access:** {plan_summary(info)}\n"
        f"⏳ **Duration:** {DURATION_LABELS[duration]}\n"
        f"💰 **Price:** ₹{info['inr'][duration]} / ${info['usd'][duration]:g}\n\n"
        "1️⃣ Scan the QR below.\n"
        "2️⃣ Complete the UPI payment.\n"
        "3️⃣ Send the payment screenshot to the Admin.\n"
        f"4️⃣ Include your User ID: `{user_id}`\n\n"
        "✅ Payment verification is required before activation.\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯"
    )
    keyboard = manual_payment_keyboard(key)
    qr_path = os.path.expanduser(PAYMENT_QR_PATH)
    if not os.path.isabs(qr_path):
        qr_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), qr_path)
    if os.path.isfile(qr_path):
        await query.message.reply_photo(
            photo=qr_path,
            caption=text,
            reply_markup=keyboard,
        )
    else:
        await query.message.reply_text(
            text + "\n\n⚠️ Payment QR image is missing. Please contact Admin.",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    try:
        await query.message.delete()
    except Exception:
        pass
