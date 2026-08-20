import asyncio
import logging
import sqlite3
import os
from contextlib import suppress

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from aiogram.client.session.aiohttp import AiohttpSession


# ============================================================
# SETTINGS
# ============================================================

BOT_TOKEN = "PUT_YOUR_NEW_BOT_TOKEN_HERE"

ADMIN_ID = 123456789

# مثال:
# @my_channel
# یا -1001234567890
FORCE_CHANNEL = "@THEASYLUM2"

PROXY_URL = None
# اگر پروکسی لازم داری:
# PROXY_URL = "socks5://127.0.0.1:10808"

DB_NAME = "/data/bot.db" if os.path.isdir("/data") else "bot.db"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


# ============================================================
# DATABASE
# ============================================================

db = sqlite3.connect(DB_NAME)
db.row_factory = sqlite3.Row


def init_db():
    cursor = db.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            searches INTEGER DEFAULT 0,
            is_blocked INTEGER DEFAULT 0
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            target_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS force_join_channels (
            channel_id TEXT PRIMARY KEY,
            title TEXT,
            username TEXT,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        INSERT OR IGNORE INTO force_join_channels(channel_id, title, username)
        VALUES (?, ?, ?)
    """, (FORCE_CHANNEL, FORCE_CHANNEL, FORCE_CHANNEL))

    db.commit()


def add_user(user):
    db.execute("""
        INSERT OR IGNORE INTO users
        (user_id, username, first_name)
        VALUES (?, ?, ?)
    """, (
        user.id,
        user.username,
        user.first_name
    ))

    db.execute("""
        UPDATE users
        SET username = ?, first_name = ?
        WHERE user_id = ?
    """, (
        user.username,
        user.first_name,
        user.id
    ))

    db.commit()


def get_user(user_id):
    return db.execute(
        "SELECT * FROM users WHERE user_id = ?",
        (user_id,)
    ).fetchone()


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

    return users, searches, blocked


def get_all_users():
    return db.execute(
        "SELECT user_id FROM users WHERE is_blocked = 0"
    ).fetchall()


def get_force_join_channels():
    return db.execute("SELECT * FROM force_join_channels ORDER BY added_at DESC").fetchall()

def add_force_join_channel(channel_id, title=None, username=None):
    db.execute("INSERT OR REPLACE INTO force_join_channels(channel_id,title,username) VALUES (?,?,?)", (str(channel_id), title or str(channel_id), username or str(channel_id)))
    db.commit()

def remove_force_join_channel(channel_id):
    db.execute("DELETE FROM force_join_channels WHERE channel_id = ?", (str(channel_id),))
    db.commit()

def force_join_text():
    rows=get_force_join_channels()
    if not rows: return "❌ هیچ کانال عضویت اجباری ثبت نشده است."
    return "📋 <b>کانال‌های عضویت اجباری</b>\n\n" + "\n".join(f"{i}. <b>{r['title'] or r['username'] or r['channel_id']}</b> — <code>{r['channel_id']}</code>" for i,r in enumerate(rows,1))

def force_join_admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ افزودن کانال", callback_data="fj_add")],
        [InlineKeyboardButton(text="🗑 حذف کانال", callback_data="fj_remove")],
        [InlineKeyboardButton(text="📋 لیست کانال‌ها", callback_data="fj_list")],
        [InlineKeyboardButton(text="🧪 تست کانال‌ها", callback_data="fj_test")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_home")]
    ])

# ============================================================
# BOT
# ============================================================

dp = Dispatcher()

admin_actions = {}


# ============================================================
# KEYBOARDS
# ============================================================

def main_keyboard():

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🔎 جستجو با آیدی")
            ],
        ],
        resize_keyboard=True,
        is_persistent=True
    )


def start_inline_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 خرید اشتراک",
                    callback_data="buy"
                ),
                InlineKeyboardButton(
                    text="📞 ارتباط با ادمین",
                    callback_data="admin"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📖 راهنما",
                    callback_data="help"
                )
            ]
        ]
    )


def force_join_keyboard():
    buttons=[]
    for row in get_force_join_channels():
        u=str(row["username"] or row["channel_id"])
        if u.startswith("@"): u=u[1:]
        if u and not u.startswith("-100"):
            buttons.append([InlineKeyboardButton(text=f"📢 عضویت در {row['title'] or u}", url=f"https://t.me/{u}")])
    buttons.append([InlineKeyboardButton(text="✅ بررسی عضویت", callback_data="check_join")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📊 آمار",
                    callback_data="admin_stats"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📢 ارسال همگانی",
                    callback_data="broadcast"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🚫 مسدود کردن",
                    callback_data="block"
                ),
                InlineKeyboardButton(
                    text="✅ رفع مسدودی",
                    callback_data="unblock"
                )
            ],
            [InlineKeyboardButton(text="📢 مدیریت عضویت اجباری", callback_data="force_join_admin")]
        ]
    )


# ============================================================
# MEMBERSHIP
# ============================================================

async def check_membership(bot: Bot, user_id: int):
    rows=get_force_join_channels()
    if not rows: return True
    for row in rows:
        try:
            member=await bot.get_chat_member(chat_id=row["channel_id"], user_id=user_id)
            if member.status not in {"member","administrator","creator"}: return False
        except Exception as error:
            logger.warning("Membership check failed for %s: %s", row["channel_id"], error)
            return False
    return True


# ============================================================
# START TEXT
# ============================================================

def start_text(user_id):

    return (
        "🌟 <b>بات جستجوی حرفه‌ای</b> 🌟\n\n"
        "🔍 <b>سرویس جستجو</b>\n\n"
        "👤 <b>اطلاعات شما:</b>\n"
        f"• آیدی: <code>{user_id}</code>\n"
        "• وضعیت: <b>فعال ✅</b>\n\n"
        "ℹ️ برای استفاده از امکانات، ابتدا راهنما را مطالعه کنید.\n\n"
        "🔎 <b>جستجو:</b> برای جستجوی یک Telegram ID "
        "از دکمه پایین استفاده کنید."
    )


# ============================================================
# START
# ============================================================

@dp.message(CommandStart())
async def start_handler(message: Message, bot: Bot):

    user = message.from_user

    add_user(user)

    if is_blocked(user.id):

        await message.answer(
            "🚫 دسترسی شما به این ربات مسدود شده است."
        )

        return

    joined = await check_membership(
        bot,
        user.id
    )

    if not joined:

        await message.answer(
            "🔐 <b>عضویت الزامی</b>\n\n"
            "برای استفاده از ربات ابتدا در کانال ما عضو شوید "
            "و سپس روی «بررسی عضویت» بزنید.",
            parse_mode="HTML",
            reply_markup=force_join_keyboard()
        )

        return

    await message.answer(
        start_text(user.id),
        parse_mode="HTML",
        reply_markup=start_inline_keyboard()
    )

    await message.answer(
        "از منوی زیر استفاده کنید 👇",
        reply_markup=main_keyboard()
    )


# ============================================================
# CHECK JOIN
# ============================================================

@dp.callback_query(F.data == "check_join")
async def check_join_callback(
    callback: CallbackQuery,
    bot: Bot
):

    user = callback.from_user

    joined = await check_membership(
        bot,
        user.id
    )

    if not joined:

        await callback.answer(
            "❌ هنوز عضویت شما تأیید نشده است.",
            show_alert=True
        )

        return

    await callback.message.edit_text(
        start_text(user.id),
        parse_mode="HTML",
        reply_markup=start_inline_keyboard()
    )

    await callback.message.answer(
        "✅ دسترسی شما فعال شد.",
        reply_markup=main_keyboard()
    )

    await callback.answer()


# ============================================================
# INLINE MENU
# ============================================================

@dp.callback_query(F.data == "help")
async def help_callback(callback: CallbackQuery):

    text = (
        "📖 <b>راهنمای استفاده</b>\n\n"
        "1️⃣ ابتدا در کانال عضو شوید.\n"
        "2️⃣ روی «🔎 جستجو با آیدی» بزنید.\n"
        "3️⃣ Telegram ID موردنظر را ارسال کنید.\n\n"
        "⚠️ فقط اطلاعاتی نمایش داده می‌شود که "
        "ربات به‌صورت مجاز به آن دسترسی داشته باشد."
    )

    await callback.message.answer(
        text,
        parse_mode="HTML"
    )

    await callback.answer()


@dp.callback_query(F.data == "buy")
async def buy_callback(callback: CallbackQuery):

    await callback.message.answer(
        "💳 <b>خرید اشتراک</b>\n\n"
        "برای فعال‌سازی اشتراک با ادمین ارتباط بگیرید.",
        parse_mode="HTML"
    )

    await callback.answer()


@dp.callback_query(F.data == "admin")
async def admin_callback(callback: CallbackQuery):

    await callback.message.answer(
        "📞 برای ارتباط با ادمین از این آیدی استفاده کنید:\n\n"
        f"@{(await callback.bot.get_me()).username}",
        parse_mode="HTML"
    )

    await callback.answer()


# ============================================================
# SEARCH BUTTON
# ============================================================

@dp.message(F.text == "🔎 جستجو با آیدی")
async def search_button(message: Message):

    if is_blocked(message.from_user.id):

        await message.answer(
            "🚫 دسترسی شما مسدود است."
        )

        return

    await message.answer(
        "🔎 <b>جستجو با Telegram ID</b>\n\n"
        "لطفاً شناسه عددی کاربر را ارسال کنید.\n\n"
        "مثال:\n"
        "<code>123456789</code>",
        parse_mode="HTML"
    )


# ============================================================
# ID SEARCH
# ============================================================

@dp.message(F.text)
async def text_handler(message: Message, bot: Bot):

    if await admin_channel_action(message, bot):
        return

    if message.text.startswith("/"):
        return

    if is_blocked(message.from_user.id):

        await message.answer(
            "🚫 دسترسی شما مسدود است."
        )

        return

    text = message.text.strip()

    if not text.isdigit():

        await message.answer(
            "❌ شناسه نامعتبر است.\n\n"
            "لطفاً فقط Telegram ID عددی ارسال کنید."
        )

        return

    try:

        target_id = int(text)

    except ValueError:

        await message.answer(
            "❌ شناسه واردشده معتبر نیست."
        )

        return

    if target_id <= 0:

        await message.answer(
            "❌ Telegram ID معتبر نیست."
        )

        return

    # ثبت آمار جستجو
    save_search(
        message.from_user.id,
        target_id
    )

    # --------------------------------------------------------
    # Telegram Bot API اطلاعات عمومی محدودی از ID دارد.
    # نمی‌توان از یک ID دلخواه، شماره تلفن یا اطلاعات خصوصی
    # شخص را استخراج کرد.
    # --------------------------------------------------------

    result = (
        "🔎 <b>نتیجه جستجو</b>\n\n"
        f"🆔 آیدی: <code>{target_id}</code>\n\n"
        "ℹ️ ربات نمی‌تواند صرفاً با داشتن Telegram ID، "
        "شماره تلفن یا اطلاعات خصوصی صاحب حساب را استخراج کند.\n\n"
        "اگر این کاربر با ربات تعامل داشته باشد، "
        "می‌توان اطلاعات مجاز مربوط به تعامل او را ثبت کرد."
    )

    await message.answer(
        result,
        parse_mode="HTML"
    )


# ============================================================
# ADMIN PANEL
# ============================================================

@dp.message(Command("admin"))
async def admin_command(message: Message):

    if message.from_user.id != ADMIN_ID:

        await message.answer(
            "⛔ دسترسی غیرمجاز."
        )

        return

    await message.answer(
        "🛠 <b>پنل مدیریت</b>\n\n"
        "یکی از گزینه‌ها را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=admin_keyboard()
    )


@dp.callback_query(F.data == "admin_home")
async def admin_home_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return await callback.answer("⛔ دسترسی غیرمجاز.", show_alert=True)
    await callback.message.edit_text("🛠 <b>پنل مدیریت</b>\n\nیکی از گزینه‌ها را انتخاب کنید:", parse_mode="HTML", reply_markup=admin_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "force_join_admin")
async def force_join_admin(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return await callback.answer("⛔ دسترسی غیرمجاز.", show_alert=True)
    await callback.message.edit_text("📢 <b>مدیریت عضویت اجباری</b>\n\n"+force_join_text(), parse_mode="HTML", reply_markup=force_join_admin_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "fj_list")
async def fj_list(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return await callback.answer("⛔ دسترسی غیرمجاز.", show_alert=True)
    await callback.message.edit_text(force_join_text(), parse_mode="HTML", reply_markup=force_join_admin_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "fj_add")
async def fj_add(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return await callback.answer("⛔ دسترسی غیرمجاز.", show_alert=True)
    admin_actions[callback.from_user.id]="add_channel"
    await callback.message.answer("➕ آیدی کانال را ارسال کن.\nمثال: <code>@THEASYLUM2</code> یا <code>-1001234567890</code>", parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "fj_remove")
async def fj_remove(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return await callback.answer("⛔ دسترسی غیرمجاز.", show_alert=True)
    admin_actions[callback.from_user.id]="remove_channel"
    await callback.message.answer("🗑 آیدی کانالی که می‌خواهی حذف شود را ارسال کن.")
    await callback.answer()

@dp.callback_query(F.data == "fj_test")
async def fj_test(callback: CallbackQuery, bot: Bot):
    if callback.from_user.id != ADMIN_ID: return await callback.answer("⛔ دسترسی غیرمجاز.", show_alert=True)
    me=await bot.get_me(); results=[]
    for row in get_force_join_channels():
        try:
            chat=await bot.get_chat(row["channel_id"]); member=await bot.get_chat_member(chat.id, me.id)
            results.append(f"{'✅' if member.status in {'administrator','creator'} else '❌'} {chat.title or row['channel_id']}")
        except Exception:
            results.append(f"❌ {row['channel_id']} — دسترسی ندارد")
    await callback.message.answer("🧪 <b>نتیجه تست</b>\n\n"+("\n".join(results) if results else "کانالی وجود ندارد."), parse_mode="HTML")
    await callback.answer()

async def admin_channel_action(message: Message, bot: Bot):
    uid=message.from_user.id; action=admin_actions.get(uid)
    if uid!=ADMIN_ID or not action: return False
    value=message.text.strip()
    if action=="add_channel":
        try:
            chat=await bot.get_chat(value); me=await bot.get_chat_member(chat.id,(await bot.get_me()).id)
            if me.status not in {"administrator","creator"}:
                await message.answer("❌ ربات باید در این کانال ادمین باشد."); return True
            username=f"@{chat.username}" if chat.username else str(chat.id)
            add_force_join_channel(str(chat.id),chat.title,username)
            await message.answer("✅ کانال به عضویت اجباری اضافه شد.",reply_markup=admin_keyboard())
        except Exception:
            await message.answer("❌ کانال پیدا نشد یا ربات به آن دسترسی ندارد.")
        finally: admin_actions.pop(uid,None)
        return True
    if action=="remove_channel":
        remove_force_join_channel(value); admin_actions.pop(uid,None)
        await message.answer("✅ کانال از عضویت اجباری حذف شد.",reply_markup=admin_keyboard()); return True
    return False

# ============================================================
# ADMIN STATS
# ============================================================

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):

    if callback.from_user.id != ADMIN_ID:

        await callback.answer(
            "⛔ دسترسی غیرمجاز.",
            show_alert=True
        )

        return

    users, searches, blocked = get_stats()

    text = (
        "📊 <b>آمار ربات</b>\n\n"
        f"👥 کاربران: <b>{users}</b>\n"
        f"🔎 جستجوها: <b>{searches}</b>\n"
        f"🚫 کاربران مسدود: <b>{blocked}</b>"
    )

    await callback.message.answer(
        text,
        parse_mode="HTML"
    )

    await callback.answer()


# ============================================================
# ADMIN BLOCK
# ============================================================

@dp.callback_query(F.data == "block")
async def block_callback(callback: CallbackQuery):

    if callback.from_user.id != ADMIN_ID:

        await callback.answer(
            "⛔ دسترسی غیرمجاز.",
            show_alert=True
        )

        return

    await callback.message.answer(
        "🚫 آیدی کاربری که می‌خواهید مسدود شود را ارسال کنید."
    )

    await callback.answer()


@dp.callback_query(F.data == "unblock")
async def unblock_callback(callback: CallbackQuery):

    if callback.from_user.id != ADMIN_ID:

        await callback.answer(
            "⛔ دسترسی غیرمجاز.",
            show_alert=True
        )

        return

    await callback.message.answer(
        "✅ آیدی کاربری که می‌خواهید رفع مسدود شود را ارسال کنید."
    )

    await callback.answer()


# ============================================================
# ADMIN TEXT COMMANDS
# ============================================================

@dp.message(Command("block"))
async def block_command(message: Message):

    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.split()

    if len(parts) != 2 or not parts[1].isdigit():

        await message.answer(
            "فرمت صحیح:\n"
            "/block 123456789"
        )

        return

    user_id = int(parts[1])

    set_blocked(
        user_id,
        True
    )

    await message.answer(
        f"🚫 کاربر <code>{user_id}</code> مسدود شد.",
        parse_mode="HTML"
    )


@dp.message(Command("unblock"))
async def unblock_command(message: Message):

    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.split()

    if len(parts) != 2 or not parts[1].isdigit():

        await message.answer(
            "فرمت صحیح:\n"
            "/unblock 123456789"
        )

        return

    user_id = int(parts[1])

    set_blocked(
        user_id,
        False
    )

    await message.answer(
        f"✅ کاربر <code>{user_id}</code> رفع مسدودی شد.",
        parse_mode="HTML"
    )


# ============================================================
# BROADCAST
# ============================================================

@dp.callback_query(F.data == "broadcast")
async def broadcast_callback(callback: CallbackQuery):

    if callback.from_user.id != ADMIN_ID:

        await callback.answer(
            "⛔ دسترسی غیرمجاز.",
            show_alert=True
        )

        return

    await callback.message.answer(
        "📢 برای ارسال همگانی، پیام را با دستور زیر بفرست:\n\n"
        "<code>/broadcast متن پیام</code>",
        parse_mode="HTML"
    )

    await callback.answer()


@dp.message(Command("broadcast"))
async def broadcast_command(
    message: Message,
    bot: Bot
):

    if message.from_user.id != ADMIN_ID:
        return

    text = message.text.partition(" ")[2].strip()

    if not text:

        await message.answer(
            "متن پیام را وارد کن.\n\n"
            "مثال:\n"
            "/broadcast سلام به همه کاربران"
        )

        return

    users = get_all_users()

    success = 0
    failed = 0

    for row in users:

        user_id = row["user_id"]

        try:

            await bot.send_message(
                user_id,
                text
            )

            success += 1

            await asyncio.sleep(0.05)

        except Exception:

            failed += 1

    await message.answer(
        "📢 <b>ارسال همگانی تمام شد</b>\n\n"
        f"✅ موفق: {success}\n"
        f"❌ ناموفق: {failed}",
        parse_mode="HTML"
    )


# ============================================================
# UNKNOWN TEXT
# ============================================================

@dp.message()
async def fallback_handler(message: Message):

    await message.answer(
        "از منوی زیر استفاده کنید 👇",
        reply_markup=main_keyboard()
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    init_db()

    bot = None

    try:

        logger.info("Starting bot...")

        if PROXY_URL:

            session = AiohttpSession(
                proxy=PROXY_URL
            )

            bot = Bot(
                token=BOT_TOKEN,
                session=session
            )

        else:

            bot = Bot(
                token=BOT_TOKEN
            )

        me = await bot.get_me()

        logger.info(
            "Connected as @%s (%s)",
            me.username,
            me.id
        )

        await bot.delete_webhook(
            drop_pending_updates=True
        )

        await dp.start_polling(bot)

    except Exception as error:

        logger.exception(
            "BOT ERROR: %s",
            error
        )

    finally:

        if bot:

            with suppress(Exception):

                await bot.session.close()

        db.close()


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        logger.info("Bot stopped.")