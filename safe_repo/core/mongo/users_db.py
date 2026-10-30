# safe_repo user database helpers

from config import MONGO_DB, FREE_TRIAL_USES
from motor.motor_asyncio import AsyncIOMotorClient as MongoCli
from pymongo.errors import DuplicateKeyError

mongo = MongoCli(MONGO_DB)

# New SRB database
database = mongo["jarvis_srb_new"]

# Preserve existing collection structure
db = database["users_db"]
referral_claims = db.referral_claims


async def get_users():
    return [
        user["user"]
        async for user in db.users.find(
            {"user": {"$gt": 0}},
            {"user": 1}
        )
    ]


async def get_user(user):
    return await db.users.find_one({"user": user}) is not None


async def add_user(user, first_name=None, last_name=None, username=None):
    uid = int(getattr(user, "id", user))

    update = {
        "$set": {
            "first_name": first_name if first_name is not None else getattr(user, "first_name", None),
            "last_name": last_name if last_name is not None else getattr(user, "last_name", None),
            "username": username if username is not None else getattr(user, "username", None),
        },
        "$setOnInsert": {"user": uid},
    }

    await db.users.update_one({"user": uid}, update, upsert=True)


async def register_start(user, referral_id=None):
    """Register a start and atomically award one valid referral."""

    uid = int(getattr(user, "id", user))

    result = await db.users.update_one(
        {"user": uid},
        {
            "$set": {
                "first_name": getattr(user, "first_name", None),
                "last_name": getattr(user, "last_name", None),
                "username": getattr(user, "username", None),
            },
            "$setOnInsert": {"user": uid},
        },
        upsert=True,
    )

    is_new = result.upserted_id is not None
    rewarded = False
    referrer = None

    try:
        referrer = int(referral_id) if referral_id is not None else None
    except (TypeError, ValueError):
        referrer = None

    if is_new and referrer and referrer != uid:
        await referral_claims.create_index("referred_user", unique=True)

        try:
            await referral_claims.insert_one({
                "referred_user": uid,
                "referrer": referrer
            })
        except DuplicateKeyError:
            return is_new, False, None

        reward = await db.users.update_one(
            {"user": referrer, "referred_users": {"$ne": uid}},
            {
                "$addToSet": {"referred_users": uid},
                "$inc": {"referral_tokens_earned": 2},
            },
        )

        rewarded = reward.modified_count == 1

        if rewarded:
            await db.users.update_one(
                {"user": uid, "referrer_id": {"$exists": False}},
                {
                    "$set": {
                        "referrer_id": referrer,
                        "referral_rewarded": True
                    }
                },
            )
        else:
            await referral_claims.delete_one({"referred_user": uid})

    return is_new, rewarded, referrer if rewarded else None


async def get_user_records(limit=100):
    cursor = db.users.find(
        {"user": {"$gt": 0}},
        {
            "_id": 0,
            "user": 1,
            "first_name": 1,
            "last_name": 1,
            "username": 1
        },
    ).sort("_id", -1).limit(int(limit))

    return [doc async for doc in cursor]


async def del_user(user):
    await db.users.delete_one({"user": user})


async def get_trial_uses(user):
    doc = await db.users.find_one({"user": user}, {"trial_uses": 1})
    return int(doc.get("trial_uses", 0)) if doc else 0


async def get_referral_stats(user):
    doc = await db.users.find_one(
        {"user": int(user)},
        {
            "trial_uses": 1,
            "referral_tokens_earned": 1,
            "referred_users": 1
        },
    )

    earned = int(doc.get("referral_tokens_earned", 0)) if doc else 0
    used_total = int(doc.get("trial_uses", 0)) if doc else 0
    used = min(earned, max(0, used_total - int(FREE_TRIAL_USES)))

    referred_users = (doc.get("referred_users") or []) if doc else []

    return {
        "successful_referrals": len(referred_users),
        "tokens_earned": earned,
        "tokens_used": used,
        "tokens_remaining": max(0, earned - used),
    }


async def get_trial_limit(user):
    stats = await get_referral_stats(user)
    return int(FREE_TRIAL_USES) + stats["tokens_earned"]


async def increment_trial_uses(user, amount=1):
    await db.users.update_one(
        {"user": user},
        {
            "$inc": {"trial_uses": int(amount)},
            "$setOnInsert": {"user": user}
        },
        upsert=True,
    )

    return await get_trial_uses(user)


async def set_trial_uses(user, uses):
    await db.users.update_one(
        {"user": user},
        {
            "$set": {"trial_uses": max(0, int(uses))}
        },
        upsert=True,
    )


async def get_daily_usage(user):
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    doc = await db.users.find_one(
        {"user": user},
        {"daily_usage": 1, "daily_usage_date": 1}
    )

    if not doc or doc.get("daily_usage_date") != today:
        return 0

    return int(doc.get("daily_usage", 0))


async def increment_daily_usage(user, amount=1):
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    doc = await db.users.find_one(
        {"user": user},
        {"daily_usage": 1, "daily_usage_date": 1}
    )

    if not doc or doc.get("daily_usage_date") != today:
        await db.users.update_one(
            {"user": user},
            {
                "$set": {
                    "user": user,
                    "daily_usage": int(amount),
                    "daily_usage_date": today
                }
            },
            upsert=True,
        )
    else:
        await db.users.update_one(
            {"user": user},
            {"$inc": {"daily_usage": int(amount)}},
            upsert=True,
        )

    return await get_daily_usage(user)
