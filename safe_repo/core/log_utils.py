from datetime import datetime
from config import LOG_GROUP
from safe_repo import app


def user_label(user):
    if not user:
        return "Unknown"
    name = " ".join(x for x in [user.first_name, user.last_name] if x)
    username = f"@{user.username}" if user.username else "No username"
    return f"{name or 'Unknown'} ({username})"


async def log_event(text):
    if not LOG_GROUP:
        return
    try:
        await app.send_message(LOG_GROUP, f"{text}\n\n🕒 {datetime.now().strftime('%d-%m-%Y %I:%M:%S %p')}")
    except Exception as e:
        print(f"Log error: {e}")
