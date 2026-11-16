import asyncio
from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from safe_repo import app
from config import PREMIUM_URL, FREE_TRIAL_USES, PLAN_CONFIG, PUBLIC_BASE_URL
from safe_repo.core.payment_service import create_checkout_token, redeem_premium_code, verify_latest_order_payment, verify_latest_paypal_payment
from config import PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET

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
    rows.extend([[InlineKeyboardButton("💬 Contact Admin", url=PREMIUM_URL)], [InlineKeyboardButton("⬅️ Back to Plans", callback_data="buyplans:back")]])
    return InlineKeyboardMarkup(rows)


def payment_methods_keyboard(key, duration):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇮🇳 UPI", callback_data=f"paymethod:upi:{key}:{duration}")],
        [InlineKeyboardButton("💳 Debit/Credit Card & Apple Pay", callback_data=f"paymethod:card:{key}:{duration}")],
        [InlineKeyboardButton("🅿️ PayPal", callback_data=f"paymethod:paypal:{key}:{duration}")],
        [InlineKeyboardButton("🎧 Need Support", url=PREMIUM_URL)],
        [InlineKeyboardButton("« Back to Plans", callback_data="buyplans:back")],
    ])


def selected_method_keyboard(url, key, duration, method):
    pay_label = "🇮🇳 Pay with UPI" if method == "upi" else "💳 Pay with Card / Apple Pay"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(pay_label, url=url)],
        [InlineKeyboardButton("✅ Verify Payment", callback_data=f"payverify:{key}:{duration}")],
        [InlineKeyboardButton("🎧 Need Support", url=PREMIUM_URL)],
        [InlineKeyboardButton("« Back to Payment Methods", callback_data=f"payplan:{key}:{duration}")],
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
    lines.append("👇 Select a plan below. INR/USD checkout currency can be selected on the payment page.")
    return "\n".join(lines)

PREMIUM_TEXT = build_plans_text()


@app.on_message(filters.command(["premium", "buypremium"]))
async def premium_info(_, message):
    await message.reply_text(PREMIUM_TEXT, reply_markup=premium_keyboard(), disable_web_page_preview=True)


@app.on_message(filters.command("redeempremium"))
async def redeem_premium(_, message):
    website_url = (PUBLIC_BASE_URL or "https://jarvis-srb.onrender.com").rstrip("/")
    if len(message.command) < 2:
        text = (
            "🎟️ **Redeem Premium Code**\n\n"
            "Is command ka use website se purchase karne ke baad mila hua unique code redeem karne ke liye hota hai.\n\n"
            "**Kaise redeem karein?**\n"
            "1️⃣ Website ke Pricing page par plan aur duration choose karke payment complete karein.\n"
            "2️⃣ Payment ke baad mila code copy karein.\n"
            "3️⃣ Yahan is format mein bhejein:\n"
            "`/redeempremium JARVIS-XXXXXXXXXXXX`\n\n"
            "✅ Code verify hote hi aapka purchased Premium plan activate ho jayega.\n"
            "⚠️ Har code sirf ek baar aur ek Telegram account par redeem ho sakta hai.\n\n"
            "Agar abhi purchase nahi kiya hai, website par plans aur direct payment available hain."
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🌐 Visit Website & Buy Premium", url=f"{website_url}/pricing")]])
        return await message.reply_text(text, reply_markup=keyboard, disable_web_page_preview=True)

    code = message.command[1].strip()
    if len(code) > 80:
        return await message.reply_text("❌ Invalid redeem code format. Please send the code exactly as shown on the website.")
    try:
        ok, result = redeem_premium_code(code, message.from_user.id)
    except Exception as exc:
        print(f"Premium code redemption failed: {exc}")
        return await message.reply_text("⚠️ Code redemption could not be completed right now. Please try again shortly or contact support.")
    await message.reply_text(result, disable_web_page_preview=True)


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
    text = (
        f"╭━━━〔 💎 𝗣𝗥𝗘𝗠𝗜𝗨𝗠 𝗖𝗛𝗘𝗖𝗞𝗢𝗨𝗧 〕━━━╮\n\n"
        f"🏷️ **Plan:** {info['label']}\n"
        f"📦 **Access:** {plan_summary(info)}\n"
        f"⏳ **Duration:** {DURATION_LABELS[duration]}\n"
        f"🇮🇳 **UPI / India price:** ₹{info['inr'][duration]}\n"
        f"🌍 **International price:** ${info['usd'][duration]:g}\n\n"
        "✨ Please select your preferred payment method below.\n\n"
        "✅ Payment is verified securely before Premium is activated.\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯"
    )
    await query.message.edit_text(text, reply_markup=payment_methods_keyboard(key, duration), disable_web_page_preview=True)


@app.on_callback_query(filters.regex(r"^paymethod:(upi|card|paypal):(basic|standard|premium|ultimate|elite):(weekly|monthly|yearly)$"))
async def select_payment_method(_, query):
    _, method, key, duration = query.data.split(":", 3)
    await query.answer()
    if method == "paypal":
        if not (PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET and PUBLIC_BASE_URL):
            return await query.message.edit_text(
                "🅿️ **PayPal checkout is not configured yet.**\n\nThe administrator must add PayPal Business REST API credentials and PUBLIC_BASE_URL in Render before this gateway can accept payments.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🇮🇳 UPI", callback_data=f"paymethod:upi:{key}:{duration}")],
                    [InlineKeyboardButton("💳 Debit/Credit Card & Apple Pay", callback_data=f"paymethod:card:{key}:{duration}")],
                    [InlineKeyboardButton("🎧 Need Support", url=PREMIUM_URL)],
                    [InlineKeyboardButton("« Back to Payment Methods", callback_data=f"payplan:{key}:{duration}")],
                ]),
            )
        try:
            paypal_token = create_checkout_token(query.from_user.id, key, duration)
            paypal_url = f"{PUBLIC_BASE_URL}/paypal/checkout/{paypal_token}"
        except Exception as exc:
            print(f"Could not create PayPal checkout link: {exc}")
            return await query.message.reply_text("⚠️ PayPal checkout is temporarily unavailable. Please contact support.")
        return await query.message.edit_text(
            f"╭━━━〔 🅿️ 𝗣𝗔𝗬𝗣𝗔𝗟 𝗖𝗛𝗘𝗖𝗞𝗢𝗨𝗧 〕━━━╮\n\n"
            f"🏷️ **Plan:** {PLAN_CONFIG[key]['label']}\n"
            f"⏳ **Duration:** {DURATION_LABELS[duration]}\n"
            f"🌍 **PayPal amount:** ${PLAN_CONFIG[key]['usd'][duration]:.2f} USD\n\n"
            "Tap Pay with PayPal and approve the payment on PayPal's secure checkout.\n"
            "Premium activates after the capture is verified. If confirmation is delayed, return and tap Verify Payment.\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🅿️ Pay with PayPal", url=paypal_url)],
                [InlineKeyboardButton("✅ Verify Payment", callback_data=f"payverify:paypal:{key}:{duration}")],
                [InlineKeyboardButton("🎧 Need Support", url=PREMIUM_URL)],
                [InlineKeyboardButton("« Back to Payment Methods", callback_data=f"payplan:{key}:{duration}")],
            ]),
            disable_web_page_preview=True,
        )
    if not PUBLIC_BASE_URL:
        return await query.message.edit_text(
            "⚠️ Online payment is not configured yet. Please contact Admin.\n\nThe administrator needs to set PUBLIC_BASE_URL and Razorpay credentials in Render.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎧 Need Support", url=PREMIUM_URL)], [InlineKeyboardButton("« Back to Payment Methods", callback_data=f"payplan:{key}:{duration}")]])
        )
    try:
        token = create_checkout_token(query.from_user.id, key, duration)
        checkout_url = f"{PUBLIC_BASE_URL}/checkout/{token}"
    except Exception as exc:
        print(f"Could not create checkout link: {exc}")
        return await query.message.reply_text("⚠️ Payment is temporarily unavailable. Please contact Admin.")
    method_name = "UPI" if method == "upi" else "Debit/Credit Card & Apple Pay"
    text = (
        f"╭━━━〔 {'🇮🇳' if method == 'upi' else '💳'} 𝗣𝗔𝗬𝗠𝗘𝗡𝗧 〕━━━╮\n\n"
        f"🏷️ **Plan:** {PLAN_CONFIG[key]['label']}\n"
        f"⏳ **Duration:** {DURATION_LABELS[duration]}\n"
        f"💳 **Selected method:** {method_name}\n\n"
        "🚀 Tap the payment button below. Razorpay's secure checkout will open.\n"
        "🌍 Choose INR or USD on the checkout page; the existing currency and pricing system is unchanged.\n\n"
        "✅ Premium activates automatically after Razorpay confirms the captured payment.\n"
        "⚠️ If activation is delayed, return here and tap Verify Payment. Do not pay again immediately.\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯"
    )
    await query.message.edit_text(text, reply_markup=selected_method_keyboard(checkout_url, key, duration, method), disable_web_page_preview=True)


@app.on_callback_query(filters.regex(r"^payverify:(basic|standard|premium|ultimate|elite):(weekly|monthly|yearly)$"))
async def verify_payment_from_bot(_, query):
    _, key, duration = query.data.split(":", 2)
    await query.answer("Checking Razorpay payment...")
    try:
        ok, result = await asyncio.to_thread(verify_latest_order_payment, query.from_user.id, key, duration)
    except Exception as exc:
        print(f"Manual payment verification failed for {query.from_user.id}: {exc}")
        ok, result = False, "Could not verify the payment right now. Please try again shortly or contact support."
    await query.message.reply_text(("✅ **" if ok else "ℹ️ **") + result + ("**" if ok else ""), disable_web_page_preview=True)


@app.on_callback_query(filters.regex(r"^payverify:paypal:(basic|standard|premium|ultimate|elite):(weekly|monthly|yearly)$"))
async def verify_paypal_from_bot(_, query):
    _, _, key, duration = query.data.split(":", 3)
    await query.answer("Checking PayPal payment...")
    try:
        ok, result = await asyncio.to_thread(verify_latest_paypal_payment, query.from_user.id, key, duration)
    except Exception as exc:
        print(f"Manual PayPal verification failed for {query.from_user.id}: {exc}")
        ok, result = False, "Could not verify PayPal right now. Please try again shortly or contact support."
    await query.message.reply_text(("✅ **" if ok else "ℹ️ **") + result + ("**" if ok else ""), disable_web_page_preview=True)
