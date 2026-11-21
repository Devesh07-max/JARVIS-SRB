"""PayPal Orders v2 integration using server-side REST verification."""
from datetime import datetime, timezone
from decimal import Decimal
import requests

from config import PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET, PAYPAL_BASE_URL, PLAN_CONFIG
from safe_repo.core.payment_service import payment_collection, activate_order


def _configured():
    return bool(PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET)


def _access_token():
    if not _configured():
        raise RuntimeError("PayPal is not configured. Add PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET.")
    response = requests.post(
        f"{PAYPAL_BASE_URL}/v1/oauth2/token",
        auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        headers={"Accept": "application/json", "Accept-Language": "en_US"},
        timeout=20,
    )
    response.raise_for_status()
    token = response.json().get("access_token")
    if not token:
        raise RuntimeError("PayPal did not return an access token.")
    return token


def _request(method, path, *, json_body=None, request_id=None):
    token = _access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if request_id:
        headers["PayPal-Request-Id"] = request_id
    response = requests.request(
        method, f"{PAYPAL_BASE_URL}{path}", headers=headers,
        json=json_body, timeout=30,
    )
    if response.status_code >= 400:
        try:
            detail = response.json().get("message") or response.text
        except Exception:
            detail = response.text
        raise RuntimeError(f"PayPal API error ({response.status_code}): {str(detail)[:350]}")
    return response.json() if response.content else {}


def create_paypal_order(payload):
    if payload.get("m", "bot") != "bot" or not payload.get("u"):
        raise ValueError("PayPal checkout is available only for a Telegram bot purchase.")
    plan, duration = payload["p"], payload["d"]
    price = Decimal(str(PLAN_CONFIG[plan]["usd"][duration])).quantize(Decimal("0.01"))
    value = f"{price:.2f}"
    order = _request("POST", "/v2/checkout/orders", json_body={
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": f"{payload['u']}:{plan}:{duration}",
            "custom_id": str(payload["u"]),
            "description": f"JARVIS SRB {plan.title()} - {duration.title()} Premium",
            "amount": {"currency_code": "USD", "value": value},
        }],
        "application_context": {
            "brand_name": "JARVIS SRB",
            "user_action": "PAY_NOW",
            "shipping_preference": "NO_SHIPPING",
        },
    })
    order_id = order.get("id")
    if not order_id:
        raise RuntimeError("PayPal did not return an order ID.")
    payment_collection.insert_one({
        "_id": order_id,
        "user_id": int(payload["u"]),
        "purchase_mode": "bot",
        "provider": "paypal",
        "plan": plan,
        "duration": duration,
        "currency": "USD",
        "amount": value,
        "status": "created",
        "created_at": datetime.now(timezone.utc),
    })
    return order


def _activate_captured(order_doc, paypal_order):
    units = paypal_order.get("purchase_units") or []
    captures = []
    for unit in units:
        for capture in (unit.get("payments") or {}).get("captures", []):
            captures.append(capture)
    for capture in captures:
        amount = capture.get("amount") or {}
        expected = Decimal(str(order_doc["amount"])).quantize(Decimal("0.01"))
        actual = Decimal(str(amount.get("value", "0"))).quantize(Decimal("0.01"))
        if (capture.get("status") == "COMPLETED" and
                amount.get("currency_code") == "USD" and actual == expected):
            return activate_order(order_doc["_id"], capture.get("id"))
    return False, "PayPal payment is not captured yet. Please wait and verify again."


def capture_paypal_order(order_id, user_id, plan, duration):
    order_doc = payment_collection.find_one({
        "_id": order_id, "user_id": int(user_id), "provider": "paypal",
        "plan": plan, "duration": duration,
    })
    if not order_doc:
        raise ValueError("PayPal order does not match this checkout session.")
    if order_doc.get("status") == "activated":
        return True, "Your Premium plan is already active."
    result = _request("POST", f"/v2/checkout/orders/{order_id}/capture", json_body={})
    ok, message = _activate_captured(order_doc, result)
    if ok:
        payment_collection.update_one({"_id": order_id}, {"$set": {"provider": "paypal", "paypal_status": "COMPLETED"}})
    return ok, message


def recover_paypal_order(user_id, plan, duration):
    order_doc = payment_collection.find_one(
        {"user_id": int(user_id), "provider": "paypal", "plan": plan, "duration": duration},
        sort=[("created_at", -1)],
    )
    if not order_doc:
        return False, "No PayPal checkout order was found. Please select PayPal first."
    if order_doc.get("status") == "activated":
        return True, "Your Premium plan is already active. Use /myplan to check it."
    try:
        current = _request("GET", f"/v2/checkout/orders/{order_doc['_id']}")
        if current.get("status") == "APPROVED":
            return capture_paypal_order(order_doc["_id"], user_id, plan, duration)
        if current.get("status") == "COMPLETED":
            ok, message = _activate_captured(order_doc, current)
            return ok, message
        return False, "PayPal has not completed this payment yet. Finish checkout, then tap Verify Payment again."
    except Exception as exc:
        print(f"PayPal verification error for {order_doc['_id']}: {exc}")
        return False, "Could not verify PayPal right now. Please try again shortly or contact support."
