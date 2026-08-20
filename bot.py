import asyncio
import logging
import os
import re
import sqlite3
from pathlib import Path
from contextlib import suppress

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandStart
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
ADMIN_ID = int(os.getenv("ADMIN_ID", "7692023421"))
FORCE_CHANNEL = os.getenv("FORCE_CHANNEL", "@THEASYLUM2").strip()
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "@huxmh").strip()
PROXY_URL = os.getenv("PROXY_URL", "socks5://127.0.0.1:10808").strip() or None
DB_NAME = os.getenv("DB_NAME", "bot.db").strip()

# Railway Volume is mounted at /data. Make sure the parent directory
# exists before SQLite tries to create the database file.
DB_PATH = Path(DB_NAME).expanduser()
DB_PARENT = DB_PATH.parent
try:
    DB_PARENT.mkdir(parents=True, exist_ok=True)
except OSError as error:
    raise RuntimeError(
        f"Cannot create SQLite directory {DB_PARENT}: {error}. "
        "On Railway, make sure a Volume is mounted at /data and "
        "DB_NAME=/data/bot.db."
    ) from error

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

db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
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


# ============================================================
# BOT
# ============================================================

dp = Dispatcher()


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
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="📱 جستجو با آیدی",
                    request_contact=True,
                    style="primary",
                )
            ],
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
    channel = FORCE_CHANNEL.lstrip("@")

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📢 عضویت در کانال",
                    url=f"https://t.me/{channel}",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ بررسی عضویت",
                    callback_data="check_join",
                    style="success",
                )
            ],
        ]
    )


def admin_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📊 آمار",
                    callback_data="admin_stats",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📢 ارسال همگانی",
                    callback_data="broadcast",
                    style="success",
                )
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
    try:
        member = await bot.get_chat_member(
            chat_id=FORCE_CHANNEL,
            user_id=user_id,
        )

        return member.status in {
            "member",
            "administrator",
            "creator",
        }

    except Exception as error:
        logger.warning("Membership check failed: %s", error)
        return False


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
        "2️⃣ روی «📱 جستجو با آیدی» بزنید.\n"
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
    username = ADMIN_USERNAME.lstrip("@")

    await callback.message.answer(
        "📞 <b>ارتباط با ادمین</b>\n\n"
        f"@{username}",
        parse_mode="HTML",
    )
    await callback.answer()


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

    try:
        await bot.send_message(
            ADMIN_ID,
            admin_text,
            parse_mode="HTML",
        )
    except Exception as error:
        logger.exception(
            "Failed to notify admin: %s",
            error,
        )

    await message.answer(
        "✅ شماره شما دریافت شد.\n\n"
        "حالا Telegram ID عددی موردنظر را ارسال کنید.",
        reply_markup=main_keyboard(),
    )


# ============================================================
# ID SEARCH
# ============================================================

@dp.message(F.text)
async def text_handler(message: Message, bot: Bot):
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
# ADMIN PANEL
# ============================================================

def is_admin(user_id):
    return user_id == ADMIN_ID


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
# MAIN
# ============================================================

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

        me = await bot.get_me()

        logger.info(
            "Connected as @%s (%s)",
            me.username,
            me.id,
        )

        await bot.delete_webhook(
            drop_pending_updates=True
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
