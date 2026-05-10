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
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is not set!")
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
    "user_cache": defaultdict(dict),                      # {chat_id: {user_id: {username, first_name}}}
    # ── CHAT STATS / RANKING ──────────────────────────────
    # {chat_id: {user_id: {"name": str, "total": int, "daily": {date_str: int}, "weekly": {week_str: int}}}}
    "chat_stats": defaultdict(lambda: defaultdict(lambda: {"name": "Unknown", "total": 0, "daily": defaultdict(int), "weekly": defaultdict(int)})),
}

WARN_LIMIT = 3  # Auto-ban after 3 warns

# ─── TELEGRAM-AS-DATABASE ──────────────────────────────
# Set STORAGE_CHANNEL env var to a private channel ID where the bot is admin.
# The bot saves chat_stats as JSON messages there, and reloads on restart.
# Snapshots happen every SNAPSHOT_INTERVAL seconds (default 5 min).
STORAGE_CHANNEL   = os.environ.get("STORAGE_CHANNEL", "")  # e.g. "-1001234567890"
SNAPSHOT_INTERVAL = 300  # seconds between saves

# Maps chat_id (int) -> message_id of its snapshot in STORAGE_CHANNEL
_snapshot_msg_ids: dict = {}
_snapshot_lock = threading.Lock()


def _stats_to_json(chat_id: int) -> str:
    """Serialise one chat's stats to a compact JSON string."""
    stats = db["chat_stats"].get(chat_id)
    if not stats:
        return "{}"
    out = {}
    for uid, d in stats.items():
        out[str(uid)] = {
            "name":     d.get("name", ""),
            "username": d.get("username", ""),
            "total":    d.get("total", 0),
            "daily":    dict(d.get("daily", {})),
            "weekly":   dict(d.get("weekly", {})),
        }
    return json.dumps(out, ensure_ascii=False)


def _json_to_stats(chat_id: int, raw: str):
    """Load serialised stats back into db."""
    try:
        data = json.loads(raw)
    except Exception:
        return
    for uid_str, d in data.items():
        uid = int(uid_str)
        stat = db["chat_stats"][chat_id][uid]
        stat["name"]     = d.get("name", "")
        stat["username"] = d.get("username", "")
        stat["total"]    = d.get("total", 0)
        stat["daily"]    = defaultdict(int, d.get("daily", {}))
        stat["weekly"]   = defaultdict(int, d.get("weekly", {}))


def _parse_and_load_snapshot(text: str, expected_cid: int = None):
    """Parse a snapshot message text and load into db."""
    try:
        lines = text.strip().splitlines()
        if not lines[0].startswith("#calypso_stats"):
            return
        chat_id = int(lines[1].strip())
        if expected_cid and chat_id != expected_cid:
            return
        json_blob = "\n".join(lines[2:])
        _json_to_stats(chat_id, json_blob)
    except Exception as e:
        logger.warning(f"_parse_and_load_snapshot error: {e}")


def tg_load_stats(bot):
    """On startup: read pinned index from storage channel and restore all snapshots."""
    if not STORAGE_CHANNEL:
        logger.info("ᴛɢ ꜱᴛᴏʀᴀɢᴇ: STORAGE_CHANNEL not set — running in memory-only mode.")
        return
    try:
        channel_id = int(STORAGE_CHANNEL)
        chat_obj   = bot.get_chat(channel_id)
        index_msg  = chat_obj.pinned_message
        if not index_msg or not index_msg.text or not index_msg.text.startswith("#calypso_index"):
            logger.info("ᴛɢ ꜱᴛᴏʀᴀɢᴇ: no index found — fresh start.")
            return
        # Parse index: each line is  "chat_id: message_id"
        for line in index_msg.text.strip().splitlines()[1:]:
            if ":" not in line:
                continue
            try:
                cid_str, mid_str = line.split(":", 1)
                cid = int(cid_str.strip())
                mid = int(mid_str.strip())
                _snapshot_msg_ids[cid] = mid
                # Forward the snapshot to read its text, then delete the copy
                fwd = bot.forward_message(
                    chat_id=channel_id,
                    from_chat_id=channel_id,
                    message_id=mid,
                    disable_notification=True
                )
                if fwd and fwd.text:
                    _parse_and_load_snapshot(fwd.text, cid)
                try:
                    bot.delete_message(channel_id, fwd.message_id)
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"tg_load_stats: failed on line '{line}': {e}")
        logger.info(f"ᴛɢ ꜱᴛᴏʀᴀɢᴇ: loaded {len(_snapshot_msg_ids)} chat(s) from channel.")
    except Exception as e:
        logger.warning(f"tg_load_stats error: {e}")


def _trim_stats_text(cid: int) -> str:
    """If JSON is too large, keep only last 7 daily keys and last 4 weekly keys."""
    stats = db["chat_stats"].get(cid, {})
    out = {}
    for uid, d in stats.items():
        daily_keys  = sorted(d.get("daily",  {}).keys())[-7:]
        weekly_keys = sorted(d.get("weekly", {}).keys())[-4:]
        out[str(uid)] = {
            "name":     d.get("name", ""),
            "username": d.get("username", ""),
            "total":    d.get("total", 0),
            "daily":    {k: d["daily"][k]  for k in daily_keys},
            "weekly":   {k: d["weekly"][k] for k in weekly_keys},
        }
    return f"#calypso_stats\n{cid}\n" + json.dumps(out, ensure_ascii=False)


def tg_save_stats(bot):
    """Save all chats' stats to the storage channel."""
    if not STORAGE_CHANNEL:
        return
    try:
        channel_id = int(STORAGE_CHANNEL)
        chat_ids   = list(db["chat_stats"].keys())
        with _snapshot_lock:
            for cid in chat_ids:
                text = f"#calypso_stats\n{cid}\n{_stats_to_json(cid)}"
                if len(text) > 4090:
                    text = _trim_stats_text(cid)
                mid = _snapshot_msg_ids.get(cid)
                if mid:
                    try:
                        bot.edit_message_text(chat_id=channel_id, message_id=mid, text=text)
                    except Exception:
                        msg = bot.send_message(channel_id, text, disable_notification=True)
                        _snapshot_msg_ids[cid] = msg.message_id
                else:
                    msg = bot.send_message(channel_id, text, disable_notification=True)
                    _snapshot_msg_ids[cid] = msg.message_id
            _update_index(bot, channel_id)
        logger.info(f"ᴛɢ ꜱᴛᴏʀᴀɢᴇ: saved {len(chat_ids)} chat(s).")
    except Exception as e:
        logger.warning(f"tg_save_stats error: {e}")


def _update_index(bot, channel_id: int):
    """Pin an index message listing all snapshot message IDs."""
    text = "#calypso_index\n" + "\n".join(f"{cid}: {mid}" for cid, mid in _snapshot_msg_ids.items())
    try:
        chat_obj = bot.get_chat(channel_id)
        if chat_obj.pinned_message and chat_obj.pinned_message.text and \
                chat_obj.pinned_message.text.startswith("#calypso_index"):
            bot.edit_message_text(
                chat_id=channel_id,
                message_id=chat_obj.pinned_message.message_id,
                text=text
            )
            return
    except Exception:
        pass
    try:
        msg = bot.send_message(channel_id, text, disable_notification=True)
        bot.pin_chat_message(channel_id, msg.message_id, disable_notification=True)
    except Exception as e:
        logger.warning(f"_update_index error: {e}")


def _snapshot_loop(bot):
    """Background thread: auto-save stats every SNAPSHOT_INTERVAL seconds."""
    time.sleep(30)  # let bot fully start first
    while True:
        try:
            tg_save_stats(bot)
        except Exception as e:
            logger.warning(f"snapshot_loop error: {e}")
        time.sleep(SNAPSHOT_INTERVAL)
# ───────────────────────────────────────────────────────

# Admin cache: {chat_id: (timestamp, [user_ids])}
_admin_cache: dict = {}
_ADMIN_CACHE_TTL = 300  # seconds

def get_admin_ids(chat) -> list:
    """Return list of admin user IDs, cached for 5 minutes."""
    now = time.time()
    cached = _admin_cache.get(chat.id)
    if cached and now - cached[0] < _ADMIN_CACHE_TTL:
        return cached[1]
    ids = [m.user.id for m in chat.get_administrators()]
    _admin_cache[chat.id] = (now, ids)
    return ids

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

def calypso_header(title):
    return f"⌁ {title}\n"

def sc(text):
    """Convert plain ASCII error text to small caps for consistent styling."""
    return to_small_caps(text)

def admin_required(func):
    @wraps(func)
    def wrapper(update: Update, context: CallbackContext):
        user = update.effective_user
        chat = update.effective_chat
        if chat.type == "private":
            return func(update, context)
        admins = get_admin_ids(chat)
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
            username = arg.lstrip("@").lower()
            # Method 1: Try direct Telegram API lookup
            try:
                user = context.bot.get_chat(arg)
                return user.id, user.first_name or arg
            except Exception:
                pass
            # Method 2: Search group admins (always accessible)
            try:
                chat = update.effective_chat
                admins_full = chat.get_administrators()
                for member in admins_full:
                    u = member.user
                    if u.username and u.username.lower() == username:
                        return u.id, u.first_name or arg
            except Exception:
                pass
            # Method 3: Check cached db from past messages
            for chat_id, user_map in db.get("user_cache", {}).items():
                for uid, udata in user_map.items():
                    if udata.get("username", "").lower() == username:
                        return uid, udata.get("first_name", arg)
            # All methods failed
            update.message.reply_text(
                f"❌ ꜰᴀɪʟᴇᴅ ᴛᴏ ꜰɪɴᴅ ᴜꜱᴇʀ {arg}.\n"
                "ᴛɪᴘ: ʀᴇᴘʟʏ ᴅɪʀᴇᴄᴛʟʏ ᴛᴏ ᴛʜᴇ ᴜꜱᴇʀ'ꜱ ᴍᴇꜱꜱᴀɢᴇ ꜰᴏʀ ʙᴇꜱᴛ ʀᴇꜱᴜʟᴛꜱ."
            )
            return None, None
    return None, None

def cache_user(update, context):
    """Cache every user who sends a message so we can resolve them by username later.
       Also tracks per-user message counts for the /ranking command."""
    if not update.message or not update.effective_user:
        return
    u = update.effective_user
    chat_id = update.effective_chat.id
    if u.username:
        db["user_cache"][chat_id][u.id] = {
            "username": u.username.lower(),
            "first_name": u.first_name or str(u.id)
        }
    # ── Track stats for ranking ──────────────────────────
    if update.effective_chat.type != "private":
        today_str = datetime.now().strftime("%Y-%m-%d")
        week_str  = datetime.now().strftime("%Y-W%W")
        stat = db["chat_stats"][chat_id][u.id]
        stat["name"]     = u.first_name or u.username or str(u.id)
        stat["username"] = u.username or ""
        stat["total"] += 1
        stat["daily"][today_str]  += 1
        stat["weekly"][week_str]  += 1

def log_action(context, chat, action, user, by, reason=""):
    if not LOG_CHANNEL:
        return
    try:
        context.bot.send_message(
            LOG_CHANNEL,
            f"*📋 ʟᴏɢ*\n"
            f"*ᴄʜᴀᴛ:* {chat.title} (`{chat.id}`)\n"
            f"*ᴀᴄᴛɪᴏɴ:* `{action}`\n"
            f"*ᴜꜱᴇʀ:* [{user}](tg://user?id={user})\n"
            f"*ʙʏ:* {by}\n"
            f"*ʀᴇᴀꜱᴏɴ:* {reason or 'ɴᴏ ʀᴇᴀꜱᴏɴ'}",
            parse_mode=ParseMode.MARKDOWN
        )
    except:
        pass

# ═══════════════════════════════════════════════════════
#  1. START / HELP
# ═══════════════════════════════════════════════════════

def send_help_menu(update, context):
    """Send the full help menu with category buttons (used in PM only)."""
    categories = [
        ["ᴍᴏᴅᴇʀᴀᴛɪᴏɴ", "help_mod"],
        ["ᴡᴇʟᴄᴏᴍᴇ",    "help_welcome"],
        ["ᴡᴀʀɴɪɴɢꜱ",   "help_warns"],
        ["ʟᴏᴄᴋꜱ",       "help_locks"],
        ["ꜰɪʟᴛᴇʀꜱ",    "help_filters"],
        ["ɴᴏᴛᴇꜱ",       "help_notes"],
        ["ʙʟᴀᴄᴋʟɪꜱᴛ",  "help_blacklist"],
        ["ᴀᴅᴍɪɴ",       "help_admin"],
        ["ᴀɴᴛɪꜰʟᴏᴏᴅ",  "help_antiflood"],
        ["ᴄᴀᴘᴛᴄʜᴀ",    "help_captcha"],
        ["ʀᴜʟᴇꜱ",       "help_rules"],
        ["ᴘɪɴ",          "help_pin"],
        ["ɪɴꜰᴏ",         "help_info"],
        ["ᴘᴜʀɢᴇꜱ",      "help_purges"],
        ["ʙʀᴏᴀᴅᴄᴀꜱᴛ",  "help_broadcast"],
        ["ʀᴀɴᴋɪɴɢ",    "help_ranking"],
    ]
    kb = []
    row = []
    for label, data in categories:
        row.append(InlineKeyboardButton(label, callback_data=data))
        if len(row) == 3:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    text = (
        f"{calypso_header('❓ ʜᴇʟᴘ ᴍᴇɴᴜ')}"
        "ʜᴇʏ! ɪ'ᴍ *ᴄᴀʟʏᴘꜱᴏ*, ᴀ ɢʀᴏᴜᴘ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ ʙᴏᴛ.\n"
        "ɪ ʜᴀᴠᴇ ʟᴏᴛꜱ ᴏꜰ ʜᴀɴᴅʏ ꜰᴇᴀᴛᴜʀᴇꜱ ꜱᴜᴄʜ ᴀꜱ ꜰʟᴏᴏᴅ ᴄᴏɴᴛʀᴏʟ,\n"
        "ᴡᴀʀɴɪɴɢ ꜱʏꜱᴛᴇᴍ, ɴᴏᴛᴇꜱ, ꜰɪʟᴛᴇʀꜱ ᴀɴᴅ ᴍᴜᴄʜ ᴍᴏʀᴇ.\n\n"
        "ᴀʟʟ ᴄᴏᴍᴍᴀɴᴅꜱ ᴡᴏʀᴋ ᴡɪᴛʜ / ᴀɴᴅ . ᴘʀᴇꜰɪxᴇꜱ.\n\n"
        "ꜱᴇʟᴇᴄᴛ ᴀ ᴄᴀᴛᴇɢᴏʀʏ ʙᴇʟᴏᴡ ↓"
    )
    if update.callback_query:
        update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb)
        )
    else:
        update.message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb)
        )

def start(update: Update, context: CallbackContext):
    # Deep-link: /start help — sent from group button → show help menu in PM
    if context.args and context.args[0] == "help":
        send_help_menu(update, context)
        return
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("ᴀᴅᴅ ᴛᴏ ɢʀᴏᴜᴘ", url=f"https://t.me/{context.bot.username}?startgroup=true"),
        InlineKeyboardButton("ᴊᴏɪɴ ꜰᴏʀ ᴜᴘᴅᴀᴛᴇꜱ", url="https://t.me/calypsoGc"),
    ]])
    update.message.reply_text(
        f"{calypso_header('👋 ʜᴇʟʟᴏ!')}"
        "ɪ'ᴍ *ᴄᴀʟʏᴘꜱᴏ* — ᴀ ᴘʀᴏꜰᴇꜱꜱɪᴏɴᴀʟ ɢʀᴏᴜᴘ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ ʙᴏᴛ.\n"
        "ᴀᴅᴅ ᴍᴇ ᴛᴏ ʏᴏᴜʀ ɢʀᴏᴜᴘ ᴀɴᴅ ᴜꜱᴇ /ʜᴇʟᴘ ᴛᴏ ɢᴇᴛ ꜱᴛᴀʀᴛᴇᴅ.\n\n"
        "ᴊᴏɪɴ ᴏᴜʀ ᴄʜᴀɴɴᴇʟ ꜰᴏʀ ᴜᴘᴅᴀᴛᴇꜱ ᴀɴᴅ ɴᴇᴡꜱ.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=buttons
    )

def help_command(update: Update, context: CallbackContext):
    chat = update.effective_chat
    # ── GROUP: send a short message with a PM deep-link button ──
    if chat.type != "private":
        bot_username = context.bot.username
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "📩 ʜᴇʟᴘ ᴀɴᴅ ᴄᴏᴍᴍᴀɴᴅꜱ",
                url=f"https://t.me/{bot_username}?start=help"
            )
        ]])
        update.message.reply_text(
            "❖ ᴄʟɪᴄᴋ ᴛʜᴇ ʙᴜᴛᴛᴏɴ ʙᴇʟᴏᴡ ᴛᴏ\n"
            "ɢᴇᴛ ᴍʏ ʜᴇʟᴘ ᴍᴇɴᴜ ɪɴ ʏᴏᴜʀ ᴘᴍ. 📩",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb
        )
        return
    # ── PM: show full menu directly ──
    send_help_menu(update, context)

HELP_TEXTS = {
    "help_mod": (
        "ᴍᴏᴅᴇʀᴀᴛɪᴏɴ\n\n"
        "/ʙᴀɴ [ᴜꜱᴇʀ] [ʀᴇᴀꜱᴏɴ] — ʙᴀɴ ᴜꜱᴇʀ\n"
        "/ᴜɴʙᴀɴ [ᴜꜱᴇʀ] — ᴜɴʙᴀɴ ᴜꜱᴇʀ\n"
        "/ᴋɪᴄᴋ [ᴜꜱᴇʀ] [ʀᴇᴀꜱᴏɴ] — ᴋɪᴄᴋ ᴜꜱᴇʀ\n"
        "/ᴍᴜᴛᴇ [ᴜꜱᴇʀ] [ᴛɪᴍᴇ] — ᴍᴜᴛᴇ ᴜꜱᴇʀ\n"
        "/ᴜɴᴍᴜᴛᴇ [ᴜꜱᴇʀ] — ᴜɴᴍᴜᴛᴇ ᴜꜱᴇʀ\n"
        "/ᴛᴍᴜᴛᴇ [ᴜꜱᴇʀ] [ᴛɪᴍᴇ] — ᴛᴇᴍᴘ ᴍᴜᴛᴇ\n"
        "/ᴛʙᴀɴ [ᴜꜱᴇʀ] [ᴛɪᴍᴇ] — ᴛᴇᴍᴘ ʙᴀɴ\n"
    ),
    "help_welcome": (
        "ᴡᴇʟᴄᴏᴍᴇ\n\n"
        "/ꜱᴇᴛᴡᴇʟᴄᴏᴍᴇ [ᴍꜱɢ] — ꜱᴇᴛ ᴡᴇʟᴄᴏᴍᴇ ᴍᴇꜱꜱᴀɢᴇ\n"
        "/ꜱᴇᴛɢᴏᴏᴅʙʏᴇ [ᴍꜱɢ] — ꜱᴇᴛ ɢᴏᴏᴅʙʏᴇ ᴍᴇꜱꜱᴀɢᴇ\n"
        "/ᴡᴇʟᴄᴏᴍᴇ ᴏɴ/ᴏꜰꜰ — ᴛᴏɢɢʟᴇ ᴡᴇʟᴄᴏᴍᴇ\n"
        "/ɢᴏᴏᴅʙʏᴇ ᴏɴ/ᴏꜰꜰ — ᴛᴏɢɢʟᴇ ɢᴏᴏᴅʙʏᴇ\n"
        "/ʀᴇꜱᴇᴛᴡᴇʟᴄᴏᴍᴇ — ʀᴇꜱᴇᴛ ᴛᴏ ᴅᴇꜰᴀᴜʟᴛ\n\n"
        "ᴜꜱᴇ {first}, {last}, {username}, {mention}, {chatname} ᴀꜱ ᴘʟᴀᴄᴇʜᴏʟᴅᴇʀꜱ.\n"
    ),
    "help_warns": (
        "ᴡᴀʀɴɪɴɢꜱ\n\n"
        "/ᴡᴀʀɴ [ᴜꜱᴇʀ] [ʀᴇᴀꜱᴏɴ] — ᴡᴀʀɴ ᴜꜱᴇʀ\n"
        "/ᴜɴᴡᴀʀɴ [ᴜꜱᴇʀ] — ʀᴇᴍᴏᴠᴇ ʟᴀꜱᴛ ᴡᴀʀɴ\n"
        "/ᴡᴀʀɴꜱ [ᴜꜱᴇʀ] — ᴄʜᴇᴄᴋ ᴡᴀʀɴꜱ\n"
        "/ʀᴇꜱᴇᴛᴡᴀʀɴꜱ [ᴜꜱᴇʀ] — ʀᴇꜱᴇᴛ ᴡᴀʀɴꜱ\n"
        f"ᴀᴜᴛᴏ-ʙᴀɴ ᴀꜰᴛᴇʀ *{WARN_LIMIT}* ᴡᴀʀɴꜱ.\n"
    ),
    "help_locks": (
        "ʟᴏᴄᴋꜱ\n\n"
        "/ʟᴏᴄᴋ [ᴛʏᴘᴇ] — ʟᴏᴄᴋ ᴍᴇꜱꜱᴀɢᴇ ᴛʏᴘᴇ\n"
        "/ᴜɴʟᴏᴄᴋ [ᴛʏᴘᴇ] — ᴜɴʟᴏᴄᴋ ᴍᴇꜱꜱᴀɢᴇ ᴛʏᴘᴇ\n"
        "/ʟᴏᴄᴋꜱ — ꜱʜᴏᴡ ᴄᴜʀʀᴇɴᴛ ʟᴏᴄᴋꜱ\n\n"
        "ᴛʏᴘᴇꜱ: ᴀʟʟ, ᴍᴇᴅɪᴀ, ꜱᴛɪᴄᴋᴇʀ, ɢɪꜰ, ᴜʀʟ, ꜰᴏʀᴡᴀʀᴅ, ᴘʜᴏᴛᴏ, ᴠɪᴅᴇᴏ, ᴠᴏɪᴄᴇ, ᴀᴜᴅɪᴏ, ᴅᴏᴄᴜᴍᴇɴᴛ\n"
    ),
    "help_filters": (
        "ꜰɪʟᴛᴇʀꜱ\n\n"
        "/ꜰɪʟᴛᴇʀ [ᴋᴇʏᴡᴏʀᴅ] [ʀᴇᴘʟʏ] — ᴀᴅᴅ ᴀᴜᴛᴏ-ʀᴇᴘʟʏ ꜰɪʟᴛᴇʀ\n"
        "/ꜱᴛᴏᴘ [ᴋᴇʏᴡᴏʀᴅ] — ʀᴇᴍᴏᴠᴇ ꜰɪʟᴛᴇʀ\n"
        "/ꜰɪʟᴛᴇʀꜱ — ʟɪꜱᴛ ᴀʟʟ ꜰɪʟᴛᴇʀꜱ\n"
    ),
    "help_notes": (
        "ɴᴏᴛᴇꜱ\n\n"
        "/ꜱᴀᴠᴇ [ɴᴀᴍᴇ] [ᴛᴇxᴛ] — ꜱᴀᴠᴇ ᴀ ɴᴏᴛᴇ\n"
        "/ɢᴇᴛ [ɴᴀᴍᴇ] ᴏʀ #ɴᴀᴍᴇ — ʀᴇᴛʀɪᴇᴠᴇ ɴᴏᴛᴇ\n"
        "/ᴄʟᴇᴀʀ [ɴᴀᴍᴇ] — ᴅᴇʟᴇᴛᴇ ɴᴏᴛᴇ\n"
        "/ɴᴏᴛᴇꜱ — ʟɪꜱᴛ ᴀʟʟ ɴᴏᴛᴇꜱ\n"
    ),
    "help_blacklist": (
        "ʙʟᴀᴄᴋʟɪꜱᴛ\n\n"
        "/ᴀᴅᴅʙʟᴀᴄᴋʟɪꜱᴛ [ᴡᴏʀᴅ] — ᴀᴅᴅ ᴡᴏʀᴅ ᴛᴏ ʙʟᴀᴄᴋʟɪꜱᴛ\n"
        "/ʀᴍʙʟᴀᴄᴋʟɪꜱᴛ [ᴡᴏʀᴅ] — ʀᴇᴍᴏᴠᴇ ᴡᴏʀᴅ\n"
        "/ʙʟᴀᴄᴋʟɪꜱᴛ — ꜱʜᴏᴡ ʙʟᴀᴄᴋʟɪꜱᴛ\n"
        "ᴍᴇꜱꜱᴀɢᴇꜱ ᴡɪᴛʜ ʙʟᴀᴄᴋʟɪꜱᴛᴇᴅ ᴡᴏʀᴅꜱ ᴀʀᴇ ᴀᴜᴛᴏ-ᴅᴇʟᴇᴛᴇᴅ.\n"
    ),
    "help_admin": (
        "ᴀᴅᴍɪɴ\n\n"
        "/ᴘʀᴏᴍᴏᴛᴇ [ᴜꜱᴇʀ] [ᴛɪᴛʟᴇ] — ᴘʀᴏᴍᴏᴛᴇ ᴛᴏ ᴀᴅᴍɪɴ\n"
        "/ᴅᴇᴍᴏᴛᴇ [ᴜꜱᴇʀ] — ᴅᴇᴍᴏᴛᴇ ᴀᴅᴍɪɴ\n"
        "/ꜱᴇᴛᴛɪᴛʟᴇ [ᴜꜱᴇʀ] [ᴛɪᴛʟᴇ] — ꜱᴇᴛ ᴀᴅᴍɪɴ ᴛɪᴛʟᴇ\n"
        "/ᴀᴅᴍɪɴʟɪꜱᴛ — ʟɪꜱᴛ ᴀʟʟ ᴀᴅᴍɪɴꜱ\n"
        "/ɪᴅ — ɢᴇᴛ ᴜꜱᴇʀ/ᴄʜᴀᴛ ɪᴅ\n"
        "/ɪɴꜰᴏ [ᴜꜱᴇʀ] — ɢᴇᴛ ᴜꜱᴇʀ ɪɴꜰᴏ\n"
        "ᴛɪᴛʟᴇ ʟɪᴍɪᴛ: 16 ᴄʜᴀʀᴀᴄᴛᴇʀꜱ.\n"
    ),
    "help_antiflood": (
        "ᴀɴᴛɪꜰʟᴏᴏᴅ\n\n"
        "/ꜱᴇᴛꜰʟᴏᴏᴅ [ɴ/ᴏꜰꜰ] — ꜱᴇᴛ ᴀɴᴛɪ-ꜰʟᴏᴏᴅ ʟɪᴍɪᴛ\n"
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
        "/ꜱᴇᴛʀᴜʟᴇꜱ [ᴛᴇxᴛ] — ꜱᴇᴛ ɢʀᴏᴜᴘ ʀᴜʟᴇꜱ\n"
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
        "/ɪɴꜰᴏ [ᴜꜱᴇʀ] — ɢᴇᴛ ᴜꜱᴇʀ ɪɴꜰᴏ ᴀɴᴅ ᴡᴀʀɴꜱ\n"
        "/ᴀᴅᴍɪɴʟɪꜱᴛ — ꜱʜᴏᴡ ᴀʟʟ ɢʀᴏᴜᴘ ᴀᴅᴍɪɴꜱ\n"
    ),
    "help_purges": (
        "ᴘᴜʀɢᴇꜱ\n\n"
        "/ᴘᴜʀɢᴇ [ɴ] — ᴅᴇʟᴇᴛᴇ ʟᴀꜱᴛ ɴ ᴍᴇꜱꜱᴀɢᴇꜱ\n"
        "/ᴅᴇʟ — ᴅᴇʟᴇᴛᴇ ʀᴇᴘʟɪᴇᴅ ᴍᴇꜱꜱᴀɢᴇ\n"
    ),
    "help_broadcast": (
        "ʙʀᴏᴀᴅᴄᴀꜱᴛ\n\n"
        "/ʙʀᴏᴀᴅᴄᴀꜱᴛ [ᴍᴇꜱꜱᴀɢᴇ] — ꜱᴇɴᴅ ᴍᴇꜱꜱᴀɢᴇ ᴛᴏ ᴀʟʟ ᴄʜᴀᴛꜱ\n\n"
        "ᴏᴡɴᴇʀ ᴏɴʟʏ ᴄᴏᴍᴍᴀɴᴅ.\n"
    ),
    "help_ranking": (
        "ʀᴀɴᴋɪɴɢ\n\n"
        "/ʀᴀɴᴋɪɴɢ — ꜱʜᴏᴡ ᴛᴏᴘ 10 ᴄʜᴀᴛᴛᴇʀꜱ ɪɴ ᴛʜɪꜱ ɢʀᴏᴜᴘ\n\n"
        "ᴛʜʀᴇᴇ ᴠɪᴇᴡꜱ ᴀᴠᴀɪʟᴀʙʟᴇ:\n"
        "📅 ᴛᴏᴅᴀʏ — ᴍᴇꜱꜱᴀɢᴇꜱ ꜱᴇɴᴛ ᴛᴏᴅᴀʏ\n"
        "📆 ᴡᴇᴇᴋ — ᴍᴇꜱꜱᴀɢᴇꜱ ᴛʜɪꜱ ᴡᴇᴇᴋ\n"
        "🏆 ᴛᴏᴛᴀʟ — ᴀʟʟ-ᴛɪᴍᴇ ᴄᴏᴜɴᴛ\n\n"
        "ꜱʜᴏᴡꜱ ᴀ ᴘʀᴏɢʀᴇꜱꜱ-ʙᴀʀ ᴛᴀʙʟᴇ ᴀɴᴅ 🥇🥈🥉 ᴍᴇᴅᴀʟꜱ.\n"
        "ᴡᴏʀᴋꜱ ᴡɪᴛʜ .ʀᴀɴᴋɪɴɢ ᴛᴏᴏ.\n"
    ),
}

def help_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    data = query.data
    if data == "help_main":
        send_help_menu(update, context)
    elif data in HELP_TEXTS:
        kb = [[InlineKeyboardButton("« ʙᴀᴄᴋ", callback_data="help_main")]]
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
    if uid in get_admin_ids(update.effective_chat):
        update.message.reply_text("❌ ᴄᴀɴɴᴏᴛ ʙᴀɴ ᴀɴ ᴀᴅᴍɪɴɪꜱᴛʀᴀᴛᴏʀ.")
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
        log_action(context, update.effective_chat, "ʙᴀɴ", uid, update.effective_user.first_name, reason)
    except BadRequest as e:
        update.message.reply_text(f"❌ ꜰᴀɪʟᴇᴅ: {sc(e.message)}")

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
        update.message.reply_text(f"❌ ꜰᴀɪʟᴇᴅ: {sc(e.message)}")

@admin_required
def kick(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid:
        update.message.reply_text("⚠️ ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴜꜱᴇʀ ᴏʀ ᴘʀᴏᴠɪᴅᴇ ᴜꜱᴇʀ ɪᴅ.")
        return
    if uid in get_admin_ids(update.effective_chat):
        update.message.reply_text("❌ ᴄᴀɴɴᴏᴛ ᴋɪᴄᴋ ᴀɴ ᴀᴅᴍɪɴɪꜱᴛʀᴀᴛᴏʀ.")
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
        log_action(context, update.effective_chat, "ᴋɪᴄᴋ", uid, update.effective_user.first_name, reason)
    except BadRequest as e:
        update.message.reply_text(f"❌ ꜰᴀɪʟᴇᴅ: {sc(e.message)}")

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
    if uid in get_admin_ids(update.effective_chat):
        update.message.reply_text("❌ ᴄᴀɴɴᴏᴛ ᴍᴜᴛᴇ ᴀɴ ᴀᴅᴍɪɴɪꜱᴛʀᴀᴛᴏʀ.")
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
        err = e.message.lower()
        if "user is an administrator" in err:
            update.message.reply_text("❌ ᴄᴀɴɴᴏᴛ ᴍᴜᴛᴇ ᴀɴ ᴀᴅᴍɪɴɪꜱᴛʀᴀᴛᴏʀ.")
        elif "chat_admin_required" in err:
            update.message.reply_text("❌ ɪ ɴᴇᴇᴅ ᴛʜᴇ *ʀᴇꜱᴛʀɪᴄᴛ ᴍᴇᴍʙᴇʀꜱ* ᴘᴇʀᴍɪꜱꜱɪᴏɴ ᴛᴏ ᴍᴜᴛᴇ.", parse_mode=ParseMode.MARKDOWN)
        else:
            update.message.reply_text(f"❌ ꜰᴀɪʟᴇᴅ: {sc(e.message)}")

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
        update.message.reply_text(f"❌ ꜰᴀɪʟᴇᴅ: {sc(e.message)}")

@admin_required
def tmute(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid or not context.args:
        update.message.reply_text("ᴜꜱᴀɢᴇ: /ᴛᴍᴜᴛᴇ [ᴜꜱᴇʀ] [ᴛɪᴍᴇ ᴇ.ɢ. 10m, 2h, 1d]")
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
        update.message.reply_text(f"❌ ꜰᴀɪʟᴇᴅ: {sc(e.message)}")

@admin_required
def tban(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid or not context.args:
        update.message.reply_text("ᴜꜱᴀɢᴇ: /ᴛʙᴀɴ [ᴜꜱᴇʀ] [ᴛɪᴍᴇ ᴇ.ɢ. 10m, 2h, 1d]")
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
        update.message.reply_text(f"❌ ꜰᴀɪʟᴇᴅ: {sc(e.message)}")

# ═══════════════════════════════════════════════════════
#  4. WARN SYSTEM
# ═══════════════════════════════════════════════════════

@admin_required
def warn(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid:
        update.message.reply_text("⚠️ ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴜꜱᴇʀ ᴏʀ ᴘʀᴏᴠɪᴅᴇ ᴜꜱᴇʀ ɪᴅ.")
        return
    chat = update.effective_chat
    chat_id = chat.id
    # Block warning admins
    if uid in get_admin_ids(chat):
        update.message.reply_text("❌ ᴄᴀɴɴᴏᴛ ᴡᴀʀɴ ᴀɴ ᴀᴅᴍɪɴɪꜱᴛʀᴀᴛᴏʀ.")
        return
    # Reason: skip first arg if it's the user target, take the rest
    if update.message.reply_to_message:
        reason = " ".join(context.args) if context.args else "ɴᴏ ʀᴇᴀꜱᴏɴ"
    else:
        reason = " ".join(context.args[1:]) if context.args and len(context.args) > 1 else "ɴᴏ ʀᴇᴀꜱᴏɴ"
    db["warns"][chat_id][uid] += 1
    count = db["warns"][chat_id][uid]
    if count >= WARN_LIMIT:
        try:
            chat.kick_member(uid)  # kick_member in PTB v13 = ban permanently
            db["warns"][chat_id][uid] = 0
            update.message.reply_text(
                f"{calypso_header('🚫 ᴀᴜᴛᴏ-ʙᴀɴ')}"
                f"*ᴜꜱᴇʀ:* [{to_small_caps(name)}](tg://user?id={uid})\n"
                f"ʀᴇᴀᴄʜᴇᴅ *{WARN_LIMIT} ᴡᴀʀɴꜱ* — ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ ʙᴀɴɴᴇᴅ.",
                parse_mode=ParseMode.MARKDOWN
            )
            log_action(context, chat, "ᴀᴜᴛᴏ-ʙᴀɴ", uid, "ᴄᴀʟʏᴘꜱᴏ", f"{WARN_LIMIT} ᴡᴀʀɴꜱ")
        except BadRequest as e:
            update.message.reply_text(f"❌ ᴄᴏᴜʟᴅ ɴᴏᴛ ʙᴀɴ: {sc(e.message)}")
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
        update.message.reply_text("ᴜꜱᴀɢᴇ: /ꜱᴇᴛɢᴏᴏᴅʙʏᴇ [ᴍᴇꜱꜱᴀɢᴇ]")
        return
    db["goodbye"][chat_id]["msg"] = msg_text[1]
    update.message.reply_text("✅ ɢᴏᴏᴅʙʏᴇ ᴍᴇꜱꜱᴀɢᴇ ꜱᴀᴠᴇᴅ.")

@admin_required
def resetwelcome(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    db["welcome"][chat_id]["msg"] = None
    update.message.reply_text("✅ ᴡᴇʟᴄᴏᴍᴇ ᴍᴇꜱꜱᴀɢᴇ ʀᴇꜱᴇᴛ ᴛᴏ ᴅᴇꜰᴀᴜʟᴛ.")

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
            "ᴡᴇʟᴄᴏᴍᴇ {mention} ᴛᴏ *{chatname}*!\n"
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
    # data format: "captcha_{chat_id}_{user_id}"
    # chat_id may be negative, so split from the right to avoid splitting on its minus sign
    try:
        prefix, user_id_str = data.rsplit("_", 1)
        chat_id_str = prefix[len("captcha_"):]
        chat_id, user_id = int(chat_id_str), int(user_id_str)
    except (ValueError, IndexError):
        query.answer("ɪɴᴠᴀʟɪᴅ ᴄᴀᴘᴛᴄʜᴀ ᴅᴀᴛᴀ.", show_alert=True)
        return
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
            context.bot.kick_chat_member(chat_id, user_id)   # ban
            context.bot.unban_chat_member(chat_id, user_id)  # unban so they can rejoin (true kick)
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
            n = min(int(context.args[0]), 100)  # Cap at 100 for safety
            if n < 1:
                update.message.reply_text("❌ ᴘʟᴇᴀꜱᴇ ᴘʀᴏᴠɪᴅᴇ ᴀ ᴘᴏꜱɪᴛɪᴠᴇ ɴᴜᴍʙᴇʀ.")
                return
            # Delete n messages before the purge command (not including it)
            ids = list(range(msg.message_id - n, msg.message_id))
            ids = [i for i in ids if i > 0]  # Filter out invalid IDs
            try:
                context.bot.delete_messages(msg.chat_id, ids)
            except:
                for i in ids:
                    try: context.bot.delete_message(msg.chat_id, i)
                    except: pass
            try: msg.delete()
            except: pass
            context.bot.send_message(msg.chat_id, f"🗑 ᴅᴇʟᴇᴛᴇᴅ `{n}` ᴍᴇꜱꜱᴀɢᴇꜱ.", parse_mode=ParseMode.MARKDOWN)
            return
        update.message.reply_text("ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴍᴇꜱꜱᴀɢᴇ ᴛᴏ ᴘᴜʀɢᴇ ꜰʀᴏᴍ, ᴏʀ ᴜꜱᴇ /ᴘᴜʀɢᴇ [ɴ].")
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
    msg = update.message
    chat_id = update.effective_chat.id
    parts = msg.text.split(None, 2) if msg.text else []

    if not parts or len(parts) < 2:
        msg.reply_text("ᴜꜱᴀɢᴇ: /ꜰɪʟᴛᴇʀ [ᴋᴇʏᴡᴏʀᴅ] [ʀᴇᴘʟʏ ᴛᴇxᴛ]\nᴏʀ ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴍᴇᴅɪᴀ ᴍᴇꜱꜱᴀɢᴇ ᴡɪᴛʜ /ꜰɪʟᴛᴇʀ [ᴋᴇʏᴡᴏʀᴅ]")
        return

    keyword = parts[1].lower()

    # If replying to a message, capture that message as the filter reply
    if msg.reply_to_message:
        reply_msg = msg.reply_to_message
        if reply_msg.sticker:
            db["filters"][chat_id][keyword] = {"type": "sticker", "file_id": reply_msg.sticker.file_id}
        elif reply_msg.animation:
            db["filters"][chat_id][keyword] = {"type": "animation", "file_id": reply_msg.animation.file_id}
        elif reply_msg.photo:
            db["filters"][chat_id][keyword] = {"type": "photo", "file_id": reply_msg.photo[-1].file_id, "caption": reply_msg.caption or ""}
        elif reply_msg.video:
            db["filters"][chat_id][keyword] = {"type": "video", "file_id": reply_msg.video.file_id, "caption": reply_msg.caption or ""}
        elif reply_msg.voice:
            db["filters"][chat_id][keyword] = {"type": "voice", "file_id": reply_msg.voice.file_id}
        elif reply_msg.document:
            db["filters"][chat_id][keyword] = {"type": "document", "file_id": reply_msg.document.file_id, "caption": reply_msg.caption or ""}
        elif reply_msg.text:
            db["filters"][chat_id][keyword] = {"type": "text", "text": reply_msg.text}
        else:
            msg.reply_text("❌ ᴜɴꜱᴜᴘᴘᴏʀᴛᴇᴅ ᴍᴇᴅɪᴀ ᴛʏᴘᴇ.")
            return
    elif len(parts) >= 3:
        db["filters"][chat_id][keyword] = {"type": "text", "text": parts[2]}
    else:
        msg.reply_text("ᴜꜱᴀɢᴇ: /ꜰɪʟᴛᴇʀ [ᴋᴇʏᴡᴏʀᴅ] [ʀᴇᴘʟʏ ᴛᴇxᴛ]\nᴏʀ ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴍᴇᴅɪᴀ ᴍᴇꜱꜱᴀɢᴇ ᴡɪᴛʜ /ꜰɪʟᴛᴇʀ [ᴋᴇʏᴡᴏʀᴅ]")
        return

    msg.reply_text(f"✅ ꜰɪʟᴛᴇʀ `{keyword}` ᴀᴅᴅᴇᴅ.", parse_mode=ParseMode.MARKDOWN)

@admin_required
def stop_filter(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text("ᴜꜱᴀɢᴇ: /ꜱᴛᴏᴘ [ᴋᴇʏᴡᴏʀᴅ]")
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
    if not update.message:
        return
    msg = update.message
    chat_id = update.effective_chat.id
    # Match against text or caption (for media messages)
    text = (msg.text or msg.caption or "").lower()
    if not text:
        return
    for kw, reply_data in db["filters"][chat_id].items():
        if kw in text:
            # Support both old plain-string filters and new dict filters
            if isinstance(reply_data, str):
                msg.reply_text(reply_data, parse_mode=ParseMode.MARKDOWN)
            elif reply_data["type"] == "text":
                msg.reply_text(reply_data["text"], parse_mode=ParseMode.MARKDOWN)
            elif reply_data["type"] == "sticker":
                context.bot.send_sticker(chat_id, reply_data["file_id"])
            elif reply_data["type"] == "animation":
                context.bot.send_animation(chat_id, reply_data["file_id"])
            elif reply_data["type"] == "photo":
                context.bot.send_photo(chat_id, reply_data["file_id"], caption=reply_data.get("caption") or None, parse_mode=ParseMode.MARKDOWN)
            elif reply_data["type"] == "video":
                context.bot.send_video(chat_id, reply_data["file_id"], caption=reply_data.get("caption") or None, parse_mode=ParseMode.MARKDOWN)
            elif reply_data["type"] == "voice":
                context.bot.send_voice(chat_id, reply_data["file_id"])
            elif reply_data["type"] == "document":
                context.bot.send_document(chat_id, reply_data["file_id"], caption=reply_data.get("caption") or None, parse_mode=ParseMode.MARKDOWN)
            break

# ═══════════════════════════════════════════════════════
#  8. NOTES
# ═══════════════════════════════════════════════════════

@admin_required
def save_note(update: Update, context: CallbackContext):
    parts = update.message.text.split(None, 2)
    if len(parts) < 3:
        update.message.reply_text("ᴜꜱᴀɢᴇ: /ꜱᴀᴠᴇ [ɴᴀᴍᴇ] [ᴛᴇxᴛ]")
        return
    chat_id = update.effective_chat.id
    name, text = parts[1].lower(), parts[2]
    db["notes"][chat_id][name] = text
    update.message.reply_text(f"✅ ɴᴏᴛᴇ `{name}` ꜱᴀᴠᴇᴅ. ʀᴇᴛʀɪᴇᴠᴇ ᴡɪᴛʜ `/ɢᴇᴛ {name}` ᴏʀ `#{name}`", parse_mode=ParseMode.MARKDOWN)

def get_note(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    name = context.args[0].lower() if context.args else None
    if not name:
        update.message.reply_text("ᴜꜱᴀɢᴇ: /ɢᴇᴛ [ɴᴀᴍᴇ]")
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
        update.message.reply_text("ᴜꜱᴀɢᴇ: /ᴄʟᴇᴀʀ [ɴᴀᴍᴇ]")
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
        update.message.reply_text("ᴜꜱᴀɢᴇ: /ᴀᴅᴅʙʟᴀᴄᴋʟɪꜱᴛ [ᴡᴏʀᴅ]")
        return
    chat_id = update.effective_chat.id
    word = " ".join(context.args).lower()
    db["blacklist"][chat_id].add(word)
    update.message.reply_text(f"✅ `{word}` ᴀᴅᴅᴇᴅ ᴛᴏ ʙʟᴀᴄᴋʟɪꜱᴛ.", parse_mode=ParseMode.MARKDOWN)

@admin_required
def rm_blacklist(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text("ᴜꜱᴀɢᴇ: /ʀᴍʙʟᴀᴄᴋʟɪꜱᴛ [ᴡᴏʀᴅ]")
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
    chat = update.effective_chat
    user = update.effective_user
    if user and user.id in get_admin_ids(chat):
        return
    chat_id = chat.id
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
    "media": Filters.photo | Filters.video | Filters.document | Filters.audio,
    "audio": Filters.audio,
}

@admin_required
def lock(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text("ᴜꜱᴀɢᴇ: /ʟᴏᴄᴋ [ᴛʏᴘᴇ]\nᴛʏᴘᴇꜱ: ꜱᴛɪᴄᴋᴇʀ, ɢɪꜰ, ᴜʀʟ, ꜰᴏʀᴡᴀʀᴅ, ᴘʜᴏᴛᴏ, ᴠɪᴅᴇᴏ, ᴠᴏɪᴄᴇ, ᴅᴏᴄᴜᴍᴇɴᴛ")
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
        update.message.reply_text("ᴜꜱᴀɢᴇ: /ᴜɴʟᴏᴄᴋ [ᴛʏᴘᴇ]")
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
    admins = get_admin_ids(chat)
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
    elif locks.get("audio") and msg.audio:
        delete = True
    elif locks.get("document") and msg.document:
        delete = True
    elif locks.get("media") and (msg.photo or msg.video or msg.document or msg.audio):
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
        update.message.reply_text("ᴜꜱᴀɢᴇ: /ꜱᴇᴛꜰʟᴏᴏᴅ [ɴᴜᴍʙᴇʀ/ᴏꜰꜰ]")
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
    admins = get_admin_ids(chat)
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
        update.message.reply_text("ᴜꜱᴀɢᴇ: /ꜱᴇᴛʀᴜʟᴇꜱ [ᴛᴇxᴛ]")
        return
    db["rules"][update.effective_chat.id] = parts[1]
    update.message.reply_text("✅ ʀᴜʟᴇꜱ ꜱᴀᴠᴇᴅ.")

def rules(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    r = db["rules"][chat_id]
    if not r:
        update.message.reply_text("ɴᴏ ʀᴜʟᴇꜱ ꜱᴇᴛ. ᴜꜱᴇ /ꜱᴇᴛʀᴜʟᴇꜱ ᴛᴏ ꜱᴇᴛ ᴛʜᴇᴍ.")
        return
    update.message.reply_text(
        f"{calypso_header('📜 ɢʀᴏᴜᴘ ʀᴜʟᴇꜱ')}{r}",
        parse_mode=ParseMode.MARKDOWN
    )

# ═══════════════════════════════════════════════════════
#  13. PROMOTE / DEMOTE / SETTITLE
# ═══════════════════════════════════════════════════════

@admin_required
def promote(update: Update, context: CallbackContext):
    """
    Usage:
      /promote @user [custom title]
      Reply to a message: /promote [custom title]
    Promotes the user to admin and optionally sets their title.
    """
    uid, name = get_target_user(update, context)
    if not uid:
        update.message.reply_text(
            "ᴜꜱᴀɢᴇ: /ᴘʀᴏᴍᴏᴛᴇ [ᴜꜱᴇʀ] [ᴛɪᴛʟᴇ]\n"
            "ᴇxᴀᴍᴘʟᴇ: /ᴘʀᴏᴍᴏᴛᴇ @ᴜꜱᴇʀ ᴍᴏᴅᴇʀᴀᴛᴏʀ"
        )
        return
    chat = update.effective_chat

    # --- Parse custom title from args ---
    # If replying: all args are the title
    # If by username/id: args[0] is the target, args[1:] is the title
    if update.message.reply_to_message:
        custom_title = " ".join(context.args).strip() if context.args else ""
    else:
        custom_title = " ".join(context.args[1:]).strip() if context.args and len(context.args) > 1 else ""

    # Telegram title limit: 16 chars
    if len(custom_title) > 16:
        update.message.reply_text("❌ ᴀᴅᴍɪɴ ᴛɪᴛʟᴇ ᴄᴀɴɴᴏᴛ ᴇxᴄᴇᴇᴅ *16 ᴄʜᴀʀᴀᴄᴛᴇʀꜱ*.", parse_mode=ParseMode.MARKDOWN)
        return

    try:
        chat.promote_member(
            uid,
            can_change_info=True,
            can_delete_messages=True,
            can_restrict_members=True,
            can_pin_messages=True,
            can_invite_users=True,
            can_manage_chat=True,
        )
        # Set custom title if provided
        if custom_title:
            try:
                context.bot.set_chat_administrator_custom_title(chat.id, uid, custom_title)
            except BadRequest:
                pass  # Title set failure is non-fatal

        _admin_cache.pop(chat.id, None)  # Invalidate cache

        title_line = f"\n*ᴛɪᴛʟᴇ:* `{custom_title}`" if custom_title else ""
        update.message.reply_text(
            f"{calypso_header('⭐ ᴘʀᴏᴍᴏᴛᴇᴅ')}"
            f"*ᴜꜱᴇʀ:* [{to_small_caps(name)}](tg://user?id={uid})\n"
            f"*ʙʏ:* {to_small_caps(update.effective_user.first_name)}"
            f"{title_line}",
            parse_mode=ParseMode.MARKDOWN
        )
        log_action(context, chat, "ᴘʀᴏᴍᴏᴛᴇ", uid, update.effective_user.first_name, custom_title or "ɴᴏ ᴛɪᴛʟᴇ")
    except BadRequest as e:
        err = e.message.lower()
        if "chat_admin_required" in err or "not enough rights" in err:
            update.message.reply_text("❌ ɪ ɴᴇᴇᴅ ᴛʜᴇ *ᴀᴅᴅ ᴀᴅᴍɪɴꜱ* ᴘᴇʀᴍɪꜱꜱɪᴏɴ ᴛᴏ ᴘʀᴏᴍᴏᴛᴇ ᴜꜱᴇʀꜱ.", parse_mode=ParseMode.MARKDOWN)
        elif "can't remove chat owner" in err or "cant remove chat owner" in err:
            update.message.reply_text("❌ ᴄᴀɴɴᴏᴛ ᴘʀᴏᴍᴏᴛᴇ/ᴅᴇᴍᴏᴛᴇ ᴛʜᴇ ᴄʜᴀᴛ ᴏᴡɴᴇʀ.")
        else:
            update.message.reply_text(f"❌ ꜰᴀɪʟᴇᴅ: {sc(e.message)}")

@admin_required
def settitle(update: Update, context: CallbackContext):
    """
    Usage:
      /settitle @user New Title
      Reply to a message: /settitle New Title
    Sets or updates the custom title of an existing admin.
    """
    uid, name = get_target_user(update, context)
    if not uid:
        update.message.reply_text(
            "ᴜꜱᴀɢᴇ: /ꜱᴇᴛᴛɪᴛʟᴇ [ᴜꜱᴇʀ] [ᴛɪᴛʟᴇ]\n"
            "ᴇxᴀᴍᴘʟᴇ: /ꜱᴇᴛᴛɪᴛʟᴇ @ᴜꜱᴇʀ ʜᴇᴀᴅ ᴍᴏᴅ"
        )
        return
    chat = update.effective_chat

    if update.message.reply_to_message:
        custom_title = " ".join(context.args).strip() if context.args else ""
    else:
        custom_title = " ".join(context.args[1:]).strip() if context.args and len(context.args) > 1 else ""

    if not custom_title:
        update.message.reply_text("⚠️ ᴘʟᴇᴀꜱᴇ ᴘʀᴏᴠɪᴅᴇ ᴀ ᴛɪᴛʟᴇ.")
        return
    if len(custom_title) > 16:
        update.message.reply_text("❌ ᴀᴅᴍɪɴ ᴛɪᴛʟᴇ ᴄᴀɴɴᴏᴛ ᴇxᴄᴇᴇᴅ *16 ᴄʜᴀʀᴀᴄᴛᴇʀꜱ*.", parse_mode=ParseMode.MARKDOWN)
        return

    # Check that the target is actually an admin
    admins = chat.get_administrators()
    is_admin = any(m.user.id == uid for m in admins)
    if not is_admin:
        update.message.reply_text("❌ ᴛʜᴀᴛ ᴜꜱᴇʀ ɪꜱ ɴᴏᴛ ᴀɴ ᴀᴅᴍɪɴ. ᴜꜱᴇ /ᴘʀᴏᴍᴏᴛᴇ ꜰɪʀꜱᴛ.")
        return

    try:
        context.bot.set_chat_administrator_custom_title(chat.id, uid, custom_title)
        update.message.reply_text(
            f"{calypso_header('🏷 ᴛɪᴛʟᴇ ᴜᴘᴅᴀᴛᴇᴅ')}"
            f"*ᴜꜱᴇʀ:* [{to_small_caps(name)}](tg://user?id={uid})\n"
            f"*ɴᴇᴡ ᴛɪᴛʟᴇ:* `{custom_title}`",
            parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest as e:
        err = e.message.lower()
        if "not enough rights" in err or "chat_admin_required" in err:
            update.message.reply_text("❌ ɪ ᴄᴀɴ ᴏɴʟʏ ꜱᴇᴛ ᴛɪᴛʟᴇꜱ ᴏɴ ᴀᴅᴍɪɴꜱ ɪ ᴘʀᴏᴍᴏᴛᴇᴅ ᴍʏꜱᴇʟꜰ.")
        else:
            update.message.reply_text(f"❌ ꜰᴀɪʟᴇᴅ: {sc(e.message)}")

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
        _admin_cache.pop(update.effective_chat.id, None)  # Invalidate cache
        update.message.reply_text(
            f"✅ [{to_small_caps(name)}](tg://user?id={uid}) ᴅᴇᴍᴏᴛᴇᴅ.",
            parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest as e:
        err = e.message.lower()
        if "chat_admin_required" in err or "not enough rights" in err:
            update.message.reply_text("❌ ɪ ɴᴇᴇᴅ ᴛʜᴇ *ᴀᴅᴅ ᴀᴅᴍɪɴꜱ* ᴘᴇʀᴍɪꜱꜱɪᴏɴ ᴛᴏ ᴅᴇᴍᴏᴛᴇ ᴜꜱᴇʀꜱ.", parse_mode=ParseMode.MARKDOWN)
        elif "can't remove chat owner" in err or "cant remove chat owner" in err:
            update.message.reply_text("❌ ᴄᴀɴɴᴏᴛ ᴅᴇᴍᴏᴛᴇ ᴛʜᴇ ᴄʜᴀᴛ ᴏᴡɴᴇʀ.")
        else:
            update.message.reply_text(f"❌ ꜰᴀɪʟᴇᴅ: {sc(e.message)}")

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
        update.message.reply_text(f"❌ ꜰᴀɪʟᴇᴅ: {sc(e.message)}")

@admin_required
def unpin(update: Update, context: CallbackContext):
    try:
        update.effective_chat.unpin_message()
        update.message.reply_text("📌 ᴍᴇꜱꜱᴀɢᴇ ᴜɴᴘɪɴɴᴇᴅ.")
    except BadRequest as e:
        update.message.reply_text(f"❌ ꜰᴀɪʟᴇᴅ: {sc(e.message)}")

# ═══════════════════════════════════════════════════════
#  15. ID / INFO
# ═══════════════════════════════════════════════════════

def get_id(update: Update, context: CallbackContext):
    msg = update.message
    if msg.reply_to_message:
        u = msg.reply_to_message.from_user
        update.message.reply_text(
            f"*ᴜꜱᴇʀ ɪᴅ:* `{u.id}`\n*ᴄʜᴀᴛ ɪᴅ:* `{msg.chat_id}`",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        update.message.reply_text(
            f"*ʏᴏᴜʀ ɪᴅ:* `{msg.from_user.id}`\n*ᴄʜᴀᴛ ɪᴅ:* `{msg.chat_id}`",
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
        update.message.reply_text("ᴜꜱᴀɢᴇ: /ᴄᴀᴘᴛᴄʜᴀ ᴏɴ/ᴏꜰꜰ")

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

# ─── KNOWN CHATS (for broadcast) ──────────────────────
_known_chats: set = set()

def track_chat(update: Update, context: CallbackContext):
    """Track every chat the bot is active in."""
    if update.effective_chat and update.effective_chat.type != "private":
        _known_chats.add(update.effective_chat.id)

# ═══════════════════════════════════════════════════════
#  18. BROADCAST (Owner only)
# ═══════════════════════════════════════════════════════

@owner_required
def broadcast(update: Update, context: CallbackContext):
    parts = update.message.text.split(None, 1)
    if len(parts) < 2:
        update.message.reply_text("ᴜꜱᴀɢᴇ: /ʙʀᴏᴀᴅᴄᴀꜱᴛ [ᴍᴇꜱꜱᴀɢᴇ]")
        return
    text = parts[1]
    success, failed = 0, 0
    for cid in list(_known_chats):
        try:
            context.bot.send_message(cid, text, parse_mode=ParseMode.MARKDOWN)
            success += 1
        except:
            failed += 1
    update.message.reply_text(
        f"📢 ʙʀᴏᴀᴅᴄᴀꜱᴛ ᴄᴏᴍᴘʟᴇᴛᴇ.\n"
        f"✅ ꜱᴇɴᴛ: `{success}` | ❌ ꜰᴀɪʟᴇᴅ: `{failed}`",
        parse_mode=ParseMode.MARKDOWN
    )

# ═══════════════════════════════════════════════════════
#  19. PING
# ═══════════════════════════════════════════════════════

def ping(update: Update, context: CallbackContext):
    start_time = time.time()
    msg = update.message.reply_text("🏓 ᴘɪɴɢɪɴɢ...")
    elapsed = round((time.time() - start_time) * 1000, 2)
    msg.edit_text(
        f"⌁ ᴘᴏɴɢ · `{elapsed}ms` ⌁",
        parse_mode=ParseMode.MARKDOWN
    )

# ═══════════════════════════════════════════════════════
#  20. RANKING
# ═══════════════════════════════════════════════════════

def _mention(uid: int, name: str, username: str) -> str:
    """Small-caps name as a tg:// mention. Username is embedded invisibly."""
    sc_name = to_small_caps(name[:18])
    return f"[{sc_name}](tg://user?id={uid})"


def _build_ranking_text(chat_id: int, period: str) -> str:
    """Build the leaderboard message for the given period.

    period: 'today' | 'week' | 'total'
    Returns None when there is no data at all.
    """
    stats = db["chat_stats"].get(chat_id, {})
    if not stats:
        return None

    today_str = datetime.now().strftime("%Y-%m-%d")
    week_str  = datetime.now().strftime("%Y-W%W")

    if period == "today":
        scores = [
            (uid, d.get("name", "?"), d.get("username", ""), d["daily"].get(today_str, 0))
            for uid, d in stats.items()
        ]
        period_label = to_small_caps(f"today  {datetime.now().strftime('%d %b %Y')}")
    elif period == "week":
        scores = [
            (uid, d.get("name", "?"), d.get("username", ""), d["weekly"].get(week_str, 0))
            for uid, d in stats.items()
        ]
        period_label = to_small_caps(f"week  w{datetime.now().strftime('%W')}  {datetime.now().strftime('%Y')}")
    else:
        scores = [
            (uid, d.get("name", "?"), d.get("username", ""), d["total"])
            for uid, d in stats.items()
        ]
        period_label = to_small_caps("all-time total")

    # Sort descending, top 10, drop zeros
    scores = sorted(
        [(uid, name, uname, cnt) for uid, name, uname, cnt in scores if cnt > 0],
        key=lambda x: x[3], reverse=True
    )[:10]

    if not scores:
        return (
            f"*{to_small_caps('chat ranking')}* ❓\n\n"
            f"— {period_label}\n\n"
            f"{to_small_caps('no data yet — start chatting!')}"
        )

    lines = [
        f"*{to_small_caps('chat ranking')}* ❓\n",
        f"— {period_label}\n",
    ]

    for rank, (uid, name, uname, cnt) in enumerate(scores, 1):
        mention = _mention(uid, name, uname)
        lines.append(f"⋆ {mention}  {to_small_caps(f'- {cnt} texts')}")

    return "\n".join(lines)


def _ranking_keyboard(current: str) -> InlineKeyboardMarkup:
    """Three period buttons — active one gets ⌁ prefix."""
    periods = [
        (to_small_caps("today"), "rank_today"),
        (to_small_caps("week"),  "rank_week"),
        (to_small_caps("total"), "rank_total"),
    ]
    row = []
    for label, cb in periods:
        period_key = cb.split("_")[1]
        display = f"⌁ {label}" if period_key == current else label
        row.append(InlineKeyboardButton(display, callback_data=cb))
    return InlineKeyboardMarkup([row])


def ranking(update: Update, context: CallbackContext):
    """/ranking or .ranking — group-only leaderboard."""
    chat = update.effective_chat
    if chat.type == "private":
        update.message.reply_text(
            f"⚠️ {to_small_caps('ranking only works in groups.')}",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    text = _build_ranking_text(chat.id, "today")
    if not text:
        update.message.reply_text(
            f"*{to_small_caps('chat ranking')}* ❓\n\n"
            f"{to_small_caps('no data yet — start chatting!')}",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_ranking_keyboard("today"),
        disable_web_page_preview=True
    )


def ranking_callback(update: Update, context: CallbackContext):
    """Switch period when Today / Week / Total button is pressed."""
    query = update.callback_query
    query.answer()
    period  = query.data.split("_")[1]   # 'today' | 'week' | 'total'
    chat_id = query.message.chat_id

    text = _build_ranking_text(chat_id, period)
    if not text:
        text = (
            f"*{to_small_caps('chat ranking')}* ❓\n\n"
            f"{to_small_caps('no data yet — start chatting!')}"
        )
    try:
        query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_ranking_keyboard(period),
            disable_web_page_preview=True
        )
    except Exception:
        pass  # Telegram raises error when content is identical — safe to swallow

# ═══════════════════════════════════════════════════════
#  DOT-COMMAND DISPATCHER
# ═══════════════════════════════════════════════════════

# Maps command name → handler function
_DOT_COMMANDS = {
    "start":        lambda u, c: start(u, c),
    "help":         lambda u, c: help_command(u, c),
    "ban":          lambda u, c: ban(u, c),
    "unban":        lambda u, c: unban(u, c),
    "kick":         lambda u, c: kick(u, c),
    "mute":         lambda u, c: mute(u, c),
    "unmute":       lambda u, c: unmute(u, c),
    "tmute":        lambda u, c: tmute(u, c),
    "tban":         lambda u, c: tban(u, c),
    "warn":         lambda u, c: warn(u, c),
    "unwarn":       lambda u, c: unwarn(u, c),
    "warns":        lambda u, c: warns(u, c),
    "resetwarns":   lambda u, c: resetwarns(u, c),
    "purge":        lambda u, c: purge(u, c),
    "del":          lambda u, c: delete_msg(u, c),
    "filter":       lambda u, c: add_filter(u, c),
    "stop":         lambda u, c: stop_filter(u, c),
    "filters":      lambda u, c: list_filters(u, c),
    "save":         lambda u, c: save_note(u, c),
    "get":          lambda u, c: get_note(u, c),
    "clear":        lambda u, c: clear_note(u, c),
    "notes":        lambda u, c: list_notes(u, c),
    "addblacklist": lambda u, c: add_blacklist(u, c),
    "rmblacklist":  lambda u, c: rm_blacklist(u, c),
    "blacklist":    lambda u, c: show_blacklist(u, c),
    "lock":         lambda u, c: lock(u, c),
    "unlock":       lambda u, c: unlock(u, c),
    "locks":        lambda u, c: show_locks(u, c),
    "setflood":     lambda u, c: setflood(u, c),
    "antiflood":    lambda u, c: antiflood_status(u, c),
    "setrules":     lambda u, c: setrules(u, c),
    "rules":        lambda u, c: rules(u, c),
    "setwelcome":   lambda u, c: setwelcome(u, c),
    "setgoodbye":   lambda u, c: setgoodbye(u, c),
    "resetwelcome": lambda u, c: resetwelcome(u, c),
    "welcome":      lambda u, c: toggle_welcome(u, c),
    "goodbye":      lambda u, c: toggle_welcome(u, c),
    "promote":      lambda u, c: promote(u, c),
    "demote":       lambda u, c: demote(u, c),
    "settitle":     lambda u, c: settitle(u, c),
    "pin":          lambda u, c: pin(u, c),
    "unpin":        lambda u, c: unpin(u, c),
    "id":           lambda u, c: get_id(u, c),
    "info":         lambda u, c: info(u, c),
    "adminlist":    lambda u, c: adminlist(u, c),
    "broadcast":    lambda u, c: broadcast(u, c),
    "captcha":      lambda u, c: captcha_cmd(u, c),
    "ping":         lambda u, c: ping(u, c),
    "ranking":      lambda u, c: ranking(u, c),
}

def dot_command_handler(update: Update, context: CallbackContext):
    """Handle commands prefixed with '.' instead of '/'."""
    msg = update.message
    if not msg or not msg.text:
        return
    text = msg.text.strip()
    if not text.startswith("."):
        return
    # Parse .command and args (strip bot @mention if present)
    parts = text[1:].split()
    if not parts:
        return
    cmd = parts[0].split("@")[0].lower()
    # Inject args into context just like CommandHandler does
    context.args = parts[1:] if len(parts) > 1 else []
    handler = _DOT_COMMANDS.get(cmd)
    if handler:
        handler(update, context)

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
    dp.add_handler(CommandHandler("resetwelcome", resetwelcome))
    dp.add_handler(CommandHandler(["welcome", "goodbye"], toggle_welcome))
    dp.add_handler(CommandHandler("promote", promote))
    dp.add_handler(CommandHandler("demote", demote))
    dp.add_handler(CommandHandler("settitle", settitle))
    dp.add_handler(CommandHandler("pin", pin))
    dp.add_handler(CommandHandler("unpin", unpin))
    dp.add_handler(CommandHandler("id", get_id))
    dp.add_handler(CommandHandler("info", info))
    dp.add_handler(CommandHandler("adminlist", adminlist))
    dp.add_handler(CommandHandler("broadcast", broadcast))
    dp.add_handler(CommandHandler("captcha", captcha_cmd))
    dp.add_handler(CommandHandler("ping", ping))
    dp.add_handler(CommandHandler("ranking", ranking))

    # Message handlers
    dp.add_handler(MessageHandler(Filters.status_update.new_chat_members, welcome_member))
    dp.add_handler(MessageHandler(Filters.status_update.left_chat_member, goodbye_member))
    dp.add_handler(MessageHandler(Filters.all & ~Filters.private, track_chat), group=0)  # Track known chats
    dp.add_handler(MessageHandler(Filters.all, cache_user), group=-1)  # ← FIX: separate group so it always runs
    dp.add_handler(MessageHandler(Filters.regex(r"^\.[a-zA-Z]"), dot_command_handler), group=1)  # Dot-commands
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, check_filters), group=1)
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, hashtag_note), group=2)
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, check_blacklist), group=3)
    dp.add_handler(MessageHandler(Filters.all & ~Filters.command, check_locks), group=4)
    dp.add_handler(MessageHandler(Filters.all & ~Filters.command, check_flood), group=5)

    # Callbacks
    dp.add_handler(CallbackQueryHandler(help_callback, pattern="^help_"))
    dp.add_handler(CallbackQueryHandler(captcha_callback, pattern="^captcha_"))
    dp.add_handler(CallbackQueryHandler(ranking_callback, pattern="^rank_"))

    # ── Telegram-as-database: load stats on startup ──────
    tg_load_stats(updater.bot)

    # ── Start background snapshot thread ─────────────────
    if STORAGE_CHANNEL:
        threading.Thread(target=_snapshot_loop, args=(updater.bot,), daemon=True).start()
        logger.info(f"ᴛɢ ꜱᴛᴏʀᴀɢᴇ: snapshot thread started (every {SNAPSHOT_INTERVAL}s).")
    else:
        logger.warning("ᴛɢ ꜱᴛᴏʀᴀɢᴇ: STORAGE_CHANNEL not set — stats will reset on restart!")

    logger.info(f"{'='*45}")
    logger.info(f"  {CALYPSO} ꜱᴛᴀʀᴛᴇᴅ ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ!")
    logger.info(f"{'='*45}")

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    # ─── KEEP-ALIVE SERVER (for Koyeb) ─────────────────
    class KeepAliveHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write("ᴄᴀʟʏᴘꜱᴏʙᴏᴛ ɪꜱ ᴀʟɪᴠᴇ!".encode("utf-8"))
        def log_message(self, format, *args):
            pass  # Suppress access logs

    def run_server():
        port = int(os.environ.get("PORT", 8080))
        server = HTTPServer(("0.0.0.0", port), KeepAliveHandler)
        server.serve_forever()

    threading.Thread(target=run_server, daemon=True).start()
    logger.info("ᴋᴇᴇᴘ-ᴀʟɪᴠᴇ ꜱᴇʀᴠᴇʀ ꜱᴛᴀʀᴛᴇᴅ.")
    # ───────────────────────────────────────────────────
    main()
