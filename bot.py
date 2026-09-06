import asyncio
import os
import secrets
import sqlite3
from pathlib import Path
from html import escape

from dotenv import load_dotenv
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommand,
)
from aiogram.client.session.aiohttp import AiohttpSession

# =========================================================
# PATH / ENV
# =========================================================
BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
DB_PATH = BASE_DIR / "uploader.db"
load_dotenv(ENV_FILE, override=True)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PROXY_URL = os.getenv("PROXY_URL", "").strip()
BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8080").strip().rstrip("/")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
JOIN_CHANNEL = os.getenv("JOIN_CHANNEL", "@eldnv").strip()

STORAGE_CHANNEL_ID_RAW = os.getenv("STORAGE_CHANNEL_ID", "").strip()
if STORAGE_CHANNEL_ID_RAW:
    try:
        STORAGE_CHANNEL_ID = int(STORAGE_CHANNEL_ID_RAW)
    except ValueError:
        STORAGE_CHANNEL_ID = STORAGE_CHANNEL_ID_RAW
else:
    STORAGE_CHANNEL_ID = None

# Admins from .env are the protected/root admins.
ROOT_ADMIN_IDS = set()
for item in os.getenv("ADMIN_IDS", "").split(","):
    item = item.strip()
    if item.isdigit():
        ROOT_ADMIN_IDS.add(int(item))

if not BOT_TOKEN:
    raise RuntimeError(f"BOT_TOKEN پیدا نشد. فایل .env را بررسی کن: {ENV_FILE}")

# =========================================================
# BOT
# =========================================================
session = AiohttpSession(proxy=PROXY_URL or None, timeout=15)
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher()

admin_message_mode = set()

# Per-user temporary upload mode:
# None = normal, "single" = one item, "group" = collecting group
upload_modes = {}

# Per-admin temporary admin action
admin_actions = {}

# =========================================================
# DATABASE
# =========================================================
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS blocked_users (
            user_id INTEGER PRIMARY KEY,
            blocked_by INTEGER,
            reason TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            language TEXT DEFAULT 'fa',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            added_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS join_channels (
            channel_id TEXT PRIMARY KEY,
            title TEXT,
            username TEXT,
            invite_url TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Backward-compatible migration for old databases.
    try:
        cur.execute("ALTER TABLE join_channels ADD COLUMN invite_url TEXT")
    except sqlite3.OperationalError:
        pass

    # Keep the original .env channel as the initial forced-join channel.
    if JOIN_CHANNEL:
        cur.execute(
            "INSERT OR IGNORE INTO join_channels(channel_id, title, username, invite_url) VALUES (?, ?, ?, ?)",
            (JOIN_CHANNEL, JOIN_CHANNEL, JOIN_CHANNEL if JOIN_CHANNEL.startswith("@") else "", "")
        )

    cur.execute("""
        CREATE TABLE IF NOT EXISTS files (
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
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT UNIQUE NOT NULL,
            owner_id INTEGER NOT NULL,
            title TEXT,
            downloads INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS group_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            file_id TEXT,
            file_name TEXT,
            file_size INTEGER DEFAULT 0,
            file_type TEXT,
            text_content TEXT,
            FOREIGN KEY(group_id) REFERENCES groups(id) ON DELETE CASCADE
        )
    """)

    cur.execute(
        "INSERT OR IGNORE INTO bot_settings(key, value) VALUES (?, ?)",
        ("storage_photos", "0")
    )
    if STORAGE_CHANNEL_ID is not None:
        cur.execute(
            "INSERT OR IGNORE INTO bot_settings(key, value) VALUES (?, ?)",
            ("storage_channel_id", str(STORAGE_CHANNEL_ID))
        )

    conn.commit()

    # Sync root admins into DB. They cannot be removed from the bot UI.
    for admin_id in ROOT_ADMIN_IDS:
        cur.execute(
            "INSERT OR IGNORE INTO admins(user_id, added_by) VALUES (?, ?)",
            (admin_id, admin_id)
        )
    conn.commit()
    conn.close()


def register_user(user_id, username, first_name):
    conn = get_db()
    conn.execute("""
        INSERT INTO users(user_id, username, first_name)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name
    """, (user_id, username, first_name))
    conn.commit()
    conn.close()


def get_setting(key, default=None):
    conn = get_db()
    row = conn.execute("SELECT value FROM bot_settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_db()
    conn.execute(
        "INSERT INTO bot_settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value))
    )
    conn.commit()
    conn.close()


def get_storage_channel_id():
    raw = get_setting("storage_channel_id", "")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return raw


def storage_photos_enabled():
    return get_setting("storage_photos", "0") == "1"


def get_user_language(user_id):
    conn = get_db()
    row = conn.execute("SELECT language FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row["language"] if row else "fa"


def set_user_language(user_id, language):
    language = "en" if language == "en" else "fa"
    conn = get_db()
    conn.execute(
        "INSERT INTO user_settings(user_id, language) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET language=excluded.language, updated_at=CURRENT_TIMESTAMP",
        (user_id, language)
    )
    conn.commit()
    conn.close()


def is_blocked(user_id):
    if user_id in ROOT_ADMIN_IDS:
        return False
    conn = get_db()
    row = conn.execute("SELECT 1 FROM blocked_users WHERE user_id=? LIMIT 1", (user_id,)).fetchone()
    conn.close()
    return row is not None


def block_user(user_id, blocked_by, reason=""):
    if user_id in ROOT_ADMIN_IDS or is_admin(user_id):
        return False, "ادمین‌ها و ادمین‌های اصلی قابل مسدودسازی نیستند."
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO blocked_users(user_id, blocked_by, reason) VALUES (?, ?, ?)",
        (user_id, blocked_by, reason or "")
    )
    conn.commit()
    conn.close()
    return True, "کاربر مسدود شد."


def unblock_user(user_id):
    conn = get_db()
    cur = conn.execute("DELETE FROM blocked_users WHERE user_id=?", (user_id,))
    conn.commit()
    removed = cur.rowcount > 0
    conn.close()
    return (True, "مسدودی کاربر برداشته شد.") if removed else (False, "این کاربر در لیست مسدودها نیست.")


def list_users(limit=20, offset=0):
    conn = get_db()
    rows = conn.execute(
        "SELECT u.*, CASE WHEN b.user_id IS NULL THEN 0 ELSE 1 END AS blocked "
        "FROM users u LEFT JOIN blocked_users b ON b.user_id=u.user_id "
        "ORDER BY u.created_at DESC LIMIT ? OFFSET ?",
        (limit, offset)
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    return rows, total


def list_blocked_users(limit=20, offset=0):
    conn = get_db()
    rows = conn.execute(
        "SELECT b.*, u.username, u.first_name FROM blocked_users b "
        "LEFT JOIN users u ON u.user_id=b.user_id "
        "ORDER BY b.created_at DESC LIMIT ? OFFSET ?",
        (limit, offset)
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM blocked_users").fetchone()[0]
    conn.close()
    return rows, total


def get_user_by_id(user_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row


def admin_only_root(user_id):
    return user_id in ROOT_ADMIN_IDS


def is_admin(user_id):
    if user_id in ROOT_ADMIN_IDS:
        return True
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM admins WHERE user_id=? LIMIT 1", (user_id,)
    ).fetchone()
    conn.close()
    return row is not None


def add_admin(user_id, added_by):
    if user_id in ROOT_ADMIN_IDS:
        return False, "این کاربر از قبل ادمین اصلی است."
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO admins(user_id, added_by) VALUES (?, ?)",
            (user_id, added_by)
        )
        conn.commit()
        return True, "ادمین اضافه شد."
    except sqlite3.IntegrityError:
        return False, "این کاربر از قبل ادمین است."
    finally:
        conn.close()


def remove_admin(user_id):
    if user_id in ROOT_ADMIN_IDS:
        return False, "ادمین اصلی را نمی‌توان حذف کرد."
    conn = get_db()
    cur = conn.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
    conn.commit()
    removed = cur.rowcount > 0
    conn.close()
    return (True, "ادمین حذف شد.") if removed else (False, "این کاربر ادمین نیست.")


def list_admins():
    conn = get_db()
    rows = conn.execute("""
        SELECT a.user_id, a.added_by, a.created_at,
               u.username, u.first_name
        FROM admins a
        LEFT JOIN users u ON u.user_id=a.user_id
        ORDER BY a.created_at ASC
    """).fetchall()
    conn.close()
    return rows


def list_join_channels():
    conn = get_db()
    rows = conn.execute(
        "SELECT channel_id, title, username, invite_url, created_at FROM join_channels ORDER BY created_at ASC"
    ).fetchall()
    conn.close()
    return rows


def add_join_channel(channel_id, title=None, username=None, invite_url=None):
    channel_id = str(channel_id).strip()
    if not channel_id:
        return False, "شناسه کانال خالی است."
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO join_channels(channel_id, title, username, invite_url) VALUES (?, ?, ?, ?)",
            (channel_id, title or channel_id, username or (channel_id if channel_id.startswith("@") else ""), invite_url or "")
        )
        conn.commit()
        return True, "کانال با موفقیت اضافه شد."
    except sqlite3.IntegrityError:
        return False, "این کانال از قبل در لیست است."
    finally:
        conn.close()


def update_join_channel(channel_id, title, username, invite_url):
    conn = get_db()
    cur = conn.execute(
        "UPDATE join_channels SET title=?, username=?, invite_url=? WHERE channel_id=?",
        (title or channel_id, username or "", invite_url or "", str(channel_id))
    )
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


def remove_join_channel(channel_id):
    channel_id = str(channel_id).strip()
    conn = get_db()
    cur = conn.execute("DELETE FROM join_channels WHERE channel_id=?", (channel_id,))
    conn.commit()
    removed = cur.rowcount > 0
    conn.close()
    return (True, "کانال حذف شد.") if removed else (False, "این کانال در لیست نیست.")



def create_file(owner_id, file_id=None, file_name=None, file_size=0,
                file_type="file", text_content=None):
    token = secrets.token_urlsafe(12)
    conn = get_db()
    conn.execute("""
        INSERT INTO files(
            token, owner_id, file_id, file_name, file_size,
            file_type, text_content
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        token, owner_id, file_id, file_name, file_size or 0,
        file_type, text_content
    ))
    conn.commit()
    conn.close()
    return token


def get_file_by_token(token):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM files WHERE token=? AND active=1 LIMIT 1",
        (token,)
    ).fetchone()
    conn.close()
    return row


def get_user_files(user_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM files WHERE owner_id=? ORDER BY id DESC",
        (user_id,)
    ).fetchall()
    conn.close()
    return rows


def delete_file(file_id, user_id):
    conn = get_db()
    cur = conn.execute(
        "DELETE FROM files WHERE id=? AND owner_id=?",
        (file_id, user_id)
    )
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


def increment_file_download(token):
    conn = get_db()
    conn.execute(
        "UPDATE files SET downloads=downloads+1 WHERE token=?", (token,)
    )
    conn.commit()
    conn.close()


def create_group(owner_id, title, items):
    token = secrets.token_urlsafe(12)
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO groups(token, owner_id, title) VALUES (?, ?, ?)",
        (token, owner_id, title or "مجموعه فایل")
    )
    group_id = cur.lastrowid
    for pos, item in enumerate(items, 1):
        cur.execute("""
            INSERT INTO group_items(
                group_id, position, file_id, file_name, file_size,
                file_type, text_content
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            group_id, pos, item.get("file_id"), item.get("file_name"),
            item.get("file_size", 0), item.get("file_type"),
            item.get("text_content")
        ))
    conn.commit()
    conn.close()
    return token


def get_group_by_token(token):
    conn = get_db()
    group = conn.execute(
        "SELECT * FROM groups WHERE token=? AND active=1 LIMIT 1",
        (token,)
    ).fetchone()
    if not group:
        conn.close()
        return None, []
    items = conn.execute(
        "SELECT * FROM group_items WHERE group_id=? ORDER BY position ASC",
        (group["id"],)
    ).fetchall()
    conn.close()
    return group, items


def increment_group_download(token):
    conn = get_db()
    conn.execute(
        "UPDATE groups SET downloads=downloads+1 WHERE token=?", (token,)
    )
    conn.commit()
    conn.close()


def user_stats(user_id):
    conn = get_db()
    row = conn.execute("""
        SELECT COUNT(*) files,
               COALESCE(SUM(file_size),0) size,
               COALESCE(SUM(downloads),0) downloads
        FROM files WHERE owner_id=?
    """, (user_id,)).fetchone()
    conn.close()
    return row


def global_stats():
    conn = get_db()
    users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    groups = conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
    downloads = conn.execute(
        "SELECT COALESCE(SUM(downloads),0) FROM files"
    ).fetchone()[0]
    group_downloads = conn.execute(
        "SELECT COALESCE(SUM(downloads),0) FROM groups"
    ).fetchone()[0]
    size = conn.execute(
        "SELECT COALESCE(SUM(file_size),0) FROM files"
    ).fetchone()[0]
    blocked = conn.execute("SELECT COUNT(*) FROM blocked_users").fetchone()[0]
    admins = conn.execute("SELECT COUNT(*) FROM admins").fetchone()[0]
    conn.close()
    return users, files, groups, downloads + group_downloads, size, blocked, admins


# =========================================================
# HELPERS / MEMBERSHIP
# =========================================================
def format_size(size):
    if not size:
        return "0 B"
    size = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"


def bot_link(username, kind, token):
    return f"https://t.me/{username}?start={kind}_{token}"


async def check_membership(user_id):
    channels = list_join_channels()
    if not channels:
        return True
    for row in channels:
        try:
            member = await bot.get_chat_member(row["channel_id"], user_id)
            status = getattr(member, "status", "")
            if status in ("creator", "administrator", "member"):
                continue
            if status == "restricted" and bool(getattr(member, "is_member", False)):
                continue
            return False
        except Exception as e:
            print(f"Membership check error for {row['channel_id']}: {e}")
            return False
    return True


def join_keyboard():
    rows = []
    for row in list_join_channels():
        title = row["title"] or row["channel_id"]
        username = row["username"] or ""
        invite = row["invite_url"] or ""
        url = ""
        if username.startswith("@"):
            url = f"https://t.me/{username[1:]}"
        elif invite.startswith("https://t.me/"):
            url = invite
        if url:
            rows.append([InlineKeyboardButton(text=f"📢 عضویت در {title}", url=url)])
    rows.append([InlineKeyboardButton(text="🔄 بررسی عضویت", callback_data="check_join")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "check_join")
async def check_join_callback(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    if is_blocked(user_id):
        await callback.message.answer("🚫 دسترسی شما مسدود است.")
        return
    if await check_membership(user_id):
        await callback.message.answer("✅ عضویت شما تأیید شد.", reply_markup=main_keyboard(user_id))
    else:
        await callback.message.answer("❌ هنوز در همه کانال‌های اجباری عضو نیستی.", reply_markup=join_keyboard())


def main_keyboard(user_id):
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🟢 آپلود تکی"), KeyboardButton(text="🟣 آپلود گروهی")],
            [KeyboardButton(text="🔵 فایل‌های من"), KeyboardButton(text="📊 آمار من")],
            [KeyboardButton(text="⚙️ تنظیمات"), KeyboardButton(text="👤 حساب من")],
        ], resize_keyboard=True, is_persistent=True, input_field_placeholder="یک گزینه را انتخاب کنید…"
    )


def settings_keyboard(user_id):
    # All management navigation is a ReplyKeyboard, shown directly under the chat.
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👥 لیست کاربران"), KeyboardButton(text="🚫 لیست مسدودها")],
            [KeyboardButton(text="👑 لیست ادمین‌ها"), KeyboardButton(text="➕ افزودن ادمین")],
            [KeyboardButton(text="➖ حذف ادمین"), KeyboardButton(text="🔐 عضویت اجباری")],
            [KeyboardButton(text="📊 آمار کلی"), KeyboardButton(text="📁 تنظیمات فایل‌ها")],
            [KeyboardButton(text="🌐 تغییر زبان"), KeyboardButton(text="📣 پیام همگانی")],
            [KeyboardButton(text="🔙 منوی اصلی")],
        ], resize_keyboard=True, is_persistent=True, input_field_placeholder="تنظیمات را انتخاب کنید…"
    )


def file_settings_keyboard():
    photos = "روشن 🟢" if storage_photos_enabled() else "خاموش 🔴"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📦 کانال ذخیره‌سازی")],
            [KeyboardButton(text=f"🖼 ذخیره عکس‌ها: {photos}")],
            [KeyboardButton(text="🌐 تنظیمات لینک وب")],
            [KeyboardButton(text="🔙 بازگشت به تنظیمات")],
        ], resize_keyboard=True, is_persistent=True
    )


def language_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🇮🇷 فارسی"), KeyboardButton(text="🇬🇧 English")],
            [KeyboardButton(text="🔙 بازگشت به تنظیمات")],
        ], resize_keyboard=True, is_persistent=True
    )


def join_manage_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ افزودن کانال"), KeyboardButton(text="🗑 حذف کانال")],
            [KeyboardButton(text="📋 لیست کانال‌ها"), KeyboardButton(text="🧪 تست کانال‌ها")],
            [KeyboardButton(text="🔙 بازگشت به تنظیمات")],
        ], resize_keyboard=True, is_persistent=True
    )


async def answer_access_denied(target):
    user_id = getattr(getattr(target, "from_user", None), "id", None)
    if hasattr(target, "answer"):
        try:
            await target.answer(f"⛔ دسترسی مدیریت ندارید.\n🆔 آیدی شما: <code>{user_id}</code>\n\nاگر این آیدی Root Admin است، آن را در ADMIN_IDS فایل .env قرار دهید و ربات را کامل Restart کنید.", parse_mode="HTML")
        except TypeError:
            await target.answer("⛔ دسترسی مدیریت ندارید.")


async def send_join_manage_panel(message):
    if not is_admin(message.from_user.id):
        await answer_access_denied(message); return
    rows=list_join_channels()
    if rows:
        lines=[]
        for r in rows:
            kind="عمومی 🌐" if r['username'] else ("خصوصی 🔒" if r['invite_url'] else "بدون لینک ⚠️")
            lines.append(f"• <b>{escape(r['title'] or r['channel_id'])}</b> | <code>{r['channel_id']}</code> | {kind}")
        body="\n".join(lines)
    else: body="❌ هنوز کانالی اضافه نشده."
    await message.answer("🔐 <b>عضویت اجباری</b>\n\n"+body+"\n\n⚠️ ربات باید در همه کانال‌ها Administrator باشد.",parse_mode="HTML",reply_markup=join_manage_keyboard())

async def send_settings_panel(message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ شما ادمین نیستید.\n🆔 آیدی شما: <code>%s</code>" % message.from_user.id,parse_mode="HTML"); return
    await message.answer("⚙️ <b>تنظیمات مدیریت</b>\n\nیک بخش را انتخاب کن:",parse_mode="HTML",reply_markup=settings_keyboard(message.from_user.id))

@dp.callback_query(F.data == "settings")
async def settings_button(callback: CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        await answer_access_denied(callback.message)
        return
    await send_settings_panel(callback.message)


@dp.callback_query(F.data == "admin")
async def admin_panel(callback: CallbackQuery):
    # Old callback is redirected to Settings for backward compatibility.
    await callback.answer()
    if not is_admin(callback.from_user.id):
        await answer_access_denied(callback.message)
        return
    await send_settings_panel(callback.message)


@dp.callback_query(F.data.startswith("users_list:"))
async def users_list_button(callback: CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        await answer_access_denied(callback.message)
        return
    try:
        page = max(0, int(callback.data.split(":", 1)[1]))
    except ValueError:
        page = 0
    limit = 15
    rows, total = list_users(limit, page * limit)
    if not rows:
        await callback.message.answer("👥 هنوز کاربری ثبت نشده است.")
        return
    text = f"👥 <b>لیست کاربران</b>\nصفحه {page + 1} از {(total + limit - 1) // limit}\n\n"
    buttons = []
    for row in rows:
        name = escape(row["first_name"] or "بدون نام")
        username = f"@{escape(row['username'])}" if row["username"] else "بدون username"
        status = "🚫" if row["blocked"] else "🟢"
        text += f"{status} <b>{name}</b> • <code>{row['user_id']}</code> • {username}\n"
        if not row["blocked"] and not is_admin(row["user_id"]):
            buttons.append([InlineKeyboardButton(text=f"🚫 مسدود کردن {row['user_id']}", callback_data=f"block_user:{row['user_id']}:{page}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"users_list:{page - 1}"))
    if (page + 1) * limit < total:
        nav.append(InlineKeyboardButton(text="بعدی ➡️", callback_data=f"users_list:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔙 تنظیمات", callback_data="settings")])
    await callback.message.answer(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("block_user:"))
async def block_user_button(callback: CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        await answer_access_denied(callback.message)
        return
    parts = callback.data.split(":")
    try:
        target = int(parts[1])
        page = int(parts[2]) if len(parts) > 2 else 0
    except (ValueError, IndexError):
        await callback.message.answer("❌ شناسه کاربر نامعتبر است.")
        return
    ok, text = block_user(target, callback.from_user.id)
    await callback.message.answer(("✅ " if ok else "❌ ") + text)
    if ok:
        await callback.message.answer("🔄 کاربر مسدود شد. لیست کاربران را دوباره باز کن تا وضعیت جدید را ببینی.")


@dp.callback_query(F.data.startswith("blocked_list:"))
async def blocked_list_button(callback: CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        await answer_access_denied(callback.message)
        return
    try:
        page = max(0, int(callback.data.split(":", 1)[1]))
    except ValueError:
        page = 0
    limit = 15
    rows, total = list_blocked_users(limit, page * limit)
    if not rows:
        await callback.message.answer("🚫 لیست کاربران مسدود خالی است.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 تنظیمات", callback_data="settings")]]))
        return
    text = f"🚫 <b>لیست کاربران مسدود</b>\nصفحه {page + 1} از {(total + limit - 1) // limit}\n\n"
    buttons = []
    for row in rows:
        name = escape(row["first_name"] or "بدون نام")
        username = f"@{escape(row['username'])}" if row["username"] else "بدون username"
        text += f"🚫 <b>{name}</b> • <code>{row['user_id']}</code> • {username}\n"
        buttons.append([InlineKeyboardButton(text=f"♻️ رفع مسدودی {row['user_id']}", callback_data=f"unblock_user:{row['user_id']}:{page}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"blocked_list:{page - 1}"))
    if (page + 1) * limit < total:
        nav.append(InlineKeyboardButton(text="بعدی ➡️", callback_data=f"blocked_list:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔙 تنظیمات", callback_data="settings")])
    await callback.message.answer(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("unblock_user:"))
async def unblock_user_button(callback: CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        await answer_access_denied(callback.message)
        return
    parts = callback.data.split(":")
    try:
        target = int(parts[1])
    except (ValueError, IndexError):
        await callback.message.answer("❌ شناسه کاربر نامعتبر است.")
        return
    ok, text = unblock_user(target)
    await callback.message.answer(("✅ " if ok else "❌ ") + text)


@dp.callback_query(F.data == "file_settings")
async def file_settings_button(callback: CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        await answer_access_denied(callback.message)
        return
    storage = get_storage_channel_id()
    await callback.message.answer(
        "📁 <b>تنظیمات فایل‌ها</b>\n\n"
        f"📦 کانال ذخیره‌سازی: <code>{escape(str(storage) if storage else 'تنظیم نشده')}</code>\n"
        f"🖼 ذخیره عکس‌ها: {'روشن' if storage_photos_enabled() else 'خاموش'}\n"
        f"🌐 آدرس وب: <code>{escape(BASE_URL)}</code>",
        parse_mode="HTML",
        reply_markup=file_settings_keyboard()
    )


@dp.callback_query(F.data == "storage_channel")
async def storage_channel_button(callback: CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        await answer_access_denied(callback.message)
        return
    admin_actions[callback.from_user.id] = "storage_channel"
    await callback.message.answer(
        "📦 شناسه کانال ذخیره‌سازی را بفرست.\n\n"
        "مثال: <code>-1001234567890</code>\n"
        "برای غیرفعال‌کردن: <code>off</code>\n\n"
        "⚠️ ربات باید در کانال ادمین باشد.", parse_mode="HTML"
    )


@dp.callback_query(F.data == "toggle_storage_photos")
async def toggle_storage_photos(callback: CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        await answer_access_denied(callback.message)
        return
    set_setting("storage_photos", "0" if storage_photos_enabled() else "1")
    await callback.message.answer("✅ تنظیم ذخیره عکس‌ها تغییر کرد.", reply_markup=file_settings_keyboard())


@dp.callback_query(F.data == "web_settings")
async def web_settings_button(callback: CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        await answer_access_denied(callback.message)
        return
    await callback.message.answer(
        f"🌐 <b>تنظیمات وب</b>\n\nآدرس فعلی: <code>{escape(BASE_URL)}</code>\n\n"
        "برای تغییر BASE_URL و WEB_PORT باید فایل .env را ویرایش و ربات را ری‌استارت کنی.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 تنظیمات فایل‌ها", callback_data="file_settings")]])
    )


@dp.callback_query(F.data == "language_settings")
async def language_settings_button(callback: CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        await answer_access_denied(callback.message)
        return
    current = "فارسی 🇮🇷" if get_user_language(callback.from_user.id) == "fa" else "English 🇬🇧"
    await callback.message.answer(f"🌐 <b>تغییر زبان</b>\n\nزبان فعلی: {current}", parse_mode="HTML", reply_markup=language_keyboard())


@dp.callback_query(F.data.startswith("lang:"))
async def language_change_button(callback: CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        await answer_access_denied(callback.message)
        return
    lang = callback.data.split(":", 1)[1]
    set_user_language(callback.from_user.id, lang)
    await callback.message.answer("✅ زبان تغییر کرد.", reply_markup=admin_keyboard(callback.from_user.id))


@dp.callback_query(F.data == "join_manage")
async def join_manage_button(callback: CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        await answer_access_denied(callback.message)
        return
    await send_join_manage_panel(callback.message)


@dp.callback_query(F.data == "join_add")
async def join_add_button(callback: CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        await answer_access_denied(callback.message)
        return
    admin_actions[callback.from_user.id] = "join_add"
    await callback.message.answer(
        "➕ شناسه کانال را بفرست.\n\nمثال عمومی: <code>@mychannel</code>\nمثال خصوصی: <code>-1001234567890</code>\n\n"
        "⚠️ ربات باید در کانال ادمین باشد تا بتواند عضویت را بررسی کند.",
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "join_remove")
async def join_remove_button(callback: CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        await answer_access_denied(callback.message)
        return
    admin_actions[callback.from_user.id] = "join_remove"
    await callback.message.answer("➖ شناسه همان کانال را بفرست، مثلاً <code>@mychannel</code> یا <code>-100123...</code>", parse_mode="HTML")


@dp.callback_query(F.data == "join_list")
async def join_list_button(callback: CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        await answer_access_denied(callback.message)
        return
    await send_join_manage_panel(callback.message)


@dp.callback_query(F.data == "admin_add")
async def admin_add_button(callback: CallbackQuery):
    await callback.answer()
    if not admin_only_root(callback.from_user.id):
        await callback.message.answer("⛔ فقط ادمین اصلی اجازه افزودن ادمین دارد.")
        return
    admin_actions[callback.from_user.id] = "add"
    await callback.message.answer(
        "➕ <b>افزودن ادمین</b>\n\nآیدی عددی کاربر را بفرست.\n"
        "مثال: <code>123456789</code>\n\nلغو: /cancel", parse_mode="HTML"
    )


@dp.callback_query(F.data == "admin_remove")
async def admin_remove_button(callback: CallbackQuery):
    await callback.answer()
    if not admin_only_root(callback.from_user.id):
        await callback.message.answer("⛔ فقط ادمین اصلی اجازه حذف ادمین دارد.")
        return
    admin_actions[callback.from_user.id] = "remove"
    await callback.message.answer(
        "➖ <b>حذف ادمین</b>\n\nآیدی عددی ادمین را بفرست.\n"
        "ادمین‌های اصلی داخل ADMIN_IDS قابل حذف نیستند.\n\nلغو: /cancel", parse_mode="HTML"
    )


@dp.callback_query(F.data == "admin_list")
async def admin_list_button(callback: CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    rows = list_admins()
    if not rows:
        await callback.message.answer("👥 لیست ادمین‌ها خالی است.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 تنظیمات", callback_data="settings")]]))
        return
    text = "👥 <b>لیست ادمین‌ها</b>\n\n"
    buttons = []
    for i, row in enumerate(rows, 1):
        username = f"@{escape(row['username'])}" if row["username"] else "بدون username"
        root = " 👑 اصلی" if row["user_id"] in ROOT_ADMIN_IDS else ""
        text += f"{i}. <code>{row['user_id']}</code> • {username}{root}\n"
        if row["user_id"] not in ROOT_ADMIN_IDS:
            buttons.append([InlineKeyboardButton(text=f"➖ حذف {row['user_id']}", callback_data=f"admin_remove_direct:{row['user_id']}")])
    buttons.append([InlineKeyboardButton(text="🔙 تنظیمات", callback_data="settings")])
    await callback.message.answer(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("admin_remove_direct:"))
async def admin_remove_direct_button(callback: CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    try:
        target = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.message.answer("❌ شناسه نامعتبر.")
        return
    ok, text = remove_admin(target)
    await callback.message.answer(("✅ " if ok else "❌ ") + text)


@dp.callback_query(F.data == "admin_stats")
async def admin_stats_button(callback: CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        return
    users, files, groups, downloads, size, blocked, admins = global_stats()
    await callback.message.answer(
        "📊 <b>آمار کلی</b>\n\n"
        f"👥 کاربران: {users}\n"
        f"🚫 مسدودها: {blocked}\n"
        f"👑 ادمین‌ها: {admins}\n"
        f"📁 فایل‌ها: {files}\n"
        f"📦 گروه‌ها: {groups}\n"
        f"⬇️ دانلودها: {downloads}\n"
        f"💾 حجم: {format_size(size)}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 تنظیمات", callback_data="settings")]])
    )


@dp.message(Command("cancel"))
async def cancel_handler(message: Message):
    admin_actions.pop(message.from_user.id, None)
    if upload_modes.get(message.from_user.id) == "group":
        upload_modes.pop(message.from_user.id, None)
        upload_modes.pop(f"items:{message.from_user.id}", None)
    else:
        upload_modes.pop(message.from_user.id, None)
    await message.answer("❌ عملیات لغو شد.")


# =========================================================
# CONTENT / UPLOAD
# =========================================================
def extract_item(message: Message):
    if message.document:
        d = message.document
        return {
            "file_id": d.file_id,
            "file_name": d.file_name or "file",
            "file_size": d.file_size or 0,
            "file_type": "document",
            "text_content": None
        }
    if message.photo:
        p = message.photo[-1]
        return {
            "file_id": p.file_id,
            "file_name": "photo.jpg",
            "file_size": p.file_size or 0,
            "file_type": "photo",
            "text_content": None
        }
    if message.video:
        v = message.video
        return {
            "file_id": v.file_id,
            "file_name": v.file_name or "video.mp4",
            "file_size": v.file_size or 0,
            "file_type": "video",
            "text_content": None
        }
    if message.audio:
        a = message.audio
        return {
            "file_id": a.file_id,
            "file_name": a.file_name or "audio.mp3",
            "file_size": a.file_size or 0,
            "file_type": "audio",
            "text_content": None
        }
    if message.voice:
        v = message.voice
        return {
            "file_id": v.file_id,
            "file_name": "voice.ogg",
            "file_size": v.file_size or 0,
            "file_type": "voice",
            "text_content": None
        }
    if message.animation:
        a = message.animation
        return {
            "file_id": a.file_id,
            "file_name": a.file_name or "animation.gif",
            "file_size": a.file_size or 0,
            "file_type": "animation",
            "text_content": None
        }
    if message.text and not message.text.startswith("/"):
        return {
            "file_id": None,
            "file_name": "text.txt",
            "file_size": len(message.text.encode("utf-8")),
            "file_type": "text",
            "text_content": message.text
        }
    return None


async def handle_content(message: Message):
    user_id = message.from_user.id

    register_user(user_id, message.from_user.username, message.from_user.first_name)
    if is_blocked(user_id):
        await message.answer("🚫 دسترسی شما به این ربات مسدود شده است.")
        return

    # Admin management input takes priority.
    action = admin_actions.get(user_id)
    if action:
        if not is_admin(user_id):
            admin_actions.pop(user_id, None)
            return
        raw = (message.text or "").strip()
        if action in ("add", "remove"):
            if not admin_only_root(user_id):
                admin_actions.pop(user_id, None)
                await message.answer("⛔ فقط ادمین اصلی اجازه مدیریت ادمین‌ها را دارد.")
                return
            # Accept numeric ID or a reply to the target user's message.
            if message.reply_to_message and message.reply_to_message.from_user:
                target = message.reply_to_message.from_user.id
            elif raw.isdigit():
                target = int(raw)
            else:
                await message.answer("❌ آیدی عددی بفرست یا پیام همان کاربر را Reply کن.")
                return
        else:
            target = raw
        if action == "add":
            ok, text = add_admin(target, user_id)
        elif action == "remove":
            ok, text = remove_admin(target)
        elif action == "join_add":
            try:
                chat = await bot.get_chat(raw)
                me = await bot.get_me()
                bot_member = await bot.get_chat_member(chat.id, me.id)
                if bot_member.status not in ("administrator", "creator"):
                    ok, text = False, "❌ ربات در این کانال Administrator نیست."
                else:
                    username = f"@{chat.username}" if getattr(chat, "username", None) else ""
                    invite_url = ""
                    if not username:
                        try:
                            invite = await bot.create_chat_invite_link(chat.id, name="Force Join")
                            invite_url = invite.invite_link
                        except Exception:
                            invite_url = ""
                    ok, text = add_join_channel(str(chat.id), chat.title, username, invite_url)
            except Exception as e:
                ok, text = False, f"❌ کانال معتبر نیست یا دسترسی ربات کافی نیست: {e}"
        elif action == "join_remove":
            ok, text = remove_join_channel(raw)
        elif action == "storage_channel":
            if raw.lower() in {"off", "none", "0"}:
                set_setting("storage_channel_id", "")
                ok, text = True, "کانال ذخیره‌سازی غیرفعال شد."
            else:
                try:
                    channel_id = int(raw)
                except ValueError:
                    channel_id = raw
                set_setting("storage_channel_id", channel_id)
                ok, text = True, "کانال ذخیره‌سازی ذخیره شد."
        elif action == "broadcast":
            # Broadcast the original Telegram message so media/captions are preserved.
            admin_actions.pop(user_id, None)
            conn = get_db()
            users = conn.execute("SELECT user_id FROM users").fetchall()
            conn.close()
            sent = 0
            failed = 0
            for row in users:
                target_id = row["user_id"]
                if target_id == user_id or is_blocked(target_id):
                    continue
                try:
                    await bot.copy_message(
                        chat_id=target_id,
                        from_chat_id=message.chat.id,
                        message_id=message.message_id
                    )
                    sent += 1
                except Exception:
                    failed += 1
            await message.answer(f"📢 ارسال همگانی تمام شد.\n\n✅ ارسال موفق: {sent}\n❌ ناموفق: {failed}")
            return
        else:
            ok, text = False, "عملیات نامعتبر است."
        admin_actions.pop(user_id, None)
        await message.answer(("✅ " if ok else "❌ ") + text)
        if action.startswith("join_"):
            await send_join_manage_panel(message)
        elif action == "storage_channel":
            await file_settings_button.__wrapped__(message) if False else message.answer("📁 برای مشاهده تنظیمات فایل‌ها، دوباره «تنظیمات → تنظیمات فایل‌ها» را باز کن.")
        return

    if not is_admin(user_id):
        # Non-admins never get upload controls and cannot upload.
        if message.text and message.text.startswith("/"):
            return
        if not await check_membership(user_id):
            await message.answer(
                "🔒 برای استفاده از ربات ابتدا در کانال عضو شو:",
                reply_markup=join_keyboard()
            )
        else:
            await message.answer(user_text(), parse_mode="HTML")
        return

    mode = upload_modes.get(user_id)
    item = extract_item(message)
    if not item:
        return

    # Group mode: keep everything inside one group.
    if mode == "group":
        upload_modes.setdefault(f"items:{user_id}", []).append(item)
        count = len(upload_modes[f"items:{user_id}"])
        # Never send photos to Storage.
        await maybe_storage_copy(message, allow=True)
        await message.answer(
            f"✅ آیتم {count} به مجموعه اضافه شد.\n"
            "ادامه بده یا «ساخت لینک» را بزن."
        )
        return

    # Single mode, or no mode: save as a single item for admins.
    upload_modes.pop(user_id, None)
    await maybe_storage_copy(message, allow=True)
    token = create_file(
        owner_id=user_id,
        file_id=item["file_id"],
        file_name=item["file_name"],
        file_size=item["file_size"],
        file_type=item["file_type"],
        text_content=item["text_content"]
    )
    me = await bot.get_me()
    tg_link = bot_link(me.username, "file", token)
    web_link = f"{BASE_URL}/f/{token}"
    await message.answer(
        "✅ <b>آپلود تکی با موفقیت انجام شد!</b>\n\n"
        f"📁 {escape(item['file_name'])}\n"
        f"💾 {format_size(item['file_size'])}\n\n"
        f"🤖 <b>لینک ربات:</b>\n{tg_link}\n\n"
        f"🌐 <b>لینک وب:</b>\n{web_link}",
        parse_mode="HTML"
    )



@dp.message(Command("done"))
async def done_handler(message: Message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return await message.answer("⛔ فقط ادمین‌ها می‌توانند آپلود گروهی انجام دهند.")
    if upload_modes.get(user_id) != "group":
        return await message.answer("ℹ️ در حال حاضر آپلود گروهی فعالی نداری.", reply_markup=main_keyboard(user_id))
    items = upload_modes.get(f"items:{user_id}", [])
    if not items:
        return await message.answer("❌ هنوز هیچ فایلی به مجموعه اضافه نکردی.")
    token = create_group(user_id, "مجموعه فایل", items)
    upload_modes.pop(user_id, None)
    upload_modes.pop(f"items:{user_id}", None)
    me = await bot.get_me()
    tg_link = bot_link(me.username, "group", token)
    web_link = f"{BASE_URL}/g/{token}"
    await message.answer(
        "✅ <b>مجموعه ساخته شد!</b>\n\n"
        f"📁 تعداد آیتم‌ها: {len(items)}\n\n"
        f"🤖 لینک ربات: {tg_link}\n"
        f"🌐 لینک وب: {web_link}",
        parse_mode="HTML", reply_markup=main_keyboard(user_id)
    )


# =========================================================
# REPLY-KEYBOARD MENU HANDLERS
# =========================================================
async def show_main_menu(message: Message):
    await message.answer("🏠 <b>منوی اصلی</b>\n\nیکی از گزینه‌های زیر را انتخاب کن:", parse_mode="HTML", reply_markup=main_keyboard(message.from_user.id))

@dp.message(Command("start"))
async def start_handler(message: Message):
    user = message.from_user
    register_user(user.id, user.username, user.first_name)
    admin_actions.pop(user.id, None)
    upload_modes.pop(user.id, None)
    if is_blocked(user.id):
        await message.answer("🚫 دسترسی شما به این ربات مسدود شده است.")
        return
    if not is_admin(user.id) and not await check_membership(user.id):
        await message.answer("🔒 برای استفاده از ربات ابتدا در کانال‌های زیر عضو شو:", reply_markup=join_keyboard())
        return
    await message.answer("👋 خوش آمدی!", reply_markup=main_keyboard(user.id))

@dp.message(Command("help"))
async def help_handler(message: Message):
    await message.answer("ℹ️ <b>راهنما</b>\n\n🟢 آپلود تکی: یک فایل ارسال کن.\n🟣 آپلود گروهی: چند فایل بفرست و در پایان /done را بزن.\n🔵 فایل‌های من: فایل‌های ذخیره‌شده را ببین.\n⚙️ تنظیمات: فقط برای ادمین‌ها.", parse_mode="HTML", reply_markup=main_keyboard(message.from_user.id))

@dp.message(F.text == "⚙️ تنظیمات")
async def settings_reply_handler(message: Message):
    await send_settings_panel(message)

@dp.message(F.text == "🔙 منوی اصلی")
@dp.message(F.text == "🔙 بازگشت به تنظیمات")
async def back_reply_handler(message: Message):
    if message.text == "🔙 منوی اصلی":
        await show_main_menu(message)
    else:
        await send_settings_panel(message)

@dp.message(F.text == "👥 لیست کاربران")
async def users_reply_handler(message: Message):
    if not is_admin(message.from_user.id): return await answer_access_denied(message)
    rows,total=list_users(20,0)
    text="👥 <b>لیست کاربران</b>\n\n"
    if not rows: text += "هنوز کاربری ثبت نشده است."
    else:
        for r in rows:
            text += f"{'🚫' if r['blocked'] else '🟢'} <b>{escape(r['first_name'] or 'بدون نام')}</b> • <code>{r['user_id']}</code> • @{escape(r['username'] or '-') }\n"
    await message.answer(text,parse_mode="HTML",reply_markup=settings_keyboard(message.from_user.id))

@dp.message(F.text == "🚫 لیست مسدودها")
async def blocked_reply_handler(message: Message):
    if not is_admin(message.from_user.id): return await answer_access_denied(message)
    rows,total=list_blocked_users(20,0)
    text="🚫 <b>لیست مسدودها</b>\n\n"
    if not rows: text += "لیست خالی است."
    else:
        for r in rows: text += f"• <b>{escape(r['first_name'] or 'بدون نام')}</b> • <code>{r['user_id']}</code>\n"
    await message.answer(text,parse_mode="HTML",reply_markup=settings_keyboard(message.from_user.id))

@dp.message(F.text == "👑 لیست ادمین‌ها")
async def admins_reply_handler(message: Message):
    if not is_admin(message.from_user.id): return await answer_access_denied(message)
    rows=list_admins()
    text="👑 <b>لیست ادمین‌ها</b>\n\n"
    for r in rows:
        text += f"• <code>{r['user_id']}</code> {'👑 اصلی' if r['user_id'] in ROOT_ADMIN_IDS else '🛡 ادمین'}\n"
    await message.answer(text or "لیست خالی است.",parse_mode="HTML",reply_markup=settings_keyboard(message.from_user.id))

@dp.message(F.text == "➕ افزودن ادمین")
async def add_admin_reply_handler(message: Message):
    if not admin_only_root(message.from_user.id): return await message.answer("⛔ فقط ادمین اصلی اجازه افزودن ادمین دارد.")
    admin_actions[message.from_user.id]="add"
    await message.answer("➕ آیدی عددی کاربر را بفرست یا پیام همان کاربر را Reply کن.\nلغو: /cancel",reply_markup=settings_keyboard(message.from_user.id))

@dp.message(F.text == "➖ حذف ادمین")
async def remove_admin_reply_handler(message: Message):
    if not admin_only_root(message.from_user.id): return await message.answer("⛔ فقط ادمین اصلی اجازه حذف ادمین دارد.")
    admin_actions[message.from_user.id]="remove"
    await message.answer("➖ آیدی ادمین را بفرست یا پیام همان کاربر را Reply کن.\nلغو: /cancel",reply_markup=settings_keyboard(message.from_user.id))

@dp.message(F.text == "🔐 عضویت اجباری")
async def join_manage_reply_handler(message: Message):
    await send_join_manage_panel(message)

@dp.message(F.text == "📋 لیست کانال‌ها")
async def join_list_reply_handler(message: Message):
    await send_join_manage_panel(message)

@dp.message(F.text == "➕ افزودن کانال")
async def join_add_reply_handler(message: Message):
    if not is_admin(message.from_user.id): return await answer_access_denied(message)
    admin_actions[message.from_user.id]="join_add"
    await message.answer("➕ @username کانال یا شناسه عددی مثل -1001234567890 را بفرست.\n⚠️ ربات باید Administrator کانال باشد.\nلغو: /cancel",reply_markup=join_manage_keyboard())

@dp.message(F.text == "🗑 حذف کانال")
async def join_remove_reply_handler(message: Message):
    if not is_admin(message.from_user.id): return await answer_access_denied(message)
    admin_actions[message.from_user.id]="join_remove"
    await message.answer("🗑 @username یا شناسه کانالی که می‌خواهی حذف شود را بفرست.\nلغو: /cancel",reply_markup=join_manage_keyboard())

@dp.message(F.text == "🧪 تست کانال‌ها")
async def join_test_reply_handler(message: Message):
    if not is_admin(message.from_user.id): return await answer_access_denied(message)
    rows=list_join_channels(); lines=[]
    me=await bot.get_me()
    for r in rows:
        try:
            chat=await bot.get_chat(r['channel_id'])
            member=await bot.get_chat_member(chat.id,me.id)
            ok=member.status in ("administrator","creator")
            lines.append(f"{'✅' if ok else '❌'} {escape(chat.title or str(chat.id))} — ربات: {member.status}")
        except Exception as e:
            lines.append(f"❌ {escape(r['title'] or str(r['channel_id']))} — خطا: {escape(str(e)[:120])}")
    await message.answer("🧪 <b>نتیجه تست کانال‌ها</b>\n\n" + ("\n".join(lines) if lines else "لیست خالی است."),parse_mode="HTML",reply_markup=join_manage_keyboard())

@dp.message(F.text == "📊 آمار کلی")
async def global_stats_reply_handler(message: Message):
    if not is_admin(message.from_user.id): return await answer_access_denied(message)
    users,files,groups,downloads,size,blocked,admins=global_stats()
    await message.answer(f"📊 <b>آمار کلی</b>\n\n👥 کاربران: {users}\n🚫 مسدودها: {blocked}\n👑 ادمین‌ها: {admins}\n📁 فایل‌ها: {files}\n📦 گروه‌ها: {groups}\n⬇️ دانلودها: {downloads}\n💾 حجم: {format_size(size)}",parse_mode="HTML",reply_markup=settings_keyboard(message.from_user.id))

@dp.message(F.text == "📁 تنظیمات فایل‌ها")
async def file_settings_reply_handler(message: Message):
    if not is_admin(message.from_user.id): return await answer_access_denied(message)
    storage=get_storage_channel_id()
    await message.answer(f"📁 <b>تنظیمات فایل‌ها</b>\n\n📦 کانال ذخیره‌سازی: <code>{escape(str(storage) if storage else 'تنظیم نشده')}</code>\n🖼 ذخیره عکس‌ها: {'روشن' if storage_photos_enabled() else 'خاموش'}\n🌐 آدرس وب: <code>{escape(BASE_URL)}</code>",parse_mode="HTML",reply_markup=file_settings_keyboard())

@dp.message(F.text == "📦 کانال ذخیره‌سازی")
async def storage_reply_handler(message: Message):
    if not is_admin(message.from_user.id): return await answer_access_denied(message)
    admin_actions[message.from_user.id]="storage_channel"
    await message.answer("📦 شناسه کانال ذخیره‌سازی را بفرست (مثلاً -100123...) یا برای خاموش کردن <code>off</code> بنویس.",parse_mode="HTML")

@dp.message(F.text.startswith("🖼 ذخیره عکس‌ها:"))
async def toggle_photos_reply_handler(message: Message):
    if not is_admin(message.from_user.id): return await answer_access_denied(message)
    set_setting("storage_photos","0" if storage_photos_enabled() else "1")
    await message.answer("✅ وضعیت ذخیره عکس‌ها تغییر کرد.",reply_markup=file_settings_keyboard())

@dp.message(F.text == "🌐 تغییر زبان")
async def language_reply_handler(message: Message):
    if not is_admin(message.from_user.id): return await answer_access_denied(message)
    await message.answer("🌐 زبان را انتخاب کن:",reply_markup=language_keyboard())

@dp.message(F.text == "🇮🇷 فارسی")
async def fa_reply_handler(message: Message):
    set_user_language(message.from_user.id,"fa"); await message.answer("✅ زبان فارسی فعال شد.",reply_markup=settings_keyboard(message.from_user.id))

@dp.message(F.text == "🇬🇧 English")
async def en_reply_handler(message: Message):
    set_user_language(message.from_user.id,"en"); await message.answer("✅ English enabled.",reply_markup=settings_keyboard(message.from_user.id))

@dp.message(F.text == "📣 پیام همگانی")
async def broadcast_reply_handler(message: Message):
    if not is_admin(message.from_user.id): return await answer_access_denied(message)
    admin_actions[message.from_user.id]="broadcast"
    await message.answer("📣 پیام، عکس، ویدیو یا فایل موردنظر را در پیام بعدی بفرست. همان پیام برای کاربران ارسال می‌شود.\nلغو: /cancel")

@dp.message(F.text == "🟢 آپلود تکی")
async def upload_one_reply_handler(message: Message):
    if not is_admin(message.from_user.id): return await message.answer("⛔ فقط ادمین‌ها می‌توانند فایل آپلود کنند.")
    upload_modes[message.from_user.id]="single"; await message.answer("🟢 فایل را بفرست.")

@dp.message(F.text == "🟣 آپلود گروهی")
async def upload_group_reply_handler(message: Message):
    if not is_admin(message.from_user.id): return await message.answer("⛔ فقط ادمین‌ها می‌توانند فایل آپلود کنند.")
    upload_modes[message.from_user.id]="group"; upload_modes[f"items:{message.from_user.id}"]=[]; await message.answer("🟣 فایل‌ها را یکی‌یکی بفرست و در پایان /done را بزن.")

@dp.message(F.text == "🔵 فایل‌های من")
async def my_files_reply_handler(message: Message):
    if not is_admin(message.from_user.id): return await message.answer("⛔ فقط ادمین‌ها می‌توانند فایل‌ها را ببینند.")
    rows=get_user_files(message.from_user.id)
    if not rows: return await message.answer("📁 هنوز فایلی نداری.",reply_markup=main_keyboard(message.from_user.id))
    text="📁 <b>فایل‌های من</b>\n\n"+"\n".join(f"• {escape(r['file_name'] or 'file')} — {format_size(r['file_size'])}" for r in rows[:30])
    await message.answer(text,parse_mode="HTML",reply_markup=main_keyboard(message.from_user.id))

@dp.message(F.text == "📊 آمار من")
async def my_stats_reply_handler(message: Message):
    if not is_admin(message.from_user.id): return await message.answer("⛔ فقط ادمین‌ها می‌توانند آمار را ببینند.")
    r=user_stats(message.from_user.id)
    await message.answer(f"📊 <b>آمار من</b>\n\n📁 فایل‌ها: {r['files']}\n⬇️ دانلودها: {r['downloads']}\n💾 حجم: {format_size(r['size'])}",parse_mode="HTML",reply_markup=main_keyboard(message.from_user.id))

@dp.message(F.text == "👤 حساب من")
async def account_reply_handler(message: Message):
    await message.answer(f"👤 <b>حساب شما</b>\n\n🆔 <code>{message.from_user.id}</code>\n👤 @{escape(message.from_user.username or '-') }\n🛡 وضعیت: {'ادمین' if is_admin(message.from_user.id) else 'کاربر'}",parse_mode="HTML",reply_markup=main_keyboard(message.from_user.id))

@dp.message()
async def catch_all_message(message: Message):
    await handle_content(message)


# =========================================================
# STORAGE CHANNEL DETECTOR
# =========================================================
@dp.channel_post()
async def channel_post_handler(message: Message):
    print(f"📦 Channel post detected: {message.chat.id} / {message.chat.title}")


# =========================================================
# WEB
# =========================================================
async def home(request: web.Request):
    return web.Response(
        text="""
<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Telegram Uploader</title></head>
<body style="background:#101114;color:#fff;font-family:Arial;text-align:center;padding:90px">
<h1>🤖 Telegram Uploader</h1><p>Online ✅</p>
</body></html>
""",
        content_type="text/html"
    )


async def download_page(request: web.Request):
    token = request.match_info["token"]
    row = get_file_by_token(token)
    if not row:
        return web.Response(text="File not found", status=404)

    name = escape(row["file_name"] or "file")
    size = format_size(row["file_size"])
    if row["file_type"] == "text":
        body = f"<pre style='white-space:pre-wrap;text-align:left'>{escape(row['text_content'] or '')}</pre>"
    else:
        body = f"<p>💾 {size}<br>⬇️ {row['downloads']}</p><a href='/download/{token}'>⬇️ دانلود فایل</a>"

    html = f"""
<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name}</title></head>
<body style="margin:0;min-height:100vh;background:linear-gradient(135deg,#101114,#1d2028);color:#fff;font-family:Arial;display:flex;align-items:center;justify-content:center">
<div style="width:90%;max-width:650px;padding:35px;background:#1b1e25;border-radius:24px;text-align:center">
<div style="font-size:60px">📁</div><h1>{name}</h1>{body}
</div></body></html>
"""
    return web.Response(text=html, content_type="text/html")


async def group_page(request: web.Request):
    token = request.match_info["token"]
    group, items = get_group_by_token(token)
    if not group:
        return web.Response(text="Group not found", status=404)
    rows = []
    for i, item in enumerate(items, 1):
        name = escape(item["file_name"] or ("متن" if item["file_type"] == "text" else "file"))
        if item["file_type"] == "text":
            rows.append(f"<div style='padding:12px;background:#242833;border-radius:12px;margin:8px 0'><b>{i}. {name}</b><pre style='white-space:pre-wrap;text-align:left'>{escape(item['text_content'] or '')}</pre></div>")
        else:
            rows.append(f"<div style='padding:12px;background:#242833;border-radius:12px;margin:8px 0'><b>{i}. {name}</b></div>")
    html = f"""
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(group['title'] or 'Group')}</title></head>
<body style="background:#101114;color:#fff;font-family:Arial;padding:30px">
<div style="max-width:700px;margin:auto;background:#1b1e25;border-radius:24px;padding:25px">
<h1>📦 {escape(group['title'] or 'مجموعه فایل')}</h1>
<p>📁 {len(items)} آیتم</p>
{''.join(rows)}
<p><a style="display:inline-block;padding:14px 25px;border-radius:12px;background:#238636;color:white;text-decoration:none" href="https://t.me/{(await bot.get_me()).username}?start=group_{token}">🤖 دریافت در ربات</a></p>
</div></body></html>
"""
    return web.Response(text=html, content_type="text/html")


async def download_file(request: web.Request):
    token = request.match_info["token"]
    row = get_file_by_token(token)
    if not row or row["file_type"] == "text":
        return web.Response(text="File not downloadable", status=404)
    try:
        file_info = await bot.get_file(row["file_id"])
        if not file_info.file_path:
            return web.Response(text="Telegram file unavailable", status=500)

        telegram_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
        response = web.StreamResponse(
            status=200,
            headers={"Content-Disposition": f'attachment; filename="{row["file_name"] or "file"}"'}
        )
        await response.prepare(request)
        increment_file_download(token)
        async for chunk in bot.session.stream_content(telegram_url):
            await response.write(chunk)
        await response.write_eof()
        return response
    except Exception as error:
        print("DOWNLOAD ERROR:", error)
        return web.Response(text="Download failed", status=500)


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", home)
    app.router.add_get("/f/{token}", download_page)
    app.router.add_get("/download/{token}", download_file)
    app.router.add_get("/g/{token}", group_page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()
    print(f"🌐 Web server running on http://127.0.0.1:{WEB_PORT}")


# =========================================================
# BOT COMMAND MENU
# =========================================================
async def set_commands():
    await bot.set_my_commands([
        BotCommand(command="start", description="شروع"),
        BotCommand(command="help", description="راهنما"),
        BotCommand(command="upload", description="آپلود تکی"),
        BotCommand(command="group", description="آپلود گروهی"),
        BotCommand(command="done", description="پایان آپلود گروهی"),
        BotCommand(command="files", description="فایل‌های من"),
        BotCommand(command="stats", description="آمار"),
        BotCommand(command="me", description="حساب من"),
        BotCommand(command="admin", description="تنظیمات"),
    ])


# =========================================================
# MAIN
# =========================================================
async def main():
    init_db()
    print("=" * 55)
    print("🤖 Telegram Uploader v9")
    print(f"🔌 Proxy: {PROXY_URL}")
    print(f"📢 Join channel: {JOIN_CHANNEL}")
    print(f"📦 Storage: {STORAGE_CHANNEL_ID}")
    print(f"🌐 Web: {BASE_URL}")
    print("=" * 55)

    try:
        me = await bot.get_me()
        await set_commands()
        await start_web_server()
        print(f"✅ Telegram connected: @{me.username}")
        print("🚀 Bot is running...")
        await dp.start_polling(bot)
    finally:
        await session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Bot stopped.")
