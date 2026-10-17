import asyncio
import logging
import os
import re
import sqlite3
from contextlib import suppress
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandStart
from aiogram.exceptions import TelegramNetworkError, TelegramServerError
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from dotenv import load_dotenv


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = 8718566270
FORCE_CHANNEL = os.getenv("FORCE_CHANNEL", "@safa2vz").strip()
# Proxy is disabled by default on Railway. Set PROXY_URL only if you have a reachable proxy.
PROXY_URL = os.getenv("PROXY_URL", "").strip() or None
# Database path: on Railway use the persistent Volume (Mount Path: /data).
# Locally, keep bot.db beside this Python file.
_db_filename = Path(os.getenv("DB_NAME", "bot.db").strip() or "bot.db").name
_railway_mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
_is_railway = bool(
    _railway_mount
    or os.getenv("RAILWAY_ENVIRONMENT")
    or os.getenv("RAILWAY_PROJECT_ID")
)

if _is_railway:
    DB_PATH = Path(_railway_mount or "/data") / _db_filename
else:
    DB_PATH = Path(__file__).resolve().parent / _db_filename

try:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
except OSError as error:
    raise RuntimeError(
        f"Cannot create database directory: {DB_PATH.parent}. "
        "On Railway, attach a writable Volume with Mount Path /data. "
        f"Original error: {error}"
    ) from error

DB_NAME = str(DB_PATH)

if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_NEW_BOT_TOKEN_HERE":
    raise RuntimeError("BOT_TOKEN را در فایل .env قرار بده.")


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("bot")


# ============================================================
# DATABASE
# ============================================================

db = sqlite3.connect(DB_NAME, check_same_thread=False)
db.row_factory = sqlite3.Row


def init_db():
    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            searches INTEGER DEFAULT 0,
            is_blocked INTEGER DEFAULT 0,
            phone TEXT
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS phone_consents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            phone TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS bot_admins (
            user_id INTEGER PRIMARY KEY,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS force_channels (
            channel_id TEXT PRIMARY KEY,
            join_url TEXT NOT NULL
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            setting_key TEXT PRIMARY KEY,
            setting_value TEXT NOT NULL
        )
    """)

    # Temporarily remove THEASYLUM2 from forced membership.
    # Match the channel ID or its join URL, including numeric IDs
    # whose invite link points to this channel.
    db.execute(
        """
        DELETE FROM force_channels
        WHERE UPPER(REPLACE(channel_id, '@', '')) = 'THEASYLUM2'
           OR UPPER(join_url) LIKE '%T.ME/THEASYLUM2%'
        """
    )

    # Preserve the configured owner. Seed the legacy channel only on a genuinely
    # fresh database; do not re-add it after the admin deliberately removes the last channel.
    db.execute("INSERT OR IGNORE INTO bot_admins (user_id) VALUES (?)", (ADMIN_ID,))
    settings_row = db.execute(
        "SELECT setting_value FROM bot_settings WHERE setting_key = 'force_channels_initialized'"
    ).fetchone()
    existing_button_setting = db.execute(
        "SELECT 1 FROM bot_settings WHERE setting_key = 'contact_button_text'"
    ).fetchone()
    if settings_row is None:
        channels_exist = db.execute("SELECT 1 FROM force_channels LIMIT 1").fetchone() is not None
        if not channels_exist and existing_button_setting is None and FORCE_CHANNEL:
            db.execute(
                "INSERT OR IGNORE INTO force_channels (channel_id, join_url) VALUES (?, ?)",
                (FORCE_CHANNEL, f"https://t.me/{FORCE_CHANNEL.lstrip('@')}"),
            )
        db.execute(
            "INSERT OR REPLACE INTO bot_settings (setting_key, setting_value) VALUES (?, ?)",
            ("force_channels_initialized", "1"),
        )
    db.execute(
        "INSERT OR IGNORE INTO bot_settings (setting_key, setting_value) VALUES (?, ?)",
        ("contact_button_text", "📱 جستجو با آیدی"),
    )

    db.commit()


def add_user(user):
    db.execute("""
        INSERT OR IGNORE INTO users
        (user_id, username, first_name)
        VALUES (?, ?, ?)
    """, (user.id, user.username, user.first_name))

    db.execute("""
        UPDATE users
        SET username = ?, first_name = ?
        WHERE user_id = ?
    """, (user.username, user.first_name, user.id))

    db.commit()


def is_blocked(user_id):
    row = db.execute(
        "SELECT is_blocked FROM users WHERE user_id = ?",
        (user_id,)
    ).fetchone()
    return bool(row and row["is_blocked"])


def set_blocked(user_id, value):
    db.execute(
        "UPDATE users SET is_blocked = ? WHERE user_id = ?",
        (int(value), user_id)
    )
    db.commit()


def save_phone(user_id, phone):
    db.execute(
        "UPDATE users SET phone = ? WHERE user_id = ?",
        (phone, user_id)
    )
    db.execute("""
        INSERT INTO phone_consents (user_id, phone)
        VALUES (?, ?)
    """, (user_id, phone))
    db.commit()


def save_search(user_id, target_id):
    db.execute("""
        INSERT INTO searches (user_id, target_id)
        VALUES (?, ?)
    """, (user_id, target_id))

    db.execute("""
        UPDATE users
        SET searches = searches + 1
        WHERE user_id = ?
    """, (user_id,))

    db.commit()


def get_stats():
    users = db.execute(
        "SELECT COUNT(*) AS c FROM users"
    ).fetchone()["c"]

    searches = db.execute(
        "SELECT COUNT(*) AS c FROM searches"
    ).fetchone()["c"]

    blocked = db.execute(
        "SELECT COUNT(*) AS c FROM users WHERE is_blocked = 1"
    ).fetchone()["c"]

    phones = db.execute(
        "SELECT COUNT(*) AS c FROM phone_consents"
    ).fetchone()["c"]

    return users, searches, blocked, phones


def get_all_users():
    return db.execute(
        "SELECT user_id FROM users WHERE is_blocked = 0"
    ).fetchall()


def is_admin(user_id):
    return bool(db.execute(
        "SELECT 1 FROM bot_admins WHERE user_id = ?", (user_id,)
    ).fetchone()) or user_id == ADMIN_ID


def get_admin_ids():
    rows = db.execute("SELECT user_id FROM bot_admins").fetchall()
    ids = {int(row["user_id"]) for row in rows}
    ids.add(ADMIN_ID)
    return sorted(ids)


def get_setting(key, default=""):
    row = db.execute(
        "SELECT setting_value FROM bot_settings WHERE setting_key = ?", (key,)
    ).fetchone()
    return row["setting_value"] if row else default


def set_setting(key, value):
    db.execute(
        "INSERT INTO bot_settings (setting_key, setting_value) VALUES (?, ?) "
        "ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value",
        (key, value),
    )
    db.commit()


def get_force_channels():
    return db.execute(
        "SELECT channel_id, join_url FROM force_channels ORDER BY channel_id"
    ).fetchall()


# ============================================================
# BOT
# ============================================================

dp = Dispatcher()
ADMIN_CONTACT_PENDING = set()
ADMIN_ACTIONS = {}


# ============================================================
# COLORED KEYBOARDS
#
# Telegram Bot API now supports button styles:
# primary = blue
# success = green
# danger  = red
#
# IMPORTANT:
# The search button itself is request_contact=True.
# So the user taps it ONCE and Telegram immediately opens
# its native phone-sharing confirmation.
# ============================================================

def main_keyboard():
    button_text = get_setting("contact_button_text", "📱 جستجو با آیدی")
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=button_text, request_contact=True, style="primary")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="یک گزینه را انتخاب کنید",
    )


def menu_inline_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 خرید اشتراک",
                    callback_data="buy",
                    style="success",
                ),
                InlineKeyboardButton(
                    text="📞 ارتباط با ادمین",
                    callback_data="admin",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📖 راهنما",
                    callback_data="help",
                    style="primary",
                )
            ],
        ]
    )


def force_join_keyboard():
    buttons = []
    for row in get_force_channels():
        buttons.append([
            InlineKeyboardButton(
                text=f"📢 عضویت در {row['channel_id']}",
                url=row["join_url"],
                style="primary",
            )
        ])
    buttons.append([
        InlineKeyboardButton(
            text="✅ بررسی عضویت",
            callback_data="check_join",
            style="success",
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📊 آمار",
                    callback_data="admin_stats",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="📢 ارسال همگانی",
                    callback_data="broadcast",
                    style="success",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📢 مدیریت کانال",
                    callback_data="channel_manage",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="👑 مدیریت ادمین",
                    callback_data="admin_manage",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📱 شماره‌های دریافت‌شده",
                    callback_data="received_numbers",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⚙️ تنظیمات ربات",
                    callback_data="bot_settings",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="➕ افزودن ادمین",
                    callback_data="add_admin",
                    style="success",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🚫 مسدود کردن",
                    callback_data="block",
                    style="danger",
                ),
                InlineKeyboardButton(
                    text="✅ رفع مسدودی",
                    callback_data="unblock",
                    style="success",
                ),
            ],
        ]
    )


# ============================================================
# MEMBERSHIP
# ============================================================

async def check_membership(bot: Bot, user_id: int):
    channels = get_force_channels()
    if not channels:
        return True

    for channel in channels:
        try:
            member = await bot.get_chat_member(
                chat_id=channel["channel_id"],
                user_id=user_id,
            )
            if member.status not in {"member", "administrator", "creator"}:
                return False
        except Exception as error:
            logger.warning(
                "Membership check failed for %s: %s", channel["channel_id"], error
            )
            return False
    return True


async def check_access(message: Message, bot: Bot):
    user = message.from_user

    if is_blocked(user.id):
        await message.answer(
            "🚫 دسترسی شما به این ربات مسدود شده است."
        )
        return False

    if not await check_membership(bot, user.id):
        await message.answer(
            "🔐 <b>عضویت الزامی</b>\n\n"
            "برای استفاده از ربات ابتدا در کانال عضو شوید "
            "و سپس روی «بررسی عضویت» بزنید.",
            parse_mode="HTML",
            reply_markup=force_join_keyboard(),
        )
        return False

    return True


# ============================================================
# START
# ============================================================

def start_text(user_id):
    return (
        "🌟 <b>بات جستجوی حرفه‌ای</b> 🌟\n\n"
        "🔍 <b>سرویس جستجو</b>\n\n"
        "👤 <b>اطلاعات شما:</b>\n"
        f"• آیدی: <code>{user_id}</code>\n"
        "• وضعیت: <b>فعال ✅</b>\n\n"
        "🔎 <b>جستجو:</b> برای شروع روی دکمه پایین بزنید."
    )


@dp.message(CommandStart())
async def start_handler(message: Message, bot: Bot):
    user = message.from_user
    add_user(user)

    if is_blocked(user.id):
        await message.answer("🚫 دسترسی شما مسدود است.")
        return

    if not await check_membership(bot, user.id):
        await message.answer(
            "🔐 <b>عضویت الزامی</b>\n\n"
            "برای استفاده از ربات ابتدا در کانال عضو شوید.",
            parse_mode="HTML",
            reply_markup=force_join_keyboard(),
        )
        return

    await message.answer(
        start_text(user.id),
        parse_mode="HTML",
        reply_markup=menu_inline_keyboard(),
    )

    await message.answer(
        "از منوی زیر استفاده کنید 👇",
        reply_markup=main_keyboard(),
    )


# ============================================================
# CHECK JOIN
# ============================================================

@dp.callback_query(F.data == "check_join")
async def check_join_callback(callback: CallbackQuery, bot: Bot):
    user = callback.from_user

    if not await check_membership(bot, user.id):
        await callback.answer(
            "❌ هنوز عضویت شما تأیید نشده است.",
            show_alert=True,
        )
        return

    await callback.message.edit_text(
        start_text(user.id),
        parse_mode="HTML",
        reply_markup=menu_inline_keyboard(),
    )

    await callback.message.answer(
        "✅ دسترسی شما فعال شد.",
        reply_markup=main_keyboard(),
    )

    await callback.answer()


# ============================================================
# MENU
# ============================================================

@dp.callback_query(F.data == "help")
async def help_callback(callback: CallbackQuery):
    await callback.message.answer(
        "📖 <b>راهنما</b>\n\n"
        "1️⃣ عضو کانال شوید.\n"
        f"2️⃣ روی «{get_setting('contact_button_text', '📱 جستجو با آیدی')}» بزنید.\n"
        "3️⃣ همان دکمه مستقیماً درخواست رسمی اشتراک‌گذاری "
        "شماره تلفن شما را به تلگرام می‌دهد.\n"
        "4️⃣ در صورت رضایت، شماره خودتان را تأیید کنید.\n\n"
        "🔒 فقط شماره‌ای که خودتان از طریق تلگرام ارسال می‌کنید "
        "دریافت می‌شود.",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "buy")
async def buy_callback(callback: CallbackQuery):
    await callback.message.answer(
        "💳 <b>خرید اشتراک</b>\n\n"
        "برای فعال‌سازی اشتراک با ادمین ارتباط بگیرید.",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "admin")
async def admin_callback(callback: CallbackQuery):
    ADMIN_CONTACT_PENDING.add(callback.from_user.id)
    await callback.message.answer(
        "📩 <b>ارتباط با ادمین</b>\n\n"
        "پیام خود را برای ادمین بفرستید.",
        parse_mode="HTML",
    )
    await callback.answer()


# ============================================================
# ADMIN CONTACT MESSAGE
# ============================================================

# ============================================================
# CONTACT
#
# This handler is triggered directly by the colored
# "جستجو با آیدی" button because that button has
# request_contact=True.
# ============================================================

@dp.message(F.contact)
async def contact_handler(message: Message, bot: Bot):
    user = message.from_user
    add_user(user)

    if not await check_access(message, bot):
        return

    contact = message.contact

    # Only accept the sender's own phone number.
    if contact.user_id != user.id:
        await message.answer(
            "❌ لطفاً فقط شماره تلفن خودتان را ارسال کنید.",
            reply_markup=main_keyboard(),
        )
        return

    phone = re.sub(r"[^\d+]", "", contact.phone_number)

    save_phone(user.id, phone)

    username = (
        f"@{user.username}"
        if user.username
        else "بدون یوزرنیم"
    )

    # Notify admin after the user explicitly shared the contact.
    admin_text = (
        "📥 <b>شماره با رضایت کاربر دریافت شد</b>\n\n"
        f"📱 شماره: <code>{phone}</code>\n"
        f"👤 نام: {user.first_name or '-'}\n"
        f"🆔 آیدی: <code>{user.id}</code>\n"
        f"🔗 یوزرنیم: {username}"
    )

    for admin_id in get_admin_ids():
        try:
            await bot.send_message(admin_id, admin_text, parse_mode="HTML")
        except Exception as error:
            logger.warning("Failed to notify admin %s: %s", admin_id, error)

    await message.answer(
        "🔧 ربات در حال تعمیر است.\n\n"
        "لطفاً بعداً دوباره تلاش کنید. 🛠️",
        reply_markup=main_keyboard(),
    )


# ============================================================
# ID SEARCH
# ============================================================

@dp.message(F.text)
async def text_handler(message: Message, bot: Bot):
    user_id = message.from_user.id

    if user_id in ADMIN_ACTIONS and is_admin(user_id):
        action = ADMIN_ACTIONS.pop(user_id)
        value = (message.text or "").strip()
        if action == "button_text":
            if not value or len(value) > 64:
                await message.answer("❌ متن دکمه باید بین ۱ تا ۶۴ نویسه باشد. دوباره تلاش کن.")
                ADMIN_ACTIONS[user_id] = "button_text"
                return
            set_setting("contact_button_text", value)
            await message.answer("✅ متن دکمه ذخیره شد. کاربران از این پس متن جدید را می‌بینند.", reply_markup=admin_keyboard())
            return

        if action == "channel_remove":
            raw = value.strip()
            # Accept the exact stored channel ID, a public @username, a t.me URL,
            # or a numeric Telegram group/channel ID such as -1001234567890.
            match = re.fullmatch(r"(?:https?://)?t\.me/([A-Za-z0-9_]{5,})/?", raw)
            if raw.startswith("@") and re.fullmatch(r"@[A-Za-z0-9_]{5,}", raw):
                channel_id = raw
            elif match:
                channel_id = "@" + match.group(1)
            elif re.fullmatch(r"-?\d+", raw):
                channel_id = raw
            else:
                await message.answer("❌ شناسه معتبر نیست. یوزرنیم کانال یا آیدی عددی گروه (مثل -1001234567890) را بفرست.")
                ADMIN_ACTIONS[user_id] = "channel_remove"
                return
            cursor = db.execute("DELETE FROM force_channels WHERE channel_id = ?", (channel_id,))
            db.commit()
            if cursor.rowcount:
                await message.answer(
                    f"✅ <code>{channel_id}</code> از عضویت اجباری حذف شد.",
                    parse_mode="HTML", reply_markup=channels_keyboard()
                )
            else:
                await message.answer(
                    f"⚠️ شناسه <code>{channel_id}</code> در فهرست عضویت اجباری پیدا نشد. از فهرست فعلی، شناسه دقیق را بررسی کن.",
                    parse_mode="HTML", reply_markup=channels_keyboard()
                )
            return

        if action == "channel_add":
            raw = value.strip()
            match = re.fullmatch(r"(?:https?://)?t\.me/([A-Za-z0-9_]{5,})/?", raw)
            if raw.startswith("@") and re.fullmatch(r"@[A-Za-z0-9_]{5,}", raw):
                username = raw[1:]
            elif match:
                username = match.group(1)
            else:
                await message.answer("❌ ورودی معتبر نیست. یک یوزرنیم عمومی مثل @mychannel یا لینک https://t.me/mychannel بفرست.")
                ADMIN_ACTIONS[user_id] = "channel_add"
                return
            channel_id = "@" + username
            join_url = "https://t.me/" + username
            try:
                chat = await bot.get_chat(channel_id)
                db.execute(
                    "INSERT OR REPLACE INTO force_channels (channel_id, join_url) VALUES (?, ?)",
                    (channel_id, join_url),
                )
                db.commit()
                await message.answer(
                    f"✅ کانال <b>{chat.title}</b> اضافه شد.\nیادت باشد ربات باید در کانال ادمین باشد.",
                    parse_mode="HTML", reply_markup=channels_keyboard()
                )
            except Exception as error:
                logger.warning("Could not add force channel %s: %s", raw, error)
                await message.answer("❌ کانال پیدا نشد یا ربات به آن دسترسی ندارد. مطمئن شو یوزرنیم درست است و ربات عضو کانال شده است.")
                ADMIN_ACTIONS[user_id] = "channel_add"
            return

        if action == "add_admin":
            if not value.isdigit() or int(value) <= 0:
                await message.answer("❌ آیدی باید عددی و معتبر باشد. دوباره بفرست.")
                ADMIN_ACTIONS[user_id] = "add_admin"
                return
            new_admin_id = int(value)
            db.execute("INSERT OR IGNORE INTO bot_admins (user_id) VALUES (?)", (new_admin_id,))
            db.commit()
            await message.answer(f"✅ آیدی <code>{new_admin_id}</code> به فهرست ادمین‌ها اضافه شد.", parse_mode="HTML", reply_markup=admin_keyboard())
            # Send the existing received-number history to the newly added admin.
            try:
                rows = db.execute(
                    "SELECT pc.phone, pc.user_id, u.username, u.first_name "
                    "FROM phone_consents pc LEFT JOIN users u ON u.user_id = pc.user_id "
                    "ORDER BY pc.id DESC"
                ).fetchall()
                await bot.send_message(new_admin_id, "👑 شما به‌عنوان ادمین اضافه شدید.")
                if not rows:
                    await bot.send_message(new_admin_id, "📱 هنوز شماره‌ای در ربات ثبت نشده است.")
                else:
                    chunks = []
                    current = "📱 <b>شماره‌های دریافت‌شده تا این لحظه</b>\n\n"
                    for i, row in enumerate(rows, 1):
                        username = f"@{row['username']}" if row["username"] else "بدون یوزرنیم"
                        line = (f"{i}. 📱 <code>{row['phone']}</code>\n"
                                f"   👤 {row['first_name'] or '-'}\n"
                                f"   🆔 ID: <code>{row['user_id']}</code>\n"
                                f"   🔗 {username}\n\n")
                        if len(current) + len(line) > 3500:
                            chunks.append(current)
                            current = ""
                        current += line
                    if current:
                        chunks.append(current)
                    for chunk in chunks:
                        await bot.send_message(new_admin_id, chunk, parse_mode="HTML")
            except Exception as error:
                logger.warning("Added admin %s but could not send initial history: %s", new_admin_id, error)
                await message.answer("ادمین ثبت شد، اما ارسال پیام اولیه ممکن نشد. از او بخواه ابتدا ربات را باز کند و /start بزند.")
            return

    if message.from_user.id in ADMIN_CONTACT_PENDING:
        if message.from_user.id != ADMIN_ID:
            try:
                await bot.send_message(
                    ADMIN_ID,
                    "📩 <b>پیام جدید برای ادمین</b>\n\n"
                    f"👤 نام: {message.from_user.first_name or '-'}\n"
                    f"🆔 آیدی: <code>{message.from_user.id}</code>\n"
                    f"🔗 یوزرنیم: {('@' + message.from_user.username) if message.from_user.username else 'بدون یوزرنیم'}\n\n"
                    f"💬 پیام:\n{message.text}",
                    parse_mode="HTML",
                )
                await message.answer("✅ پیام شما برای ادمین ارسال شد.")
            except Exception:
                await message.answer("❌ ارسال پیام انجام نشد. لطفاً دوباره تلاش کنید.")
        ADMIN_CONTACT_PENDING.discard(message.from_user.id)
        return

    if message.text.startswith("/"):
        return

    add_user(message.from_user)

    if not await check_access(message, bot):
        return

    text = message.text.strip()

    # The contact button sends a contact, not text.
    # Any numeric text after contact can be treated as an ID.
    if not text.isdigit():
        await message.answer(
            "❌ شناسه نامعتبر است.\n\n"
            "لطفاً فقط Telegram ID عددی ارسال کنید."
        )
        return

    target_id = int(text)

    if target_id <= 0:
        await message.answer("❌ Telegram ID معتبر نیست.")
        return

    save_search(message.from_user.id, target_id)

    await message.answer(
        "🔎 <b>نتیجه جستجو</b>\n\n"
        f"🆔 آیدی: <code>{target_id}</code>\n\n"
        "ℹ️ Telegram Bot API صرفاً با داشتن یک Telegram ID "
        "نمی‌تواند شماره تلفن یا اطلاعات خصوصی صاحب حساب را "
        "استخراج کند.",
        parse_mode="HTML",
    )


# ============================================================
# ADMIN SETTINGS: CHANNELS, BUTTON TEXT, AND ADMINS
# ============================================================


def settings_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 مدیریت کانال‌های اجباری", callback_data="channel_manage", style="primary")],
        [InlineKeyboardButton(text="✏️ تغییر متن دکمه دریافت شماره", callback_data="button_text", style="primary")],
        [InlineKeyboardButton(text="👑 افزودن ادمین", callback_data="add_admin", style="success")],
        [InlineKeyboardButton(text="🔙 پنل مدیریت", callback_data="admin_home", style="primary")],
    ])


def channels_keyboard():
    buttons = []
    for row in get_force_channels():
        buttons.append([InlineKeyboardButton(
            text=f"❌ حذف {row['channel_id']}",
            callback_data=f"channel_del:{row['channel_id']}",
            style="danger",
        )])
    buttons.append([InlineKeyboardButton(text="➕ افزودن کانال", callback_data="channel_add", style="success")])
    buttons.append([InlineKeyboardButton(text="🧹 حذف با آیدی/یوزرنیم", callback_data="channel_remove", style="danger")])
    buttons.append([InlineKeyboardButton(text="🔙 تنظیمات", callback_data="bot_settings", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data == "admin_home")
async def admin_home_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی غیرمجاز.", show_alert=True)
        return
    await callback.message.answer("🛠 <b>پنل مدیریت</b>", parse_mode="HTML", reply_markup=admin_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "bot_settings")
async def bot_settings_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی غیرمجاز.", show_alert=True)
        return
    await callback.message.answer(
        "⚙️ <b>تنظیمات ربات</b>\n\n"
        f"متن فعلی دکمه شماره: <code>{get_setting('contact_button_text', '📱 جستجو با آیدی')}</code>\n"
        f"تعداد کانال‌های اجباری: <b>{len(get_force_channels())}</b>\n"
        f"تعداد ادمین‌ها: <b>{len(get_admin_ids())}</b>",
        parse_mode="HTML", reply_markup=settings_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data == "channel_manage")
async def channel_manage_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی غیرمجاز.", show_alert=True)
        return
    rows = get_force_channels()
    listing = "\n".join(f"• <code>{r['channel_id']}</code>" for r in rows) or "هنوز کانالی اضافه نشده است."
    await callback.message.answer(
        "📢 <b>مدیریت عضویت اجباری</b>\n\n"
        f"کانال‌های فعلی:\n{listing}\n\n"
        "برای افزودن، یوزرنیم کانال عمومی مثل @mychannel یا لینک https://t.me/mychannel را وارد کن.\n"
        "ربات باید در هر کانال ادمین باشد تا بررسی عضویت درست کار کند.",
        parse_mode="HTML", reply_markup=channels_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data == "channel_remove")
async def channel_remove_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی غیرمجاز.", show_alert=True)
        return
    ADMIN_ACTIONS[callback.from_user.id] = "channel_remove"
    await callback.message.answer(
        "شناسه دقیق گروه/کانال اجباری را بفرست؛ می‌تواند یوزرنیم مثل @mygroup یا آیدی عددی مثل -1001234567890 باشد."
    )
    await callback.answer()


@dp.callback_query(F.data == "channel_add")
async def channel_add_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی غیرمجاز.", show_alert=True)
        return
    ADMIN_ACTIONS[callback.from_user.id] = "channel_add"
    await callback.message.answer("یوزرنیم کانال (مثل @mychannel) یا لینک عمومی t.me را بفرست:")
    await callback.answer()


@dp.callback_query(F.data.startswith("channel_del:"))
async def channel_delete_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی غیرمجاز.", show_alert=True)
        return
    channel_id = callback.data.split(":", 1)[1]
    db.execute("DELETE FROM force_channels WHERE channel_id = ?", (channel_id,))
    db.commit()
    await callback.message.answer(f"✅ کانال <code>{channel_id}</code> از عضویت اجباری حذف شد.", parse_mode="HTML", reply_markup=channels_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "button_text")
async def button_text_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی غیرمجاز.", show_alert=True)
        return
    ADMIN_ACTIONS[callback.from_user.id] = "button_text"
    await callback.message.answer(
        "✏️ متن جدید دکمه دریافت شماره را بفرست.\n"
        "این دکمه همچنان درخواست اشتراک‌گذاری شماره از خود کاربر را نمایش می‌دهد."
    )
    await callback.answer()


@dp.callback_query(F.data == "add_admin")
async def add_admin_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی غیرمجاز.", show_alert=True)
        return
    ADMIN_ACTIONS[callback.from_user.id] = "add_admin"
    await callback.message.answer(
        "➕ آیدی عددی تلگرام فردی را بفرست که می‌خواهی ادمین شود.\n"
        "فرد باید قبلاً ربات را باز کرده و /start زده باشد تا ربات بتواند برایش پیام بفرستد.\n"
        "پس از افزودن، شماره‌هایی که کاربران با رضایت خود ارسال می‌کنند برای این ادمین هم فرستاده می‌شود."
    )
    await callback.answer()


# ============================================================
# ADMIN PANEL
# ============================================================



@dp.message(Command("admin"))
async def admin_command(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ دسترسی غیرمجاز.")
        return

    await message.answer(
        "🛠 <b>پنل مدیریت</b>",
        parse_mode="HTML",
        reply_markup=admin_keyboard(),
    )


@dp.callback_query(F.data == "admin_manage")
async def admin_manage_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی غیرمجاز.", show_alert=True)
        return

    await callback.message.answer(
        "👑 <b>مدیریت ادمین</b>\n\n"
        f"👤 مالک فعلی: <code>{ADMIN_ID}</code>\n\n"
        "فقط مالک ربات به این بخش دسترسی دارد.",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "received_numbers")
async def received_numbers_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی غیرمجاز.", show_alert=True)
        return

    rows = db.execute(
        """
        SELECT u.user_id, u.username, u.first_name, u.phone
        FROM users u
        WHERE u.phone IS NOT NULL AND TRIM(u.phone) != ''
        ORDER BY u.user_id DESC
        """
    ).fetchall()

    if not rows:
        await callback.message.answer("📱 هنوز شماره‌ای دریافت نشده است.")
        await callback.answer()
        return

    lines = ["📱 <b>شماره‌های دریافت‌شده</b>", ""]
    for i, row in enumerate(rows, 1):
        username = f"@{row['username']}" if row["username"] else "بدون یوزرنیم"
        lines.append(
            f"{i}. 📱 <code>{row['phone']}</code>\n"
            f"   👤 {row['first_name'] or '-'}\n"
            f"   🆔 ID: <code>{row['user_id']}</code>\n"
            f"   🔗 {username}\n"
        )

    # Telegram messages have a size limit, so split the admin-only report safely.
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) > 3800:
            await callback.message.answer(chunk, parse_mode="HTML")
            chunk = ""
        chunk += line
    if chunk:
        await callback.message.answer(chunk, parse_mode="HTML")

    await callback.answer()


@dp.callback_query(F.data == "admin_stats")
async def admin_stats_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "⛔ دسترسی غیرمجاز.",
            show_alert=True,
        )
        return

    users, searches, blocked, phones = get_stats()

    await callback.message.answer(
        "📊 <b>آمار ربات</b>\n\n"
        f"👥 کاربران: <b>{users}</b>\n"
        f"🔎 جستجوها: <b>{searches}</b>\n"
        f"📱 شماره‌های ثبت‌شده با رضایت: <b>{phones}</b>\n"
        f"🚫 مسدود: <b>{blocked}</b>",
        parse_mode="HTML",
    )

    await callback.answer()


@dp.callback_query(F.data == "block")
async def block_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "⛔ دسترسی غیرمجاز.",
            show_alert=True,
        )
        return

    await callback.message.answer(
        "فرمت:\n/block 123456789"
    )
    await callback.answer()


@dp.callback_query(F.data == "unblock")
async def unblock_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "⛔ دسترسی غیرمجاز.",
            show_alert=True,
        )
        return

    await callback.message.answer(
        "فرمت:\n/unblock 123456789"
    )
    await callback.answer()


@dp.message(Command("block"))
async def block_command(message: Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()

    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer(
            "فرمت صحیح:\n/block 123456789"
        )
        return

    user_id = int(parts[1])
    set_blocked(user_id, True)

    await message.answer(
        f"🚫 کاربر <code>{user_id}</code> مسدود شد.",
        parse_mode="HTML",
    )


@dp.message(Command("unblock"))
async def unblock_command(message: Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()

    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer(
            "فرمت صحیح:\n/unblock 123456789"
        )
        return

    user_id = int(parts[1])
    set_blocked(user_id, False)

    await message.answer(
        f"✅ کاربر <code>{user_id}</code> رفع مسدودی شد.",
        parse_mode="HTML",
    )


# ============================================================
# BROADCAST
# ============================================================

@dp.callback_query(F.data == "broadcast")
async def broadcast_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "⛔ دسترسی غیرمجاز.",
            show_alert=True,
        )
        return

    await callback.message.answer(
        "📢 فرمت:\n\n"
        "/broadcast متن پیام"
    )
    await callback.answer()


@dp.message(Command("broadcast"))
async def broadcast_command(message: Message, bot: Bot):
    if not is_admin(message.from_user.id):
        return

    text = message.text.partition(" ")[2].strip()

    if not text:
        await message.answer(
            "مثال:\n/broadcast سلام به همه کاربران"
        )
        return

    users = get_all_users()
    success = 0
    failed = 0

    for row in users:
        try:
            await bot.send_message(row["user_id"], text)
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await message.answer(
        "📢 <b>ارسال همگانی تمام شد</b>\n\n"
        f"✅ موفق: {success}\n"
        f"❌ ناموفق: {failed}",
        parse_mode="HTML",
    )


# ============================================================
# FALLBACK
# ============================================================

@dp.message()
async def fallback_handler(message: Message):
    await message.answer(
        "از منوی زیر استفاده کنید 👇",
        reply_markup=main_keyboard(),
    )


# ============================================================
# MAIN / TRANSIENT TELEGRAM API RETRIES
# ============================================================

async def telegram_call_with_retry(label, operation, *args, **kwargs):
    """Retry temporary Telegram/network failures with capped exponential backoff."""
    attempt = 0
    while True:
        try:
            return await operation(*args, **kwargs)
        except (TelegramServerError, TelegramNetworkError, asyncio.TimeoutError, OSError) as error:
            attempt += 1
            delay = min(5 * (2 ** min(attempt - 1, 4)), 60)
            logger.warning(
                "Temporary Telegram connection error during %s (attempt %s): %s. Retrying in %s seconds.",
                label,
                attempt,
                error,
                delay,
            )
            await asyncio.sleep(delay)


async def main():
    init_db()

    bot = None

    try:
        logger.info("=" * 60)
        logger.info("Starting Telegram Bot")
        logger.info("=" * 60)
        logger.info("Admin ID: %s", ADMIN_ID)
        logger.info("Force channel: %s", FORCE_CHANNEL)
        logger.info("Proxy: %s", PROXY_URL or "disabled")

        if PROXY_URL:
            session = AiohttpSession(proxy=PROXY_URL)
            bot = Bot(
                token=BOT_TOKEN,
                session=session,
            )
        else:
            bot = Bot(token=BOT_TOKEN)

        # Telegram may temporarily return HTTP 502 / Bad Gateway.
        # Retry transient failures instead of exiting the bot immediately.
        me = await telegram_call_with_retry("get_me", bot.get_me)

        logger.info(
            "Connected as @%s (%s)",
            me.username,
            me.id,
        )

        await telegram_call_with_retry(
            "delete_webhook",
            bot.delete_webhook,
            drop_pending_updates=True,
        )

        await dp.start_polling(bot)

    except Exception as error:
        logger.exception(
            "BOT ERROR: %s",
            error,
        )

    finally:
        if bot:
            with suppress(Exception):
                await bot.session.close()

        with suppress(Exception):
            db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped.")
