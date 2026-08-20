import asyncio
import os
import secrets
import sqlite3
from pathlib import Path
from html import escape
from urllib.parse import quote

from dotenv import load_dotenv
from aiohttp import web, ClientSession, ClientTimeout

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand,
)
from aiogram.client.session.aiohttp import AiohttpSession


# =========================================================
# CONFIG
# =========================================================

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PROXY_URL = os.getenv("PROXY_URL", "").strip()
BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8080").strip().rstrip("/")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
JOIN_CHANNEL = os.getenv("JOIN_CHANNEL", "@THEASYLUM2").strip()

ROOT_ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
# Root admin fallback for the owner account. ADMIN_IDS in .env still takes precedence.
ROOT_ADMIN_IDS.add(7692023421)

DB_PATH = Path("/data/uploader.db") if Path("/data").exists() else (BASE_DIR / "uploader.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN در فایل .env پیدا نشد.")

session = AiohttpSession(
    proxy=PROXY_URL or None,
    timeout=30,
)
bot = Bot(BOT_TOKEN, session=session)
dp = Dispatcher()

# Runtime-only state. A restart intentionally clears unfinished operations.
upload_modes = {}       # uid -> "single" | "group"
upload_items = {}       # uid -> list[item]
admin_actions = {}      # uid -> action
broadcast_lock = asyncio.Lock()


# =========================================================
# DATABASE
# =========================================================

def db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS blocked_users(
        user_id INTEGER PRIMARY KEY,
        blocked_by INTEGER,
        reason TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS admins(
        user_id INTEGER PRIMARY KEY,
        added_by INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS user_settings(
        user_id INTEGER PRIMARY KEY,
        language TEXT DEFAULT 'fa',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS bot_settings(
        key TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE TABLE IF NOT EXISTS join_channels(
        channel_id TEXT PRIMARY KEY,
        title TEXT,
        username TEXT,
        invite_url TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS files(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token TEXT UNIQUE NOT NULL,
        owner_id INTEGER NOT NULL,
        file_id TEXT,
        file_name TEXT,
        file_size INTEGER DEFAULT 0,
        file_type TEXT,
        text_content TEXT,
        downloads INTEGER DEFAULT 0,
        active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS groups(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token TEXT UNIQUE NOT NULL,
        owner_id INTEGER NOT NULL,
        title TEXT,
        downloads INTEGER DEFAULT 0,
        active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS group_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        position INTEGER NOT NULL,
        file_id TEXT,
        file_name TEXT,
        file_size INTEGER DEFAULT 0,
        file_type TEXT,
        text_content TEXT,
        FOREIGN KEY(group_id) REFERENCES groups(id) ON DELETE CASCADE
    );
    """)

    # Existing DB migration.
    try:
        c.execute("ALTER TABLE join_channels ADD COLUMN invite_url TEXT")
    except sqlite3.OperationalError:
        pass

    c.execute("""
        INSERT OR IGNORE INTO bot_settings(key, value)
        VALUES('storage_photos', '0')
    """)

    c.execute("""
        INSERT OR IGNORE INTO bot_settings(key, value)
        VALUES('bot_enabled', '1')
    """)

    c.execute("""
        INSERT OR IGNORE INTO bot_settings(key, value)
        VALUES('auto_delete_20', '0')
    """)

    c.execute("""
        INSERT OR IGNORE INTO bot_settings(key, value)
        VALUES('force_join_enabled', '1')
    """)

    c.execute("""
        INSERT OR IGNORE INTO bot_settings(key, value)
        VALUES('start_text', '👋 خوش آمدی!

🤖 به ربات آپلود فایل خوش آمدی.')
    """)

    if JOIN_CHANNEL:
        # Keep the configured channel authoritative; remove the old default
        # from previous versions so users are not unexpectedly forced into it.
        if JOIN_CHANNEL != "@eldnv":
            c.execute("DELETE FROM join_channels WHERE channel_id='@eldnv'")
        c.execute("""
            INSERT OR IGNORE INTO join_channels
            (channel_id, title, username, invite_url)
            VALUES(?,?,?,?)
        """, (
            JOIN_CHANNEL,
            JOIN_CHANNEL,
            JOIN_CHANNEL if JOIN_CHANNEL.startswith("@") else "",
            "",
        ))

    for aid in ROOT_ADMIN_IDS:
        c.execute("""
            INSERT OR IGNORE INTO admins(user_id, added_by)
            VALUES(?,?)
        """, (aid, aid))

    c.commit()
    c.close()


def register_user(user_id, username, first_name):
    c = db()
    c.execute("""
        INSERT INTO users(user_id, username, first_name)
        VALUES(?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name
    """, (user_id, username, first_name))
    c.execute("""
        INSERT OR IGNORE INTO user_settings(user_id, language)
        VALUES(?, 'fa')
    """, (user_id,))
    c.commit()
    c.close()


def setting(key, default=""):
    c = db()
    r = c.execute(
        "SELECT value FROM bot_settings WHERE key=?",
        (key,),
    ).fetchone()
    c.close()
    return r["value"] if r else default


def set_setting(key, value):
    c = db()
    c.execute("""
        INSERT INTO bot_settings(key, value)
        VALUES(?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, str(value)))
    c.commit()
    c.close()


def bot_is_enabled():
    return setting("bot_enabled", "1") == "1"


def bot_status_text():
    return "روشن 🟢" if bot_is_enabled() else "خاموش 🔴"


def is_root(uid):
    return uid in ROOT_ADMIN_IDS


def is_admin(uid):
    if uid in ROOT_ADMIN_IDS:
        return True
    c = db()
    r = c.execute(
        "SELECT 1 FROM admins WHERE user_id=?",
        (uid,),
    ).fetchone()
    c.close()
    return bool(r)


def is_blocked(uid):
    if uid in ROOT_ADMIN_IDS:
        return False
    c = db()
    r = c.execute(
        "SELECT 1 FROM blocked_users WHERE user_id=?",
        (uid,),
    ).fetchone()
    c.close()
    return bool(r)


# =========================================================
# ADMIN
# =========================================================

def add_admin(uid, by):
    if uid in ROOT_ADMIN_IDS:
        return False, "این کاربر ادمین اصلی است."
    c = db()
    try:
        c.execute(
            "INSERT INTO admins(user_id, added_by) VALUES(?,?)",
            (uid, by),
        )
        c.commit()
        return True, "ادمین اضافه شد."
    except sqlite3.IntegrityError:
        return False, "این کاربر از قبل ادمین است."
    finally:
        c.close()


def remove_admin(uid):
    if uid in ROOT_ADMIN_IDS:
        return False, "ادمین اصلی قابل حذف نیست."
    c = db()
    cur = c.execute("DELETE FROM admins WHERE user_id=?", (uid,))
    c.commit()
    ok = cur.rowcount > 0
    c.close()
    return (True, "ادمین حذف شد.") if ok else (False, "این کاربر ادمین نیست.")


def block_user(uid, by):
    if uid in ROOT_ADMIN_IDS or is_admin(uid):
        return False, "ادمین‌ها قابل مسدودسازی نیستند."
    c = db()
    c.execute("""
        INSERT OR REPLACE INTO blocked_users(user_id, blocked_by)
        VALUES(?,?)
    """, (uid, by))
    c.commit()
    c.close()
    return True, "کاربر مسدود شد."


def unblock_user(uid):
    c = db()
    cur = c.execute("DELETE FROM blocked_users WHERE user_id=?", (uid,))
    c.commit()
    ok = cur.rowcount > 0
    c.close()
    return (True, "مسدودی برداشته شد.") if ok else (False, "کاربر مسدود نیست.")


# =========================================================
# FORCE JOIN
# =========================================================

def list_join_channels():
    c = db()
    rows = c.execute("""
        SELECT * FROM join_channels ORDER BY created_at
    """).fetchall()
    c.close()
    return rows


def add_join_channel(cid, title="", username="", invite_url=""):
    c = db()
    try:
        c.execute("""
            INSERT INTO join_channels(channel_id, title, username, invite_url)
            VALUES(?,?,?,?)
        """, (str(cid), title or str(cid), username or "", invite_url or ""))
        c.commit()
        return True, "کانال اضافه شد."
    except sqlite3.IntegrityError:
        return False, "این کانال از قبل وجود دارد."
    finally:
        c.close()


def remove_join_channel(cid):
    c = db()
    cur = c.execute("DELETE FROM join_channels WHERE channel_id=?", (str(cid),))
    c.commit()
    ok = cur.rowcount > 0
    c.close()
    return (True, "کانال حذف شد.") if ok else (False, "کانال پیدا نشد.")


def join_keyboard():
    rows = []
    for r in list_join_channels():
        url = ""
        if (r["username"] or "").startswith("@"):
            url = "https://t.me/" + r["username"][1:]
        elif r["invite_url"]:
            url = r["invite_url"]
        if url:
            rows.append([InlineKeyboardButton(
                text=f"📢 عضویت در {r['title']}",
                url=url,
            )])
    rows.append([InlineKeyboardButton(
        text="🔄 بررسی عضویت",
        callback_data="check_join",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def check_membership(uid):
    if setting("force_join_enabled", "1") != "1":
        return True

    channels = list_join_channels()
    if not channels:
        return True

    for r in channels:
        try:
            member = await bot.get_chat_member(r["channel_id"], uid)
            if member.status in ("creator", "administrator", "member"):
                continue
            if member.status == "restricted" and getattr(member, "is_member", False):
                continue
            return False
        except Exception as e:
            print("MEMBERSHIP ERROR:", r["channel_id"], repr(e))
            return False
    return True


# =========================================================
# FILE DATABASE
# =========================================================

def new_token():
    return secrets.token_urlsafe(12)


def create_file(owner_id, item):
    c = db()
    try:
        for _ in range(5):
            token = new_token()
            try:
                c.execute("""
                    INSERT INTO files(
                        token, owner_id, file_id, file_name,
                        file_size, file_type, text_content
                    )
                    VALUES(?,?,?,?,?,?,?)
                """, (
                    token, owner_id, item.get("file_id"),
                    item.get("file_name"), item.get("file_size", 0),
                    item.get("file_type"), item.get("text_content"),
                ))
                c.commit()
                return token
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError("ساخت توکن یکتا ناموفق بود.")
    finally:
        c.close()


def get_file(token):
    c = db()
    r = c.execute("""
        SELECT * FROM files
        WHERE token=? AND active=1
    """, (token,)).fetchone()
    c.close()
    return r


def get_user_files(uid):
    c = db()
    rows = c.execute("""
        SELECT * FROM files
        WHERE owner_id=? AND active=1
        ORDER BY id DESC
    """, (uid,)).fetchall()
    c.close()
    return rows


def deactivate_file(uid, token):
    c = db()
    cur = c.execute("""
        UPDATE files
        SET active=0
        WHERE token=? AND owner_id=? AND active=1
    """, (token, uid))
    c.commit()
    ok = cur.rowcount > 0
    c.close()
    return ok

def rename_file(uid, token, new_name):
    c = db()
    cur = c.execute("""
        UPDATE files SET file_name=?
        WHERE token=? AND owner_id=? AND active=1
    """, (new_name, token, uid))
    c.commit()
    ok = cur.rowcount > 0
    c.close()
    return ok


def deactivate_group(uid, token):
    c = db()
    cur = c.execute("""
        UPDATE groups
        SET active=0
        WHERE token=? AND owner_id=? AND active=1
    """, (token, uid))
    c.commit()
    ok = cur.rowcount > 0
    c.close()
    return ok


def create_group(uid, title, items):
    c = db()
    try:
        for _ in range(5):
            token = new_token()
            try:
                cur = c.execute("""
                    INSERT INTO groups(token, owner_id, title)
                    VALUES(?,?,?)
                """, (token, uid, title or "مجموعه فایل"))
                gid = cur.lastrowid

                for position, item in enumerate(items, 1):
                    c.execute("""
                        INSERT INTO group_items(
                            group_id, position, file_id, file_name,
                            file_size, file_type, text_content
                        )
                        VALUES(?,?,?,?,?,?,?)
                    """, (
                        gid, position, item.get("file_id"),
                        item.get("file_name"), item.get("file_size", 0),
                        item.get("file_type"), item.get("text_content"),
                    ))
                c.commit()
                return token
            except sqlite3.IntegrityError:
                c.rollback()
        raise RuntimeError("ساخت توکن گروه ناموفق بود.")
    finally:
        c.close()


def get_group(token):
    c = db()
    group = c.execute("""
        SELECT * FROM groups
        WHERE token=? AND active=1
    """, (token,)).fetchone()
    if not group:
        c.close()
        return None, []
    items = c.execute("""
        SELECT * FROM group_items
        WHERE group_id=? ORDER BY position
    """, (group["id"],)).fetchall()
    c.close()
    return group, items


def increment_download(token):
    c = db()
    c.execute(
        "UPDATE files SET downloads=downloads+1 WHERE token=? AND active=1",
        (token,),
    )
    c.commit()
    c.close()


def increment_group_download(token):
    c = db()
    c.execute(
        "UPDATE groups SET downloads=downloads+1 WHERE token=? AND active=1",
        (token,),
    )
    c.commit()
    c.close()


# =========================================================
# HELPERS
# =========================================================

def fmt_size(n):
    if not n:
        return "0 B"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"


async def bot_username():
    return (await bot.get_me()).username


def tg_link(username, kind, token):
    return f"https://t.me/{username}?start={kind}_{token}"


def clear_user_state(uid):
    admin_actions.pop(uid, None)
    upload_modes.pop(uid, None)
    upload_items.pop(uid, None)


# =========================================================
# KEYBOARDS / UI STYLE
# =========================================================

# Telegram Bot API now supports predefined button styles:
# primary (blue), success (green), danger (red).
# The compatibility fallback keeps the bot working on older aiogram builds.
def styled_button(text, style=None):
    try:
        return KeyboardButton(text=text, style=style) if style else KeyboardButton(text=text)
    except TypeError:
        return KeyboardButton(text=text)


def styled_inline_button(text, *, style=None, callback_data=None, url=None):
    kwargs = {"text": text}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    if style:
        kwargs["style"] = style
    try:
        return InlineKeyboardButton(**kwargs)
    except TypeError:
        kwargs.pop("style", None)
        return InlineKeyboardButton(**kwargs)


LANGUAGES = {
    "fa": {
        "name": "🇮🇷 فارسی",
        "upload": "⬆️ آپلود فایل",
        "group": "📂 آپلود گروهی",
        "files": "📊 مشاهده فایل‌ها و آمار",
        "broadcast": "📣 ارسال پیام همگانی",
        "toggle_on": "🔴 خاموش کردن ربات",
        "toggle_off": "🟢 روشن کردن ربات",
        "settings": "⚙️ تنظیمات",
        "back": "🏠 بازگشت به منوی اصلی",
        "admins": "👑 مدیریت ادمین‌ها",
        "blocks": "🚫 مدیریت مسدودی",
        "start_view": "👀 مشاهده استارت از دید کاربر",
        "users": "👥 لیست کاربران",
        "blocked_list": "📋 لیست مسدودها",
        "admin_list": "👑 لیست ادمین‌ها",
        "add_admin": "➕ افزودن ادمین",
        "remove_admin": "➖ حذف ادمین",
        "force": "🔐 عضویت اجباری",
        "stats": "📊 آمار کلی",
        "file_settings": "📁 تنظیمات فایل‌ها",
    }
}

def L(uid, key):
    return LANGUAGES["fa"][key]


def localized_label_map():
    d = LANGUAGES["fa"]
    keys = {
        "upload":"upload", "group":"group", "files":"files",
        "broadcast":"broadcast", "toggle_on":"toggle_on",
        "toggle_off":"toggle_off", "settings":"settings", "back":"back",
        "admins":"admins", "blocks":"blocks", "start_view":"start_view",
        "users":"users", "blocked_list":"blocked_list", "admin_list":"admin_list",
        "add_admin":"add_admin", "remove_admin":"remove_admin",
        "force":"force", "stats":"stats", "file_settings":"file_settings"
    }
    return {d[key]: canonical for key, canonical in keys.items()}


def main_keyboard(uid=None):
    uid = uid or next(iter(ROOT_ADMIN_IDS), 0)
    enabled = bot_is_enabled()
    d=LANGUAGES["fa"]
    toggle_text=d["toggle_on"] if enabled else d["toggle_off"]
    return ReplyKeyboardMarkup(keyboard=[
        [styled_button(d["upload"],"success"), styled_button(d["group"],"success")],
        [styled_button(d["files"],"primary"), styled_button(d["broadcast"],"primary")],
        [styled_button(toggle_text,"danger" if enabled else "success"), styled_button(d["settings"],"primary")],
    ], resize_keyboard=True, is_persistent=True)


def upload_success_keyboard(bot_url, web_url, token):
    share_url=f"https://t.me/share/url?url={quote(bot_url,safe='')}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [styled_inline_button("📁 مشاهده فایل‌ها",style="primary",callback_data="ui_files"),styled_inline_button("➕ افزودن فایل",style="success",callback_data="ui_upload")],
        [styled_inline_button("🤖 دریافت در ربات",style="primary",url=bot_url),styled_inline_button("🌐 لینک وب",style="primary",url=web_url)],
        [styled_inline_button("📤 اشتراک‌گذاری",style="primary",url=share_url)],
        [styled_inline_button("✏️ ویرایش نام",style="primary",callback_data=f"rename_file:{token}"),styled_inline_button("🗑 حذف فایل",style="danger",callback_data=f"delete_file:{token}")],
    ])


def group_success_keyboard(bot_url, web_url, token):
    share_url=f"https://t.me/share/url?url={quote(bot_url,safe='')}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [styled_inline_button("📁 مشاهده فایل‌ها",style="primary",callback_data="ui_files"),styled_inline_button("➕ افزودن فایل",style="success",callback_data=f"group_add:{token}")],
        [styled_inline_button("🤖 دریافت در ربات",style="primary",url=bot_url),styled_inline_button("🌐 لینک وب",style="primary",url=web_url)],
        [styled_inline_button("📤 اشتراک‌گذاری",style="primary",url=share_url)],
        [styled_inline_button("🗑 حذف مجموعه",style="danger",callback_data=f"delete_group:{token}")],
    ])


def group_upload_keyboard():
    return ReplyKeyboardMarkup(keyboard=[[styled_button("❌ انصراف","danger"),styled_button("✅ پایان","success")]],resize_keyboard=True,is_persistent=True)


def settings_keyboard(uid=None):
    # Keep the original compact settings menu. Admin/block sub-actions are
    # intentionally opened inside their own management menus.
    uid=uid or next(iter(ROOT_ADMIN_IDS),0)
    d=LANGUAGES["fa"]
    return ReplyKeyboardMarkup(
        keyboard=[
            [styled_button(d["admins"],"primary"), styled_button(d["blocks"],"danger")],
            [styled_button(d["users"],"primary"), styled_button(d["force"],"primary")],
            [styled_button(d["stats"],"primary"), styled_button(d["file_settings"],"primary")],
            [styled_button(d["broadcast"],"success")],
            [styled_button(d["start_view"],"primary")],
            [styled_button(f"🗑 حذف خودکار ۲۰ ثانیه‌ای: {'روشن 🟢' if setting('auto_delete_20','0') == '1' else 'خاموش 🔴'}", "danger" if setting('auto_delete_20','0') == '1' else "primary")],
            [styled_button(d["back"],"primary")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def file_settings_keyboard():
    status = "روشن 🟢" if setting("storage_photos", "0") == "1" else "خاموش 🔴"
    return ReplyKeyboardMarkup(
        keyboard=[
            [styled_button("📦 کانال ذخیره‌سازی", "primary")],
            [styled_button(f"🖼 ذخیره عکس‌ها: {status}", "success" if status.startswith("روشن") else "danger")],
            [styled_button("🌐 تنظیمات لینک وب", "primary")],
            [styled_button("🔙 بازگشت به تنظیمات", "primary")],
            [styled_button("🏠 منوی اصلی", "primary")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def join_manage_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [styled_button("➕ افزودن کانال", "success"), styled_button("🗑 حذف کانال", "danger")],
            [styled_button("📋 لیست کانال‌ها", "primary"), styled_button("🧪 تست کانال‌ها", "🔐 فعال/غیرفعال کردن عضویت اجباری", "primary")],
            [styled_button("🔐 فعال/غیرفعال کردن عضویت اجباری", "primary")],
            [styled_button("🔙 بازگشت به تنظیمات", "primary")],
            [styled_button("🏠 منوی اصلی", "primary")],
        ],
        resize_keyboard=True,
    )


# =========================================================
# STORAGE
# =========================================================

async def maybe_storage_copy(message: Message):
    raw = setting("storage_channel_id", "")
    if not raw:
        return
    if message.photo and setting("storage_photos", "0") != "1":
        return

    try:
        target = int(raw) if str(raw).lstrip("-").isdigit() else raw
        await bot.copy_message(
            chat_id=target,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except Exception as e:
        print("STORAGE COPY ERROR:", repr(e))


# =========================================================
# EXTRACT / SEND
# =========================================================

def extract_item(message: Message):
    if message.document:
        x = message.document
        return {
            "file_id": x.file_id,
            "file_name": x.file_name or "file",
            "file_size": x.file_size or 0,
            "file_type": "document",
            "text_content": None,
        }

    if message.photo:
        x = message.photo[-1]
        return {
            "file_id": x.file_id,
            "file_name": "photo.jpg",
            "file_size": x.file_size or 0,
            "file_type": "photo",
            "text_content": None,
        }

    if message.video:
        x = message.video
        return {
            "file_id": x.file_id,
            "file_name": x.file_name or "video.mp4",
            "file_size": x.file_size or 0,
            "file_type": "video",
            "text_content": None,
        }

    if message.audio:
        x = message.audio
        return {
            "file_id": x.file_id,
            "file_name": x.file_name or "audio.mp3",
            "file_size": x.file_size or 0,
            "file_type": "audio",
            "text_content": None,
        }

    if message.voice:
        return {
            "file_id": message.voice.file_id,
            "file_name": "voice.ogg",
            "file_size": message.voice.file_size or 0,
            "file_type": "voice",
            "text_content": None,
        }

    if message.animation:
        x = message.animation
        return {
            "file_id": x.file_id,
            "file_name": x.file_name or "animation.gif",
            "file_size": x.file_size or 0,
            "file_type": "animation",
            "text_content": None,
        }

    if message.sticker:
        x = message.sticker
        if getattr(x, "is_video", False):
            file_name = "sticker.webm"
        elif getattr(x, "is_animated", False):
            file_name = "sticker.tgs"
        else:
            file_name = "sticker.webp"
        return {
            "file_id": x.file_id,
            "file_name": file_name,
            "file_size": x.file_size or 0,
            "file_type": "sticker",
            "text_content": None,
            "sticker_animated": bool(getattr(x, "is_animated", False)),
            "sticker_video": bool(getattr(x, "is_video", False)),
            "sticker_emoji": getattr(x, "emoji", None),
        }

    if message.text and not message.text.startswith("/"):
        return {
            "file_id": None,
            "file_name": "متن",
            "file_size": len(message.text.encode("utf-8")),
            "file_type": "text",
            "text_content": message.text,
        }

    return None


async def send_item(chat_id, item):
    t = item["file_type"]
    fid = item["file_id"]

    if t == "text":
        return await bot.send_message(
            chat_id,
            item.get("text_content") or "",
        )
    if t == "document":
        return await bot.send_document(chat_id, fid)
    if t == "photo":
        return await bot.send_photo(chat_id, fid)
    if t == "video":
        return await bot.send_video(chat_id, fid)
    if t == "audio":
        return await bot.send_audio(chat_id, fid)
    if t == "voice":
        return await bot.send_voice(chat_id, fid)
    if t == "animation":
        return await bot.send_animation(chat_id, fid)
    if t == "sticker":
        return await bot.send_sticker(chat_id, fid)
    raise ValueError(f"Unsupported file type: {t}")


# =========================================================
# AUTO DELETE
# =========================================================

async def _delete_later(chat_id, message_ids, delay=20):
    await asyncio.sleep(delay)
    for mid in message_ids:
        try:
            await bot.delete_message(chat_id, mid)
        except Exception as e:
            print("AUTO DELETE FAILED:", repr(e))

async def auto_delete_sent_messages(chat_id, sent_messages):
    if setting("auto_delete_20", "0") != "1":
        return
    ids = [m.message_id for m in sent_messages if m is not None]
    if not ids:
        return
    try:
        warning = await bot.send_message(
            chat_id,
            "⚠️ این فایل را در Saved Messages ذخیره کن؛ ۲۰ ثانیه دیگر پیام حذف می‌شود.",
        )
        ids.append(warning.message_id)
    except Exception as e:
        print("AUTO DELETE WARNING ERROR:", repr(e))
    asyncio.create_task(_delete_later(chat_id, ids, 20))

# =========================================================
# UPLOAD PROCESS
# =========================================================

async def process_upload(message: Message):
    uid = message.from_user.id

    if not is_admin(uid):
        return await message.answer("⛔ فقط ادمین‌ها اجازه آپلود دارند.")

    item = extract_item(message)
    if not item:
        return await message.answer("❌ این نوع محتوا قابل آپلود نیست.")

    mode = upload_modes.get(uid)

    if mode == "group":
        upload_items.setdefault(uid, []).append(item)
        await maybe_storage_copy(message)
        count = len(upload_items[uid])
        return await message.answer(
            f"✅ آیتم <b>{count}</b> اضافه شد.\n\n"
            f"📁 {escape(item['file_name'] or 'file')}\n"
            f"💾 {fmt_size(item['file_size'])}\n\n"
            "📤 فایل بعدی را بفرست.\n"
            "وقتی تمام شد روی «✅ پایان» بزن.",
            parse_mode="HTML",
        )

    if mode == "single":
        try:
            await maybe_storage_copy(message)
            token = create_file(uid, item)
            username = await bot_username()
            bot_url = tg_link(username, "file", token)
            web_url = f"{BASE_URL}/f/{token}"
            clear_user_state(uid)
            share_url = f"https://t.me/share/url?url={quote(bot_url, safe='')}"

            return await message.answer(
                "╭─────── ✨ ───────╮\n"
                "│  <b>آپلود با موفقیت انجام شد!</b>  │\n"
                "╰──────────────────╯\n\n"
                f"📄 <b>{escape(item['file_name'] or 'file')}</b>\n"
                f"💾 حجم: <b>{fmt_size(item['file_size'])}</b>\n"
                f"🔐 شناسه: <code>{escape(token)}</code>\n"
                f"🔗 <code>{escape(bot_url)}</code>\n\n"
                "یکی از گزینه‌های زیر را انتخاب کن:",
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=upload_success_keyboard(bot_url, web_url, token),
            )
        except Exception as e:
            print("UPLOAD ERROR:", repr(e))
            return await message.answer(
                f"❌ ذخیره فایل انجام نشد.\n\n<code>{escape(str(e))}</code>",
                parse_mode="HTML",
            )

    return await message.answer("ℹ️ حالت آپلود فعال نیست.", reply_markup=main_keyboard())


@dp.callback_query(F.data == "ui_files")
async def upload_ui_files(callback: CallbackQuery):
    await callback.answer()
    await my_files(callback.message)


@dp.callback_query(F.data == "ui_upload")
async def upload_ui_upload(callback: CallbackQuery):
    await callback.answer()
    await upload_single(callback.message)


@dp.callback_query(F.data.startswith("rename_file:"))
async def rename_uploaded_file(callback: CallbackQuery):
    uid=callback.from_user.id
    if not is_admin(uid): return await callback.answer("⛔ دسترسی ندارید.",show_alert=True)
    token=callback.data.split(":",1)[1]
    if not get_file(token): return await callback.answer("❌ فایل پیدا نشد.",show_alert=True)
    admin_actions[uid]=f"rename_file:{token}"
    await callback.answer()
    await callback.message.answer("✏️ نام جدید فایل را بفرست.\nلغو: /cancel")

@dp.callback_query(F.data.startswith("delete_file:"))
async def delete_uploaded_file(callback: CallbackQuery):
    token = callback.data.split(":", 1)[1]
    uid = callback.from_user.id
    if not is_admin(uid):
        return await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
    if not deactivate_file(uid, token):
        return await callback.answer("❌ فایل پیدا نشد یا قبلاً حذف شده.", show_alert=True)
    await callback.answer("🗑 فایل حذف شد.", show_alert=False)
    try:
        await callback.message.edit_text(
            "🗑 <b>فایل حذف شد.</b>\n\n"
            "فایل از لیست فایل‌های فعال شما خارج شد.",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
    except Exception:
        await callback.message.answer("🗑 فایل حذف شد.", reply_markup=main_keyboard())


@dp.callback_query(F.data.startswith("group_add:"))
async def group_add_file_callback(callback: CallbackQuery):
    uid = callback.from_user.id
    if not is_admin(uid):
        return await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
    admin_actions.pop(uid, None)
    upload_modes[uid] = "group"
    upload_items[uid] = []
    await callback.answer("➕ حالت افزودن فایل فعال شد.")
    await callback.message.answer(
        "📦 <b>افزودن فایل به مجموعه</b>\n\n"
        "فایل‌های جدید را بفرست و در پایان /done را بزن.\n"
        "لغو: /cancel",
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("delete_group:"))
async def delete_uploaded_group(callback: CallbackQuery):
    token = callback.data.split(":", 1)[1]
    uid = callback.from_user.id
    if not is_admin(uid):
        return await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
    if not deactivate_group(uid, token):
        return await callback.answer("❌ مجموعه پیدا نشد یا قبلاً حذف شده.", show_alert=True)
    await callback.answer("🗑 مجموعه حذف شد.")
    try:
        await callback.message.edit_text(
            "🗑 <b>مجموعه حذف شد.</b>\n\n"
            "مجموعه از لینک وب و لیست فعال خارج شد.",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
    except Exception:
        await callback.message.answer("🗑 مجموعه حذف شد.", reply_markup=main_keyboard())


# =========================================================
# START / FORCE JOIN
# =========================================================

@dp.message(Command("start"))
async def start_handler(message: Message):
    uid = message.from_user.id
    register_user(uid, message.from_user.username, message.from_user.first_name)
    clear_user_state(uid)

    if is_blocked(uid):
        return await message.answer("🚫 دسترسی شما مسدود است.")

    if not bot_is_enabled() and not is_admin(uid):
        return await message.answer(
            "🚧 <b>ربات موقتاً خاموش است.</b>\n\nلطفاً بعداً دوباره تلاش کن.",
            parse_mode="HTML",
        )

    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        token = args[1]

        if token.startswith("file_"):
            if not is_admin(uid) and not await check_membership(uid):
                return await message.answer(
                    "🔒 ابتدا در کانال‌های اجباری عضو شو.",
                    reply_markup=join_keyboard(),
                )

            row = get_file(token[5:])
            if not row:
                return await message.answer("❌ فایل پیدا نشد.")

            try:
                sent_message = await send_item(message.chat.id, dict(row))
                increment_download(row["token"])
                if not is_admin(uid):
                    await auto_delete_sent_messages(message.chat.id, [sent_message])
                if is_admin(uid):
                    username=await bot_username(); bot_url=tg_link(username,"file",row["token"]); web_url=f"{BASE_URL}/f/{row['token']}"
                    await message.answer("🛠 <b>مدیریت فایل</b>",parse_mode="HTML",reply_markup=upload_success_keyboard(bot_url,web_url,row["token"]))
            except Exception as e:
                print("START FILE ERROR:", repr(e))
                return await message.answer("❌ ارسال فایل انجام نشد.")
            return

        if token.startswith("group_"):
            if not is_admin(uid) and not await check_membership(uid):
                return await message.answer(
                    "🔒 ابتدا در کانال‌های اجباری عضو شو.",
                    reply_markup=join_keyboard(),
                )

            group, items = get_group(token[6:])
            if not group:
                return await message.answer("❌ مجموعه پیدا نشد.")

            await message.answer(
                f"📦 <b>{escape(group['title'] or 'مجموعه فایل')}</b>\n"
                f"📁 تعداد: {len(items)}",
                parse_mode="HTML",
            )

            sent = 0
            sent_messages = []
            for item in items:
                try:
                    sm = await send_item(message.chat.id, dict(item))
                    sent_messages.append(sm)
                    sent += 1
                except Exception as e:
                    print("GROUP SEND ERROR:", repr(e))

            if sent:
                increment_group_download(group["token"])
                if not is_admin(uid):
                    await auto_delete_sent_messages(message.chat.id, sent_messages)
            if is_admin(uid):
                username=await bot_username(); bot_url=tg_link(username,"group",group["token"]); web_url=f"{BASE_URL}/g/{group['token']}"
                await message.answer("🛠 <b>مدیریت مجموعه</b>",parse_mode="HTML",reply_markup=group_success_keyboard(bot_url,web_url,group["token"]))
            return

    if not is_admin(uid) and not await check_membership(uid):
        return await message.answer(
            "🔒 برای استفاده از ربات ابتدا عضو کانال‌های زیر شو:",
            reply_markup=join_keyboard(),
        )

    start_text_value = setting("start_text", "👋 خوش آمدی!")
    if is_admin(uid):
        await message.answer(start_text_value, parse_mode="HTML", reply_markup=main_keyboard(uid))
    else:
        # Normal users get no admin keyboard.
        await message.answer(start_text_value, parse_mode="HTML", reply_markup=None)


@dp.callback_query(F.data == "check_join")
async def check_join(callback: CallbackQuery):
    if await check_membership(callback.from_user.id):
        await callback.answer("عضویت تأیید شد.", show_alert=False)
        if is_admin(callback.from_user.id):
            await callback.message.answer("✅ عضویت تأیید شد.", reply_markup=main_keyboard(callback.from_user.id))
        else:
            await callback.message.answer("✅ عضویت تأیید شد.")
    else:
        await callback.answer("هنوز عضو همه کانال‌ها نیستی.", show_alert=True)
        await callback.message.answer(
            "❌ هنوز در همه کانال‌ها عضو نیستی.",
            reply_markup=join_keyboard(),
        )


# =========================================================
# UPLOAD BUTTONS
# =========================================================

@dp.message(F.text == "⬆️ آپلود فایل")
async def upload_single(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ فقط ادمین‌ها اجازه آپلود دارند.")
    admin_actions.pop(message.from_user.id, None)
    upload_modes[message.from_user.id] = "single"
    upload_items.pop(message.from_user.id, None)
    await message.answer(
        "🟢 <b>آپلود تکی فعال شد.</b>\n\n"
        "فایل، عکس، ویدیو، صدا یا متن را بفرست.\n"
        "لغو: /cancel",
        parse_mode="HTML",
    )


@dp.message(F.text == "📂 آپلود گروهی")
async def upload_group(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ فقط ادمین‌ها اجازه آپلود دارند.")
    admin_actions.pop(message.from_user.id,None)
    uid=message.from_user.id
    upload_modes[uid]="group"; upload_items[uid]=[]
    await message.answer("🟣 <b>آپلود گروهی فعال شد.</b>\n\nفایل‌ها را یکی‌یکی بفرست.\nبعد از اتمام روی «✅ پایان» بزن.\nبرای لغو «❌ انصراف» را بزن.",parse_mode="HTML",reply_markup=group_upload_keyboard())


@dp.message(
    F.document | F.photo | F.video | F.audio | F.voice | F.animation | F.sticker
)
async def media_upload_handler(message: Message):
    uid = message.from_user.id
    register_user(uid, message.from_user.username, message.from_user.first_name)

    if is_blocked(uid):
        return await message.answer("🚫 دسترسی شما مسدود است.")
    if not bot_is_enabled() and not is_admin(uid):
        return await message.answer(
            "🚧 <b>ربات موقتاً خاموش است.</b>",
            parse_mode="HTML",
        )
    if not is_admin(uid):
        return await message.answer("⛔ فقط ادمین‌ها اجازه آپلود دارند.")
    if not upload_modes.get(uid):
        return await message.answer(
            "ℹ️ ابتدا «🟢 آپلود تکی» یا «🟣 آپلود گروهی» را انتخاب کن.",
            reply_markup=main_keyboard(),
        )
    await process_upload(message)


# =========================================================
# FILES
# =========================================================

def stats_dashboard_keyboard(rows, username):
    buttons = []
    for row in rows[:10]:
        name = (row["file_name"] or "فایل").strip()
        token = row["token"]
        bot_url = tg_link(username, "file", token)
        web_url = f"{BASE_URL}/f/{token}"
        buttons.append([
            styled_inline_button(f"📄 {name[:24]}", style="primary", url=bot_url),
            styled_inline_button("🌐 وب", style="primary", url=web_url),
        ])
    buttons.append([
        styled_inline_button("🔄 بروزرسانی آمار", style="primary", callback_data="stats_dashboard"),
        styled_inline_button("🏠 منوی اصلی", style="primary", callback_data="dashboard_home"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_stats_dashboard(bot_info, user_id, user_row, users_total, active_users, blocked_users,
                          files_total, groups_total, downloads, total_size, recent_files):
    username = bot_info.username or "-"
    bot_id = bot_info.id
    owner_id = next(iter(ROOT_ADMIN_IDS), user_id) if ROOT_ADMIN_IDS else user_id
    status = "🟢 روشن" if bot_is_enabled() else "🔴 خاموش"
    storage_channel = setting("storage_channel_id", "تنظیم نشده") or "تنظیم نشده"
    photo_storage = "🟢 روشن" if setting("storage_photos", "0") == "1" else "🔴 خاموش"
    join_channel = JOIN_CHANNEL or "تنظیم نشده"

    lines = [
        "📊 <b>داشبورد آمار و فایل‌ها</b>",
        "<i>اطلاعات واقعی ثبت‌شده در ربات</i>",
        "",
        "👥 <b>آمار کاربران</b>",
        "<pre>",
        f"کل کاربران             {users_total}",
        f"فعال                    🟢 {active_users}",
        f"مسدود                   🔴 {blocked_users}",
        "</pre>",
        "",
        "📁 <b>آمار فایل‌ها</b>",
        "<pre>",
        f"تعداد فایل‌ها            {files_total}",
        f"تعداد گروه‌ها            {groups_total}",
        f"مجموع دانلود فایل‌ها     {downloads}",
        f"حجم فایل‌های فعال        {fmt_size(total_size)}",
        "</pre>",
        "",
        "🤖 <b>مشخصات ربات</b>",
        "<pre>",
        f"نام ربات                 @{username}",
        f"آیدی ربات                {bot_id}",
        f"صاحب ربات                {owner_id}",
        f"وضعیت ربات               {status}",
        f"ذخیره عکس‌ها             {photo_storage}",
        f"کانال ذخیره‌سازی         {storage_channel}",
        f"عضویت اجباری             {join_channel}",
        "</pre>",
        "",
        "📄 <b>آخرین فایل‌های من</b>",
    ]

    if recent_files:
        for i, row in enumerate(recent_files[:10], 1):
            name = escape((row["file_name"] or "فایل")[:42])
            lines.append(
                f"{i}. 📄 <b>{name}</b>  •  {fmt_size(row['file_size'])}  •  ⬇️ {row['downloads']}"
            )
    else:
        lines.append("<i>هنوز فایل فعالی ثبت نشده است.</i>")

    lines.extend([
        "",
        f"👤 <b>حساب شما:</b> <code>{user_id}</code>  •  {'👑 ادمین' if is_admin(user_id) else '👤 کاربر'}",
    ])
    return "\n".join(lines)


@dp.message(F.text == "📊 مشاهده فایل‌ها و آمار")
async def my_files(message: Message):
    uid = message.from_user.id
    if not is_admin(uid):
        return await message.answer("⛔ فقط ادمین‌ها اجازه مشاهده فایل‌ها و آمار را دارند.")

    c = db()
    user_row = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    users_total = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    blocked_users = c.execute("SELECT COUNT(*) FROM blocked_users").fetchone()[0]
    active_users = max(users_total - blocked_users, 0)
    files_total = c.execute("SELECT COUNT(*) FROM files WHERE active=1").fetchone()[0]
    groups_total = c.execute("SELECT COUNT(*) FROM groups WHERE active=1").fetchone()[0]
    downloads = c.execute("SELECT COALESCE(SUM(downloads),0) FROM files WHERE active=1").fetchone()[0]
    total_size = c.execute("SELECT COALESCE(SUM(file_size),0) FROM files WHERE active=1").fetchone()[0]
    recent_files = c.execute("""
        SELECT * FROM files
        WHERE owner_id=? AND active=1
        ORDER BY id DESC LIMIT 10
    """, (uid,)).fetchall()
    c.close()

    bot_info = await bot.get_me()
    text = build_stats_dashboard(
        bot_info, uid, user_row, users_total, active_users, blocked_users,
        files_total, groups_total, downloads, total_size, recent_files
    )
    await message.answer(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=stats_dashboard_keyboard(recent_files, bot_info.username),
    )


@dp.callback_query(F.data == "stats_dashboard")
async def stats_dashboard_callback(callback: CallbackQuery):
    uid = callback.from_user.id
    if not is_admin(uid):
        return await callback.answer("⛔ دسترسی ندارید.", show_alert=True)

    c = db()
    user_row = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    users_total = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    blocked_users = c.execute("SELECT COUNT(*) FROM blocked_users").fetchone()[0]
    active_users = max(users_total - blocked_users, 0)
    files_total = c.execute("SELECT COUNT(*) FROM files WHERE active=1").fetchone()[0]
    groups_total = c.execute("SELECT COUNT(*) FROM groups WHERE active=1").fetchone()[0]
    downloads = c.execute("SELECT COALESCE(SUM(downloads),0) FROM files WHERE active=1").fetchone()[0]
    total_size = c.execute("SELECT COALESCE(SUM(file_size),0) FROM files WHERE active=1").fetchone()[0]
    recent_files = c.execute("""
        SELECT * FROM files
        WHERE owner_id=? AND active=1
        ORDER BY id DESC LIMIT 10
    """, (uid,)).fetchall()
    c.close()

    bot_info = await bot.get_me()
    text = build_stats_dashboard(
        bot_info, uid, user_row, users_total, active_users, blocked_users,
        files_total, groups_total, downloads, total_size, recent_files
    )
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=stats_dashboard_keyboard(recent_files, bot_info.username),
    )
    await callback.answer("🔄 آمار بروزرسانی شد")


@dp.callback_query(F.data == "dashboard_home")
async def dashboard_home_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
    await callback.message.answer("🏠 <b>منوی اصلی</b>", parse_mode="HTML", reply_markup=main_keyboard())
    await callback.answer()


# =========================================================
# DONE / CANCEL
# =========================================================

async def finalize_group(message: Message):
    uid=message.from_user.id
    if not is_admin(uid): return await message.answer("⛔ فقط ادمین‌ها می‌توانند آپلود گروهی انجام دهند.")
    if upload_modes.get(uid)!="group": return await message.answer("ℹ️ آپلود گروهی فعالی نداری.",reply_markup=main_keyboard(uid))
    items=upload_items.get(uid,[])
    if not items: return await message.answer("❌ هنوز هیچ فایلی اضافه نکردی.",reply_markup=group_upload_keyboard())
    try:
        token=create_group(uid,"مجموعه فایل",items); username=await bot_username(); bot_url=tg_link(username,"group",token); web_url=f"{BASE_URL}/g/{token}"
        count=len(items); total_size=sum(int(x.get("file_size") or 0) for x in items); clear_user_state(uid)
        await message.answer("📦 <b>آپلود گروهی با موفقیت انجام شد!</b>\n\n"+f"📁 تعداد آیتم‌ها: <b>{count}</b>\n💾 حجم کل: <b>{fmt_size(total_size)}</b>\n🔐 شناسه: <code>{escape(token)}</code>\n🔗 <code>{escape(bot_url)}</code>\n\nیکی از گزینه‌ها را انتخاب کن:",parse_mode="HTML",disable_web_page_preview=True,reply_markup=group_success_keyboard(bot_url,web_url,token))
        await message.answer("🏠",reply_markup=main_keyboard(uid))
    except Exception as e:
        print("GROUP CREATE ERROR:",repr(e)); await message.answer(f"❌ ساخت مجموعه انجام نشد.\n<code>{escape(str(e))}</code>",parse_mode="HTML")

@dp.message(F.text == "✅ پایان")
async def finish_group_button(message: Message):
    await finalize_group(message)

@dp.message(F.text == "❌ انصراف")
async def cancel_upload_button(message: Message):
    clear_user_state(message.from_user.id); await message.answer("❌ آپلود لغو شد.",reply_markup=main_keyboard(message.from_user.id))

@dp.message(Command("done"))
async def done_handler(message: Message):
    await finalize_group(message)

@dp.message(Command("cancel"))
async def cancel(message: Message):
    clear_user_state(message.from_user.id); await message.answer("❌ عملیات لغو شد.",reply_markup=main_keyboard(message.from_user.id))


# =========================================================
# SETTINGS / NAVIGATION
# =========================================================

async def send_settings(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ شما ادمین نیستید.")
    await message.answer(
        "⚙️ <b>تنظیمات مدیریت</b>\n\nیک بخش را انتخاب کن:",
        parse_mode="HTML",
        reply_markup=settings_keyboard(),
    )


@dp.message(F.text.in_({"🔴 خاموش کردن ربات", "🟢 روشن کردن ربات"}))
async def toggle_bot(message: Message):
    uid = message.from_user.id
    if not is_admin(uid):
        if not bot_is_enabled():
            return await message.answer(
                "🚧 <b>ربات موقتاً خاموش است.</b>\n\nلطفاً بعداً دوباره تلاش کن.",
                parse_mode="HTML",
            )
        return await message.answer("⛔ فقط ادمین‌ها اجازه تغییر وضعیت ربات را دارند.")

    new_value = "0" if bot_is_enabled() else "1"
    set_setting("bot_enabled", new_value)
    if new_value == "1":
        return await message.answer(
            "🟢 <b>ربات روشن شد.</b>\n\nکاربران دوباره می‌توانند از ربات استفاده کنند.",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
    return await message.answer(
        "🔴 <b>ربات خاموش شد.</b>\n\nکاربران عادی تا زمان روشن شدن مجدد دسترسی نخواهند داشت.",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


@dp.message(F.text == "👀 مشاهده استارت از دید کاربر")
async def start_preview(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ دسترسی ندارید.")
    text = setting("start_text", "👋 خوش آمدی!")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✏️ ویرایش متن استارت", callback_data="edit_start_text")
    ]])
    await message.answer("👀 <b>نمایش استارت از دید کاربر:</b>", parse_mode="HTML")
    await message.answer(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "edit_start_text")
async def edit_start_text_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
    admin_actions[callback.from_user.id] = "edit_start"
    upload_modes.pop(callback.from_user.id, None)
    upload_items.pop(callback.from_user.id, None)
    await callback.answer()
    await callback.message.answer("✏️ متن جدید استارت را ارسال کن. متن دقیقاً همان‌طور که ذخیره شود نمایش داده می‌شود.")

@dp.message(F.text.startswith("🗑 حذف خودکار ۲۰ ثانیه‌ای:"))
async def toggle_auto_delete(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ دسترسی ندارید.")
    new_value = "0" if setting("auto_delete_20", "0") == "1" else "1"
    set_setting("auto_delete_20", new_value)
    status = "روشن 🟢" if new_value == "1" else "خاموش 🔴"
    await message.answer(
        f"🗑 حذف خودکار ۲۰ ثانیه‌ای {status} شد.\n\n"
        "وقتی کاربر فایل یا مجموعه را دریافت کند، ربات هشدار می‌دهد که آن را در Saved Messages ذخیره کند و ۲۰ ثانیه بعد پیام فایل‌ها را حذف می‌کند.",
        reply_markup=settings_keyboard(message.from_user.id),
    )

@dp.message(F.text == "⚙️ تنظیمات")
async def settings_handler(message: Message):
    await send_settings(message)


@dp.message(F.text.in_({
    "🏠 بازگشت به منوی اصلی",
    "🏠 منوی اصلی",
    "🔙 منوی اصلی",
}))
async def back_main(message: Message):
    clear_user_state(message.from_user.id)
    await message.answer("🏠 منوی اصلی", reply_markup=main_keyboard())


@dp.message(F.text == "🔙 بازگشت به تنظیمات")
async def back_settings(message: Message):
    clear_user_state(message.from_user.id)
    await send_settings(message)


# =========================================================
# FILE SETTINGS
# =========================================================

@dp.message(F.text == "📁 تنظیمات فایل‌ها")
async def file_settings(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ دسترسی ندارید.")
    storage = setting("storage_channel_id", "تنظیم نشده")
    await message.answer(
        f"📁 <b>تنظیمات فایل‌ها</b>\n\n"
        f"📦 کانال ذخیره‌سازی: <code>{escape(str(storage))}</code>\n"
        f"🖼 ذخیره عکس‌ها: "
        f"{'روشن 🟢' if setting('storage_photos','0') == '1' else 'خاموش 🔴'}\n"
        f"🌐 آدرس وب: <code>{escape(BASE_URL)}</code>",
        parse_mode="HTML",
        reply_markup=file_settings_keyboard(),
    )


@dp.message(F.text == "📦 کانال ذخیره‌سازی")
async def storage_channel(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ دسترسی ندارید.")
    admin_actions[message.from_user.id] = "storage"
    upload_modes.pop(message.from_user.id, None)
    upload_items.pop(message.from_user.id, None)
    await message.answer(
        "📦 شناسه کانال ذخیره‌سازی را بفرست.\n\n"
        "مثال: <code>-1001234567890</code>\n\n"
        "خاموش کردن: <code>off</code>\n\n"
        "⚠️ ربات باید در کانال Administrator باشد.",
        parse_mode="HTML",
    )


@dp.message(F.text.startswith("🖼 ذخیره عکس‌ها:"))
async def toggle_photos(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ دسترسی ندارید.")
    new = "0" if setting("storage_photos", "0") == "1" else "1"
    set_setting("storage_photos", new)
    await message.answer(
        "✅ وضعیت ذخیره عکس‌ها تغییر کرد.",
        reply_markup=file_settings_keyboard(),
    )


@dp.message(F.text == "🌐 تنظیمات لینک وب")
async def web_settings(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ دسترسی ندارید.")
    await message.answer(
        f"🌐 <b>تنظیمات لینک وب</b>\n\n"
        f"آدرس فعلی:\n<code>{escape(BASE_URL)}</code>\n\n"
        "برای تغییر، BASE_URL را در .env تغییر بده و ربات را Restart کن.",
        parse_mode="HTML",
        reply_markup=file_settings_keyboard(),
    )


# =========================================================
# FORCE JOIN MANAGEMENT
# =========================================================

@dp.message(F.text.in_({"🔐 عضویت اجباری", "📋 لیست کانال‌ها"}))
async def join_manage(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ دسترسی ندارید.")
    rows = list_join_channels()
    force_status = "فعال 🟢" if setting("force_join_enabled", "1") == "1" else "غیرفعال 🔴"
    text = "🔐 <b>پنل مدیریت عضویت اجباری</b>\n\n"
    text += f"وضعیت عضویت اجباری: <b>{force_status}</b>\n\n"
    if not rows:
        text += "❌ هیچ کانالی ثبت نشده.\n"
    else:
        text += "📢 <b>کانال‌های اجباری:</b>\n"
        for row in rows:
            ident = row["username"] or row["channel_id"]
            text += (
                f"• <b>{escape(row['title'] or ident)}</b>\n"
                f"  🆔 <code>{escape(str(row['channel_id']))}</code>\n"
                f"  🔗 {escape(ident)}\n"
            )
    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=join_manage_keyboard(),
    )


@dp.message(F.text == "➕ افزودن کانال")
async def add_channel_start(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ دسترسی ندارید.")
    admin_actions[message.from_user.id] = "join_add"
    upload_modes.pop(message.from_user.id, None)
    upload_items.pop(message.from_user.id, None)
    await message.answer(
        "➕ @username یا شناسه عددی کانال را بفرست.\n"
        "مثال: <code>@mychannel</code>\n\nلغو: /cancel",
        parse_mode="HTML",
    )


@dp.message(F.text == "🗑 حذف کانال")
async def remove_channel_start(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ دسترسی ندارید.")
    admin_actions[message.from_user.id] = "join_remove"
    await message.answer("🗑 شناسه کانال را بفرست.\nلغو: /cancel")


@dp.message(F.text == "🔐 فعال/غیرفعال کردن عضویت اجباری")
async def toggle_force_join(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ دسترسی ندارید.")

    new_value = "0" if setting("force_join_enabled", "1") == "1" else "1"
    set_setting("force_join_enabled", new_value)
    status = "فعال 🟢" if new_value == "1" else "غیرفعال 🔴"

    await message.answer(
        f"🔐 عضویت اجباری اکنون <b>{status}</b> است.\n\n"
        "کانال‌های ثبت‌شده باقی می‌مانند و با فعال‌سازی دوباره استفاده می‌شوند.",
        parse_mode="HTML",
        reply_markup=join_manage_keyboard(),
    )


@dp.message(F.text == "🧪 تست کانال‌ها")
async def test_channels(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ دسترسی ندارید.")

    me = await bot.get_me()
    lines = []

    for row in list_join_channels():
        try:
            chat = await bot.get_chat(row["channel_id"])
            member = await bot.get_chat_member(chat.id, me.id)
            ok = member.status in ("administrator", "creator")
            lines.append(
                f"{'✅' if ok else '❌'} "
                f"{escape(chat.title or str(chat.id))} | {member.status}"
            )
        except Exception as e:
            lines.append(
                f"❌ {escape(str(row['channel_id']))} | {escape(str(e)[:100])}"
            )

    await message.answer(
        "🧪 <b>نتیجه تست</b>\n\n" + ("\n".join(lines) or "لیست خالی است."),
        parse_mode="HTML",
        reply_markup=join_manage_keyboard(),
    )


# =========================================================
# ADMIN LISTS / STATS
# =========================================================

@dp.message(F.text == "👥 لیست کاربران")
async def users_list(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ دسترسی ندارید.")
    c = db()
    rows = c.execute("""
        SELECT u.*,
            CASE WHEN b.user_id IS NULL THEN 0 ELSE 1 END blocked
        FROM users u
        LEFT JOIN blocked_users b ON b.user_id=u.user_id
        ORDER BY u.created_at DESC LIMIT 50
    """).fetchall()
    c.close()

    text = "👥 <b>لیست کاربران</b>\n\n"
    for row in rows:
        text += (
            f"{'🚫' if row['blocked'] else '🟢'} "
            f"{escape(row['first_name'] or 'بدون نام')} • "
            f"<code>{row['user_id']}</code> • "
            f"@{escape(row['username'] or '-')}\n"
        )
    await message.answer(
        text if rows else text + "لیست خالی است.",
        parse_mode="HTML",
        reply_markup=settings_keyboard(),
    )


@dp.message(F.text == "📋 لیست مسدودها")
async def blocked_list(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ دسترسی ندارید.")
    c = db()
    rows = c.execute("""
        SELECT b.*, u.first_name, u.username
        FROM blocked_users b
        LEFT JOIN users u ON u.user_id=b.user_id
        ORDER BY b.created_at DESC
    """).fetchall()
    c.close()

    text = "🚫 <b>لیست مسدودها</b>\n\n"
    for row in rows:
        text += (
            f"• {escape(row['first_name'] or '-')} "
            f"• <code>{row['user_id']}</code>\n"
        )
    await message.answer(
        text if rows else text + "لیست خالی است.",
        parse_mode="HTML",
        reply_markup=settings_keyboard(),
    )


@dp.message(F.text == "👑 لیست ادمین‌ها")
async def admin_list(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ دسترسی ندارید.")
    c = db()
    rows = c.execute("""
        SELECT a.*, u.username, u.first_name
        FROM admins a
        LEFT JOIN users u ON u.user_id=a.user_id
        ORDER BY a.created_at
    """).fetchall()
    c.close()

    text = "👑 <b>لیست ادمین‌ها</b>\n\n"
    for row in rows:
        text += (
            f"• <code>{row['user_id']}</code> "
            f"{'👑 اصلی' if row['user_id'] in ROOT_ADMIN_IDS else '🛡 ادمین'}\n"
        )
    await message.answer(
        text if rows else text + "لیست خالی است.",
        parse_mode="HTML",
        reply_markup=settings_keyboard(),
    )


@dp.message(F.text == "📊 آمار کلی")
async def global_stats(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ دسترسی ندارید.")
    c = db()
    users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    files = c.execute("SELECT COUNT(*) FROM files WHERE active=1").fetchone()[0]
    groups = c.execute("SELECT COUNT(*) FROM groups WHERE active=1").fetchone()[0]
    blocked = c.execute("SELECT COUNT(*) FROM blocked_users").fetchone()[0]
    admins = c.execute("SELECT COUNT(*) FROM admins").fetchone()[0]
    downloads = c.execute("SELECT COALESCE(SUM(downloads),0) FROM files").fetchone()[0]
    group_downloads = c.execute(
        "SELECT COALESCE(SUM(downloads),0) FROM groups"
    ).fetchone()[0]
    size = c.execute(
        "SELECT COALESCE(SUM(file_size),0) FROM files WHERE active=1"
    ).fetchone()[0]
    c.close()

    await message.answer(
        "📊 <b>آمار کلی</b>\n\n"
        f"👥 کاربران: {users}\n"
        f"📁 فایل‌ها: {files}\n"
        f"📦 گروه‌ها: {groups}\n"
        f"⬇️ دانلود فایل‌ها: {downloads}\n"
        f"⬇️ دریافت گروه‌ها: {group_downloads}\n"
        f"💾 حجم: {fmt_size(size)}\n"
        f"🚫 مسدودها: {blocked}\n"
        f"👑 ادمین‌ها: {admins}",
        parse_mode="HTML",
        reply_markup=settings_keyboard(),
    )


# =========================================================
# ADMIN / BLOCK MANAGEMENT
# =========================================================

def admin_manage_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [styled_button("➕ افزودن ادمین","success"), styled_button("➖ حذف ادمین","danger")],
        [styled_button("👑 لیست ادمین‌ها","primary")],
        [styled_button("🔙 بازگشت به تنظیمات","primary")],
    ], resize_keyboard=True)

def block_manage_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [styled_button("➕ مسدود کردن کاربر","danger"), styled_button("➖ رفع مسدودی","success")],
        [styled_button("📋 لیست مسدودها","primary")],
        [styled_button("🔙 بازگشت به تنظیمات","primary")],
    ], resize_keyboard=True)

@dp.message(F.text == "👑 مدیریت ادمین‌ها")
async def admin_manage(message: Message):
    if not is_root(message.from_user.id): return await message.answer("⛔ فقط Root Admin اجازه دارد.")
    await message.answer("👑 <b>مدیریت ادمین‌ها</b>",parse_mode="HTML",reply_markup=admin_manage_keyboard())

@dp.message(F.text == "🚫 مدیریت مسدودی")
async def block_manage(message: Message):
    if not is_admin(message.from_user.id): return await message.answer("⛔ دسترسی ندارید.")
    await message.answer("🚫 <b>مدیریت مسدودی</b>",parse_mode="HTML",reply_markup=block_manage_keyboard())

@dp.message(F.text == "➕ مسدود کردن کاربر")
async def block_start(message: Message):
    if not is_admin(message.from_user.id): return await message.answer("⛔ دسترسی ندارید.")
    admin_actions[message.from_user.id]="block_user"
    upload_modes.pop(message.from_user.id,None); upload_items.pop(message.from_user.id,None)
    await message.answer("🚫 آیدی عددی کاربر را بفرست.\nلغو: /cancel")

@dp.message(F.text == "➖ رفع مسدودی")
async def unblock_start(message: Message):
    if not is_admin(message.from_user.id): return await message.answer("⛔ دسترسی ندارید.")
    admin_actions[message.from_user.id]="unblock_user"
    upload_modes.pop(message.from_user.id,None); upload_items.pop(message.from_user.id,None)
    await message.answer("➖ آیدی عددی کاربر را بفرست.\nلغو: /cancel")

# =========================================================
# ADMIN ACTIONS
# =========================================================

@dp.message(F.text == "➕ افزودن ادمین")
async def add_admin_start(message: Message):
    if not is_root(message.from_user.id):
        return await message.answer("⛔ فقط Root Admin اجازه دارد.")
    admin_actions[message.from_user.id] = "add_admin"
    upload_modes.pop(message.from_user.id, None)
    upload_items.pop(message.from_user.id, None)
    await message.answer("➕ آیدی عددی کاربر را بفرست.\nلغو: /cancel")


@dp.message(F.text == "➖ حذف ادمین")
async def remove_admin_start(message: Message):
    if not is_root(message.from_user.id):
        return await message.answer("⛔ فقط Root Admin اجازه دارد.")
    admin_actions[message.from_user.id] = "remove_admin"
    upload_modes.pop(message.from_user.id, None)
    upload_items.pop(message.from_user.id, None)
    await message.answer("➖ آیدی عددی ادمین را بفرست.\nلغو: /cancel")


@dp.message(F.text == "📣 ارسال پیام همگانی")
async def broadcast_start(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ دسترسی ندارید.")
    admin_actions[message.from_user.id] = "broadcast"
    upload_modes.pop(message.from_user.id, None)
    upload_items.pop(message.from_user.id, None)
    await message.answer(
        "📣 پیام، عکس، ویدیو، فایل یا متن موردنظر را در پیام بعدی بفرست.\n\n"
        "پیام برای کاربران ثبت‌شده ارسال می‌شود.\n"
        "لغو: /cancel"
    )


# =========================================================
# LANGUAGE
# =========================================================

# =========================================================
# MY STATS / ACCOUNT
# =========================================================

@dp.message(F.text == "📊 آمار من")
async def my_stats(message: Message):
    uid = message.from_user.id
    if not is_admin(uid):
        return await message.answer("⛔ دسترسی ندارید.")
    c = db()
    row = c.execute("""
        SELECT COUNT(*) files,
            COALESCE(SUM(file_size),0) size,
            COALESCE(SUM(downloads),0) downloads
        FROM files WHERE owner_id=? AND active=1
    """, (uid,)).fetchone()
    groups = c.execute(
        "SELECT COUNT(*) FROM groups WHERE owner_id=? AND active=1",
        (uid,),
    ).fetchone()[0]
    c.close()

    await message.answer(
        "📊 <b>آمار من</b>\n\n"
        f"📁 فایل‌ها: {row['files']}\n"
        f"📦 گروه‌ها: {groups}\n"
        f"⬇️ دانلودها: {row['downloads']}\n"
        f"💾 حجم: {fmt_size(row['size'])}",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


@dp.message(F.text == "👤 حساب من")
async def account(message: Message):
    await message.answer(
        "👤 <b>حساب شما</b>\n\n"
        f"🆔 <code>{message.from_user.id}</code>\n"
        f"👤 @{escape(message.from_user.username or '-')}\n"
        f"🛡 وضعیت: "
        f"{'ادمین' if is_admin(message.from_user.id) else 'کاربر'}",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


# =========================================================
# COMMANDS
# =========================================================

@dp.message(Command("upload"))
async def upload_command(message: Message):
    await upload_single(message)


@dp.message(Command("group"))
async def group_command(message: Message):
    await upload_group(message)


@dp.message(Command("files"))
async def files_command(message: Message):
    await my_files(message)


@dp.message(Command("stats"))
async def stats_command(message: Message):
    await my_stats(message)


@dp.message(Command("me"))
async def me_command(message: Message):
    await account(message)


@dp.message(Command("admin"))
async def admin_command(message: Message):
    await send_settings(message)


@dp.message(Command("help"))
async def help_command(message: Message):
    await message.answer(
        "ℹ️ <b>راهنما</b>\n\n"
        "⬆️ آپلود فایل: یک فایل بفرست.\n"
        "📂 آپلود گروهی: چند فایل بفرست و /done بزن.\n"
        "📊 مشاهده فایل‌ها و آمار: مدیریت فایل‌ها و دیدن آمار.\n"
        "📣 ارسال پیام همگانی: پیام را برای کاربران بفرست.\n"
        "⚙️ تنظیمات: مدیریت ربات.",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


# =========================================================
# BROADCAST
# =========================================================

async def broadcast_message(source: Message):
    c = db()
    user_ids = [r["user_id"] for r in c.execute(
        "SELECT user_id FROM users ORDER BY user_id"
    ).fetchall()]
    c.close()

    sent = 0
    failed = 0

    for uid in user_ids:
        try:
            await bot.copy_message(
                chat_id=uid,
                from_chat_id=source.chat.id,
                message_id=source.message_id,
            )
            sent += 1
        except Exception as e:
            failed += 1
            print("BROADCAST ERROR", uid, repr(e))
        await asyncio.sleep(0.04)

    return sent, failed


# =========================================================
# TEXT / ADMIN INPUT
# =========================================================

MENU_TEXTS = {
    "⬆️ آپلود فایل", "📂 آپلود گروهی", "📊 مشاهده فایل‌ها و آمار",
    "📣 ارسال پیام همگانی", "🔴 خاموش کردن ربات", "🟢 روشن کردن ربات",
    "⚙️ تنظیمات", "👤 حساب من", "👥 لیست کاربران", "🚫 لیست مسدودها",
    "👑 لیست ادمین‌ها", "➕ افزودن ادمین", "➖ حذف ادمین", "🔐 عضویت اجباری",
    "📊 آمار کلی", "📁 تنظیمات فایل‌ها", "📣 پیام همگانی",
    "📦 کانال ذخیره‌سازی", "🌐 تنظیمات لینک وب", "🏠 بازگشت به منوی اصلی",
    "🏠 منوی اصلی", "🔙 منوی اصلی", "🔙 بازگشت به تنظیمات", "➕ افزودن کانال", "🗑 حذف کانال", "📋 لیست کانال‌ها",
    "🧪 تست کانال‌ها", "🔐 فعال/غیرفعال کردن عضویت اجباری",
}


@dp.message(F.text)
async def text_router(message: Message):
    uid = message.from_user.id
    register_user(uid, message.from_user.username, message.from_user.first_name)

    if is_blocked(uid):
        return await message.answer("🚫 دسترسی شما مسدود است.")

    if not bot_is_enabled() and not is_admin(uid):
        return await message.answer(
            "🚧 <b>ربات موقتاً خاموش است.</b>\n\nلطفاً بعداً دوباره تلاش کن.",
            parse_mode="HTML",
        )

    if message.text.startswith("/"):
        return

    # Localized menu aliases are normalized here so all languages use the same actions.
    aliases=localized_label_map()
    canonical=aliases.get(message.text)
    if canonical:
        if canonical=="upload": return await upload_single(message)
        if canonical=="group": return await upload_group(message)
        if canonical=="files": return await my_files(message)
        if canonical=="broadcast": return await broadcast_start(message)
        if canonical=="settings": return await settings_handler(message)
        if canonical=="admins": return await admin_manage(message)
        if canonical=="blocks": return await block_manage(message)
        if canonical=="start_view": return await start_preview(message)
        if canonical=="users": return await users_list(message)
        if canonical=="blocked_list": return await blocked_list(message)
        if canonical=="admin_list": return await admin_list(message)
        if canonical=="add_admin": return await add_admin_start(message)
        if canonical=="remove_admin": return await remove_admin_start(message)
        if canonical=="force": return await join_manage(message)
        if canonical=="stats": return await global_stats(message)
        if canonical=="file_settings": return await file_settings(message)
        if canonical=="back": return await back_main(message)
        if canonical=="toggle_on" or canonical=="toggle_off": return await toggle_bot(message)


    action = admin_actions.get(uid)

    # Admin action has priority over upload state.
    if action:
        if not is_admin(uid):
            admin_actions.pop(uid, None)
            return
        return await handle_admin_action_input(message, action)

    if upload_modes.get(uid):
        if not is_admin(uid):
            return
        return await process_upload(message)

    # No active operation.
    return


async def handle_admin_action_input(message: Message, action: str):
    uid = message.from_user.id
    raw = (message.text or "").strip()

    if action.startswith("rename_file:"):
        token=action.split(":",1)[1]
        if not raw: return await message.answer("❌ نام خالی است.")
        if len(raw)>200: return await message.answer("❌ نام خیلی طولانی است.")
        ok=rename_file(uid,token,raw)
        admin_actions.pop(uid,None)
        row=get_file(token)
        if ok and row:
            username=await bot_username(); bot_url=tg_link(username,"file",token); web_url=f"{BASE_URL}/f/{token}"
            return await message.answer("✅ نام فایل تغییر کرد.",reply_markup=upload_success_keyboard(bot_url,web_url,token))
        return await message.answer("❌ فایل پیدا نشد.",reply_markup=main_keyboard(uid))

    if action == "edit_start":
        if not raw:
            return await message.answer("❌ متن نمی‌تواند خالی باشد.")
        set_setting("start_text", raw)
        admin_actions.pop(uid,None)
        return await message.answer("✅ متن استارت ذخیره شد.",reply_markup=settings_keyboard(uid))

    if action == "storage":
        if raw.lower() == "off":
            set_setting("storage_channel_id", "")
            admin_actions.pop(uid, None)
            return await message.answer(
                "✅ کانال ذخیره‌سازی خاموش شد.",
                reply_markup=file_settings_keyboard(),
            )

        if not raw:
            return await message.answer("❌ شناسه کانال را بفرست.")

        # Validate the channel before saving.
        try:
            target = int(raw) if raw.lstrip("-").isdigit() else raw
            chat = await bot.get_chat(target)
            me = await bot.get_me()
            member = await bot.get_chat_member(chat.id, me.id)
            if member.status not in ("administrator", "creator"):
                return await message.answer(
                    "❌ ربات در کانال Administrator نیست."
                )
        except Exception as e:
            return await message.answer(
                f"❌ کانال معتبر نیست یا دسترسی وجود ندارد.\n<code>{escape(str(e))}</code>",
                parse_mode="HTML",
            )

        set_setting("storage_channel_id", raw)
        admin_actions.pop(uid, None)
        return await message.answer(
            "✅ کانال ذخیره‌سازی ذخیره شد.",
            reply_markup=file_settings_keyboard(),
        )

    if action == "join_add":
        try:
            chat = await bot.get_chat(raw)
            me = await bot.get_me()
            member = await bot.get_chat_member(chat.id, me.id)

            if member.status not in ("administrator", "creator"):
                return await message.answer(
                    "❌ ربات در این کانال Administrator نیست."
                )

            username = (
                f"@{chat.username}"
                if getattr(chat, "username", None)
                else ""
            )
            invite = ""
            if not username:
                try:
                    inv = await bot.create_chat_invite_link(chat.id)
                    invite = inv.invite_link
                except Exception as e:
                    print("INVITE LINK ERROR:", repr(e))

            ok, text = add_join_channel(
                chat.id, chat.title, username, invite
            )
        except Exception as e:
            ok, text = False, f"خطا: {e}"

        admin_actions.pop(uid, None)
        return await message.answer(
            ("✅ " if ok else "❌ ") + text,
            reply_markup=join_manage_keyboard(),
        )

    if action == "join_remove":
        ok, text = remove_join_channel(raw)
        admin_actions.pop(uid, None)
        return await message.answer(
            ("✅ " if ok else "❌ ") + text,
            reply_markup=join_manage_keyboard(),
        )

    if action in ("block_user", "unblock_user"):
        if not is_admin(uid):
            admin_actions.pop(uid,None)
            return await message.answer("⛔ دسترسی ندارید.")
        if not raw.isdigit():
            return await message.answer("❌ فقط آیدی عددی بفرست.")
        target=int(raw)
        if action=="block_user":
            ok,text=block_user(target,uid)
        else:
            ok,text=unblock_user(target)
        admin_actions.pop(uid,None)
        return await message.answer(("✅ " if ok else "❌ ")+text,reply_markup=block_manage_keyboard())

    if action in ("add_admin", "remove_admin"):
        if not is_root(uid):
            admin_actions.pop(uid, None)
            return await message.answer("⛔ فقط Root Admin اجازه دارد.")
        if not raw.isdigit():
            return await message.answer("❌ فقط آیدی عددی بفرست.")

        target = int(raw)
        if action == "add_admin":
            ok, text = add_admin(target, uid)
        else:
            ok, text = remove_admin(target)

        admin_actions.pop(uid, None)
        return await message.answer(
            ("✅ " if ok else "❌ ") + text,
            reply_markup=settings_keyboard(),
        )

    if action == "broadcast":
        async with broadcast_lock:
            admin_actions.pop(uid, None)
            status = await message.answer("📣 ارسال همگانی شروع شد...")
            sent, failed = await broadcast_message(message)
            await status.edit_text(
                f"📣 <b>ارسال همگانی تمام شد.</b>\n\n"
                f"✅ موفق: {sent}\n"
                f"❌ ناموفق: {failed}",
                parse_mode="HTML",
            )
        return


# =========================================================
# WEB
# =========================================================

async def home(request):
    return web.Response(
        text="""<!doctype html>
<html lang="fa"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Telegram Uploader</title></head>
<body style="background:#101114;color:white;font-family:Arial;text-align:center;padding:80px">
<h1>🤖 Telegram Uploader</h1><p>Online ✅</p>
</body></html>""",
        content_type="text/html",
    )


def html_layout(title, body):
    return f"""<!doctype html>
<html lang="fa">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title>
</head>
<body style="margin:0;min-height:100vh;background:#101114;color:white;font-family:Arial;display:flex;align-items:center;justify-content:center;padding:20px;box-sizing:border-box">
<div style="width:100%;max-width:700px;padding:28px;background:#1b1e25;border-radius:24px;text-align:center;box-sizing:border-box">
{body}
</div>
</body></html>"""


async def _telegram_file_bytes(file_id):
    """Return Telegram file metadata and an HTTP stream response source."""
    info = await bot.get_file(file_id)
    if not info.file_path:
        raise RuntimeError("Telegram file unavailable")
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{info.file_path}"
    timeout = ClientTimeout(total=None, sock_connect=30, sock_read=120)
    return url, timeout


async def telegram_media_response(request, file_id, filename="file", inline=True):
    if not file_id:
        return web.Response(text="File unavailable", status=404)
    try:
        url, timeout = await _telegram_file_bytes(file_id)
        headers = {}
        if inline:
            headers["Content-Disposition"] = f'inline; filename="{quote(filename)}"'
        else:
            headers["Content-Disposition"] = f'attachment; filename="{quote(filename)}"'

        async with ClientSession(timeout=timeout) as http:
            async with http.get(url, proxy=PROXY_URL or None) as resp:
                if resp.status != 200:
                    return web.Response(text=f"Telegram download failed: {resp.status}", status=502)
                out = web.StreamResponse(
                    status=200,
                    headers={
                        "Content-Type": resp.headers.get("Content-Type", "application/octet-stream"),
                        **headers,
                    },
                )
                await out.prepare(request)
                async for chunk in resp.content.iter_chunked(256 * 1024):
                    await out.write(chunk)
                await out.write_eof()
                return out
    except (ConnectionResetError, asyncio.CancelledError):
        raise
    except Exception as e:
        print("WEB MEDIA ERROR:", repr(e))
        return web.Response(text="Download failed", status=500)


def web_media_preview(item, src):
    t = item["file_type"]
    if t == "photo":
        return (f"<img src='{src}' alt='photo' style='width:100%;max-height:420px;object-fit:contain;"
                "border-radius:16px;background:#111820;margin:10px 0'>")
    if t == "video":
        return (f"<video controls playsinline preload='metadata' style='width:100%;max-height:420px;border-radius:16px;"
                f"background:#000;margin:10px 0'><source src='{src}'></video>")
    if t == "audio":
        return f"<audio controls style='width:100%;margin:10px 0'><source src='{src}'></audio>"
    if t == "voice":
        return f"<audio controls style='width:100%;margin:10px 0'><source src='{src}'></audio>"
    if t == "animation":
        return (f"<img src='{src}' alt='animation' style='width:100%;max-height:420px;object-fit:contain;"
                "border-radius:16px;background:#111820;margin:10px 0'>")
    if t == "sticker":
        is_video_sticker = item.get("sticker_video") or str(item.get("file_name") or "").lower().endswith(".webm")
        is_animated_sticker = item.get("sticker_animated") or str(item.get("file_name") or "").lower().endswith(".tgs")
        if is_video_sticker:
            return (f"<video controls playsinline loop preload='metadata' style='width:100%;max-height:420px;"
                    f"border-radius:16px;background:#111820;margin:10px 0>"
                    f"<source src='{src}' type='video/webm'></video>")
        if is_animated_sticker:
            return ("<div style='padding:18px;border-radius:14px;background:#111820;margin:10px 0'>"
                    "🎞️ استیکر متحرک (.tgs) — برای دریافت روی دکمه دانلود بزن.</div>")
        return (f"<img src='{src}' alt='sticker' style='width:100%;max-height:360px;object-fit:contain;"
                "border-radius:16px;background:#111820;margin:10px 0'>")
    return ""


async def download_page(request):
    token = request.match_info["token"]
    row = get_file(token)
    if not row:
        return web.Response(text="File not found", status=404)

    name = escape(row["file_name"] or "file")
    size = fmt_size(row["file_size"])
    src = f"/download/{quote(token)}"

    if row["file_type"] == "text":
        body = (
            "<div style='font-size:60px'>📄</div>"
            f"<h1>{name}</h1>"
            f"<pre style='white-space:pre-wrap;text-align:left;background:#242833;padding:18px;border-radius:14px'>{escape(row['text_content'] or '')}</pre>"
        )
    else:
        preview = web_media_preview(row, src)
        body = (
            "<div style='font-size:60px'>📁</div>"
            f"<h1>{name}</h1>"
            f"<p>💾 {size}</p>"
            f"{preview}"
            f"<a href='{src}' download style='display:inline-block;padding:14px 25px;background:#238636;color:white;text-decoration:none;border-radius:12px'>⬇️ دانلود فایل</a>"
        )

    return web.Response(text=html_layout(name, body), content_type="text/html")


async def group_item_media(request):
    token = request.match_info["token"]
    position = int(request.match_info["position"])
    group, items = get_group(token)
    if not group or position < 1 or position > len(items):
        return web.Response(text="Group item not found", status=404)
    item = items[position - 1]
    if item["file_type"] == "text" or not item["file_id"]:
        return web.Response(text="Item has no downloadable media", status=404)
    return await telegram_media_response(
        request,
        item["file_id"],
        item["file_name"] or "file",
        inline=True,
    )


async def group_item_download(request):
    token = request.match_info["token"]
    position = int(request.match_info["position"])
    group, items = get_group(token)
    if not group or position < 1 or position > len(items):
        return web.Response(text="Group item not found", status=404)
    item = items[position - 1]
    if item["file_type"] == "text" or not item["file_id"]:
        return web.Response(text="Item is not downloadable", status=404)
    return await telegram_media_response(
        request,
        item["file_id"],
        item["file_name"] or "file",
        inline=False,
    )


async def group_page(request):
    token = request.match_info["token"]
    group, items = get_group(token)
    if not group:
        return web.Response(text="Group not found", status=404)

    rows = []
    for i, item in enumerate(items, 1):
        name = escape(item["file_name"] or "file")
        media_src = f"/g/{quote(token)}/item/{i}"
        download_src = f"/g/{quote(token)}/download/{i}"
        if item["file_type"] == "text":
            content = (
                f"<pre style='white-space:pre-wrap;text-align:left;background:#111820;padding:14px;border-radius:12px'>"
                f"{escape(item['text_content'] or '')}</pre>"
            )
            action = ""
        else:
            content = web_media_preview(item, media_src)
            action = (
                f"<a href='{download_src}' style='display:inline-block;padding:10px 16px;border-radius:10px;"
                "background:#238636;color:white;text-decoration:none;margin-top:6px'>⬇️ دانلود</a>"
            )
        rows.append(
            f"<div style='padding:14px;background:#242833;border-radius:14px;margin:10px 0;text-align:right'>"
            f"<b>{i}. {name}</b>"
            f"<div style='opacity:.8;margin-top:5px'>💾 {fmt_size(item['file_size'])}</div>"
            f"{content}{action}</div>"
        )

    username = await bot_username()
    bot_url = f"https://t.me/{username}?start=group_{quote(token)}"

    body = (
        f"<div style='font-size:60px'>📦</div>"
        f"<h1>{escape(group['title'] or 'مجموعه فایل')}</h1>"
        f"<p>📁 {len(items)} آیتم • ⬇️ {group['downloads']} دریافت</p>"
        f"{''.join(rows)}"
        f"<a href='{escape(bot_url, quote=True)}' style='display:inline-block;padding:14px 25px;border-radius:12px;background:#238636;color:white;text-decoration:none'>🤖 دریافت مجموعه در ربات</a>"
    )
    return web.Response(text=html_layout(group["title"] or "Group", body), content_type="text/html")


async def download_file(request):
    token = request.match_info["token"]
    row = get_file(token)

    if not row or row["file_type"] == "text" or not row["file_id"]:
        return web.Response(text="File not downloadable", status=404)

    try:
        info = await bot.get_file(row["file_id"])
        if not info.file_path:
            return web.Response(text="Telegram file unavailable", status=500)

        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{info.file_path}"

        # Use the same proxy if configured. This is important on servers
        # where Telegram's file endpoint is reachable only through the proxy.
        timeout = ClientTimeout(total=None, sock_connect=30, sock_read=120)
        headers = {
            "Content-Disposition": (
                f'attachment; filename="{quote(row["file_name"] or "file")}"'
            )
        }

        async with ClientSession(timeout=timeout) as http:
            async with http.get(url, proxy=PROXY_URL or None) as resp:
                if resp.status != 200:
                    return web.Response(
                        text=f"Telegram download failed: {resp.status}",
                        status=502,
                    )

                response = web.StreamResponse(
                    status=200,
                    headers={
                        "Content-Type": resp.headers.get(
                            "Content-Type", "application/octet-stream"
                        ),
                        "Content-Disposition": headers["Content-Disposition"],
                    },
                )
                await response.prepare(request)

                async for chunk in resp.content.iter_chunked(256 * 1024):
                    await response.write(chunk)

                await response.write_eof()

        increment_download(token)
        return response

    except (ConnectionResetError, asyncio.CancelledError):
        raise
    except Exception as e:
        print("DOWNLOAD ERROR:", repr(e))
        return web.Response(text="Download failed", status=500)


async def start_web_server():
    app = web.Application(client_max_size=0)
    app.router.add_get("/", home)
    app.router.add_get("/f/{token}", download_page)
    app.router.add_get("/g/{token}/item/{position}", group_item_media)
    app.router.add_get("/g/{token}/download/{position}", group_item_download)
    app.router.add_get("/g/{token}", group_page)
    app.router.add_get("/download/{token}", download_file)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()
    print(f"🌐 Web server: {BASE_URL}")


# =========================================================
# COMMANDS
# =========================================================

async def set_commands():
    await bot.set_my_commands([
        BotCommand(command="start", description="شروع"),
        BotCommand(command="help", description="راهنما"),
        BotCommand(command="upload", description="آپلود تکی"),
        BotCommand(command="group", description="آپلود گروهی"),
        BotCommand(command="done", description="پایان گروه"),
        BotCommand(command="files", description="فایل‌ها و آمار"),
        BotCommand(command="stats", description="آمار"),
        BotCommand(command="me", description="حساب من"),
        BotCommand(command="admin", description="تنظیمات"),
        BotCommand(command="cancel", description="لغو عملیات"),
    ])


# =========================================================
# ERROR HANDLER
# =========================================================

@dp.errors()
async def global_error_handler(event):
    print("UNHANDLED BOT ERROR:", repr(event.exception))
    return True


# =========================================================
# MAIN
# =========================================================

async def main():
    init_db()

    print("=" * 55)
    print("🤖 Telegram Uploader - REBUILT")
    print(f"🌐 BASE_URL: {BASE_URL}")
    print(f"📢 JOIN_CHANNEL: {JOIN_CHANNEL}")
    print("=" * 55)

    try:
        me = await bot.get_me()
        await set_commands()
        await start_web_server()

        print(f"✅ Connected: @{me.username}")
        print("🚀 Bot is running...")

        await dp.start_polling(bot)

    finally:
        await session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Bot stopped.")
