from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient as MongoCli
from config import MONGO_DB, MONGO_DATABASE_NAME

mongo = MongoCli(MONGO_DB)
database = mongo[MONGO_DATABASE_NAME]
stats = database["counters"]

async def increment(key, amount=1):
    await stats.update_one(
        {"_id": "global"},
        {"$inc": {key: int(amount)}, "$set": {"updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )

async def get_stats():
    doc = await stats.find_one({"_id": "global"}) or {}
    return {
        "downloads": int(doc.get("downloads", 0)),
        "download_success": int(doc.get("download_success", 0)),
        "download_failed": int(doc.get("download_failed", 0)),
        "batches": int(doc.get("batches", 0)),
        "batch_completed": int(doc.get("batch_completed", 0)),
        "batch_cancelled": int(doc.get("batch_cancelled", 0)),
        "updated_at": doc.get("updated_at"),
    }
