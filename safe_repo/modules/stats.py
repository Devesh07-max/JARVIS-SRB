from safe_repo.core.mongo.users_db import get_users, get_user_records
from safe_repo.core.mongo.plans_db import premium_users, check_premium
from safe_repo.core.mongo.stats_db import get_stats

async def build_stats(client):
    data = await get_stats()
    users = len(await get_users())
    recent_records = await get_user_records(limit=5)
    recent_lines = []
    for rec in recent_records:
        name = " ".join(x for x in (rec.get("first_name"), rec.get("last_name")) if x).strip() or "Unknown"
        username = f"@{rec.get('username')}" if rec.get("username") else ""
        recent_lines.append(f"• {name} {username} — `{rec.get('user')}`".rstrip())
    recent_text = "\n".join(recent_lines) if recent_lines else "• No registered users yet"
    active = 0
    for uid in await premium_users():
        try:
            if await check_premium(uid): active += 1
        except Exception: pass
    try:
        from safe_repo.modules.main import users_loop
        processing = sum(1 for running in users_loop.values() if running)
    except Exception:
        processing = 0
    attempts = data['downloads']; success = data['download_success']; failed = data['download_failed']
    rate = (success / attempts * 100) if attempts else 0
    return (
        f"╭━━━〔 📊 𝗕𝗢𝗧 𝗔𝗗𝗠𝗜𝗡 𝗗𝗔𝗦𝗛𝗕𝗢𝗔𝗥𝗗 〕━━━╮\n\n"
        f"🤖 **Bot:** {(await client.get_me()).mention}\n\n"
        f"👥 **USERS**\n├─ Registered: `{users}`\n├─ Premium: `{active}`\n└─ Trial/Free: `{max(0, users-active)}`\n\n"
        f"🧑‍💻 **RECENT USERS**\n{recent_text}\n\n"
        f"⚡ **LIVE**\n└─ Currently processing: `{processing}`\n\n"
        f"📥 **DOWNLOADS**\n├─ Attempts: `{attempts}`\n├─ Successful: `{success}`\n├─ Failed: `{failed}`\n└─ Success rate: `{rate:.1f}%`\n\n"
        f"📦 **BATCHES**\n├─ Started: `{data['batches']}`\n├─ Completed: `{data['batch_completed']}`\n└─ Cancelled: `{data['batch_cancelled']}`\n\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯"
    )
