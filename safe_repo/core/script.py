# safe_repo

START_TXT = """
╭━━━〔 ✨ 𝗪𝗘𝗟𝗖𝗢𝗠𝗘 〕━━━╮

👋 **Hello, {}!**

📥 **Advanced Content Saver**
A fast and simple way to save supported Telegram messages and files.

🎁 **FREE TRIAL**
You receive **10 free uses** when you start a new account.

╭─〔 🚀 𝗤𝗨𝗜𝗖𝗞 𝗦𝗧𝗔𝗥𝗧 〕
│
│ 🔗 Send a Telegram message link directly
│ 🔐 Use /login for private sources
│ 📦 Use /batch for multiple messages
│ 📋 Check your access with /myplan
│ 💎 Need more? Open /buypremium
│
╰────────────────────

💡 **Tip:** Use /help anytime for the complete guide.

👇 **Choose an option below to get started.**
╰━━━━━━━━━━━━━━━━━━━━╯
"""

FORCE_MSG = """
╭━━━〔 🔒 𝗔𝗖𝗖𝗘𝗦𝗦 𝗥𝗘𝗤𝗨𝗜𝗥𝗘𝗗 〕━━━╮

👋 **Hello, {}!**

To continue using the bot, please join the required updates channel(s) below.

✅ After joining, press **Try Again** or send /start once more.

╰━━━━━━━━━━━━━━━━━━━━╯
"""

HELP_TXT = """
📚 **HELP CENTER**

💎 /buypremium — View Premium plans and purchase instructions.
⭐ /premium — Open Premium information.
⚙️ /customisations — Manage your download preferences.
🔐 /login — Connect your Telegram account.
📦 /batch — Process multiple messages in a range.
⛔ /cancel — Stop an active batch.
📋 /myplan — Check your current access and trial.
"""

HELP2_TXT = """
╭━━━〔 🔗 𝗛𝗢𝗪 𝗧𝗢 𝗨𝗦𝗘 〕━━━╮

🌐 **PUBLIC SOURCE**
Send the Telegram message link directly.

🔐 **PRIVATE CHANNEL / GROUP**
First use /login, then send a link to a message that your logged-in account can access.

🤖 **BOT MESSAGE LINK**
Use the supported Telegram message-link format shown by Telegram/your client.

🧵 **GROUP TOPIC**
For private topics, use the message link format supported by your Telegram client.

📦 **BATCH**
Use /batch, then provide the start and end message links when requested.

⚠️ **If something fails**
Check that the link is valid and that your logged-in account has access to the source.

╰━━━━━━━━━━━━━━━━━━━━╯
"""

ADMIN_TXT = """
╭━━━〔 👑 𝗔𝗗𝗠𝗜𝗡 𝗣𝗔𝗡𝗘𝗟 〕━━━╮

➕ /add — Activate Premium for a user.
➖ /rem — Remove Premium from a user.
🔍 /check — Check a user's Premium status.
📢 /gcast — Broadcast a replied message.
📣 /announce — Send an announcement.
📊 /stats — View bot statistics.
⚡ /speedtest — Test server connectivity.

🔐 These controls are restricted to the bot owner.
╰━━━━━━━━━━━━━━━━━━━━╯
"""

CUSTOMISATIONS_TXT = """
╭━━━〔 🎨 𝗖𝗨𝗦𝗧𝗢𝗠𝗜𝗦𝗔𝗧𝗜𝗢𝗡𝗦 〕━━━╮

Customize how the bot handles your downloads.

📝 **Caption** — Set or change your caption.
🔤 **Replace Words** — Automatically replace selected words.
🗑️ **Remove Words** — Remove words from captions/filenames.
🎬 **Video Thumbnail** — Separate set/preview/delete controls.
🎵📄 **Media Thumbnail** — Separate audio/document thumbnail controls.
🔐 **Session** — Manage your logged-in session.
🔄 **Reset All** — Restore saved settings to default.

╰━━━━━━━━━━━━━━━━━━━━╯
"""

CAPTI0NS_TXT = """
╭━━━〔 📝 𝗖𝗔𝗣𝗧𝗜𝗢𝗡 〕━━━╮

Send the caption you want the bot to use for your files.

💡 You can update it anytime from Settings.
╰━━━━━━━━━━━━━━━━━━━━╯
"""

THUMBNAIL_TXT = """
╭━━━〔 🖼️ 𝗧𝗛𝗨𝗠𝗕𝗡𝗔𝗜𝗟 〕━━━╮

Send a photo to set it as your default thumbnail.

🗑️ You can remove it later from Settings.
╰━━━━━━━━━━━━━━━━━━━━╯
"""

SESSION_TXT = """
╭━━━〔 🔐 𝗦𝗘𝗦𝗦𝗜𝗢𝗡 〕━━━╮

Your Telegram session is used to access messages that your account is already authorized to view.

⚠️ Never share your OTP, password, or session string with anyone.
╰━━━━━━━━━━━━━━━━━━━━╯
"""

CHANNEL_TXT = """
╭━━━〔 📡 𝗖𝗛𝗔𝗡𝗡𝗘𝗟 〕━━━╮

Configure the destination channel used by your saved settings.

💡 Use Reset All Settings if you want to clear saved preferences.
╰━━━━━━━━━━━━━━━━━━━━╯
"""
