import datetime
from motor.motor_asyncio import AsyncIOMotorClient as MongoCli
from config import MONGO_DB, MONGO_DATABASE_NAME

mongo = MongoCli(MONGO_DB)
database = mongo[MONGO_DATABASE_NAME]
db = database["premium_db"]

async def add_premium(user_id, expire_date, plan="premium"):
    data = await check_premium(user_id)
    payload = {"expire_date": expire_date, "plan": str(plan).lower()}
    if data and data.get("_id"):
        await db.update_one({"_id": user_id}, {"$set": payload})
    else:
        await db.insert_one({"_id": user_id, **payload})

async def remove_premium(user_id):
    await db.delete_one({"_id": user_id})

async def check_premium(user_id):
    data = await db.find_one({"_id": user_id})
    if data and data.get("expire_date"):
        expiry = data["expire_date"]
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=datetime.timezone.utc)
        if expiry <= datetime.datetime.now(datetime.timezone.utc):
            await remove_premium(user_id)
            return None
    return data

async def get_plan(user_id):
    data = await check_premium(user_id)
    return (data or {}).get("plan")

async def premium_users():
    id_list = []
    async for data in db.find():
        if await check_premium(data["_id"]):
            id_list.append(data["_id"])
    return id_list

async def check_and_remove_expired_users():
    current_time = datetime.datetime.now(datetime.timezone.utc)
    async for data in db.find():
        expire_date = data.get("expire_date")
        if expire_date:
            if expire_date.tzinfo is None:
                expire_date = expire_date.replace(tzinfo=datetime.timezone.utc)
            if expire_date < current_time:
                await remove_premium(data["_id"])
                print(f"Removed user {data['_id']} due to expired plan.")
