# safe_repo configuration
from os import getenv
import os

def _int_env(name, default=0):
    value = getenv(name, str(default)).strip()
    return int(value) if value else default

API_ID = _int_env("API_ID", 0)
API_HASH = getenv("API_HASH", "").strip()
BOT_TOKEN = getenv("BOT_TOKEN", "").strip()

def _csv_int_env(name, default=""):
    raw = getenv(name, default).strip()
    if not raw:
        return []
    values = []
    for item in raw.split(","):
        item = item.strip()
        if item:
            try:
                values.append(int(item))
            except ValueError:
                raise ValueError(f"{name} must contain comma-separated integer IDs")
    return values

OWNER_ID = _csv_int_env("OWNER_ID", "")
MONGO_DB = getenv("MONGO_DB", "").strip()
# All JARVIS-SRB application data is isolated in this database.
MONGO_DATABASE_NAME = "jarvis_srb_new"
LOG_GROUP = _int_env("LOG_GROUP", 0)

# Force-subscribe channels. CHANNEL_ID is intentionally updated to the new
# primary channel requested for deployment.
CHANNEL_ID = _int_env("CHANNEL_ID", -1004427245780)
CHANNEL_ID_2 = _int_env("CHANNEL_ID_2", -1004421326321)
CHANNEL_IDS = [x for x in (CHANNEL_ID, CHANNEL_ID_2) if x]

PREMIUM_CONTACT = "@PremiumPurchaseRobot"
PREMIUM_URL = "https://t.me/PremiumPurchaseRobot"
SUPPORT_URL = "https://t.me/PremiumPurchaseRobot"

PREMIUM_PLAN_1 = "10 links/day"
PREMIUM_PLAN_2 = "50 links/day + /batch"
PREMIUM_PLAN_3 = "100 links/day + /batch"
PREMIUM_PLAN_4 = "Unlimited ♾️ + /batch + range"
PREMIUM_PLAN_5 = ""
PREMIUM_PLAN_6 = ""

# Razorpay checkout configuration. Set these in Render Environment Variables.
RAZORPAY_KEY_ID = getenv("RAZORPAY_KEY_ID", "").strip()
RAZORPAY_KEY_SECRET = getenv("RAZORPAY_KEY_SECRET", "").strip()
RAZORPAY_WEBHOOK_SECRET = getenv("RAZORPAY_WEBHOOK_SECRET", "").strip()
PAYMENT_LINK_SECRET = getenv("PAYMENT_LINK_SECRET", "").strip()
PUBLIC_BASE_URL = getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")

PREMIUM_PURCHASE_TEXT = """💎 Premium Purchase

Choose a plan, select its duration and tap Pay Here. Complete payment securely through Razorpay. Your plan will activate automatically after payment verification.

If a payment fails or is cancelled, no plan will be activated. You can retry payment from the bot.
"""

FREE_TRIAL_USES = 10
BATCH_SIZE = 400
BATCH_DELAY = 1.5
UPLOAD_TOTAL_TIMEOUT = _int_env("UPLOAD_TOTAL_TIMEOUT", 1000)
POST_UPLOAD_TIMEOUT = max(15, _int_env("POST_UPLOAD_TIMEOUT", 60))
FORWARD_TIMEOUT = max(15, _int_env("FORWARD_TIMEOUT", 90))
TRANSFER_IDLE_TIMEOUT = max(60, _int_env("TRANSFER_IDLE_TIMEOUT", 120))
TRANSFER_RETRIES = max(1, _int_env("TRANSFER_RETRIES", 3))
# Maximum time allowed for one batch item before it is skipped and the batch continues.
FILE_TASK_TIMEOUT = max(120, _int_env("FILE_TASK_TIMEOUT", 1800))
# Public-media downloader bounds.  yt-dlp and ffmpeg remain isolated from the
# existing Telegram batch pipeline.
MEDIA_INPUT_TIMEOUT = max(30, _int_env("MEDIA_INPUT_TIMEOUT", 120))
MEDIA_DOWNLOAD_TIMEOUT = max(60, _int_env("MEDIA_DOWNLOAD_TIMEOUT", 1800))


# Subscription plan configuration. Prices can be changed here without touching the code.
PLAN_CONFIG = {
    "basic": {
        "label": "⚡ Basic", "daily_limit": 10, "batch": False, "batch_limit": 0,
        "inr": {"weekly": 25, "monthly": 80, "yearly": 900},
        "usd": {"weekly": 1.00, "monthly": 2.00, "yearly": 12.00},
    },
    "standard": {
        "label": "🥈 Standard", "daily_limit": 50, "batch": True, "batch_limit": 50,
        "inr": {"weekly": 45, "monthly": 160, "yearly": 1850},
        "usd": {"weekly": 1.00, "monthly": 3.00, "yearly": 24.00},
    },
    "premium": {
        "label": "🥇 Premium", "daily_limit": 100, "batch": True, "batch_limit": 100,
        "inr": {"weekly": 75, "monthly": 270, "yearly": 2900},
        "usd": {"weekly": 1.00, "monthly": 4.00, "yearly": 37.00},
    },
    "ultimate": {
        "label": "💎 Ultimate ✨ Best Value", "daily_limit": None, "batch": True, "batch_limit": BATCH_SIZE,
        "range": True,
        "inr": {"weekly": 120, "monthly": 400, "yearly": 4100},
        "usd": {"weekly": 2.00, "monthly": 5.00, "yearly": 52.00},
    },
    "elite": {
        "label": "🔥 Elite", "daily_limit": None, "batch": True, "batch_limit": BATCH_SIZE,
        "no_footer": True, "range": True,
        "inr": {"weekly": 140, "monthly": 480, "yearly": 4900},
        "usd": {"weekly": 2.00, "monthly": 6.00, "yearly": 62.00},
    },
}

DEFAULT_PLAN = "basic"
PAYMENT_QR_PATH = getenv("PAYMENT_QR_PATH", "payment_qr.jpg")
MEDIA_FOOTER = "@Restricted_Content_SaveBot ⚡"

# PayPal REST checkout. Set LIVE credentials for production or SANDBOX credentials for testing.
PAYPAL_CLIENT_ID = getenv("PAYPAL_CLIENT_ID", "").strip()
PAYPAL_CLIENT_SECRET = getenv("PAYPAL_CLIENT_SECRET", "").strip()
PAYPAL_BASE_URL = getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com").strip().rstrip("/")
