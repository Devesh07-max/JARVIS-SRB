"""Razorpay order creation, verification and idempotent subscription activation."""
import hashlib
import hmac
import json
import os
import time
import uuid
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
    payload = {"u": int(user_id), "p": plan, "d": duration, "e": int(time.time()) + 1800}
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
        if int(payload["u"]) <= 0 or payload["p"] not in PLAN_CONFIG or payload["d"] not in DURATION_DAYS:
            return None
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
            "telegram_user_id": str(payload["u"]),
            "plan": payload["p"],
            "duration": payload["d"],
            "source": "jarvis_save_restricted_bot",
        },
    })
    payment_collection.insert_one({
        "_id": order["id"], "user_id": int(payload["u"]), "plan": payload["p"],
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


def activate_order(order_id, payment_id=None):
    """Apply the paid order once, extending from the later of now/current expiry."""
    order = payment_collection.find_one({"_id": order_id})
    if not order:
        return False, "Payment order was not found. Contact admin."
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
    order = payment_collection.find_one({"_id": order_id, "user_id": int(token_payload["u"])})
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
            activate_order(order_id, payment_id)
    elif event.get("event") == "payment.failed":
        payment_collection.update_one(
            {"_id": order_id, "status": {"$nin": ["activated", "activating"]}},
            {"$set": {"status": "failed", "payment_id": payment_id, "failed_at": datetime.now(timezone.utc)}},
        )
