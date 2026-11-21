from pyrogram import filters
from safe_repo import app


@app.on_message(filters.command("id"))
async def id_command(_, message):
    """Show the Telegram user/chat ID.

    /id shows the sender's own ID. When used as a reply to another user's
    message, it also shows the replied user's ID.
    """
    sender = message.from_user
    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
        name = target.mention or target.first_name or "User"
        text = (
            "╭━━━〔 🆔 𝗨𝗦𝗘𝗥 𝗜𝗗 〕━━━╮\n\n"
            f"👤 **User:** {name}\n"
            f"🔢 **User ID:** `{target.id}`\n\n"
            "💡 This is the ID of the user whose message you replied to.\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯"
        )
    else:
        text = (
            "╭━━━〔 🆔 𝗬𝗢𝗨𝗥 𝗜𝗗 〕━━━╮\n\n"
            f"👤 **Name:** {sender.mention}\n"
            f"🔢 **User ID:** `{sender.id}`\n\n"
            "💡 You can copy this ID and use it where a Telegram User ID is required.\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯"
        )
    await message.reply_text(text)
