"""
╔══════════════════════════════════════════════╗
║           ᴄᴀʟʏᴘꜱᴏʙᴏᴛ - Telegram Bot          ║
║     Professional Group Management System     ║
╚══════════════════════════════════════════════╝

Free Hosting: Railway.app / Render.com / Koyeb.com
"""

import logging
import re
import json
import os
import time
from datetime import datetime, timedelta
from functools import wraps
from collections import defaultdict

from telegram import (
    Update, ChatPermissions, InlineKeyboardButton,
    InlineKeyboardMarkup, ParseMode
)
from telegram.ext import (
    Updater, CommandHandler, MessageHandler,
    Filters, CallbackQueryHandler, CallbackContext
)
from telegram.error import BadRequest, TelegramError

# ─── CONFIG ────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
OWNER_ID   = int(os.environ.get("OWNER_ID", "123456789"))  # Your Telegram user ID
LOG_CHANNEL = os.environ.get("LOG_CHANNEL", "")           # Optional log channel ID

# ─── LOGGING ───────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("ᴄᴀʟʏᴘꜱᴏʙᴏᴛ")

# ─── IN-MEMORY STORAGE (replace with MongoDB for persistence) ──
db = {
    "warns":    defaultdict(lambda: defaultdict(int)),    # {chat_id: {user_id: count}}
    "filters":  defaultdict(dict),                        # {chat_id: {keyword: reply}}
    "notes":    defaultdict(dict),                        # {chat_id: {name: text}}
    "welcome":  defaultdict(lambda: {"enabled": True, "msg": None}),
    "goodbye":  defaultdict(lambda: {"enabled": True, "msg": None}),
    "antiflood":defaultdict(lambda: {"enabled": False, "limit": 5}),
    "antispam": defaultdict(lambda: False),
    "blacklist":defaultdict(set),                         # {chat_id: {word, ...}}
    "locks":    defaultdict(dict),
    "rules":    defaultdict(str),
    "banned":   defaultdict(set),
    "flood_tracker": defaultdict(lambda: defaultdict(list)),
    "captcha":  defaultdict(lambda: False),
    "pending_captcha": {},                                # {(chat_id, user_id): message_id}
}

WARN_LIMIT = 3  # Auto-ban after 3 warns

# ═══════════════════════════════════════════════════════
#  DECORATORS & HELPERS
# ═══════════════════════════════════════════════════════

def mono(text): return f"`{text}`"
def bold(text): return f"*{text}*"
def italic(text): return f"_{text}_"

CALYPSO = "ᴄᴀʟʏᴘꜱᴏʙᴏᴛ"

def calypso_header(title):
    return f"*╔══ {CALYPSO} ══╗*\n*║* {title}\n*╚══════════════════╝*\n"

def admin_required(func):
    @wraps(func)
    def wrapper(update: Update, context: CallbackContext):
        user = update.effective_user
        chat = update.effective_chat
        if chat.type == "private":
            return func(update, context)
        admins = [m.user.id for m in chat.get_administrators()]
        if user.id not in admins and user.id != OWNER_ID:
            update.message.reply_text(
                f"{calypso_header('⛔ ᴘᴇʀᴍɪꜱꜱɪᴏɴ ᴅᴇɴɪᴇᴅ')}"
                "You are not an administrator.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        return func(update, context)
    return wrapper

def owner_required(func):
    @wraps(func)
    def wrapper(update: Update, context: CallbackContext):
        if update.effective_user.id != OWNER_ID:
            update.message.reply_text("⛔ Owner only command.")
            return
        return func(update, context)
    return wrapper

def get_target_user(update, context):
    """Returns (user_id, first_name) from reply or args."""
    msg = update.message
    if msg.reply_to_message:
        u = msg.reply_to_message.from_user
        return u.id, u.first_name
    if context.args:
        arg = context.args[0]
        if arg.lstrip("-").isdigit():
            return int(arg), str(arg)
        elif arg.startswith("@"):
            return arg, arg
    return None, None

def log_action(context, chat, action, user, by, reason=""):
    if not LOG_CHANNEL:
        return
    try:
        context.bot.send_message(
            LOG_CHANNEL,
            f"*📋 ʟᴏɢ*\n"
            f"*Chat:* {chat.title} (`{chat.id}`)\n"
            f"*Action:* `{action}`\n"
            f"*User:* [{user}](tg://user?id={user})\n"
            f"*By:* {by}\n"
            f"*Reason:* {reason or 'No reason'}",
            parse_mode=ParseMode.MARKDOWN
        )
    except:
        pass

# ═══════════════════════════════════════════════════════
#  1. START / HELP
# ═══════════════════════════════════════════════════════

def start(update: Update, context: CallbackContext):
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("➕ ᴀᴅᴅ ᴛᴏ ɢʀᴏᴜᴘ", url=f"https://t.me/{context.bot.username}?startgroup=true"),
        InlineKeyboardButton("📖 ʜᴇʟᴘ", callback_data="help_main"),
    ]])
    update.message.reply_text(
        f"{calypso_header('ᴡᴇʟᴄᴏᴍᴇ')}"
        f"I am *{CALYPSO}* — an elite group management bot.\n\n"
        "🛡 Protect your group with advanced moderation\n"
        "⚡ Lightning fast & reliable\n"
        "🎯 20+ powerful features\n\n"
        f"Add me to your group and use /help to get started.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=buttons
    )

def help_command(update: Update, context: CallbackContext):
    categories = [
        ["🛡 Moderation", "help_mod"],
        ["👋 Welcome", "help_welcome"],
        ["⚠️ Warns", "help_warns"],
        ["🔒 Locks", "help_locks"],
        ["🔎 Filters", "help_filters"],
        ["📋 Notes", "help_notes"],
        ["🚫 Blacklist", "help_blacklist"],
        ["⚙️ Admin", "help_admin"],
    ]
    kb = [[InlineKeyboardButton(a, callback_data=b)] for a, b in categories]
    kb.append([InlineKeyboardButton("🏠 Home", callback_data="help_main")])
    update.message.reply_text(
        f"{calypso_header('ʜᴇʟᴘ ᴄᴇɴᴛʀᴇ')}"
        "Choose a category below:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb)
    )

HELP_TEXTS = {
    "help_mod": (
        f"{calypso_header('🛡 ᴍᴏᴅᴇʀᴀᴛɪᴏɴ')}"
        "`/ban [user] [reason]` — Ban user\n"
        "`/unban [user]` — Unban user\n"
        "`/kick [user] [reason]` — Kick user\n"
        "`/mute [user] [time]` — Mute user\n"
        "`/unmute [user]` — Unmute user\n"
        "`/tmute [user] [time]` — Temp mute\n"
        "`/tban [user] [time]` — Temp ban\n"
        "`/purge [n]` — Delete last N messages\n"
        "`/del` — Delete replied message\n"
    ),
    "help_welcome": (
        f"{calypso_header('👋 ᴡᴇʟᴄᴏᴍᴇ')}"
        "`/setwelcome [msg]` — Set welcome message\n"
        "`/setgoodbye [msg]` — Set goodbye message\n"
        "`/welcome on/off` — Toggle welcome\n"
        "`/goodbye on/off` — Toggle goodbye\n"
        "`/resetwelcome` — Reset to default\n\n"
        "Use `{first}`, `{last}`, `{username}`, `{mention}`, `{chatname}` as placeholders.\n"
    ),
    "help_warns": (
        f"{calypso_header('⚠️ ᴡᴀʀɴɪɴɢꜱ')}"
        "`/warn [user] [reason]` — Warn user\n"
        "`/unwarn [user]` — Remove last warn\n"
        "`/warns [user]` — Check warns\n"
        "`/resetwarns [user]` — Reset warns\n"
        f"Auto-ban after *{WARN_LIMIT}* warns.\n"
    ),
    "help_locks": (
        f"{calypso_header('🔒 ʟᴏᴄᴋꜱ')}"
        "`/lock [type]` — Lock message type\n"
        "`/unlock [type]` — Unlock message type\n"
        "`/locks` — Show current locks\n\n"
        "Types: `all`, `media`, `sticker`, `gif`, `url`, `forward`, `photo`, `video`, `voice`, `document`\n"
    ),
    "help_filters": (
        f"{calypso_header('🔎 ꜰɪʟᴛᴇʀꜱ')}"
        "`/filter [keyword] [reply]` — Add auto-reply filter\n"
        "`/stop [keyword]` — Remove filter\n"
        "`/filters` — List all filters\n"
    ),
    "help_notes": (
        f"{calypso_header('📋 ɴᴏᴛᴇꜱ')}"
        "`/save [name] [text]` — Save a note\n"
        "`/get [name]` or `#name` — Retrieve note\n"
        "`/clear [name]` — Delete note\n"
        "`/notes` — List all notes\n"
    ),
    "help_blacklist": (
        f"{calypso_header('🚫 ʙʟᴀᴄᴋʟɪꜱᴛ')}"
        "`/addblacklist [word]` — Add word to blacklist\n"
        "`/rmblacklist [word]` — Remove word\n"
        "`/blacklist` — Show blacklist\n"
        "Messages with blacklisted words are auto-deleted.\n"
    ),
    "help_admin": (
        f"{calypso_header('⚙️ ᴀᴅᴍɪɴ')}"
        "`/promote [user]` — Promote to admin\n"
        "`/demote [user]` — Demote admin\n"
        "`/pin` — Pin replied message\n"
        "`/unpin` — Unpin message\n"
        "`/setrules [text]` — Set group rules\n"
        "`/rules` — Show rules\n"
        "`/setflood [n/off]` — Anti-flood limit\n"
        "`/antiflood` — Check flood status\n"
        "`/id` — Get user/chat ID\n"
        "`/info [user]` — Get user info\n"
        "`/captcha on/off` — Toggle captcha\n"
    ),
}

def help_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    data = query.data
    if data == "help_main":
        categories = [
            ["🛡 Moderation", "help_mod"],
            ["👋 Welcome", "help_welcome"],
            ["⚠️ Warns", "help_warns"],
            ["🔒 Locks", "help_locks"],
            ["🔎 Filters", "help_filters"],
            ["📋 Notes", "help_notes"],
            ["🚫 Blacklist", "help_blacklist"],
            ["⚙️ Admin", "help_admin"],
        ]
        kb = [[InlineKeyboardButton(a, callback_data=b)] for a, b in categories]
        query.edit_message_text(
            f"{calypso_header('ʜᴇʟᴘ ᴄᴇɴᴛʀᴇ')}"
            "Choose a category below:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb)
        )
    elif data in HELP_TEXTS:
        kb = [[InlineKeyboardButton("🔙 Back", callback_data="help_main")]]
        query.edit_message_text(
            HELP_TEXTS[data],
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb)
        )

# ═══════════════════════════════════════════════════════
#  2. BAN / UNBAN / KICK
# ═══════════════════════════════════════════════════════

@admin_required
def ban(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid:
        update.message.reply_text("⚠️ Reply to a user or provide user ID.")
        return
    reason = " ".join(context.args[1:]) if context.args and len(context.args) > 1 else "No reason"
    try:
        update.effective_chat.kick_member(uid)
        update.message.reply_text(
            f"{calypso_header('🔨 ᴜꜱᴇʀ ʙᴀɴɴᴇᴅ')}"
            f"*User:* [{name}](tg://user?id={uid})\n"
            f"*Reason:* {reason}\n"
            f"*By:* {update.effective_user.first_name}",
            parse_mode=ParseMode.MARKDOWN
        )
        log_action(context, update.effective_chat, "BAN", uid, update.effective_user.first_name, reason)
    except BadRequest as e:
        update.message.reply_text(f"❌ Failed: {e.message}")

@admin_required
def unban(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid:
        update.message.reply_text("⚠️ Reply to a user or provide user ID.")
        return
    try:
        update.effective_chat.unban_member(uid)
        update.message.reply_text(
            f"{calypso_header('✅ ᴜꜱᴇʀ ᴜɴʙᴀɴɴᴇᴅ')}"
            f"*User:* [{name}](tg://user?id={uid})",
            parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest as e:
        update.message.reply_text(f"❌ Failed: {e.message}")

@admin_required
def kick(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid:
        update.message.reply_text("⚠️ Reply to a user or provide user ID.")
        return
    reason = " ".join(context.args[1:]) if context.args and len(context.args) > 1 else "No reason"
    try:
        update.effective_chat.kick_member(uid)
        update.effective_chat.unban_member(uid)  # unban so they can rejoin
        update.message.reply_text(
            f"{calypso_header('👢 ᴜꜱᴇʀ ᴋɪᴄᴋᴇᴅ')}"
            f"*User:* [{name}](tg://user?id={uid})\n"
            f"*Reason:* {reason}",
            parse_mode=ParseMode.MARKDOWN
        )
        log_action(context, update.effective_chat, "KICK", uid, update.effective_user.first_name, reason)
    except BadRequest as e:
        update.message.reply_text(f"❌ Failed: {e.message}")

# ═══════════════════════════════════════════════════════
#  3. MUTE / UNMUTE / TEMP BAN / TEMP MUTE
# ═══════════════════════════════════════════════════════

def parse_time(time_str):
    """Parse '10m', '2h', '1d' into seconds."""
    if not time_str:
        return None
    match = re.match(r"(\d+)([smhd])", time_str.lower())
    if not match:
        return None
    val, unit = int(match.group(1)), match.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return val * multipliers[unit]

@admin_required
def mute(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid:
        update.message.reply_text("⚠️ Reply to a user or provide user ID.")
        return
    try:
        update.effective_chat.restrict_member(
            uid,
            permissions=ChatPermissions(can_send_messages=False)
        )
        update.message.reply_text(
            f"{calypso_header('🔇 ᴜꜱᴇʀ ᴍᴜᴛᴇᴅ')}"
            f"*User:* [{name}](tg://user?id={uid})",
            parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest as e:
        update.message.reply_text(f"❌ Failed: {e.message}")

@admin_required
def unmute(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid:
        update.message.reply_text("⚠️ Reply to a user or provide user ID.")
        return
    try:
        update.effective_chat.restrict_member(
            uid,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            )
        )
        update.message.reply_text(
            f"{calypso_header('🔊 ᴜꜱᴇʀ ᴜɴᴍᴜᴛᴇᴅ')}"
            f"*User:* [{name}](tg://user?id={uid})",
            parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest as e:
        update.message.reply_text(f"❌ Failed: {e.message}")

@admin_required
def tmute(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid or not context.args:
        update.message.reply_text("Usage: /tmute [user] [time e.g. 10m, 2h, 1d]")
        return
    duration = parse_time(context.args[-1])
    if not duration:
        update.message.reply_text("❌ Invalid time. Use: 10m, 2h, 1d")
        return
    until = datetime.now() + timedelta(seconds=duration)
    try:
        update.effective_chat.restrict_member(
            uid,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until
        )
        update.message.reply_text(
            f"{calypso_header('⏱ ᴛᴇᴍᴘ ᴍᴜᴛᴇ')}"
            f"*User:* [{name}](tg://user?id={uid})\n"
            f"*Duration:* `{context.args[-1]}`\n"
            f"*Until:* `{until.strftime('%Y-%m-%d %H:%M')}`",
            parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest as e:
        update.message.reply_text(f"❌ Failed: {e.message}")

@admin_required
def tban(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid or not context.args:
        update.message.reply_text("Usage: /tban [user] [time e.g. 10m, 2h, 1d]")
        return
    duration = parse_time(context.args[-1])
    if not duration:
        update.message.reply_text("❌ Invalid time. Use: 10m, 2h, 1d")
        return
    until = datetime.now() + timedelta(seconds=duration)
    try:
        update.effective_chat.kick_member(uid, until_date=until)
        update.message.reply_text(
            f"{calypso_header('⏳ ᴛᴇᴍᴘ ʙᴀɴ')}"
            f"*User:* [{name}](tg://user?id={uid})\n"
            f"*Duration:* `{context.args[-1]}`\n"
            f"*Until:* `{until.strftime('%Y-%m-%d %H:%M')}`",
            parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest as e:
        update.message.reply_text(f"❌ Failed: {e.message}")

# ═══════════════════════════════════════════════════════
#  4. WARN SYSTEM
# ═══════════════════════════════════════════════════════

@admin_required
def warn(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid:
        update.message.reply_text("⚠️ Reply to a user or provide user ID.")
        return
    chat_id = update.effective_chat.id
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "No reason"
    db["warns"][chat_id][uid] += 1
    count = db["warns"][chat_id][uid]
    if count >= WARN_LIMIT:
        try:
            update.effective_chat.kick_member(uid)
            db["warns"][chat_id][uid] = 0
            update.message.reply_text(
                f"{calypso_header('🚫 ᴀᴜᴛᴏ-ʙᴀɴ')}"
                f"*User:* [{name}](tg://user?id={uid})\n"
                f"Reached *{WARN_LIMIT} warns* — automatically banned.",
                parse_mode=ParseMode.MARKDOWN
            )
        except BadRequest as e:
            update.message.reply_text(f"❌ Could not ban: {e.message}")
    else:
        update.message.reply_text(
            f"{calypso_header('⚠️ ᴡᴀʀɴᴇᴅ')}"
            f"*User:* [{name}](tg://user?id={uid})\n"
            f"*Reason:* {reason}\n"
            f"*Warns:* `{count}/{WARN_LIMIT}`",
            parse_mode=ParseMode.MARKDOWN
        )

@admin_required
def unwarn(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid:
        update.message.reply_text("⚠️ Reply to a user or provide user ID.")
        return
    chat_id = update.effective_chat.id
    if db["warns"][chat_id][uid] > 0:
        db["warns"][chat_id][uid] -= 1
    count = db["warns"][chat_id][uid]
    update.message.reply_text(
        f"{calypso_header('✅ ᴡᴀʀɴ ʀᴇᴍᴏᴠᴇᴅ')}"
        f"*User:* [{name}](tg://user?id={uid})\n"
        f"*Warns:* `{count}/{WARN_LIMIT}`",
        parse_mode=ParseMode.MARKDOWN
    )

def warns(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid:
        uid = update.effective_user.id
        name = update.effective_user.first_name
    chat_id = update.effective_chat.id
    count = db["warns"][chat_id].get(uid, 0)
    update.message.reply_text(
        f"{calypso_header('⚠️ ᴡᴀʀɴꜱ ɪɴꜰᴏ')}"
        f"*User:* [{name}](tg://user?id={uid})\n"
        f"*Warns:* `{count}/{WARN_LIMIT}`",
        parse_mode=ParseMode.MARKDOWN
    )

@admin_required
def resetwarns(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid:
        update.message.reply_text("⚠️ Reply to a user or provide user ID.")
        return
    db["warns"][update.effective_chat.id][uid] = 0
    update.message.reply_text(
        f"✅ Warns reset for [{name}](tg://user?id={uid}).",
        parse_mode=ParseMode.MARKDOWN
    )

# ═══════════════════════════════════════════════════════
#  5. WELCOME / GOODBYE
# ═══════════════════════════════════════════════════════

@admin_required
def setwelcome(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    msg_text = update.message.text.split(None, 1)
    if len(msg_text) < 2:
        update.message.reply_text("Usage: /setwelcome [message]\nPlaceholders: {first}, {last}, {username}, {mention}, {chatname}")
        return
    db["welcome"][chat_id]["msg"] = msg_text[1]
    update.message.reply_text(f"✅ Welcome message saved.")

@admin_required
def setgoodbye(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    msg_text = update.message.text.split(None, 1)
    if len(msg_text) < 2:
        update.message.reply_text("Usage: /setgoodbye [message]")
        return
    db["goodbye"][chat_id]["msg"] = msg_text[1]
    update.message.reply_text(f"✅ Goodbye message saved.")

@admin_required
def toggle_welcome(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    args = context.args
    cmd = update.message.text.split()[0].lstrip("/").lower()
    key = "welcome" if "welcome" in cmd else "goodbye"
    if args and args[0].lower() in ("on", "off"):
        db[key][chat_id]["enabled"] = args[0].lower() == "on"
        update.message.reply_text(f"✅ {key.capitalize()} is now *{'ON' if db[key][chat_id]['enabled'] else 'OFF'}*.", parse_mode=ParseMode.MARKDOWN)
    else:
        update.message.reply_text(f"Usage: /{key} on/off")

def welcome_member(update: Update, context: CallbackContext):
    chat = update.effective_chat
    chat_id = chat.id
    cfg = db["welcome"][chat_id]
    if not cfg["enabled"]:
        return
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue

        # Captcha check
        if db["captcha"][chat_id]:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ I'm Human — Click to Verify", callback_data=f"captcha_{chat_id}_{member.id}")
            ]])
            try:
                update.effective_chat.restrict_member(
                    member.id,
                    permissions=ChatPermissions(can_send_messages=False)
                )
            except:
                pass
            m = update.message.reply_text(
                f"👋 Welcome [{member.first_name}](tg://user?id={member.id})!\n"
                "Please verify you're human within 60 seconds.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb
            )
            db["pending_captcha"][(chat_id, member.id)] = m.message_id
            context.job_queue.run_once(
                captcha_timeout,
                60,
                context={"chat_id": chat_id, "user_id": member.id, "msg_id": m.message_id},
                name=f"captcha_{chat_id}_{member.id}"
            )
            return

        template = cfg["msg"] or (
            f"{calypso_header('👋 ᴡᴇʟᴄᴏᴍᴇ')}"
            "Welcome {{mention}} to *{{chatname}}*!\n"
            "Please read the rules and enjoy your stay. 🎉"
        )
        text = template.replace("{first}", member.first_name)
        text = text.replace("{last}", member.last_name or "")
        text = text.replace("{username}", f"@{member.username}" if member.username else member.first_name)
        text = text.replace("{mention}", f"[{member.first_name}](tg://user?id={member.id})")
        text = text.replace("{chatname}", chat.title)
        update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

def goodbye_member(update: Update, context: CallbackContext):
    chat = update.effective_chat
    chat_id = chat.id
    member = update.message.left_chat_member
    if not member or member.is_bot:
        return
    cfg = db["goodbye"][chat_id]
    if not cfg["enabled"]:
        return
    template = cfg["msg"] or f"👋 *{member.first_name}* has left *{chat.title}*. Goodbye!"
    text = template.replace("{first}", member.first_name)
    text = text.replace("{mention}", f"[{member.first_name}](tg://user?id={member.id})")
    text = text.replace("{chatname}", chat.title)
    update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

def captcha_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    data = query.data
    if not data.startswith("captcha_"):
        return
    _, chat_id, user_id = data.split("_")
    chat_id, user_id = int(chat_id), int(user_id)
    if query.from_user.id != user_id:
        query.answer("This captcha is not for you!", show_alert=True)
        return
    try:
        context.bot.restrict_chat_member(
            chat_id, user_id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            )
        )
    except:
        pass
    db["pending_captcha"].pop((chat_id, user_id), None)
    query.edit_message_text(f"✅ [{query.from_user.first_name}](tg://user?id={user_id}) verified!", parse_mode=ParseMode.MARKDOWN)
    query.answer("Verified! Welcome!")

def captcha_timeout(context: CallbackContext):
    job = context.job.context
    chat_id, user_id = job["chat_id"], job["user_id"]
    if (chat_id, user_id) in db["pending_captcha"]:
        try:
            context.bot.kick_chat_member(chat_id, user_id)
            context.bot.delete_message(chat_id, job["msg_id"])
        except:
            pass
        db["pending_captcha"].pop((chat_id, user_id), None)

# ═══════════════════════════════════════════════════════
#  6. PURGE
# ═══════════════════════════════════════════════════════

@admin_required
def purge(update: Update, context: CallbackContext):
    msg = update.message
    if not msg.reply_to_message:
        if context.args and context.args[0].isdigit():
            n = int(context.args[0])
            ids = list(range(msg.message_id - n, msg.message_id + 1))
            try:
                context.bot.delete_messages(msg.chat_id, ids)
            except:
                for i in ids:
                    try: context.bot.delete_message(msg.chat_id, i)
                    except: pass
            update.message.reply_text(f"🗑 Deleted last `{n}` messages.", parse_mode=ParseMode.MARKDOWN)
            return
        update.message.reply_text("Reply to a message to purge from, or use /purge [n].")
        return
    from_id = msg.reply_to_message.message_id
    to_id = msg.message_id
    ids = list(range(from_id, to_id + 1))
    try:
        context.bot.delete_messages(msg.chat_id, ids)
    except:
        for mid in ids:
            try: context.bot.delete_message(msg.chat_id, mid)
            except: pass
    context.bot.send_message(msg.chat_id, f"🗑 Purged `{len(ids)}` messages.", parse_mode=ParseMode.MARKDOWN)

@admin_required
def delete_msg(update: Update, context: CallbackContext):
    if update.message.reply_to_message:
        try:
            update.message.reply_to_message.delete()
            update.message.delete()
        except BadRequest:
            update.message.reply_text("❌ Can't delete that message.")

# ═══════════════════════════════════════════════════════
#  7. FILTERS (Auto-Reply)
# ═══════════════════════════════════════════════════════

@admin_required
def add_filter(update: Update, context: CallbackContext):
    parts = update.message.text.split(None, 2)
    if len(parts) < 3:
        update.message.reply_text("Usage: /filter [keyword] [reply text]")
        return
    chat_id = update.effective_chat.id
    keyword, reply = parts[1].lower(), parts[2]
    db["filters"][chat_id][keyword] = reply
    update.message.reply_text(f"✅ Filter `{keyword}` added.", parse_mode=ParseMode.MARKDOWN)

@admin_required
def stop_filter(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text("Usage: /stop [keyword]")
        return
    chat_id = update.effective_chat.id
    kw = context.args[0].lower()
    db["filters"][chat_id].pop(kw, None)
    update.message.reply_text(f"✅ Filter `{kw}` removed.", parse_mode=ParseMode.MARKDOWN)

def list_filters(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    fltrs = db["filters"][chat_id]
    if not fltrs:
        update.message.reply_text("No filters set.")
        return
    text = f"{calypso_header('🔎 ᴀᴄᴛɪᴠᴇ ꜰɪʟᴛᴇʀꜱ')}"
    for k in fltrs:
        text += f"• `{k}`\n"
    update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

def check_filters(update: Update, context: CallbackContext):
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    text = update.message.text.lower()
    for kw, reply in db["filters"][chat_id].items():
        if kw in text:
            update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)
            break

# ═══════════════════════════════════════════════════════
#  8. NOTES
# ═══════════════════════════════════════════════════════

@admin_required
def save_note(update: Update, context: CallbackContext):
    parts = update.message.text.split(None, 2)
    if len(parts) < 3:
        update.message.reply_text("Usage: /save [name] [text]")
        return
    chat_id = update.effective_chat.id
    name, text = parts[1].lower(), parts[2]
    db["notes"][chat_id][name] = text
    update.message.reply_text(f"✅ Note `{name}` saved. Retrieve with `/get {name}` or `#{name}`", parse_mode=ParseMode.MARKDOWN)

def get_note(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    name = context.args[0].lower() if context.args else None
    if not name:
        update.message.reply_text("Usage: /get [name]")
        return
    note = db["notes"][chat_id].get(name)
    if note:
        update.message.reply_text(note, parse_mode=ParseMode.MARKDOWN)
    else:
        update.message.reply_text(f"❌ No note named `{name}`.", parse_mode=ParseMode.MARKDOWN)

@admin_required
def clear_note(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    name = context.args[0].lower() if context.args else None
    if not name:
        update.message.reply_text("Usage: /clear [name]")
        return
    db["notes"][chat_id].pop(name, None)
    update.message.reply_text(f"✅ Note `{name}` deleted.", parse_mode=ParseMode.MARKDOWN)

def list_notes(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    notes = db["notes"][chat_id]
    if not notes:
        update.message.reply_text("No notes saved.")
        return
    text = f"{calypso_header('📋 ꜱᴀᴠᴇᴅ ɴᴏᴛᴇꜱ')}"
    for n in notes:
        text += f"• `#{n}`\n"
    update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

def hashtag_note(update: Update, context: CallbackContext):
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    match = re.search(r"#(\w+)", update.message.text)
    if match:
        name = match.group(1).lower()
        note = db["notes"][chat_id].get(name)
        if note:
            update.message.reply_text(note, parse_mode=ParseMode.MARKDOWN)

# ═══════════════════════════════════════════════════════
#  9. BLACKLIST
# ═══════════════════════════════════════════════════════

@admin_required
def add_blacklist(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text("Usage: /addblacklist [word]")
        return
    chat_id = update.effective_chat.id
    word = " ".join(context.args).lower()
    db["blacklist"][chat_id].add(word)
    update.message.reply_text(f"✅ `{word}` added to blacklist.", parse_mode=ParseMode.MARKDOWN)

@admin_required
def rm_blacklist(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text("Usage: /rmblacklist [word]")
        return
    chat_id = update.effective_chat.id
    word = " ".join(context.args).lower()
    db["blacklist"][chat_id].discard(word)
    update.message.reply_text(f"✅ `{word}` removed from blacklist.", parse_mode=ParseMode.MARKDOWN)

def show_blacklist(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    bl = db["blacklist"][chat_id]
    if not bl:
        update.message.reply_text("Blacklist is empty.")
        return
    text = f"{calypso_header('🚫 ʙʟᴀᴄᴋʟɪꜱᴛ')}" + "\n".join(f"• `{w}`" for w in sorted(bl))
    update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

def check_blacklist(update: Update, context: CallbackContext):
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    text = update.message.text.lower()
    for word in db["blacklist"][chat_id]:
        if word in text:
            try:
                update.message.delete()
                update.effective_chat.send_message(
                    f"⚠️ [{update.effective_user.first_name}](tg://user?id={update.effective_user.id}) — message removed (blacklisted word).",
                    parse_mode=ParseMode.MARKDOWN
                )
            except:
                pass
            break

# ═══════════════════════════════════════════════════════
#  10. LOCKS
# ═══════════════════════════════════════════════════════

LOCK_TYPES = {
    "sticker": Filters.sticker,
    "gif": Filters.document.mime_type("video/mp4") | Filters.animation,
    "url": Filters.entity("url") | Filters.entity("text_link"),
    "forward": Filters.forwarded,
    "photo": Filters.photo,
    "video": Filters.video,
    "voice": Filters.voice,
    "document": Filters.document,
}

@admin_required
def lock(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text("Usage: /lock [type]\nTypes: " + ", ".join(LOCK_TYPES.keys()))
        return
    chat_id = update.effective_chat.id
    ltype = context.args[0].lower()
    if ltype == "all":
        db["locks"][chat_id] = {k: True for k in LOCK_TYPES}
        update.message.reply_text("🔒 All message types locked.")
    elif ltype in LOCK_TYPES:
        db["locks"][chat_id][ltype] = True
        update.message.reply_text(f"🔒 `{ltype}` locked.", parse_mode=ParseMode.MARKDOWN)
    else:
        update.message.reply_text(f"❌ Unknown type. Available: {', '.join(LOCK_TYPES.keys())}")

@admin_required
def unlock(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text("Usage: /unlock [type]")
        return
    chat_id = update.effective_chat.id
    ltype = context.args[0].lower()
    if ltype == "all":
        db["locks"][chat_id] = {}
        update.message.reply_text("🔓 All locks removed.")
    else:
        db["locks"][chat_id].pop(ltype, None)
        update.message.reply_text(f"🔓 `{ltype}` unlocked.", parse_mode=ParseMode.MARKDOWN)

def show_locks(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    locks = db["locks"][chat_id]
    text = f"{calypso_header('🔒 ᴄᴜʀʀᴇɴᴛ ʟᴏᴄᴋꜱ')}"
    for lt in LOCK_TYPES:
        status = "🔒" if locks.get(lt) else "🔓"
        text += f"{status} `{lt}`\n"
    update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

def check_locks(update: Update, context: CallbackContext):
    if not update.message:
        return
    chat = update.effective_chat
    user = update.effective_user
    admins = [m.user.id for m in chat.get_administrators()]
    if user.id in admins:
        return
    chat_id = chat.id
    locks = db["locks"][chat_id]
    msg = update.message

    delete = False
    if locks.get("sticker") and msg.sticker:
        delete = True
    elif locks.get("gif") and (msg.animation):
        delete = True
    elif locks.get("url") and msg.entities:
        for e in msg.entities:
            if e.type in ("url", "text_link"):
                delete = True
                break
    elif locks.get("forward") and msg.forward_date:
        delete = True
    elif locks.get("photo") and msg.photo:
        delete = True
    elif locks.get("video") and msg.video:
        delete = True
    elif locks.get("voice") and msg.voice:
        delete = True
    elif locks.get("document") and msg.document:
        delete = True

    if delete:
        try:
            msg.delete()
        except:
            pass

# ═══════════════════════════════════════════════════════
#  11. ANTI-FLOOD
# ═══════════════════════════════════════════════════════

@admin_required
def setflood(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    if not context.args:
        update.message.reply_text("Usage: /setflood [number/off]")
        return
    val = context.args[0].lower()
    if val == "off":
        db["antiflood"][chat_id]["enabled"] = False
        update.message.reply_text("✅ Anti-flood disabled.")
    elif val.isdigit() and int(val) > 1:
        db["antiflood"][chat_id]["enabled"] = True
        db["antiflood"][chat_id]["limit"] = int(val)
        update.message.reply_text(f"✅ Anti-flood set to `{val}` messages.", parse_mode=ParseMode.MARKDOWN)
    else:
        update.message.reply_text("❌ Invalid value.")

def antiflood_status(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    cfg = db["antiflood"][chat_id]
    status = "ON" if cfg["enabled"] else "OFF"
    update.message.reply_text(
        f"{calypso_header('⚡ ᴀɴᴛɪ-ꜰʟᴏᴏᴅ')}"
        f"Status: *{status}*\nLimit: `{cfg['limit']}` msg/5s",
        parse_mode=ParseMode.MARKDOWN
    )

def check_flood(update: Update, context: CallbackContext):
    if not update.message:
        return
    chat = update.effective_chat
    user = update.effective_user
    chat_id = chat.id
    cfg = db["antiflood"][chat_id]
    if not cfg["enabled"]:
        return
    admins = [m.user.id for m in chat.get_administrators()]
    if user.id in admins:
        return
    now = time.time()
    tracker = db["flood_tracker"][chat_id][user.id]
    tracker = [t for t in tracker if now - t < 5]
    tracker.append(now)
    db["flood_tracker"][chat_id][user.id] = tracker
    if len(tracker) > cfg["limit"]:
        try:
            chat.restrict_member(user.id, permissions=ChatPermissions(can_send_messages=False))
            update.message.reply_text(
                f"⚡ [{user.first_name}](tg://user?id={user.id}) muted for flooding!",
                parse_mode=ParseMode.MARKDOWN
            )
            db["flood_tracker"][chat_id][user.id] = []
        except:
            pass

# ═══════════════════════════════════════════════════════
#  12. RULES
# ═══════════════════════════════════════════════════════

@admin_required
def setrules(update: Update, context: CallbackContext):
    parts = update.message.text.split(None, 1)
    if len(parts) < 2:
        update.message.reply_text("Usage: /setrules [text]")
        return
    db["rules"][update.effective_chat.id] = parts[1]
    update.message.reply_text("✅ Rules saved.")

def rules(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    r = db["rules"][chat_id]
    if not r:
        update.message.reply_text("No rules set. Use /setrules to set them.")
        return
    update.message.reply_text(
        f"{calypso_header('📜 ɢʀᴏᴜᴘ ʀᴜʟᴇꜱ')}{r}",
        parse_mode=ParseMode.MARKDOWN
    )

# ═══════════════════════════════════════════════════════
#  13. PROMOTE / DEMOTE
# ═══════════════════════════════════════════════════════

@admin_required
def promote(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid:
        update.message.reply_text("⚠️ Reply to a user or provide user ID.")
        return
    try:
        update.effective_chat.promote_member(
            uid,
            can_delete_messages=True,
            can_restrict_members=True,
            can_pin_messages=True,
            can_invite_users=True,
            can_manage_chat=True,
        )
        update.message.reply_text(
            f"{calypso_header('⭐ ᴘʀᴏᴍᴏᴛᴇᴅ')}"
            f"[{name}](tg://user?id={uid}) is now an admin!",
            parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest as e:
        update.message.reply_text(f"❌ Failed: {e.message}")

@admin_required
def demote(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid:
        update.message.reply_text("⚠️ Reply to a user or provide user ID.")
        return
    try:
        update.effective_chat.promote_member(
            uid,
            can_delete_messages=False,
            can_restrict_members=False,
            can_pin_messages=False,
            can_invite_users=False,
            can_manage_chat=False,
        )
        update.message.reply_text(
            f"✅ [{name}](tg://user?id={uid}) demoted.",
            parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest as e:
        update.message.reply_text(f"❌ Failed: {e.message}")

# ═══════════════════════════════════════════════════════
#  14. PIN / UNPIN
# ═══════════════════════════════════════════════════════

@admin_required
def pin(update: Update, context: CallbackContext):
    if not update.message.reply_to_message:
        update.message.reply_text("Reply to a message to pin it.")
        return
    loud = not (context.args and context.args[0].lower() in ("silent", "quiet"))
    try:
        update.effective_chat.pin_message(
            update.message.reply_to_message.message_id,
            disable_notification=not loud
        )
        update.message.reply_text("📌 Message pinned.")
    except BadRequest as e:
        update.message.reply_text(f"❌ Failed: {e.message}")

@admin_required
def unpin(update: Update, context: CallbackContext):
    try:
        update.effective_chat.unpin_message()
        update.message.reply_text("📌 Message unpinned.")
    except BadRequest as e:
        update.message.reply_text(f"❌ Failed: {e.message}")

# ═══════════════════════════════════════════════════════
#  15. ID / INFO
# ═══════════════════════════════════════════════════════

def get_id(update: Update, context: CallbackContext):
    msg = update.message
    if msg.reply_to_message:
        u = msg.reply_to_message.from_user
        update.message.reply_text(
            f"*User ID:* `{u.id}`\n*Chat ID:* `{msg.chat_id}`",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        update.message.reply_text(
            f"*Your ID:* `{msg.from_user.id}`\n*Chat ID:* `{msg.chat_id}`",
            parse_mode=ParseMode.MARKDOWN
        )

def info(update: Update, context: CallbackContext):
    msg = update.message
    user = msg.reply_to_message.from_user if msg.reply_to_message else msg.from_user
    chat_id = msg.chat_id
    warns_count = db["warns"][chat_id].get(user.id, 0)
    text = (
        f"{calypso_header('👤 ᴜꜱᴇʀ ɪɴꜰᴏ')}"
        f"*Name:* {user.first_name} {user.last_name or ''}\n"
        f"*Username:* @{user.username or 'N/A'}\n"
        f"*ID:* `{user.id}`\n"
        f"*Warns:* `{warns_count}/{WARN_LIMIT}`\n"
        f"*Link:* [Profile](tg://user?id={user.id})"
    )
    update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

# ═══════════════════════════════════════════════════════
#  16. CAPTCHA TOGGLE
# ═══════════════════════════════════════════════════════

@admin_required
def captcha_cmd(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    if context.args and context.args[0].lower() in ("on", "off"):
        db["captcha"][chat_id] = context.args[0].lower() == "on"
        status = "ON" if db["captcha"][chat_id] else "OFF"
        update.message.reply_text(f"✅ Captcha is now *{status}*.", parse_mode=ParseMode.MARKDOWN)
    else:
        update.message.reply_text("Usage: /captcha on/off")

# ═══════════════════════════════════════════════════════
#  17. ADMINLIST
# ═══════════════════════════════════════════════════════

def adminlist(update: Update, context: CallbackContext):
    chat = update.effective_chat
    admins = chat.get_administrators()
    text = f"{calypso_header('👑 ᴀᴅᴍɪɴ ʟɪꜱᴛ')}"
    for a in admins:
        u = a.user
        title = f" — _{a.custom_title}_" if a.custom_title else ""
        text += f"• [{u.first_name}](tg://user?id={u.id}){title}\n"
    update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

# ═══════════════════════════════════════════════════════
#  18. BROADCAST (Owner only)
# ═══════════════════════════════════════════════════════

@owner_required
def broadcast(update: Update, context: CallbackContext):
    parts = update.message.text.split(None, 1)
    if len(parts) < 2:
        update.message.reply_text("Usage: /broadcast [message]")
        return
    # In production, iterate over all known chat IDs
    update.message.reply_text(f"📢 Broadcast sent:\n{parts[1]}")

# ═══════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════

def main():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # Commands
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("help", help_command))
    dp.add_handler(CommandHandler("ban", ban))
    dp.add_handler(CommandHandler("unban", unban))
    dp.add_handler(CommandHandler("kick", kick))
    dp.add_handler(CommandHandler("mute", mute))
    dp.add_handler(CommandHandler("unmute", unmute))
    dp.add_handler(CommandHandler("tmute", tmute))
    dp.add_handler(CommandHandler("tban", tban))
    dp.add_handler(CommandHandler("warn", warn))
    dp.add_handler(CommandHandler("unwarn", unwarn))
    dp.add_handler(CommandHandler("warns", warns))
    dp.add_handler(CommandHandler("resetwarns", resetwarns))
    dp.add_handler(CommandHandler("purge", purge))
    dp.add_handler(CommandHandler("del", delete_msg))
    dp.add_handler(CommandHandler("filter", add_filter))
    dp.add_handler(CommandHandler("stop", stop_filter))
    dp.add_handler(CommandHandler("filters", list_filters))
    dp.add_handler(CommandHandler("save", save_note))
    dp.add_handler(CommandHandler("get", get_note))
    dp.add_handler(CommandHandler("clear", clear_note))
    dp.add_handler(CommandHandler("notes", list_notes))
    dp.add_handler(CommandHandler("addblacklist", add_blacklist))
    dp.add_handler(CommandHandler("rmblacklist", rm_blacklist))
    dp.add_handler(CommandHandler("blacklist", show_blacklist))
    dp.add_handler(CommandHandler("lock", lock))
    dp.add_handler(CommandHandler("unlock", unlock))
    dp.add_handler(CommandHandler("locks", show_locks))
    dp.add_handler(CommandHandler("setflood", setflood))
    dp.add_handler(CommandHandler("antiflood", antiflood_status))
    dp.add_handler(CommandHandler("setrules", setrules))
    dp.add_handler(CommandHandler("rules", rules))
    dp.add_handler(CommandHandler("setwelcome", setwelcome))
    dp.add_handler(CommandHandler("setgoodbye", setgoodbye))
    dp.add_handler(CommandHandler(["welcome", "goodbye"], toggle_welcome))
    dp.add_handler(CommandHandler("promote", promote))
    dp.add_handler(CommandHandler("demote", demote))
    dp.add_handler(CommandHandler("pin", pin))
    dp.add_handler(CommandHandler("unpin", unpin))
    dp.add_handler(CommandHandler("id", get_id))
    dp.add_handler(CommandHandler("info", info))
    dp.add_handler(CommandHandler("adminlist", adminlist))
    dp.add_handler(CommandHandler("broadcast", broadcast))
    dp.add_handler(CommandHandler("captcha", captcha_cmd))

    # Message handlers
    dp.add_handler(MessageHandler(Filters.status_update.new_chat_members, welcome_member))
    dp.add_handler(MessageHandler(Filters.status_update.left_chat_member, goodbye_member))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, check_filters))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, hashtag_note))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, check_blacklist))
    dp.add_handler(MessageHandler(Filters.all & ~Filters.command, check_locks))
    dp.add_handler(MessageHandler(Filters.all & ~Filters.command, check_flood))

    # Callbacks
    dp.add_handler(CallbackQueryHandler(help_callback, pattern="^help_"))
    dp.add_handler(CallbackQueryHandler(captcha_callback, pattern="^captcha_"))

    logger.info(f"{'='*45}")
    logger.info(f"  {CALYPSO} started successfully!")
    logger.info(f"{'='*45}")

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
