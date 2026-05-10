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
import threading
from datetime import datetime, timedelta
from functools import wraps
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer

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
BOT_TOKEN = "8747553103:AAEQID2cXVmkLjDzYsfd17ZWoz2gcRHIxlc"
OWNER_ID   = 7518156464
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

def to_small_caps(text):
    normal = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    small  = "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"
    return text.translate(str.maketrans(normal, small))
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
                "ʏᴏᴜ ᴀʀᴇ ɴᴏᴛ ᴀɴ ᴀᴅᴍɪɴɪꜱᴛʀᴀᴛᴏʀ.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        return func(update, context)
    return wrapper

def owner_required(func):
    @wraps(func)
    def wrapper(update: Update, context: CallbackContext):
        if update.effective_user.id != OWNER_ID:
            update.message.reply_text("⛔ ᴏᴡɴᴇʀ ᴏɴʟʏ ᴄᴏᴍᴍᴀɴᴅ.")
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
            # Numeric ID — try to get the user's name via Telegram
            try:
                user = context.bot.get_chat(int(arg))
                return int(arg), user.first_name or str(arg)
            except:
                return int(arg), str(arg)
        elif arg.startswith("@"):
            # Username — resolve to user ID via Telegram API
            try:
                user = context.bot.get_chat(arg)
                return user.id, user.first_name or arg
            except Exception as e:
                # Could not resolve username
                update.message.reply_text(
                    f"❌ ꜰᴀɪʟᴇᴅ ᴛᴏ ꜰɪɴᴅ ᴜꜱᴇʀ {arg}.\n"
                    "ᴛɪᴘ: ʀᴇᴘʟʏ ᴅɪʀᴇᴄᴛʟʏ ᴛᴏ ᴛʜᴇ ᴜꜱᴇʀ'ꜱ ᴍᴇꜱꜱᴀɢᴇ ꜰᴏʀ ʙᴇꜱᴛ ʀᴇꜱᴜʟᴛꜱ."
                )
                return None, None
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
        InlineKeyboardButton("ᴀᴅᴅ ᴛᴏ ɢʀᴏᴜᴘ", url=f"https://t.me/{context.bot.username}?startgroup=true"),
        InlineKeyboardButton("ᴊᴏɪɴ ꜰᴏʀ ᴜᴘᴅᴀᴛᴇ", url="https://t.me/calypsoGc"),
    ]])
    update.message.reply_text(
        "ʜᴇʏ ᴛʜᴇʀᴇ! ᴍʏ ɴᴀᴍᴇ ɪꜱ ᴄᴀʟʏᴘꜱᴏ - ɪ'ᴍ ʜᴇʀᴇ ᴛᴏ ʜᴇʟᴘ ʏᴏᴜ ᴍᴀɴᴀɢᴇ ʏᴏᴜʀ ɢʀᴏᴜᴘꜱ! ᴜꜱᴇ /ʜᴇʟᴘ ᴛᴏ ꜰɪɴᴅ ᴏᴜᴛ ʜᴏᴡ ᴛᴏ ᴜꜱᴇ ᴍᴇ ᴛᴏ ᴍʏ ꜰᴜʟʟ ᴘᴏᴛᴇɴᴛɪᴀʟ.\n\n"
        "ᴊᴏɪɴ ᴄʜᴀɴɴᴇʟ ᴛᴏ ɢᴇᴛ ɪɴꜰᴏʀᴍᴀᴛɪᴏɴ ᴏɴ ᴀʟʟ ᴛʜᴇ ʟᴀᴛᴇꜱᴛ ᴜᴘᴅᴀᴛᴇꜱ.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=buttons
    )

def help_command(update: Update, context: CallbackContext):
    categories = [
        ["ᴍᴏᴅᴇʀᴀᴛɪᴏɴ", "help_mod"],
        ["ᴡᴇʟᴄᴏᴍᴇ", "help_welcome"],
        ["ᴡᴀʀɴɪɴɢꜱ", "help_warns"],
        ["ʟᴏᴄᴋꜱ", "help_locks"],
        ["ꜰɪʟᴛᴇʀꜱ", "help_filters"],
        ["ɴᴏᴛᴇꜱ", "help_notes"],
        ["ʙʟᴀᴄᴋʟɪꜱᴛ", "help_blacklist"],
        ["ᴀᴅᴍɪɴ", "help_admin"],
        ["ᴀɴᴛɪꜰʟᴏᴏᴅ", "help_antiflood"],
        ["ᴄᴀᴘᴛᴄʜᴀ", "help_captcha"],
        ["ʀᴜʟᴇꜱ", "help_rules"],
        ["ᴘɪɴ", "help_pin"],
        ["ɪɴꜰᴏ", "help_info"],
        ["ᴘᴜʀɢᴇꜱ", "help_purges"],
        ["ʙʀᴏᴀᴅᴄᴀꜱᴛ", "help_broadcast"],
    ]
    # 3 per row like Rose bot
    kb = []
    row = []
    for i, (label, data) in enumerate(categories):
        row.append(InlineKeyboardButton(label, callback_data=data))
        if len(row) == 3:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    update.message.reply_text(
        "ʜᴇʏ! ᴍʏ ɴᴀᴍᴇ ɪꜱ ᴄᴀʟʏᴘꜱᴏ ɪ ᴀᴍ ᴀ ɢʀᴏᴜᴘ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ ʙᴏᴛ, ʜᴇʀᴇ ᴛᴏ ʜᴇʟᴘ ʏᴏᴜ ɢᴇᴛ ᴀʀᴏᴜɴᴅ ᴀɴᴅ ᴋᴇᴇᴘ ᴛʜᴇ ᴏʀᴅᴇʀ ɪɴ ʏᴏᴜʀ ɢʀᴏᴜᴘꜱ! ɪ ʜᴀᴠᴇ ʟᴏᴛꜱ ᴏꜰ ʜᴀɴᴅʏ ꜰᴇᴀᴛᴜʀᴇꜱ, ꜱᴜᴄʜ ᴀꜱ ꜰʟᴏᴏᴅ ᴄᴏɴᴛʀᴏʟ, ᴀ ᴡᴀʀɴɪɴɢ ꜱʏꜱᴛᴇᴍ, ᴀ ɴᴏᴛᴇ ᴋᴇᴇᴘɪɴɢ ꜱʏꜱᴛᴇᴍ, ᴀɴᴅ ᴇᴠᴇɴ ᴘʀᴇᴅᴇᴛᴇʀᴍɪɴᴇᴅ ʀᴇᴘʟɪᴇꜱ ᴏɴ ᴄᴇʀᴛᴀɪɴ ᴋᴇʏᴡᴏʀᴅꜱ.\n\n"
        "ʜᴇʟᴘꜰᴜʟ ᴄᴏᴍᴍᴀɴᴅꜱ:\n"
        "- /ꜱᴛᴀʀᴛ: ꜱᴛᴀʀᴛꜱ ᴍᴇ! ʏᴏᴜ'ᴠᴇ ᴘʀᴏʙᴀʙʟʏ ᴀʟʀᴇᴀᴅʏ ᴜꜱᴇᴅ ᴛʜɪꜱ.\n"
        "- /ʜᴇʟᴘ: ꜱᴇɴᴅꜱ ᴛʜɪꜱ ᴍᴇꜱꜱᴀɢᴇ\n\n"
        "ᴛʜᴀɴᴋꜱ ꜰᴏʀ ꜱᴜᴘᴘᴏʀᴛɪɴɢ\n\n"
        "ᴀʟʟ ᴄᴏᴍᴍᴀɴᴅꜱ ᴄᴀɴ ʙᴇ ᴜꜱᴇᴅ ᴡɪᴛʜ ᴛʜᴇ ꜰᴏʟʟᴏᴡɪɴɢ: / !",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb)
    )

HELP_TEXTS = {
    "help_mod": (
        "ᴍᴏᴅᴇʀᴀᴛɪᴏɴ\n\n"
        "/ʙᴀɴ [user] [reason] — ʙᴀɴ ᴜꜱᴇʀ\n"
        "/ᴜɴʙᴀɴ [user] — ᴜɴʙᴀɴ ᴜꜱᴇʀ\n"
        "/ᴋɪᴄᴋ [user] [reason] — ᴋɪᴄᴋ ᴜꜱᴇʀ\n"
        "/ᴍᴜᴛᴇ [user] [time] — ᴍᴜᴛᴇ ᴜꜱᴇʀ\n"
        "/ᴜɴᴍᴜᴛᴇ [user] — ᴜɴᴍᴜᴛᴇ ᴜꜱᴇʀ\n"
        "/ᴛᴍᴜᴛᴇ [user] [time] — ᴛᴇᴍᴘ ᴍᴜᴛᴇ\n"
        "/ᴛʙᴀɴ [user] [time] — ᴛᴇᴍᴘ ʙᴀɴ\n"
    ),
    "help_welcome": (
        "ᴡᴇʟᴄᴏᴍᴇ\n\n"
        "/ꜱᴇᴛᴡᴇʟᴄᴏᴍᴇ [msg] — ꜱᴇᴛ ᴡᴇʟᴄᴏᴍᴇ ᴍᴇꜱꜱᴀɢᴇ\n"
        "/ꜱᴇᴛɢᴏᴏᴅʙʏᴇ [msg] — ꜱᴇᴛ ɢᴏᴏᴅʙʏᴇ ᴍᴇꜱꜱᴀɢᴇ\n"
        "/ᴡᴇʟᴄᴏᴍᴇ ᴏɴ/ᴏꜰꜰ — ᴛᴏɢɢʟᴇ ᴡᴇʟᴄᴏᴍᴇ\n"
        "/ɢᴏᴏᴅʙʏᴇ ᴏɴ/ᴏꜰꜰ — ᴛᴏɢɢʟᴇ ɢᴏᴏᴅʙʏᴇ\n"
        "/ʀᴇꜱᴇᴛᴡᴇʟᴄᴏᴍᴇ — ʀᴇꜱᴇᴛ ᴛᴏ ᴅᴇꜰᴀᴜʟᴛ\n\n"
        "ᴜꜱᴇ {first}, {last}, {username}, {mention}, {chatname} ᴀꜱ ᴘʟᴀᴄᴇʜᴏʟᴅᴇʀꜱ.\n"
    ),
    "help_warns": (
        "ᴡᴀʀɴɪɴɢꜱ\n\n"
        "/ᴡᴀʀɴ [user] [reason] — ᴡᴀʀɴ ᴜꜱᴇʀ\n"
        "/ᴜɴᴡᴀʀɴ [user] — ʀᴇᴍᴏᴠᴇ ʟᴀꜱᴛ ᴡᴀʀɴ\n"
        "/ᴡᴀʀɴꜱ [user] — ᴄʜᴇᴄᴋ ᴡᴀʀɴꜱ\n"
        "/ʀᴇꜱᴇᴛᴡᴀʀɴꜱ [user] — ʀᴇꜱᴇᴛ ᴡᴀʀɴꜱ\n"
        f"ᴀᴜᴛᴏ-ʙᴀɴ ᴀꜰᴛᴇʀ *{WARN_LIMIT}* ᴡᴀʀɴꜱ.\n"
    ),
    "help_locks": (
        "ʟᴏᴄᴋꜱ\n\n"
        "/ʟᴏᴄᴋ [type] — ʟᴏᴄᴋ ᴍᴇꜱꜱᴀɢᴇ ᴛʏᴘᴇ\n"
        "/ᴜɴʟᴏᴄᴋ [type] — ᴜɴʟᴏᴄᴋ ᴍᴇꜱꜱᴀɢᴇ ᴛʏᴘᴇ\n"
        "/ʟᴏᴄᴋꜱ — ꜱʜᴏᴡ ᴄᴜʀʀᴇɴᴛ ʟᴏᴄᴋꜱ\n\n"
        "ᴛʏᴘᴇꜱ: ᴀʟʟ, ᴍᴇᴅɪᴀ, ꜱᴛɪᴄᴋᴇʀ, ɢɪꜰ, ᴜʀʟ, ꜰᴏʀᴡᴀʀᴅ, ᴘʜᴏᴛᴏ, ᴠɪᴅᴇᴏ, ᴠᴏɪᴄᴇ, ᴅᴏᴄᴜᴍᴇɴᴛ\n"
    ),
    "help_filters": (
        "ꜰɪʟᴛᴇʀꜱ\n\n"
        "/ꜰɪʟᴛᴇʀ [keyword] [reply] — ᴀᴅᴅ ᴀᴜᴛᴏ-ʀᴇᴘʟʏ ꜰɪʟᴛᴇʀ\n"
        "/ꜱᴛᴏᴘ [keyword] — ʀᴇᴍᴏᴠᴇ ꜰɪʟᴛᴇʀ\n"
        "/ꜰɪʟᴛᴇʀꜱ — ʟɪꜱᴛ ᴀʟʟ ꜰɪʟᴛᴇʀꜱ\n"
    ),
    "help_notes": (
        "ɴᴏᴛᴇꜱ\n\n"
        "/ꜱᴀᴠᴇ [name] [text] — ꜱᴀᴠᴇ ᴀ ɴᴏᴛᴇ\n"
        "/ɢᴇᴛ [name] ᴏʀ #name — ʀᴇᴛʀɪᴇᴠᴇ ɴᴏᴛᴇ\n"
        "/ᴄʟᴇᴀʀ [name] — ᴅᴇʟᴇᴛᴇ ɴᴏᴛᴇ\n"
        "/ɴᴏᴛᴇꜱ — ʟɪꜱᴛ ᴀʟʟ ɴᴏᴛᴇꜱ\n"
    ),
    "help_blacklist": (
        "ʙʟᴀᴄᴋʟɪꜱᴛ\n\n"
        "/ᴀᴅᴅʙʟᴀᴄᴋʟɪꜱᴛ [word] — ᴀᴅᴅ ᴡᴏʀᴅ ᴛᴏ ʙʟᴀᴄᴋʟɪꜱᴛ\n"
        "/ʀᴍʙʟᴀᴄᴋʟɪꜱᴛ [word] — ʀᴇᴍᴏᴠᴇ ᴡᴏʀᴅ\n"
        "/ʙʟᴀᴄᴋʟɪꜱᴛ — ꜱʜᴏᴡ ʙʟᴀᴄᴋʟɪꜱᴛ\n"
        "ᴍᴇꜱꜱᴀɢᴇꜱ ᴡɪᴛʜ ʙʟᴀᴄᴋʟɪꜱᴛᴇᴅ ᴡᴏʀᴅꜱ ᴀʀᴇ ᴀᴜᴛᴏ-ᴅᴇʟᴇᴛᴇᴅ.\n"
    ),
    "help_admin": (
        "ᴀᴅᴍɪɴ\n\n"
        "/ᴘʀᴏᴍᴏᴛᴇ [user] — ᴘʀᴏᴍᴏᴛᴇ ᴛᴏ ᴀᴅᴍɪɴ\n"
        "/ᴅᴇᴍᴏᴛᴇ [user] — ᴅᴇᴍᴏᴛᴇ ᴀᴅᴍɪɴ\n"
        "/ᴀᴅᴍɪɴʟɪꜱᴛ — ʟɪꜱᴛ ᴀʟʟ ᴀᴅᴍɪɴꜱ\n"
        "/ɪᴅ — ɢᴇᴛ ᴜꜱᴇʀ/ᴄʜᴀᴛ ɪᴅ\n"
        "/ɪɴꜰᴏ [user] — ɢᴇᴛ ᴜꜱᴇʀ ɪɴꜰᴏ\n"
    ),
    "help_antiflood": (
        "ᴀɴᴛɪꜰʟᴏᴏᴅ\n\n"
        "/ꜱᴇᴛꜰʟᴏᴏᴅ [n/off] — ꜱᴇᴛ ᴀɴᴛɪ-ꜰʟᴏᴏᴅ ʟɪᴍɪᴛ\n"
        "/ᴀɴᴛɪꜰʟᴏᴏᴅ — ᴄʜᴇᴄᴋ ꜰʟᴏᴏᴅ ꜱᴛᴀᴛᴜꜱ\n\n"
        "ᴜꜱᴇʀꜱ ᴡʜᴏ ꜱᴇɴᴅ ᴍᴏʀᴇ ᴍᴇꜱꜱᴀɢᴇꜱ ᴛʜᴀɴ ᴛʜᴇ ʟɪᴍɪᴛ ᴡɪʟʟ ʙᴇ ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ ᴋɪᴄᴋᴇᴅ.\n"
    ),
    "help_captcha": (
        "ᴄᴀᴘᴛᴄʜᴀ\n\n"
        "/ᴄᴀᴘᴛᴄʜᴀ ᴏɴ/ᴏꜰꜰ — ᴛᴏɢɢʟᴇ ᴄᴀᴘᴛᴄʜᴀ ꜰᴏʀ ɴᴇᴡ ᴍᴇᴍʙᴇʀꜱ\n\n"
        "ᴡʜᴇɴ ᴇɴᴀʙʟᴇᴅ, ɴᴇᴡ ᴍᴇᴍʙᴇʀꜱ ᴍᴜꜱᴛ ᴄᴏᴍᴘʟᴇᴛᴇ ᴀ ᴄᴀᴘᴛᴄʜᴀ ʙᴇꜰᴏʀᴇ ꜱᴘᴇᴀᴋɪɴɢ.\n"
    ),
    "help_rules": (
        "ʀᴜʟᴇꜱ\n\n"
        "/ꜱᴇᴛʀᴜʟᴇꜱ [text] — ꜱᴇᴛ ɢʀᴏᴜᴘ ʀᴜʟᴇꜱ\n"
        "/ʀᴜʟᴇꜱ — ꜱʜᴏᴡ ɢʀᴏᴜᴘ ʀᴜʟᴇꜱ\n"
    ),
    "help_pin": (
        "ᴘɪɴ\n\n"
        "/ᴘɪɴ — ᴘɪɴ ʀᴇᴘʟɪᴇᴅ ᴍᴇꜱꜱᴀɢᴇ\n"
        "/ᴘɪɴ ꜱɪʟᴇɴᴛ — ᴘɪɴ ᴡɪᴛʜᴏᴜᴛ ɴᴏᴛɪꜰɪᴄᴀᴛɪᴏɴ\n"
        "/ᴜɴᴘɪɴ — ᴜɴᴘɪɴ ᴍᴇꜱꜱᴀɢᴇ\n"
    ),
    "help_info": (
        "ɪɴꜰᴏ\n\n"
        "/ɪᴅ — ɢᴇᴛ ʏᴏᴜʀ ɪᴅ ᴏʀ ʀᴇᴘʟɪᴇᴅ ᴜꜱᴇʀ'ꜱ ɪᴅ\n"
        "/ɪɴꜰᴏ [user] — ɢᴇᴛ ᴜꜱᴇʀ ɪɴꜰᴏ ᴀɴᴅ ᴡᴀʀɴꜱ\n"
        "/ᴀᴅᴍɪɴʟɪꜱᴛ — ꜱʜᴏᴡ ᴀʟʟ ɢʀᴏᴜᴘ ᴀᴅᴍɪɴꜱ\n"
    ),
    "help_purges": (
        "ᴘᴜʀɢᴇꜱ\n\n"
        "/ᴘᴜʀɢᴇ [n] — ᴅᴇʟᴇᴛᴇ ʟᴀꜱᴛ ɴ ᴍᴇꜱꜱᴀɢᴇꜱ\n"
        "/ᴅᴇʟ — ᴅᴇʟᴇᴛᴇ ʀᴇᴘʟɪᴇᴅ ᴍᴇꜱꜱᴀɢᴇ\n"
    ),
    "help_broadcast": (
        "ʙʀᴏᴀᴅᴄᴀꜱᴛ\n\n"
        "/ʙʀᴏᴀᴅᴄᴀꜱᴛ [message] — ꜱᴇɴᴅ ᴍᴇꜱꜱᴀɢᴇ ᴛᴏ ᴀʟʟ ᴄʜᴀᴛꜱ\n\n"
        "ᴏᴡɴᴇʀ ᴏɴʟʏ ᴄᴏᴍᴍᴀɴᴅ.\n"
    ),
}

def help_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    data = query.data
    if data == "help_main":
        categories = [
            ["ᴍᴏᴅᴇʀᴀᴛɪᴏɴ", "help_mod"],
            ["ᴡᴇʟᴄᴏᴍᴇ", "help_welcome"],
            ["ᴡᴀʀɴɪɴɢꜱ", "help_warns"],
            ["ʟᴏᴄᴋꜱ", "help_locks"],
            ["ꜰɪʟᴛᴇʀꜱ", "help_filters"],
            ["ɴᴏᴛᴇꜱ", "help_notes"],
            ["ʙʟᴀᴄᴋʟɪꜱᴛ", "help_blacklist"],
            ["ᴀᴅᴍɪɴ", "help_admin"],
            ["ᴀɴᴛɪꜰʟᴏᴏᴅ", "help_antiflood"],
            ["ᴄᴀᴘᴛᴄʜᴀ", "help_captcha"],
            ["ʀᴜʟᴇꜱ", "help_rules"],
            ["ᴘɪɴ", "help_pin"],
            ["ɪɴꜰᴏ", "help_info"],
            ["ᴘᴜʀɢᴇꜱ", "help_purges"],
            ["ʙʀᴏᴀᴅᴄᴀꜱᴛ", "help_broadcast"],
        ]
        kb = []
        row = []
        for i, (label, cdata) in enumerate(categories):
            row.append(InlineKeyboardButton(label, callback_data=cdata))
            if len(row) == 3:
                kb.append(row)
                row = []
        if row:
            kb.append(row)
        query.edit_message_text(
            "ʜᴇʏ! ᴍʏ ɴᴀᴍᴇ ɪꜱ ᴄᴀʟʏᴘꜱᴏ ɪ ᴀᴍ ᴀ ɢʀᴏᴜᴘ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ ʙᴏᴛ, ʜᴇʀᴇ ᴛᴏ ʜᴇʟᴘ ʏᴏᴜ ɢᴇᴛ ᴀʀᴏᴜɴᴅ ᴀɴᴅ ᴋᴇᴇᴘ ᴛʜᴇ ᴏʀᴅᴇʀ ɪɴ ʏᴏᴜʀ ɢʀᴏᴜᴘꜱ!\n\n"
            "ᴀʟʟ ᴄᴏᴍᴍᴀɴᴅꜱ ᴄᴀɴ ʙᴇ ᴜꜱᴇᴅ ᴡɪᴛʜ ᴛʜᴇ ꜰᴏʟʟᴏᴡɪɴɢ: / !",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb)
        )
    elif data in HELP_TEXTS:
        kb = [[InlineKeyboardButton("ʙᴀᴄᴋ", callback_data="help_main")]]
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
        update.message.reply_text("⚠️ ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴜꜱᴇʀ ᴏʀ ᴘʀᴏᴠɪᴅᴇ ᴜꜱᴇʀ ɪᴅ.")
        return
    reason = " ".join(context.args[1:]) if context.args and len(context.args) > 1 else "ɴᴏ ʀᴇᴀꜱᴏɴ"
    try:
        update.effective_chat.kick_member(uid)
        update.message.reply_text(
            f"{calypso_header('🔨 ᴜꜱᴇʀ ʙᴀɴɴᴇᴅ')}"
            f"*ᴜꜱᴇʀ:* [{to_small_caps(name)}](tg://user?id={uid})\n"
            f"*ʀᴇᴀꜱᴏɴ:* {reason}\n"
            f"*ʙʏ:* {update.effective_user.first_name}",
            parse_mode=ParseMode.MARKDOWN
        )
        log_action(context, update.effective_chat, "BAN", uid, update.effective_user.first_name, reason)
    except BadRequest as e:
        update.message.reply_text(f"❌ ꜰᴀɪʟᴇᴅ: {e.message}")

@admin_required
def unban(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid:
        update.message.reply_text("⚠️ ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴜꜱᴇʀ ᴏʀ ᴘʀᴏᴠɪᴅᴇ ᴜꜱᴇʀ ɪᴅ.")
        return
    try:
        update.effective_chat.unban_member(uid)
        update.message.reply_text(
            f"{calypso_header('✅ ᴜꜱᴇʀ ᴜɴʙᴀɴɴᴇᴅ')}"
            f"*ᴜꜱᴇʀ:* [{to_small_caps(name)}](tg://user?id={uid})",
            parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest as e:
        update.message.reply_text(f"❌ ꜰᴀɪʟᴇᴅ: {e.message}")

@admin_required
def kick(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid:
        update.message.reply_text("⚠️ ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴜꜱᴇʀ ᴏʀ ᴘʀᴏᴠɪᴅᴇ ᴜꜱᴇʀ ɪᴅ.")
        return
    reason = " ".join(context.args[1:]) if context.args and len(context.args) > 1 else "ɴᴏ ʀᴇᴀꜱᴏɴ"
    try:
        update.effective_chat.kick_member(uid)
        update.effective_chat.unban_member(uid)  # unban so they can rejoin
        update.message.reply_text(
            f"{calypso_header('👢 ᴜꜱᴇʀ ᴋɪᴄᴋᴇᴅ')}"
            f"*ᴜꜱᴇʀ:* [{to_small_caps(name)}](tg://user?id={uid})\n"
            f"*ʀᴇᴀꜱᴏɴ:* {reason}",
            parse_mode=ParseMode.MARKDOWN
        )
        log_action(context, update.effective_chat, "KICK", uid, update.effective_user.first_name, reason)
    except BadRequest as e:
        update.message.reply_text(f"❌ ꜰᴀɪʟᴇᴅ: {e.message}")

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
        update.message.reply_text("⚠️ ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴜꜱᴇʀ ᴏʀ ᴘʀᴏᴠɪᴅᴇ ᴜꜱᴇʀ ɪᴅ.")
        return
    try:
        update.effective_chat.restrict_member(
            uid,
            permissions=ChatPermissions(can_send_messages=False)
        )
        update.message.reply_text(
            f"{calypso_header('🔇 ᴜꜱᴇʀ ᴍᴜᴛᴇᴅ')}"
            f"*ᴜꜱᴇʀ:* [{to_small_caps(name)}](tg://user?id={uid})",
            parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest as e:
        update.message.reply_text(f"❌ ꜰᴀɪʟᴇᴅ: {e.message}")

@admin_required
def unmute(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid:
        update.message.reply_text("⚠️ ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴜꜱᴇʀ ᴏʀ ᴘʀᴏᴠɪᴅᴇ ᴜꜱᴇʀ ɪᴅ.")
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
            f"*ᴜꜱᴇʀ:* [{to_small_caps(name)}](tg://user?id={uid})",
            parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest as e:
        update.message.reply_text(f"❌ ꜰᴀɪʟᴇᴅ: {e.message}")

@admin_required
def tmute(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid or not context.args:
        update.message.reply_text("ᴜꜱᴀɢᴇ: /tmute [ᴜꜱᴇʀ] [ᴛɪᴍᴇ ᴇ.ɢ. 10m, 2h, 1d]")
        return
    duration = parse_time(context.args[-1])
    if not duration:
        update.message.reply_text("❌ ɪɴᴠᴀʟɪᴅ ᴛɪᴍᴇ. ᴜꜱᴇ: 10m, 2h, 1d")
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
            f"*ᴜꜱᴇʀ:* [{to_small_caps(name)}](tg://user?id={uid})\n"
            f"*ᴅᴜʀᴀᴛɪᴏɴ:* `{context.args[-1]}`\n"
            f"*ᴜɴᴛɪʟ:* `{until.strftime('%Y-%m-%d %H:%M')}`",
            parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest as e:
        update.message.reply_text(f"❌ ꜰᴀɪʟᴇᴅ: {e.message}")

@admin_required
def tban(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid or not context.args:
        update.message.reply_text("ᴜꜱᴀɢᴇ: /tban [ᴜꜱᴇʀ] [ᴛɪᴍᴇ ᴇ.ɢ. 10m, 2h, 1d]")
        return
    duration = parse_time(context.args[-1])
    if not duration:
        update.message.reply_text("❌ ɪɴᴠᴀʟɪᴅ ᴛɪᴍᴇ. ᴜꜱᴇ: 10m, 2h, 1d")
        return
    until = datetime.now() + timedelta(seconds=duration)
    try:
        update.effective_chat.kick_member(uid, until_date=until)
        update.message.reply_text(
            f"{calypso_header('⏳ ᴛᴇᴍᴘ ʙᴀɴ')}"
            f"*ᴜꜱᴇʀ:* [{to_small_caps(name)}](tg://user?id={uid})\n"
            f"*ᴅᴜʀᴀᴛɪᴏɴ:* `{context.args[-1]}`\n"
            f"*ᴜɴᴛɪʟ:* `{until.strftime('%Y-%m-%d %H:%M')}`",
            parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest as e:
        update.message.reply_text(f"❌ ꜰᴀɪʟᴇᴅ: {e.message}")

# ═══════════════════════════════════════════════════════
#  4. WARN SYSTEM
# ═══════════════════════════════════════════════════════

@admin_required
def warn(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid:
        update.message.reply_text("⚠️ ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴜꜱᴇʀ ᴏʀ ᴘʀᴏᴠɪᴅᴇ ᴜꜱᴇʀ ɪᴅ.")
        return
    chat_id = update.effective_chat.id
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "ɴᴏ ʀᴇᴀꜱᴏɴ"
    db["warns"][chat_id][uid] += 1
    count = db["warns"][chat_id][uid]
    if count >= WARN_LIMIT:
        try:
            update.effective_chat.kick_member(uid)
            db["warns"][chat_id][uid] = 0
            update.message.reply_text(
                f"{calypso_header('🚫 ᴀᴜᴛᴏ-ʙᴀɴ')}"
                f"*ᴜꜱᴇʀ:* [{to_small_caps(name)}](tg://user?id={uid})\n"
                f"ʀᴇᴀᴄʜᴇᴅ *{WARN_LIMIT} ᴡᴀʀɴꜱ* — ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ ʙᴀɴɴᴇᴅ.",
                parse_mode=ParseMode.MARKDOWN
            )
        except BadRequest as e:
            update.message.reply_text(f"❌ ᴄᴏᴜʟᴅ ɴᴏᴛ ʙᴀɴ: {e.message}")
    else:
        update.message.reply_text(
            f"{calypso_header('⚠️ ᴡᴀʀɴᴇᴅ')}"
            f"*ᴜꜱᴇʀ:* [{to_small_caps(name)}](tg://user?id={uid})\n"
            f"*ʀᴇᴀꜱᴏɴ:* {reason}\n"
            f"*ᴡᴀʀɴꜱ:* `{count}/{WARN_LIMIT}`",
            parse_mode=ParseMode.MARKDOWN
        )

@admin_required
def unwarn(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid:
        update.message.reply_text("⚠️ ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴜꜱᴇʀ ᴏʀ ᴘʀᴏᴠɪᴅᴇ ᴜꜱᴇʀ ɪᴅ.")
        return
    chat_id = update.effective_chat.id
    if db["warns"][chat_id][uid] > 0:
        db["warns"][chat_id][uid] -= 1
    count = db["warns"][chat_id][uid]
    update.message.reply_text(
        f"{calypso_header('✅ ᴡᴀʀɴ ʀᴇᴍᴏᴠᴇᴅ')}"
        f"*ᴜꜱᴇʀ:* [{to_small_caps(name)}](tg://user?id={uid})\n"
        f"*ᴡᴀʀɴꜱ:* `{count}/{WARN_LIMIT}`",
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
        f"*ᴜꜱᴇʀ:* [{to_small_caps(name)}](tg://user?id={uid})\n"
        f"*ᴡᴀʀɴꜱ:* `{count}/{WARN_LIMIT}`",
        parse_mode=ParseMode.MARKDOWN
    )

@admin_required
def resetwarns(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid:
        update.message.reply_text("⚠️ ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴜꜱᴇʀ ᴏʀ ᴘʀᴏᴠɪᴅᴇ ᴜꜱᴇʀ ɪᴅ.")
        return
    db["warns"][update.effective_chat.id][uid] = 0
    update.message.reply_text(
        f"✅ ᴡᴀʀɴꜱ ʀᴇꜱᴇᴛ ꜰᴏʀ [{to_small_caps(name)}](tg://user?id={uid}).",
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
        update.message.reply_text("ᴜꜱᴀɢᴇ: /setwelcome [ᴍᴇꜱꜱᴀɢᴇ]\nᴘʟᴀᴄᴇʜᴏʟᴅᴇʀꜱ: {first}, {last}, {username}, {mention}, {chatname}")
        return
    db["welcome"][chat_id]["msg"] = msg_text[1]
    update.message.reply_text("✅ ᴡᴇʟᴄᴏᴍᴇ ᴍᴇꜱꜱᴀɢᴇ ꜱᴀᴠᴇᴅ.")

@admin_required
def setgoodbye(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    msg_text = update.message.text.split(None, 1)
    if len(msg_text) < 2:
        update.message.reply_text("ᴜꜱᴀɢᴇ: /setgoodbye [ᴍᴇꜱꜱᴀɢᴇ]")
        return
    db["goodbye"][chat_id]["msg"] = msg_text[1]
    update.message.reply_text("✅ ɢᴏᴏᴅʙʏᴇ ᴍᴇꜱꜱᴀɢᴇ ꜱᴀᴠᴇᴅ.")

@admin_required
def toggle_welcome(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    args = context.args
    cmd = update.message.text.split()[0].lstrip("/").lower()
    key = "welcome" if "welcome" in cmd else "goodbye"
    if args and args[0].lower() in ("on", "off"):
        db[key][chat_id]["enabled"] = args[0].lower() == "on"
        status_str = "ᴏɴ" if db[key][chat_id]['enabled'] else "ᴏꜰꜰ"
        key_str = "ᴡᴇʟᴄᴏᴍᴇ" if key == "welcome" else "ɢᴏᴏᴅʙʏᴇ"
        update.message.reply_text(f"✅ {key_str} ɪꜱ ɴᴏᴡ *{status_str}*.", parse_mode=ParseMode.MARKDOWN)
    else:
        update.message.reply_text(f"ᴜꜱᴀɢᴇ: /{key} ᴏɴ/ᴏꜰꜰ")

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
                InlineKeyboardButton("✅ ɪ'ᴍ ʜᴜᴍᴀɴ — ᴄʟɪᴄᴋ ᴛᴏ ᴠᴇʀɪꜰʏ", callback_data=f"captcha_{chat_id}_{member.id}")
            ]])
            try:
                update.effective_chat.restrict_member(
                    member.id,
                    permissions=ChatPermissions(can_send_messages=False)
                )
            except:
                pass
            m = update.message.reply_text(
                f"👋 ᴡᴇʟᴄᴏᴍᴇ [{member.first_name}](tg://user?id={member.id})!\n"
                "ᴘʟᴇᴀꜱᴇ ᴠᴇʀɪꜰʏ ʏᴏᴜ'ʀᴇ ʜᴜᴍᴀɴ ᴡɪᴛʜɪɴ 60 ꜱᴇᴄᴏɴᴅꜱ.",
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
            "ᴡᴇʟᴄᴏᴍᴇ {{mention}} ᴛᴏ *{{chatname}}*!\n"
            "ᴘʟᴇᴀꜱᴇ ʀᴇᴀᴅ ᴛʜᴇ ʀᴜʟᴇꜱ ᴀɴᴅ ᴇɴᴊᴏʏ ʏᴏᴜʀ ꜱᴛᴀʏ. 🎉"
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
    template = cfg["msg"] or f"👋 *{member.first_name}* ʜᴀꜱ ʟᴇꜰᴛ *{chat.title}*. ɢᴏᴏᴅʙʏᴇ!"
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
        query.answer("ᴛʜɪꜱ ᴄᴀᴘᴛᴄʜᴀ ɪꜱ ɴᴏᴛ ꜰᴏʀ ʏᴏᴜ!", show_alert=True)
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
    query.edit_message_text(f"✅ [{query.from_user.first_name}](tg://user?id={user_id}) ᴠᴇʀɪꜰɪᴇᴅ!", parse_mode=ParseMode.MARKDOWN)
    query.answer("ᴠᴇʀɪꜰɪᴇᴅ! ᴡᴇʟᴄᴏᴍᴇ!")

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
            update.message.reply_text(f"🗑 ᴅᴇʟᴇᴛᴇᴅ ʟᴀꜱᴛ `{n}` ᴍᴇꜱꜱᴀɢᴇꜱ.", parse_mode=ParseMode.MARKDOWN)
            return
        update.message.reply_text("ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴍᴇꜱꜱᴀɢᴇ ᴛᴏ ᴘᴜʀɢᴇ ꜰʀᴏᴍ, ᴏʀ ᴜꜱᴇ /purge [n].")
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
    context.bot.send_message(msg.chat_id, f"🗑 ᴘᴜʀɢᴇᴅ `{len(ids)}` ᴍᴇꜱꜱᴀɢᴇꜱ.", parse_mode=ParseMode.MARKDOWN)

@admin_required
def delete_msg(update: Update, context: CallbackContext):
    if update.message.reply_to_message:
        try:
            update.message.reply_to_message.delete()
            update.message.delete()
        except BadRequest:
            update.message.reply_text("❌ ᴄᴀɴ'ᴛ ᴅᴇʟᴇᴛᴇ ᴛʜᴀᴛ ᴍᴇꜱꜱᴀɢᴇ.")

# ═══════════════════════════════════════════════════════
#  7. FILTERS (Auto-Reply)
# ═══════════════════════════════════════════════════════

@admin_required
def add_filter(update: Update, context: CallbackContext):
    parts = update.message.text.split(None, 2)
    if len(parts) < 3:
        update.message.reply_text("ᴜꜱᴀɢᴇ: /filter [ᴋᴇʏᴡᴏʀᴅ] [ʀᴇᴘʟʏ ᴛᴇxᴛ]")
        return
    chat_id = update.effective_chat.id
    keyword, reply = parts[1].lower(), parts[2]
    db["filters"][chat_id][keyword] = reply
    update.message.reply_text(f"✅ ꜰɪʟᴛᴇʀ `{keyword}` ᴀᴅᴅᴇᴅ.", parse_mode=ParseMode.MARKDOWN)

@admin_required
def stop_filter(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text("ᴜꜱᴀɢᴇ: /stop [ᴋᴇʏᴡᴏʀᴅ]")
        return
    chat_id = update.effective_chat.id
    kw = context.args[0].lower()
    db["filters"][chat_id].pop(kw, None)
    update.message.reply_text(f"✅ ꜰɪʟᴛᴇʀ `{kw}` ʀᴇᴍᴏᴠᴇᴅ.", parse_mode=ParseMode.MARKDOWN)

def list_filters(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    fltrs = db["filters"][chat_id]
    if not fltrs:
        update.message.reply_text("ɴᴏ ꜰɪʟᴛᴇʀꜱ ꜱᴇᴛ.")
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
        update.message.reply_text("ᴜꜱᴀɢᴇ: /save [ɴᴀᴍᴇ] [ᴛᴇxᴛ]")
        return
    chat_id = update.effective_chat.id
    name, text = parts[1].lower(), parts[2]
    db["notes"][chat_id][name] = text
    update.message.reply_text(f"✅ ɴᴏᴛᴇ `{name}` ꜱᴀᴠᴇᴅ. ʀᴇᴛʀɪᴇᴠᴇ ᴡɪᴛʜ `/get {name}` ᴏʀ `#{name}`", parse_mode=ParseMode.MARKDOWN)

def get_note(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    name = context.args[0].lower() if context.args else None
    if not name:
        update.message.reply_text("ᴜꜱᴀɢᴇ: /get [ɴᴀᴍᴇ]")
        return
    note = db["notes"][chat_id].get(name)
    if note:
        update.message.reply_text(note, parse_mode=ParseMode.MARKDOWN)
    else:
        update.message.reply_text(f"❌ ɴᴏ ɴᴏᴛᴇ ɴᴀᴍᴇᴅ `{name}`.", parse_mode=ParseMode.MARKDOWN)

@admin_required
def clear_note(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    name = context.args[0].lower() if context.args else None
    if not name:
        update.message.reply_text("ᴜꜱᴀɢᴇ: /clear [ɴᴀᴍᴇ]")
        return
    db["notes"][chat_id].pop(name, None)
    update.message.reply_text(f"✅ ɴᴏᴛᴇ `{name}` ᴅᴇʟᴇᴛᴇᴅ.", parse_mode=ParseMode.MARKDOWN)

def list_notes(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    notes = db["notes"][chat_id]
    if not notes:
        update.message.reply_text("ɴᴏ ɴᴏᴛᴇꜱ ꜱᴀᴠᴇᴅ.")
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
        update.message.reply_text("ᴜꜱᴀɢᴇ: /addblacklist [ᴡᴏʀᴅ]")
        return
    chat_id = update.effective_chat.id
    word = " ".join(context.args).lower()
    db["blacklist"][chat_id].add(word)
    update.message.reply_text(f"✅ `{word}` ᴀᴅᴅᴇᴅ ᴛᴏ ʙʟᴀᴄᴋʟɪꜱᴛ.", parse_mode=ParseMode.MARKDOWN)

@admin_required
def rm_blacklist(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text("ᴜꜱᴀɢᴇ: /rmblacklist [ᴡᴏʀᴅ]")
        return
    chat_id = update.effective_chat.id
    word = " ".join(context.args).lower()
    db["blacklist"][chat_id].discard(word)
    update.message.reply_text(f"✅ `{word}` ʀᴇᴍᴏᴠᴇᴅ ꜰʀᴏᴍ ʙʟᴀᴄᴋʟɪꜱᴛ.", parse_mode=ParseMode.MARKDOWN)

def show_blacklist(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    bl = db["blacklist"][chat_id]
    if not bl:
        update.message.reply_text("ʙʟᴀᴄᴋʟɪꜱᴛ ɪꜱ ᴇᴍᴘᴛʏ.")
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
                    f"⚠️ [{update.effective_user.first_name}](tg://user?id={update.effective_user.id}) — ᴍᴇꜱꜱᴀɢᴇ ʀᴇᴍᴏᴠᴇᴅ (ʙʟᴀᴄᴋʟɪꜱᴛᴇᴅ ᴡᴏʀᴅ).",
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
        update.message.reply_text("ᴜꜱᴀɢᴇ: /lock [ᴛʏᴘᴇ]\nᴛʏᴘᴇꜱ: ꜱᴛɪᴄᴋᴇʀ, ɢɪꜰ, ᴜʀʟ, ꜰᴏʀᴡᴀʀᴅ, ᴘʜᴏᴛᴏ, ᴠɪᴅᴇᴏ, ᴠᴏɪᴄᴇ, ᴅᴏᴄᴜᴍᴇɴᴛ")
        return
    chat_id = update.effective_chat.id
    ltype = context.args[0].lower()
    if ltype == "all":
        db["locks"][chat_id] = {k: True for k in LOCK_TYPES}
        update.message.reply_text("🔒 ᴀʟʟ ᴍᴇꜱꜱᴀɢᴇ ᴛʏᴘᴇꜱ ʟᴏᴄᴋᴇᴅ.")
    elif ltype in LOCK_TYPES:
        db["locks"][chat_id][ltype] = True
        update.message.reply_text(f"🔒 `{ltype}` ʟᴏᴄᴋᴇᴅ.", parse_mode=ParseMode.MARKDOWN)
    else:
        update.message.reply_text(f"❌ ᴜɴᴋɴᴏᴡɴ ᴛʏᴘᴇ. ᴀᴠᴀɪʟᴀʙʟᴇ: ꜱᴛɪᴄᴋᴇʀ, ɢɪꜰ, ᴜʀʟ, ꜰᴏʀᴡᴀʀᴅ, ᴘʜᴏᴛᴏ, ᴠɪᴅᴇᴏ, ᴠᴏɪᴄᴇ, ᴅᴏᴄᴜᴍᴇɴᴛ")

@admin_required
def unlock(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text("ᴜꜱᴀɢᴇ: /unlock [ᴛʏᴘᴇ]")
        return
    chat_id = update.effective_chat.id
    ltype = context.args[0].lower()
    if ltype == "all":
        db["locks"][chat_id] = {}
        update.message.reply_text("🔓 ᴀʟʟ ʟᴏᴄᴋꜱ ʀᴇᴍᴏᴠᴇᴅ.")
    else:
        db["locks"][chat_id].pop(ltype, None)
        update.message.reply_text(f"🔓 `{ltype}` ᴜɴʟᴏᴄᴋᴇᴅ.", parse_mode=ParseMode.MARKDOWN)

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
        update.message.reply_text("ᴜꜱᴀɢᴇ: /setflood [ɴᴜᴍʙᴇʀ/off]")
        return
    val = context.args[0].lower()
    if val == "off":
        db["antiflood"][chat_id]["enabled"] = False
        update.message.reply_text("✅ ᴀɴᴛɪ-ꜰʟᴏᴏᴅ ᴅɪꜱᴀʙʟᴇᴅ.")
    elif val.isdigit() and int(val) > 1:
        db["antiflood"][chat_id]["enabled"] = True
        db["antiflood"][chat_id]["limit"] = int(val)
        update.message.reply_text(f"✅ ᴀɴᴛɪ-ꜰʟᴏᴏᴅ ꜱᴇᴛ ᴛᴏ `{val}` ᴍᴇꜱꜱᴀɢᴇꜱ.", parse_mode=ParseMode.MARKDOWN)
    else:
        update.message.reply_text("❌ ɪɴᴠᴀʟɪᴅ ᴠᴀʟᴜᴇ.")

def antiflood_status(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    cfg = db["antiflood"][chat_id]
    status = "ᴏɴ" if cfg["enabled"] else "ᴏꜰꜰ"
    update.message.reply_text(
        f"{calypso_header('⚡ ᴀɴᴛɪ-ꜰʟᴏᴏᴅ')}"
        f"ꜱᴛᴀᴛᴜꜱ: *{status}*\nʟɪᴍɪᴛ: `{cfg['limit']}` ᴍꜱɢ/5ꜱ",
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
                f"⚡ [{user.first_name}](tg://user?id={user.id}) ᴍᴜᴛᴇᴅ ꜰᴏʀ ꜰʟᴏᴏᴅɪɴɢ!",
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
        update.message.reply_text("ᴜꜱᴀɢᴇ: /setrules [ᴛᴇxᴛ]")
        return
    db["rules"][update.effective_chat.id] = parts[1]
    update.message.reply_text("✅ ʀᴜʟᴇꜱ ꜱᴀᴠᴇᴅ.")

def rules(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    r = db["rules"][chat_id]
    if not r:
        update.message.reply_text("ɴᴏ ʀᴜʟᴇꜱ ꜱᴇᴛ. ᴜꜱᴇ /setrules ᴛᴏ ꜱᴇᴛ ᴛʜᴇᴍ.")
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
        update.message.reply_text("⚠️ ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴜꜱᴇʀ ᴏʀ ᴘʀᴏᴠɪᴅᴇ ᴜꜱᴇʀ ɪᴅ.")
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
            f"[{to_small_caps(name)}](tg://user?id={uid}) ɪꜱ ɴᴏᴡ ᴀɴ ᴀᴅᴍɪɴ!",
            parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest as e:
        update.message.reply_text(f"❌ ꜰᴀɪʟᴇᴅ: {e.message}")

@admin_required
def demote(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid:
        update.message.reply_text("⚠️ ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴜꜱᴇʀ ᴏʀ ᴘʀᴏᴠɪᴅᴇ ᴜꜱᴇʀ ɪᴅ.")
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
            f"✅ [{to_small_caps(name)}](tg://user?id={uid}) ᴅᴇᴍᴏᴛᴇᴅ.",
            parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest as e:
        update.message.reply_text(f"❌ ꜰᴀɪʟᴇᴅ: {e.message}")

# ═══════════════════════════════════════════════════════
#  14. PIN / UNPIN
# ═══════════════════════════════════════════════════════

@admin_required
def pin(update: Update, context: CallbackContext):
    if not update.message.reply_to_message:
        update.message.reply_text("ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴍᴇꜱꜱᴀɢᴇ ᴛᴏ ᴘɪɴ ɪᴛ.")
        return
    loud = not (context.args and context.args[0].lower() in ("silent", "quiet"))
    try:
        update.effective_chat.pin_message(
            update.message.reply_to_message.message_id,
            disable_notification=not loud
        )
        update.message.reply_text("📌 ᴍᴇꜱꜱᴀɢᴇ ᴘɪɴɴᴇᴅ.")
    except BadRequest as e:
        update.message.reply_text(f"❌ ꜰᴀɪʟᴇᴅ: {e.message}")

@admin_required
def unpin(update: Update, context: CallbackContext):
    try:
        update.effective_chat.unpin_message()
        update.message.reply_text("📌 ᴍᴇꜱꜱᴀɢᴇ ᴜɴᴘɪɴɴᴇᴅ.")
    except BadRequest as e:
        update.message.reply_text(f"❌ ꜰᴀɪʟᴇᴅ: {e.message}")

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
        f"*ɴᴀᴍᴇ:* {user.first_name} {user.last_name or ''}\n"
        f"*ᴜꜱᴇʀɴᴀᴍᴇ:* @{user.username or 'ɴ/ᴀ'}\n"
        f"*ɪᴅ:* `{user.id}`\n"
        f"*ᴡᴀʀɴꜱ:* `{warns_count}/{WARN_LIMIT}`\n"
        f"*ʟɪɴᴋ:* [ᴘʀᴏꜰɪʟᴇ](tg://user?id={user.id})"
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
        status = "ᴏɴ" if db["captcha"][chat_id] else "ᴏꜰꜰ"
        update.message.reply_text(f"✅ ᴄᴀᴘᴛᴄʜᴀ ɪꜱ ɴᴏᴡ *{status}*.", parse_mode=ParseMode.MARKDOWN)
    else:
        update.message.reply_text("ᴜꜱᴀɢᴇ: /captcha ᴏɴ/ᴏꜰꜰ")

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
        update.message.reply_text("ᴜꜱᴀɢᴇ: /broadcast [ᴍᴇꜱꜱᴀɢᴇ]")
        return
    # In production, iterate over all known chat IDs
    update.message.reply_text(f"📢 ʙʀᴏᴀᴅᴄᴀꜱᴛ ꜱᴇɴᴛ:\n{parts[1]}")

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
    # ─── KEEP-ALIVE SERVER (for Koyeb) ─────────────────
    class KeepAliveHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write("CalypsoBot is alive!".encode("utf-8"))
        def log_message(self, format, *args):
            pass  # Suppress access logs

    def run_server():
        port = int(os.environ.get("PORT", 8080))
        server = HTTPServer(("0.0.0.0", port), KeepAliveHandler)
        server.serve_forever()

    threading.Thread(target=run_server, daemon=True).start()
    logger.info("Keep-alive server started.")
    # ───────────────────────────────────────────────────
    main()
