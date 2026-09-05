"""
MHDV Telegram Bot — hammasi shu bitta main.py fayl ichida.
Render.com'da ishga tushirishga moslashtirilgan (aiohttp health-check server + polling).

Ishga tushirish:
    pip install -r requirements.txt
    python main.py

.env fayl namunasi uchun .env.example ga qarang.
"""

import os
import csv
import json
import asyncio
import logging
import datetime
from io import StringIO, BytesIO

import aiosqlite
from dotenv import load_dotenv
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
    FSInputFile,
    BufferedInputFile,
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

# =============================================================================
# 1. KONFIGURATSIYA (.env)
# =============================================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

_raw_admins = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in _raw_admins.split(",") if x.strip().isdigit()]

DB_PATH = os.getenv("DB_PATH", "mhdv_bot.db")
PORT = int(os.getenv("PORT", "10000"))  # Render avtomatik PORT beradi

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("mhdv_bot")

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN topilmadi! Render'da Environment Variables bo'limiga "
        "BOT_TOKEN qo'shing (.env.example ga qarang)."
    )
if not ADMIN_IDS:
    logger.warning(
        "ADMIN_IDS bo'sh! Hech kim admin panelga kira olmaydi. "
        "Render Environment Variables ichiga ADMIN_IDS=123456789 kabi qo'shing."
    )

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

PAGE_SIZE = 10  # foydalanuvchilar/zakazlar ro'yxatida bir sahifadagi elementlar soni

DEFAULT_SETTINGS = {
    "website_info": "Veb-sayt xizmatlari haqida ma'lumot hali admin tomonidan kiritilmagan.",
    "logo_info": "Logo xizmatlari haqida ma'lumot hali admin tomonidan kiritilmagan.",
    "bot_info": "Bu bot — MHDV jamoasining rasmiy yordamchi botidir. U orqali veb-sayt "
                "va logo buyurtma berishingiz, takliflaringizni yetkazishingiz mumkin.",
    "mhdv_info": "MHDV haqida ma'lumot hali admin tomonidan kiritilmagan.",
    "card_number": "0000 0000 0000 0000",
    "card_owner": "F.I.Sh kiritilmagan",
    "social_links": "Ijtimoiy tarmoqlar hali admin tomonidan kiritilmagan.",
    "faq": "Tez-tez so'raladigan savollar hali admin tomonidan kiritilmagan.",
    "admin_contact_name": "MHDV Admin",
    "admin_contact_phone": "",
    "maintenance_mode": "0",
}

# =============================================================================
# 2. MA'LUMOTLAR BAZASI (SQLite / aiosqlite)
# =============================================================================


def now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER UNIQUE NOT NULL,
                full_name TEXT,
                username TEXT,
                is_blocked INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER NOT NULL,
                order_type TEXT NOT NULL,
                data TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                reject_reason TEXT,
                created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                admin_reply TEXT,
                created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER,
                tg_id INTEGER NOT NULL,
                file_id TEXT,
                file_type TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER NOT NULL,
                direction TEXT NOT NULL,
                text TEXT,
                created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER NOT NULL,
                score INTEGER NOT NULL,
                created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.commit()
        for key, value in DEFAULT_SETTINGS.items():
            await db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value)
            )
        await db.commit()


# ---------- USERS ----------

async def db_add_user(tg_id, full_name, username):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        if row is None:
            await db.execute(
                "INSERT INTO users (tg_id, full_name, username, created_at) VALUES (?, ?, ?, ?)",
                (tg_id, full_name, username, now()),
            )
            await db.commit()
            return True  # yangi foydalanuvchi
        await db.execute(
            "UPDATE users SET full_name=?, username=? WHERE tg_id=?",
            (full_name, username, tg_id),
        )
        await db.commit()
        return False


async def db_get_user(tg_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
        return await cur.fetchone()


async def db_get_all_users():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users ORDER BY id DESC")
        return await cur.fetchall()


async def db_is_blocked(tg_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT is_blocked FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        return bool(row[0]) if row else False


async def db_toggle_block(tg_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT is_blocked FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        if row is None:
            return None
        new_val = 0 if row[0] else 1
        await db.execute("UPDATE users SET is_blocked=? WHERE tg_id=?", (new_val, tg_id))
        await db.commit()
        return bool(new_val)


async def db_count_users_today():
    today = datetime.date.today().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users WHERE created_at LIKE ?", (f"{today}%",))
        return (await cur.fetchone())[0]


async def db_count_users_total():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        return (await cur.fetchone())[0]


async def db_count_users_blocked():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users WHERE is_blocked=1")
        return (await cur.fetchone())[0]


# ---------- ORDERS ----------

async def db_create_order(tg_id, order_type, data: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO orders (tg_id, order_type, data, created_at) VALUES (?, ?, ?, ?)",
            (tg_id, order_type, json.dumps(data, ensure_ascii=False), now()),
        )
        await db.commit()
        return cur.lastrowid


async def db_get_order(order_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM orders WHERE id=?", (order_id,))
        return await cur.fetchone()


async def db_get_orders_by_type(order_type, status=None):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if status:
            cur = await db.execute(
                "SELECT * FROM orders WHERE order_type=? AND status=? ORDER BY id DESC",
                (order_type, status),
            )
        else:
            cur = await db.execute(
                "SELECT * FROM orders WHERE order_type=? ORDER BY id DESC", (order_type,)
            )
        return await cur.fetchall()


async def db_get_orders_by_user(tg_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM orders WHERE tg_id=? ORDER BY id DESC", (tg_id,))
        return await cur.fetchall()


async def db_set_order_status(order_id, status, reject_reason=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE orders SET status=?, reject_reason=? WHERE id=?",
            (status, reject_reason, order_id),
        )
        await db.commit()


async def db_orders_stats_today():
    today = datetime.date.today().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT status, COUNT(*) as c FROM orders WHERE created_at LIKE ? GROUP BY status",
            (f"{today}%",),
        )
        rows = await cur.fetchall()
        return {r["status"]: r["c"] for r in rows}


async def db_orders_count_total():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM orders")
        return (await cur.fetchone())[0]


# ---------- FEEDBACK ----------

async def db_add_feedback(tg_id, kind, text):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO feedback (tg_id, kind, text, created_at) VALUES (?, ?, ?, ?)",
            (tg_id, kind, text, now()),
        )
        await db.commit()
        return cur.lastrowid


async def db_get_feedback(feedback_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM feedback WHERE id=?", (feedback_id,))
        return await cur.fetchone()


async def db_get_feedback_list():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM feedback ORDER BY id DESC")
        return await cur.fetchall()


async def db_set_feedback_reply(feedback_id, reply_text):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE feedback SET admin_reply=? WHERE id=?", (reply_text, feedback_id))
        await db.commit()


# ---------- PAYMENTS ----------

async def db_add_payment(order_id, tg_id, file_id, file_type):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO payments (order_id, tg_id, file_id, file_type, created_at) VALUES (?, ?, ?, ?, ?)",
            (order_id, tg_id, file_id, file_type, now()),
        )
        await db.commit()
        return cur.lastrowid


async def db_get_payment(payment_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM payments WHERE id=?", (payment_id,))
        return await cur.fetchone()


async def db_get_payments(status=None):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if status:
            cur = await db.execute("SELECT * FROM payments WHERE status=? ORDER BY id DESC", (status,))
        else:
            cur = await db.execute("SELECT * FROM payments ORDER BY id DESC")
        return await cur.fetchall()


async def db_confirm_payment(payment_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE payments SET status='confirmed' WHERE id=?", (payment_id,))
        await db.commit()


# ---------- MESSAGES (admin bilan yozishmalar) ----------

async def db_add_message(tg_id, direction, text):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages (tg_id, direction, text, created_at) VALUES (?, ?, ?, ?)",
            (tg_id, direction, text, now()),
        )
        await db.commit()


async def db_get_messages(tg_id, limit=15):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM messages WHERE tg_id=? ORDER BY id DESC LIMIT ?", (tg_id, limit)
        )
        rows = await cur.fetchall()
        return list(reversed(rows))


async def db_get_chat_users():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT tg_id, MAX(created_at) as last_time FROM messages GROUP BY tg_id ORDER BY last_time DESC"
        )
        return await cur.fetchall()


# ---------- RATINGS ----------

async def db_add_rating(tg_id, score):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO ratings (tg_id, score, created_at) VALUES (?, ?, ?)", (tg_id, score, now())
        )
        await db.commit()


async def db_avg_rating():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT AVG(score), COUNT(*) FROM ratings")
        row = await cur.fetchone()
        return (round(row[0], 2) if row[0] else 0, row[1])


# ---------- SETTINGS ----------

async def db_get_setting(key):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else ""


async def db_set_setting(key, value):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await db.commit()


# =============================================================================
# 3. FSM HOLATLARI
# =============================================================================

class UserFlow(StatesGroup):
    web_site_name = State()
    web_purpose = State()
    web_sphere = State()
    web_budget = State()
    web_requirements = State()
    web_additional = State()

    logo_brand_name = State()
    logo_direction = State()
    logo_budget = State()
    logo_additional = State()

    feedback_taklif = State()
    feedback_shikoyat = State()

    admin_chat = State()

    awaiting_payment = State()


class AdminFlow(StatesGroup):
    reject_reason = State()
    broadcast_text = State()
    new_setting_value = State()
    reply_to_user = State()
    reply_to_feedback = State()


# =============================================================================
# 4. TUGMA MATNLARI (Reply Keyboard)
# =============================================================================

BTN_WEBSITE = "🌐 Veb-sayt buyurtma qilish"
BTN_LOGO = "🎨 Logo buyurtma qilish"
BTN_INFO = "ℹ️ Ma'lumot"
BTN_FEEDBACK = "💬 Taklif va shikoyat"
BTN_SOCIAL = "🌍 Ijtimoiy tarmoqlar"
BTN_ADMIN_CHAT = "👨‍💼 Admin bilan muloqot"
BTN_MY_ORDERS = "📦 Mening buyurtmalarim"
BTN_FAQ = "❓ Ko'p so'raladigan savollar"
BTN_RATE = "⭐️ Botni baholash"

BTN_BACK = "🔙 Orqaga"
BTN_SKIP = "❌ Yo'q"
BTN_CANCEL = "🚫 Bekor qilish"

BTN_ABOUT_BOT = "🤖 Bot haqida"
BTN_ABOUT_MHDV = "🏢 MHDV haqida"

BTN_TAKLIF = "💡 Taklif"
BTN_SHIKOYAT = "⚠️ Shikoyat"

RATE_BUTTONS = ["⭐️", "⭐️⭐️", "⭐️⭐️⭐️", "⭐️⭐️⭐️⭐️", "⭐️⭐️⭐️⭐️⭐️"]

# ---- Admin panel tugmalari ----
ABTN_USERS = "👥 Foydalanuvchilar"
ABTN_STATS = "📊 Statistika"
ABTN_CHATS = "💬 Chatlar"
ABTN_FEEDBACK = "📝 Taklif va shikoyatlar"
ABTN_ORDERS = "🛒 Zakazlar"
ABTN_PAYMENTS = "💳 To'lovlar"
ABTN_EDIT_DATA = "✏️ Ma'lumotlarni almashtirish"
ABTN_BROADCAST = "📢 Hammaga xabar yuborish"
ABTN_EXPORT = "📁 Foydalanuvchilarni eksport qilish"
ABTN_SETTINGS = "⚙️ Bot sozlamalari"

ABTN_ORDER_WEBSITE = "🌐 Veb-sayt zakazlari"
ABTN_ORDER_LOGO = "🎨 Logo zakazlari"

ABTN_ACCEPT = "✅ Qabul qilish"
ABTN_REJECT = "❌ Rad etish"

ABTN_CONFIRM_PAYMENT = "✅ To'lovni tasdiqlash"

ABTN_WRITE_USER = "✍️ Foydalanuvchiga yozish"
ABTN_BLOCK_USER = "⛔️ Bloklash"
ABTN_UNBLOCK_USER = "✅ Blokdan chiqarish"
ABTN_USER_ORDERS = "📦 Buyurtmalari"
ABTN_PRIVATE_LINK = "💬 Shaxsiy chatga o'tish"

ABTN_NEXT_PAGE = "➡️ Keyingi"
ABTN_PREV_PAGE = "⬅️ Oldingi"

ABTN_EDIT_WEBSITE = "🌐 Veb-sayt ma'lumoti"
ABTN_EDIT_LOGO = "🎨 Logo ma'lumoti"
ABTN_EDIT_BOT = "🤖 Bot haqida ma'lumot"
ABTN_EDIT_MHDV = "🏢 MHDV haqida ma'lumot"
ABTN_EDIT_CARD_NUM = "💳 Karta raqami"
ABTN_EDIT_CARD_OWNER = "👤 Karta egasi"
ABTN_EDIT_SOCIAL = "🌍 Ijtimoiy tarmoqlar"
ABTN_EDIT_FAQ = "❓ FAQ ma'lumoti"
ABTN_EDIT_ADMIN_PHONE = "📞 Admin telefon raqami"

ABTN_MAINTENANCE_TOGGLE = "🔧 Texnik ishlar rejimini almashtirish"

SETTING_LABELS = {
    "website_info": ABTN_EDIT_WEBSITE,
    "logo_info": ABTN_EDIT_LOGO,
    "bot_info": ABTN_EDIT_BOT,
    "mhdv_info": ABTN_EDIT_MHDV,
    "card_number": ABTN_EDIT_CARD_NUM,
    "card_owner": ABTN_EDIT_CARD_OWNER,
    "social_links": ABTN_EDIT_SOCIAL,
    "faq": ABTN_EDIT_FAQ,
    "admin_contact_phone": ABTN_EDIT_ADMIN_PHONE,
}
LABEL_TO_SETTING = {v: k for k, v in SETTING_LABELS.items()}


# =============================================================================
# 5. KLAVIATURALAR (Reply Keyboard)
# =============================================================================

def kb(rows, resize=True, placeholder=None):
    keyboard = [[KeyboardButton(text=t) for t in row] for row in rows]
    return ReplyKeyboardMarkup(
        keyboard=keyboard, resize_keyboard=resize, input_field_placeholder=placeholder
    )


def main_menu_kb():
    return kb([
        [BTN_WEBSITE],
        [BTN_LOGO],
        [BTN_INFO, BTN_FEEDBACK],
        [BTN_SOCIAL, BTN_ADMIN_CHAT],
        [BTN_MY_ORDERS, BTN_FAQ],
        [BTN_RATE],
    ])


def back_kb():
    return kb([[BTN_BACK]])


def skip_back_kb():
    return kb([[BTN_SKIP], [BTN_BACK]])


def cancel_kb():
    return kb([[BTN_CANCEL]])


def info_menu_kb():
    return kb([[BTN_ABOUT_BOT], [BTN_ABOUT_MHDV], [BTN_BACK]])


def feedback_menu_kb():
    return kb([[BTN_TAKLIF], [BTN_SHIKOYAT], [BTN_BACK]])


def rate_kb():
    return kb([RATE_BUTTONS[:3], RATE_BUTTONS[3:], [BTN_BACK]])


def admin_menu_kb():
    return kb([
        [ABTN_USERS, ABTN_STATS],
        [ABTN_CHATS, ABTN_FEEDBACK],
        [ABTN_ORDERS, ABTN_PAYMENTS],
        [ABTN_EDIT_DATA, ABTN_BROADCAST],
        [ABTN_EXPORT, ABTN_SETTINGS],
    ])


def order_type_menu_kb():
    return kb([[ABTN_ORDER_WEBSITE], [ABTN_ORDER_LOGO], [BTN_BACK]])


def edit_data_menu_kb():
    return kb([
        [ABTN_EDIT_WEBSITE], [ABTN_EDIT_LOGO],
        [ABTN_EDIT_BOT], [ABTN_EDIT_MHDV],
        [ABTN_EDIT_CARD_NUM], [ABTN_EDIT_CARD_OWNER],
        [ABTN_EDIT_ADMIN_PHONE],
        [ABTN_EDIT_SOCIAL], [ABTN_EDIT_FAQ],
        [BTN_BACK],
    ])


def settings_menu_kb():
    return kb([[ABTN_MAINTENANCE_TOGGLE], [BTN_BACK]])


def paginated_list_kb(items_labels, page, page_size=PAGE_SIZE, extra_rows=None):
    """items_labels: har bir element uchun tugma matni ro'yxati."""
    start = page * page_size
    chunk = items_labels[start:start + page_size]
    rows = [[label] for label in chunk]
    nav_row = []
    if page > 0:
        nav_row.append(ABTN_PREV_PAGE)
    if start + page_size < len(items_labels):
        nav_row.append(ABTN_NEXT_PAGE)
    if nav_row:
        rows.append(nav_row)
    if extra_rows:
        rows.extend(extra_rows)
    rows.append([BTN_BACK])
    return kb(rows)


def user_card_kb(is_blocked_flag):
    block_btn = ABTN_UNBLOCK_USER if is_blocked_flag else ABTN_BLOCK_USER
    return kb([
        [ABTN_WRITE_USER],
        [block_btn],
        [ABTN_USER_ORDERS],
        [ABTN_PRIVATE_LINK],
        [BTN_BACK],
    ])


def order_action_kb():
    return kb([[ABTN_ACCEPT], [ABTN_REJECT], [BTN_BACK]])


def payment_action_kb():
    return kb([[ABTN_CONFIRM_PAYMENT], [BTN_BACK]])


# =============================================================================
# 6. YORDAMCHI FUNKSIYALAR
# =============================================================================

def is_admin(tg_id: int) -> bool:
    return tg_id in ADMIN_IDS


def get_user_fsm(tg_id: int) -> FSMContext:
    """Berilgan foydalanuvchi uchun FSMContext (private chat: chat_id == user_id)."""
    from aiogram.fsm.storage.base import StorageKey
    return FSMContext(
        storage=dp.storage,
        key=StorageKey(bot_id=bot.id, chat_id=tg_id, user_id=tg_id),
    )


def user_display_name(user_row) -> str:
    name = user_row["full_name"] or "Noma'lum"
    uname = f"@{user_row['username']}" if user_row["username"] else "username yo'q"
    return f"{name} ({uname}) — ID:{user_row['tg_id']}"


async def notify_admins(text: str, exclude=None):
    for admin_id in ADMIN_IDS:
        if exclude and admin_id == exclude:
            continue
        try:
            await bot.send_message(admin_id, text)
        except (TelegramForbiddenError, TelegramBadRequest):
            pass


async def safe_send(tg_id: int, text: str, **kwargs):
    try:
        await bot.send_message(tg_id, text, **kwargs)
        return True
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        logger.warning(f"Xabar yuborib bo'lmadi ({tg_id}): {e}")
        return False


ORDER_TYPE_TITLES = {"website": "🌐 Veb-sayt", "logo": "🎨 Logo"}


def format_website_order(data: dict) -> str:
    return (
        f"🌐 <b>Veb-sayt buyurtmasi</b>\n\n"
        f"📌 Sayt nomi: {data.get('site_name')}\n"
        f"🎯 Maqsad: {data.get('purpose')}\n"
        f"🏷 Soha: {data.get('sphere')}\n"
        f"💰 Byudjet: {data.get('budget')}\n"
        f"📋 Asosiy talablar: {data.get('requirements')}\n"
        f"➕ Qo'shimcha: {data.get('additional')}"
    )


def format_logo_order(data: dict) -> str:
    return (
        f"🎨 <b>Logo buyurtmasi</b>\n\n"
        f"🏷 Brend nomi: {data.get('brand_name')}\n"
        f"🧭 Brend yo'nalishi: {data.get('direction')}\n"
        f"💰 Byudjet: {data.get('budget')}\n"
        f"➕ Qo'shimcha: {data.get('additional')}"
    )


def format_order_short(order_row) -> str:
    data = json.loads(order_row["data"])
    if order_row["order_type"] == "website":
        title = data.get("site_name", "—")
    else:
        title = data.get("brand_name", "—")
    status_icon = {"pending": "🕓", "accepted": "✅", "rejected": "❌"}.get(order_row["status"], "🕓")
    return f"{status_icon} #{order_row['id']} — {title} ({order_row['created_at']})"


# =============================================================================
# 7. FOYDALANUVCHI OQIMLARI (Reply Keyboard asosida)
# =============================================================================

WEBSITE_STEPS = [
    (UserFlow.web_site_name, "🌐 Veb-sayt nomini kiriting (yoki tахminiy nomini yozing):", back_kb),
    (UserFlow.web_purpose, "🎯 Sayt nima uchun kerak? Maqsadini qisqacha yozing:", back_kb),
    (UserFlow.web_sphere, "🏷 Qaysi sohada faoliyat yuritasiz? (masalan: savdo, ta'lim, restoran...):", back_kb),
    (UserFlow.web_budget, "💰 Loyihaga ajratilgan byudjetingiz qancha?:", back_kb),
    (UserFlow.web_requirements, "📋 Saytga bo'lgan asosiy talablaringizni yozing:", back_kb),
    (UserFlow.web_additional, "➕ Qo'shimcha ma'lumot bo'lsa yozing. Bo'lmasa \"❌ Yo'q\" tugmasini bosing:", skip_back_kb),
]
WEBSITE_KEYS = ["site_name", "purpose", "sphere", "budget", "requirements", "additional"]
WEBSITE_STATES = [s for s, _, _ in WEBSITE_STEPS]

LOGO_STEPS = [
    (UserFlow.logo_brand_name, "🏷 Brend/kompaniya nomini kiriting:", back_kb),
    (UserFlow.logo_direction, "🧭 Brendingiz qaysi yo'nalishda faoliyat yuritadi?:", back_kb),
    (UserFlow.logo_budget, "💰 Logo uchun ajratilgan byudjetingiz qancha?:", back_kb),
    (UserFlow.logo_additional, "➕ Qo'shimcha ma'lumot (ranglar, uslub va h.k.) bo'lsa yozing. "
                               "Bo'lmasa \"❌ Yo'q\" tugmasini bosing:", skip_back_kb),
]
LOGO_KEYS = ["brand_name", "direction", "budget", "additional"]
LOGO_STATES = [s for s, _, _ in LOGO_STEPS]


async def start_flow(message: Message, state: FSMContext, steps, data_key: str):
    await state.clear()
    await state.update_data(**{data_key: {}})
    first_state, prompt, kb_func = steps[0]
    await state.set_state(first_state)
    await message.answer(prompt, reply_markup=kb_func())


async def step_forward(message: Message, state: FSMContext, steps, keys, data_key: str, finish_fn, value=None):
    current = await state.get_state()
    idx = next(i for i, (s, _, _) in enumerate(steps) if s.state == current)
    data = await state.get_data()
    bucket = data.get(data_key, {})
    bucket[keys[idx]] = value if value is not None else message.text.strip()
    await state.update_data(**{data_key: bucket})

    if idx + 1 < len(steps):
        next_state, prompt, kb_func = steps[idx + 1]
        await state.set_state(next_state)
        await message.answer(prompt, reply_markup=kb_func())
    else:
        await finish_fn(message, state, bucket)


async def step_back(message: Message, state: FSMContext, steps):
    current = await state.get_state()
    idx = next(i for i, (s, _, _) in enumerate(steps) if s.state == current)
    if idx == 0:
        await state.clear()
        await message.answer("🏠 Bosh menyu:", reply_markup=main_menu_kb())
    else:
        prev_state, prompt, kb_func = steps[idx - 1]
        await state.set_state(prev_state)
        await message.answer(f"⬅️ Oldingi savolga qaytdik.\n\n{prompt}", reply_markup=kb_func())


async def finish_website_order(message: Message, state: FSMContext, bucket: dict):
    order_id = await db_create_order(message.from_user.id, "website", bucket)
    await state.clear()
    await message.answer(
        "✅ Buyurtmangiz qabul qilindi! Tez orada admin ko'rib chiqadi va siz bilan bog'lanadi.\n\n"
        + format_website_order(bucket),
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )
    user = await db_get_user(message.from_user.id)
    await notify_admins(
        f"🆕 Yangi <b>veb-sayt</b> buyurtmasi (#{order_id})\n"
        f"👤 {user_display_name(user)}\n\n{format_website_order(bucket)}\n\n"
        f"Ko'rish uchun: Admin panel → {ABTN_ORDERS} → {ABTN_ORDER_WEBSITE}"
    )


async def finish_logo_order(message: Message, state: FSMContext, bucket: dict):
    order_id = await db_create_order(message.from_user.id, "logo", bucket)
    await state.clear()
    await message.answer(
        "✅ Buyurtmangiz qabul qilindi! Tez orada admin ko'rib chiqadi va siz bilan bog'lanadi.\n\n"
        + format_logo_order(bucket),
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )
    user = await db_get_user(message.from_user.id)
    await notify_admins(
        f"🆕 Yangi <b>logo</b> buyurtmasi (#{order_id})\n"
        f"👤 {user_display_name(user)}\n\n{format_logo_order(bucket)}\n\n"
        f"Ko'rish uchun: Admin panel → {ABTN_ORDERS} → {ABTN_ORDER_LOGO}"
    )


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()

    if is_admin(message.from_user.id):
        await enter_admin_mode(message, state)
        return

    if await db_is_blocked(message.from_user.id):
        await message.answer("⛔️ Siz botdan foydalanish huquqidan mahrum qilingansiz.")
        return
    is_new = await db_add_user(
        message.from_user.id, message.from_user.full_name, message.from_user.username
    )
    name = message.from_user.full_name or "mehmon"
    await message.answer(
        f"👋 Salom, {name}! MHDV botiga xush kelibsiz.\n\n"
        f"Quyidagi menyudan kerakli bo'limni tanlang 👇",
        reply_markup=main_menu_kb(),
    )
    if is_new:
        await notify_admins(f"🆕 Yangi foydalanuvchi botga qo'shildi:\n{message.from_user.full_name} "
                             f"(@{message.from_user.username or '—'}) — ID:{message.from_user.id}")


@dp.message(F.text == BTN_WEBSITE)
async def btn_website(message: Message, state: FSMContext):
    if await db_is_blocked(message.from_user.id):
        return
    await start_flow(message, state, WEBSITE_STEPS, "web_data")


@dp.message(F.text == BTN_BACK, StateFilter(*WEBSITE_STATES))
async def website_back(message: Message, state: FSMContext):
    await step_back(message, state, WEBSITE_STEPS)


@dp.message(F.text == BTN_SKIP, StateFilter(UserFlow.web_additional))
async def website_skip_additional(message: Message, state: FSMContext):
    await step_forward(message, state, WEBSITE_STEPS, WEBSITE_KEYS, "web_data", finish_website_order, value="—")


@dp.message(StateFilter(*WEBSITE_STATES))
async def website_step_input(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Iltimos, matn ko'rinishida javob yozing.")
        return
    await step_forward(message, state, WEBSITE_STEPS, WEBSITE_KEYS, "web_data", finish_website_order)


@dp.message(F.text == BTN_LOGO)
async def btn_logo(message: Message, state: FSMContext):
    if await db_is_blocked(message.from_user.id):
        return
    await start_flow(message, state, LOGO_STEPS, "logo_data")


@dp.message(F.text == BTN_BACK, StateFilter(*LOGO_STATES))
async def logo_back(message: Message, state: FSMContext):
    await step_back(message, state, LOGO_STEPS)


@dp.message(F.text == BTN_SKIP, StateFilter(UserFlow.logo_additional))
async def logo_skip_additional(message: Message, state: FSMContext):
    await step_forward(message, state, LOGO_STEPS, LOGO_KEYS, "logo_data", finish_logo_order, value="—")


@dp.message(StateFilter(*LOGO_STATES))
async def logo_step_input(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Iltimos, matn ko'rinishida javob yozing.")
        return
    await step_forward(message, state, LOGO_STEPS, LOGO_KEYS, "logo_data", finish_logo_order)


@dp.message(F.text == BTN_INFO)
async def btn_info(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("ℹ️ Ma'lumot bo'limi. Nimani bilmoqchisiz?", reply_markup=info_menu_kb())


@dp.message(F.text == BTN_ABOUT_BOT)
async def btn_about_bot(message: Message):
    text = await db_get_setting("bot_info")
    await message.answer(f"🤖 <b>Bot haqida</b>\n\n{text}", reply_markup=info_menu_kb(), parse_mode="HTML")


@dp.message(F.text == BTN_ABOUT_MHDV)
async def btn_about_mhdv(message: Message):
    text = await db_get_setting("mhdv_info")
    await message.answer(f"🏢 <b>MHDV haqida</b>\n\n{text}", reply_markup=info_menu_kb(), parse_mode="HTML")


@dp.message(F.text == BTN_FEEDBACK)
async def btn_feedback(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("💬 Taklif yoki shikoyatingizni tanlang:", reply_markup=feedback_menu_kb())


@dp.message(F.text == BTN_TAKLIF)
async def btn_taklif(message: Message, state: FSMContext):
    await state.set_state(UserFlow.feedback_taklif)
    await message.answer("💡 Taklifingizni yozing:", reply_markup=back_kb())


@dp.message(F.text == BTN_SHIKOYAT)
async def btn_shikoyat(message: Message, state: FSMContext):
    await state.set_state(UserFlow.feedback_shikoyat)
    await message.answer("⚠️ Shikoyatingizni yozing:", reply_markup=back_kb())


@dp.message(F.text == BTN_BACK, StateFilter(UserFlow.feedback_taklif, UserFlow.feedback_shikoyat))
async def feedback_back(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("💬 Taklif yoki shikoyatingizni tanlang:", reply_markup=feedback_menu_kb())


@dp.message(StateFilter(UserFlow.feedback_taklif))
async def feedback_taklif_input(message: Message, state: FSMContext):
    fid = await db_add_feedback(message.from_user.id, "taklif", message.text)
    await state.clear()
    await message.answer("✅ Taklifingiz uchun rahmat! Adminga yuborildi.", reply_markup=main_menu_kb())
    user = await db_get_user(message.from_user.id)
    await notify_admins(f"💡 Yangi <b>taklif</b> (#{fid})\n👤 {user_display_name(user)}\n\n📝 {message.text}")


@dp.message(StateFilter(UserFlow.feedback_shikoyat))
async def feedback_shikoyat_input(message: Message, state: FSMContext):
    fid = await db_add_feedback(message.from_user.id, "shikoyat", message.text)
    await state.clear()
    await message.answer("✅ Shikoyatingiz qabul qilindi! Adminga yuborildi.", reply_markup=main_menu_kb())
    user = await db_get_user(message.from_user.id)
    await notify_admins(f"⚠️ Yangi <b>shikoyat</b> (#{fid})\n👤 {user_display_name(user)}\n\n📝 {message.text}")


@dp.message(F.text == BTN_SOCIAL)
async def btn_social(message: Message, state: FSMContext):
    await state.clear()
    text = await db_get_setting("social_links")
    await message.answer(f"🌍 <b>Ijtimoiy tarmoqlarimiz</b>\n\n{text}", reply_markup=main_menu_kb(), parse_mode="HTML")


@dp.message(F.text == BTN_ADMIN_CHAT)
async def btn_admin_chat(message: Message, state: FSMContext):
    await state.set_state(UserFlow.admin_chat)
    await message.answer(
        "👨‍💼 Admin bilan muloqot rejimi.\nXabaringizni yozing, u to'g'ridan-to'g'ri adminga yuboriladi.\n"
        "Chiqish uchun \"🔙 Orqaga\" tugmasini bosing.",
        reply_markup=back_kb(),
    )


@dp.message(F.text == BTN_BACK, StateFilter(UserFlow.admin_chat))
async def admin_chat_back(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🏠 Bosh menyu:", reply_markup=main_menu_kb())


@dp.message(StateFilter(UserFlow.admin_chat))
async def admin_chat_input(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Iltimos, matn ko'rinishida yozing.")
        return
    await db_add_message(message.from_user.id, "in", message.text)
    user = await db_get_user(message.from_user.id)
    await notify_admins(
        f"✉️ {user_display_name(user)} dan yangi xabar:\n\n{message.text}\n\n"
        f"Javob berish uchun: Admin panel → {ABTN_CHATS}"
    )
    await message.answer("✅ Xabaringiz adminga yuborildi. Javobni shu yerda kuting.", reply_markup=back_kb())


@dp.message(F.text == BTN_MY_ORDERS)
async def btn_my_orders(message: Message, state: FSMContext):
    await state.clear()
    orders = await db_get_orders_by_user(message.from_user.id)
    if not orders:
        await message.answer("📦 Sizda hali buyurtmalar yo'q.", reply_markup=main_menu_kb())
        return
    lines = ["📦 <b>Sizning buyurtmalaringiz:</b>\n"]
    for o in orders:
        title = ORDER_TYPE_TITLES.get(o["order_type"], o["order_type"])
        status_text = {"pending": "🕓 Ko'rib chiqilmoqda", "accepted": "✅ Qabul qilindi",
                       "rejected": "❌ Rad etildi"}.get(o["status"], o["status"])
        line = f"{title} — #{o['id']} — {status_text} ({o['created_at']})"
        if o["status"] == "rejected" and o["reject_reason"]:
            line += f"\n   Sabab: {o['reject_reason']}"
        lines.append(line)
    await message.answer("\n".join(lines), reply_markup=main_menu_kb(), parse_mode="HTML")


@dp.message(F.text == BTN_FAQ)
async def btn_faq(message: Message, state: FSMContext):
    await state.clear()
    text = await db_get_setting("faq")
    await message.answer(f"❓ <b>Ko'p so'raladigan savollar</b>\n\n{text}", reply_markup=main_menu_kb(), parse_mode="HTML")


@dp.message(F.text == BTN_RATE)
async def btn_rate(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("⭐️ Botimizni qanday baholaysiz?", reply_markup=rate_kb())


@dp.message(F.text.in_(RATE_BUTTONS))
async def rate_input(message: Message, state: FSMContext):
    score = RATE_BUTTONS.index(message.text) + 1
    await db_add_rating(message.from_user.id, score)
    await message.answer("🙏 Baholaganingiz uchun rahmat!", reply_markup=main_menu_kb())


@dp.message(F.text == BTN_CANCEL)
async def universal_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🚫 Bekor qilindi.", reply_markup=main_menu_kb())


@dp.message(F.text == BTN_BACK)
async def universal_back_to_main(message: Message, state: FSMContext):
    if is_admin(message.from_user.id):
        data = await state.get_data()
        section = data.get("admin_section", "main")
        parent = ADMIN_PARENT.get(section, "main")
        await render_admin_section(message, state, parent)
        return
    await state.clear()
    await message.answer("🏠 Bosh menyu:", reply_markup=main_menu_kb())


# ---- To'lov cheki qabul qilish (buyurtma qabul qilingandan keyin) ----

@dp.message(UserFlow.awaiting_payment, F.photo | F.document)
async def receive_payment_receipt(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("payment_order_id")
    if message.photo:
        file_id = message.photo[-1].file_id
        file_type = "photo"
    else:
        file_id = message.document.file_id
        file_type = "document"
    await db_add_payment(order_id, message.from_user.id, file_id, file_type)
    await state.clear()
    await message.answer(
        "✅ Chek qabul qilindi! Admin tekshirib, tasdiqlagach sizga xabar beriladi.",
        reply_markup=main_menu_kb(),
    )
    user = await db_get_user(message.from_user.id)
    await notify_admins(
        f"💳 {user_display_name(user)} to'lov chekini yubordi (buyurtma #{order_id}).\n"
        f"Ko'rish uchun: Admin panel → {ABTN_PAYMENTS}"
    )


@dp.message(UserFlow.awaiting_payment)
async def receive_payment_wrong_type(message: Message):
    await message.answer("📎 Iltimos, chekni rasm yoki PDF fayl ko'rinishida yuboring.")


# =============================================================================
# 8. ADMIN PANELI
# =============================================================================

import re


def extract_tgid(text: str):
    m = re.search(r"ID:(\d+)", text or "")
    return int(m.group(1)) if m else None


def extract_hash_id(text: str):
    m = re.search(r"#(\d+)", text or "")
    return int(m.group(1)) if m else None


async def enter_admin_mode(message: Message, state: FSMContext):
    await state.clear()
    await state.update_data(admin_mode=True, admin_section="main")
    total = await db_count_users_total()
    today = await db_count_users_today()
    await message.answer(
        f"🛠 <b>Admin panelga xush kelibsiz!</b>\n\n"
        f"👥 Jami foydalanuvchilar: {total}\n"
        f"🆕 Bugun qo'shilganlar: {today}\n\n"
        f"Kerakli bo'limni tanlang 👇",
        reply_markup=admin_menu_kb(),
        parse_mode="HTML",
    )


@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔️ Sizda admin panelga kirish huquqi yo'q.")
        return
    # Admin ID'lar doimiy ravishda admin panelida bo'ladi — parol shart emas.
    await enter_admin_mode(message, state)


def guard_admin(func):
    async def wrapper(message: Message, state: FSMContext):
        if not is_admin(message.from_user.id):
            return
        return await func(message, state)
    return wrapper


# ---------- Admin: BTN_BACK navigatsiyasi ----------

ADMIN_PARENT = {
    "users_list": "main", "user_card": "users_list",
    "chats_list": "main", "chat_view": "chats_list",
    "feedback_list": "main", "feedback_view": "feedback_list",
    "orders_type": "main", "orders_list": "orders_type", "order_view": "orders_list",
    "payments_list": "main", "payment_view": "payments_list",
    "edit_menu": "main", "settings_menu": "main",
}


@dp.message(F.text == BTN_BACK, StateFilter(
    AdminFlow.reject_reason, AdminFlow.broadcast_text, AdminFlow.new_setting_value,
    AdminFlow.reply_to_user, AdminFlow.reply_to_feedback,
))
async def admin_input_cancel(message: Message, state: FSMContext):
    data = await state.get_data()
    section = data.get("admin_section", "main")
    await state.set_state(None)
    await render_admin_section(message, state, section)


async def render_admin_section(message: Message, state: FSMContext, section: str, **ctx):
    if section == "main":
        await state.update_data(admin_section="main")
        await message.answer("🛠 Admin bosh menyu:", reply_markup=admin_menu_kb())
    elif section == "users_list":
        await show_users_list(message, state, ctx.get("page", 0))
    elif section == "chats_list":
        await show_chats_list(message, state, ctx.get("page", 0))
    elif section == "feedback_list":
        await show_feedback_list(message, state, ctx.get("page", 0))
    elif section == "orders_type":
        await state.update_data(admin_section="orders_type")
        await message.answer("🛒 Qaysi bo'lim zakazlarini ko'rmoqchisiz?", reply_markup=order_type_menu_kb())
    elif section == "orders_list":
        await show_orders_list(message, state, ctx.get("order_type"), ctx.get("page", 0))
    elif section == "payments_list":
        await show_payments_list(message, state, ctx.get("page", 0))
    elif section == "edit_menu":
        await state.update_data(admin_section="edit_menu")
        await message.answer("✏️ Qaysi ma'lumotni o'zgartirmoqchisiz?", reply_markup=edit_data_menu_kb())
    elif section == "settings_menu":
        await show_settings_menu(message, state)
    else:
        await state.update_data(admin_section="main")
        await message.answer("🛠 Admin bosh menyu:", reply_markup=admin_menu_kb())


# ---------- 1) Foydalanuvchilar ----------

@dp.message(F.text == ABTN_USERS)
@guard_admin
async def admin_users_btn(message: Message, state: FSMContext):
    await show_users_list(message, state, 0)


async def show_users_list(message: Message, state: FSMContext, page: int):
    users = await db_get_all_users()
    labels = [f"👤 {u['full_name'] or 'Nomalum'} — ID:{u['tg_id']}" for u in users]
    await state.update_data(admin_section="users_list", list_page=page)
    if not labels:
        await message.answer("👥 Hozircha foydalanuvchilar yo'q.", reply_markup=kb([[BTN_BACK]]))
        return
    await message.answer(
        f"👥 <b>Foydalanuvchilar</b> (jami: {len(labels)})\nKerakli foydalanuvchini tanlang:",
        reply_markup=paginated_list_kb(labels, page),
        parse_mode="HTML",
    )


@dp.message(F.text == ABTN_NEXT_PAGE)
@guard_admin
async def admin_next_page(message: Message, state: FSMContext):
    data = await state.get_data()
    page = data.get("list_page", 0) + 1
    await paginate_current(message, state, page)


@dp.message(F.text == ABTN_PREV_PAGE)
@guard_admin
async def admin_prev_page(message: Message, state: FSMContext):
    data = await state.get_data()
    page = max(0, data.get("list_page", 0) - 1)
    await paginate_current(message, state, page)


async def paginate_current(message: Message, state: FSMContext, page: int):
    data = await state.get_data()
    section = data.get("admin_section")
    if section == "users_list":
        await show_users_list(message, state, page)
    elif section == "chats_list":
        await show_chats_list(message, state, page)
    elif section == "feedback_list":
        await show_feedback_list(message, state, page)
    elif section == "orders_list":
        await show_orders_list(message, state, data.get("order_type"), page)
    elif section == "payments_list":
        await show_payments_list(message, state, page)


async def show_user_card(message: Message, state: FSMContext, tg_id: int, from_chat: bool = False):
    user = await db_get_user(tg_id)
    if not user:
        await message.answer("Foydalanuvchi topilmadi.")
        return
    await state.update_data(admin_section="user_card", selected_tg_id=tg_id, came_from_chat=False)
    orders = await db_get_orders_by_user(tg_id)
    status = "⛔️ Bloklangan" if user["is_blocked"] else "✅ Faol"
    text = (
        f"👤 <b>{user['full_name'] or 'Nomalum'}</b>\n"
        f"🆔 ID: {user['tg_id']}\n"
        f"🔗 Username: @{user['username'] or '—'}\n"
        f"📌 Holat: {status}\n"
        f"📦 Buyurtmalar soni: {len(orders)}\n"
        f"📅 Ro'yxatdan o'tgan: {user['created_at']}"
    )
    await message.answer(text, reply_markup=user_card_kb(bool(user["is_blocked"])), parse_mode="HTML")


@dp.message(F.text == ABTN_WRITE_USER)
@guard_admin
async def admin_write_user(message: Message, state: FSMContext):
    data = await state.get_data()
    tg_id = data.get("selected_tg_id")
    if not tg_id:
        return
    await state.set_state(AdminFlow.reply_to_user)
    await message.answer(f"✍️ ID:{tg_id} foydalanuvchiga yubormoqchi bo'lgan xabaringizni yozing:", reply_markup=back_kb())


@dp.message(StateFilter(AdminFlow.reply_to_user))
async def admin_write_user_input(message: Message, state: FSMContext):
    data = await state.get_data()
    tg_id = data.get("selected_tg_id")
    came_from_chat = data.get("came_from_chat", False)
    ok = await safe_send(tg_id, f"👨‍💼 <b>Admin:</b>\n{message.text}", parse_mode="HTML")
    await db_add_message(tg_id, "out", message.text)
    await state.set_state(None)
    if ok:
        await message.answer("✅ Xabar yuborildi.")
    else:
        await message.answer("⚠️ Xabar yuborilmadi (foydalanuvchi botni bloklagan bo'lishi mumkin).")
    if came_from_chat:
        await show_chat_view(message, state, tg_id)
    else:
        await show_user_card(message, state, tg_id)


@dp.message(F.text.in_([ABTN_BLOCK_USER, ABTN_UNBLOCK_USER]))
@guard_admin
async def admin_toggle_block(message: Message, state: FSMContext):
    data = await state.get_data()
    tg_id = data.get("selected_tg_id")
    if not tg_id:
        return
    new_status = await db_toggle_block(tg_id)
    if new_status:
        await message.answer("⛔️ Foydalanuvchi bloklandi.")
        await safe_send(tg_id, "⛔️ Siz botdan foydalanish huquqidan mahrum qilindingiz.")
    else:
        await message.answer("✅ Foydalanuvchi blokdan chiqarildi.")
        await safe_send(tg_id, "✅ Sizga botdan foydalanish huquqi qaytarildi.")
    await show_user_card(message, state, tg_id)


@dp.message(F.text == ABTN_USER_ORDERS)
@guard_admin
async def admin_user_orders(message: Message, state: FSMContext):
    data = await state.get_data()
    tg_id = data.get("selected_tg_id")
    orders = await db_get_orders_by_user(tg_id)
    if not orders:
        await message.answer("📦 Bu foydalanuvchining buyurtmalari yo'q.")
        return
    lines = [format_order_short(o) for o in orders]
    await message.answer("📦 Buyurtmalar:\n\n" + "\n".join(lines))


@dp.message(F.text == ABTN_PRIVATE_LINK)
@guard_admin
async def admin_private_link(message: Message, state: FSMContext):
    data = await state.get_data()
    tg_id = data.get("selected_tg_id")
    user = await db_get_user(tg_id)
    if user and user["username"]:
        await message.answer(f"💬 Shaxsiy chat: https://t.me/{user['username']}")
    else:
        await message.answer(
            f"ℹ️ Bu foydalanuvchida username yo'q, shaxsiy chatga to'g'ridan-to'g'ri o'tib bo'lmaydi.\n"
            f"Buning o'rniga \"{ABTN_WRITE_USER}\" tugmasi orqali bot ichidan yozing."
        )


# ---------- 2) Statistika ----------

@dp.message(F.text == ABTN_STATS)
@guard_admin
async def admin_stats(message: Message, state: FSMContext):
    total_users = await db_count_users_total()
    today_users = await db_count_users_today()
    blocked = await db_count_users_blocked()
    today_orders = await db_orders_stats_today()
    total_orders = await db_orders_count_total()
    avg_rating, rating_count = await db_avg_rating()
    pending = today_orders.get("pending", 0)
    accepted = today_orders.get("accepted", 0)
    rejected = today_orders.get("rejected", 0)
    text = (
        "📊 <b>Statistika</b>\n\n"
        f"👥 Jami foydalanuvchilar: {total_users}\n"
        f"🆕 Bugun qo'shilgan: {today_users}\n"
        f"⛔️ Bloklangan: {blocked}\n\n"
        f"🛒 Jami buyurtmalar: {total_orders}\n"
        f"📅 Bugungi buyurtmalar:\n"
        f"   🕓 Ko'rib chiqilmoqda: {pending}\n"
        f"   ✅ Qabul qilingan: {accepted}\n"
        f"   ❌ Rad etilgan: {rejected}\n\n"
        f"⭐️ O'rtacha baho: {avg_rating} ({rating_count} ta baho)"
    )
    await message.answer(text, reply_markup=admin_menu_kb(), parse_mode="HTML")
    await state.update_data(admin_section="main")


# ---------- 3) Chatlar ----------

@dp.message(F.text == ABTN_CHATS)
@guard_admin
async def admin_chats_btn(message: Message, state: FSMContext):
    await show_chats_list(message, state, 0)


async def show_chats_list(message: Message, state: FSMContext, page: int):
    chats = await db_get_chat_users()
    labels = []
    for c in chats:
        u = await db_get_user(c["tg_id"])
        name = u["full_name"] if u else "Noma'lum"
        labels.append(f"💬 {name} — ID:{c['tg_id']}")
    await state.update_data(admin_section="chats_list", list_page=page)
    if not labels:
        await message.answer("💬 Hozircha hech kim yozmagan.", reply_markup=kb([[BTN_BACK]]))
        return
    await message.answer(
        "💬 <b>Chatlar</b>\nKim bilan suhbatni ko'rmoqchisiz?",
        reply_markup=paginated_list_kb(labels, page),
        parse_mode="HTML",
    )


async def show_chat_view(message: Message, state: FSMContext, tg_id: int):
    await state.update_data(admin_section="chat_view", selected_tg_id=tg_id, came_from_chat=True)
    msgs = await db_get_messages(tg_id, limit=15)
    user = await db_get_user(tg_id)
    if not msgs:
        text = "Xabarlar tarixi bo'sh."
    else:
        lines = []
        for m in msgs:
            who = "👤 Foydalanuvchi" if m["direction"] == "in" else "👨‍💼 Admin"
            lines.append(f"{who} ({m['created_at']}): {m['text']}")
        text = "\n".join(lines)
    await message.answer(
        f"💬 <b>{user['full_name'] if user else tg_id}</b> bilan suhbat:\n\n{text}",
        reply_markup=kb([[ABTN_WRITE_USER], [BTN_BACK]]),
        parse_mode="HTML",
    )





# ---------- 4) Taklif va shikoyatlar ----------

@dp.message(F.text == ABTN_FEEDBACK)
@guard_admin
async def admin_feedback_btn(message: Message, state: FSMContext):
    await show_feedback_list(message, state, 0)


async def show_feedback_list(message: Message, state: FSMContext, page: int):
    items = await db_get_feedback_list()
    labels = []
    for f in items:
        icon = "💡" if f["kind"] == "taklif" else "⚠️"
        preview = (f["text"][:20] + "…") if len(f["text"]) > 20 else f["text"]
        replied = " ✅" if f["admin_reply"] else ""
        labels.append(f"{icon} #{f['id']} — {preview}{replied}")
    await state.update_data(admin_section="feedback_list", list_page=page)
    if not labels:
        await message.answer("📝 Hozircha taklif/shikoyatlar yo'q.", reply_markup=kb([[BTN_BACK]]))
        return
    await message.answer(
        "📝 <b>Taklif va shikoyatlar</b>\nKo'rmoqchi bo'lganingizni tanlang:",
        reply_markup=paginated_list_kb(labels, page),
        parse_mode="HTML",
    )


async def show_feedback_view(message: Message, state: FSMContext, feedback_id: int):
    f = await db_get_feedback(feedback_id)
    if not f:
        await message.answer("Topilmadi.")
        return
    await state.update_data(admin_section="feedback_view", selected_feedback_id=feedback_id)
    user = await db_get_user(f["tg_id"])
    kind_text = "💡 Taklif" if f["kind"] == "taklif" else "⚠️ Shikoyat"
    text = (
        f"{kind_text} #{f['id']}\n"
        f"👤 {user_display_name(user) if user else f['tg_id']}\n"
        f"📅 {f['created_at']}\n\n"
        f"📝 {f['text']}"
    )
    if f["admin_reply"]:
        text += f"\n\n✅ <b>Javob berilgan:</b> {f['admin_reply']}"
    await message.answer(text, reply_markup=kb([["✍️ Javob yozish"], [BTN_BACK]]), parse_mode="HTML")


@dp.message(F.text == "✍️ Javob yozish")
@guard_admin
async def admin_feedback_reply_start(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("admin_section") != "feedback_view":
        return
    await state.set_state(AdminFlow.reply_to_feedback)
    await message.answer("✍️ Javobingizni yozing:", reply_markup=back_kb())


@dp.message(StateFilter(AdminFlow.reply_to_feedback))
async def admin_feedback_reply_input(message: Message, state: FSMContext):
    data = await state.get_data()
    fid = data.get("selected_feedback_id")
    f = await db_get_feedback(fid)
    await db_set_feedback_reply(fid, message.text)
    await state.set_state(None)
    ok = await safe_send(
        f["tg_id"],
        f"👨‍💼 <b>Admin javobi</b> (sizning #{fid} murojaatingizga):\n\n{message.text}",
        parse_mode="HTML",
    )
    await message.answer("✅ Javob yuborildi." if ok else "⚠️ Javob yuborilmadi (foydalanuvchi bloklagan bo'lishi mumkin).")
    await show_feedback_view(message, state, fid)


# ---------- 5) Zakazlar ----------

@dp.message(F.text == ABTN_ORDERS)
@guard_admin
async def admin_orders_btn(message: Message, state: FSMContext):
    await state.update_data(admin_section="orders_type")
    await message.answer("🛒 Qaysi bo'lim zakazlarini ko'rmoqchisiz?", reply_markup=order_type_menu_kb())


@dp.message(F.text == ABTN_ORDER_WEBSITE)
@guard_admin
async def admin_orders_website(message: Message, state: FSMContext):
    await show_orders_list(message, state, "website", 0)


@dp.message(F.text == ABTN_ORDER_LOGO)
@guard_admin
async def admin_orders_logo(message: Message, state: FSMContext):
    await show_orders_list(message, state, "logo", 0)


async def show_orders_list(message: Message, state: FSMContext, order_type: str, page: int):
    orders = await db_get_orders_by_type(order_type)
    labels = [format_order_short(o) for o in orders]
    await state.update_data(admin_section="orders_list", order_type=order_type, list_page=page)
    title = ORDER_TYPE_TITLES.get(order_type, order_type)
    if not labels:
        await message.answer(f"{title}: hozircha buyurtmalar yo'q.", reply_markup=kb([[BTN_BACK]]))
        return
    await message.answer(
        f"{title} <b>zakazlari</b> (jami: {len(labels)})\nKerakli buyurtmani tanlang:",
        reply_markup=paginated_list_kb(labels, page),
        parse_mode="HTML",
    )


async def show_order_view(message: Message, state: FSMContext, order_id: int):
    o = await db_get_order(order_id)
    if not o:
        await message.answer("Buyurtma topilmadi.")
        return
    await state.update_data(admin_section="order_view", selected_order_id=order_id)
    data = json.loads(o["data"])
    body = format_website_order(data) if o["order_type"] == "website" else format_logo_order(data)
    user = await db_get_user(o["tg_id"])
    status_text = {"pending": "🕓 Ko'rib chiqilmoqda", "accepted": "✅ Qabul qilingan",
                   "rejected": "❌ Rad etilgan"}.get(o["status"], o["status"])
    text = f"{body}\n\n👤 {user_display_name(user) if user else o['tg_id']}\n📌 Holat: {status_text}"
    if o["status"] == "rejected" and o["reject_reason"]:
        text += f"\n❌ Sabab: {o['reject_reason']}"
    markup = order_action_kb() if o["status"] == "pending" else kb([[BTN_BACK]])
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@dp.message(F.text == ABTN_ACCEPT)
@guard_admin
async def admin_order_accept(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("selected_order_id")
    if not order_id:
        return
    order = await db_get_order(order_id)
    await db_set_order_status(order_id, "accepted")
    card_num = await db_get_setting("card_number")
    card_owner = await db_get_setting("card_owner")
    admin_phone = await db_get_setting("admin_contact_phone")
    admin_name = await db_get_setting("admin_contact_name")
    await safe_send(
        order["tg_id"],
        f"✅ Xaridingiz tasdiqlandi! Endi to'lovni amalga oshirishingiz mumkin.\n\n"
        f"💳 Karta raqami: <code>{card_num}</code>\n👤 Karta egasi: {card_owner}\n\n"
        f"To'lov qilgach, chek rasmini yoki PDF faylini shu botga yuboring.",
        parse_mode="HTML",
    )
    if admin_phone:
        try:
            await bot.send_contact(order["tg_id"], phone_number=admin_phone, first_name=admin_name or "Admin")
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
    await get_user_fsm(order["tg_id"]).set_state(UserFlow.awaiting_payment)
    await get_user_fsm(order["tg_id"]).update_data(payment_order_id=order_id)
    await message.answer("✅ Buyurtma qabul qilindi deb belgilandi, foydalanuvchiga xabar yuborildi.")
    await show_orders_list(message, state, order["order_type"], data.get("list_page", 0))


@dp.message(F.text == ABTN_REJECT)
@guard_admin
async def admin_order_reject_start(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("selected_order_id"):
        return
    await state.set_state(AdminFlow.reject_reason)
    await message.answer("❌ Rad etish sababini yozing:", reply_markup=back_kb())


@dp.message(StateFilter(AdminFlow.reject_reason))
async def admin_order_reject_input(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("selected_order_id")
    order = await db_get_order(order_id)
    await db_set_order_status(order_id, "rejected", message.text)
    await state.set_state(None)
    await safe_send(
        order["tg_id"],
        f"❌ Xaridingiz admin tomonidan tasdiqlanmadi.\n\nSabab: {message.text}",
    )
    await message.answer("❌ Buyurtma rad etildi, foydalanuvchiga xabar yuborildi.")
    await show_orders_list(message, state, order["order_type"], data.get("list_page", 0))


# ---------- 6) To'lovlar ----------

@dp.message(F.text == ABTN_PAYMENTS)
@guard_admin
async def admin_payments_btn(message: Message, state: FSMContext):
    await show_payments_list(message, state, 0)


async def show_payments_list(message: Message, state: FSMContext, page: int):
    payments = await db_get_payments(status="pending")
    labels = []
    for p in payments:
        user = await db_get_user(p["tg_id"])
        name = user["full_name"] if user else p["tg_id"]
        labels.append(f"💳 #{p['id']} — {name}")
    await state.update_data(admin_section="payments_list", list_page=page)
    if not labels:
        await message.answer("💳 Hozircha tasdiqlanmagan to'lovlar yo'q.", reply_markup=kb([[BTN_BACK]]))
        return
    await message.answer(
        "💳 <b>To'lovlar</b>\nCheki ko'rmoqchi bo'lgan to'lovni tanlang:",
        reply_markup=paginated_list_kb(labels, page),
        parse_mode="HTML",
    )


async def show_payment_view(message: Message, state: FSMContext, payment_id: int):
    p = await db_get_payment(payment_id)
    if not p:
        await message.answer("Topilmadi.")
        return
    await state.update_data(admin_section="payment_view", selected_payment_id=payment_id)
    user = await db_get_user(p["tg_id"])
    caption = (
        f"💳 To'lov #{p['id']}\n👤 {user_display_name(user) if user else p['tg_id']}\n"
        f"📅 {p['created_at']}\n📌 Holat: {'✅ Tasdiqlangan' if p['status']=='confirmed' else '🕓 Kutilmoqda'}"
    )
    markup = payment_action_kb() if p["status"] == "pending" else kb([[BTN_BACK]])
    try:
        if p["file_type"] == "photo":
            await bot.send_photo(message.chat.id, p["file_id"], caption=caption, reply_markup=markup)
        else:
            await bot.send_document(message.chat.id, p["file_id"], caption=caption, reply_markup=markup)
    except (TelegramBadRequest, TelegramForbiddenError):
        await message.answer(caption + "\n\n⚠️ Fayl yuborilmadi.", reply_markup=markup)


@dp.message(F.text == ABTN_CONFIRM_PAYMENT)
@guard_admin
async def admin_confirm_payment(message: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("selected_payment_id")
    if not pid:
        return
    p = await db_get_payment(pid)
    await db_confirm_payment(pid)
    await safe_send(p["tg_id"], "✅ To'lovingiz tasdiqlandi! Tez orada admin siz bilan bog'lanadi.")
    user = await db_get_user(p["tg_id"])
    link = f"https://t.me/{user['username']}" if user and user["username"] else f"ID:{p['tg_id']} (username yo'q, \"{ABTN_WRITE_USER}\" orqali yozing)"
    await message.answer(f"✅ To'lov tasdiqlandi. Foydalanuvchi bilan bog'lanish: {link}")
    await show_payments_list(message, state, data.get("list_page", 0))


# ---------- 7) Ma'lumotlarni almashtirish ----------

@dp.message(F.text == ABTN_EDIT_DATA)
@guard_admin
async def admin_edit_data_btn(message: Message, state: FSMContext):
    await state.update_data(admin_section="edit_menu")
    await message.answer("✏️ Qaysi ma'lumotni o'zgartirmoqchisiz?", reply_markup=edit_data_menu_kb())


@dp.message(F.text.in_(list(LABEL_TO_SETTING.keys())))
@guard_admin
async def admin_edit_choose_field(message: Message, state: FSMContext):
    key = LABEL_TO_SETTING[message.text]
    current = await db_get_setting(key)
    await state.update_data(editing_key=key)
    await state.set_state(AdminFlow.new_setting_value)
    await message.answer(
        f"✏️ <b>{message.text}</b>\n\n🔹 Hozirgi qiymat:\n{current}\n\n"
        f"👇 Yangi qiymatni yozing (eskisi o'rniga saqlanadi):",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )


@dp.message(StateFilter(AdminFlow.new_setting_value))
async def admin_edit_save(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("editing_key")
    await db_set_setting(key, message.text)
    await state.set_state(None)
    await message.answer("✅ Ma'lumot yangilandi!")
    await state.update_data(admin_section="edit_menu")
    await message.answer("✏️ Yana biror narsani o'zgartirmoqchimisiz?", reply_markup=edit_data_menu_kb())


# ---------- 8) Hammaga xabar yuborish (broadcast) ----------

@dp.message(F.text == ABTN_BROADCAST)
@guard_admin
async def admin_broadcast_btn(message: Message, state: FSMContext):
    await state.set_state(AdminFlow.broadcast_text)
    await message.answer(
        "📢 Barcha foydalanuvchilarga yubormoqchi bo'lgan xabaringizni yozing:", reply_markup=back_kb()
    )


@dp.message(StateFilter(AdminFlow.broadcast_text))
async def admin_broadcast_input(message: Message, state: FSMContext):
    users = await db_get_all_users()
    await state.set_state(None)
    await state.update_data(admin_section="main")
    sent, failed = 0, 0
    await message.answer(f"📢 Yuborish boshlandi... ({len(users)} foydalanuvchi)")
    for u in users:
        if u["is_blocked"]:
            continue
        ok = await safe_send(u["tg_id"], f"📢 <b>E'lon:</b>\n\n{message.text}", parse_mode="HTML")
        if ok:
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)
    await message.answer(f"✅ Xabar yuborildi: {sent} ta\n⚠️ Yuborilmadi: {failed} ta", reply_markup=admin_menu_kb())


# ---------- 9) Eksport ----------

@dp.message(F.text == ABTN_EXPORT)
@guard_admin
async def admin_export_btn(message: Message, state: FSMContext):
    users = await db_get_all_users()
    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ID", "Telegram ID", "Ism", "Username", "Bloklangan", "Ro'yxatdan o'tgan sana"])
    for u in users:
        writer.writerow([u["id"], u["tg_id"], u["full_name"], u["username"], u["is_blocked"], u["created_at"]])
    data_bytes = buf.getvalue().encode("utf-8-sig")
    file = BufferedInputFile(data_bytes, filename="foydalanuvchilar.csv")
    await message.answer_document(file, caption=f"📁 Jami {len(users)} ta foydalanuvchi.")


# ---------- 10) Bot sozlamalari ----------

@dp.message(F.text == ABTN_SETTINGS)
@guard_admin
async def admin_settings_btn(message: Message, state: FSMContext):
    await show_settings_menu(message, state)


async def show_settings_menu(message: Message, state: FSMContext):
    await state.update_data(admin_section="settings_menu")
    maintenance = await db_get_setting("maintenance_mode")
    status = "🔴 Yoqilgan (foydalanuvchilar botdan foydalana olmaydi)" if maintenance == "1" else "🟢 O'chirilgan (bot normal ishlayapti)"
    await message.answer(
        f"⚙️ <b>Bot sozlamalari</b>\n\n🔧 Texnik ishlar rejimi: {status}",
        reply_markup=settings_menu_kb(),
        parse_mode="HTML",
    )


@dp.message(F.text == ABTN_MAINTENANCE_TOGGLE)
@guard_admin
async def admin_toggle_maintenance(message: Message, state: FSMContext):
    current = await db_get_setting("maintenance_mode")
    new_val = "0" if current == "1" else "1"
    await db_set_setting("maintenance_mode", new_val)
    await message.answer("✅ Sozlama o'zgartirildi.")
    await show_settings_menu(message, state)


# =============================================================================
# 9. MIDDLEWARE: bloklangan foydalanuvchilar va texnik ishlar rejimi
# =============================================================================

@dp.message.outer_middleware()
async def maintenance_and_block_middleware(handler, event: Message, data):
    tg_id = event.from_user.id if event.from_user else None
    if tg_id is None or is_admin(tg_id):
        return await handler(event, data)

    if await db_is_blocked(tg_id):
        if event.text == "/start":
            await event.answer("⛔️ Siz botdan foydalanish huquqidan mahrum qilingansiz.")
        return

    maintenance = await db_get_setting("maintenance_mode")
    if maintenance == "1":
        await event.answer("🔧 Hozirda texnik ishlar olib borilmoqda. Iltimos, birozdan so'ng qayta urinib ko'ring.")
        return

    return await handler(event, data)


# =============================================================================
# 10. YAKUNIY FALLBACK (tanib bo'lmagan xabarlar / dinamik admin tugmalari)
# =============================================================================

@dp.message(F.text)
async def fallback_handler(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer(
            "🤔 Tushunmadim. Iltimos, menyudagi tugmalardan birini tanlang.",
            reply_markup=main_menu_kb(),
        )
        return

    data = await state.get_data()
    section = data.get("admin_section")
    text = message.text

    if section in ("users_list", "chats_list"):
        tgid = extract_tgid(text)
        if tgid:
            if section == "chats_list":
                await show_chat_view(message, state, tgid)
            else:
                await show_user_card(message, state, tgid)
            return
    elif section == "feedback_list":
        fid = extract_hash_id(text)
        if fid:
            await show_feedback_view(message, state, fid)
            return
    elif section == "orders_list":
        oid = extract_hash_id(text)
        if oid:
            await show_order_view(message, state, oid)
            return
    elif section == "payments_list":
        pid = extract_hash_id(text)
        if pid:
            await show_payment_view(message, state, pid)
            return

    await message.answer("🤔 Noma'lum buyruq. Iltimos, menyudan tanlang.", reply_markup=admin_menu_kb())
    await state.update_data(admin_section="main")


@dp.message()
async def fallback_non_text(message: Message, state: FSMContext):
    await message.answer("🤔 Iltimos, menyudagi tugmalardan foydalaning.")


# =============================================================================
# 11. RENDER UCHUN AIOHTTP HEALTH-CHECK SERVER + POLLING
# =============================================================================

async def health(request):
    return web.Response(text="MHDV bot ishlayapti ✅")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info(f"Health-check server {PORT}-portda ishga tushdi (Render uchun).")


async def main():
    await init_db()
    await start_web_server()
    logger.info("Bot polling boshlandi...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot to'xtatildi.")