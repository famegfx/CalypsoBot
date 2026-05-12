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
import io
import textwrap
from datetime import datetime, timedelta
from functools import wraps
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from PIL import Image, ImageDraw, ImageFont


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
    "approved":  defaultdict(set),                        # {chat_id: {user_id, ...}} — exempt from flood/locks/blacklist
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
SNAPSHOT_INTERVAL = 30   # seconds between saves

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
            chat = update.effective_chat
            # Method 1: Check current chat's user cache (most reliable — seen messages)
            current_chat_cache = db["user_cache"].get(chat.id, {})
            for uid, udata in current_chat_cache.items():
                if udata.get("username", "").lower() == username:
                    return uid, udata.get("first_name", arg)
            # Method 2: Search group admins (always accessible via API)
            try:
                admins_full = chat.get_administrators()
                for member in admins_full:
                    u = member.user
                    if u.username and u.username.lower() == username:
                        return u.id, u.first_name or arg
            except Exception:
                pass
            # Method 3: Try get_chat_member with username directly
            try:
                member = context.bot.get_chat_member(chat.id, arg)
                u = member.user
                return u.id, u.first_name or arg
            except Exception:
                pass
            # Method 4: Try direct Telegram API get_chat as last resort
            try:
                user = context.bot.get_chat(arg)
                if user and user.id:
                    return user.id, user.first_name or arg
            except Exception:
                pass
            # All methods failed
            update.message.reply_text(
                f"✦ ꜰᴀɪʟᴇᴅ ᴛᴏ ꜰɪɴᴅ ᴜꜱᴇʀ {arg}.\n"
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
        # ── Milestone check ──────────────────────────────
        check_milestones(context, chat_id)

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
        ["ʀᴀɴᴋɪɴɢ",     "help_ranking"],
        ["ǫᴜᴏᴛᴇ",       "help_quote"],
        ["ᴀɪ ᴄʜᴀᴛ",    "help_ai"],
        ["ᴀᴘᴘʀᴏᴠᴀʟ",   "help_approval"],
        ["ɢʀᴏᴜᴘ ᴛᴏᴏʟꜱ","help_grouptools"],
        ["ᴜᴛɪʟɪᴛʏ",    "help_utility"],
        ["ᴜᴛɪʟɪᴛʏ ᴠ2",  "help_utility2"],
        ["ꜰᴜɴ",          "help_fun"],
        ["ᴀɴᴛɪ-ꜱᴘᴀᴍ",  "help_antispam"],
        ["ꜱᴛᴀᴛꜱ",        "help_stats"],
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
    # Track PM users for broadcast
    if update.effective_chat.type == "private" and update.effective_user:
        _known_users.add(update.effective_user.id)
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
    "help_music": (
        "🎵 ᴍᴜꜱɪᴄ\n\n"
        "/play <ꜱᴏɴɢ/ᴜʀʟ> — ᴘʟᴀʏ ɪɴ ᴠᴏɪᴄᴇ ᴄʜᴀᴛ\n"
        "/skip — ꜱᴋɪᴘ ᴄᴜʀʀᴇɴᴛ ꜱᴏɴɢ\n"
        "/pause — ᴘᴀᴜꜱᴇ ᴘʟᴀʏʙᴀᴄᴋ\n"
        "/resume — ʀᴇꜱᴜᴍᴇ ᴘʟᴀʏʙᴀᴄᴋ\n"
    ),
    "help_quote": (
        "ǫᴜᴏᴛᴇ ꜱᴛɪᴄᴋᴇʀ\n\n"
        "/quote — ʀᴇᴘʟʏ ᴛᴏ ᴀɴʏ ᴍᴇꜱꜱᴀɢᴇ ᴛᴏ ᴍᴀᴋᴇ ᴀ ǫᴜᴏᴛᴇ ꜱᴛɪᴄᴋᴇʀ\n"
        "/quote [ᴛᴇxᴛ] — ᴛʏᴘᴇ ʏᴏᴜʀ ᴏᴡɴ ǫᴜᴏᴛᴇ\n\n"
        "ꜱᴛɪᴄᴋᴇʀ ʜᴀꜱ:\n"
        "• ᴛᴏᴘ ᴛᴇxᴛ — ǫᴜᴏᴛᴇ ɪɴ ꜱᴍᴀʟʟ ᴄᴀᴘꜱ\n"
        "• ʙᴏᴛᴛᴏᴍ ᴛᴇxᴛ — ꜱᴇɴᴅᴇʀ ɴᴀᴍᴇ\n"
        "• ᴅᴀʀᴋ ᴀᴇꜱᴛʜᴇᴛɪᴄ ᴅᴇꜱɪɢɴ\n"
    ),
    "help_ai": (
        "ᴀɪ ᴄʜᴀᴛ\n\n"
        "/ai on — ᴀɪ ʀᴇᴘʟɪᴇꜱ ᴇɴᴀʙʟᴇ ᴋᴀʀᴏ\n"
        "/ai off — ᴀɪ ʙᴀɴᴅ ᴋᴀʀᴏ\n"
        "/ai [ꜱᴀᴡᴀᴀʟ] — ᴅɪʀᴇᴄᴛ ᴋᴜᴄʜ ᴘᴜᴄʜʜᴏ\n"
        "/image [ᴘʀᴏᴍᴘᴛ] — ᴀɪ ꜱᴇ ɪᴍᴀɢᴇ ʙᴀɴᴀᴏ 🎨\n\n"
        "ʜɪɴɢʟɪꜱʜ + ꜱᴀᴠᴀɢᴇ ꜱᴛʏʟᴇ 😈\n"
        "GROQ_API_KEY ꜱᴇᴛ ᴋᴀʀᴏ ʀᴇᴀʟ ᴀɪ ᴋᴇ ʟɪʏᴇ.\n"
    ),
    "help_approval": (
        "ᴀᴘᴘʀᴏᴠᴀʟ ꜱɪꜱᴛᴇᴍ\n\n"
        "/approval on — ᴊᴏɪɴ ʀᴇǫᴜᴇꜱᴛ ᴍᴏᴅ ᴏɴ\n"
        "/approval off — ᴅɪꜱᴀʙʟᴇ\n"
        "/approve [ʀᴇᴘʟʏ/@ᴜꜱᴇʀ] — ʏᴜꜱᴇʀ ᴀᴘᴘʀᴏᴠᴇ ᴋᴀʀᴏ\n"
        "/unapprove [ʀᴇᴘʟʏ/@ᴜꜱᴇʀ] — ᴀᴘᴘʀᴏᴠᴀʟ ʜᴀᴛᴀᴏ\n"
        "/approved — ꜱᴀʙ ᴀᴘᴘʀᴏᴠᴇᴅ ʏᴜꜱᴇʀꜱ ᴅᴇᴋʜᴏ\n\n"
        "ᴊᴏɪɴ ʀᴇǫᴜᴇꜱᴛ ᴀᴀɴᴇ ᴘᴇ ɢʀᴏᴜᴘ ᴍᴇɪɴ ɴᴏᴛɪꜰɪᴄᴀᴛɪᴏɴ ᴀᴀᴛɪ ʜᴀɪ.\n"
        "ᴀᴘᴘʀᴏᴠᴇᴅ ʏᴜꜱᴇʀꜱ ꜰʟᴏᴏᴅ/ʟᴏᴄᴋ/ʙʟᴀᴄᴋʟɪꜱᴛ ꜱᴇ ᴇxᴇᴍᴘᴛ ʜᴀɪɴ.\n"
    ),
    "help_grouptools": (
        "ɢʀᴏᴜᴘ ᴛᴏᴏʟꜱ\n\n"
        "/report — ᴍᴇꜱꜱᴀɢᴇ ᴋᴏ ʀᴇᴘʟʏ ᴋᴀʀᴋᴇ ᴀᴅᴍɪɴꜱ ᴋᴏ ʀɪᴘᴏʀᴛ ᴋᴀʀᴏ\n"
        "/afk [ʀᴇᴀꜱᴏɴ] — ᴀᴘɴᴇ ᴀᴀᴘ ᴋᴏ ᴀꜰᴋ ᴍᴀʀᴋ ᴋᴀʀᴏ\n"
        "/warnreasons [ʀᴇᴘʟʏ] — ᴡᴀʀɴꜱ + ʀᴇᴀꜱᴏɴꜱ ᴅᴇᴋʜᴏ\n\n"
        "ᴋɪꜱɪ ᴀꜰᴋ ʏᴜꜱᴇʀ ᴋᴏ ᴛᴀɢ ᴋᴀʀᴏ — ʙᴏᴛ ɴᴏᴛɪꜰʏ ᴋᴀʀᴇɢᴀ.\n"
        "ᴍᴇꜱꜱᴀɢᴇ ʙʜᴇᴊɴᴇ ᴘᴇ ᴀꜰᴋ ꜱᴛᴀᴛᴜꜱ ᴀᴜᴛᴏ ʜᴀᴛᴀ ᴊᴀᴛᴀ ʜᴀɪ.\n"
    ),
    "help_utility": (
        "ᴜᴛɪʟɪᴛʏ\n\n"
        "/calc [ᴇxᴘʀ] — ᴋᴀʟᴋᴜʟᴇᴛᴏʀ ᴇ.ɢ. /calc 50*2+10\n"
        "/time [ᴢᴏɴᴇ] — ᴋɪꜱɪ ʙʜɪ ᴛᴀɪᴍᴢᴏɴ ᴋᴀ ᴛᴀɪᴍ\n"
        "/tr [ʟᴀɴɢ] [ᴛᴇxᴛ] — ᴛᴇxᴛ ᴛʀᴀɴꜱʟᴇᴛ ᴋᴀʀᴏ\n"
        "/weather [ꜱʜᴀʜᴀʀ] — ᴍᴀᴜꜱᴀᴍ ᴅᴇᴋʜᴏ\n\n"
        "ᴢᴏɴᴇꜱ: ist, pak, dubai, uk, utc, est, pst, jst\n"
        "/tr ᴋᴇ ʟɪʏᴇ ʀᴇᴘʟʏ ʙʜɪ ᴋᴀʀ ꜱᴀᴋᴛᴇ ʜᴏ.\n"
    ),
    "help_fun": (
        "ꜰᴜɴ ᴄᴏᴍᴍᴀɴᴅꜱ\n\n"
        "/kiss /slap /hug /pat\n"
        "/punch /cuddle /poke /highfive\n"
        "/ship — ʟᴏᴠᴇ % ᴍᴇᴛᴇʀ\n"
        "/roast @user — ꜱᴀᴠᴀɢᴇ ʀᴏᴀꜱᴛ\n"
        "/truth — ʀᴀɴᴅᴏᴍ ᴛʀᴜᴛʜ ǫ\n"
        "/dare — ʀᴀɴᴅᴏᴍ ᴅᴀʀᴇ\n"
        "/8ball [ꜱᴀᴡᴀᴀʟ] — ᴍᴀɢɪᴄ 8 ʙᴀʟʟ\n"
        "/roll [ɴ] — ᴅɪᴄᴇ ʀᴏʟʟ\n"
        "/flip — ᴄᴏɪɴ ꜰʟɪᴘ\n\n"
        "ʀᴇᴘʟʏ ᴛᴏ ꜱᴏᴍᴇᴏɴᴇ ᴏʀ ᴡʀɪᴛᴇ ᴛʜᴇɪʀ ɴᴀᴍᴇ.\n"
        "ꜱᴇɴᴅꜱ ᴀ ɢɪꜰ + ᴀᴄᴛɪᴏɴ ᴍᴇꜱꜱᴀɢᴇ! 🎊\n"
    ),
    "help_utility2": (
        "ᴜᴛɪʟɪᴛʏ ᴠ2\n\n"
        "/wiki [ᴛᴏᴘɪᴄ] — ᴡɪᴋɪᴘᴇᴅɪᴀ ꜱᴜᴍᴍᴀʀʏ\n"
        "/define [ᴡᴏʀᴅ] — ᴅɪᴄᴛɪᴏɴᴀʀʏ ᴍᴇᴀɴɪɴɢ\n"
        "/remind [ᴛɪᴍᴇ] [ᴛᴇxᴛ] — ʀᴇᴍɪɴᴅᴇʀ ꜱᴇᴛ ᴋᴀʀᴏ\n"
        "/poll [ꜱᴀᴡᴀᴀʟ] | ᴏᴘ1 | ᴏᴘ2 — ᴘᴏʟʟ ʙᴀɴᴀᴏ\n"
        "/image [ᴘʀᴏᴍᴘᴛ] — ᴀɪ ɪᴍᴀɢᴇ ɢᴇɴᴇʀᴀᴛᴇ ᴋᴀʀᴏ\n\n"
        "ᴛɪᴍᴇ ꜰᴏʀᴍᴀᴛ: 30ꜱ, 10ᴍ, 2ʜ, 1ᴅ\n"
    ),
    "help_antispam": (
        "ᴀɴᴛɪ-ꜱᴘᴀᴍ\n\n"
        "/antilink on/off — ʟɪɴᴋꜱ ᴀᴜᴛᴏ-ᴅɪʟɪᴛ\n"
        "/antiforward on/off — ꜰᴏʀᴡᴀʀᴅ ᴍꜱɢ ᴀᴜᴛᴏ-ᴅɪʟɪᴛ\n"
        "/slowmode [ꜱᴇᴄꜱ] — ꜱʟᴏ ᴍᴏᴅ ꜱᴇᴛ ᴋᴀʀᴏ\n\n"
        "ᴀᴅᴍɪɴꜱ ᴀᴜʀ ᴀᴘᴘʀᴏᴠᴇᴅ ʏᴜꜱᴇʀꜱ ᴇxᴇᴍᴘᴛ ʜᴀɪɴ.\n"
    ),
    "help_stats": (
        "ꜱᴛᴀᴛꜱ\n\n"
        "/ranking — ɢʀᴏᴜᴘ ᴛᴏᴘ 10 ʟᴇᴀᴅᴇʀʙᴏᴀʀᴅ\n"
        "/mystats — ᴀᴘɴᴀ ᴘᴇʀꜱᴏɴᴀʟ ꜱᴛᴀᴛꜱ ᴅᴇᴋʜᴏ\n\n"
        "ᴛʜʀᴇᴇ ᴠɪᴇᴡꜱ: ᴛᴏᴅᴀʏ / ᴡᴇᴇᴋ / ᴛᴏᴛᴀʟ\n"
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


def action_callback(update: Update, context: CallbackContext):
    """
    Central handler for all action buttons:
    action_unban_{chat_id}_{uid}_{name}
    action_unmute_{chat_id}_{uid}_{name}
    action_unwarn_{chat_id}_{uid}_{name}
    Only admins can press these buttons.
    """
    query = update.callback_query
    clicker = query.from_user
    try:
        parts  = query.data.split("_", 4)   # ['action', action, chat_id, uid, name]
        action  = parts[1]
        chat_id = int(parts[2])
        uid     = int(parts[3])
        name    = parts[4] if len(parts) > 4 else str(uid)
    except Exception:
        query.answer("ɪɴᴠᴀʟɪᴅ ᴅᴀᴛᴀ.", show_alert=True)
        return

    # Verify clicker is admin
    try:
        member = context.bot.get_chat_member(chat_id, clicker.id)
        if member.status not in ("administrator", "creator") and clicker.id != OWNER_ID:
            query.answer(to_small_caps("sirf admins press kar sakte hain!"), show_alert=True)
            return
    except Exception:
        query.answer(to_small_caps("verification fail ho gayi."), show_alert=True)
        return

    try:
        if action == "unban":
            context.bot.unban_chat_member(chat_id, uid)
            query.answer(to_small_caps("unban ho gaya ⌁"))
            query.edit_message_reply_markup(reply_markup=None)
            query.message.reply_text(
                f"⌁ [{to_small_caps(name)}](tg://user?id={uid}) ᴜɴʙᴀɴ ʜᴏ ɢᴀʏᴀ\n"
                f"*ʙʏ:* {to_small_caps(clicker.first_name)}",
                parse_mode=ParseMode.MARKDOWN
            )

        elif action == "unmute":
            context.bot.restrict_chat_member(
                chat_id, uid,
                permissions=ChatPermissions(
                    can_send_messages=True,
                    can_send_media_messages=True,
                    can_send_polls=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                )
            )
            query.answer(to_small_caps("unmute ho gaya ⌁"))
            query.edit_message_reply_markup(reply_markup=None)
            query.message.reply_text(
                f"🔊 [{to_small_caps(name)}](tg://user?id={uid}) ᴜɴᴍᴜᴛᴇ ʜᴏ ɢᴀʏᴀ\n"
                f"*ʙʏ:* {to_small_caps(clicker.first_name)}",
                parse_mode=ParseMode.MARKDOWN
            )

        elif action == "unwarn":
            if db["warns"][chat_id][uid] > 0:
                db["warns"][chat_id][uid] -= 1
            count = db["warns"][chat_id][uid]
            query.answer(to_small_caps("warn remove ho gaya ⌁"))
            query.edit_message_reply_markup(reply_markup=None)
            query.message.reply_text(
                f"⌁ One warning removed from [{to_small_caps(name)}](tg://user?id={uid})\n"
                f"*ᴡᴀʀɴꜱ:* `{count}/{WARN_LIMIT}` | *ʙʏ:* {to_small_caps(clicker.first_name)}",
                parse_mode=ParseMode.MARKDOWN
            )
    except BadRequest as e:
        query.answer(f"✦ {e.message}", show_alert=True)
    except Exception as e:
        query.answer(f"✦ ᴇʀʀᴏʀ: {str(e)[:50]}", show_alert=True)

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
        update.message.reply_text("✦ ᴄᴀɴɴᴏᴛ ʙᴀɴ ᴀɴ ᴀᴅᴍɪɴɪꜱᴛʀᴀᴛᴏʀ.")
        return
    chat_id = update.effective_chat.id
    reason = " ".join(context.args[1:]) if context.args and len(context.args) > 1 else "ɴᴏ ʀᴇᴀꜱᴏɴ"
    try:
        update.effective_chat.kick_member(uid)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔓 ᴜɴʙᴀɴ", callback_data=f"action_unban_{chat_id}_{uid}_{name[:20]}")
        ]])
        update.message.reply_text(
            f"{calypso_header('🔨 ᴜꜱᴇʀ ʙᴀɴɴᴇᴅ')}"
            f"*ᴜꜱᴇʀ:* [{to_small_caps(name)}](tg://user?id={uid})\n"
            f"*ʀᴇᴀꜱᴏɴ:* {reason}\n"
            f"*ʙʏ:* {update.effective_user.first_name}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb
        )
        log_action(context, update.effective_chat, "ʙᴀɴ", uid, update.effective_user.first_name, reason)
    except BadRequest as e:
        update.message.reply_text(f"✦ ꜰᴀɪʟᴇᴅ: {sc(e.message)}")

@admin_required
def unban(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid:
        update.message.reply_text("⚠️ ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴜꜱᴇʀ ᴏʀ ᴘʀᴏᴠɪᴅᴇ ᴜꜱᴇʀ ɪᴅ.")
        return
    try:
        update.effective_chat.unban_member(uid)
        update.message.reply_text(
            f"{calypso_header('⌁ ᴜꜱᴇʀ ᴜɴʙᴀɴɴᴇᴅ')}"
            f"*ᴜꜱᴇʀ:* [{to_small_caps(name)}](tg://user?id={uid})\n"
            f"*ʙʏ:* {to_small_caps(update.effective_user.first_name)}",
            parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest as e:
        update.message.reply_text(f"✦ ꜰᴀɪʟᴇᴅ: {sc(e.message)}")

@admin_required
def kick(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid:
        update.message.reply_text("⚠️ ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴜꜱᴇʀ ᴏʀ ᴘʀᴏᴠɪᴅᴇ ᴜꜱᴇʀ ɪᴅ.")
        return
    if uid in get_admin_ids(update.effective_chat):
        update.message.reply_text("✦ ᴄᴀɴɴᴏᴛ ᴋɪᴄᴋ ᴀɴ ᴀᴅᴍɪɴɪꜱᴛʀᴀᴛᴏʀ.")
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
        update.message.reply_text(f"✦ ꜰᴀɪʟᴇᴅ: {sc(e.message)}")

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
        update.message.reply_text("✦ ᴄᴀɴɴᴏᴛ ᴍᴜᴛᴇ ᴀɴ ᴀᴅᴍɪɴɪꜱᴛʀᴀᴛᴏʀ.")
        return
    chat_id = update.effective_chat.id
    try:
        update.effective_chat.restrict_member(
            uid,
            permissions=ChatPermissions(can_send_messages=False)
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔊 ᴜɴᴍᴜᴛᴇ", callback_data=f"action_unmute_{chat_id}_{uid}_{name[:20]}")
        ]])
        update.message.reply_text(
            f"{calypso_header('🔇 ᴜꜱᴇʀ ᴍᴜᴛᴇᴅ')}"
            f"*ᴜꜱᴇʀ:* [{to_small_caps(name)}](tg://user?id={uid})",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb
        )
    except BadRequest as e:
        err = e.message.lower()
        if "user is an administrator" in err:
            update.message.reply_text("✦ ᴄᴀɴɴᴏᴛ ᴍᴜᴛᴇ ᴀɴ ᴀᴅᴍɪɴɪꜱᴛʀᴀᴛᴏʀ.")
        elif "chat_admin_required" in err:
            update.message.reply_text("✦ ɪ ɴᴇᴇᴅ ᴛʜᴇ *ʀᴇꜱᴛʀɪᴄᴛ ᴍᴇᴍʙᴇʀꜱ* ᴘᴇʀᴍɪꜱꜱɪᴏɴ ᴛᴏ ᴍᴜᴛᴇ.", parse_mode=ParseMode.MARKDOWN)
        else:
            update.message.reply_text(f"✦ ꜰᴀɪʟᴇᴅ: {sc(e.message)}")

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
            f"*ᴜꜱᴇʀ:* [{to_small_caps(name)}](tg://user?id={uid})\n"
            f"*ʙʏ:* {to_small_caps(update.effective_user.first_name)}",
            parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest as e:
        update.message.reply_text(f"✦ ꜰᴀɪʟᴇᴅ: {sc(e.message)}")

@admin_required
def tmute(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid or not context.args:
        update.message.reply_text("ᴜꜱᴀɢᴇ: /ᴛᴍᴜᴛᴇ [ᴜꜱᴇʀ] [ᴛɪᴍᴇ ᴇ.ɢ. 10m, 2h, 1d]")
        return
    duration = parse_time(context.args[-1])
    if not duration:
        update.message.reply_text("✦ ɪɴᴠᴀʟɪᴅ ᴛɪᴍᴇ. ᴜꜱᴇ: 10m, 2h, 1d")
        return
    until = datetime.now() + timedelta(seconds=duration)
    chat_id = update.effective_chat.id
    try:
        update.effective_chat.restrict_member(
            uid,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔊 ᴜɴᴍᴜᴛᴇ", callback_data=f"action_unmute_{chat_id}_{uid}_{name[:20]}")
        ]])
        update.message.reply_text(
            f"{calypso_header('⏱ ᴛᴇᴍᴘ ᴍᴜᴛᴇ')}"
            f"*ᴜꜱᴇʀ:* [{to_small_caps(name)}](tg://user?id={uid})\n"
            f"*ᴅᴜʀᴀᴛɪᴏɴ:* `{context.args[-1]}`\n"
            f"*ᴜɴᴛɪʟ:* `{until.strftime('%Y-%m-%d %H:%M')}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb
        )
    except BadRequest as e:
        update.message.reply_text(f"✦ ꜰᴀɪʟᴇᴅ: {sc(e.message)}")

@admin_required
def tban(update: Update, context: CallbackContext):
    uid, name = get_target_user(update, context)
    if not uid or not context.args:
        update.message.reply_text("ᴜꜱᴀɢᴇ: /ᴛʙᴀɴ [ᴜꜱᴇʀ] [ᴛɪᴍᴇ ᴇ.ɢ. 10m, 2h, 1d]")
        return
    duration = parse_time(context.args[-1])
    if not duration:
        update.message.reply_text("✦ ɪɴᴠᴀʟɪᴅ ᴛɪᴍᴇ. ᴜꜱᴇ: 10m, 2h, 1d")
        return
    until = datetime.now() + timedelta(seconds=duration)
    chat_id = update.effective_chat.id
    try:
        update.effective_chat.kick_member(uid, until_date=until)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔓 ᴜɴʙᴀɴ", callback_data=f"action_unban_{chat_id}_{uid}_{name[:20]}")
        ]])
        update.message.reply_text(
            f"{calypso_header('⏳ ᴛᴇᴍᴘ ʙᴀɴ')}"
            f"*ᴜꜱᴇʀ:* [{to_small_caps(name)}](tg://user?id={uid})\n"
            f"*ᴅᴜʀᴀᴛɪᴏɴ:* `{context.args[-1]}`\n"
            f"*ᴜɴᴛɪʟ:* `{until.strftime('%Y-%m-%d %H:%M')}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb
        )
    except BadRequest as e:
        update.message.reply_text(f"✦ ꜰᴀɪʟᴇᴅ: {sc(e.message)}")

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
        update.message.reply_text("✦ ᴄᴀɴɴᴏᴛ ᴡᴀʀɴ ᴀɴ ᴀᴅᴍɪɴɪꜱᴛʀᴀᴛᴏʀ.")
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
            chat.kick_member(uid)
            db["warns"][chat_id][uid] = 0
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔓 ᴜɴʙᴀɴ", callback_data=f"action_unban_{chat_id}_{uid}_{name[:20]}")
            ]])
            update.message.reply_text(
                f"{calypso_header('🚫 ᴀᴜᴛᴏ-ʙᴀɴ')}"
                f"*ᴜꜱᴇʀ:* [{to_small_caps(name)}](tg://user?id={uid})\n"
                f"ʀᴇᴀᴄʜᴇᴅ *{WARN_LIMIT} ᴡᴀʀɴꜱ* — ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ ʙᴀɴɴᴇᴅ.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb
            )
            log_action(context, chat, "ᴀᴜᴛᴏ-ʙᴀɴ", uid, "ᴄᴀʟʏᴘꜱᴏ", f"{WARN_LIMIT} ᴡᴀʀɴꜱ")
        except BadRequest as e:
            update.message.reply_text(f"✦ ᴄᴏᴜʟᴅ ɴᴏᴛ ʙᴀɴ: {sc(e.message)}")
    else:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⌁ ᴜɴᴡᴀʀɴ", callback_data=f"action_unwarn_{chat_id}_{uid}_{name[:20]}")
        ]])
        update.message.reply_text(
            f"{calypso_header('⚠️ ᴡᴀʀɴᴇᴅ')}"
            f"*ᴜꜱᴇʀ:* [{to_small_caps(name)}](tg://user?id={uid})\n"
            f"*ʀᴇᴀꜱᴏɴ:* {reason}\n"
            f"*ᴡᴀʀɴꜱ:* `{count}/{WARN_LIMIT}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb
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
        f"{calypso_header('⌁ ᴡᴀʀɴ ʀᴇᴍᴏᴠᴇᴅ')}"
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
        f"⌁ ᴡᴀʀɴꜱ ʀᴇꜱᴇᴛ ꜰᴏʀ [{to_small_caps(name)}](tg://user?id={uid}).",
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
    update.message.reply_text("⌁ ᴡᴇʟᴄᴏᴍᴇ ᴍᴇꜱꜱᴀɢᴇ ꜱᴀᴠᴇᴅ.")

@admin_required
def setgoodbye(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    msg_text = update.message.text.split(None, 1)
    if len(msg_text) < 2:
        update.message.reply_text("ᴜꜱᴀɢᴇ: /ꜱᴇᴛɢᴏᴏᴅʙʏᴇ [ᴍᴇꜱꜱᴀɢᴇ]")
        return
    db["goodbye"][chat_id]["msg"] = msg_text[1]
    update.message.reply_text("⌁ ɢᴏᴏᴅʙʏᴇ ᴍᴇꜱꜱᴀɢᴇ ꜱᴀᴠᴇᴅ.")

@admin_required
def resetwelcome(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    db["welcome"][chat_id]["msg"] = None
    update.message.reply_text("⌁ ᴡᴇʟᴄᴏᴍᴇ ᴍᴇꜱꜱᴀɢᴇ ʀᴇꜱᴇᴛ ᴛᴏ ᴅᴇꜰᴀᴜʟᴛ.")

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
        update.message.reply_text(f"⌁ {key_str} ɪꜱ ɴᴏᴡ *{status_str}*.", parse_mode=ParseMode.MARKDOWN)
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
                InlineKeyboardButton("⌁ ɪ'ᴍ ʜᴜᴍᴀɴ — ᴄʟɪᴄᴋ ᴛᴏ ᴠᴇʀɪꜰʏ", callback_data=f"captcha_{chat_id}_{member.id}")
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
        text = template.replace("{first}", member.first_name or "")
        text = text.replace("{last}", member.last_name or "")
        # Fix: safe {username} — use first_name if no username (prevents crash)
        text = text.replace("{username}", f"@{member.username}" if member.username else (member.first_name or str(member.id)))
        text = text.replace("{mention}", f"[{member.first_name or member.id}](tg://user?id={member.id})")
        text = text.replace("{chatname}", chat.title or "")
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
    template = cfg["msg"] or f"👋 *{to_small_caps(member.first_name)}* {to_small_caps('group chhod gaya.')} *{to_small_caps(chat.title or '')}* {to_small_caps('mein yaad rahega.')}"
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
    query.edit_message_text(f"⌁ [{query.from_user.first_name}](tg://user?id={user_id}) ᴠᴇʀɪꜰɪᴇᴅ!", parse_mode=ParseMode.MARKDOWN)
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
                update.message.reply_text("✦ ᴘʟᴇᴀꜱᴇ ᴘʀᴏᴠɪᴅᴇ ᴀ ᴘᴏꜱɪᴛɪᴠᴇ ɴᴜᴍʙᴇʀ.")
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
            update.message.reply_text("✦ ᴄᴀɴ'ᴛ ᴅᴇʟᴇᴛᴇ ᴛʜᴀᴛ ᴍᴇꜱꜱᴀɢᴇ.")

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
            msg.reply_text("✦ ᴜɴꜱᴜᴘᴘᴏʀᴛᴇᴅ ᴍᴇᴅɪᴀ ᴛʏᴘᴇ.")
            return
    elif len(parts) >= 3:
        db["filters"][chat_id][keyword] = {"type": "text", "text": parts[2]}
    else:
        msg.reply_text("ᴜꜱᴀɢᴇ: /ꜰɪʟᴛᴇʀ [ᴋᴇʏᴡᴏʀᴅ] [ʀᴇᴘʟʏ ᴛᴇxᴛ]\nᴏʀ ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴍᴇᴅɪᴀ ᴍᴇꜱꜱᴀɢᴇ ᴡɪᴛʜ /ꜰɪʟᴛᴇʀ [ᴋᴇʏᴡᴏʀᴅ]")
        return

    msg.reply_text(f"⌁ ꜰɪʟᴛᴇʀ `{keyword}` ᴀᴅᴅᴇᴅ.", parse_mode=ParseMode.MARKDOWN)

@admin_required
def stop_filter(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text("ᴜꜱᴀɢᴇ: /ꜱᴛᴏᴘ [ᴋᴇʏᴡᴏʀᴅ]")
        return
    chat_id = update.effective_chat.id
    kw = context.args[0].lower()
    db["filters"][chat_id].pop(kw, None)
    update.message.reply_text(f"⌁ ꜰɪʟᴛᴇʀ `{kw}` ʀᴇᴍᴏᴠᴇᴅ.", parse_mode=ParseMode.MARKDOWN)

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
    update.message.reply_text(f"⌁ ɴᴏᴛᴇ `{name}` ꜱᴀᴠᴇᴅ. ʀᴇᴛʀɪᴇᴠᴇ ᴡɪᴛʜ `/ɢᴇᴛ {name}` ᴏʀ `#{name}`", parse_mode=ParseMode.MARKDOWN)

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
        update.message.reply_text(f"✦ ɴᴏ ɴᴏᴛᴇ ɴᴀᴍᴇᴅ `{name}`.", parse_mode=ParseMode.MARKDOWN)

@admin_required
def clear_note(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    name = context.args[0].lower() if context.args else None
    if not name:
        update.message.reply_text("ᴜꜱᴀɢᴇ: /ᴄʟᴇᴀʀ [ɴᴀᴍᴇ]")
        return
    db["notes"][chat_id].pop(name, None)
    update.message.reply_text(f"⌁ ɴᴏᴛᴇ `{name}` ᴅᴇʟᴇᴛᴇᴅ.", parse_mode=ParseMode.MARKDOWN)

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
    update.message.reply_text(f"⌁ `{word}` ᴀᴅᴅᴇᴅ ᴛᴏ ʙʟᴀᴄᴋʟɪꜱᴛ.", parse_mode=ParseMode.MARKDOWN)

@admin_required
def rm_blacklist(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text("ᴜꜱᴀɢᴇ: /ʀᴍʙʟᴀᴄᴋʟɪꜱᴛ [ᴡᴏʀᴅ]")
        return
    chat_id = update.effective_chat.id
    word = " ".join(context.args).lower()
    db["blacklist"][chat_id].discard(word)
    update.message.reply_text(f"⌁ `{word}` ʀᴇᴍᴏᴠᴇᴅ ꜰʀᴏᴍ ʙʟᴀᴄᴋʟɪꜱᴛ.", parse_mode=ParseMode.MARKDOWN)

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
    if user and user.id in db["approved"].get(chat_id, set()):
        return
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
        update.message.reply_text(f"✦ ᴜɴᴋɴᴏᴡɴ ᴛʏᴘᴇ. ᴀᴠᴀɪʟᴀʙʟᴇ: ꜱᴛɪᴄᴋᴇʀ, ɢɪꜰ, ᴜʀʟ, ꜰᴏʀᴡᴀʀᴅ, ᴘʜᴏᴛᴏ, ᴠɪᴅᴇᴏ, ᴠᴏɪᴄᴇ, ᴅᴏᴄᴜᴍᴇɴᴛ")

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
    if user and user.id in db["approved"].get(chat_id, set()):
        return
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
        update.message.reply_text("⌁ ᴀɴᴛɪ-ꜰʟᴏᴏᴅ ᴅɪꜱᴀʙʟᴇᴅ.")
    elif val.isdigit() and int(val) > 1:
        db["antiflood"][chat_id]["enabled"] = True
        db["antiflood"][chat_id]["limit"] = int(val)
        update.message.reply_text(f"⌁ ᴀɴᴛɪ-ꜰʟᴏᴏᴅ ꜱᴇᴛ ᴛᴏ `{val}` ᴍᴇꜱꜱᴀɢᴇꜱ.", parse_mode=ParseMode.MARKDOWN)
    else:
        update.message.reply_text("✦ ɪɴᴠᴀʟɪᴅ ᴠᴀʟᴜᴇ.")

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
    if user.id in db["approved"].get(chat_id, set()):
        return
    tracker = db["flood_tracker"][chat_id][user.id]
    now = time.time()
    tracker = [t for t in tracker if now - t < 5]
    tracker.append(now)
    db["flood_tracker"][chat_id][user.id] = tracker
    if len(tracker) > cfg["limit"]:
        try:
            chat.restrict_member(user.id, permissions=ChatPermissions(can_send_messages=False))
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔊 ᴜɴᴍᴜᴛᴇ", callback_data=f"action_unmute_{chat_id}_{user.id}_{(user.first_name or 'user')[:20]}")
            ]])
            update.message.reply_text(
                f"⚡ [{to_small_caps(user.first_name or 'user')}](tg://user?id={user.id}) {to_small_caps('flood ke liye mute ho gaya!')}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb
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
    update.message.reply_text("⌁ ʀᴜʟᴇꜱ ꜱᴀᴠᴇᴅ.")

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
        update.message.reply_text("✦ ᴀᴅᴍɪɴ ᴛɪᴛʟᴇ ᴄᴀɴɴᴏᴛ ᴇxᴄᴇᴇᴅ *16 ᴄʜᴀʀᴀᴄᴛᴇʀꜱ*.", parse_mode=ParseMode.MARKDOWN)
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
            update.message.reply_text("✦ ɪ ɴᴇᴇᴅ ᴛʜᴇ *ᴀᴅᴅ ᴀᴅᴍɪɴꜱ* ᴘᴇʀᴍɪꜱꜱɪᴏɴ ᴛᴏ ᴘʀᴏᴍᴏᴛᴇ ᴜꜱᴇʀꜱ.", parse_mode=ParseMode.MARKDOWN)
        elif "can't remove chat owner" in err or "cant remove chat owner" in err:
            update.message.reply_text("✦ ᴄᴀɴɴᴏᴛ ᴘʀᴏᴍᴏᴛᴇ/ᴅᴇᴍᴏᴛᴇ ᴛʜᴇ ᴄʜᴀᴛ ᴏᴡɴᴇʀ.")
        else:
            update.message.reply_text(f"✦ ꜰᴀɪʟᴇᴅ: {sc(e.message)}")

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
        update.message.reply_text("✦ ᴀᴅᴍɪɴ ᴛɪᴛʟᴇ ᴄᴀɴɴᴏᴛ ᴇxᴄᴇᴇᴅ *16 ᴄʜᴀʀᴀᴄᴛᴇʀꜱ*.", parse_mode=ParseMode.MARKDOWN)
        return

    # Check that the target is actually an admin
    admins = chat.get_administrators()
    is_admin = any(m.user.id == uid for m in admins)
    if not is_admin:
        update.message.reply_text("✦ ᴛʜᴀᴛ ᴜꜱᴇʀ ɪꜱ ɴᴏᴛ ᴀɴ ᴀᴅᴍɪɴ. ᴜꜱᴇ /ᴘʀᴏᴍᴏᴛᴇ ꜰɪʀꜱᴛ.")
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
            update.message.reply_text("✦ ɪ ᴄᴀɴ ᴏɴʟʏ ꜱᴇᴛ ᴛɪᴛʟᴇꜱ ᴏɴ ᴀᴅᴍɪɴꜱ ɪ ᴘʀᴏᴍᴏᴛᴇᴅ ᴍʏꜱᴇʟꜰ.")
        else:
            update.message.reply_text(f"✦ ꜰᴀɪʟᴇᴅ: {sc(e.message)}")

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
            f"⌁ [{to_small_caps(name)}](tg://user?id={uid}) ᴅᴇᴍᴏᴛᴇᴅ.",
            parse_mode=ParseMode.MARKDOWN
        )
    except BadRequest as e:
        err = e.message.lower()
        if "chat_admin_required" in err or "not enough rights" in err:
            update.message.reply_text("✦ ɪ ɴᴇᴇᴅ ᴛʜᴇ *ᴀᴅᴅ ᴀᴅᴍɪɴꜱ* ᴘᴇʀᴍɪꜱꜱɪᴏɴ ᴛᴏ ᴅᴇᴍᴏᴛᴇ ᴜꜱᴇʀꜱ.", parse_mode=ParseMode.MARKDOWN)
        elif "can't remove chat owner" in err or "cant remove chat owner" in err:
            update.message.reply_text("✦ ᴄᴀɴɴᴏᴛ ᴅᴇᴍᴏᴛᴇ ᴛʜᴇ ᴄʜᴀᴛ ᴏᴡɴᴇʀ.")
        else:
            update.message.reply_text(f"✦ ꜰᴀɪʟᴇᴅ: {sc(e.message)}")

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
        update.message.reply_text(f"✦ ꜰᴀɪʟᴇᴅ: {sc(e.message)}")

@admin_required
def unpin(update: Update, context: CallbackContext):
    try:
        update.effective_chat.unpin_message()
        update.message.reply_text("📌 ᴍᴇꜱꜱᴀɢᴇ ᴜɴᴘɪɴɴᴇᴅ.")
    except BadRequest as e:
        update.message.reply_text(f"✦ ꜰᴀɪʟᴇᴅ: {sc(e.message)}")

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
        f"*ɴᴀᴍᴇ:* {to_small_caps(user.first_name)} {to_small_caps(user.last_name or '')}\n"
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
        update.message.reply_text(f"⌁ ᴄᴀᴘᴛᴄʜᴀ ɪꜱ ɴᴏᴡ *{status}*.", parse_mode=ParseMode.MARKDOWN)
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

# ─── KNOWN CHATS + USERS (for broadcast) ──────────────
_known_chats: set = set()
_known_users: set = set()   # users who /start'd the bot in PM

def track_chat(update: Update, context: CallbackContext):
    """Track every chat the bot is active in."""
    if update.effective_chat and update.effective_chat.type != "private":
        _known_chats.add(update.effective_chat.id)
    elif update.effective_chat and update.effective_chat.type == "private":
        if update.effective_user:
            _known_users.add(update.effective_user.id)

# ═══════════════════════════════════════════════════════
#  18. BROADCAST (Owner only) — fixed: all groups + all PM users
# ═══════════════════════════════════════════════════════

@owner_required
def broadcast(update: Update, context: CallbackContext):
    parts = update.message.text.split(None, 1)
    if len(parts) < 2:
        update.message.reply_text(
            "ᴜꜱᴀɢᴇ: /ʙʀᴏᴀᴅᴄᴀꜱᴛ [ᴍᴇꜱꜱᴀɢᴇ]\n"
            "ꜱᴇɴᴅꜱ ᴛᴏ ᴀʟʟ ɢʀᴏᴜᴘꜱ + ᴀʟʟ ᴘᴍ ᴜꜱᴇʀꜱ."
        )
        return
    text = parts[1]
    success, failed = 0, 0
    # ── Send to all known groups ──
    for cid in list(_known_chats):
        try:
            context.bot.send_message(cid, text, parse_mode=ParseMode.MARKDOWN)
            success += 1
            time.sleep(0.05)   # avoid flood
        except Exception:
            failed += 1
    # ── Send to all PM users who started the bot ──
    for uid in list(_known_users):
        if uid == update.effective_user.id:
            continue   # skip owner themselves
        try:
            context.bot.send_message(uid, text, parse_mode=ParseMode.MARKDOWN)
            success += 1
            time.sleep(0.05)
        except Exception:
            failed += 1
    update.message.reply_text(
        f"📢 ʙʀᴏᴀᴅᴄᴀꜱᴛ ᴄᴏᴍᴘʟᴇᴛᴇ.\n"
        f"⌁ ꜱᴇɴᴛ: `{success}` | ✦ ꜰᴀɪʟᴇᴅ: `{failed}`\n"
        f"🏘 ɢʀᴏᴜᴘꜱ: `{len(_known_chats)}` | 👤 ᴘᴍ ᴜꜱᴇʀꜱ: `{len(_known_users)}`",
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
#  20. AI CHAT FEATURE  🤖
# ═══════════════════════════════════════════════════════
# Commands:
#   /ai on   — enable AI replies in this group
#   /ai off  — disable AI replies
#   /ai [question] — ask something directly
#
# When enabled: bot replies to messages that mention it by name
# or start with "calypso" naturally in hinglish, savage small caps style
# ═══════════════════════════════════════════════════════

import urllib.request as _urlreq

_AI_GROQ_KEY = os.environ.get("GROQ_API_KEY", "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")
_ai_enabled: dict = defaultdict(lambda: False)   # {chat_id: bool}
_ai_history: dict = defaultdict(list)            # {(chat_id, user_id): [{role,content}, ...]}
_AI_HISTORY_LIMIT = 20                           # max messages kept per chat

# Fix 10: Per-user AI cooldown (30 seconds)
_ai_cooldown: dict = {}   # {(chat_id, user_id): last_used_timestamp}
_AI_COOLDOWN_SECS = 8

# Fix 8: Anti-link/forward-spam settings
db["antilink"] = defaultdict(lambda: False)       # {chat_id: bool}
db["antiforward"] = defaultdict(lambda: False)    # {chat_id: bool}

# ── Trigger words — bot replies when AI is ON and these appear ──────
_AI_TRIGGERS = {
    "hello", "hi", "hey", "heyy", "heyyy", "hlo", "hii", "hiiii",
    "bhai", "yaar", "dost", "kal", "bata", "suno", "bol", "kya",
    "kaise", "kaisa", "kya kar", "kya hua", "kya bolta", "kya scene",
    "arey", "are", "abe", "oye", "oi", "yo", "sup", "wassup",
    "good morning", "good night", "gm", "gn", "subah", "raat",
    "bc", "bkl", "chutiye", "yaar suno", "bhai suno", "help", "batao",
    "kya lagta", "sach bata", "seriously", "really", "pakka", "sach mein",
    "lol", "lmao", "haha", "hehe", "xd", "💀", "😭", "🔥",
    # Fix 7: Urdu/Roman Urdu additions
    "kia", "kia hal", "kaise ho", "theek", "thik", "sahi",
    "nahi", "nahin", "haan", "han", "accha", "acha", "wah",
    "yaar bol", "dost bata", "koi hai", "koi btao", "btao",
    "matlab", "samjha", "samjho", "pata", "maloom", "jaanta",
    "kyun", "kyunki", "isliye", "toh", "phir", "fir",
    "mazaa", "maza", "funny", "boring", "thaka", "neend",
    "khana", "khaana", "pani", "chai", "mood", "feel",
}

_AI_SYSTEM_PROMPT = """Tu Calypso hai — lekin yeh mat soch ki tu koi character play kar raha hai. Tu bas ek banda hai jo is group mein baat kar raha hai.

Sabse zaroori cheez: Jo bola gaya hai usse SAMAJH pehle. Matlab kya hai, mood kya hai, kya chahta hai woh banda — phir reply kar. Jaise koi dost sunta hai aur phir bolता है.

Agar koi keh raha hai "yaar thak gaya" — toh seedha "kya hua?" puchh, lecture mat de.
Agar koi koi sawaal pooch raha hai — properly answer kar, phir bas.
Agar koi joke maar raha hai — hasne wali reply de, serious mat ho ja.
Agar koi vent kar raha hai — sun, samajh, ek do line mein respond kar.
Agar koi confused hai — clearly explain kar apne words mein.
Agar koi argument pe hai — apni side lo, dono side mat karo.

Baat karne ka tarika:
Hinglish — jaise real zindagi mein log bolte hain. Na pure Hindi, na pure English. Mix jo natural lage.
Reply chhoti rakh jab chhoti ho sakti hai. Lambi tab karo jab actually zaroori ho.
Emoji agar bilkul zaroori lage tabhi, warna nahi.
Koi script nahi hai — har message apne aap pe reply karta hai.

Conversation yaad rakh — agar pehle kuch baat hui hai toh usse naturally continue kar, dobara se mat shuru kar."""

_FALLBACK_REPLIES = [
    "abe yaar net slow hai mera, phir puchh 😭",
    "bc kuch toh gadbad ho gayi, ek baar aur bol",
    "yaar server ne doka de diya, retry kar",
    "bhai AI thak gaya thoda, thodi der mein bata",
]

def _call_groq_ai(user_text: str, chat_id: int = 0, user_id: int = 0, user_name: str = "") -> str:
    """Call Groq API with per-user conversation history. Falls back to local reply if fails."""
    import random as _r
    if not _AI_GROQ_KEY:
        return _r.choice(_FALLBACK_REPLIES)
    hist_key = (chat_id, user_id) if user_id else chat_id
    try:
        import json as _json
        # Add user's name to system context so bot can address them
        system = _AI_SYSTEM_PROMPT
        if user_name:
            system += f"\n\nIs waqt tujhse baat kar raha hai: {user_name}"
        messages = [{"role": "system", "content": system}]
        history = _ai_history.get(hist_key, [])
        messages.extend(history[-_AI_HISTORY_LIMIT:])
        messages.append({"role": "user", "content": user_text})

        payload = _json.dumps({
            "model": "llama-3.3-70b-versatile",
            "messages": messages,
            "max_tokens": 350,
            "temperature": 0.9,
            "top_p": 0.95,
            "frequency_penalty": 0.6,
            "presence_penalty": 0.5,
        }).encode("utf-8")
        req = _urlreq.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {_AI_GROQ_KEY}",
                "Content-Type": "application/json"
            }
        )
        with _urlreq.urlopen(req, timeout=12) as resp:
            result = _json.loads(resp.read())
            reply = result["choices"][0]["message"]["content"].strip()
        # Save to per-user history
        _ai_history[hist_key].append({"role": "user", "content": user_text})
        _ai_history[hist_key].append({"role": "assistant", "content": reply})
        if len(_ai_history[hist_key]) > _AI_HISTORY_LIMIT * 2:
            _ai_history[hist_key] = _ai_history[hist_key][-_AI_HISTORY_LIMIT:]
        return reply
    except Exception as e:
        logger.warning(f"AI error: {e}")
        return _r.choice(_FALLBACK_REPLIES)


@admin_required
def ai_cmd(update: Update, context: CallbackContext):
    """/ai on | /ai off | /ai [question]"""
    chat_id = update.effective_chat.id
    if not context.args:
        status = to_small_caps("on") + " ⌁" if _ai_enabled[chat_id] else to_small_caps("off") + " ·"
        update.message.reply_text(
            f"{calypso_header('🤖 ᴀɪ ᴄʜᴀᴛ')}"
            f"ꜱᴛᴀᴛᴜꜱ: *{status}*\n\n"
            f"ᴜꜱᴀɢᴇ:\n"
            f"`/ai on` — ᴇɴᴀʙʟᴇ\n"
            f"`/ai off` — ᴅɪꜱᴀʙʟᴇ\n"
            f"`/ai [ꜱᴀᴡᴀᴀʟ]` — ᴅɪʀᴇᴄᴛ ꜱᴀᴡᴀᴀʟ\n\n"
            f"ᴡʜᴇɴ ᴏɴ: ʙᴏᴛ ʀᴇᴘʟɪᴇꜱ ᴛᴏ ᴛʀɪɢɢᴇʀ ᴡᴏʀᴅꜱ + ʀᴇᴘʟɪᴇꜱ ᴛᴏ ɪᴛ.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    first_arg = context.args[0].lower()
    if first_arg == "on":
        _ai_enabled[chat_id] = True
        update.message.reply_text(
            f"{calypso_header('🤖 ᴀɪ ᴄʜᴀᴛ')}"
            "ᴀɪ ᴏɴ ʜᴏ ɢᴀʏᴀ ⌁\n"
            "ʙᴀᴀᴛ ᴋᴀʀᴏ — ʜᴇʟʟᴏ, ʙʜᴀɪ, ʏᴀᴀʀ ʏᴀ ʀᴇᴘʟʏ ᴋᴀʀᴏ ᴍᴇʀᴇ ᴍᴇꜱꜱᴀɢᴇ ᴘᴇ.",
            parse_mode=ParseMode.MARKDOWN
        )
    elif first_arg == "off":
        _ai_enabled[chat_id] = False
        # Clear all per-user histories for this chat
        keys_to_del = [k for k in _ai_history if (isinstance(k, tuple) and k[0] == chat_id) or k == chat_id]
        for k in keys_to_del:
            _ai_history.pop(k, None)
        update.message.reply_text(
            f"{calypso_header('🤖 ᴀɪ ᴄʜᴀᴛ')}"
            "ᴀɪ ʙᴀɴᴅ ᴋᴀʀ ᴅɪʏᴀ ·",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        # Direct question — always works regardless of AI on/off
        question = " ".join(context.args)
        try:
            context.bot.send_chat_action(chat_id, action="typing")
        except Exception:
            pass
        thinking_msg = update.message.reply_text(to_small_caps("soch raha hoon... ⌁"))
        reply = _call_groq_ai(question, chat_id, update.effective_user.id, update.effective_user.first_name or "")
        try:
            thinking_msg.delete()
        except Exception:
            pass
        update.message.reply_text(reply)


def ai_message_handler(update: Update, context: CallbackContext):
    """Auto-reply when AI is ON — triggers on keyword match or reply to bot."""
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    if not _ai_enabled.get(chat_id):
        return
    text = update.message.text
    text_lower = text.lower().strip()
    bot_username = (context.bot.username or "calypso").lower()

    trigger = False

    # 1. Someone replied to bot's message
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        if update.message.reply_to_message.from_user.id == context.bot.id:
            trigger = True

    # 2. Bot @mentioned by username (NOT just name in text — avoid eavesdropping)
    if update.message.entities:
        for entity in update.message.entities:
            if entity.type == "mention":
                mentioned = text[entity.offset:entity.offset + entity.length].lstrip("@").lower()
                if mentioned == bot_username:
                    trigger = True
                    break

    # 3. Trigger words (only if NOT a mention-only check, avoid privacy issues)
    if not trigger:
        # Check if any trigger word appears as standalone word
        words = set(re.findall(r"[\w']+", text_lower))
        # Also check multi-word triggers
        for tw in _AI_TRIGGERS:
            if " " in tw:
                if tw in text_lower:
                    trigger = True
                    break
            elif tw in words:
                trigger = True
                break

    if not trigger:
        return

    # Fix 10: Per-user AI rate limiting
    now = time.time()
    cool_key = (chat_id, update.effective_user.id)
    last_used = _ai_cooldown.get(cool_key, 0)
    if now - last_used < _AI_COOLDOWN_SECS:
        return  # silently ignore — don't spam "cooldown" messages
    _ai_cooldown[cool_key] = now

    # Show typing action for realism
    try:
        context.bot.send_chat_action(chat_id, action="typing")
    except Exception:
        pass

    # If replying to someone's message, include that context
    full_text = text
    if update.message.reply_to_message and update.message.reply_to_message.text:
        replied_text = update.message.reply_to_message.text[:200]
        replied_name = (update.message.reply_to_message.from_user.first_name or "") if update.message.reply_to_message.from_user else ""
        if replied_name:
            full_text = f'[{replied_name} ne pehle kaha tha: "{replied_text}"]\n{text}'

    reply = _call_groq_ai(full_text, chat_id, update.effective_user.id, update.effective_user.first_name or "")
    update.message.reply_text(reply)


# ═══════════════════════════════════════════════════════
#  21. APPROVAL SYSTEM  ⌁
# ═══════════════════════════════════════════════════════
# /approval on/off  — toggle join request notifications in group
# /approve          — approve a user (exempt from flood/locks/blacklist)
# /unapprove        — remove approval
# /approved         — list approved users
# When join request arrives → bot posts in GROUP with Accept/Reject buttons
# ═══════════════════════════════════════════════════════

_approval_enabled: dict = defaultdict(lambda: False)   # {chat_id: bool}


@admin_required
def approval_cmd(update: Update, context: CallbackContext):
    """/approval on|off — toggle join request notification system."""
    chat_id = update.effective_chat.id
    if not context.args:
        status = "ᴏɴ ⌁" if _approval_enabled[chat_id] else "ᴏꜰꜰ ✦"
        update.message.reply_text(
            f"{calypso_header('⌁ ᴀᴘᴘʀᴏᴠᴀʟ ꜱʏꜱᴛᴇᴍ')}"
            f"ꜱᴛᴀᴛᴜꜱ: *{status}*\n"
            "`/approval on` — ᴊᴏɪɴ ʀᴇǫᴜᴇꜱᴛ ᴍᴏᴅ ᴇɴᴀʙʟᴇ\n"
            "`/approval off` — ᴅɪꜱᴀʙʟᴇ",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    val = context.args[0].lower()
    if val == "on":
        _approval_enabled[chat_id] = True
        update.message.reply_text(
            "⌁ *ᴀᴘᴘʀᴏᴠᴀʟ ꜱʏꜱᴛᴇᴍ ᴏɴ* ⌁\n"
            "ᴊᴏɪɴ ʀᴇǫᴜᴇꜱᴛꜱ ᴡɪʟʟ ɴᴏᴡ ʙᴇ ɴᴏᴛɪꜰɪᴇᴅ ɪɴ ᴛʜᴇ ɢʀᴏᴜᴘ.",
            parse_mode=ParseMode.MARKDOWN
        )
    elif val == "off":
        _approval_enabled[chat_id] = False
        update.message.reply_text("✦ ᴀᴘᴘʀᴏᴠᴀʟ ꜱʏꜱᴛᴇᴍ ᴅɪꜱᴀʙʟᴇᴅ.")
    else:
        update.message.reply_text("ᴜꜱᴀɢᴇ: /approval on|off")


@admin_required
def approve_cmd(update: Update, context: CallbackContext):
    """/approve — approve a user, exempt from flood/locks/blacklist."""
    uid, name = get_target_user(update, context)
    if not uid:
        update.message.reply_text(
            "⚠️ " + to_small_caps("reply karo ya @username/id do")
        )
        return
    chat_id = update.effective_chat.id
    db["approved"][chat_id].add(uid)
    update.message.reply_text(
        f"{calypso_header('⌁ ᴜꜱᴇʀ ᴀᴘᴘʀᴏᴠᴇᴅ')}"
        f"[{to_small_caps(name)}](tg://user?id={uid}) has been approved.\n"
        "ᴛʜɪꜱ ᴜꜱᴇʀ ɪꜱ ɴᴏᴡ ᴇxᴇᴍᴘᴛ ꜰʀᴏᴍ ꜰʟᴏᴏᴅ/ʟᴏᴄᴋ/ʙʟᴀᴄᴋʟɪꜱᴛ.",
        parse_mode=ParseMode.MARKDOWN
    )


@admin_required
def unapprove_cmd(update: Update, context: CallbackContext):
    """/unapprove — remove approval from a user."""
    uid, name = get_target_user(update, context)
    if not uid:
        update.message.reply_text("⚠️ " + to_small_caps("reply karo ya @username/id do"))
        return
    chat_id = update.effective_chat.id
    db["approved"][chat_id].discard(uid)
    update.message.reply_text(
        f"✦ [{to_small_caps(name)}](tg://user?id={uid}) approval has been removed.",
        parse_mode=ParseMode.MARKDOWN
    )


@admin_required
def approved_list_cmd(update: Update, context: CallbackContext):
    """/approved — list all approved users in this chat."""
    chat_id = update.effective_chat.id
    uids = db["approved"].get(chat_id, set())
    if not uids:
        update.message.reply_text("ɴᴏ ᴀᴘᴘʀᴏᴠᴇᴅ ᴜꜱᴇʀꜱ ɪɴ ᴛʜɪꜱ ᴄʜᴀᴛ.")
        return
    lines = [f"{calypso_header('⌁ ᴀᴘᴘʀᴏᴠᴇᴅ ᴜꜱᴇʀꜱ')}"]
    for uid in uids:
        udata = db["user_cache"].get(chat_id, {}).get(uid, {})
        name = udata.get("first_name") or str(uid)
        lines.append(f"• [{to_small_caps(name)}](tg://user?id={uid})")
    update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


def handle_join_request(update: Update, context: CallbackContext):
    """Handle incoming join requests — post notification IN THE GROUP with buttons."""
    req = update.chat_join_request
    if not req:
        return
    chat_id = req.chat.id
    if not _approval_enabled.get(chat_id):
        # Auto-approve if system is off
        try:
            context.bot.approve_chat_join_request(chat_id, req.from_user.id)
        except Exception:
            pass
        return
    user  = req.from_user
    uname = f"@{user.username}" if user.username else to_small_caps("no username")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⌁ ᴀᴄᴄᴇᴘᴛ", callback_data=f"jr_approve_{chat_id}_{user.id}"),
        InlineKeyboardButton("✦ ʀᴇᴊᴇᴄᴛ",  callback_data=f"jr_reject_{chat_id}_{user.id}"),
    ]])
    msg_text = (
        f"{calypso_header('🔔 ɴᴇᴡ ᴊᴏɪɴ ʀᴇǫᴜᴇꜱᴛ')}"
        f"[{to_small_caps(user.first_name)}](tg://user?id={user.id}) "
        + to_small_caps("wants to join the group") + "\n\n"
        f"*ᴜꜱᴇʀɴᴀᴍᴇ:* {uname}\n"
        f"*ɪᴅ:* `{user.id}`"
    )
    # Post notification in the GROUP itself
    try:
        context.bot.send_message(
            chat_id,
            msg_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb
        )
    except Exception as e:
        logger.warning(f"handle_join_request: failed to notify group: {e}")


def join_request_callback(update: Update, context: CallbackContext):
    """Approve or reject a join request via inline button — admin only."""
    query = update.callback_query
    data  = query.data
    clicker = query.from_user
    try:
        parts   = data.split("_")
        action  = parts[1]           # approve | reject
        chat_id = int(parts[2])
        user_id = int(parts[3])
    except Exception:
        query.answer("ɪɴᴠᴀʟɪᴅ ᴅᴀᴛᴀ.", show_alert=True)
        return
    # Only admins can press
    try:
        member = context.bot.get_chat_member(chat_id, clicker.id)
        if member.status not in ("administrator", "creator"):
            query.answer(to_small_caps("sirf admins kar sakte hain yeh!"), show_alert=True)
            return
    except Exception:
        pass
    try:
        if action == "approve":
            context.bot.approve_chat_join_request(chat_id, user_id)
            query.edit_message_text(
                query.message.text + f"\n\n⌁ *{to_small_caps('accepted')}* ʙʏ {to_small_caps(clicker.first_name)}",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            context.bot.decline_chat_join_request(chat_id, user_id)
            query.edit_message_text(
                query.message.text + f"\n\n✦ *{to_small_caps('rejected')}* ʙʏ {to_small_caps(clicker.first_name)}",
                parse_mode=ParseMode.MARKDOWN
            )
        query.answer()
    except Exception as e:
        query.answer(to_small_caps(f"error: {str(e)[:50]}"), show_alert=True)


# ═══════════════════════════════════════════════════════
#  23. GROUP TOOLS — report, afk, warnreasons
# ═══════════════════════════════════════════════════════

def report_cmd(update: Update, context: CallbackContext):
    """/report — reply to a message to report it to admins."""
    msg = update.message
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == "private":
        msg.reply_text(to_small_caps("yeh group mein use karo bhai"))
        return
    if not msg.reply_to_message:
        msg.reply_text(to_small_caps("jis message ko report karna ho usse reply karo"))
        return
    reported_user = msg.reply_to_message.from_user
    reason = " ".join(context.args) if context.args else to_small_caps("koi reason nahi diya")
    try:
        admins = chat.get_administrators()
        admin_mentions = " ".join(
            f"[{to_small_caps(a.user.first_name)}](tg://user?id={a.user.id})"
            for a in admins if not a.user.is_bot
        )
    except Exception:
        admin_mentions = to_small_caps("admins")
    msg.reply_text(
        f"{calypso_header('🚨 ʀᴇᴘᴏʀᴛ')}"
        f"*ʀᴇᴘᴏʀᴛᴇᴅ ʙʏ:* [{to_small_caps(user.first_name)}](tg://user?id={user.id})\n"
        f"*ᴀɢᴀɪɴꜱᴛ:* [{to_small_caps(reported_user.first_name)}](tg://user?id={reported_user.id})\n"
        f"*ʀᴇᴀꜱᴏɴ:* {reason}\n\n"
        f"📣 {admin_mentions}",
        parse_mode=ParseMode.MARKDOWN
    )


_afk_users: dict = {}

def afk_cmd(update: Update, context: CallbackContext):
    """/afk [reason] — mark yourself as away."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    reason = " ".join(context.args) if context.args else to_small_caps("koi reason nahi")
    _afk_users[(chat_id, user.id)] = {"reason": reason, "time": time.time()}
    update.message.reply_text(
        f"💤 [{to_small_caps(user.first_name)}](tg://user?id={user.id}) "
        + to_small_caps("ab afk hai") + f"\n*ʀᴇᴀꜱᴏɴ:* {reason}",
        parse_mode=ParseMode.MARKDOWN
    )

def afk_watcher(update: Update, context: CallbackContext):
    """Remove AFK on message; warn if someone tags an AFK user."""
    if not update.message or not update.effective_user:
        return
    msg  = update.message
    user = update.effective_user
    chat_id = update.effective_chat.id
    key = (chat_id, user.id)
    if key in _afk_users:
        afk_data = _afk_users.pop(key)
        gone_secs = int(time.time() - afk_data["time"])
        if gone_secs < 60:
            gone_str = to_small_caps(f"{gone_secs}s")
        elif gone_secs < 3600:
            gone_str = to_small_caps(f"{gone_secs//60}m")
        else:
            gone_str = to_small_caps(f"{gone_secs//3600}h {(gone_secs%3600)//60}m")
        msg.reply_text(
            f"👋 [{to_small_caps(user.first_name)}](tg://user?id={user.id}) "
            + to_small_caps("wapas aa gaya") + f" *(gone {gone_str})*",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    # Fix 2: Also remove AFK when user replies to someone (they are clearly back)
    if msg.reply_to_message and msg.reply_to_message.from_user:
        replied_uid = msg.reply_to_message.from_user.id
        if replied_uid == user.id and (chat_id, user.id) in _afk_users:
            _afk_users.pop((chat_id, user.id), None)
    if msg.entities:
        for entity in msg.entities:
            if entity.type == "mention":
                mentioned = msg.text[entity.offset:entity.offset + entity.length].lstrip("@")
                for uid, udata in db["user_cache"].get(chat_id, {}).items():
                    if udata.get("username", "").lower() == mentioned.lower():
                        if (chat_id, uid) in _afk_users:
                            afk_data = _afk_users[(chat_id, uid)]
                            msg.reply_text(
                                f"💤 [{to_small_caps(udata.get('first_name', mentioned))}](tg://user?id={uid}) "
                                + to_small_caps("abhi afk hai") + f"\n*ʀᴇᴀꜱᴏɴ:* {afk_data['reason']}",
                                parse_mode=ParseMode.MARKDOWN
                            )
            elif entity.type == "text_mention" and entity.user:
                uid = entity.user.id
                if (chat_id, uid) in _afk_users:
                    afk_data = _afk_users[(chat_id, uid)]
                    msg.reply_text(
                        f"💤 [{to_small_caps(entity.user.first_name)}](tg://user?id={uid}) "
                        + to_small_caps("abhi afk hai") + f"\n*ʀᴇᴀꜱᴏɴ:* {afk_data['reason']}",
                        parse_mode=ParseMode.MARKDOWN
                    )


_warn_reasons: dict = defaultdict(lambda: defaultdict(list))

def warnreasons_cmd(update: Update, context: CallbackContext):
    """/warnreasons — show warns + reasons for a user."""
    uid, name = get_target_user(update, context)
    if not uid:
        update.message.reply_text(to_small_caps("reply karo ya user id do"))
        return
    chat_id = update.effective_chat.id
    count   = db["warns"][chat_id].get(uid, 0)
    reasons = _warn_reasons[chat_id].get(uid, [])
    if count == 0:
        update.message.reply_text(to_small_caps(f"{name} ka koi warn nahi hai."))
        return
    lines = [
        f"{calypso_header('⚠️ ᴡᴀʀɴ ʀᴇᴀꜱᴏɴꜱ')}",
        f"*ᴜꜱᴇʀ:* [{to_small_caps(name)}](tg://user?id={uid})",
        f"*ᴡᴀʀɴꜱ:* `{count}/{WARN_LIMIT}`\n",
    ]
    for i, r in enumerate(reasons, 1):
        lines.append(f"{i}. {r}")
    update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ═══════════════════════════════════════════════════════
#  24. UTILITY — calc, time, tr, weather
# ═══════════════════════════════════════════════════════

import urllib.parse as _urlparse

def calc_cmd(update: Update, context: CallbackContext):
    """/calc [expression] — simple calculator."""
    if not context.args:
        update.message.reply_text(to_small_caps("usage: /calc 2+2*10"))
        return
    expr = " ".join(context.args)
    safe = re.sub(r"[^0-9+\-*/().% ]", "", expr)
    if not safe.strip():
        update.message.reply_text(to_small_caps("bhai sirf numbers aur +/-/* daalo"))
        return
    try:
        result = eval(safe, {"__builtins__": {}})
        result = round(float(result), 10)
        result = int(result) if result == int(result) else result
        update.message.reply_text(
            f"🧮 `{safe}` = *{result}*",
            parse_mode=ParseMode.MARKDOWN
        )
    except ZeroDivisionError:
        update.message.reply_text(to_small_caps("bc zero se divide nahi hota 💀"))
    except Exception:
        update.message.reply_text(to_small_caps("yeh expression samajh nahi aaya"))


_TIMEZONES = {
    "ist":   ("Asia/Kolkata",    5, 30),
    "india": ("Asia/Kolkata",    5, 30),
    "utc":   ("UTC",             0,  0),
    "gmt":   ("UTC",             0,  0),
    "est":   ("US/Eastern",     -5,  0),
    "pst":   ("US/Pacific",     -8,  0),
    "cst":   ("Asia/Shanghai",   8,  0),
    "jst":   ("Asia/Tokyo",      9,  0),
    "pk":    ("Asia/Karachi",    5,  0),
    "pak":   ("Asia/Karachi",    5,  0),
    "dubai": ("Asia/Dubai",      4,  0),
    "uk":    ("Europe/London",   0,  0),
}

def time_cmd(update: Update, context: CallbackContext):
    """/time [zone] — current time in a timezone."""
    zone = context.args[0].lower() if context.args else "ist"
    tz_data = _TIMEZONES.get(zone)
    if not tz_data:
        zones = ", ".join(_TIMEZONES.keys())
        update.message.reply_text(to_small_caps(f"zone nahi mila. available: {zones}"))
        return
    label, hours, mins = tz_data
    from datetime import timezone as _tz
    offset = timedelta(hours=hours, minutes=mins)
    now    = datetime.now(_tz(offset))
    update.message.reply_text(
        f"🕐 *{to_small_caps(label)}*\n`{now.strftime('%d %b %Y  %H:%M:%S')}`",
        parse_mode=ParseMode.MARKDOWN
    )


def tr_cmd(update: Update, context: CallbackContext):
    """/tr [lang] [text] or reply — translate via MyMemory with LibreTranslate fallback."""
    msg = update.message
    if context.args:
        lang = context.args[0].lower()
        if len(lang) <= 3 and lang.isalpha():
            text = " ".join(context.args[1:])
        else:
            lang = "en"
            text = " ".join(context.args)
    else:
        lang = "en"
        text = ""
    if not text and msg.reply_to_message:
        text = msg.reply_to_message.text or msg.reply_to_message.caption or ""
    if not text:
        msg.reply_text(to_small_caps("usage: /tr hi Hello  ya reply karke /tr hi"))
        return
    if len(text) > 500:
        text = text[:500]
    # Try MyMemory first
    translated = None
    try:
        import urllib.request as _ur
        encoded = _urlparse.quote(text)
        url = f"https://api.mymemory.translated.net/get?q={encoded}&langpair=auto|{lang}"
        with _ur.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read())
        t = data["responseData"]["translatedText"]
        if t and t.lower() != text.lower() and not t.lower().startswith("mymemory"):
            translated = t
    except Exception as e:
        logger.warning(f"MyMemory tr error: {e}")
    # Fix 4: LibreTranslate fallback if MyMemory fails or rate-limited
    if not translated:
        try:
            import urllib.request as _ur
            payload = json.dumps({"q": text, "source": "auto", "target": lang, "format": "text"}).encode()
            req = _ur.Request(
                "https://libretranslate.de/translate",
                data=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"}
            )
            with _ur.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())
            t = data.get("translatedText", "")
            if t and t.lower() != text.lower():
                translated = t
        except Exception as e:
            logger.warning(f"LibreTranslate tr error: {e}")
    if not translated:
        msg.reply_text(to_small_caps("translation nahi mila, baad mein try karo"))
        return
    msg.reply_text(
        f"🌐 *{to_small_caps('translated')}* → `{lang}`\n{translated}",
        parse_mode=ParseMode.MARKDOWN
    )


def weather_cmd(update: Update, context: CallbackContext):
    """/weather [city] — current weather via wttr.in (free, no key)."""
    if not context.args:
        update.message.reply_text(to_small_caps("usage: /weather delhi"))
        return
    city = "+".join(context.args)
    try:
        import urllib.request as _ur
        url = f"https://wttr.in/{_urlparse.quote(city)}?format=j1"
        req = _ur.Request(url, headers={"User-Agent": "curl/7.68.0"})
        with _ur.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        cur       = data["current_condition"][0]
        area      = data["nearest_area"][0]
        city_name = area["areaName"][0]["value"]
        country   = area["country"][0]["value"]
        temp_c    = cur["temp_C"]
        feels_c   = cur["FeelsLikeC"]
        humidity  = cur["humidity"]
        wind_kmph = cur["windspeedKmph"]
        desc      = cur["weatherDesc"][0]["value"]
        update.message.reply_text(
            f"{calypso_header('🌤 ᴡᴇᴀᴛʜᴇʀ')}"
            f"📍 *{to_small_caps(city_name)}, {to_small_caps(country)}*\n\n"
            f"🌡 *{to_small_caps('temp')}:* `{temp_c}°C` *(feels {feels_c}°C)*\n"
            f"💧 *{to_small_caps('humidity')}:* `{humidity}%`\n"
            f"💨 *{to_small_caps('wind')}:* `{wind_kmph} km/h`\n"
            f"☁️ *{to_small_caps('condition')}:* {to_small_caps(desc)}",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.warning(f"weather_cmd error: {e}")
        update.message.reply_text(to_small_caps("city nahi mila ya service down hai. try: /weather delhi"))


# ═══════════════════════════════════════════════════════
#  22. FUN COMMANDS  🎉
# ═══════════════════════════════════════════════════════
# /kiss /slap /hug /pat /punch /cuddle /poke /highfive
# Reply to someone → sends GIF + cute action message
# ═══════════════════════════════════════════════════════

# nekos.best action name mapping (API endpoint names)
_NEKOS_BEST_ACTION = {
    "kiss":     "kiss",
    "slap":     "slap",
    "hug":      "hug",
    "pat":      "pat",
    "punch":    "punch",
    "cuddle":   "cuddle",
    "poke":     "poke",
    "highfive": "handshake",   # nekos.best uses 'handshake' for highfive
}

# Fallback static GIFs (only used if nekos.best API fails)
_FUN_GIFS_FALLBACK = {
    "kiss": [
        "https://i.imgur.com/KdaGmDT.gif",
        "https://i.imgur.com/HH4fZNm.gif",
    ],
    "slap": [
        "https://i.imgur.com/PJMpH.gif",
        "https://i.imgur.com/XFMblfd.gif",
    ],
    "hug": [
        "https://i.imgur.com/FqMTFi8.gif",
        "https://i.imgur.com/2GRDRsJ.gif",
    ],
    "pat": [
        "https://i.imgur.com/EQ3XSRR.gif",
        "https://i.imgur.com/a7iBNor.gif",
    ],
    "punch": [
        "https://i.imgur.com/qhTtKMY.gif",
        "https://i.imgur.com/rrqD9yn.gif",
    ],
    "cuddle": [
        "https://i.imgur.com/FqMTFi8.gif",
        "https://i.imgur.com/2GRDRsJ.gif",
    ],
    "poke": [
        "https://i.imgur.com/2GRDRsJ.gif",
        "https://i.imgur.com/a7iBNor.gif",
    ],
    "highfive": [
        "https://i.imgur.com/a7iBNor.gif",
        "https://i.imgur.com/EQ3XSRR.gif",
    ],
}


def _fetch_nekos_gif(action: str) -> str | None:
    """Fetch a random anime GIF URL from nekos.best API. Returns URL or None."""
    import urllib.request as _ur
    endpoint = _NEKOS_BEST_ACTION.get(action, action)
    try:
        url = f"https://nekos.best/api/v2/{endpoint}"
        req = _ur.Request(url, headers={"User-Agent": "CalypsoBot/1.0"})
        with _ur.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        results = data.get("results", [])
        if results:
            return results[0].get("url")
    except Exception as e:
        logger.warning(f"nekos.best fetch failed for {action}: {e}")
    return None

_FUN_MESSAGES = {
    "kiss":     ["{sender} ne {target} ko kiss kiya 💋", "{sender} smooch maar diya {target} ko 😘", "{target} ko {sender} ka pyaar mila 💕"],
    "slap":     ["{sender} ne {target} ko ek tight tamacha maara 👋😂", "THAPPAD! {sender} se {target} ko 😤", "{target} bhai, {sender} ne thoka 💀"],
    "hug":      ["{sender} ne {target} ko jaadu ki jhappi di 🤗", "aww {sender} huggin {target} 💞", "{target} ko {sender} ka pyara hug mila 🫂"],
    "pat":      ["{sender} ne {target} ka sir thapthapaya 🥺", "good boy/girl pat from {sender} to {target} 👆", "{target} ko {sender} ne head pat diya ✨"],
    "punch":    ["{sender} ne {target} ko ghoos maara 👊💥", "BOOM! {sender} ka punch {target} pe 😤", "{target} bhai, {sender} ne doka diya 😂"],
    "cuddle":   ["{sender} aur {target} ek dusre se chipak gaye 🥰", "cuddle time! {sender} + {target} 💕", "aww {sender} ne {target} ko cuddle kiya 🫂"],
    "poke":     ["{sender} ne {target} ko poke kiya 👉😏", "hey {target}! {sender} tera dhyan chahta hai 😄", "{sender} poking {target} again... 😅"],
    "highfive": ["{sender} aur {target} ka high five! 🙌✨", "yasss! {sender} + {target} high five 🙌", "{target} bhai, {sender} ne slaap kiya high five mein 🤝"],
}

import random as _random

_FUN_EMOJI = {
    "kiss": "💋", "slap": "👋", "hug": "🤗", "pat": "🥺",
    "punch": "👊", "cuddle": "🫂", "poke": "👉", "highfive": "🙌"
}

def _make_fun_handler(action: str):
    def handler(update: Update, context: CallbackContext):
        msg    = update.message
        sender = msg.from_user.first_name or "someone"
        if msg.reply_to_message and msg.reply_to_message.from_user:
            target = msg.reply_to_message.from_user.first_name or "koi"
        elif context.args:
            target = " ".join(context.args)
        else:
            target = to_small_caps("khud ke saath")
        caption_template = _random.choice(_FUN_MESSAGES[action])
        caption = to_small_caps(
            caption_template.replace("{sender}", sender).replace("{target}", target)
        )
        sent = False
        # 1. Try nekos.best API (primary — always fresh anime GIFs)
        gif_url = _fetch_nekos_gif(action)
        if gif_url:
            try:
                msg.reply_animation(animation=gif_url, caption=caption)
                sent = True
            except Exception as e:
                logger.warning(f"fun cmd send_animation failed (nekos.best): {e}")
        # 2. Fallback to static imgur GIFs
        if not sent:
            fallback_urls = _FUN_GIFS_FALLBACK.get(action, [])
            _random.shuffle(fallback_urls)
            for fb_url in fallback_urls:
                try:
                    msg.reply_animation(animation=fb_url, caption=caption)
                    sent = True
                    break
                except Exception:
                    continue
        # 3. Last resort: text only
        if not sent:
            msg.reply_text(f"{_FUN_EMOJI.get(action, '✨')} {caption}")
    return handler

def fun_cmd(update: Update, context: CallbackContext):
    """/fun — show all fun commands."""
    update.message.reply_text(
        f"{calypso_header('🎉 ꜰᴜɴ ᴄᴏᴍᴍᴀɴᴅꜱ')}"
        "ʀᴇᴘʟʏ ᴛᴏ ꜱᴏᴍᴇᴏɴᴇ ᴏʀ ᴡʀɪᴛᴇ ᴛʜᴇɪʀ ɴᴀᴍᴇ:\n\n"
        "💋 /kiss — ᴋɪꜱꜱ ᴋᴀʀᴏ\n"
        "👋 /slap — ᴛʜᴀᴘᴘᴀᴅ ᴍᴀᴀʀᴏ\n"
        "🤗 /hug — ʜᴜɢ ᴅᴏ\n"
        "🥺 /pat — ʜᴇᴀᴅ ᴘᴀᴛ ᴅᴏ\n"
        "👊 /punch — ɢʜᴏᴏꜱ ᴍᴀᴀʀᴏ\n"
        "🫂 /cuddle — ᴄᴜᴅᴅʟᴇ ᴋᴀʀᴏ\n"
        "👉 /poke — ᴘᴏᴋᴇ ᴋᴀʀᴏ\n"
        "🙌 /highfive — ʜɪɢʜ ꜰɪᴠᴇ ᴋᴀʀᴏ\n\n"
        "ꜱᴇɴᴅꜱ ᴀɴɪᴍᴇ ɢɪꜰ + ᴀᴄᴛɪᴏɴ ᴍᴇꜱꜱᴀɢᴇ! 🎊",
        parse_mode=ParseMode.MARKDOWN
    )


kiss_cmd     = _make_fun_handler("kiss")
slap_cmd     = _make_fun_handler("slap")
hug_cmd      = _make_fun_handler("hug")
pat_cmd      = _make_fun_handler("pat")
punch_cmd    = _make_fun_handler("punch")
cuddle_cmd   = _make_fun_handler("cuddle")
poke_cmd     = _make_fun_handler("poke")
highfive_cmd = _make_fun_handler("highfive")

# ═══════════════════════════════════════════════════════
#  20. RANKING
# ═══════════════════════════════════════════════════════

# ── Fonts ───────────────────────────────────────────────
_FONT_CAPS = "/usr/share/texmf/fonts/opentype/public/lm/lmromancaps10-regular.otf"
_FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_FONT_REG  = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# ── Milestone tracking ──────────────────────────────────
_milestone_sent: dict = defaultdict(int)
MILESTONES = [500, 1000, 1500, 2000, 3000, 5000, 7500, 10000]


def generate_leaderboard_image(scores: list, period_label: str) -> io.BytesIO:
    """
    scores : list of (name, count) sorted descending, max 10
    Returns: BytesIO PNG — sleek dark design with color accents
    """
    W, H = 1280, 720
    # Deep dark background with subtle gradient feel
    img  = Image.new("RGB", (W, H), color=(8, 8, 12))
    draw = ImageDraw.Draw(img)

    # ── Side accent bar (left) ────────────────────────
    draw.rectangle([0, 0, 4, H], fill=(255, 255, 255, 80))

    try:
        f_title  = ImageFont.truetype(_FONT_BOLD, 54)
        f_period = ImageFont.truetype(_FONT_REG,  22)
        f_name   = ImageFont.truetype(_FONT_REG,  23)
        f_count  = ImageFont.truetype(_FONT_BOLD, 20)
        f_rank   = ImageFont.truetype(_FONT_BOLD, 19)
        f_sig    = ImageFont.truetype(_FONT_REG,  16)
    except Exception:
        f_title = f_period = f_name = f_count = f_rank = f_sig = ImageFont.load_default()

    # ── Title ──────────────────────────────────────────
    title = to_small_caps("leaderboard")
    tw = draw.textlength(title, font=f_title)
    draw.text(((W - tw) / 2, 18), title, fill=(245, 245, 245), font=f_title)

    # Thin elegant underline
    draw.line([(W//2 - 200, 86), (W//2 + 200, 86)], fill=(255, 255, 255, 60), width=1)

    # Period label
    pl = to_small_caps(period_label.lower())
    pw = draw.textlength(pl, font=f_period)
    draw.text(((W - pw) / 2, 96), pl, fill=(130, 130, 160), font=f_period)

    if not scores:
        msg = to_small_caps("no data yet — start chatting!")
        mw = draw.textlength(msg, font=f_period)
        draw.text(((W - mw) / 2, H // 2), msg, fill=(60, 60, 80), font=f_period)
        buf = io.BytesIO(); img.save(buf, "PNG"); buf.seek(0)
        return buf

    n       = len(scores)
    PAD_L   = 72
    RANK_W  = 48
    NAME_W  = 240
    BAR_X   = PAD_L + RANK_W + NAME_W + 24
    BAR_MAX = W - BAR_X - 110
    BAR_H   = 24
    ROW_H   = max(38, (H - 158 - 30) // n)
    Y0      = 142
    max_cnt = scores[0][1] or 1

    # Color palette for top 3
    MEDALS  = ["🥇", "🥈", "🥉"]
    # Bar colors: gold, silver, bronze, then white fading
    BAR_COLORS = [
        (255, 200, 50),   # gold
        (192, 192, 210),  # silver
        (200, 130, 80),   # bronze
        (180, 180, 200),
        (160, 160, 180),
        (140, 140, 160),
        (120, 120, 140),
        (100, 100, 120),
        (85,  85, 110),
        (70,  70, 95),
    ]

    for i, (name, count) in enumerate(scores):
        y     = Y0 + i * ROW_H
        bar_w = max(6, int((count / max_cnt) * BAR_MAX))
        bar_color = BAR_COLORS[i] if i < len(BAR_COLORS) else (60, 60, 80)

        # ── Row separator ──
        if i > 0:
            draw.line([(PAD_L, y - 2), (W - 40, y - 2)], fill=(25, 25, 35), width=1)

        # ── Rank number ──
        rank_str = str(i + 1)
        draw.text((PAD_L, y + 4), rank_str, fill=(80, 80, 100), font=f_rank)

        # ── Name ──
        dname = to_small_caps((name[:20] + "\u2026") if len(name) > 20 else name)
        name_color = bar_color if i < 3 else (200, 200, 220)
        draw.text((PAD_L + RANK_W, y + 4), dname, fill=name_color, font=f_name)

        # ── Bar ──
        draw.rectangle([BAR_X, y + 2, BAR_X + bar_w, y + 2 + BAR_H], fill=bar_color)

        # ── Count ──
        cs = f"{count:,}"
        cw = draw.textlength(cs, font=f_count)
        if bar_w > cw + 18:
            draw.text((BAR_X + bar_w - cw - 7, y + 4), cs, fill=(8, 8, 12), font=f_count)
        else:
            draw.text((BAR_X + bar_w + 8, y + 4), cs, fill=bar_color, font=f_count)

    # ── Signature ──────────────────────────────────────
    sig = to_small_caps("calypso bot")
    sw  = draw.textlength(sig, font=f_sig)
    draw.text((W - sw - 24, H - 26), sig, fill=(40, 40, 60), font=f_sig)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def check_milestones(context: CallbackContext, chat_id: int):
    """Fire a group notification when daily message count hits a milestone."""
    today_str   = datetime.now().strftime("%Y-%m-%d")
    stats       = db["chat_stats"].get(chat_id, {})
    today_total = sum(d["daily"].get(today_str, 0) for d in stats.values())
    for milestone in MILESTONES:
        if today_total >= milestone and _milestone_sent[chat_id] < milestone:
            _milestone_sent[chat_id] = milestone
            try:
                context.bot.send_message(
                    chat_id,
                    f"*{to_small_caps('milestone reached')}* \U0001f3af\n\n"
                    f"\u23c1 *{milestone}* {to_small_caps('messages today')}!\n"
                    f"{to_small_caps('keep the conversation going')} \U0001f525",
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception:
                pass
            break


def _mention(uid: int, name: str, username: str) -> str:
    """Small-caps name as a tg:// mention."""
    sc_name = to_small_caps(name[:18])
    return f"[{sc_name}](tg://user?id={uid})"


def _get_scores(chat_id: int, period: str):
    """Returns (scores, period_label) where scores = [(uid,name,uname,cnt)]."""
    stats = db["chat_stats"].get(chat_id, {})
    today_str = datetime.now().strftime("%Y-%m-%d")
    week_str  = datetime.now().strftime("%Y-W%W")

    if period == "today":
        raw = [(uid, d.get("name","?"), d.get("username",""), d["daily"].get(today_str,0)) for uid,d in stats.items()]
        label = to_small_caps(f"today  {datetime.now().strftime('%d %b %Y')}")
    elif period == "week":
        raw = [(uid, d.get("name","?"), d.get("username",""), d["weekly"].get(week_str,0)) for uid,d in stats.items()]
        label = to_small_caps(f"week  w{datetime.now().strftime('%W')}  {datetime.now().strftime('%Y')}")
    else:
        raw = [(uid, d.get("name","?"), d.get("username",""), d["total"]) for uid,d in stats.items()]
        label = to_small_caps("all-time total")

    scores = sorted([(u,n,un,c) for u,n,un,c in raw if c > 0], key=lambda x: x[3], reverse=True)[:10]
    return scores, label


def _build_ranking_text(chat_id: int, period: str) -> str:
    """Build the leaderboard text for the given period. Returns None if no data."""
    stats = db["chat_stats"].get(chat_id, {})
    if not stats:
        return None

    scores, period_label = _get_scores(chat_id, period)

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
    """/ranking or .ranking — group-only leaderboard with chart image."""
    chat = update.effective_chat
    if chat.type == "private":
        update.message.reply_text(
            f"⚠️ {to_small_caps('ranking only works in groups.')}",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    stats = db["chat_stats"].get(chat.id, {})
    if not stats:
        update.message.reply_text(
            f"*{to_small_caps('chat ranking')}* ❓\n\n"
            f"{to_small_caps('no data yet — start chatting!')}",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    period = "today"
    scores, period_label = _get_scores(chat.id, period)
    text = _build_ranking_text(chat.id, period)

    # Send chart image + text caption
    try:
        img_scores = [(name, cnt) for _, name, _, cnt in scores]
        img_buf    = generate_leaderboard_image(img_scores, period_label)
        update.message.reply_photo(
            photo=img_buf,
            caption=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_ranking_keyboard(period)
        )
    except Exception:
        # Fallback to text-only if image fails
        update.message.reply_text(
            text or f"*{to_small_caps('chat ranking')}* ❓\n\n{to_small_caps('no data yet')}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_ranking_keyboard(period),
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
#  QUOTE STICKER SYSTEM  🖼
# ═══════════════════════════════════════════════════════
#
#  /quote — reply to any message → get a beautiful quote sticker
#  /quote [text] — type your own quote
#
#  Design: dark background, quote text on top (small caps),
#          sender name on bottom, calypso watermark
#
# ═══════════════════════════════════════════════════════

# Sticker canvas size (Telegram sticker max = 512x512)
_STICKER_W = 512
_STICKER_H = 512

# Font paths (DejaVu available on Render)
_FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_FONT_REG  = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _make_quote_sticker(quote_text: str, author_name: str) -> io.BytesIO:
    """
    Generate a 512x512 PNG quote sticker.
    Top: quote in small caps (wrapped)
    Bottom: — author name in small caps
    Background: deep black with subtle gradient lines
    """
    W, H = _STICKER_W, _STICKER_H
    img  = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # ── Background: dark rounded rect ──────────────────
    bg = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bg_draw = ImageDraw.Draw(bg)
    radius = 40
    bg_draw.rounded_rectangle([0, 0, W, H], radius=radius, fill=(18, 18, 22, 255))
    # Subtle top accent line
    bg_draw.line([(radius, 8), (W - radius, 8)], fill=(255, 255, 255, 40), width=1)
    # Subtle bottom accent line
    bg_draw.line([(radius, H - 8), (W - radius, H - 8)], fill=(255, 255, 255, 20), width=1)
    img.alpha_composite(bg)
    draw = ImageDraw.Draw(img)

    # ── Decorative quote mark top-left ─────────────────
    try:
        f_bigquote = ImageFont.truetype(_FONT_BOLD, 90)
    except Exception:
        f_bigquote = ImageFont.load_default()
    draw.text((28, 10), "\u201c", fill=(255, 255, 255, 35), font=f_bigquote)

    # ── Load fonts ─────────────────────────────────────
    try:
        f_quote  = ImageFont.truetype(_FONT_BOLD, 32)
        f_author = ImageFont.truetype(_FONT_REG,  26)
        f_water  = ImageFont.truetype(_FONT_REG,  18)
    except Exception:
        f_quote = f_author = f_water = ImageFont.load_default()

    # ── Quote text (small caps, wrapped) ───────────────
    sc_quote = to_small_caps(quote_text)
    # Wrap to fit width (approx 18 chars per line for font size 32)
    lines = textwrap.wrap(sc_quote, width=20)
    if len(lines) > 8:
        lines = lines[:8]
        lines[-1] = lines[-1][:17] + "…"

    total_text_h = len(lines) * 42
    quote_y_start = max(80, (H // 2) - (total_text_h // 2) - 30)

    for i, line in enumerate(lines):
        lw = draw.textlength(line, font=f_quote)
        x  = (W - lw) / 2
        y  = quote_y_start + i * 44
        # Soft shadow
        draw.text((x + 2, y + 2), line, fill=(0, 0, 0, 120), font=f_quote)
        draw.text((x, y), line, fill=(240, 240, 240, 255), font=f_quote)

    # ── Divider line ───────────────────────────────────
    div_y = quote_y_start + total_text_h + 18
    draw.line([(W // 2 - 80, div_y), (W // 2 + 80, div_y)], fill=(255, 255, 255, 60), width=1)

    # ── Author name ────────────────────────────────────
    sc_author = "— " + to_small_caps(author_name[:30])
    aw = draw.textlength(sc_author, font=f_author)
    author_y = div_y + 14
    draw.text(((W - aw) / 2 + 1, author_y + 1), sc_author, fill=(0, 0, 0, 100), font=f_author)
    draw.text(((W - aw) / 2, author_y), sc_author, fill=(180, 180, 200, 255), font=f_author)

    # ── Watermark ──────────────────────────────────────
    wm = to_small_caps("calypso")
    ww = draw.textlength(wm, font=f_water)
    draw.text((W - ww - 18, H - 30), wm, fill=(255, 255, 255, 30), font=f_water)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def quote_cmd(update: Update, context: CallbackContext):
    """/quote — reply to a message or type text to get a quote sticker."""
    msg    = update.message
    user   = update.effective_user
    author = user.first_name or user.username or "unknown"

    # Get quote text: from reply or from args
    quote_text = ""
    if msg.reply_to_message:
        replied = msg.reply_to_message
        quote_text = replied.text or replied.caption or ""
        # Author is the replied-to user
        ru = replied.from_user
        if ru:
            author = ru.first_name or ru.username or author
    if not quote_text and context.args:
        quote_text = " ".join(context.args).strip()

    if not quote_text:
        msg.reply_text(
            f"{calypso_header('🖼 ǫᴜᴏᴛᴇ ꜱᴛɪᴄᴋᴇʀ')}"
            "ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴍᴇꜱꜱᴀɢᴇ ᴏʀ ᴛʏᴘᴇ:\n"
            "`/quote your text here`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # Trim long quotes
    if len(quote_text) > 200:
        quote_text = quote_text[:197] + "…"

    try:
        sticker_buf = _make_quote_sticker(quote_text, author)
        msg.reply_sticker(sticker_buf)
    except Exception as e:
        logger.warning(f"quote_cmd error: {e}")
        msg.reply_text(f"✦ {to_small_caps('could not generate sticker.')}")

# ═══════════════════════════════════════════════════════
#  FIX 8: ANTI-LINK & ANTI-FORWARD SPAM
# ═══════════════════════════════════════════════════════

@admin_required
def antilink_cmd(update: Update, context: CallbackContext):
    """/antilink on|off — auto-delete messages with URLs/invite links."""
    chat_id = update.effective_chat.id
    if not context.args or context.args[0].lower() not in ("on", "off"):
        status = "ᴏɴ ⌁" if db["antilink"][chat_id] else "ᴏꜰꜰ ✦"
        update.message.reply_text(
            f"{calypso_header('🔗 ᴀɴᴛɪ-ʟɪɴᴋ')}"
            f"ꜱᴛᴀᴛᴜꜱ: *{status}*\n`/antilink on` ʏᴀ `/antilink off`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    db["antilink"][chat_id] = (context.args[0].lower() == "on")
    status = "ᴏɴ ⌁" if db["antilink"][chat_id] else "ᴏꜰꜰ ✦"
    update.message.reply_text(f"🔗 ᴀɴᴛɪ-ʟɪɴᴋ ɪꜱ ɴᴏᴡ *{status}*.", parse_mode=ParseMode.MARKDOWN)


@admin_required
def antiforward_cmd(update: Update, context: CallbackContext):
    """/antiforward on|off — auto-delete forwarded messages."""
    chat_id = update.effective_chat.id
    if not context.args or context.args[0].lower() not in ("on", "off"):
        status = "ᴏɴ ⌁" if db["antiforward"][chat_id] else "ᴏꜰꜰ ✦"
        update.message.reply_text(
            f"{calypso_header('📨 ᴀɴᴛɪ-ꜰᴏʀᴡᴀʀᴅ')}"
            f"ꜱᴛᴀᴛᴜꜱ: *{status}*\n`/antiforward on` ʏᴀ `/antiforward off`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    db["antiforward"][chat_id] = (context.args[0].lower() == "on")
    status = "ᴏɴ ⌁" if db["antiforward"][chat_id] else "ᴏꜰꜰ ✦"
    update.message.reply_text(f"📨 ᴀɴᴛɪ-ꜰᴏʀᴡᴀʀᴅ ɪꜱ ɴᴏᴡ *{status}*.", parse_mode=ParseMode.MARKDOWN)


def check_antilink_antiforward(update: Update, context: CallbackContext):
    """Auto-delete links/forwards if antilink/antiforward is on."""
    if not update.message or not update.effective_user:
        return
    chat = update.effective_chat
    user = update.effective_user
    chat_id = chat.id
    # Admins and approved users exempt
    if user.id in get_admin_ids(chat):
        return
    if user.id in db["approved"].get(chat_id, set()):
        return
    msg = update.message
    deleted = False
    # Anti-forward
    if db["antiforward"].get(chat_id) and msg.forward_date:
        try:
            msg.delete()
            chat.send_message(
                f"⚠️ [{to_small_caps(user.first_name or str(user.id))}](tg://user?id={user.id}) "
                + to_small_caps("— forwarded message delete ho gaya (anti-forward on hai)"),
                parse_mode=ParseMode.MARKDOWN
            )
            deleted = True
        except Exception:
            pass
    # Anti-link
    if not deleted and db["antilink"].get(chat_id):
        has_link = False
        if msg.entities:
            for e in msg.entities:
                if e.type in ("url", "text_link"):
                    has_link = True
                    break
        if not has_link and msg.text:
            # Check for t.me invite links in plain text
            if re.search(r"(https?://|t\.me/|telegram\.me/|@\w+\.\w+)", msg.text, re.IGNORECASE):
                has_link = True
        if has_link:
            try:
                msg.delete()
                chat.send_message(
                    f"⚠️ [{to_small_caps(user.first_name or str(user.id))}](tg://user?id={user.id}) "
                    + to_small_caps("— link delete ho gaya (anti-link on hai)"),
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception:
                pass


# ═══════════════════════════════════════════════════════
#  FIX 9: /mystats — personal message count
# ═══════════════════════════════════════════════════════

def mystats_cmd(update: Update, context: CallbackContext):
    """/mystats — apna personal message stats dekho."""
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == "private":
        update.message.reply_text(to_small_caps("yeh group mein use karo bhai"))
        return
    chat_id = chat.id
    stat = db["chat_stats"].get(chat_id, {}).get(user.id)
    if not stat or stat.get("total", 0) == 0:
        update.message.reply_text(to_small_caps("abhi tak koi stats nahi hain tere."))
        return
    today_str = datetime.now().strftime("%Y-%m-%d")
    week_str  = datetime.now().strftime("%Y-W%W")
    today_cnt = stat["daily"].get(today_str, 0)
    week_cnt  = stat["weekly"].get(week_str, 0)
    total_cnt = stat.get("total", 0)
    # Calculate rank in group
    all_stats = db["chat_stats"].get(chat_id, {})
    sorted_users = sorted(all_stats.items(), key=lambda x: x[1].get("total", 0), reverse=True)
    rank = next((i + 1 for i, (uid, _) in enumerate(sorted_users) if uid == user.id), "?")
    update.message.reply_text(
        f"{calypso_header('📊 ᴍʏ ꜱᴛᴀᴛꜱ')}"
        f"*ɴᴀᴍᴇ:* [{to_small_caps(user.first_name or str(user.id))}](tg://user?id={user.id})\n\n"
        f"📅 *ᴀᴀᴊ:* `{today_cnt}` ᴍꜱɢ\n"
        f"📆 *ɪꜱ ʜᴀꜰᴛᴇ:* `{week_cnt}` ᴍꜱɢ\n"
        f"🏆 *ᴛᴏᴛᴀʟ:* `{total_cnt}` ᴍꜱɢ\n"
        f"🥇 *ʀᴀɴᴋ:* `#{rank}`",
        parse_mode=ParseMode.MARKDOWN
    )


# ═══════════════════════════════════════════════════════
#  ADMIN: /slowmode
# ═══════════════════════════════════════════════════════

@admin_required
def slowmode_cmd(update: Update, context: CallbackContext):
    """/slowmode [seconds] — set slow mode. 0 to disable."""
    if not context.args or not context.args[0].isdigit():
        update.message.reply_text(to_small_caps("usage: /slowmode 10  ya  /slowmode 0 (disable)"))
        return
    secs = int(context.args[0])
    secs = min(secs, 3600)  # Telegram max is 3600
    try:
        context.bot.set_chat_slow_mode_delay(update.effective_chat.id, secs)
        if secs == 0:
            update.message.reply_text("⏱ " + to_small_caps("slow mode off kar diya."))
        else:
            update.message.reply_text(
                f"⏱ *ꜱʟᴏᴡ ᴍᴏᴅᴇ:* `{secs}s`",
                parse_mode=ParseMode.MARKDOWN
            )
    except Exception as e:
        update.message.reply_text(to_small_caps(f"error: {str(e)[:60]}"))


# ═══════════════════════════════════════════════════════
#  FUN & ENGAGEMENT FEATURES
# ═══════════════════════════════════════════════════════

# /ship — love percentage between two users
def ship_cmd(update: Update, context: CallbackContext):
    """/ship @user1 @user2 or reply or random — love percentage."""
    import hashlib
    msg = update.message
    chat = update.effective_chat
    chat_id = chat.id
    names = []
    ids   = []

    if msg.reply_to_message and msg.reply_to_message.from_user:
        u1 = msg.reply_to_message.from_user
        u2 = msg.from_user
        names = [u1.first_name or "someone", u2.first_name or "you"]
        ids   = [u1.id, u2.id]
    elif context.args:
        names = [a.lstrip("@") for a in context.args[:2]]
        if len(names) < 2:
            names.insert(0, msg.from_user.first_name or "you")
        ids = [0, 0]
    else:
        # Random ship from group user cache
        cached = list(db["user_cache"].get(chat_id, {}).items())
        if len(cached) >= 2:
            import random as _r2
            picks = _r2.sample(cached, 2)
            ids   = [picks[0][0], picks[1][0]]
            names = [picks[0][1].get("first_name", str(picks[0][0])),
                     picks[1][1].get("first_name", str(picks[1][0]))]
        else:
            update.message.reply_text(
                to_small_caps("usage: /ship @user1 @user2  ya kisi ke reply mein\n"
                              "ya bina kuch likhe random ship ke liye")
            )
            return

    seed_str = "".join(sorted([n.lower() for n in names]))
    pct = int(hashlib.md5(seed_str.encode()).hexdigest(), 16) % 101

    if pct >= 85:
        verdict = to_small_caps("soulmates confirmed 🔥")
        heart = "❤️"
    elif pct >= 65:
        verdict = to_small_caps("cute match hai yaar 💕")
        heart = "🩷"
    elif pct >= 45:
        verdict = to_small_caps("thodi mehnat aur karo")
        heart = "🤍"
    elif pct >= 25:
        verdict = to_small_caps("zyada umeed mat rakho 💀")
        heart = "🖤"
    else:
        verdict = to_small_caps("disaster hai bhai 😭")
        heart = "💔"

    n1 = to_small_caps(names[0][:18])
    n2 = to_small_caps(names[1][:18])

    update.message.reply_text(
        f"*{n1}* {heart} *{n2}*\n\n"
        f"*{pct}%*\n\n"
        f"⌁ {verdict}",
        parse_mode=ParseMode.MARKDOWN
    )


# /roast @user — AI roast
def roast_cmd(update: Update, context: CallbackContext):
    """/roast @user or reply — AI-generated savage roast."""
    msg = update.message
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target = msg.reply_to_message.from_user.first_name or "is banda"
    elif context.args:
        target = " ".join(context.args).lstrip("@")
    else:
        update.message.reply_text(to_small_caps("kisko roast karna hai? reply karo ya naam likho"))
        return
    prompt = f"'{target}' ko ek savage, funny Hinglish roast do — 2-3 lines mein. Desi style, brutal but funny."
    thinking = msg.reply_text(to_small_caps("roast prepare ho raha hai... 🔥"))
    roast = _call_groq_ai(prompt, 0)
    try:
        thinking.delete()
    except Exception:
        pass
    update.message.reply_text(f"🔥 *{to_small_caps(target)}* ke liye:\n\n{roast}", parse_mode=ParseMode.MARKDOWN)


# /truth and /dare
_TRUTHS = [
    to_small_caps("tera sabse embarrassing moment kya hai?"),
    to_small_caps("kabhi kisi ko dekh ke pyaar hua? kaun tha?"),
    to_small_caps("aaj tak ka sabse bada jhooth kya bola hai?"),
    to_small_caps("kisi ka number save hai tere phone mein jo unhe pata nahi?"),
    to_small_caps("teri worst habit kya hai jo koi nahi jaanta?"),
    to_small_caps("kabhi kisi ki baat sun ke khush tha baad mein guilty feel hua?"),
    to_small_caps("life mein sabse zyada kisne disappoint kiya?"),
    to_small_caps("teri fav app kaunsi hai jo dosto ko batate nahi?"),
]
_DARES = [
    to_small_caps("next 5 messages mein sirf emojis mein baat karo"),
    to_small_caps("apni profile pic 1 ghante ke liye kisi funny image se badlo"),
    to_small_caps("is group ke sabse old message ko react karo ❤️"),
    to_small_caps("kisi ek member ko apni dil ki baat bolo"),
    to_small_caps("abhi ek voice message bhejo aur koi gaana gao"),
    to_small_caps("apna WhatsApp status ek funny line likho — screenshot bhejo"),
    to_small_caps("group mein sabka naam mention karo aur ek compliment do"),
]

def truth_cmd(update: Update, context: CallbackContext):
    """/truth — random truth question."""
    q = _random.choice(_TRUTHS)
    update.message.reply_text(f"🎯 *ᴛʀᴜᴛʜ*\n\n{q}", parse_mode=ParseMode.MARKDOWN)

def dare_cmd(update: Update, context: CallbackContext):
    """/dare — random dare."""
    d = _random.choice(_DARES)
    update.message.reply_text(f"🎲 *ᴅᴀʀᴇ*\n\n{d}", parse_mode=ParseMode.MARKDOWN)


# /8ball [question]
_8BALL_ANSWERS = [
    to_small_caps("haan bilkul! 🔥"),
    to_small_caps("100% pakka! ✅"),
    to_small_caps("signs bata rahe hain haan 🌟"),
    to_small_caps("iske baare mein sure nahi hoon 🤔"),
    to_small_caps("dubara pooch bhai 😅"),
    to_small_caps("thodi der mein jawaab milega"),
    to_small_caps("nahi re, bilkul nahi 💀"),
    to_small_caps("chances bohot kam hain 😬"),
    to_small_caps("kabhi nahi bc 😂"),
    to_small_caps("tera future dark hai iss maamle mein 🖤"),
    to_small_caps("ho sakta hai, effort daalo"),
    to_small_caps("destiny tumhare haath mein hai"),
]

def eightball_cmd(update: Update, context: CallbackContext):
    """/8ball [question] — magic 8 ball."""
    if not context.args:
        update.message.reply_text(to_small_caps("koi sawaal toh pooch! /8ball kya main pass hounga?"))
        return
    q = " ".join(context.args)
    ans = _random.choice(_8BALL_ANSWERS)
    update.message.reply_text(
        f"🎱 *{to_small_caps(q[:100])}*\n\n⌁ {ans}",
        parse_mode=ParseMode.MARKDOWN
    )


# /roll and /flip
def roll_cmd(update: Update, context: CallbackContext):
    """/roll [max] — dice roll."""
    max_val = 6
    if context.args and context.args[0].isdigit():
        max_val = min(int(context.args[0]), 1000)
    result = _random.randint(1, max_val)
    update.message.reply_text(
        f"🎲 *{to_small_caps('dice roll')}* (1-{max_val})\n\n⌁ *{result}*",
        parse_mode=ParseMode.MARKDOWN
    )

def flip_cmd(update: Update, context: CallbackContext):
    """/flip — coin flip."""
    result = _random.choice(["heads 👑", "tails 🪙"])
    update.message.reply_text(
        f"🪙 *{to_small_caps('coin flip')}*\n\n⌁ *{to_small_caps(result)}*",
        parse_mode=ParseMode.MARKDOWN
    )


# /wiki [topic]
def wiki_cmd(update: Update, context: CallbackContext):
    """/wiki [topic] — Wikipedia summary."""
    if not context.args:
        update.message.reply_text(to_small_caps("usage: /wiki artificial intelligence"))
        return
    query = "+".join(context.args)
    try:
        import urllib.request as _ur
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{_urlparse.quote(query)}"
        req = _ur.Request(url, headers={"User-Agent": "CalypsoBot/1.0"})
        with _ur.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        title   = data.get("title", query)
        extract = data.get("extract", "")
        if not extract:
            update.message.reply_text(to_small_caps("koi article nahi mila, kuch aur try karo"))
            return
        # Trim to 3 sentences
        sentences = re.split(r'(?<=[.!?]) +', extract)
        summary = " ".join(sentences[:3])
        if len(summary) > 600:
            summary = summary[:597] + "…"
        wiki_url = data.get("content_urls", {}).get("desktop", {}).get("page", "")
        link_part = f"\n\n🔗 [ꜰᴜʟʟ ᴀʀᴛɪᴄʟᴇ]({wiki_url})" if wiki_url else ""
        update.message.reply_text(
            f"{calypso_header('📚 ' + to_small_caps(title))}"
            f"{summary}{link_part}",
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.warning(f"wiki_cmd error: {e}")
        update.message.reply_text(to_small_caps("wikipedia se data nahi aaya, baad mein try karo"))


# /define [word]
def define_cmd(update: Update, context: CallbackContext):
    """/define [word] — dictionary definition."""
    if not context.args:
        update.message.reply_text(to_small_caps("usage: /define serendipity"))
        return
    word = context.args[0].lower().strip()
    try:
        import urllib.request as _ur
        url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{_urlparse.quote(word)}"
        with _ur.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read())
        if not data or not isinstance(data, list):
            raise ValueError("no data")
        entry = data[0]
        phonetic = entry.get("phonetic", "")
        meanings = entry.get("meanings", [])
        if not meanings:
            raise ValueError("no meanings")
        lines = [f"{calypso_header('📖 ' + to_small_caps(word))}"]
        if phonetic:
            lines.append(f"*ꜰᴏɴᴇᴛɪᴄ:* `{phonetic}`\n")
        for m in meanings[:2]:
            pos = to_small_caps(m.get("partOfSpeech", ""))
            defs = m.get("definitions", [])
            if defs:
                d = defs[0].get("definition", "")
                ex = defs[0].get("example", "")
                lines.append(f"*{pos}:* {d}")
                if ex:
                    lines.append(f"_e.g. {ex}_")
        update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.warning(f"define_cmd error: {e}")
        update.message.reply_text(to_small_caps(f"'{word}' ka definition nahi mila"))


# /remind [time] [text]
_reminders: dict = {}   # {job_name: (chat_id, user_id, text)}

def remind_cmd(update: Update, context: CallbackContext):
    """/remind [time] [text] — set a reminder. e.g. /remind 30m meeting"""
    if len(context.args) < 2:
        update.message.reply_text(
            to_small_caps("usage: /remind 10m kuch kaam karna\n"
                          "time format: 30s, 10m, 2h, 1d")
        )
        return
    time_str = context.args[0].lower()
    text = " ".join(context.args[1:])
    # Parse time
    match = re.match(r"^(\d+)([smhd])$", time_str)
    if not match:
        update.message.reply_text(to_small_caps("galat format. use: 30s, 5m, 2h, 1d"))
        return
    amount, unit = int(match.group(1)), match.group(2)
    secs = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit] * amount
    if secs > 7 * 86400:
        update.message.reply_text(to_small_caps("max 7 din tak reminder set kar sakte ho"))
        return
    user = update.effective_user
    chat_id = update.effective_chat.id
    job_name = f"remind_{chat_id}_{user.id}_{int(time.time())}"
    def reminder_job(ctx: CallbackContext):
        jdata = ctx.job.context
        try:
            ctx.bot.send_message(
                jdata["chat_id"],
                f"⏰ [{to_small_caps(jdata['name'])}](tg://user?id={jdata['uid']}) "
                f"*ʀᴇᴍɪɴᴅᴇʀ:*\n{jdata['text']}",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass
    context.job_queue.run_once(
        reminder_job, secs,
        context={"chat_id": chat_id, "uid": user.id, "name": user.first_name or str(user.id), "text": text},
        name=job_name
    )
    # Human readable time
    if unit == "s": t_label = f"{amount} second"
    elif unit == "m": t_label = f"{amount} minute"
    elif unit == "h": t_label = f"{amount} ghante"
    else: t_label = f"{amount} din"
    update.message.reply_text(
        f"⏰ *ʀᴇᴍɪɴᴅᴇʀ ꜱᴇᴛ!*\n"
        f"*ᴛɪᴍᴇ:* {to_small_caps(t_label + ' mein')}\n"
        f"*ʏᴀᴀᴅ ᴅɪʟᴀᴏɢᴀ:* {text}",
        parse_mode=ParseMode.MARKDOWN
    )


# /poll [question] — quick poll
def poll_cmd(update: Update, context: CallbackContext):
    """/poll [question] | option1 | option2 ... — create a poll."""
    if not context.args:
        update.message.reply_text(
            to_small_caps("usage: /poll kya pakoda chahiye? | haan | nahi | maybe")
        )
        return
    full_text = " ".join(context.args)
    parts = [p.strip() for p in full_text.split("|") if p.strip()]
    if len(parts) < 3:
        update.message.reply_text(
            to_small_caps("kam se kam 1 sawaal aur 2 options chahiye:\n/poll sawaal | option1 | option2")
        )
        return
    question = parts[0]
    options  = parts[1:10]  # Telegram max 10
    try:
        context.bot.send_poll(
            chat_id=update.effective_chat.id,
            question=question,
            options=options,
            is_anonymous=False,
        )
    except Exception as e:
        update.message.reply_text(to_small_caps(f"poll nahi ban paya: {str(e)[:60]}"))



# ═══════════════════════════════════════════════════════
#  IMAGE GENERATION  🎨
# ═══════════════════════════════════════════════════════
# Uses Pollinations.ai (free, no API key needed)
# /image [prompt] — generate AI image from text
# ═══════════════════════════════════════════════════════

def image_cmd(update: Update, context: CallbackContext):
    """/image [prompt] — generate an AI image from text."""
    msg = update.message
    if not context.args:
        msg.reply_text(
            f"{calypso_header('🎨 ɪᴍᴀɢᴇ ɢᴇɴᴇʀᴀᴛᴏʀ')}"
            "ᴜꜱᴀɢᴇ: `/image a futuristic city at night`\n"
            "ʏᴀ ᴋᴜᴄʜ ʙʜɪ ʟɪᴋʜᴏ ᴀᴜʀ ᴀɪ ɪᴍᴀɢᴇ ʙᴀɴᴀ ᴅᴇɢᴀ! 🔥",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    prompt = " ".join(context.args)
    if len(prompt) > 300:
        prompt = prompt[:300]

    thinking = msg.reply_text(to_small_caps("image ban rahi hai... 🎨"))
    try:
        import urllib.request as _ur
        import urllib.parse as _up
        # Pollinations.ai — free image gen, no key needed
        encoded_prompt = _up.quote(prompt)
        # Add quality enhancers
        full_prompt = f"{prompt}, high quality, detailed, 4k"
        encoded_full = _up.quote(full_prompt)
        url = f"https://image.pollinations.ai/prompt/{encoded_full}?width=1024&height=1024&nologo=true"
        req = _ur.Request(url, headers={"User-Agent": "CalypsoBot/1.0"})
        with _ur.urlopen(req, timeout=30) as resp:
            img_data = resp.read()
        img_buf = io.BytesIO(img_data)
        img_buf.seek(0)
        try:
            thinking.delete()
        except Exception:
            pass
        msg.reply_photo(
            photo=img_buf,
            caption=f"🎨 *{to_small_caps(prompt[:80])}*",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.warning(f"image_cmd error: {e}")
        try:
            thinking.delete()
        except Exception:
            pass
        msg.reply_text(to_small_caps("image nahi ban payi yaar, dobara try karo 😅"))


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
    "quote":        lambda u, c: quote_cmd(u, c),
    "ai":           lambda u, c: ai_cmd(u, c),
    "approval":     lambda u, c: approval_cmd(u, c),
    "approve":      lambda u, c: approve_cmd(u, c),
    "unapprove":    lambda u, c: unapprove_cmd(u, c),
    "approved":     lambda u, c: approved_list_cmd(u, c),
    "report":       lambda u, c: report_cmd(u, c),
    "afk":          lambda u, c: afk_cmd(u, c),
    "warnreasons":  lambda u, c: warnreasons_cmd(u, c),
    "calc":         lambda u, c: calc_cmd(u, c),
    "time":         lambda u, c: time_cmd(u, c),
    "tr":           lambda u, c: tr_cmd(u, c),
    "weather":      lambda u, c: weather_cmd(u, c),
    "fun":          lambda u, c: fun_cmd(u, c),
    "kiss":         lambda u, c: kiss_cmd(u, c),
    "slap":         lambda u, c: slap_cmd(u, c),
    "hug":          lambda u, c: hug_cmd(u, c),
    "pat":          lambda u, c: pat_cmd(u, c),
    "punch":        lambda u, c: punch_cmd(u, c),
    "cuddle":       lambda u, c: cuddle_cmd(u, c),
    "poke":         lambda u, c: poke_cmd(u, c),
    "highfive":     lambda u, c: highfive_cmd(u, c),
    "antilink":     lambda u, c: antilink_cmd(u, c),
    "antiforward":  lambda u, c: antiforward_cmd(u, c),
    "mystats":      lambda u, c: mystats_cmd(u, c),
    "slowmode":     lambda u, c: slowmode_cmd(u, c),
    "ship":         lambda u, c: ship_cmd(u, c),
    "roast":        lambda u, c: roast_cmd(u, c),
    "truth":        lambda u, c: truth_cmd(u, c),
    "dare":         lambda u, c: dare_cmd(u, c),
    "8ball":        lambda u, c: eightball_cmd(u, c),
    "roll":         lambda u, c: roll_cmd(u, c),
    "flip":         lambda u, c: flip_cmd(u, c),
    "wiki":         lambda u, c: wiki_cmd(u, c),
    "define":       lambda u, c: define_cmd(u, c),
    "remind":       lambda u, c: remind_cmd(u, c),
    "poll":         lambda u, c: poll_cmd(u, c),
    "image":        lambda u, c: image_cmd(u, c),
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
    dp.add_handler(CommandHandler("quote",   quote_cmd))

    # ── New features ────────────────────────────────────
    dp.add_handler(CommandHandler("ai",          ai_cmd))
    dp.add_handler(CommandHandler("approval",    approval_cmd))
    dp.add_handler(CommandHandler("approve",     approve_cmd))
    dp.add_handler(CommandHandler("unapprove",   unapprove_cmd))
    dp.add_handler(CommandHandler("approved",    approved_list_cmd))
    # Group tools
    dp.add_handler(CommandHandler("report",      report_cmd))
    dp.add_handler(CommandHandler("afk",         afk_cmd))
    dp.add_handler(CommandHandler("warnreasons", warnreasons_cmd))
    # Utility
    dp.add_handler(CommandHandler("calc",        calc_cmd))
    dp.add_handler(CommandHandler("time",        time_cmd))
    dp.add_handler(CommandHandler("tr",          tr_cmd))
    dp.add_handler(CommandHandler("weather",     weather_cmd))
    dp.add_handler(CommandHandler("fun",      fun_cmd))
    dp.add_handler(CommandHandler("kiss",     kiss_cmd))
    dp.add_handler(CommandHandler("slap",     slap_cmd))
    dp.add_handler(CommandHandler("hug",      hug_cmd))
    dp.add_handler(CommandHandler("pat",      pat_cmd))
    dp.add_handler(CommandHandler("punch",    punch_cmd))
    dp.add_handler(CommandHandler("cuddle",   cuddle_cmd))
    dp.add_handler(CommandHandler("poke",     poke_cmd))
    dp.add_handler(CommandHandler("highfive", highfive_cmd))

    # ── New features v2 ─────────────────────────────────
    dp.add_handler(CommandHandler("antilink",    antilink_cmd))
    dp.add_handler(CommandHandler("antiforward", antiforward_cmd))
    dp.add_handler(CommandHandler("mystats",     mystats_cmd))
    dp.add_handler(CommandHandler("slowmode",    slowmode_cmd))
    dp.add_handler(CommandHandler("ship",        ship_cmd))
    dp.add_handler(CommandHandler("roast",       roast_cmd))
    dp.add_handler(CommandHandler("truth",       truth_cmd))
    dp.add_handler(CommandHandler("dare",        dare_cmd))
    dp.add_handler(CommandHandler("8ball",       eightball_cmd))
    dp.add_handler(CommandHandler("roll",        roll_cmd))
    dp.add_handler(CommandHandler("flip",        flip_cmd))
    dp.add_handler(CommandHandler("wiki",        wiki_cmd))
    dp.add_handler(CommandHandler("define",      define_cmd))
    dp.add_handler(CommandHandler("remind",      remind_cmd))
    dp.add_handler(CommandHandler("poll",        poll_cmd))
    dp.add_handler(CommandHandler("image",       image_cmd))

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
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, ai_message_handler), group=6)
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, afk_watcher), group=7)
    dp.add_handler(MessageHandler(Filters.all & ~Filters.command & ~Filters.private, check_antilink_antiforward), group=8)

    # ── Join request handler ─────────────────────────────
    from telegram.ext import TypeHandler
    dp.add_handler(TypeHandler(type=Update, callback=lambda u, c: handle_join_request(u, c) if u.chat_join_request else None), group=9)

    # Callbacks
    dp.add_handler(CallbackQueryHandler(help_callback, pattern="^help_"))
    dp.add_handler(CallbackQueryHandler(captcha_callback, pattern="^captcha_"))
    dp.add_handler(CallbackQueryHandler(ranking_callback, pattern="^rank_"))
    dp.add_handler(CallbackQueryHandler(join_request_callback, pattern="^jr_"))
    dp.add_handler(CallbackQueryHandler(action_callback, pattern="^action_"))

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
