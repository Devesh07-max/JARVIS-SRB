"""Razorpay order creation, verification and idempotent subscription activation."""
import hashlib
import hmac
import json
import os
import time
import uuid
import secrets
from datetime import datetime, timedelta, timezone

import razorpay
import requests
from pymongo import MongoClient, ReturnDocument

from config import (
    MONGO_DB, BOT_TOKEN, RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET,
    RAZORPAY_WEBHOOK_SECRET, PAYMENT_LINK_SECRET, PLAN_CONFIG, MONGO_DATABASE_NAME,
)

mongo_sync = MongoClient(MONGO_DB, serverSelectionTimeoutMS=8000)
# Keep payment and premium records isolated in this bot's dedicated database.
_db = mongo_sync.get_database(MONGO_DATABASE_NAME)
premium_collection = _db.get_collection("premium_db")
payment_collection = _db.get_collection("razorpay_payments")

DURATION_DAYS = {"weekly": 7, "monthly": 30, "yearly": 365}


def razorpay_client():
    if not (RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET):
        raise RuntimeError("Razorpay keys are not configured on the server.")
    return razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))


def _link_secret():
    secret = PAYMENT_LINK_SECRET or RAZORPAY_KEY_SECRET
    if not secret:
        raise RuntimeError("Set PAYMENT_LINK_SECRET in Render environment variables.")
    return secret.encode("utf-8")


def create_checkout_token(user_id, plan, duration):
    payload = {"u": int(user_id), "p": plan, "d": duration, "m": "bot", "e": int(time.time()) + 1800}
    return _sign_checkout_payload(payload)


def create_website_checkout_token(plan, duration):
    """Create a public website checkout that issues a one-time redeem code."""
    if plan not in PLAN_CONFIG or duration not in DURATION_DAYS:
        raise ValueError("Invalid plan or duration")
    payload = {"p": plan, "d": duration, "m": "redeem", "e": int(time.time()) + 1800}
    return _sign_checkout_payload(payload)


def _sign_checkout_payload(payload):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    import base64
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    sig = hmac.new(_link_secret(), body.encode(), hashlib.sha256).hexdigest()
    return body + "." + sig


def read_checkout_token(token):
    import base64
    try:
        body, signature = token.split(".", 1)
        expected = hmac.new(_link_secret(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        payload = json.loads(raw)
        if int(payload.get("e", 0)) < int(time.time()):
            return None
        mode = payload.get("m", "bot")  # legacy signed bot links remain valid
        if mode not in ("bot", "redeem") or payload["p"] not in PLAN_CONFIG or payload["d"] not in DURATION_DAYS:
            return None
        if mode == "bot" and int(payload.get("u", 0)) <= 0:
            return None
        payload["m"] = mode
        return payload
    except Exception:
        return None


def amount_for(plan, duration, currency):
    value = PLAN_CONFIG[plan]["inr" if currency == "INR" else "usd"][duration]
    return int(round(value * 100))


def create_order(payload, currency):
    if currency not in ("INR", "USD"):
        raise ValueError("Unsupported currency")
    amount = amount_for(payload["p"], payload["d"], currency)
    order = razorpay_client().order.create({
        "amount": amount,
        "currency": currency,
        "receipt": ("jb" + uuid.uuid4().hex[:18])[:40],
        "notes": {
            "telegram_user_id": str(payload.get("u", "")),
            "plan": payload["p"],
            "duration": payload["d"],
            "source": "website_redeem" if payload.get("m") == "redeem" else "jarvis_save_restricted_bot",
        },
    })
    payment_collection.insert_one({
        "_id": order["id"], "user_id": int(payload["u"]) if payload.get("u") else None,
        "purchase_mode": payload.get("m", "bot"), "plan": payload["p"],
        "duration": payload["d"], "currency": currency, "amount": amount,
        "status": "created", "created_at": datetime.now(timezone.utc),
    })
    return order


def _notify_user(user_id, text):
    if not BOT_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": int(user_id), "text": text, "parse_mode": "HTML"}, timeout=12,
        )
    except Exception as exc:
        print(f"Telegram payment notification failed: {exc}")


def issue_redeem_code(order_id):
    """Create or return the same redeem code for a captured website order."""
    order = payment_collection.find_one({"_id": order_id})
    if not order:
        raise ValueError("Payment order was not found.")
    if order.get("purchase_mode") != "redeem":
        return None
    if order.get("redeem_code"):
        return order["redeem_code"]
    for _ in range(5):
        code = "JARVIS-" + secrets.token_hex(6).upper()
        payment_collection.update_one(
            {"_id": order_id, "redeem_code": {"$exists": False}},
            {"$set": {"redeem_code": code, "status": "redeem_ready", "redeem_created_at": datetime.now(timezone.utc)}},
        )
        current = payment_collection.find_one({"_id": order_id}) or {}
        if current.get("redeem_code"):
            return current["redeem_code"]
    raise RuntimeError("Could not generate a unique redeem code. Please contact support.")


def redeem_premium_code(code, user_id):
    """Bind a paid website code to one Telegram account and activate it once."""
    normalized = (code or "").strip().upper()
    order = payment_collection.find_one({"redeem_code": normalized, "purchase_mode": "redeem"})
    if not order or order.get("status") not in ("redeem_ready", "redeeming", "activating", "activated"):
        return False, "❌ Invalid code. Please check it and try again."
    owner = order.get("redeemed_by")
    if owner and int(owner) != int(user_id):
        return False, "❌ This redeem code has already been used. Each code can be redeemed only once."
    if order.get("status") == "activated" and owner == int(user_id):
        return False, "ℹ️ This code has already been redeemed by your account. Use /myplan to check your Premium."
    if not owner:
        claimed = payment_collection.update_one(
            {"_id": order["_id"], "status": "redeem_ready", "redeemed_by": {"$exists": False}},
            {"$set": {"status": "redeeming", "redeemed_by": int(user_id), "user_id": int(user_id),
                      "redeemed_at": datetime.now(timezone.utc)}},
        )
        if not claimed.modified_count:
            return redeem_premium_code(normalized, user_id)
    else:
        payment_collection.update_one(
            {"_id": order["_id"], "redeemed_by": int(user_id)},
            {"$set": {"user_id": int(user_id)}},
        )
    ok, message = activate_order(order["_id"], order.get("payment_id"))
    return ok, message


def activate_order(order_id, payment_id=None):
    """Apply the paid order once, extending from the later of now/current expiry."""
    order = payment_collection.find_one({"_id": order_id})
    if not order:
        return False, "Payment order was not found. Contact admin."
    if not order.get("user_id"):
        return False, "This website purchase must be redeemed in Telegram first."
    if order.get("status") == "activated":
        return True, "Your Premium plan is already active."
    claim = payment_collection.find_one_and_update(
        {"_id": order_id, "activation_claimed": {"$ne": True}},
        {"$set": {"activation_claimed": True, "status": "activating", "payment_id": payment_id,
                  "paid_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )
    if not claim:
        current = payment_collection.find_one({"_id": order_id}) or {}
        if current.get("status") == "activated":
            return True, "Your Premium plan is already active."
        return False, "Payment received. Activation is being processed; please check /myplan shortly."

    now = datetime.now(timezone.utc)
    duration_ms = DURATION_DAYS[order["duration"]] * 24 * 60 * 60 * 1000
    premium_collection.update_one(
        {"_id": int(order["user_id"])},
        [{"$set": {
            "expire_date": {"$add": [{"$max": [{"$ifNull": ["$expire_date", now]}, now]}, duration_ms]},
            "plan": order["plan"],
            "last_payment_order_id": order_id,
            "updated_at": now,
        }}],
        upsert=True,
    )
    payment_collection.update_one({"_id": order_id}, {"$set": {"status": "activated", "activated_at": now}})
    expiry = premium_collection.find_one({"_id": int(order["user_id"])}, {"expire_date": 1})
    expiry_text = expiry.get("expire_date").strftime("%d-%m-%Y %I:%M %p UTC") if expiry and expiry.get("expire_date") else "updated"
    plan_label = PLAN_CONFIG[order["plan"]]["label"]
    message = (
        "<b>🎉 Premium activated successfully!</b>\n\n"
        f"💎 Plan: <b>{plan_label}</b>\n"
        f"⏳ Duration added: <b>{order['duration'].title()}</b>\n"
        f"📅 New expiry: <b>{expiry_text}</b>\n\n"
        "Use /myplan to check your subscription. Thank you! 🚀"
    )
    _notify_user(order["user_id"], message)
    return True, "Payment verified and Premium activated."


def verify_checkout_payment(token_payload, order_id, payment_id, signature):
    order_filter = {"_id": order_id}
    if token_payload.get("m", "bot") == "bot":
        order_filter["user_id"] = int(token_payload["u"])
    else:
        order_filter["purchase_mode"] = "redeem"
    order = payment_collection.find_one(order_filter)
    if not order:
        raise ValueError("Order does not match this checkout session.")
    if order["plan"] != token_payload["p"] or order["duration"] != token_payload["d"]:
        raise ValueError("Selected plan does not match the order.")
    razorpay_client().utility.verify_payment_signature({
        "razorpay_order_id": order_id,
        "razorpay_payment_id": payment_id,
        "razorpay_signature": signature,
    })
    payment = razorpay_client().payment.fetch(payment_id)
    if payment.get("order_id") != order_id:
        raise ValueError("Payment order mismatch.")
    if int(payment.get("amount", -1)) != int(order["amount"]) or payment.get("currency") != order["currency"]:
        raise ValueError("Payment amount or currency mismatch.")
    if payment.get("status") != "captured" and not payment.get("captured"):
        payment_collection.update_one({"_id": order_id}, {"$set": {"status": "pending_capture", "payment_id": payment_id}})
        return False, "Payment is not captured yet. Please wait a moment and check /myplan."
    if order.get("purchase_mode") == "redeem":
        code = issue_redeem_code(order_id)
        return True, "Payment verified. Your redeem code is ready: " + code
    return activate_order(order_id, payment_id)


def verify_webhook_signature(raw_body, signature):
    if not RAZORPAY_WEBHOOK_SECRET:
        return False
    expected = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def process_webhook_event(event):
    payload = event.get("payload", {}).get("payment", {}).get("entity", {})
    if not payload:
        return
    order_id = payload.get("order_id")
    payment_id = payload.get("id")
    if not order_id:
        return
    if event.get("event") == "payment.captured":
        order = payment_collection.find_one({"_id": order_id})
        if order and int(payload.get("amount", -1)) == int(order["amount"]) and payload.get("currency") == order["currency"]:
            if order.get("purchase_mode") == "redeem":
                issue_redeem_code(order_id)
            else:
                activate_order(order_id, payment_id)
    elif event.get("event") == "payment.failed":
        payment_collection.update_one(
            {"_id": order_id, "status": {"$nin": ["activated", "activating"]}},
            {"$set": {"status": "failed", "payment_id": payment_id, "failed_at": datetime.now(timezone.utc)}},
        )
