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
import hmac
import hashlib
import asyncio
import logging
import datetime
import urllib.parse
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
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    WebAppInfo,
    MenuButtonWebApp,
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
WEB_PANEL_PASSWORD = os.getenv("WEB_PANEL_PASSWORD", "mhdv2026").strip()

# Mini-app (Telegram Web App) ochiladigan asosiy manzil, masalan:
# https://mening-botim.onrender.com  (oxirida "/" bo'lmasin)
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip().rstrip("/")

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
if not WEBAPP_URL:
    logger.warning(
        "WEBAPP_URL sozlanmagan! Mini-ilova tugmasi botda ko'rsatilmaydi. "
        "Render Environment Variables ichiga WEBAPP_URL=https://<domeningiz> kabi qo'shing."
    )

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

PAGE_SIZE = 10  # foydalanuvchilar/zakazlar ro'yxatida bir sahifadagi elementlar soni

DEFAULT_SETTINGS = {
    "website_info": "Veb-sayt xizmatlari haqida ma'lumot hali admin tomonidan kiritilmagan.",
    "logo_info": "Logo xizmatlari haqida ma'lumot hali admin tomonidan kiritilmagan.",
    "bot_service_info": "Bot yasash xizmati haqida ma'lumot hali admin tomonidan kiritilmagan.",
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
    "reminders_enabled": "1",
    "reminder_hours": "2",
    "game_enabled": "1",
    "game_started": "0",
    "game_next_number": "100",
    "game_info": "🎨 Bu — Logo o'yini! ID raqam oling va tasodifiy tanlovda qatnashing. "
                 "G'olib bo'lsangiz, shu bot orqali xabar beramiz va admin siz bilan bog'lanadi.",
    "abandoned_flow_minutes": "45",
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                tg_id INTEGER PRIMARY KEY,
                role TEXT DEFAULT 'admin',
                added_by INTEGER,
                created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS game_participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER UNIQUE,
                game_number INTEGER UNIQUE,
                is_winner INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS order_ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER,
                tg_id INTEGER,
                rating INTEGER,
                created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admin_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER,
                action TEXT,
                details TEXT,
                created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT,
                segment TEXT DEFAULT 'all',
                send_at TEXT,
                sent INTEGER DEFAULT 0,
                created_by INTEGER,
                created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS flow_progress (
                tg_id INTEGER PRIMARY KEY,
                flow_type TEXT,
                updated_at TEXT,
                reminded INTEGER DEFAULT 0
            )
        """)
        await db.commit()

        # ---- Eski bazalarni yangi ustunlar bilan moslashtirish (migratsiya) ----
        async def _add_column_if_missing(table, column, coltype):
            cur = await db.execute(f"PRAGMA table_info({table})")
            cols = [r[1] for r in await cur.fetchall()]
            if column not in cols:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")

        await _add_column_if_missing("orders", "admin_reminded", "INTEGER DEFAULT 0")
        await _add_column_if_missing("orders", "payment_reminder_sent", "INTEGER DEFAULT 0")
        await _add_column_if_missing("payments", "admin_reminded", "INTEGER DEFAULT 0")
        await _add_column_if_missing("users", "needs_restart", "INTEGER DEFAULT 0")
        await _add_column_if_missing("orders", "stage", "TEXT")
        await _add_column_if_missing("orders", "revision_note", "TEXT")
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


async def db_set_all_needs_restart():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET needs_restart=1 WHERE is_blocked=0")
        await db.commit()


async def db_clear_needs_restart(tg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET needs_restart=0 WHERE tg_id=?", (tg_id,))
        await db.commit()


async def db_needs_restart(tg_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT needs_restart FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        return bool(row[0]) if row else False


# ---------- ADMINLAR (ko'p darajali) ----------

async def db_add_admin(tg_id: int, added_by: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO admins (tg_id, role, added_by, created_at) VALUES (?, 'admin', ?, ?)",
            (tg_id, added_by, now()),
        )
        await db.commit()


async def db_remove_admin(tg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM admins WHERE tg_id=?", (tg_id,))
        await db.commit()


async def db_is_db_admin(tg_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM admins WHERE tg_id=?", (tg_id,))
        return (await cur.fetchone()) is not None


async def db_get_admins():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM admins ORDER BY created_at")
        return await cur.fetchall()


# ---------- LOGO O'YINI ----------

GAME_LOCK = asyncio.Lock()


async def db_game_get_participant(tg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM game_participants WHERE tg_id=?", (tg_id,))
        return await cur.fetchone()


async def db_game_get_by_number(number: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM game_participants WHERE game_number=?", (number,))
        return await cur.fetchone()


async def db_game_assign_number(tg_id: int) -> int:
    """Ketma-ket, takrorlanmas ID raqam beradi (100 dan boshlab). Poyga holatidan himoyalangan."""
    async with GAME_LOCK:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT value FROM settings WHERE key='game_next_number'")
            row = await cur.fetchone()
            next_num = int(row["value"]) if row and row["value"] and row["value"].isdigit() else 100
            await db.execute(
                "INSERT INTO game_participants (tg_id, game_number, created_at) VALUES (?, ?, ?)",
                (tg_id, next_num, now()),
            )
            await db.execute(
                "INSERT INTO settings (key, value) VALUES ('game_next_number', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(next_num + 1),),
            )
            await db.commit()
            return next_num


async def db_game_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM game_participants")
        return (await cur.fetchone())[0]


async def db_game_list_participants():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM game_participants ORDER BY game_number")
        return await cur.fetchall()


async def db_game_set_winner(number: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE game_participants SET is_winner=1 WHERE game_number=?", (number,))
        await db.commit()


async def db_game_reset():
    async with GAME_LOCK:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM game_participants")
            await db.execute(
                "INSERT INTO settings (key, value) VALUES ('game_next_number', '100') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
            await db.execute(
                "INSERT INTO settings (key, value) VALUES ('game_started', '0') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
            await db.commit()


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


async def db_set_order_stage(order_id, stage):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE orders SET stage=? WHERE id=?", (stage, order_id))
        await db.commit()


async def db_set_order_revision(order_id, note: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE orders SET revision_note=? WHERE id=?", (note, order_id))
        await db.commit()


async def db_get_order_rating(order_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM order_ratings WHERE order_id=? ORDER BY id DESC LIMIT 1", (order_id,)
        )
        return await cur.fetchone()


async def db_add_order_rating(order_id, tg_id, rating):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO order_ratings (order_id, tg_id, rating, created_at) VALUES (?, ?, ?, ?)",
            (order_id, tg_id, rating, now()),
        )
        await db.commit()


async def db_log_admin_action(admin_id: int, action: str, details: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO admin_logs (admin_id, action, details, created_at) VALUES (?, ?, ?, ?)",
            (admin_id, action, details, now()),
        )
        await db.commit()


async def db_get_admin_logs(limit=50):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM admin_logs ORDER BY id DESC LIMIT ?", (limit,))
        return await cur.fetchall()


async def db_add_scheduled_broadcast(text: str, segment: str, send_at: str, created_by: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO scheduled_broadcasts (text, segment, send_at, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (text, segment, send_at, created_by, now()),
        )
        await db.commit()
        return cur.lastrowid


async def db_get_due_broadcasts():
    threshold = now()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM scheduled_broadcasts WHERE sent=0 AND send_at<=?", (threshold,)
        )
        return await cur.fetchall()


async def db_mark_broadcast_sent(broadcast_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE scheduled_broadcasts SET sent=1 WHERE id=?", (broadcast_id,))
        await db.commit()


# ---------- Tashlab ketilgan buyurtma jarayonlari ----------

async def db_flow_touch(tg_id: int, flow_type: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO flow_progress (tg_id, flow_type, updated_at, reminded) VALUES (?, ?, ?, 0) "
            "ON CONFLICT(tg_id) DO UPDATE SET flow_type=excluded.flow_type, "
            "updated_at=excluded.updated_at, reminded=0",
            (tg_id, flow_type, now()),
        )
        await db.commit()


async def db_flow_clear(tg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM flow_progress WHERE tg_id=?", (tg_id,))
        await db.commit()


async def db_get_stale_flows(minutes: int):
    threshold = (datetime.datetime.now() - datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM flow_progress WHERE reminded=0 AND updated_at<=?", (threshold,)
        )
        return await cur.fetchall()


async def db_mark_flow_reminded(tg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE flow_progress SET reminded=1 WHERE tg_id=?", (tg_id,))
        await db.commit()


async def db_get_users_with_orders():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT DISTINCT u.* FROM users u
            INNER JOIN orders o ON o.tg_id = u.tg_id
        """)
        return await cur.fetchall()


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


async def db_search_orders(query: str):
    """Buyurtmani ID yoki foydalanuvchi tg_id bo'yicha qidirish."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        results = []
        if query.isdigit():
            cur = await db.execute("SELECT * FROM orders WHERE id=?", (int(query),))
            row = await cur.fetchone()
            if row:
                results.append(row)
            cur = await db.execute(
                "SELECT * FROM orders WHERE tg_id=? ORDER BY id DESC", (int(query),)
            )
            for r in await cur.fetchall():
                if r["id"] not in [x["id"] for x in results]:
                    results.append(r)
        return results


async def db_get_stale_pending_orders(hours: int):
    threshold = (datetime.datetime.now() - datetime.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM orders WHERE status='pending' AND admin_reminded=0 AND created_at<=?",
            (threshold,),
        )
        return await cur.fetchall()


async def db_mark_order_admin_reminded(order_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE orders SET admin_reminded=1 WHERE id=?", (order_id,))
        await db.commit()


async def db_get_stale_unpaid_orders(hours: int):
    """Qabul qilingan, ammo hali chek yuborilmagan buyurtmalar."""
    threshold = (datetime.datetime.now() - datetime.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT * FROM orders
            WHERE status='accepted' AND payment_reminder_sent=0 AND created_at<=?
            AND id NOT IN (SELECT order_id FROM payments WHERE order_id IS NOT NULL)
            """,
            (threshold,),
        )
        return await cur.fetchall()


async def db_mark_order_payment_reminded(order_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE orders SET payment_reminder_sent=1 WHERE id=?", (order_id,))
        await db.commit()


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


async def db_get_payment_by_order(order_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM payments WHERE order_id=? ORDER BY id DESC LIMIT 1", (order_id,)
        )
        return await cur.fetchone()


async def db_get_stale_pending_payments(hours: int):
    threshold = (datetime.datetime.now() - datetime.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM payments WHERE status='pending' AND admin_reminded=0 AND created_at<=?",
            (threshold,),
        )
        return await cur.fetchall()


async def db_mark_payment_admin_reminded(payment_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE payments SET admin_reminded=1 WHERE id=?", (payment_id,))
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

    bot_name = State()
    bot_purpose = State()
    bot_platform = State()
    bot_budget = State()
    bot_requirements = State()
    bot_additional = State()

    ai_topic = State()
    ai_style = State()
    ai_size = State()
    ai_budget = State()
    ai_additional = State()

    feedback_taklif = State()
    feedback_shikoyat = State()

    admin_chat = State()

    awaiting_payment = State()

    revision_note = State()


class AdminFlow(StatesGroup):
    reject_reason = State()
    broadcast_text = State()
    broadcast_segment = State()
    broadcast_timing = State()
    broadcast_schedule_time = State()
    new_setting_value = State()
    reply_to_user = State()
    reply_to_feedback = State()
    add_admin_id = State()
    remove_admin_id = State()
    order_search = State()
    deliver_order = State()


# =============================================================================
# 4. TUGMA MATNLARI (Reply Keyboard)
# =============================================================================

BTN_WEBSITE = "🌐 Veb-sayt buyurtma qilish"
BTN_LOGO = "🎨 Logo buyurtma qilish"
BTN_BOT = "🤖 Bot buyurtma qilish"
BTN_AI_IMAGE = "🖼 AI rasm buyurtma qilish"
BTN_INFO = "ℹ️ Ma'lumot"
BTN_FEEDBACK = "💬 Taklif va shikoyat"
BTN_SOCIAL = "🌍 Ijtimoiy tarmoqlar"
BTN_ADMIN_CHAT = "👨‍💼 Admin bilan muloqot"
BTN_MY_ORDERS = "📦 Mening buyurtmalarim"
BTN_MY_PAYMENTS = "💳 To'lovlarim"
BTN_FAQ = "❓ Ko'p so'raladigan savollar"
BTN_RATE = "⭐️ Botni baholash"
BTN_GAME = "🎮 Logo o'yini"
BTN_MINI_APP = "🚀 Mini-ilova (barcha xizmatlar)"

BTN_BACK = "🔙 Orqaga"
BTN_SKIP = "❌ Yo'q"
BTN_CANCEL = "🚫 Bekor qilish"

BTN_ABOUT_BOT = "🤖 Bot haqida"
BTN_ABOUT_MHDV = "🏢 MHDV haqida"

BTN_TAKLIF = "💡 Taklif"
BTN_SHIKOYAT = "⚠️ Shikoyat"

GBTN_GET_ID = "🆔 ID raqam olish"
GBTN_INFO = "ℹ️ O'yin haqida ma'lumot"

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
ABTN_ADMINS = "👑 Adminlar"
ABTN_GAME = "🎮 O'yin (Logo)"

ABTN_ORDER_WEBSITE = "🌐 Veb-sayt zakazlari"
ABTN_ORDER_LOGO = "🎨 Logo zakazlari"
ABTN_ORDER_BOT = "🤖 Bot zakazlari"
ABTN_ORDER_AI_IMAGE = "🖼 AI rasm zakazlari"
ABTN_ORDER_SEARCH = "🔍 Buyurtmani ID bo'yicha qidirish"

ABTN_FILTER_PENDING = "🕓 Kutilmoqda"
ABTN_FILTER_ACCEPTED = "✅ Qabul qilingan"
ABTN_FILTER_REJECTED = "❌ Rad etilgan"
ABTN_FILTER_ALL = "📋 Barchasi"

ABTN_ACCEPT = "✅ Qabul qilish"
ABTN_REJECT = "❌ Rad etish"

ABTN_MARK_READY = "🚀 Tayyor deb belgilash"
ABTN_MARK_DELIVERED = "📬 Topshirildi deb belgilash"

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
ABTN_EDIT_BOT_SERVICE = "🤖 Bot xizmati ma'lumoti"
ABTN_EDIT_BOT = "🤖 Bot haqida ma'lumot"
ABTN_EDIT_MHDV = "🏢 MHDV haqida ma'lumot"
ABTN_EDIT_CARD_NUM = "💳 Karta raqami"
ABTN_EDIT_CARD_OWNER = "👤 Karta egasi"
ABTN_EDIT_SOCIAL = "🌍 Ijtimoiy tarmoqlar"
ABTN_EDIT_FAQ = "❓ FAQ ma'lumoti"
ABTN_EDIT_ADMIN_PHONE = "📞 Admin telefon raqami"

ABTN_MAINTENANCE_TOGGLE = "🔧 Texnik ishlar rejimini almashtirish"
ABTN_REMINDER_TOGGLE = "🔔 Eslatmalarni yoqish/o'chirish"
ABTN_EDIT_REMINDER_HOURS = "⏰ Eslatma vaqti (soat)"
ABTN_FORCE_RESTART = "🔁 Hammadan qayta /start so'rash"
ABTN_ADMIN_LOGS = "📜 Adminlar tarixi"

ABTN_SEG_ALL = "👥 Hammaga"
ABTN_SEG_ORDERED = "📦 Faqat buyurtma berganlarga"
ABTN_SEND_NOW = "🚀 Hozir yuborish"
ABTN_SEND_LATER = "⏰ Kechiktirish (daqiqada)"

FORCE_START_CALLBACK = "force_start"

ABTN_ADD_ADMIN = "➕ Admin qo'shish"
ABTN_REMOVE_ADMIN = "➖ Adminni olib tashlash"

ABTN_GAME_STATS = "📊 Nechta ID olingan"
ABTN_GAME_USERS = "👥 Foydalanuvchilar"
ABTN_GAME_START = "▶️ O'yinni boshlash"
ABTN_GAME_TOGGLE = "🔌 O'yinni yoqish/o'chirish"
ABTN_GAME_RESET = "🔄 Qayta boshlash (Restart)"
ABTN_GAME_PICK = "🎯 G'olibni tanlash"
ABTN_GAME_END = "⏹ O'yinni tugatish"
ABTN_EDIT_GAME_INFO = "🎮 O'yin ma'lumoti"

SETTING_LABELS = {
    "website_info": ABTN_EDIT_WEBSITE,
    "logo_info": ABTN_EDIT_LOGO,
    "bot_service_info": ABTN_EDIT_BOT_SERVICE,
    "bot_info": ABTN_EDIT_BOT,
    "mhdv_info": ABTN_EDIT_MHDV,
    "card_number": ABTN_EDIT_CARD_NUM,
    "card_owner": ABTN_EDIT_CARD_OWNER,
    "social_links": ABTN_EDIT_SOCIAL,
    "faq": ABTN_EDIT_FAQ,
    "admin_contact_phone": ABTN_EDIT_ADMIN_PHONE,
    "game_info": ABTN_EDIT_GAME_INFO,
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
    rows = []
    if WEBAPP_URL:
        rows.append([KeyboardButton(
            text=BTN_MINI_APP,
            web_app=WebAppInfo(url=f"{WEBAPP_URL}/miniapp"),
        )])
    rows += [
        [BTN_WEBSITE],
        [BTN_LOGO],
        [BTN_BOT],
        [BTN_AI_IMAGE],
        [BTN_GAME],
        [BTN_INFO, BTN_FEEDBACK],
        [BTN_SOCIAL, BTN_ADMIN_CHAT],
        [BTN_MY_ORDERS, BTN_MY_PAYMENTS],
        [BTN_FAQ],
        [BTN_RATE],
    ]
    keyboard = [
        row if isinstance(row[0], KeyboardButton) else [KeyboardButton(text=t) for t in row]
        for row in rows
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


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


def admin_menu_kb(role: str = "admin"):
    # Admin panelida har bir tugma o'z alohida qatorida turadi.
    rows = [
        [ABTN_USERS],
        [ABTN_STATS],
        [ABTN_CHATS],
        [ABTN_FEEDBACK],
        [ABTN_ORDERS],
        [ABTN_PAYMENTS],
        [ABTN_GAME],
    ]
    if role == "super":
        rows.append([ABTN_EDIT_DATA])
        rows.append([ABTN_BROADCAST])
        rows.append([ABTN_EXPORT])
        rows.append([ABTN_SETTINGS])
        rows.append([ABTN_ADMINS])
        rows.append([ABTN_ADMIN_LOGS])
        rows.append([ABTN_FORCE_RESTART])
    return kb(rows)


def game_menu_user_kb():
    return kb([[GBTN_GET_ID], [GBTN_INFO], [BTN_BACK]])


def game_menu_admin_kb():
    return kb([
        [ABTN_GAME_STATS],
        [ABTN_GAME_USERS],
        [ABTN_GAME_START],
        [ABTN_GAME_TOGGLE],
        [ABTN_GAME_RESET],
        [BTN_BACK],
    ])


def game_active_kb():
    return kb([
        [ABTN_GAME_PICK],
        [ABTN_GAME_END],
        [BTN_BACK],
    ])


def order_type_menu_kb():
    return kb([
        [ABTN_ORDER_WEBSITE],
        [ABTN_ORDER_LOGO],
        [ABTN_ORDER_BOT],
        [ABTN_ORDER_AI_IMAGE],
        [ABTN_ORDER_SEARCH],
        [BTN_BACK],
    ])


def order_filter_kb():
    return kb([
        [ABTN_FILTER_PENDING],
        [ABTN_FILTER_ACCEPTED],
        [ABTN_FILTER_REJECTED],
        [ABTN_FILTER_ALL],
        [BTN_BACK],
    ])


def admins_menu_kb():
    return kb([[ABTN_ADD_ADMIN], [ABTN_REMOVE_ADMIN], [BTN_BACK]])


def edit_data_menu_kb():
    return kb([
        [ABTN_EDIT_WEBSITE], [ABTN_EDIT_LOGO], [ABTN_EDIT_BOT_SERVICE],
        [ABTN_EDIT_BOT], [ABTN_EDIT_MHDV],
        [ABTN_EDIT_CARD_NUM], [ABTN_EDIT_CARD_OWNER],
        [ABTN_EDIT_ADMIN_PHONE],
        [ABTN_EDIT_SOCIAL], [ABTN_EDIT_FAQ],
        [ABTN_EDIT_GAME_INFO],
        [BTN_BACK],
    ])


def settings_menu_kb():
    return kb([
        [ABTN_MAINTENANCE_TOGGLE],
        [ABTN_REMINDER_TOGGLE],
        [ABTN_EDIT_REMINDER_HOURS],
        [BTN_BACK],
    ])


def paginated_list_kb(items_labels, page, page_size=PAGE_SIZE, extra_rows=None):
    """items_labels: har bir element uchun tugma matni ro'yxati."""
    start = page * page_size
    chunk = items_labels[start:start + page_size]
    rows = [[label] for label in chunk]
    if page > 0:
        rows.append([ABTN_PREV_PAGE])
    if start + page_size < len(items_labels):
        rows.append([ABTN_NEXT_PAGE])
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


def order_stage_ready_kb():
    return kb([[ABTN_MARK_READY], [BTN_BACK]])


def order_stage_delivered_kb():
    return kb([[ABTN_MARK_DELIVERED], [BTN_BACK]])


def payment_action_kb():
    return kb([[ABTN_CONFIRM_PAYMENT], [BTN_BACK]])


def broadcast_segment_kb():
    return kb([[ABTN_SEG_ALL], [ABTN_SEG_ORDERED], [BTN_BACK]])


def broadcast_timing_kb():
    return kb([[ABTN_SEND_NOW], [ABTN_SEND_LATER], [BTN_BACK]])


def force_start_inline_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔄 /start", callback_data=FORCE_START_CALLBACK)]]
    )


def order_rating_inline_kb(order_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=str(n) + "⭐️", callback_data=f"orate:{order_id}:{n}") for n in range(1, 6)
    ]])


# =============================================================================
# 6. YORDAMCHI FUNKSIYALAR
# =============================================================================

def is_super_admin(tg_id: int) -> bool:
    """ADMIN_IDS'da ko'rsatilgan doimiy, olib tashlab bo'lmaydigan super-adminlar."""
    return tg_id in ADMIN_IDS


async def is_admin(tg_id: int) -> bool:
    """Super-admin yoki botga qo'shilgan oddiy admin bo'lsa True."""
    if is_super_admin(tg_id):
        return True
    return await db_is_db_admin(tg_id)


async def get_admin_role(tg_id: int) -> str:
    if is_super_admin(tg_id):
        return "super"
    if await db_is_db_admin(tg_id):
        return "admin"
    return ""


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
    # Barcha super-admin va DB adminlarga xabar yuboradi.
    admin_ids = set(ADMIN_IDS)
    try:
        for row in await db_get_admins():
            admin_ids.add(int(row["tg_id"]))
    except Exception as e:
        logger.warning(f"DB adminlar ro'yxatini olishda xatolik: {e}")
    for admin_id in admin_ids:
        if exclude and admin_id == exclude:
            continue
        try:
            await bot.send_message(admin_id, text)
        except (TelegramForbiddenError, TelegramBadRequest):
            pass
        except Exception as e:
            logger.warning(f"Adminga xabar yuborilmadi ({admin_id}): {e}")


async def safe_send(tg_id: int, text: str, **kwargs):
    try:
        await bot.send_message(tg_id, text, **kwargs)
        return True
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        logger.warning(f"Xabar yuborib bo'lmadi ({tg_id}): {e}")
        return False


ORDER_TYPE_TITLES = {"website": "🌐 Veb-sayt", "logo": "🎨 Logo", "bot": "🤖 Bot", "ai_image": "🖼 AI rasm"}


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


def format_bot_order(data: dict) -> str:
    return (
        f"🤖 <b>Bot yasash buyurtmasi</b>\n\n"
        f"📌 Bot nomi: {data.get('bot_name')}\n"
        f"🎯 Vazifasi/maqsadi: {data.get('purpose')}\n"
        f"📱 Platforma: {data.get('platform')}\n"
        f"💰 Byudjet: {data.get('budget')}\n"
        f"📋 Asosiy talablar: {data.get('requirements')}\n"
        f"➕ Qo'shimcha: {data.get('additional')}"
    )


def format_ai_image_order(data: dict) -> str:
    return (
        f"🖼 <b>AI rasm buyurtmasi</b>\n\n"
        f"🖌 Mavzu: {data.get('topic')}\n"
        f"🎨 Uslub: {data.get('style')}\n"
        f"📐 O'lcham/format: {data.get('size')}\n"
        f"💰 Byudjet: {data.get('budget')}\n"
        f"➕ Qo'shimcha: {data.get('additional')}"
    )


def format_order_body(order_type: str, data: dict) -> str:
    if order_type == "website":
        return format_website_order(data)
    if order_type == "logo":
        return format_logo_order(data)
    if order_type == "bot":
        return format_bot_order(data)
    if order_type == "ai_image":
        return format_ai_image_order(data)
    return json.dumps(data, ensure_ascii=False)


def format_order_short(order_row) -> str:
    data = json.loads(order_row["data"])
    if order_row["order_type"] == "website":
        title = data.get("site_name", "—")
    elif order_row["order_type"] == "bot":
        title = data.get("bot_name", "—")
    elif order_row["order_type"] == "ai_image":
        title = data.get("topic", "—")
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

BOT_STEPS = [
    (UserFlow.bot_name, "🤖 Yasalishi kerak bo'lgan botning nomini (yoki taxminiy nomini) kiriting:", back_kb),
    (UserFlow.bot_purpose, "🎯 Bot nima vazifani bajarishi kerak? Qisqacha tasvirlab bering:", back_kb),
    (UserFlow.bot_platform, "📱 Bot qaysi platformada ishlashi kerak? (Telegram, Instagram va h.k.):", back_kb),
    (UserFlow.bot_budget, "💰 Loyihaga ajratilgan byudjetingiz qancha?:", back_kb),
    (UserFlow.bot_requirements, "📋 Botga bo'lgan asosiy talablaringizni (funksiyalarini) yozing:", back_kb),
    (UserFlow.bot_additional, "➕ Qo'shimcha ma'lumot bo'lsa yozing. Bo'lmasa \"❌ Yo'q\" tugmasini bosing:", skip_back_kb),
]
BOT_KEYS = ["bot_name", "purpose", "platform", "budget", "requirements", "additional"]
BOT_STATES = [s for s, _, _ in BOT_STEPS]

AI_IMAGE_STEPS = [
    (UserFlow.ai_topic, "🖼 Qanday rasm chizdirmoqchisiz? Mavzu yoki g'oyasini yozing:", back_kb),
    (UserFlow.ai_style, "🎨 Qaysi uslubda bo'lishini xohlaysiz? (masalan: realistik, anime, 3D, chizma...):", back_kb),
    (UserFlow.ai_size, "📐 Rasm o'lchami/formati qanday bo'lsin? (masalan: kvadrat, banner, portret...):", back_kb),
    (UserFlow.ai_budget, "💰 Ushbu xizmat uchun ajratilgan byudjetingiz qancha?:", back_kb),
    (UserFlow.ai_additional, "➕ Qo'shimcha ma'lumot (ranglar, referens tavsifi va h.k.) bo'lsa yozing. "
                             "Bo'lmasa \"❌ Yo'q\" tugmasini bosing:", skip_back_kb),
]
AI_IMAGE_KEYS = ["topic", "style", "size", "budget", "additional"]
AI_IMAGE_STATES = [s for s, _, _ in AI_IMAGE_STEPS]


async def start_flow(message: Message, state: FSMContext, steps, data_key: str):
    await state.clear()
    await state.update_data(**{data_key: {}})
    first_state, prompt, kb_func = steps[0]
    await state.set_state(first_state)
    await message.answer(prompt, reply_markup=kb_func())
    await db_flow_touch(message.from_user.id, data_key)


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
        await db_flow_touch(message.from_user.id, data_key)
    else:
        await db_flow_clear(message.from_user.id)
        await finish_fn(message, state, bucket)


async def step_back(message: Message, state: FSMContext, steps):
    current = await state.get_state()
    idx = next(i for i, (s, _, _) in enumerate(steps) if s.state == current)
    if idx == 0:
        await state.clear()
        await db_flow_clear(message.from_user.id)
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


async def finish_bot_order(message: Message, state: FSMContext, bucket: dict):
    order_id = await db_create_order(message.from_user.id, "bot", bucket)
    await state.clear()
    await message.answer(
        "✅ Buyurtmangiz qabul qilindi! Tez orada admin ko'rib chiqadi va siz bilan bog'lanadi.\n\n"
        + format_bot_order(bucket),
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )
    user = await db_get_user(message.from_user.id)
    await notify_admins(
        f"🆕 Yangi <b>bot yasash</b> buyurtmasi (#{order_id})\n"
        f"👤 {user_display_name(user)}\n\n{format_bot_order(bucket)}\n\n"
        f"Ko'rish uchun: Admin panel → {ABTN_ORDERS} → {ABTN_ORDER_BOT}"
    )


async def finish_ai_image_order(message: Message, state: FSMContext, bucket: dict):
    order_id = await db_create_order(message.from_user.id, "ai_image", bucket)
    await state.clear()
    await message.answer(
        "✅ Buyurtmangiz qabul qilindi! Tez orada admin ko'rib chiqadi va siz bilan bog'lanadi.\n\n"
        + format_ai_image_order(bucket),
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )
    user = await db_get_user(message.from_user.id)
    await notify_admins(
        f"🆕 Yangi <b>AI rasm</b> buyurtmasi (#{order_id})\n"
        f"👤 {user_display_name(user)}\n\n{format_ai_image_order(bucket)}\n\n"
        f"Ko'rish uchun: Admin panel → {ABTN_ORDERS} → {ABTN_ORDER_AI_IMAGE}"
    )


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()

    if await is_admin(message.from_user.id):
        await enter_admin_mode(message, state)
        return

    if await db_is_blocked(message.from_user.id):
        await message.answer("⛔️ Siz botdan foydalanish huquqidan mahrum qilingansiz.")
        return
    is_new = await db_add_user(
        message.from_user.id, message.from_user.full_name, message.from_user.username
    )
    await db_clear_needs_restart(message.from_user.id)
    name = message.from_user.full_name or "mehmon"
    await message.answer(
        f"👋 Salom, {name}! MHDV botiga xush kelibsiz.\n\n"
        f"Quyidagi menyudan kerakli bo'limni tanlang 👇",
        reply_markup=main_menu_kb(),
    )
    if is_new:
        await notify_admins(f"🆕 Yangi foydalanuvchi botga qo'shildi:\n{message.from_user.full_name} "
                             f"(@{message.from_user.username or '—'}) — ID:{message.from_user.id}")


class _StartCallbackProxy:
    """CallbackQuery orqali /start bosilganda cmd_start funksiyasini qayta ishlatish uchun."""

    def __init__(self, chat_id: int, from_user):
        self.chat_id = chat_id
        self.from_user = from_user

    async def answer(self, text, **kwargs):
        return await bot.send_message(self.chat_id, text, **kwargs)


@dp.callback_query(F.data == FORCE_START_CALLBACK)
async def cb_force_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer("🔄 Ishga tushirilmoqda...")
    proxy = _StartCallbackProxy(callback.message.chat.id, callback.from_user)
    await cmd_start(proxy, state)


@dp.callback_query(F.data.startswith("orate:"))
async def cb_order_rate(callback: CallbackQuery, state: FSMContext):
    try:
        _, order_id_str, score_str = callback.data.split(":")
        order_id, score = int(order_id_str), int(score_str)
    except (ValueError, IndexError):
        await callback.answer()
        return
    await db_add_order_rating(order_id, callback.from_user.id, score)
    await callback.answer("🙏 Rahmat!")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except (TelegramBadRequest, TelegramForbiddenError):
        pass
    await callback.message.answer(f"🙏 Bahoyingiz ({score}⭐️) uchun rahmat!")


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


@dp.message(F.text == BTN_BOT)
async def btn_bot(message: Message, state: FSMContext):
    if await db_is_blocked(message.from_user.id):
        return
    await start_flow(message, state, BOT_STEPS, "bot_data")


@dp.message(F.text == BTN_BACK, StateFilter(*BOT_STATES))
async def bot_back(message: Message, state: FSMContext):
    await step_back(message, state, BOT_STEPS)


@dp.message(F.text == BTN_SKIP, StateFilter(UserFlow.bot_additional))
async def bot_skip_additional(message: Message, state: FSMContext):
    await step_forward(message, state, BOT_STEPS, BOT_KEYS, "bot_data", finish_bot_order, value="—")


@dp.message(StateFilter(*BOT_STATES))
async def bot_step_input(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Iltimos, matn ko'rinishida javob yozing.")
        return
    await step_forward(message, state, BOT_STEPS, BOT_KEYS, "bot_data", finish_bot_order)


@dp.message(F.text == BTN_AI_IMAGE)
async def btn_ai_image(message: Message, state: FSMContext):
    if await db_is_blocked(message.from_user.id):
        return
    await start_flow(message, state, AI_IMAGE_STEPS, "ai_image_data")


@dp.message(F.text == BTN_BACK, StateFilter(*AI_IMAGE_STATES))
async def ai_image_back(message: Message, state: FSMContext):
    await step_back(message, state, AI_IMAGE_STEPS)


@dp.message(F.text == BTN_SKIP, StateFilter(UserFlow.ai_additional))
async def ai_image_skip_additional(message: Message, state: FSMContext):
    await step_forward(message, state, AI_IMAGE_STEPS, AI_IMAGE_KEYS, "ai_image_data", finish_ai_image_order, value="—")


@dp.message(StateFilter(*AI_IMAGE_STATES))
async def ai_image_step_input(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Iltimos, matn ko'rinishida javob yozing.")
        return
    await step_forward(message, state, AI_IMAGE_STEPS, AI_IMAGE_KEYS, "ai_image_data", finish_ai_image_order)


@dp.message(F.text == BTN_INFO)
async def btn_info(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("ℹ️ Ma'lumot bo'limi. Nimani bilmoqchisiz?", reply_markup=info_menu_kb())


@dp.message(F.text == BTN_GAME)
async def btn_game(message: Message, state: FSMContext):
    if await db_is_blocked(message.from_user.id):
        return
    await state.clear()
    await message.answer(
        "🎮 <b>Logo o'yini</b>\n\nID raqam oling va tasodifiy tanlovda qatnashing!",
        reply_markup=game_menu_user_kb(),
        parse_mode="HTML",
    )


@dp.message(F.text == GBTN_GET_ID)
async def game_get_id(message: Message, state: FSMContext):
    if await db_is_blocked(message.from_user.id):
        return
    enabled = await db_get_setting("game_enabled")
    if enabled != "1":
        await message.answer("🔴 O'yin hozircha vaqtincha ishlamayapti.")
        return
    started = await db_get_setting("game_started")
    if started == "1":
        await message.answer("🎮 O'yin allaqachon boshlangan. Endi yangi ID raqam berilmaydi.")
        return
    existing = await db_game_get_participant(message.from_user.id)
    if existing:
        await message.answer(
            f"ℹ️ Sizda allaqachon ID raqam bor: <b>#{existing['game_number']}</b>", parse_mode="HTML"
        )
        return
    number = await db_game_assign_number(message.from_user.id)
    await message.answer(
        f"🎉 Tabriklaymiz! Sizning ID raqamingiz: <b>#{number}</b>\n\n"
        f"G'olib e'lon qilinganda shu bot orqali xabar beramiz.",
        parse_mode="HTML",
    )


@dp.message(F.text == GBTN_INFO)
async def game_info_btn(message: Message, state: FSMContext):
    if await db_is_blocked(message.from_user.id):
        return
    text = await db_get_setting("game_info")
    await message.answer(text)


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
    status_icon = {"pending": "🕓", "accepted": "✅", "rejected": "❌"}
    rows = []
    for o in orders[:30]:
        title = ORDER_TYPE_TITLES.get(o["order_type"], o["order_type"])
        icon = status_icon.get(o["status"], "🕓")
        rows.append([InlineKeyboardButton(
            text=f"{icon} #{o['id']} — {title} ({o['created_at'][:10]})",
            callback_data=f"myorder:{o['id']}",
        )])
    await message.answer(
        "📦 <b>Sizning buyurtmalaringiz:</b>\n\nBatafsil ko'rish uchun tanlang:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("myorder:"))
async def cb_my_order_view(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(":")[1])
    order = await db_get_order(order_id)
    await callback.answer()
    if not order or order["tg_id"] != callback.from_user.id:
        return
    body = format_order_body(order["order_type"], json.loads(order["data"]))
    status_text = {"pending": "🕓 Ko'rib chiqilmoqda", "accepted": "✅ Qabul qilindi",
                   "rejected": "❌ Rad etildi"}.get(order["status"], order["status"])
    stage_text = {"ishlanmoqda": "🔧 Ishlanmoqda", "tayyor": "🚀 Tayyor",
                  "topshirildi": "📬 Topshirildi"}.get(order["stage"], "")
    text = f"{body}\n\n📌 Holat: {status_text}"
    if stage_text:
        text += f"\n📶 Bosqich: {stage_text}"
    if order["status"] == "rejected" and order["reject_reason"]:
        text += f"\n❌ Sabab: {order['reject_reason']}"
    if order["revision_note"]:
        text += f"\n✏️ So'ralgan tuzatish: {order['revision_note']}"

    buttons = []
    if order["status"] == "accepted" and order["stage"] in ("tayyor", "topshirildi"):
        buttons.append([InlineKeyboardButton(text="✏️ Tuzatish so'rash", callback_data=f"revise:{order_id}")])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    await callback.message.answer(text, reply_markup=markup, parse_mode="HTML")


@dp.callback_query(F.data.startswith("revise:"))
async def cb_order_revise(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(":")[1])
    order = await db_get_order(order_id)
    await callback.answer()
    if not order or order["tg_id"] != callback.from_user.id:
        return
    await state.set_state(UserFlow.revision_note)
    await state.update_data(revision_order_id=order_id)
    await callback.message.answer(
        "✏️ Nimani tuzatish kerakligini yozing — biz adminga yetkazamiz:",
        reply_markup=back_kb(),
    )


@dp.message(F.text == BTN_BACK, StateFilter(UserFlow.revision_note))
async def revision_note_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🚫 Bekor qilindi.", reply_markup=main_menu_kb())


@dp.message(StateFilter(UserFlow.revision_note))
async def revision_note_input(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("revision_order_id")
    order = await db_get_order(order_id)
    if not order:
        await state.clear()
        return
    await db_set_order_revision(order_id, message.text)
    await db_set_order_stage(order_id, "ishlanmoqda")
    await state.clear()
    await message.answer("✅ So'rovingiz adminga yuborildi. Tez orada aloqaga chiqamiz.", reply_markup=main_menu_kb())
    title = ORDER_TYPE_TITLES.get(order["order_type"], order["order_type"])
    user = await db_get_user(message.from_user.id)
    await notify_admins(
        f"✏️ <b>Tuzatish so'rovi</b> — {title} #{order_id}\n"
        f"👤 {user_display_name(user)}\n\n📝 {message.text}"
    )


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
    if await is_admin(message.from_user.id):
        data = await state.get_data()
        section = data.get("admin_section", "main")
        if section == "order_view":
            parent = data.get("order_return_section", "orders_list")
        else:
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


# ---- To'lovlarim (foydalanuvchi uchun) ----

@dp.message(F.text == BTN_MY_PAYMENTS)
async def btn_my_payments(message: Message, state: FSMContext):
    await state.clear()
    orders = await db_get_orders_by_user(message.from_user.id)
    payable_orders = [o for o in orders if o["status"] == "accepted"]
    if not payable_orders:
        await message.answer(
            "💳 Hozircha to'lov talab qiladigan buyurtmangiz yo'q.", reply_markup=main_menu_kb()
        )
        return

    unpaid_lines, pending_lines, confirmed_lines = [], [], []
    unpaid_buttons = []
    for o in payable_orders:
        title = ORDER_TYPE_TITLES.get(o["order_type"], o["order_type"])
        p = await db_get_payment_by_order(o["id"])
        line = f"{title} — #{o['id']} ({o['created_at'][:10]})"
        if not p:
            unpaid_lines.append(line)
            unpaid_buttons.append([InlineKeyboardButton(
                text=f"💳 To'lov qilish — #{o['id']}", callback_data=f"paynow:{o['id']}"
            )])
        elif p["status"] == "pending":
            pending_lines.append(line)
        else:
            confirmed_lines.append(line)

    parts = ["💳 <b>To'lovlarim</b>\n"]
    parts.append(f"❌ <b>Hali qilinmagan to'lovlar</b> ({len(unpaid_lines)}):")
    parts.append("\n".join(unpaid_lines) if unpaid_lines else "— yo'q")
    parts.append(f"\n🕓 <b>Tekshirilmoqda</b> ({len(pending_lines)}):")
    parts.append("\n".join(pending_lines) if pending_lines else "— yo'q")
    parts.append(f"\n✅ <b>Tasdiqlangan</b> ({len(confirmed_lines)}):")
    parts.append("\n".join(confirmed_lines) if confirmed_lines else "— yo'q")

    markup = InlineKeyboardMarkup(inline_keyboard=unpaid_buttons) if unpaid_buttons else None
    await message.answer("\n".join(parts), reply_markup=markup, parse_mode="HTML")
    if markup:
        await message.answer("👇 Asosiy menyu:", reply_markup=main_menu_kb())
    else:
        await message.answer("Asosiy menyu:", reply_markup=main_menu_kb())


@dp.callback_query(F.data.startswith("paynow:"))
async def cb_pay_now(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(":")[1])
    order = await db_get_order(order_id)
    await callback.answer()
    if not order or order["tg_id"] != callback.from_user.id or order["status"] != "accepted":
        return
    existing = await db_get_payment_by_order(order_id)
    if existing:
        await callback.message.answer("ℹ️ Bu buyurtma uchun to'lov allaqachon yuborilgan.")
        return
    card_num = await db_get_setting("card_number")
    card_owner = await db_get_setting("card_owner")
    await state.set_state(UserFlow.awaiting_payment)
    await state.update_data(payment_order_id=order_id)
    await callback.message.answer(
        f"💳 Karta raqami: <code>{card_num}</code>\n👤 Karta egasi: {card_owner}\n\n"
        f"To'lov qilgach, chek rasmini yoki PDF faylini shu botga yuboring.",
        parse_mode="HTML",
    )


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
    role = await get_admin_role(message.from_user.id)
    total = await db_count_users_total()
    today = await db_count_users_today()
    role_label = "👑 Super-admin" if role == "super" else "🛠 Admin"
    await message.answer(
        f"🛠 <b>Admin panelga xush kelibsiz!</b> ({role_label})\n\n"
        f"👥 Jami foydalanuvchilar: {total}\n"
        f"🆕 Bugun qo'shilganlar: {today}\n\n"
        f"Kerakli bo'limni tanlang 👇",
        reply_markup=admin_menu_kb(role),
        parse_mode="HTML",
    )


@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await message.answer("⛔️ Sizda admin panelga kirish huquqi yo'q.")
        return
    # Admin ID'lar doimiy ravishda admin panelida bo'ladi — parol shart emas.
    await enter_admin_mode(message, state)


def guard_admin(func):
    async def wrapper(message: Message, state: FSMContext):
        if not await is_admin(message.from_user.id):
            return
        return await func(message, state)
    return wrapper


def guard_super_admin(func):
    """Faqat super-adminlar (ADMIN_IDS) kira oladigan bo'limlar uchun."""
    async def wrapper(message: Message, state: FSMContext):
        if not is_super_admin(message.from_user.id):
            if await is_admin(message.from_user.id):
                await message.answer("⛔️ Bu bo'lim faqat super-adminlar uchun.")
            return
        return await func(message, state)
    return wrapper


# ---------- Admin: BTN_BACK navigatsiyasi ----------

ADMIN_PARENT = {
    "users_list": "main", "user_card": "users_list",
    "chats_list": "main", "chat_view": "chats_list",
    "feedback_list": "main", "feedback_view": "feedback_list",
    "orders_type": "main", "order_filter": "orders_type",
    "orders_list": "order_filter", "order_view": "orders_list",
    "user_orders_list": "user_card",
    "payments_list": "main", "payment_view": "payments_list",
    "edit_menu": "main", "settings_menu": "main",
    "admins_menu": "main", "admin_logs": "main",
    "game_menu": "main", "game_active": "game_menu",
    "game_users_list": "game_menu", "game_pick_list": "game_active",
}


@dp.message(F.text == BTN_BACK, StateFilter(
    AdminFlow.reject_reason, AdminFlow.broadcast_text, AdminFlow.new_setting_value,
    AdminFlow.reply_to_user, AdminFlow.reply_to_feedback,
    AdminFlow.add_admin_id, AdminFlow.remove_admin_id, AdminFlow.order_search,
    AdminFlow.broadcast_segment, AdminFlow.broadcast_timing, AdminFlow.broadcast_schedule_time,
))
async def admin_input_cancel(message: Message, state: FSMContext):
    data = await state.get_data()
    section = data.get("admin_section", "main")
    await state.set_state(None)
    await render_admin_section(message, state, section)


async def render_admin_section(message: Message, state: FSMContext, section: str, **ctx):
    if section == "main":
        role = await get_admin_role(message.from_user.id)
        await state.update_data(admin_section="main")
        await message.answer("🛠 Admin bosh menyu:", reply_markup=admin_menu_kb(role))
    elif section == "users_list":
        await show_users_list(message, state, ctx.get("page", 0))
    elif section == "chats_list":
        await show_chats_list(message, state, ctx.get("page", 0))
    elif section == "feedback_list":
        await show_feedback_list(message, state, ctx.get("page", 0))
    elif section == "orders_type":
        await state.update_data(admin_section="orders_type")
        await message.answer("🛒 Qaysi bo'lim zakazlarini ko'rmoqchisiz?", reply_markup=order_type_menu_kb())
    elif section == "order_filter":
        await state.update_data(admin_section="order_filter")
        data = await state.get_data()
        title = ORDER_TYPE_TITLES.get(data.get("order_type"), "")
        await message.answer(f"{title}: qaysi holatdagi zakazlarni ko'rmoqchisiz?", reply_markup=order_filter_kb())
    elif section == "orders_list":
        data = await state.get_data()
        await show_orders_list(
            message, state,
            ctx.get("order_type", data.get("order_type")),
            ctx.get("status", data.get("order_status_filter")),
            ctx.get("page", data.get("list_page", 0)),
        )
    elif section == "payments_list":
        await show_payments_list(message, state, ctx.get("page", 0))
    elif section == "user_orders_list":
        data = await state.get_data()
        await show_user_orders_list(
            message, state, ctx.get("tg_id", data.get("user_orders_tg_id")), ctx.get("page", 0)
        )
    elif section == "edit_menu":
        await state.update_data(admin_section="edit_menu")
        await message.answer("✏️ Qaysi ma'lumotni o'zgartirmoqchisiz?", reply_markup=edit_data_menu_kb())
    elif section == "settings_menu":
        await show_settings_menu(message, state)
    elif section == "admins_menu":
        await show_admins_section(message, state)
    elif section == "admin_logs":
        await show_admin_logs(message, state)
    elif section == "game_menu":
        await show_game_menu(message, state)
    elif section == "game_active":
        await state.update_data(admin_section="game_active")
        await message.answer("🎮 O'yin faol. Kerakli amalni tanlang:", reply_markup=game_active_kb())
    elif section == "game_users_list":
        await show_game_users_list(message, state, ctx.get("page", 0))
    elif section == "game_pick_list":
        await show_game_pick_list(message, state, ctx.get("page", 0))
    else:
        role = await get_admin_role(message.from_user.id)
        await state.update_data(admin_section="main")
        await message.answer("🛠 Admin bosh menyu:", reply_markup=admin_menu_kb(role))


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
        await show_orders_list(message, state, data.get("order_type"), data.get("order_status_filter"), page)
    elif section == "payments_list":
        await show_payments_list(message, state, page)
    elif section == "user_orders_list":
        await show_user_orders_list(message, state, data.get("user_orders_tg_id"), page)
    elif section == "game_users_list":
        await show_game_users_list(message, state, page)
    elif section == "game_pick_list":
        await show_game_pick_list(message, state, page)


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
        await db_log_admin_action(message.from_user.id, "block_user", f"ID:{tg_id}")
    else:
        await message.answer("✅ Foydalanuvchi blokdan chiqarildi.")
        await safe_send(tg_id, "✅ Sizga botdan foydalanish huquqi qaytarildi.")
        await db_log_admin_action(message.from_user.id, "unblock_user", f"ID:{tg_id}")
    await show_user_card(message, state, tg_id)


@dp.message(F.text == ABTN_USER_ORDERS)
@guard_admin
async def admin_user_orders(message: Message, state: FSMContext):
    data = await state.get_data()
    tg_id = data.get("selected_tg_id")
    await show_user_orders_list(message, state, tg_id, 0)


async def show_user_orders_list(message: Message, state: FSMContext, tg_id: int, page: int):
    orders = await db_get_orders_by_user(tg_id)
    await state.update_data(admin_section="user_orders_list", user_orders_tg_id=tg_id, list_page=page)
    if not orders:
        await message.answer("📦 Bu foydalanuvchining buyurtmalari yo'q.", reply_markup=kb([[BTN_BACK]]))
        return
    labels = [format_order_short(o) for o in orders]
    await message.answer(
        f"📦 Buyurtmalar (jami: {len(labels)}). Boshqarish uchun tanlang:",
        reply_markup=paginated_list_kb(labels, page),
    )


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
    await message.answer(text, reply_markup=admin_menu_kb(await get_admin_role(message.from_user.id)), parse_mode="HTML")
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


async def open_order_filter(message: Message, state: FSMContext, order_type: str):
    await state.update_data(admin_section="order_filter", order_type=order_type)
    title = ORDER_TYPE_TITLES.get(order_type, order_type)
    await message.answer(f"{title}: qaysi holatdagi zakazlarni ko'rmoqchisiz?", reply_markup=order_filter_kb())


@dp.message(F.text == ABTN_ORDER_WEBSITE)
@guard_admin
async def admin_orders_website(message: Message, state: FSMContext):
    await open_order_filter(message, state, "website")


@dp.message(F.text == ABTN_ORDER_LOGO)
@guard_admin
async def admin_orders_logo(message: Message, state: FSMContext):
    await open_order_filter(message, state, "logo")


@dp.message(F.text == ABTN_ORDER_BOT)
@guard_admin
async def admin_orders_bot(message: Message, state: FSMContext):
    await open_order_filter(message, state, "bot")


@dp.message(F.text == ABTN_ORDER_AI_IMAGE)
@guard_admin
async def admin_orders_ai_image(message: Message, state: FSMContext):
    await open_order_filter(message, state, "ai_image")


FILTER_LABEL_TO_STATUS = {
    ABTN_FILTER_PENDING: "pending",
    ABTN_FILTER_ACCEPTED: "accepted",
    ABTN_FILTER_REJECTED: "rejected",
    ABTN_FILTER_ALL: None,
}


@dp.message(F.text.in_(list(FILTER_LABEL_TO_STATUS.keys())))
@guard_admin
async def admin_orders_apply_filter(message: Message, state: FSMContext):
    data = await state.get_data()
    order_type = data.get("order_type")
    if not order_type or data.get("admin_section") != "order_filter":
        return
    status = FILTER_LABEL_TO_STATUS[message.text]
    await show_orders_list(message, state, order_type, status, 0)


@dp.message(F.text == ABTN_ORDER_SEARCH)
@guard_admin
async def admin_order_search_btn(message: Message, state: FSMContext):
    await state.set_state(AdminFlow.order_search)
    await message.answer("🔍 Qidirmoqchi bo'lgan buyurtma ID raqamini kiriting:", reply_markup=back_kb())


@dp.message(StateFilter(AdminFlow.order_search))
async def admin_order_search_input(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.set_state(None)
        return
    query = (message.text or "").strip()
    await state.set_state(None)
    results = await db_search_orders(query)
    if not results:
        await message.answer("🔍 Hech narsa topilmadi. Iltimos, to'g'ri ID raqamini kiriting.",
                              reply_markup=order_type_menu_kb())
        await state.update_data(admin_section="orders_type")
        return
    if len(results) == 1:
        await show_order_view(message, state, results[0]["id"], return_section="orders_type")
        return
    labels = [format_order_short(o) for o in results]
    await state.update_data(admin_section="orders_list", order_type=results[0]["order_type"],
                             order_status_filter=None, list_page=0)
    await message.answer(
        f"🔍 Qidiruv natijalari (jami: {len(labels)}):",
        reply_markup=paginated_list_kb(labels, 0),
    )


async def show_orders_list(message: Message, state: FSMContext, order_type: str, status, page: int):
    orders = await db_get_orders_by_type(order_type, status)
    labels = [format_order_short(o) for o in orders]
    await state.update_data(admin_section="orders_list", order_type=order_type,
                             order_status_filter=status, list_page=page)
    title = ORDER_TYPE_TITLES.get(order_type, order_type)
    status_label = {"pending": " (🕓 kutilmoqda)", "accepted": " (✅ qabul qilingan)",
                    "rejected": " (❌ rad etilgan)", None: ""}.get(status, "")
    if not labels:
        await message.answer(f"{title}{status_label}: hozircha buyurtmalar yo'q.", reply_markup=kb([[BTN_BACK]]))
        return
    await message.answer(
        f"{title}{status_label} <b>zakazlari</b> (jami: {len(labels)})\nKerakli buyurtmani tanlang:",
        reply_markup=paginated_list_kb(labels, page),
        parse_mode="HTML",
    )


async def show_order_view(message: Message, state: FSMContext, order_id: int, return_section: str = None):
    o = await db_get_order(order_id)
    if not o:
        await message.answer("Buyurtma topilmadi.")
        return
    if return_section is None:
        data0 = await state.get_data()
        return_section = data0.get("order_return_section", "orders_list")
    await state.update_data(
        admin_section="order_view", selected_order_id=order_id,
        order_type=o["order_type"], order_return_section=return_section,
    )
    data = json.loads(o["data"])
    body = format_order_body(o["order_type"], data)
    user = await db_get_user(o["tg_id"])
    status_text = {"pending": "🕓 Ko'rib chiqilmoqda", "accepted": "✅ Qabul qilingan",
                   "rejected": "❌ Rad etilgan"}.get(o["status"], o["status"])
    stage_text = {"ishlanmoqda": "🔧 Ishlanmoqda", "tayyor": "🚀 Tayyor (topshirilmagan)",
                  "topshirildi": "📬 Topshirildi"}.get(o["stage"], "")
    text = f"{body}\n\n👤 {user_display_name(user) if user else o['tg_id']}\n📌 Holat: {status_text}"
    if stage_text:
        text += f"\n📶 Bosqich: {stage_text}"
    if o["revision_note"]:
        text += f"\n✏️ Tuzatish so'ralgan: {o['revision_note']}"
    if o["status"] == "rejected" and o["reject_reason"]:
        text += f"\n❌ Sabab: {o['reject_reason']}"

    if o["status"] == "pending":
        markup = order_action_kb()
    elif o["status"] == "accepted" and o["stage"] in (None, "ishlanmoqda"):
        markup = order_stage_ready_kb()
    elif o["status"] == "accepted" and o["stage"] == "tayyor":
        markup = order_stage_delivered_kb()
    else:
        markup = kb([[BTN_BACK]])
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
    await db_log_admin_action(message.from_user.id, "accept_order", f"order #{order_id}")
    await show_orders_list(message, state, order["order_type"], data.get("order_status_filter"), data.get("list_page", 0))


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
    await db_log_admin_action(message.from_user.id, "reject_order", f"order #{order_id}: {message.text}")
    await show_orders_list(message, state, order["order_type"], data.get("order_status_filter"), data.get("list_page", 0))


# ---------- 5.1) Buyurtma bosqichlari: Ishlanmoqda → Tayyor → Topshirildi ----------

@dp.message(F.text == ABTN_MARK_READY)
@guard_admin
async def admin_order_mark_ready_start(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("selected_order_id")
    if not order_id:
        return
    await state.set_state(AdminFlow.deliver_order)
    await message.answer(
        "🚀 Tayyor bo'lgan faylni (hujjat yoki rasm) yuboring — u mijozga avtomatik yuboriladi:",
        reply_markup=back_kb(),
    )


@dp.message(StateFilter(AdminFlow.deliver_order), F.text == BTN_BACK)
async def admin_deliver_cancel(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("selected_order_id")
    await state.set_state(None)
    await show_order_view(message, state, order_id)


@dp.message(StateFilter(AdminFlow.deliver_order), F.document | F.photo)
async def admin_deliver_file(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("selected_order_id")
    order = await db_get_order(order_id)
    if not order:
        await state.set_state(None)
        return
    caption = "🎉 <b>Buyurtmangiz tayyor!</b>\n\nQuyida natijani topasiz. Savolingiz bo'lsa, admin bilan bog'laning."
    try:
        if message.document:
            await bot.send_document(order["tg_id"], message.document.file_id, caption=caption, parse_mode="HTML")
        elif message.photo:
            await bot.send_photo(order["tg_id"], message.photo[-1].file_id, caption=caption, parse_mode="HTML")
        sent_ok = True
    except (TelegramBadRequest, TelegramForbiddenError):
        sent_ok = False
    await db_set_order_stage(order_id, "tayyor")
    await state.set_state(None)
    await db_log_admin_action(message.from_user.id, "mark_ready", f"order #{order_id}")
    if sent_ok:
        await message.answer("✅ Fayl mijozga yuborildi. Buyurtma \"Tayyor\" deb belgilandi.")
    else:
        await message.answer("⚠️ Fayl yuborilmadi (foydalanuvchi botni bloklagan bo'lishi mumkin), "
                              "lekin buyurtma \"Tayyor\" deb belgilandi.")
    await show_order_view(message, state, order_id)


@dp.message(StateFilter(AdminFlow.deliver_order))
async def admin_deliver_file_wrong_type(message: Message, state: FSMContext):
    await message.answer("❗️ Iltimos, hujjat yoki rasm ko'rinishida fayl yuboring.")


@dp.message(F.text == ABTN_MARK_DELIVERED)
@guard_admin
async def admin_order_mark_delivered(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("selected_order_id")
    if not order_id:
        return
    order = await db_get_order(order_id)
    await db_set_order_stage(order_id, "topshirildi")
    await safe_send(
        order["tg_id"],
        "📬 Buyurtmangiz to'liq topshirildi! Xizmatimizni qanday baholaysiz?",
        reply_markup=order_rating_inline_kb(order_id),
    )
    await message.answer("✅ Buyurtma \"Topshirildi\" deb belgilandi, foydalanuvchidan baho so'raldi.")
    await db_log_admin_action(message.from_user.id, "mark_delivered", f"order #{order_id}")
    await show_order_view(message, state, order_id)


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
    if p["order_id"]:
        await db_set_order_stage(p["order_id"], "ishlanmoqda")
        await safe_send(
            p["tg_id"],
            "✅ To'lovingiz tasdiqlandi!\n\n🔧 Buyurtmangiz ustida ish boshlandi.",
        )
    else:
        await safe_send(p["tg_id"], "✅ To'lovingiz tasdiqlandi! Tez orada admin siz bilan bog'lanadi.")
    user = await db_get_user(p["tg_id"])
    link = f"https://t.me/{user['username']}" if user and user["username"] else f"ID:{p['tg_id']} (username yo'q, \"{ABTN_WRITE_USER}\" orqali yozing)"
    await message.answer(f"✅ To'lov tasdiqlandi. Foydalanuvchi bilan bog'lanish: {link}")
    await db_log_admin_action(message.from_user.id, "confirm_payment", f"payment #{pid}, order #{p['order_id']}")
    await show_payments_list(message, state, data.get("list_page", 0))


# ---------- 7) Ma'lumotlarni almashtirish ----------

@dp.message(F.text == ABTN_EDIT_DATA)
@guard_super_admin
async def admin_edit_data_btn(message: Message, state: FSMContext):
    await state.update_data(admin_section="edit_menu")
    await message.answer("✏️ Qaysi ma'lumotni o'zgartirmoqchisiz?", reply_markup=edit_data_menu_kb())


@dp.message(F.text.in_(list(LABEL_TO_SETTING.keys())))
@guard_super_admin
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
    if not is_super_admin(message.from_user.id):
        await state.set_state(None)
        return
    data = await state.get_data()
    key = data.get("editing_key")
    value = (message.text or "").strip()
    if key == "reminder_hours":
        if not value.isdigit() or int(value) <= 0:
            await message.answer("❗️ Iltimos, musbat butun son kiriting (masalan: 2):")
            return
    await db_set_setting(key, value)
    await state.set_state(None)
    await message.answer("✅ Ma'lumot yangilandi!")
    await state.update_data(admin_section="edit_menu" if key != "reminder_hours" else "settings_menu")
    if key == "reminder_hours":
        await show_settings_menu(message, state)
    else:
        await message.answer("✏️ Yana biror narsani o'zgartirmoqchimisiz?", reply_markup=edit_data_menu_kb())


# ---------- 8) Hammaga xabar yuborish (broadcast) ----------

@dp.message(F.text == ABTN_BROADCAST)
@guard_super_admin
async def admin_broadcast_btn(message: Message, state: FSMContext):
    await state.set_state(AdminFlow.broadcast_text)
    await message.answer(
        "📢 Barcha foydalanuvchilarga yubormoqchi bo'lgan xabaringizni yozing:", reply_markup=back_kb()
    )


@dp.message(StateFilter(AdminFlow.broadcast_text))
async def admin_broadcast_input(message: Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        await state.set_state(None)
        return
    await state.update_data(broadcast_text=message.text)
    await state.set_state(AdminFlow.broadcast_segment)
    await message.answer("👥 Kimlarga yuborilsin?", reply_markup=broadcast_segment_kb())


@dp.message(StateFilter(AdminFlow.broadcast_segment), F.text.in_([ABTN_SEG_ALL, ABTN_SEG_ORDERED]))
async def admin_broadcast_segment(message: Message, state: FSMContext):
    segment = "ordered" if message.text == ABTN_SEG_ORDERED else "all"
    await state.update_data(broadcast_segment=segment)
    await state.set_state(AdminFlow.broadcast_timing)
    await message.answer("⏰ Qachon yuborilsin?", reply_markup=broadcast_timing_kb())


@dp.message(StateFilter(AdminFlow.broadcast_timing), F.text == ABTN_SEND_NOW)
async def admin_broadcast_send_now(message: Message, state: FSMContext):
    data = await state.get_data()
    text = data.get("broadcast_text", "")
    segment = data.get("broadcast_segment", "all")
    await state.set_state(None)
    await state.update_data(admin_section="main")
    users = await db_get_users_with_orders() if segment == "ordered" else await db_get_all_users()
    sent, failed = 0, 0
    await message.answer(f"📢 Yuborish boshlandi... ({len(users)} foydalanuvchi)")
    for u in users:
        if u["is_blocked"]:
            continue
        ok = await safe_send(u["tg_id"], f"📢 <b>E'lon:</b>\n\n{text}", parse_mode="HTML")
        if ok:
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)
    role = await get_admin_role(message.from_user.id)
    await db_log_admin_action(message.from_user.id, "broadcast", f"segment={segment}, sent={sent}, failed={failed}")
    await message.answer(f"✅ Xabar yuborildi: {sent} ta\n⚠️ Yuborilmadi: {failed} ta", reply_markup=admin_menu_kb(role))


@dp.message(StateFilter(AdminFlow.broadcast_timing), F.text == ABTN_SEND_LATER)
async def admin_broadcast_send_later(message: Message, state: FSMContext):
    await state.set_state(AdminFlow.broadcast_schedule_time)
    await message.answer("⏰ Necha daqiqadan keyin yuborilsin? Faqat raqam kiriting (masalan: 60):",
                          reply_markup=back_kb())


@dp.message(StateFilter(AdminFlow.broadcast_schedule_time))
async def admin_broadcast_schedule_input(message: Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        await state.set_state(None)
        return
    text = (message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await message.answer("❗️ Iltimos, musbat butun son kiriting (masalan: 60):")
        return
    minutes = int(text)
    data = await state.get_data()
    broadcast_text = data.get("broadcast_text", "")
    segment = data.get("broadcast_segment", "all")
    send_at = (datetime.datetime.now() + datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    await db_add_scheduled_broadcast(broadcast_text, segment, send_at, message.from_user.id)
    await state.set_state(None)
    role = await get_admin_role(message.from_user.id)
    await state.update_data(admin_section="main")
    await db_log_admin_action(message.from_user.id, "schedule_broadcast", f"segment={segment}, send_at={send_at}")
    await message.answer(f"✅ Xabar {minutes} daqiqadan keyin ({send_at}) yuboriladi.", reply_markup=admin_menu_kb(role))


# ---------- 9) Eksport ----------

@dp.message(F.text == ABTN_EXPORT)
@guard_super_admin
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
@guard_super_admin
async def admin_settings_btn(message: Message, state: FSMContext):
    await show_settings_menu(message, state)


async def show_settings_menu(message: Message, state: FSMContext):
    await state.update_data(admin_section="settings_menu")
    maintenance = await db_get_setting("maintenance_mode")
    status = "🔴 Yoqilgan (foydalanuvchilar botdan foydalana olmaydi)" if maintenance == "1" else "🟢 O'chirilgan (bot normal ishlayapti)"
    reminders = await db_get_setting("reminders_enabled")
    rem_status = "🟢 Yoqilgan" if reminders == "1" else "🔴 O'chirilgan"
    rem_hours = await db_get_setting("reminder_hours") or "2"
    await message.answer(
        f"⚙️ <b>Bot sozlamalari</b>\n\n"
        f"🔧 Texnik ishlar rejimi: {status}\n"
        f"🔔 Avtomatik eslatmalar: {rem_status}\n"
        f"⏰ Eslatma vaqti: {rem_hours} soat",
        reply_markup=settings_menu_kb(),
        parse_mode="HTML",
    )


@dp.message(F.text == ABTN_MAINTENANCE_TOGGLE)
@guard_super_admin
async def admin_toggle_maintenance(message: Message, state: FSMContext):
    current = await db_get_setting("maintenance_mode")
    new_val = "0" if current == "1" else "1"
    await db_set_setting("maintenance_mode", new_val)
    await db_log_admin_action(message.from_user.id, "toggle_maintenance", f"new={new_val}")
    await message.answer("✅ Sozlama o'zgartirildi.")
    await show_settings_menu(message, state)


@dp.message(F.text == ABTN_REMINDER_TOGGLE)
@guard_super_admin
async def admin_toggle_reminders(message: Message, state: FSMContext):
    current = await db_get_setting("reminders_enabled")
    new_val = "0" if current == "1" else "1"
    await db_set_setting("reminders_enabled", new_val)
    await message.answer("✅ Sozlama o'zgartirildi.")
    await show_settings_menu(message, state)


@dp.message(F.text == ABTN_EDIT_REMINDER_HOURS)
@guard_super_admin
async def admin_edit_reminder_hours_btn(message: Message, state: FSMContext):
    current = await db_get_setting("reminder_hours") or "2"
    await state.update_data(editing_key="reminder_hours")
    await state.set_state(AdminFlow.new_setting_value)
    await message.answer(
        f"⏰ Hozirgi eslatma vaqti: {current} soat.\n"
        f"👇 Yangi qiymatni faqat raqam bilan yozing (masalan: 3):",
        reply_markup=back_kb(),
    )


# ---------- 10.1) Barcha foydalanuvchilarga qayta /start so'rash ----------

@dp.message(F.text == ABTN_FORCE_RESTART)
@guard_super_admin
async def admin_force_restart_btn(message: Message, state: FSMContext):
    users = await db_get_all_users()
    await db_set_all_needs_restart()
    await message.answer(f"🔁 Yuborilmoqda... ({len(users)} foydalanuvchi)")
    sent, failed = 0, 0
    for u in users:
        if u["is_blocked"]:
            continue
        try:
            await get_user_fsm(u["tg_id"]).clear()
        except Exception:
            pass
        ok = await safe_send(
            u["tg_id"],
            "🔄 <b>Bot yangilandi!</b>\n\nDavom etish uchun quyidagi tugmani bosing "
            "yoki /start buyrug'ini yuboring:",
            reply_markup=force_start_inline_kb(),
            parse_mode="HTML",
        )
        if ok:
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)
    role = await get_admin_role(message.from_user.id)
    await message.answer(
        f"✅ {sent} ta foydalanuvchiga yuborildi. ⚠️ {failed} taga yetmadi.\n"
        f"Ular qaytadan /start bermaguncha botning boshqa tugmalari ishlamaydi.",
        reply_markup=admin_menu_kb(role),
    )


# ---------- 11) Ko'p darajali adminlar ----------

@dp.message(F.text == ABTN_ADMINS)
@guard_super_admin
async def admin_admins_btn(message: Message, state: FSMContext):
    await show_admins_section(message, state)


async def show_admins_section(message: Message, state: FSMContext):
    await state.update_data(admin_section="admins_menu")
    admins = await db_get_admins()
    lines = ["👑 <b>Adminlar boshqaruvi</b>\n"]
    lines.append("🔒 <b>Doimiy super-adminlar</b> (.env orqali, olib bo'lmaydi):")
    if ADMIN_IDS:
        lines.extend([f"— ID:{aid}" for aid in ADMIN_IDS])
    else:
        lines.append("— yo'q")
    lines.append("\n🛠 <b>Oddiy adminlar</b> (botdan qo'shilgan):")
    if admins:
        for a in admins:
            lines.append(f"— ID:{a['tg_id']} (qo'shilgan: {a['created_at']})")
    else:
        lines.append("— hozircha yo'q")
    await message.answer("\n".join(lines), reply_markup=admins_menu_kb(), parse_mode="HTML")


@dp.message(F.text == ABTN_ADMIN_LOGS)
@guard_super_admin
async def admin_logs_btn(message: Message, state: FSMContext):
    await show_admin_logs(message, state)


async def show_admin_logs(message: Message, state: FSMContext):
    await state.update_data(admin_section="admin_logs")
    logs = await db_get_admin_logs(50)
    if not logs:
        await message.answer("📜 Hozircha hech qanday amal qayd etilmagan.", reply_markup=kb([[BTN_BACK]]))
        return
    lines = ["📜 <b>Adminlar tarixi</b> (oxirgi 50 ta):\n"]
    for log in logs:
        lines.append(f"🕓 {log['created_at']} — ID:{log['admin_id']} — {log['action']} — {log['details']}")
    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3800] + "\n…"
    await message.answer(text, reply_markup=kb([[BTN_BACK]]), parse_mode="HTML")


@dp.message(F.text == ABTN_ADD_ADMIN)
@guard_super_admin
async def admin_add_admin_btn(message: Message, state: FSMContext):
    await state.set_state(AdminFlow.add_admin_id)
    await message.answer(
        "➕ Yangi admin qilib tayinlamoqchi bo'lgan foydalanuvchining Telegram ID raqamini yuboring:",
        reply_markup=back_kb(),
    )


@dp.message(StateFilter(AdminFlow.add_admin_id))
async def admin_add_admin_input(message: Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        await state.set_state(None)
        return
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("❗️ Iltimos, faqat Telegram ID raqamini yuboring (masalan: 123456789):")
        return
    new_admin_id = int(text)
    if is_super_admin(new_admin_id):
        await message.answer("ℹ️ Bu foydalanuvchi allaqachon doimiy super-admin.")
    else:
        await db_add_admin(new_admin_id, message.from_user.id)
        await message.answer(f"✅ ID:{new_admin_id} endi admin sifatida qo'shildi.")
        await db_log_admin_action(message.from_user.id, "add_admin", f"ID:{new_admin_id}")
        await safe_send(
            new_admin_id,
            "🎉 Siz MHDV botida admin etib tayinlandingiz! Botga /start yuboring.",
        )
    await state.set_state(None)
    await show_admins_section(message, state)


@dp.message(F.text == ABTN_REMOVE_ADMIN)
@guard_super_admin
async def admin_remove_admin_btn(message: Message, state: FSMContext):
    await state.set_state(AdminFlow.remove_admin_id)
    await message.answer(
        "➖ Adminlikdan olib tashlamoqchi bo'lgan foydalanuvchining Telegram ID raqamini yuboring:",
        reply_markup=back_kb(),
    )


@dp.message(StateFilter(AdminFlow.remove_admin_id))
async def admin_remove_admin_input(message: Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        await state.set_state(None)
        return
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("❗️ Iltimos, faqat Telegram ID raqamini yuboring:")
        return
    target_id = int(text)
    if is_super_admin(target_id):
        await message.answer("⛔️ Doimiy super-adminni botdan olib tashlab bo'lmaydi.")
    else:
        await db_remove_admin(target_id)
        await message.answer(f"✅ ID:{target_id} adminlikdan olib tashlandi.")
        await db_log_admin_action(message.from_user.id, "remove_admin", f"ID:{target_id}")
        await safe_send(target_id, "ℹ️ Sizning admin huquqingiz olib tashlandi.")
    await state.set_state(None)
    await show_admins_section(message, state)


# ---------- 12) Logo o'yini ----------

@dp.message(F.text == ABTN_GAME)
@guard_admin
async def admin_game_btn(message: Message, state: FSMContext):
    await show_game_menu(message, state)


async def show_game_menu(message: Message, state: FSMContext):
    await state.update_data(admin_section="game_menu")
    started = await db_get_setting("game_started")
    enabled = await db_get_setting("game_enabled")
    count = await db_game_count()
    status_line = "🟢 Boshlangan (yangi ID berilmaydi)" if started == "1" else "⚪️ Boshlanmagan (ID olish mumkin)"
    onoff_line = "🟢 Yoqilgan" if enabled == "1" else "🔴 O'chirilgan"
    await message.answer(
        f"🎮 <b>Logo o'yini</b>\n\n"
        f"📊 Jami ID olganlar: {count}\n"
        f"▶️ O'yin holati: {status_line}\n"
        f"🔌 Ishlash holati: {onoff_line}",
        reply_markup=game_menu_admin_kb(),
        parse_mode="HTML",
    )


@dp.message(F.text == ABTN_GAME_STATS)
@guard_admin
async def admin_game_stats(message: Message, state: FSMContext):
    count = await db_game_count()
    await message.answer(f"📊 Hozirgacha <b>{count}</b> ta foydalanuvchi ID raqam olgan.", parse_mode="HTML")


@dp.message(F.text == ABTN_GAME_USERS)
@guard_admin
async def admin_game_users(message: Message, state: FSMContext):
    await show_game_users_list(message, state, 0)


async def show_game_users_list(message: Message, state: FSMContext, page: int):
    participants = await db_game_list_participants()
    labels = []
    for p in participants:
        user = await db_get_user(p["tg_id"])
        name = user_display_name(user) if user else str(p["tg_id"])
        win_icon = "🏆 " if p["is_winner"] else ""
        labels.append(f"{win_icon}🆔 #{p['game_number']} — {name}")
    await state.update_data(admin_section="game_users_list", list_page=page)
    if not labels:
        await message.answer("Hozircha hech kim ID raqam olmagan.", reply_markup=kb([[BTN_BACK]]))
        return
    await message.answer(
        f"👥 ID raqam olgan foydalanuvchilar (jami: {len(labels)}):",
        reply_markup=paginated_list_kb(labels, page),
    )


@dp.message(F.text == ABTN_GAME_TOGGLE)
@guard_admin
async def admin_game_toggle(message: Message, state: FSMContext):
    current = await db_get_setting("game_enabled")
    new_val = "0" if current == "1" else "1"
    await db_set_setting("game_enabled", new_val)
    await message.answer("✅ O'yin ishlash holati o'zgartirildi.")
    await show_game_menu(message, state)


@dp.message(F.text == ABTN_GAME_START)
@guard_admin
async def admin_game_start(message: Message, state: FSMContext):
    await db_set_setting("game_started", "1")
    await state.update_data(admin_section="game_active")
    await db_log_admin_action(message.from_user.id, "game_start", "")
    await message.answer(
        "🎮 O'yin boshlandi! Endi foydalanuvchilar yangi ID raqam ololmaydi.",
        reply_markup=game_active_kb(),
    )


@dp.message(F.text == ABTN_GAME_END)
@guard_admin
async def admin_game_end(message: Message, state: FSMContext):
    await db_set_setting("game_started", "0")
    await db_log_admin_action(message.from_user.id, "game_end", "")
    await message.answer("⏹ O'yin to'xtatildi. Endi foydalanuvchilar qayta ID raqam olishlari mumkin.")
    await show_game_menu(message, state)


@dp.message(F.text == ABTN_GAME_RESET)
@guard_admin
async def admin_game_reset(message: Message, state: FSMContext):
    await db_game_reset()
    await db_log_admin_action(message.from_user.id, "game_reset", "")
    await message.answer("✅ Barcha ID raqamlar o'chirildi. Hisoblagich 100ga tushirildi.")
    await show_game_menu(message, state)


@dp.message(F.text == ABTN_GAME_PICK)
@guard_admin
async def admin_game_pick_btn(message: Message, state: FSMContext):
    await show_game_pick_list(message, state, 0)


async def show_game_pick_list(message: Message, state: FSMContext, page: int):
    participants = await db_game_list_participants()
    if not participants:
        await message.answer("Hozircha hech kim ID raqam olmagan.", reply_markup=game_active_kb())
        return
    labels = []
    for p in participants:
        user = await db_get_user(p["tg_id"])
        name = user_display_name(user) if user else str(p["tg_id"])
        win_icon = "🏆 " if p["is_winner"] else ""
        labels.append(f"{win_icon}🆔 #{p['game_number']} — {name}")
    await state.update_data(admin_section="game_pick_list", list_page=page)
    await message.answer("🎯 G'olibni tanlang:", reply_markup=paginated_list_kb(labels, page))


# =============================================================================
# 8.5. TELEGRAM MINI-APP AMALLARI
# =============================================================================

@dp.message(F.web_app_data)
async def miniapp_web_data(message: Message, state: FSMContext):
    """Mini App tugmalarini botning mavjud oqimlariga bog'laydi."""
    if await db_is_blocked(message.from_user.id):
        return
    try:
        payload = json.loads(message.web_app_data.data or "{}")
    except (json.JSONDecodeError, TypeError):
        payload = {}
    action = str(payload.get("action", "")).strip()
    actions = {
        "order_website": btn_website,
        "order_logo": btn_logo,
        "order_bot": btn_bot,
        "order_ai": btn_ai_image,
        "my_orders": btn_my_orders,
        "faq": btn_faq,
        "social": btn_social,
        "admin_chat": btn_admin_chat,
        "game": btn_game,
        "feedback": btn_feedback,
        "info": btn_info,
    }
    fn = actions.get(action)
    if fn:
        await fn(message, state)
    else:
        await message.answer("Mini-ilovadan noma'lum amal keldi. /start orqali qayta urinib ko'ring.")


# =============================================================================
# 9. MIDDLEWARE: bloklangan foydalanuvchilar, texnik ishlar rejimi, flood himoyasi
# =============================================================================

FLOOD_WINDOW_SECONDS = 4
FLOOD_MAX_MESSAGES = 6
FLOOD_COOLDOWN_SECONDS = 15
_flood_tracker = {}  # tg_id -> {"timestamps": [...], "warned_until": ts}


def _check_flood(tg_id: int) -> str:
    """'block' — jim bloklash, 'warn' — bir marta ogohlantirish, '' — muammo yo'q."""
    now_ts = datetime.datetime.now().timestamp()
    entry = _flood_tracker.setdefault(tg_id, {"timestamps": [], "warned_until": 0})
    if now_ts < entry["warned_until"]:
        return "block"
    entry["timestamps"] = [t for t in entry["timestamps"] if now_ts - t <= FLOOD_WINDOW_SECONDS]
    entry["timestamps"].append(now_ts)
    if len(entry["timestamps"]) > FLOOD_MAX_MESSAGES:
        entry["warned_until"] = now_ts + FLOOD_COOLDOWN_SECONDS
        entry["timestamps"] = []
        return "warn"
    return ""


@dp.message.outer_middleware()
async def maintenance_and_block_middleware(handler, event: Message, data):
    tg_id = event.from_user.id if event.from_user else None
    if tg_id is None or await is_admin(tg_id):
        return await handler(event, data)

    flood = _check_flood(tg_id)
    if flood == "block":
        return
    if flood == "warn":
        await event.answer("⚠️ Juda tez-tez xabar yubordingiz. Iltimos, bir necha soniya kutib turing.")
        return

    if await db_is_blocked(tg_id):
        if event.text == "/start":
            await event.answer("⛔️ Siz botdan foydalanish huquqidan mahrum qilingansiz.")
        return

    if event.text != "/start" and await db_needs_restart(tg_id):
        await event.answer(
            "🔄 Bot yangilandi. Davom etish uchun quyidagi tugmani bosing yoki /start buyrug'ini yuboring:",
            reply_markup=force_start_inline_kb(),
        )
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
    if not await is_admin(message.from_user.id):
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
            await show_order_view(message, state, oid, return_section="orders_list")
            return
    elif section == "user_orders_list":
        oid = extract_hash_id(text)
        if oid:
            await show_order_view(message, state, oid, return_section="user_orders_list")
            return
    elif section == "payments_list":
        pid = extract_hash_id(text)
        if pid:
            await show_payment_view(message, state, pid)
            return
    elif section == "game_pick_list":
        number = extract_hash_id(text)
        if number is not None:
            participant = await db_game_get_by_number(number)
            if participant:
                await db_game_set_winner(number)
                await safe_send(
                    participant["tg_id"],
                    "🎉 <b>Tabriklaymiz, siz yutdingiz!</b>\n\nSiz bilan tez orada admin bog'lanadi.",
                    parse_mode="HTML",
                )
                await message.answer(
                    f"✅ ID:#{number} g'olib deb belgilandi va foydalanuvchiga xabar yuborildi.",
                    parse_mode="HTML",
                )
                await show_game_pick_list(message, state, data.get("list_page", 0))
            else:
                await message.answer("Topilmadi. Ro'yxatdan tanlang.")
            return

    await message.answer("🤔 Noma'lum buyruq. Iltimos, menyudan tanlang.",
                          reply_markup=admin_menu_kb(await get_admin_role(message.from_user.id)))
    await state.update_data(admin_section="main")


@dp.message()
async def fallback_non_text(message: Message, state: FSMContext):
    await message.answer("🤔 Iltimos, menyudagi tugmalardan foydalaning.")


# =============================================================================
# 11. AVTOMATIK ESLATMALAR (fon vazifasi)
# =============================================================================

REMINDER_CHECK_INTERVAL = 15 * 60  # har 15 daqiqada tekshiradi


async def reminder_loop():
    while True:
        try:
            enabled = await db_get_setting("reminders_enabled")
            if enabled == "1":
                hours_raw = await db_get_setting("reminder_hours") or "2"
                hours = int(hours_raw) if hours_raw.isdigit() else 2

                # 1) Uzoq vaqt ko'rib chiqilmagan (pending) buyurtmalar haqida adminlarga eslatma
                stale_orders = await db_get_stale_pending_orders(hours)
                for o in stale_orders:
                    title = ORDER_TYPE_TITLES.get(o["order_type"], o["order_type"])
                    await notify_admins(
                        f"⏰ <b>Eslatma:</b> #{o['id']} ({title}) buyurtmasi {hours} soatdan "
                        f"beri ko'rib chiqilmagan!\nAdmin panel → {ABTN_ORDERS} orqali ko'ring."
                    )
                    await db_mark_order_admin_reminded(o["id"])

                # 2) Qabul qilingan, ammo hali to'lov cheki yuborilmagan foydalanuvchilarga eslatma
                unpaid_orders = await db_get_stale_unpaid_orders(hours)
                for o in unpaid_orders:
                    await safe_send(
                        o["tg_id"],
                        "⏰ Eslatma: buyurtmangiz qabul qilingan, lekin hali to'lov chekini "
                        "yubormagansiz. Iltimos, to'lovni amalga oshirib, chek rasmini yuboring.",
                    )
                    await db_mark_order_payment_reminded(o["id"])

                # 3) Uzoq vaqt tasdiqlanmagan (pending) to'lovlar haqida adminlarga eslatma
                stale_payments = await db_get_stale_pending_payments(hours)
                for p in stale_payments:
                    await notify_admins(
                        f"⏰ <b>Eslatma:</b> #{p['id']} to'lovi {hours} soatdan beri "
                        f"tasdiqlanmagan!\nAdmin panel → {ABTN_PAYMENTS} orqali ko'ring."
                    )
                    await db_mark_payment_admin_reminded(p["id"])

            # 4) Buyurtma so'rovnomasini boshlab, yakunlamagan foydalanuvchilarga eslatma
            abandon_minutes_raw = await db_get_setting("abandoned_flow_minutes") or "45"
            abandon_minutes = int(abandon_minutes_raw) if abandon_minutes_raw.isdigit() else 45
            stale_flows = await db_get_stale_flows(abandon_minutes)
            for f in stale_flows:
                await safe_send(
                    f["tg_id"],
                    "⏳ Siz boshlagan buyurtma so'rovnomasini yakunlamadingiz.\n\n"
                    "Davom ettirish uchun oxirgi savolga javob yozing, yoki qaytadan "
                    "boshlash uchun /start buyrug'ini yuboring.",
                )
                await db_mark_flow_reminded(f["tg_id"])

            # 5) Kechiktirilgan (rejalashtirilgan) xabarlarni yuborish
            due_broadcasts = await db_get_due_broadcasts()
            for b in due_broadcasts:
                if b["segment"] == "ordered":
                    target_users = await db_get_users_with_orders()
                else:
                    target_users = await db_get_all_users()
                for u in target_users:
                    if u["is_blocked"]:
                        continue
                    await safe_send(u["tg_id"], f"📢 <b>E'lon:</b>\n\n{b['text']}", parse_mode="HTML")
                    await asyncio.sleep(0.05)
                await db_mark_broadcast_sent(b["id"])
        except Exception as e:
            logger.warning(f"Eslatmalar tsiklida xatolik: {e}")

        await asyncio.sleep(REMINDER_CHECK_INTERVAL)


# =============================================================================
# 12. RENDER UCHUN AIOHTTP HEALTH-CHECK SERVER + WEB BOSHQARUV PANELI + POLLING
# =============================================================================

async def health(request):
    return web.Response(text="MHDV bot ishlayapti ✅")


async def _miniapp_user_from_request(request):
    # Telegram WebApp initData ni tekshiradi va Telegram foydalanuvchisini qaytaradi.
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    if not init_data and request.method != "GET":
        # multipart/json klientlar uchun header asosiy yo'l; query fallback faqat qulaylik uchun.
        init_data = request.query.get("initData", "")
    if not init_data:
        return None
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", "")
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{k}={parsed[k]}" for k in sorted(parsed))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated_hash, received_hash):
            return None
        auth_date = int(parsed.get("auth_date", "0") or 0)
        if auth_date and abs(int(datetime.datetime.now().timestamp()) - auth_date) > 86400:
            return None
        user = json.loads(parsed.get("user", "{}"))
        if not user.get("id"):
            return None
        return user
    except Exception:
        return None


def _json_response(data, status=200):
    return web.json_response(data, status=status, dumps=lambda obj: json.dumps(obj, ensure_ascii=False))


async def _require_miniapp_user(request):
    user = await _miniapp_user_from_request(request)
    if not user:
        return None, _json_response({"ok": False, "error": "Telegram orqali Mini App ni oching."}, 401)
    tg_id = int(user["id"])
    await db_add_user(tg_id, " ".join(x for x in [user.get("first_name", ""), user.get("last_name", "")] if x).strip(), user.get("username"))
    if await db_is_blocked(tg_id) and not await is_admin(tg_id):
        return None, _json_response({"ok": False, "error": "Siz botdan foydalanish huquqidan mahrum qilingansiz."}, 403)
    return user, None


async def miniapp_api_me(request):
    user, err = await _require_miniapp_user(request)
    if err:
        return err
    tg_id = int(user["id"])
    db_user = await db_get_user(tg_id)
    orders = await db_get_orders_by_user(tg_id)
    messages = await db_get_messages(tg_id, 60)
    participant = await db_game_get_participant(tg_id)
    avg_rating, rating_count = await db_avg_rating()
    settings_keys = [
        "website_info", "logo_info", "bot_service_info", "bot_info", "mhdv_info",
        "card_number", "card_owner", "social_links", "faq", "admin_contact_name",
        "admin_contact_phone", "game_info", "game_enabled", "game_started"
    ]
    settings = {k: await db_get_setting(k) for k in settings_keys}
    order_items, payments = [], []
    for o in orders:
        try:
            od = json.loads(o["data"])
        except Exception:
            od = {}
        pay = await db_get_payment_by_order(o["id"])
        orating = await db_get_order_rating(o["id"])
        item = {
            "id": o["id"], "type": o["order_type"],
            "title": ORDER_TYPE_TITLES.get(o["order_type"], o["order_type"]),
            "data": od, "status": o["status"], "stage": o["stage"],
            "reject_reason": o["reject_reason"], "revision_note": o["revision_note"],
            "created_at": o["created_at"], "rating": (orating["rating"] if orating else None),
            "payment": ({"id": pay["id"], "status": pay["status"], "file_type": pay["file_type"], "created_at": pay["created_at"]} if pay else None),
        }
        order_items.append(item)
        if pay:
            payments.append({"id": pay["id"], "order_id": o["id"], "status": pay["status"], "file_type": pay["file_type"], "created_at": pay["created_at"]})

    feedback_rows = await db_get_feedback_list()
    my_feedback = [
        {"id": f["id"], "kind": f["kind"], "text": f["text"], "admin_reply": f["admin_reply"], "created_at": f["created_at"]}
        for f in feedback_rows if int(f["tg_id"]) == tg_id
    ][:20]
    stats = {
        "orders": len(order_items),
        "pending": sum(1 for o in order_items if o["status"] == "pending"),
        "accepted": sum(1 for o in order_items if o["status"] == "accepted"),
        "completed": sum(1 for o in order_items if o.get("stage") == "topshirildi"),
        "payments": len(payments),
    }
    return _json_response({
        "ok": True,
        "user": {"tg_id": tg_id, "full_name": db_user["full_name"] if db_user else user.get("first_name", ""), "username": db_user["username"] if db_user else user.get("username"), "created_at": db_user["created_at"] if db_user else "", "is_admin": await is_admin(tg_id), "role": await get_admin_role(tg_id)},
        "stats": stats, "orders": order_items, "payments": payments,
        "messages": [{"direction": m["direction"], "text": m["text"], "created_at": m["created_at"]} for m in messages],
        "feedback": my_feedback,
        "game": ({"number": participant["game_number"], "winner": bool(participant["is_winner"])} if participant else None),
        "settings": settings, "rating": {"average": avg_rating, "count": rating_count},
    })


async def miniapp_api_order(request):
    user, err = await _require_miniapp_user(request)
    if err: return err
    try: payload = await request.json()
    except Exception: return _json_response({"ok": False, "error": "Noto'g'ri ma'lumot."}, 400)
    order_type = str(payload.get("type", ""))
    schemas = {
        "website": ["site_name", "purpose", "sphere", "budget", "requirements", "additional"],
        "logo": ["brand_name", "direction", "budget", "additional"],
        "bot": ["bot_name", "purpose", "platform", "budget", "requirements", "additional"],
        "ai_image": ["topic", "style", "size", "budget", "additional"],
    }
    if order_type not in schemas: return _json_response({"ok": False, "error": "Xizmat turi noto'g'ri."}, 400)
    data = {k: str(payload.get(k, "")).strip() for k in schemas[order_type]}
    required = [k for k in schemas[order_type] if k != "additional"]
    if any(not data[k] for k in required): return _json_response({"ok": False, "error": "Majburiy maydonlarni to'ldiring."}, 400)
    data["additional"] = data.get("additional") or "—"
    order_id = await db_create_order(int(user["id"]), order_type, data)
    dbu = await db_get_user(int(user["id"]))
    await notify_admins(f"🆕 <b>Mini App orqali yangi buyurtma #{order_id}</b>\n👤 {user_display_name(dbu)}\n\n{format_order_body(order_type, data)}")
    return _json_response({"ok": True, "order_id": order_id})


async def miniapp_api_revision(request):
    user, err = await _require_miniapp_user(request)
    if err: return err
    oid = int(request.match_info["order_id"])
    order = await db_get_order(oid)
    if not order or order["tg_id"] != int(user["id"]): return _json_response({"ok": False, "error": "Buyurtma topilmadi."}, 404)
    try: payload = await request.json()
    except Exception: payload = {}
    note = str(payload.get("note", "")).strip()
    if not note: return _json_response({"ok": False, "error": "Tuzatish matnini yozing."}, 400)
    await db_set_order_revision(oid, note)
    await db_set_order_stage(oid, "ishlanmoqda")
    await notify_admins(f"✏️ <b>Mini App: tuzatish so'rovi</b> — #{oid}\n👤 ID:{user['id']}\n\n📝 {note}")
    return _json_response({"ok": True})


async def miniapp_api_order_rating(request):
    user, err = await _require_miniapp_user(request)
    if err: return err
    oid = int(request.match_info["order_id"])
    order = await db_get_order(oid)
    if not order or order["tg_id"] != int(user["id"]): return _json_response({"ok": False, "error": "Buyurtma topilmadi."}, 404)
    try: payload = await request.json(); score = int(payload.get("rating", 0))
    except Exception: score = 0
    if score not in range(1, 6): return _json_response({"ok": False, "error": "Baho 1–5 bo'lishi kerak."}, 400)
    await db_add_order_rating(oid, int(user["id"]), score)
    return _json_response({"ok": True})


async def miniapp_api_feedback(request):
    user, err = await _require_miniapp_user(request)
    if err: return err
    try: payload = await request.json()
    except Exception: payload = {}
    kind = str(payload.get("kind", "taklif")); text = str(payload.get("text", "")).strip()
    if kind not in ("taklif", "shikoyat"): kind = "taklif"
    if not text: return _json_response({"ok": False, "error": "Matn yozing."}, 400)
    fid = await db_add_feedback(int(user["id"]), kind, text)
    await notify_admins(f"📝 <b>Mini App: yangi {kind}</b> (#{fid})\n👤 ID:{user['id']}\n\n{text}")
    return _json_response({"ok": True, "id": fid})


async def miniapp_api_chat(request):
    user, err = await _require_miniapp_user(request)
    if err: return err
    try: payload = await request.json()
    except Exception: payload = {}
    text = str(payload.get("text", "")).strip()
    if not text: return _json_response({"ok": False, "error": "Xabar yozing."}, 400)
    await db_add_message(int(user["id"]), "in", text)
    dbu = await db_get_user(int(user["id"]))
    await notify_admins(f"✉️ <b>Mini App xabari</b>\n👤 {user_display_name(dbu)}\n\n{text}\n\nJavob: web/admin panel → Chatlar")
    return _json_response({"ok": True})


async def miniapp_api_rate(request):
    user, err = await _require_miniapp_user(request)
    if err: return err
    try: payload = await request.json(); score = int(payload.get("score", 0))
    except Exception: score = 0
    if score not in range(1, 6): return _json_response({"ok": False, "error": "1–5 yulduz tanlang."}, 400)
    await db_add_rating(int(user["id"]), score)
    return _json_response({"ok": True})


async def miniapp_api_game_join(request):
    user, err = await _require_miniapp_user(request)
    if err: return err
    tg_id = int(user["id"])
    if await db_get_setting("game_enabled") != "1": return _json_response({"ok": False, "error": "O'yin hozir o'chirilgan."}, 400)
    existing = await db_game_get_participant(tg_id)
    if existing: return _json_response({"ok": True, "number": existing["game_number"], "winner": bool(existing["is_winner"])})
    if await db_get_setting("game_started") == "1": return _json_response({"ok": False, "error": "Tanlov boshlangan, yangi ID berish to'xtatilgan."}, 400)
    try: number = await db_game_assign_number(tg_id)
    except Exception: return _json_response({"ok": False, "error": "ID olishda xatolik. Qayta urinib ko'ring."}, 500)
    return _json_response({"ok": True, "number": number, "winner": False})


async def miniapp_api_order_cancel(request):
    user, err = await _require_miniapp_user(request)
    if err:
        return err
    try:
        oid = int(request.match_info["order_id"])
    except Exception:
        return _json_response({"ok": False, "error": "Buyurtma ID noto'g'ri."}, 400)
    order = await db_get_order(oid)
    if not order or int(order["tg_id"]) != int(user["id"]):
        return _json_response({"ok": False, "error": "Buyurtma topilmadi."}, 404)
    if order["status"] != "pending":
        return _json_response({"ok": False, "error": "Faqat kutilayotgan buyurtmani bekor qilish mumkin."}, 400)
    await db_set_order_status(oid, "rejected", "Foydalanuvchi Mini App orqali bekor qildi")
    await notify_admins(f"🚫 Mini App: buyurtma #{oid} foydalanuvchi tomonidan bekor qilindi.\n👤 ID:{user['id']}")
    return _json_response({"ok": True})


async def miniapp_api_payment(request):
    user, err = await _require_miniapp_user(request)
    if err: return err
    reader = await request.multipart()
    order_id = None; file_bytes = None; filename = "receipt.jpg"; content_type = "application/octet-stream"
    async for part in reader:
        if part.name == "order_id":
            try: order_id = int((await part.text()).strip())
            except Exception: order_id = None
        elif part.name == "file":
            filename = part.filename or filename; content_type = part.headers.get("Content-Type", content_type)
            chunks=[]; total=0
            while True:
                chunk=await part.read_chunk()
                if not chunk: break
                total += len(chunk)
                if total > 10*1024*1024: return _json_response({"ok": False, "error": "Fayl 10 MB dan katta."}, 400)
                chunks.append(chunk)
            file_bytes=b"".join(chunks)
    order = await db_get_order(order_id) if order_id else None
    if not order or order["tg_id"] != int(user["id"]) or order["status"] != "accepted": return _json_response({"ok": False, "error": "To'lov uchun mos buyurtma topilmadi."}, 400)
    if await db_get_payment_by_order(order_id): return _json_response({"ok": False, "error": "Bu buyurtma uchun chek allaqachon yuborilgan."}, 400)
    if not file_bytes: return _json_response({"ok": False, "error": "Chek faylini tanlang."}, 400)
    sent = None; file_type = "photo" if content_type.startswith("image/") else "document"
    caption=f"💳 Mini App to'lov cheki — buyurtma #{order_id}\n👤 ID:{user['id']}"
    for aid in ADMIN_IDS:
        try:
            if file_type == "photo": sent = await bot.send_photo(aid, BufferedInputFile(file_bytes, filename=filename), caption=caption)
            else: sent = await bot.send_document(aid, BufferedInputFile(file_bytes, filename=filename), caption=caption)
            if sent: break
        except Exception: continue
    if not sent: return _json_response({"ok": False, "error": "Chekni adminga yuborib bo'lmadi. Bot orqali yuboring."}, 500)
    file_id = sent.photo[-1].file_id if sent.photo else sent.document.file_id
    pid = await db_add_payment(order_id, int(user["id"]), file_id, file_type)
    return _json_response({"ok": True, "payment_id": pid})

async def _require_miniapp_admin(request):
    user, err = await _require_miniapp_user(request)
    if err:
        return None, err
    if not await is_admin(int(user["id"])):
        return None, _json_response({"ok": False, "error": "Bu bo'lim faqat admin uchun."}, 403)
    return user, None


async def miniapp_api_admin_overview(request):
    admin, err = await _require_miniapp_admin(request)
    if err: return err
    users = await db_get_all_users()
    payments = await db_get_payments()
    feedback = await db_get_feedback_list()
    chat_users = await db_get_chat_users()
    admins = await db_get_admins()
    logs = await db_get_admin_logs(30)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 120")
        orders = await cur.fetchall()
    order_items=[]
    for o in orders:
        try: od=json.loads(o["data"])
        except Exception: od={}
        u=await db_get_user(o["tg_id"])
        order_items.append({"id":o["id"],"tg_id":o["tg_id"],"user":user_display_name(u) if u else str(o["tg_id"]),"type":o["order_type"],"title":ORDER_TYPE_TITLES.get(o["order_type"],o["order_type"]),"data":od,"status":o["status"],"stage":o["stage"],"reject_reason":o["reject_reason"],"revision_note":o["revision_note"],"created_at":o["created_at"]})
    pay_items=[]
    for x in payments[:100]:
        u=await db_get_user(x["tg_id"])
        pay_items.append({"id":x["id"],"order_id":x["order_id"],"tg_id":x["tg_id"],"user":user_display_name(u) if u else str(x["tg_id"]),"status":x["status"],"file_type":x["file_type"],"created_at":x["created_at"]})
    user_items=[{"tg_id":u["tg_id"],"full_name":u["full_name"],"username":u["username"],"blocked":bool(u["is_blocked"]),"created_at":u["created_at"]} for u in users[:150]]
    fb_items=[]
    for f in feedback[:80]:
        u=await db_get_user(f["tg_id"])
        fb_items.append({"id":f["id"],"tg_id":f["tg_id"],"user":user_display_name(u) if u else str(f["tg_id"]),"kind":f["kind"],"text":f["text"],"admin_reply":f["admin_reply"],"created_at":f["created_at"]})
    chats=[]
    for c in chat_users[:80]:
        u=await db_get_user(c["tg_id"])
        msgs=await db_get_messages(c["tg_id"], 8)
        chats.append({"tg_id":c["tg_id"],"user":user_display_name(u) if u else str(c["tg_id"]),"last_time":c["last_time"],"messages":[{"direction":m["direction"],"text":m["text"],"created_at":m["created_at"]} for m in msgs]})
    return _json_response({"ok":True,"stats":{"users":len(users),"blocked":sum(1 for u in users if u["is_blocked"]),"orders":await db_orders_count_total(),"pending":sum(1 for o in order_items if o["status"]=="pending"),"payments_pending":sum(1 for x in payments if x["status"]=="pending"),"feedback_unanswered":sum(1 for f in feedback if not f["admin_reply"]),"game":await db_game_count()},"orders":order_items,"payments":pay_items,"users":user_items,"feedback":fb_items,"chats":chats,"admins":[{"tg_id":a["tg_id"],"role":a["role"],"created_at":a["created_at"]} for a in admins],"logs":[{"admin_id":l["admin_id"],"action":l["action"],"details":l["details"],"created_at":l["created_at"]} for l in logs]})


async def miniapp_api_admin_order_action(request):
    admin, err = await _require_miniapp_admin(request)
    if err: return err
    oid=int(request.match_info["order_id"]); o=await db_get_order(oid)
    if not o: return _json_response({"ok":False,"error":"Buyurtma topilmadi."},404)
    try: payload=await request.json()
    except Exception: payload={}
    action=str(payload.get("action", "")); reason=str(payload.get("reason","")).strip()
    if action=="accept":
        await db_set_order_status(oid,"accepted")
        card_num=await db_get_setting("card_number"); card_owner=await db_get_setting("card_owner")
        await safe_send(o["tg_id"],f"✅ Buyurtmangiz qabul qilindi!\n\n💳 Karta: {card_num}\n👤 Egasi: {card_owner}\n\nTo'lovdan keyin chek yuboring.")
    elif action=="reject":
        if not reason: reason="Sabab ko'rsatilmagan"
        await db_set_order_status(oid,"rejected",reason); await safe_send(o["tg_id"],f"❌ Buyurtmangiz rad etildi.\n\nSabab: {reason}")
    elif action in ("ishlanmoqda","tayyor","topshirildi"):
        await db_set_order_stage(oid,action)
        msg={"ishlanmoqda":"🔧 Buyurtmangiz ustida ish boshlandi.","tayyor":"🚀 Buyurtmangiz tayyor holatga keldi.","topshirildi":"📬 Buyurtmangiz topshirildi. Rahmat!"}[action]
        await safe_send(o["tg_id"],msg)
    else: return _json_response({"ok":False,"error":"Noto'g'ri amal."},400)
    await db_log_admin_action(int(admin["id"]),"miniapp_order_action",f"order #{oid} -> {action}")
    return _json_response({"ok":True})


async def miniapp_api_admin_payment_confirm(request):
    admin, err = await _require_miniapp_admin(request)
    if err: return err
    pid=int(request.match_info["payment_id"]); p=await db_get_payment(pid)
    if not p: return _json_response({"ok":False,"error":"To'lov topilmadi."},404)
    await db_confirm_payment(pid)
    if p["order_id"]: await db_set_order_stage(p["order_id"],"ishlanmoqda")
    await safe_send(p["tg_id"],"✅ To'lovingiz tasdiqlandi! Buyurtmangiz ustida ish boshlandi.")
    await db_log_admin_action(int(admin["id"]),"miniapp_payment_confirm",f"payment #{pid}")
    return _json_response({"ok":True})


async def miniapp_api_admin_user_toggle(request):
    admin, err = await _require_miniapp_admin(request)
    if err: return err
    tg_id=int(request.match_info["tg_id"])
    if await is_admin(tg_id): return _json_response({"ok":False,"error":"Adminni bloklab bo'lmaydi."},400)
    state=await db_toggle_block(tg_id)
    if state is None: return _json_response({"ok":False,"error":"Foydalanuvchi topilmadi."},404)
    await safe_send(tg_id,"⛔️ Hisobingiz vaqtincha bloklandi." if state else "✅ Hisobingiz blokdan chiqarildi.")
    await db_log_admin_action(int(admin["id"]),"miniapp_user_toggle",f"ID:{tg_id} blocked={state}")
    return _json_response({"ok":True,"blocked":state})


async def miniapp_api_admin_chat_reply(request):
    admin, err = await _require_miniapp_admin(request)
    if err: return err
    tg_id=int(request.match_info["tg_id"])
    try: payload=await request.json()
    except Exception: payload={}
    text=str(payload.get("text","")).strip()
    if not text: return _json_response({"ok":False,"error":"Xabar yozing."},400)
    ok=await safe_send(tg_id,f"👨‍💼 Admin:\n\n{text}")
    if ok: await db_add_message(tg_id,"out",text)
    await db_log_admin_action(int(admin["id"]),"miniapp_chat_reply",f"to ID:{tg_id}")
    return _json_response({"ok":ok})


async def miniapp_api_admin_feedback_reply(request):
    admin, err = await _require_miniapp_admin(request)
    if err: return err
    fid=int(request.match_info["feedback_id"]); f=await db_get_feedback(fid)
    if not f: return _json_response({"ok":False,"error":"Murojaat topilmadi."},404)
    try: payload=await request.json()
    except Exception: payload={}
    text=str(payload.get("text","")).strip()
    if not text: return _json_response({"ok":False,"error":"Javob yozing."},400)
    await db_set_feedback_reply(fid,text); await safe_send(f["tg_id"],f"💬 Admin javobi:\n\n{text}")
    await db_log_admin_action(int(admin["id"]),"miniapp_feedback_reply",f"feedback #{fid}")
    return _json_response({"ok":True})


async def miniapp_api_admin_broadcast(request):
    admin, err = await _require_miniapp_admin(request)
    if err: return err
    try: payload=await request.json()
    except Exception: payload={}
    text=str(payload.get("text","")).strip(); segment=str(payload.get("segment","all"))
    if not text: return _json_response({"ok":False,"error":"Xabar matnini yozing."},400)
    users=await db_get_users_with_orders() if segment=="ordered" else await db_get_all_users()
    sent=0
    for u in users:
        if u["is_blocked"]: continue
        if await safe_send(u["tg_id"],f"📢 E'lon:\n\n{text}"): sent+=1
        await asyncio.sleep(0.03)
    await db_log_admin_action(int(admin["id"]),"miniapp_broadcast",f"segment={segment}, sent={sent}")
    return _json_response({"ok":True,"sent":sent})


async def miniapp(request):
    html_out = '<!doctype html>\n<html lang="uz">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">\n<title>MHDV</title>\n<script src="https://telegram.org/js/telegram-web-app.js"></script>\n<style>\n:root{--bg:#f4f6f9;--surface:#fff;--surface2:#f8fafc;--text:#101828;--muted:#667085;--line:#e7ebf0;--accent:#2563eb;--accent2:#7c3aed;--good:#079455;--warn:#dc6803;--bad:#d92d20;--shadow:0 16px 50px rgba(16,24,40,.12);--nav-h:74px}\n*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}html,body{margin:0;min-height:100%;font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--bg);color:var(--text)}body{padding-bottom:calc(var(--nav-h) + env(safe-area-inset-bottom) + 18px)}button,input,textarea,select{font:inherit}button{cursor:pointer}.app{max-width:760px;margin:0 auto;padding:14px 14px 10px}.topbar{display:flex;align-items:center;gap:11px;position:sticky;top:0;z-index:12;padding:8px 0 10px;background:linear-gradient(var(--bg) 70%,rgba(244,246,249,0))}.avatar{width:46px;height:46px;border-radius:15px;background:linear-gradient(145deg,#111827,#334155);color:#fff;display:grid;place-items:center;font-weight:900;font-size:18px;box-shadow:0 8px 20px rgba(15,23,42,.18)}.identity{flex:1;min-width:0}.identity strong{font-size:17px;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.sub{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.icon-btn{width:42px;height:42px;border:1px solid var(--line);border-radius:14px;background:var(--surface);display:grid;place-items:center;color:var(--text)}.icon-btn svg{width:20px;height:20px}.hero{position:relative;overflow:hidden;border-radius:26px;padding:21px;background:linear-gradient(135deg,#101828 0%,#1d2939 58%,#344054 100%);color:white;box-shadow:0 14px 30px rgba(16,24,40,.18)}.hero:after{content:"";position:absolute;width:170px;height:170px;border-radius:50%;right:-60px;top:-70px;background:linear-gradient(145deg,rgba(255,255,255,.14),rgba(255,255,255,0))}.hero .eyebrow{font-size:12px;opacity:.74;font-weight:700}.hero h1{font-size:25px;line-height:1.1;margin:7px 0 9px;max-width:390px}.hero p{margin:0;opacity:.78;font-size:13px;line-height:1.5;max-width:470px}.hero-actions{display:flex;gap:8px;margin-top:17px;flex-wrap:wrap}.pillbtn{border:0;border-radius:13px;padding:10px 13px;font-weight:800;background:#fff;color:#111827}.pillbtn.ghost{background:rgba(255,255,255,.11);color:#fff;border:1px solid rgba(255,255,255,.13)}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:12px 0}.stat{background:var(--surface);border:1px solid var(--line);border-radius:17px;padding:12px 9px;min-width:0}.stat strong{display:block;font-size:20px;line-height:1.2}.stat span{display:block;font-size:10px;color:var(--muted);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.section-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:20px 2px 9px}.section-head h2{font-size:16px;margin:0}.link-btn{border:0;background:transparent;color:var(--accent);font-weight:800;font-size:12px}.service-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.service-card{border:1px solid var(--line);background:var(--surface);border-radius:20px;padding:14px;text-align:left;min-height:126px;position:relative;overflow:hidden}.service-icon{width:42px;height:42px;border-radius:14px;display:grid;place-items:center;margin-bottom:14px}.service-icon svg{width:24px;height:24px}.svc-web{background:#eef4ff;color:#2563eb}.svc-logo{background:#f4ebff;color:#7c3aed}.svc-bot{background:#ecfdf3;color:#079455}.svc-ai{background:#fff6ed;color:#dc6803}.service-card strong{font-size:15px}.service-card p{font-size:11px;color:var(--muted);margin:5px 0 0;line-height:1.4}.service-card .arrow{position:absolute;right:12px;top:12px;color:#98a2b3}.quick-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}.quick{border:1px solid var(--line);background:var(--surface);border-radius:17px;padding:12px 7px;min-height:82px;text-align:center}.quick svg{width:22px;height:22px;margin:2px auto 8px;display:block;color:#344054}.quick span{font-size:10.5px;font-weight:800;display:block}.page{display:none;animation:fade .16s ease}.page.active{display:block}@keyframes fade{from{opacity:.25;transform:translateY(4px)}to{opacity:1;transform:none}}.card,.row{background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:14px;margin-bottom:9px}.row-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}.badge{padding:5px 8px;border-radius:999px;font-size:10px;font-weight:850;background:#f2f4f7;color:#475467;white-space:nowrap}.badge.good{background:#ecfdf3;color:#067647}.badge.warn{background:#fffaeb;color:#b54708}.badge.bad{background:#fef3f2;color:#b42318}.badge.info{background:#eff8ff;color:#175cd3}.order-title{font-size:14px;font-weight:850}.order-meta{font-size:11px;color:var(--muted);margin-top:3px}.progress{height:7px;background:#eef2f6;border-radius:999px;margin:12px 0 5px;overflow:hidden}.progress span{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,var(--accent),var(--accent2))}.stage-line{display:flex;justify-content:space-between;color:var(--muted);font-size:9px}.order-data{margin-top:10px;padding:10px;border-radius:13px;background:var(--surface2);font-size:11px;line-height:1.55;color:#475467}.actions{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px}.btn{border:0;border-radius:12px;padding:10px 12px;font-weight:800;background:#eef2f6;color:#344054}.btn.primary{background:var(--accent);color:#fff}.btn.danger{background:#fef3f2;color:#b42318}.btn.good{background:#ecfdf3;color:#067647}.btn.full{width:100%}.search{position:relative;margin-bottom:9px}.search svg{position:absolute;width:18px;height:18px;left:13px;top:50%;transform:translateY(-50%);color:#98a2b3}.search input{padding-left:40px;margin:0}.filters{display:flex;gap:7px;overflow:auto;padding:2px 0 10px;scrollbar-width:none}.filters::-webkit-scrollbar{display:none}.chip{white-space:nowrap;border:1px solid var(--line);background:var(--surface);border-radius:999px;padding:8px 11px;font-size:11px;font-weight:800;color:#475467}.chip.active{background:#101828;color:#fff;border-color:#101828}.empty{padding:28px 12px;text-align:center;color:var(--muted);font-size:13px}.empty svg{width:34px;height:34px;margin:0 auto 8px;display:block;color:#98a2b3}.chat-list{max-height:390px;overflow:auto;padding-right:2px}.bubble{max-width:84%;margin:7px 0;background:var(--surface);border:1px solid var(--line);border-radius:16px 16px 16px 5px;padding:10px 11px;font-size:13px;line-height:1.45}.bubble.out{margin-left:auto;background:#eaf2ff;border-color:#d8e7ff;border-radius:16px 16px 5px 16px}.bubble time{display:block;font-size:9px;color:var(--muted);margin-top:4px}.composer{display:flex;gap:8px;align-items:flex-end}.composer textarea{min-height:48px;max-height:110px;margin:0}.composer .btn{width:48px;height:48px;padding:0;display:grid;place-items:center}.composer .btn svg{width:20px;height:20px}.tabs{display:flex;padding:4px;background:#e9edf2;border-radius:15px;margin-bottom:10px}.tab{flex:1;border:0;background:transparent;border-radius:11px;padding:9px;font-size:11px;font-weight:850;color:#667085}.tab.active{background:var(--surface);color:#101828;box-shadow:0 2px 8px rgba(16,24,40,.07)}input,textarea,select{width:100%;border:1px solid var(--line);background:var(--surface);color:var(--text);border-radius:14px;padding:12px;font-size:16px;outline:none;margin:5px 0 9px}input:focus,textarea:focus,select:focus{border-color:#84adff;box-shadow:0 0 0 3px #eff4ff}textarea{min-height:90px;resize:vertical}.wallet-card{background:linear-gradient(145deg,#155eef,#004eeb);color:white;border-radius:23px;padding:18px;position:relative;overflow:hidden}.wallet-card:after{content:"";position:absolute;width:140px;height:140px;border:26px solid rgba(255,255,255,.08);border-radius:50%;right:-55px;top:-70px}.wallet-card small{opacity:.75}.wallet-card .number{font-size:18px;font-weight:850;letter-spacing:.8px;margin:15px 0 5px}.timeline{position:relative;padding-left:24px}.timeline:before{content:"";position:absolute;left:8px;top:6px;bottom:6px;width:2px;background:#e4e7ec}.event{position:relative;margin:0 0 13px}.event:before{content:"";position:absolute;width:10px;height:10px;border-radius:50%;left:-20px;top:4px;background:#98a2b3;box-shadow:0 0 0 4px var(--bg)}.event strong{font-size:12px}.event p{font-size:10px;color:var(--muted);margin:2px 0}.faq-item{border-bottom:1px solid var(--line)}.faq-q{width:100%;border:0;background:transparent;padding:13px 0;display:flex;justify-content:space-between;gap:10px;text-align:left;font-weight:800}.faq-a{display:none;padding:0 0 13px;font-size:12px;color:var(--muted);white-space:pre-wrap;line-height:1.55}.faq-item.open .faq-a{display:block}.rating-row{display:flex;gap:6px}.star{width:42px;height:42px;border:1px solid var(--line);background:var(--surface);border-radius:12px}.admin-only{display:none}.user-only{display:block}body.admin .admin-only{display:block}body.admin .user-only{display:none}.admin-hero{background:linear-gradient(135deg,#0b1220,#182230 60%,#344054);color:#fff;border-radius:24px;padding:19px}.admin-hero strong{font-size:22px}.admin-kpis{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-top:11px}.admin-kpi{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.08);border-radius:16px;padding:12px}.admin-kpi b{font-size:20px;display:block}.admin-kpi span{font-size:10px;opacity:.72}.nav-wrap{position:fixed;z-index:30;left:0;right:0;bottom:calc(9px + env(safe-area-inset-bottom));display:flex;justify-content:center;pointer-events:none;padding:0 10px}.navbar{pointer-events:auto;display:grid;grid-template-columns:repeat(5,1fr);align-items:center;gap:2px;width:min(540px,100%);padding:7px;background:rgba(255,255,255,.92);border:1px solid rgba(16,24,40,.08);border-radius:26px;box-shadow:0 18px 50px rgba(16,24,40,.18);backdrop-filter:blur(22px)}.nav-item{height:54px;border:0;background:transparent;border-radius:18px;color:#98a2b3;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;font-size:9px;font-weight:850;min-width:0}.nav-item svg{width:21px;height:21px;stroke-width:1.9}.nav-item.active{background:#eef4ff;color:var(--accent)}.nav-item.create{height:54px;border-radius:18px;background:linear-gradient(145deg,#2563eb,#155eef);color:#fff;box-shadow:0 8px 18px rgba(37,99,235,.32)}.nav-item.create svg{width:24px;height:24px}.modal{position:fixed;z-index:60;inset:0;background:rgba(16,24,40,.48);display:none;align-items:flex-end;justify-content:center}.modal.open{display:flex}.sheet{width:min(760px,100%);max-height:90%;overflow:auto;background:var(--bg);border-radius:28px 28px 0 0;padding:8px 14px calc(18px + env(safe-area-inset-bottom));box-shadow:var(--shadow)}.handle{width:42px;height:4px;border-radius:999px;background:#d0d5dd;margin:4px auto 12px}.sheet-head{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:10px}.sheet-head h3{margin:0;font-size:18px}.close{width:38px;height:38px;border:0;border-radius:12px;background:var(--surface);font-size:20px}.create-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.create-choice{border:1px solid var(--line);background:var(--surface);border-radius:20px;padding:17px;text-align:left}.create-choice .service-icon{margin-bottom:10px}.create-choice strong{display:block;font-size:14px}.create-choice p{font-size:10px;color:var(--muted);margin:4px 0 0}.notice{background:#eff8ff;border:1px solid #d1e9ff;color:#175cd3;border-radius:14px;padding:10px;font-size:11px;line-height:1.5}.toast{position:fixed;z-index:90;left:50%;bottom:calc(var(--nav-h) + 25px + env(safe-area-inset-bottom));transform:translate(-50%,15px);opacity:0;pointer-events:none;background:#101828;color:#fff;border-radius:13px;padding:10px 13px;font-size:12px;font-weight:750;transition:.2s}.toast.show{opacity:1;transform:translate(-50%,0)}.skeleton{height:76px;border-radius:18px;background:linear-gradient(90deg,#eef1f4 25%,#f8fafc 37%,#eef1f4 63%);background-size:400% 100%;animation:sh 1.2s infinite}@keyframes sh{0%{background-position:100% 0}100%{background-position:0 0}}@media(min-width:620px){.service-grid{grid-template-columns:repeat(4,1fr)}.create-grid{grid-template-columns:repeat(4,1fr)}.quick-grid{grid-template-columns:repeat(6,1fr)}.stats{gap:10px}.app{padding-left:18px;padding-right:18px}}\n</style>\n</head>\n<body>\n<main class="app">\n  <header class="topbar">\n    <button class="avatar" id="avatar" aria-label="Profil">M</button>\n    <div class="identity"><strong id="name">MHDV</strong><div class="sub" id="username">Yuklanmoqda...</div></div>\n    <button class="icon-btn" id="refresh" aria-label="Yangilash"></button>\n  </header>\n\n  <div class="user-only">\n    <section class="page active" data-page="home">\n      <div class="hero"><div class="eyebrow">MHDV DIGITAL STUDIO</div><h1>G‘oyadan tayyor natijagacha — bitta ilovada.</h1><p>Buyurtma bering, jarayonni kuzating, to‘lov yuboring va admin bilan yozishing.</p><div class="hero-actions"><button class="pillbtn" id="heroCreate">+ Yangi loyiha</button><button class="pillbtn ghost" data-go="orders">Jarayonni ko‘rish</button></div></div>\n      <div class="stats"><div class="stat"><strong id="stOrders">0</strong><span>Buyurtma</span></div><div class="stat"><strong id="stActive">0</strong><span>Jarayonda</span></div><div class="stat"><strong id="stDone">0</strong><span>Tayyor</span></div><div class="stat"><strong id="stPay">0</strong><span>To‘lov</span></div></div>\n      <div class="section-head"><h2>Xizmatlar</h2><button class="link-btn" id="allServices">Barchasi</button></div>\n      <div class="service-grid" id="serviceGrid"></div>\n      <div class="section-head"><h2>Tezkor amallar</h2></div>\n      <div class="quick-grid" id="quickGrid"></div>\n      <div class="section-head"><h2>Oxirgi faollik</h2><button class="link-btn" data-go="orders">Hammasi</button></div>\n      <div class="timeline" id="activityFeed"></div>\n    </section>\n\n    <section class="page" data-page="orders">\n      <div class="section-head"><h2>Loyihalarim</h2><button class="link-btn" id="newOrderBtn">+ Yangi</button></div>\n      <div class="search" id="searchWrap"><input id="orderSearch" placeholder="ID, xizmat yoki nom"><span id="searchIcon"></span></div>\n      <div class="filters"><button class="chip active" data-filter="all">Barchasi</button><button class="chip" data-filter="pending">Yangi</button><button class="chip" data-filter="accepted">Jarayonda</button><button class="chip" data-filter="completed">Topshirildi</button><button class="chip" data-filter="rejected">Rad etilgan</button></div>\n      <div id="ordersList"></div>\n    </section>\n\n    <section class="page" data-page="inbox">\n      <div class="section-head"><h2>Muloqot</h2></div>\n      <div class="tabs"><button class="tab active" data-inbox-tab="chat">Admin bilan chat</button><button class="tab" data-inbox-tab="feedback">Murojaatlar</button></div>\n      <div id="chatPane"><div class="chat-list" id="chatList"></div><div class="composer"><textarea id="chatText" placeholder="Xabar yozing..."></textarea><button class="btn primary" id="sendChat" aria-label="Yuborish"></button></div></div>\n      <div id="feedbackPane" style="display:none"><div class="card"><select id="fbKind"><option value="taklif">Taklif</option><option value="shikoyat">Shikoyat</option></select><textarea id="fbText" placeholder="Taklif yoki muammoingizni yozing..."></textarea><button class="btn primary full" id="sendFeedback">Murojaat yuborish</button></div><div id="feedbackList"></div></div>\n    </section>\n\n    <section class="page" data-page="wallet">\n      <div class="section-head"><h2>To‘lov markazi</h2></div>\n      <div class="wallet-card"><small>To‘lov uchun karta</small><div class="number" id="cardNumber">—</div><div id="cardOwner" style="font-size:12px;opacity:.82">—</div><div class="actions"><button class="pillbtn" id="copyCard">Karta raqamini nusxalash</button></div></div>\n      <div class="section-head"><h2>To‘lovlar tarixi</h2></div><div id="paymentsList"></div>\n    </section>\n\n    <section class="page" data-page="profile">\n      <div class="card"><div class="row-head"><div><div class="order-title" id="profileName">—</div><div class="order-meta" id="profileMeta">—</div></div><span class="badge info">MHDV Client</span></div></div>\n      <div class="section-head"><h2>Logo o‘yini</h2></div><div class="card"><div id="gameInfo" class="sub" style="white-space:normal;line-height:1.5"></div><div class="actions"><button class="btn primary" id="gameJoin">ID olish</button></div></div>\n      <div class="section-head"><h2>Ma’lumot</h2></div><div class="card"><strong>MHDV haqida</strong><p id="mhdvInfo" class="sub" style="white-space:pre-wrap;line-height:1.6"></p><strong>Ijtimoiy tarmoqlar</strong><p id="socialLinks" class="sub" style="white-space:pre-wrap;line-height:1.6"></p></div>\n      <div class="section-head"><h2>FAQ</h2></div><div class="card" id="faqBox"></div>\n      <div class="section-head"><h2>Botni baholash</h2></div><div class="rating-row" id="ratingRow"></div>\n    </section>\n  </div>\n\n  <div class="admin-only">\n    <section class="page active" data-apage="dashboard"><div class="admin-hero"><div class="sub" style="color:#d0d5dd">ADMIN MODE</div><strong>Boshqaruv markazi</strong><div class="admin-kpis"><div class="admin-kpi"><b id="aUsers">0</b><span>Foydalanuvchi</span></div><div class="admin-kpi"><b id="aPending">0</b><span>Yangi buyurtma</span></div><div class="admin-kpi"><b id="aPayments">0</b><span>Kutilayotgan to‘lov</span></div><div class="admin-kpi"><b id="aFeedback">0</b><span>Javobsiz murojaat</span></div></div></div><div class="section-head"><h2>Tezkor boshqaruv</h2></div><div class="quick-grid" id="adminQuick"></div><div class="section-head"><h2>So‘nggi buyurtmalar</h2><button class="link-btn" data-ago="orders">Hammasi</button></div><div id="aRecentOrders"></div></section>\n    <section class="page" data-apage="orders"><div class="section-head"><h2>Buyurtmalar</h2></div><div class="search"><input id="aOrderSearch" placeholder="ID, mijoz yoki xizmat"><span id="aSearchIcon"></span></div><div class="filters"><button class="chip active" data-afilter="all">Barchasi</button><button class="chip" data-afilter="pending">Yangi</button><button class="chip" data-afilter="accepted">Qabul</button><button class="chip" data-afilter="rejected">Rad</button></div><div id="aOrders"></div></section>\n    <section class="page" data-apage="users"><div class="section-head"><h2>Foydalanuvchilar</h2></div><div class="search"><input id="aUserSearch" placeholder="Ism, username yoki ID"><span id="aUserSearchIcon"></span></div><div id="aUsersList"></div></section>\n    <section class="page" data-apage="inbox"><div class="section-head"><h2>Chatlar</h2></div><div id="aChats"></div><div class="section-head"><h2>Murojaatlar</h2></div><div id="aFeedbackList"></div></section>\n    <section class="page" data-apage="more"><div class="section-head"><h2>Qo‘shimcha boshqaruv</h2></div><div id="aMoreContent"></div></section>\n  </div>\n</main>\n\n<div class="nav-wrap user-only"><nav class="navbar" id="userNav"><button class="nav-item active" data-nav="home"></button><button class="nav-item" data-nav="orders"></button><button class="nav-item create" id="plusUser"></button><button class="nav-item" data-nav="inbox"></button><button class="nav-item" data-nav="profile"></button></nav></div>\n<div class="nav-wrap admin-only"><nav class="navbar" id="adminNav"><button class="nav-item active" data-anav="dashboard"></button><button class="nav-item" data-anav="orders"></button><button class="nav-item create" id="plusAdmin"></button><button class="nav-item" data-anav="users"></button><button class="nav-item" data-anav="inbox"></button></nav></div>\n\n<div class="modal" id="modal"><div class="sheet"><div class="handle"></div><div class="sheet-head"><h3 id="modalTitle"></h3><button class="close" id="modalClose">×</button></div><form id="modalForm"></form></div></div>\n<div class="toast" id="toast"></div>\n<script>\nconst TG=window.Telegram?.WebApp; if(TG){TG.ready();TG.expand();TG.setHeaderColor?.(\'#f4f6f9\');TG.setBackgroundColor?.(\'#f4f6f9\')}\nconst $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];\nlet DATA=null,ADMIN=null,FILTER=\'all\',AFILTER=\'all\';\nconst initData=TG?.initData||\'\';\nconst esc=s=>String(s??\'\').replace(/[&<>\'"]/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',"\'":\'&#39;\',\'"\':\'&quot;\'}[c]));\nconst haptic=t=>{try{TG?.HapticFeedback?.impactOccurred(t||\'light\')}catch(_){}};\nfunction icon(n){const p={home:\'<path d="M3 10.8 12 3l9 7.8v9.2a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>\',orders:\'<path d="M5 4h14v16H5z"/><path d="M8 8h8M8 12h8M8 16h5"/>\',plus:\'<path d="M12 5v14M5 12h14"/>\',chat:\'<path d="M4 5h16v11H9l-5 4z"/><path d="M8 9h8M8 12h5"/>\',user:\'<circle cx="12" cy="8" r="4"/><path d="M4 21c.8-4 3.5-6 8-6s7.2 2 8 6"/>\',wallet:\'<path d="M3 7h18v12H3z"/><path d="M3 9V5h15M16 13h5"/>\',search:\'<circle cx="11" cy="11" r="7"/><path d="m16.2 16.2 4 4"/>\',refresh:\'<path d="M20 7v5h-5"/><path d="M19 12a7 7 0 1 1-2-5"/>\',send:\'<path d="m3 11 18-8-7 18-3-7z"/><path d="M11 14 21 3"/>\',globe:\'<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18"/>\',pen:\'<path d="m4 20 4.5-1 10-10-3.5-3.5-10 10z"/><path d="m13.5 6.5 3.5 3.5"/>\',bot:\'<rect x="4" y="7" width="16" height="12" rx="4"/><path d="M12 3v4M8 12h.01M16 12h.01M8 16h8"/>\',spark:\'<path d="m12 2 1.6 5.4L19 9l-5.4 1.6L12 16l-1.6-5.4L5 9l5.4-1.6zM19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/>\',game:\'<path d="M7 8h10a5 5 0 0 1 4.5 7.2l-1.1 2.2a2 2 0 0 1-3.2.5L15 16H9l-2.2 1.9a2 2 0 0 1-3.2-.5l-1.1-2.2A5 5 0 0 1 7 8z"/><path d="M7 12v4M5 14h4M16.5 13h.01M19 15h.01"/>\',bell:\'<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/>\',star:\'<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9z"/>\',users:\'<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8"/>\',broadcast:\'<path d="M3 11v2h4l9 5V6l-9 5zM19 9a4 4 0 0 1 0 6"/>\',check:\'<path d="m5 12 4 4L19 6"/>\',x:\'<path d="m6 6 12 12M18 6 6 18"/>\',copy:\'<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M15 9V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h4"/>\'};return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round">${p[n]||p.spark}</svg>`}\nfunction setNavIcons(){const items=[[\'home\',\'home\',\'Bosh\'],[\'orders\',\'orders\',\'Loyihalar\'],[\'plusUser\',\'plus\',\'Yaratish\'],[\'inbox\',\'chat\',\'Muloqot\'],[\'profile\',\'user\',\'Profil\']];items.forEach(([id,ic,label])=>{const el=id===\'plusUser\'?$(\'#plusUser\'):$(`[data-nav="${id}"]`);if(el)el.innerHTML=icon(ic)+`<span>${label}</span>`});const a=[[\'dashboard\',\'home\',\'Panel\'],[\'orders\',\'orders\',\'Zakaz\'],[\'plusAdmin\',\'plus\',\'Amal\'],[\'users\',\'users\',\'Mijoz\'],[\'inbox\',\'chat\',\'Inbox\']];a.forEach(([id,ic,label])=>{const el=id===\'plusAdmin\'?$(\'#plusAdmin\'):$(`[data-anav="${id}"]`);if(el)el.innerHTML=icon(ic)+`<span>${label}</span>`});$(\'#refresh\').innerHTML=icon(\'refresh\');$(\'#searchIcon\').innerHTML=icon(\'search\');$(\'#aSearchIcon\').innerHTML=icon(\'search\');$(\'#aUserSearchIcon\').innerHTML=icon(\'search\');$(\'#sendChat\').innerHTML=icon(\'send\')}\nsetNavIcons();\nasync function api(url,opt={}){const headers=new Headers(opt.headers||{});if(initData)headers.set(\'X-Telegram-Init-Data\',initData);const r=await fetch(url,{...opt,headers});let j={};try{j=await r.json()}catch(_){throw new Error(\'Server noto‘g‘ri javob qaytardi\')}if(!r.ok||j.ok===false)throw new Error(j.error||\'Xatolik\');return j}\nfunction toast(s){const t=$(\'#toast\');t.textContent=s;t.classList.add(\'show\');clearTimeout(t._x);t._x=setTimeout(()=>t.classList.remove(\'show\'),2200)}\nfunction showPage(p){$$(\'[data-page]\').forEach(x=>x.classList.toggle(\'active\',x.dataset.page===p));$$(\'[data-nav]\').forEach(x=>x.classList.toggle(\'active\',x.dataset.nav===p));haptic();window.scrollTo({top:0,behavior:\'smooth\'})}\nfunction showAPage(p){$$(\'[data-apage]\').forEach(x=>x.classList.toggle(\'active\',x.dataset.apage===p));$$(\'[data-anav]\').forEach(x=>x.classList.toggle(\'active\',x.dataset.anav===p));haptic();window.scrollTo({top:0,behavior:\'smooth\'})}\n$$(\'[data-nav]\').forEach(b=>b.onclick=()=>showPage(b.dataset.nav));$$(\'[data-anav]\').forEach(b=>b.onclick=()=>showAPage(b.dataset.anav));$$(\'[data-go]\').forEach(b=>b.onclick=()=>showPage(b.dataset.go));$$(\'[data-ago]\').forEach(b=>b.onclick=()=>showAPage(b.dataset.ago));$(\'#avatar\').onclick=()=>document.body.classList.contains(\'admin\')?showAPage(\'dashboard\'):showPage(\'profile\');\nfunction openModal(title,html,onSubmit){$(\'#modalTitle\').textContent=title;$(\'#modalForm\').innerHTML=html;$(\'#modal\').classList.add(\'open\');$(\'#modalForm\').onsubmit=onSubmit||((e)=>e.preventDefault());haptic(\'medium\')}\nfunction closeModal(){$(\'#modal\').classList.remove(\'open\');$(\'#modalForm\').innerHTML=\'\'}$(\'#modalClose\').onclick=closeModal;$(\'#modal\').onclick=e=>{if(e.target===$(\'#modal\'))closeModal()};\nconst services={website:{label:\'Veb-sayt\',desc:\'Biznes va shaxsiy saytlar\',icon:\'globe\',cls:\'svc-web\'},logo:{label:\'Logo\',desc:\'Brend uchun kuchli identika\',icon:\'pen\',cls:\'svc-logo\'},bot:{label:\'Telegram bot\',desc:\'Avtomatlashtirish va savdo\',icon:\'bot\',cls:\'svc-bot\'},ai_image:{label:\'AI rasm\',desc:\'Kontent va kreativ vizual\',icon:\'spark\',cls:\'svc-ai\'}};\nconst schema={website:{title:\'Yangi veb-sayt\',fields:[[\'site_name\',\'Sayt nomi\'],[\'purpose\',\'Maqsadi\'],[\'sphere\',\'Faoliyat sohasi\'],[\'budget\',\'Byudjet\'],[\'requirements\',\'Asosiy talablar\'],[\'additional\',\'Qo‘shimcha\']]},logo:{title:\'Yangi logo\',fields:[[\'brand_name\',\'Brend nomi\'],[\'direction\',\'Brend yo‘nalishi\'],[\'budget\',\'Byudjet\'],[\'additional\',\'Rang, uslub va qo‘shimcha\']]},bot:{title:\'Yangi Telegram bot\',fields:[[\'bot_name\',\'Bot nomi\'],[\'purpose\',\'Vazifasi\'],[\'platform\',\'Platforma\'],[\'budget\',\'Byudjet\'],[\'requirements\',\'Funksiyalar\'],[\'additional\',\'Qo‘shimcha\']]},ai_image:{title:\'Yangi AI rasm\',fields:[[\'topic\',\'Mavzu\'],[\'style\',\'Uslub\'],[\'size\',\'O‘lcham / format\'],[\'budget\',\'Byudjet\'],[\'additional\',\'Qo‘shimcha\']]}};\nfunction renderServiceCards(){const g=$(\'#serviceGrid\');g.innerHTML=Object.entries(services).map(([k,s])=>`<button class="service-card" data-order="${k}"><div class="service-icon ${s.cls}">${icon(s.icon)}</div><strong>${s.label}</strong><p>${s.desc}</p><span class="arrow">›</span></button>`).join(\'\');$$(\'[data-order]\').forEach(b=>b.onclick=()=>openOrder(b.dataset.order))}\nfunction createSheet(){openModal(\'Nima yaratmoqchisiz?\',`<div class="create-grid">${Object.entries(services).map(([k,s])=>`<button type="button" class="create-choice" data-create="${k}"><div class="service-icon ${s.cls}">${icon(s.icon)}</div><strong>${s.label}</strong><p>${s.desc}</p></button>`).join(\'\')}</div>`);$$(\'[data-create]\').forEach(b=>b.onclick=()=>{const k=b.dataset.create;closeModal();setTimeout(()=>openOrder(k),120)})}\n$(\'#plusUser\').onclick=createSheet;$(\'#heroCreate\').onclick=createSheet;$(\'#newOrderBtn\').onclick=createSheet;$(\'#allServices\').onclick=createSheet;\nfunction openOrder(type,prefill={}){const x=schema[type];if(!x)return;const fields=x.fields.map(([n,l])=>`<label>${l}${n===\'additional\'?\'\':\' *\'}<textarea name="${n}" maxlength="1500" ${n===\'additional\'?\'\':\'required\'}>${esc(prefill[n]||\'\')}</textarea></label>`).join(\'\');openModal(x.title,fields+\'<button class="btn primary full">Buyurtma berish</button>\',async e=>{e.preventDefault();const body={type};new FormData(e.currentTarget).forEach((v,k)=>body[k]=String(v).trim());try{const r=await api(\'/miniapp/api/orders\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify(body)});closeModal();toast(\'Buyurtma #\'+r.order_id+\' yuborildi\');await load();showPage(\'orders\')}catch(x){toast(x.message)}})}\nfunction statusInfo(o){if(o.status===\'rejected\')return{label:\'Rad etilgan\',cls:\'bad\',pct:100};if(o.stage===\'topshirildi\')return{label:\'Topshirildi\',cls:\'good\',pct:100};if(o.stage===\'tayyor\')return{label:\'Tayyor\',cls:\'good\',pct:82};if(o.stage===\'ishlanmoqda\')return{label:\'Ishlanmoqda\',cls:\'info\',pct:60};if(o.status===\'accepted\')return{label:\'Qabul qilindi\',cls:\'info\',pct:38};return{label:\'Ko‘rib chiqilmoqda\',cls:\'warn\',pct:14}}\nfunction orderCard(o){const s=statusInfo(o),data=Object.values(o.data||{}).filter(Boolean).slice(0,3).map(esc).join(\' · \');return `<div class="row"><div class="row-head"><div><div class="order-title">#${o.id} · ${esc(services[o.type]?.label||o.title)}</div><div class="order-meta">${esc(o.created_at||\'\')}</div></div><span class="badge ${s.cls}">${s.label}</span></div><div class="progress"><span style="width:${s.pct}%"></span></div><div class="stage-line"><span>Yuborildi</span><span>Jarayon</span><span>Tayyor</span></div>${data?`<div class="order-data">${data}</div>`:\'\'}${o.reject_reason?`<div class="notice" style="background:#fef3f2;border-color:#fee4e2;color:#b42318">Sabab: ${esc(o.reject_reason)}</div>`:\'\'}${o.revision_note?`<div class="notice">Tuzatish: ${esc(o.revision_note)}</div>`:\'\'}<div class="actions">${o.status===\'pending\'?`<button class="btn danger" data-cancel="${o.id}">Bekor qilish</button>`:\'\'}${o.status===\'accepted\'&&!o.payment?`<button class="btn primary" data-pay="${o.id}">Chek yuborish</button>`:\'\'}${o.stage===\'tayyor\'||o.stage===\'topshirildi\'?`<button class="btn" data-revise="${o.id}">Tuzatish</button>`:\'\'}${o.stage===\'topshirildi\'&&!o.rating?`<button class="btn good" data-rateorder="${o.id}">Baholash</button>`:\'\'}<button class="btn" data-repeat="${o.id}">Takror buyurtma</button></div></div>`}\nfunction renderOrders(){if(!DATA)return;const q=$(\'#orderSearch\').value.toLowerCase().trim();let arr=DATA.orders||[];if(FILTER===\'completed\')arr=arr.filter(o=>o.stage===\'topshirildi\');else if(FILTER!==\'all\')arr=arr.filter(o=>o.status===FILTER);if(q)arr=arr.filter(o=>(o.id+\' \'+o.type+\' \'+JSON.stringify(o.data||{})).toLowerCase().includes(q));$(\'#ordersList\').innerHTML=arr.map(orderCard).join(\'\')||`<div class="empty">${icon(\'orders\')}Buyurtma topilmadi.</div>`;bindUserRows()}\nfunction bindUserRows(){$$(\'[data-pay]\').forEach(b=>b.onclick=()=>paymentForm(+b.dataset.pay));$$(\'[data-revise]\').forEach(b=>b.onclick=()=>revisionForm(+b.dataset.revise));$$(\'[data-rateorder]\').forEach(b=>b.onclick=()=>ratingForm(+b.dataset.rateorder));$$(\'[data-repeat]\').forEach(b=>b.onclick=()=>{const o=DATA.orders.find(x=>x.id===+b.dataset.repeat);if(o)openOrder(o.type,o.data||{})});$$(\'[data-cancel]\').forEach(b=>b.onclick=async()=>{if(!confirm(\'Buyurtmani bekor qilasizmi?\'))return;try{await api(\'/miniapp/api/orders/\'+b.dataset.cancel+\'/cancel\',{method:\'POST\'});await load();toast(\'Bekor qilindi\')}catch(e){toast(e.message)}})}\nfunction paymentForm(id){openModal(\'To‘lov chekini yuborish\',`<div class="notice">Karta: <b>${esc(DATA.settings.card_number||\'\')}</b><br>Egasi: ${esc(DATA.settings.card_owner||\'\')}</div><input type="file" name="file" accept="image/*,.pdf" required><button class="btn primary full">Chek yuborish</button>`,async e=>{e.preventDefault();const fd=new FormData(e.currentTarget);fd.append(\'order_id\',id);try{await api(\'/miniapp/api/payment\',{method:\'POST\',body:fd});closeModal();await load();toast(\'Chek yuborildi\')}catch(x){toast(x.message)}})}\nfunction revisionForm(id){openModal(\'Tuzatish so‘rovi\',\'<textarea name="note" required placeholder="Nimani o‘zgartirish kerak?"></textarea><button class="btn primary full">Yuborish</button>\',async e=>{e.preventDefault();try{await api(\'/miniapp/api/orders/\'+id+\'/revision\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({note:new FormData(e.currentTarget).get(\'note\')})});closeModal();await load();toast(\'Tuzatish yuborildi\')}catch(x){toast(x.message)}})}\nfunction ratingForm(id){openModal(\'Loyihani baholang\',\'<select name="rating"><option value="5">5 — A’lo</option><option value="4">4 — Yaxshi</option><option value="3">3 — Qoniqarli</option><option value="2">2 — Yaxshilash kerak</option><option value="1">1 — Yomon</option></select><button class="btn primary full">Bahoni yuborish</button>\',async e=>{e.preventDefault();try{await api(\'/miniapp/api/orders/\'+id+\'/rating\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({rating:+new FormData(e.currentTarget).get(\'rating\')})});closeModal();await load();toast(\'Rahmat!\')}catch(x){toast(x.message)}})}\nfunction renderActivity(){const ev=[];(DATA.orders||[]).slice(0,4).forEach(o=>ev.push({t:`Buyurtma #${o.id} · ${statusInfo(o).label}`,d:o.created_at}));(DATA.payments||[]).slice(0,2).forEach(p=>ev.push({t:`To‘lov #${p.id} · ${p.status===\'confirmed\'?\'Tasdiqlandi\':\'Tekshirilmoqda\'}`,d:p.created_at}));$(\'#activityFeed\').innerHTML=ev.length?ev.map(x=>`<div class="event"><strong>${esc(x.t)}</strong><p>${esc(x.d||\'\')}</p></div>`).join(\'\'):\'<div class="empty">Hozircha faollik yo‘q.</div>\'}\nfunction renderQuick(){const q=[[\'wallet\',\'wallet\',\'To‘lovlar\'],[\'chat\',\'chat\',\'Admin chat\'],[\'game\',\'game\',\'Logo o‘yini\'],[\'bell\',\'bell\',\'Faollik\']];$(\'#quickGrid\').innerHTML=q.map(([k,ic,l])=>`<button class="quick" data-quick="${k}">${icon(ic)}<span>${l}</span></button>`).join(\'\');$$(\'[data-quick]\').forEach(b=>b.onclick=()=>{const q=b.dataset.quick;if(q===\'wallet\')showPage(\'wallet\');else if(q===\'chat\')showPage(\'inbox\');else if(q===\'game\')showPage(\'profile\');else window.scrollTo({top:document.body.scrollHeight,behavior:\'smooth\'})})}\nfunction renderPayments(){const a=DATA.payments||[];$(\'#paymentsList\').innerHTML=a.map(p=>`<div class="row"><div class="row-head"><div><div class="order-title">To‘lov #${p.id}</div><div class="order-meta">Buyurtma #${p.order_id} · ${esc(p.created_at||\'\')}</div></div><span class="badge ${p.status===\'confirmed\'?\'good\':\'warn\'}">${p.status===\'confirmed\'?\'Tasdiqlangan\':\'Tekshirilmoqda\'}</span></div></div>`).join(\'\')||`<div class="empty">${icon(\'wallet\')}Hali to‘lov yuborilmagan.</div>`}\nfunction renderInbox(){const msgs=DATA.messages||[];$(\'#chatList\').innerHTML=msgs.map(m=>`<div class="bubble ${m.direction===\'in\'?\'out\':\'\'}">${esc(m.text)}<time>${esc(m.created_at||\'\')}</time></div>`).join(\'\')||\'<div class="empty">Admin bilan suhbatni boshlashingiz mumkin.</div>\';const c=$(\'#chatList\');c.scrollTop=c.scrollHeight;$(\'#feedbackList\').innerHTML=(DATA.feedback||[]).map(f=>`<div class="row"><div class="row-head"><strong>${f.kind===\'shikoyat\'?\'Shikoyat\':\'Taklif\'} #${f.id}</strong><span class="badge ${f.admin_reply?\'good\':\'warn\'}">${f.admin_reply?\'Javob berilgan\':\'Kutilmoqda\'}</span></div><div class="order-data">${esc(f.text)}</div>${f.admin_reply?`<div class="notice">Admin: ${esc(f.admin_reply)}</div>`:\'\'}</div>`).join(\'\')||\'<div class="empty">Murojaatlar yo‘q.</div>\'}\nfunction renderProfile(){const u=DATA.user,s=DATA.settings;$(\'#profileName\').textContent=u.full_name||\'MHDV foydalanuvchi\';$(\'#profileMeta\').textContent=(u.username?\'@\'+u.username+\' · \':\'\')+\'ID \'+u.tg_id;$(\'#gameInfo\').textContent=DATA.game?`Sizning ID raqamingiz #${DATA.game.number}${DATA.game.winner?\' · G‘olib!\':\'\'}`:(s.game_info||\'O‘yin haqida ma’lumot\');$(\'#gameJoin\').textContent=DATA.game?\'ID #\'+DATA.game.number:\'ID olish\';$(\'#mhdvInfo\').textContent=s.mhdv_info||\'\';$(\'#socialLinks\').textContent=s.social_links||\'\';const faq=(s.faq||\'\').split(/\\n+/).filter(Boolean);$(\'#faqBox\').innerHTML=(faq.length?faq:[\'Tez-tez so‘raladigan savollar admin tomonidan kiritilmagan.\']).map((x,i)=>`<div class="faq-item"><button class="faq-q" type="button">Savol ${i+1}<span>+</span></button><div class="faq-a">${esc(x)}</div></div>`).join(\'\');$$(\'.faq-q\').forEach(q=>q.onclick=()=>q.parentElement.classList.toggle(\'open\'));$(\'#ratingRow\').innerHTML=[1,2,3,4,5].map(n=>`<button class="star" data-score="${n}" aria-label="${n} yulduz">★</button>`).join(\'\');$$(\'[data-score]\').forEach(b=>b.onclick=async()=>{try{await api(\'/miniapp/api/rate\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({score:+b.dataset.score})});toast(\'Bahoyingiz uchun rahmat!\')}catch(e){toast(e.message)}})}\nfunction renderUser(){const st=DATA.stats||{};$(\'#stOrders\').textContent=st.orders||0;$(\'#stActive\').textContent=st.accepted||0;$(\'#stDone\').textContent=st.completed||0;$(\'#stPay\').textContent=st.payments||0;renderServiceCards();renderQuick();renderOrders();renderActivity();renderPayments();renderInbox();renderProfile();$(\'#cardNumber\').textContent=DATA.settings.card_number||\'—\';$(\'#cardOwner\').textContent=DATA.settings.card_owner||\'—\'}\n$(\'#orderSearch\').oninput=renderOrders;$$(\'[data-filter]\').forEach(b=>b.onclick=()=>{$$(\'[data-filter]\').forEach(x=>x.classList.remove(\'active\'));b.classList.add(\'active\');FILTER=b.dataset.filter;renderOrders()});$$(\'[data-inbox-tab]\').forEach(b=>b.onclick=()=>{$$(\'[data-inbox-tab]\').forEach(x=>x.classList.remove(\'active\'));b.classList.add(\'active\');const chat=b.dataset.inboxTab===\'chat\';$(\'#chatPane\').style.display=chat?\'block\':\'none\';$(\'#feedbackPane\').style.display=chat?\'none\':\'block\'});\n$(\'#sendChat\').onclick=async()=>{const text=$(\'#chatText\').value.trim();if(!text)return;try{await api(\'/miniapp/api/chat\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({text})});$(\'#chatText\').value=\'\';await load();showPage(\'inbox\')}catch(e){toast(e.message)}};$(\'#sendFeedback\').onclick=async()=>{const text=$(\'#fbText\').value.trim();if(!text)return;try{await api(\'/miniapp/api/feedback\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({kind:$(\'#fbKind\').value,text})});$(\'#fbText\').value=\'\';await load();toast(\'Murojaat yuborildi\')}catch(e){toast(e.message)}};$(\'#gameJoin\').onclick=async()=>{try{const r=await api(\'/miniapp/api/game/join\',{method:\'POST\'});toast(\'Sizning ID #\'+r.number);await load()}catch(e){toast(e.message)}};$(\'#copyCard\').onclick=async()=>{const t=DATA?.settings?.card_number||\'\';try{await navigator.clipboard.writeText(t);toast(\'Karta raqami nusxalandi\')}catch(_){toast(t)}};\nfunction adminOrderCard(o){const s=o.status===\'pending\'?[\'Yangi\',\'warn\']:o.status===\'rejected\'?[\'Rad\',\'bad\']:o.stage===\'topshirildi\'?[\'Topshirildi\',\'good\']:[\'Jarayonda\',\'info\'];return `<div class="row"><div class="row-head"><div><div class="order-title">#${o.id} · ${esc(o.title||o.type)}</div><div class="order-meta">${esc(o.user||\'\')} · ${esc(o.created_at||\'\')}</div></div><span class="badge ${s[1]}">${s[0]}</span></div><div class="actions">${o.status===\'pending\'?`<button class="btn primary" data-aorder="${o.id}" data-action="accept">Qabul</button><button class="btn danger" data-aorder="${o.id}" data-action="reject">Rad</button>`:\'\'}${o.status===\'accepted\'?`<button class="btn" data-aorder="${o.id}" data-action="ishlanmoqda">Ishlanmoqda</button><button class="btn good" data-aorder="${o.id}" data-action="tayyor">Tayyor</button><button class="btn" data-aorder="${o.id}" data-action="topshirildi">Topshirildi</button>`:\'\'}<button class="btn" data-achat="${o.tg_id}">Yozish</button></div></div>`}\nfunction renderAdmin(){if(!ADMIN)return;const s=ADMIN.stats||{};$(\'#aUsers\').textContent=s.users||0;$(\'#aPending\').textContent=s.pending||0;$(\'#aPayments\').textContent=s.payments_pending||0;$(\'#aFeedback\').textContent=s.feedback_unanswered||0;$(\'#aRecentOrders\').innerHTML=(ADMIN.orders||[]).slice(0,6).map(adminOrderCard).join(\'\')||\'<div class="empty">Buyurtma yo‘q.</div>\';renderAdminOrders();renderAdminUsers();$(\'#aChats\').innerHTML=(ADMIN.chats||[]).map(c=>`<div class="row"><div class="row-head"><strong>${esc(c.user)}</strong><span class="badge info">Chat</span></div><div class="order-meta">${esc(c.last_time||\'\')}</div><div class="actions"><button class="btn primary" data-achat="${c.tg_id}">Javob yozish</button></div></div>`).join(\'\')||\'<div class="empty">Chat yo‘q.</div>\';$(\'#aFeedbackList\').innerHTML=(ADMIN.feedback||[]).map(f=>`<div class="row"><div class="row-head"><strong>${esc(f.user)} · #${f.id}</strong><span class="badge ${f.admin_reply?\'good\':\'warn\'}">${f.admin_reply?\'Javoblangan\':\'Yangi\'}</span></div><div class="order-data">${esc(f.text)}</div>${f.admin_reply?\'\':`<div class="actions"><button class="btn primary" data-afbreply="${f.id}">Javob</button></div>`}</div>`).join(\'\')||\'<div class="empty">Murojaat yo‘q.</div>\';bindAdmin()}\nfunction renderAdminOrders(){if(!ADMIN)return;const q=$(\'#aOrderSearch\').value.toLowerCase().trim();let a=ADMIN.orders||[];if(AFILTER!==\'all\')a=a.filter(o=>o.status===AFILTER);if(q)a=a.filter(o=>(o.id+\' \'+o.user+\' \'+o.title).toLowerCase().includes(q));$(\'#aOrders\').innerHTML=a.map(adminOrderCard).join(\'\')||\'<div class="empty">Topilmadi.</div>\';bindAdmin()}\nfunction renderAdminUsers(){if(!ADMIN)return;const q=$(\'#aUserSearch\').value.toLowerCase().trim();let a=ADMIN.users||[];if(q)a=a.filter(u=>(u.tg_id+\' \'+(u.full_name||\'\')+\' \'+(u.username||\'\')).toLowerCase().includes(q));$(\'#aUsersList\').innerHTML=a.map(u=>`<div class="row"><div class="row-head"><div><div class="order-title">${esc(u.full_name||\'Noma’lum\')}</div><div class="order-meta">${u.username?\'@\'+esc(u.username)+\' · \':\'\'}ID ${u.tg_id}</div></div><span class="badge ${u.blocked?\'bad\':\'good\'}">${u.blocked?\'Blok\':\'Faol\'}</span></div><div class="actions"><button class="btn" data-achat="${u.tg_id}">Yozish</button><button class="btn ${u.blocked?\'primary\':\'danger\'}" data-atoggle="${u.tg_id}">${u.blocked?\'Blokdan chiqarish\':\'Bloklash\'}</button></div></div>`).join(\'\')||\'<div class="empty">Topilmadi.</div>\';bindAdmin()}\nasync function adminAction(id,action){let reason=\'\';if(action===\'reject\'){reason=prompt(\'Rad etish sababi:\')||\'\';if(!reason)return}try{await api(\'/miniapp/api/admin/orders/\'+id+\'/action\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({action,reason})});await loadAdmin();toast(\'Saqlandi\')}catch(e){toast(e.message)}}\nfunction adminChat(id){openModal(\'Foydalanuvchiga yozish\',\'<textarea name="text" required placeholder="Xabar..."></textarea><button class="btn primary full">Yuborish</button>\',async e=>{e.preventDefault();try{await api(\'/miniapp/api/admin/chat/\'+id+\'/reply\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({text:new FormData(e.currentTarget).get(\'text\')})});closeModal();await loadAdmin();toast(\'Yuborildi\')}catch(x){toast(x.message)}})}\nfunction adminFeedback(id){openModal(\'Murojaatga javob\',\'<textarea name="text" required placeholder="Javob..."></textarea><button class="btn primary full">Yuborish</button>\',async e=>{e.preventDefault();try{await api(\'/miniapp/api/admin/feedback/\'+id+\'/reply\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({text:new FormData(e.currentTarget).get(\'text\')})});closeModal();await loadAdmin();toast(\'Javob yuborildi\')}catch(x){toast(x.message)}})}\nfunction bindAdmin(){$$(\'[data-aorder]\').forEach(b=>b.onclick=()=>adminAction(+b.dataset.aorder,b.dataset.action));$$(\'[data-achat]\').forEach(b=>b.onclick=()=>adminChat(+b.dataset.achat));$$(\'[data-afbreply]\').forEach(b=>b.onclick=()=>adminFeedback(+b.dataset.afbreply));$$(\'[data-atoggle]\').forEach(b=>b.onclick=async()=>{try{await api(\'/miniapp/api/admin/users/\'+b.dataset.atoggle+\'/toggle\',{method:\'POST\'});await loadAdmin();toast(\'Yangilandi\')}catch(e){toast(e.message)}})}\nfunction broadcastForm(){openModal(\'Broadcast yuborish\',\'<select name="segment"><option value="all">Hammaga</option><option value="ordered">Faqat buyurtma berganlarga</option></select><textarea name="text" required placeholder="Xabar matni..."></textarea><button class="btn primary full">Yuborish</button>\',async e=>{e.preventDefault();const f=new FormData(e.currentTarget);try{const r=await api(\'/miniapp/api/admin/broadcast\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({segment:f.get(\'segment\'),text:f.get(\'text\')})});closeModal();toast(r.sent+\' kishiga yuborildi\')}catch(x){toast(x.message)}})}\nfunction renderAdminQuick(){const q=[[\'orders\',\'orders\',\'Buyurtmalar\'],[\'payments\',\'wallet\',\'To‘lovlar\'],[\'broadcast\',\'broadcast\',\'Broadcast\'],[\'logs\',\'bell\',\'Loglar\']];$(\'#adminQuick\').innerHTML=q.map(([k,ic,l])=>`<button class="quick" data-aquick="${k}">${icon(ic)}<span>${l}</span></button>`).join(\'\');$$(\'[data-aquick]\').forEach(b=>b.onclick=()=>adminMore(b.dataset.aquick))}\nfunction adminMore(q){if(q===\'orders\'){showAPage(\'orders\');return}if(q===\'broadcast\'){broadcastForm();return}showAPage(\'more\');if(q===\'payments\'){const p=(ADMIN.payments||[]).filter(x=>x.status===\'pending\');$(\'#aMoreContent\').innerHTML=p.map(x=>`<div class="row"><div class="row-head"><strong>To‘lov #${x.id}</strong><span class="badge warn">Kutilmoqda</span></div><div class="order-meta">${esc(x.user||\'\')} · Buyurtma #${x.order_id||\'—\'}</div><div class="actions"><button class="btn primary" data-apay="${x.id}">Tasdiqlash</button></div></div>`).join(\'\')||\'<div class="empty">Kutilayotgan to‘lov yo‘q.</div>\';$$(\'[data-apay]\').forEach(z=>z.onclick=async()=>{try{await api(\'/miniapp/api/admin/payments/\'+z.dataset.apay+\'/confirm\',{method:\'POST\'});await loadAdmin();adminMore(\'payments\');toast(\'Tasdiqlandi\')}catch(e){toast(e.message)}})}else if(q===\'logs\'){$(\'#aMoreContent\').innerHTML=(ADMIN.logs||[]).map(l=>`<div class="row"><strong>${esc(l.action)}</strong><div class="order-meta">Admin ${l.admin_id} · ${esc(l.created_at)}</div><div class="order-data">${esc(l.details||\'\')}</div></div>`).join(\'\')||\'<div class="empty">Log yo‘q.</div>\'}}\n$(\'#plusAdmin\').onclick=()=>{openModal(\'Admin tezkor amallar\',`<div class="create-grid"><button type="button" class="create-choice" data-admin-create="broadcast"><div class="service-icon svc-web">${icon(\'broadcast\')}</div><strong>Broadcast</strong><p>Hammaga xabar yuborish</p></button><button type="button" class="create-choice" data-admin-create="payments"><div class="service-icon svc-bot">${icon(\'wallet\')}</div><strong>To‘lovlar</strong><p>Cheklarni tekshirish</p></button><button type="button" class="create-choice" data-admin-create="orders"><div class="service-icon svc-logo">${icon(\'orders\')}</div><strong>Buyurtmalar</strong><p>Jarayonni boshqarish</p></button><button type="button" class="create-choice" data-admin-create="logs"><div class="service-icon svc-ai">${icon(\'bell\')}</div><strong>Loglar</strong><p>Admin amallari tarixi</p></button></div>`);$$(\'[data-admin-create]\').forEach(b=>b.onclick=()=>{const q=b.dataset.adminCreate;closeModal();setTimeout(()=>adminMore(q),100)})};\n$(\'#aOrderSearch\').oninput=renderAdminOrders;$$(\'[data-afilter]\').forEach(b=>b.onclick=()=>{$$(\'[data-afilter]\').forEach(x=>x.classList.remove(\'active\'));b.classList.add(\'active\');AFILTER=b.dataset.afilter;renderAdminOrders()});$(\'#aUserSearch\').oninput=renderAdminUsers;\nasync function loadAdmin(){ADMIN=await api(\'/miniapp/api/admin/overview\');renderAdmin();renderAdminQuick()}\nasync function load(){const b=$(\'#refresh\');b.disabled=true;try{DATA=await api(\'/miniapp/api/me\');const u=DATA.user;$(\'#name\').textContent=u.full_name||\'MHDV\';$(\'#username\').textContent=(u.is_admin?\'Admin · \':\'\')+(u.username?\'@\'+u.username:\'ID \'+u.tg_id);$(\'#avatar\').textContent=(u.full_name||\'M\').trim().charAt(0).toUpperCase();if(u.is_admin){document.body.classList.add(\'admin\');await loadAdmin()}else{document.body.classList.remove(\'admin\');renderUser()}}catch(e){$(\'#name\').textContent=\'Kirish xatosi\';toast(e.message)}finally{b.disabled=false}}\n$(\'#refresh\').onclick=load;load();\n</script>\n</body></html>\n'
    return web.Response(text=html_out, content_type="text/html", headers={"Cache-Control":"no-store","X-Content-Type-Options":"nosniff"})

def _check_basic_auth(request) -> bool:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    import base64
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        _, _, password = decoded.partition(":")
    except Exception:
        return False
    return hmac.compare_digest(password, WEB_PANEL_PASSWORD)


def _unauthorized():
    return web.Response(
        status=401, text="Kirish uchun parol kerak.",
        headers={"WWW-Authenticate": 'Basic realm="MHDV Panel"'},
    )


from html import escape as esc

WEB_NAV = [
    ("/dashboard", "Bosh sahifa", "home"),
    ("/users", "Foydalanuvchilar", "users"),
    ("/orders", "Buyurtmalar", "orders"),
    ("/settings", "Sozlamalar", "settings"),
]

WEB_MORE_NAV = [
    ("/payments", "To'lovlar"),
    ("/feedback", "Fikrlar"),
    ("/chats", "Chatlar"),
    ("/game", "O'yin"),
    ("/broadcast", "Xabar yuborish"),
    ("/admins", "Adminlar"),
    ("/logs", "Loglar"),
]


def _web_icon(name: str) -> str:
    paths = {
        "home": '<path d="m4 10 8-6 8 6v9H4z"/><path d="M9 19v-5h6v5"/>',
        "users": '<path d="M16 20v-1.8a4.2 4.2 0 0 0-4.2-4.2H7.2A4.2 4.2 0 0 0 3 18.2V20"/><circle cx="9.5" cy="7" r="4"/><path d="M17 11a4 4 0 0 1 0 7.7"/>',
        "orders": '<path d="M4 7h16v13H4zM8 7V4h8v3"/>',
        "settings": '<circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 0 0-.1-1l2-1.5-2-3.4-2.5 1a8 8 0 0 0-1.7-1L14.4 3h-4.8l-.4 3.1a8 8 0 0 0-1.7 1l-2.5-1-2 3.4L5.1 11a7 7 0 0 0 0 2L3 14.5l2 3.4 2.5-1a8 8 0 0 0 1.7 1l.4 3.1h4.8l.4-3.1a8 8 0 0 0 1.7-1l2.5 1 2-3.4-2.1-1.5a7 7 0 0 0 .1-1z"/>',
    }
    return f'<svg viewBox="0 0 24 24" aria-hidden="true">{paths.get(name, paths["home"])}</svg>'


WEB_STYLE = """
:root{--bg:#f6f7f9;--card:#fff;--text:#14171d;--muted:#747b86;--line:#e7e9ee;--blue:#156fff;--sidebar:#10141d}
*{box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;background:var(--bg);color:var(--text);margin:0}.admin-layout{min-height:100vh}.side{position:fixed;left:0;top:0;bottom:0;width:245px;background:var(--sidebar);color:white;padding:20px 14px;overflow:auto}.side-brand{font-size:20px;font-weight:900;padding:6px 10px 20px}.side-brand small{display:block;color:#7f8a9d;font-size:11px;margin-top:3px}.side a{display:flex;align-items:center;gap:11px;color:#aeb6c5;text-decoration:none;padding:11px 12px;border-radius:12px;margin:3px 0;font-size:13px;font-weight:700}.side a:hover,.side a.active{background:#202735;color:#fff}.side svg{width:19px;height:19px;stroke:currentColor;fill:none;stroke-width:1.8}.side-title{font-size:10px;color:#606a7b;text-transform:uppercase;letter-spacing:.12em;padding:18px 12px 6px}.content{margin-left:245px;padding:26px;max-width:1500px}h1{font-size:27px;margin:0 0 20px;letter-spacing:-.5px}h3{font-size:16px}.card{background:#fff;border:1px solid var(--line);border-radius:18px;padding:18px;margin-bottom:15px;box-shadow:0 5px 18px rgba(20,29,48,.04);overflow:auto}table{border-collapse:collapse;width:100%;min-width:620px}td,th{border-bottom:1px solid var(--line);padding:11px 10px;text-align:left;font-size:13px}th{color:var(--muted);font-weight:700;background:#fafbfc}.stat{display:inline-block;min-width:150px;margin:7px 20px 7px 0;color:var(--muted);font-size:12px}.stat b{display:block;font-size:25px;color:#111827;margin-top:3px}a.btn,button{background:#111827;color:white;border:0;padding:9px 13px;border-radius:10px;cursor:pointer;text-decoration:none;display:inline-block;font-size:13px;margin:2px 4px 2px 0}button.danger,a.btn.danger{background:#e5484d}button.warn,a.btn.warn{background:#e89b19}input[type=text],textarea,select{background:#fff;border:1px solid #dfe3e8;color:#111827;padding:10px;border-radius:10px;width:100%;margin-bottom:9px}.pill{display:inline-block;padding:3px 8px;border-radius:12px;font-size:12px;background:#eef1f5}.pill.ok{background:#dcfce7}.pill.warn{background:#fef3c7}.pill.bad{background:#fee2e2}.pager a{margin-right:8px}.mobile-bottom{display:none}@media(max-width:820px){.side{display:none}.content{margin-left:0;padding:18px 12px 92px}.mobile-bottom{display:flex;position:fixed;z-index:99;left:10px;right:10px;bottom:10px;height:62px;border-radius:31px;background:rgba(255,255,255,.9);backdrop-filter:blur(20px);border:1px solid #fff;box-shadow:0 12px 34px rgba(17,24,39,.18);padding:7px}.mobile-bottom a{flex:1;text-decoration:none;color:#737983;display:grid;place-items:center;border-radius:25px;font-size:11px;font-weight:800}.mobile-bottom a.active{background:#edf0f4;color:#111}.mobile-bottom svg{width:20px;height:20px;stroke:currentColor;fill:none;stroke-width:1.8}h1{font-size:23px}}
"""


def web_page(title: str, body: str, active: str = "") -> web.Response:
    primary = WEB_NAV
    all_links = WEB_NAV + [(h, l, "home") for h, l in WEB_MORE_NAV]
    side_primary = "".join(f'<a href="{h}" class="{"active" if h == active else ""}">{_web_icon(i)}<span>{l}</span></a>' for h,l,i in primary)
    side_more = "".join(f'<a href="{h}" class="{"active" if h == active else ""}"><svg viewBox="0 0 24 24"><path d="M5 12h14M12 5v14"/></svg><span>{l}</span></a>' for h,l in WEB_MORE_NAV)
    mobile = "".join(f'<a href="{h}" class="{"active" if h == active else ""}">{_web_icon(i)}<span>{l}</span></a>' for h,l,i in primary)
    html_out = f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} — MHDV Admin</title><style>{WEB_STYLE}</style></head><body><div class="admin-layout"><aside class="side"><div class="side-brand">MHDV Admin<small>Boshqaruv markazi</small></div><div class="side-title">Asosiy</div>{side_primary}<div class="side-title">Boshqaruv</div>{side_more}</aside><main class="content"><h1>{esc(title)}</h1>{body}</main></div><nav class="mobile-bottom">{mobile}</nav></body></html>'''
    return web.Response(text=html_out, content_type="text/html")


def _paginate(items, page: int, per_page: int = 20):
    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    return items[start:start + per_page], page, total_pages


def pager_html(base_url: str, page: int, total_pages: int) -> str:
    if total_pages <= 1:
        return ""
    parts = ['<div class="pager">']
    if page > 0:
        parts.append(f'<a class="btn" href="{base_url}&page={page-1}">⬅️ Oldingi</a>')
    parts.append(f"<span>{page+1} / {total_pages}</span>")
    if page < total_pages - 1:
        parts.append(f'<a class="btn" href="{base_url}&page={page+1}">Keyingi ➡️</a>')
    parts.append("</div>")
    return "".join(parts)


async def dashboard(request):
    if not _check_basic_auth(request):
        return _unauthorized()

    total_users = await db_count_users_total()
    today_users = await db_count_users_today()
    blocked_users = await db_count_users_blocked()
    orders_total = await db_orders_count_total()
    game_count = await db_game_count()
    avg_rating, rating_count = await db_avg_rating()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT order_type, status, COUNT(*) as c FROM orders GROUP BY order_type, status"
        )
        order_rows = await cur.fetchall()
        cur = await db.execute("SELECT status, COUNT(*) as c FROM payments GROUP BY status")
        payment_rows = await cur.fetchall()

    order_lines = "".join(
        f"<tr><td>{esc(ORDER_TYPE_TITLES.get(r['order_type'], r['order_type']))}</td>"
        f"<td>{esc(r['status'])}</td><td>{r['c']}</td></tr>"
        for r in order_rows
    ) or "<tr><td colspan=3>—</td></tr>"
    payment_lines = "".join(
        f"<tr><td>{esc(r['status'])}</td><td>{r['c']}</td></tr>" for r in payment_rows
    ) or "<tr><td colspan=2>—</td></tr>"

    body = f"""
        <div class="card">
            <div class="stat">👥 Jami foydalanuvchilar<br><b>{total_users}</b></div>
            <div class="stat">🆕 Bugun qo'shilgan<br><b>{today_users}</b></div>
            <div class="stat">⛔️ Bloklangan<br><b>{blocked_users}</b></div>
            <div class="stat">📦 Jami buyurtmalar<br><b>{orders_total}</b></div>
            <div class="stat">🎮 O'yin ishtirokchilari<br><b>{game_count}</b></div>
            <div class="stat">⭐️ O'rtacha baho<br><b>{avg_rating:.2f}</b> ({rating_count} ta baho)</div>
        </div>
        <div class="card">
            <h3>📦 Buyurtmalar (turi / holati / soni)</h3>
            <table><tr><th>Turi</th><th>Holati</th><th>Soni</th></tr>{order_lines}</table>
        </div>
        <div class="card">
            <h3>💳 To'lovlar (holati / soni)</h3>
            <table><tr><th>Holati</th><th>Soni</th></tr>{payment_lines}</table>
        </div>
        <p style="color:#64748b">Sahifani yangilash uchun brauzerni yangilang (F5). Avtomatik yangilanmaydi.</p>
    """
    return web_page("Bosh sahifa", body, "/dashboard")


# ---------- Foydalanuvchilar ----------

async def web_users(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    page = int(request.query.get("page", 0))
    users = await db_get_all_users()
    users = list(reversed(users))
    page_items, page, total_pages = _paginate(users, page)
    rows = "".join(
        f"<tr><td>{u['id']}</td><td>{esc(u['full_name'] or '—')}</td>"
        f"<td>{'@' + esc(u['username']) if u['username'] else '—'}</td>"
        f"<td>{u['tg_id']}</td>"
        f"<td>{'<span class=\"pill bad\">bloklangan</span>' if u['is_blocked'] else '<span class=\"pill ok\">faol</span>'}</td>"
        f"<td>{esc(u['created_at'])}</td>"
        f"<td><a class='btn' href='/users/{u['tg_id']}'>Batafsil</a></td></tr>"
        for u in page_items
    ) or "<tr><td colspan=7>Foydalanuvchilar yo'q</td></tr>"
    body = f"""<div class="card"><table>
        <tr><th>#</th><th>Ism</th><th>Username</th><th>TG ID</th><th>Holat</th><th>Sana</th><th></th></tr>
        {rows}</table>{pager_html('/users?x=1', page, total_pages)}</div>"""
    return web_page("Foydalanuvchilar", body, "/users")


async def web_user_detail(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    tg_id = int(request.match_info["tg_id"])
    user = await db_get_user(tg_id)
    if not user:
        return web_page("Topilmadi", "<div class='card'>Foydalanuvchi topilmadi.</div>")
    orders = await db_get_orders_by_user(tg_id)
    order_rows = "".join(
        f"<tr><td>#{o['id']}</td><td>{esc(ORDER_TYPE_TITLES.get(o['order_type'], o['order_type']))}</td>"
        f"<td>{esc(o['status'])}</td><td>{esc(o['stage'] or '—')}</td>"
        f"<td><a class='btn' href='/orders/{o['id']}'>Ko'rish</a></td></tr>"
        for o in orders
    ) or "<tr><td colspan=5>Buyurtmalar yo'q</td></tr>"
    block_label = "✅ Blokdan chiqarish" if user["is_blocked"] else "⛔️ Bloklash"
    body = f"""
    <div class="card">
        <p><b>Ism:</b> {esc(user['full_name'] or '—')}</p>
        <p><b>Username:</b> {'@' + esc(user['username']) if user['username'] else '—'}</p>
        <p><b>Telegram ID:</b> {user['tg_id']}</p>
        <p><b>Ro'yxatdan o'tgan:</b> {esc(user['created_at'])}</p>
        <p><b>Holat:</b> {"⛔️ Bloklangan" if user['is_blocked'] else "✅ Faol"}</p>
        <form method="post" action="/users/{tg_id}/toggle-block">
            <button class="{'ok' if user['is_blocked'] else 'danger'}">{block_label}</button>
        </form>
    </div>
    <div class="card"><h3>📦 Buyurtmalari</h3><table>
        <tr><th>ID</th><th>Turi</th><th>Holat</th><th>Bosqich</th><th></th></tr>{order_rows}
    </table></div>
    """
    return web_page(f"Foydalanuvchi #{user['id']}", body, "/users")


async def web_user_toggle_block(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    tg_id = int(request.match_info["tg_id"])
    new_status = await db_toggle_block(tg_id)
    if new_status:
        await safe_send(tg_id, "⛔️ Siz botdan foydalanish huquqidan mahrum qilindingiz.")
    else:
        await safe_send(tg_id, "✅ Sizga botdan foydalanish huquqi qaytarildi.")
    await db_log_admin_action(0, "web_toggle_block", f"ID:{tg_id} (web panel)")
    raise web.HTTPFound(f"/users/{tg_id}")


# ---------- Buyurtmalar ----------

async def web_orders(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    page = int(request.query.get("page", 0))
    o_type = request.query.get("type", "")
    o_status = request.query.get("status", "")

    query = "SELECT * FROM orders WHERE 1=1"
    params = []
    if o_type:
        query += " AND order_type=?"
        params.append(o_type)
    if o_status:
        query += " AND status=?"
        params.append(o_status)
    query += " ORDER BY id DESC"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(query, params)
        orders = await cur.fetchall()

    page_items, page, total_pages = _paginate(orders, page)
    rows = "".join(
        f"<tr><td>#{o['id']}</td><td>{esc(ORDER_TYPE_TITLES.get(o['order_type'], o['order_type']))}</td>"
        f"<td>{esc(o['status'])}</td><td>{esc(o['stage'] or '—')}</td><td>{o['tg_id']}</td>"
        f"<td>{esc(o['created_at'])}</td>"
        f"<td><a class='btn' href='/orders/{o['id']}'>Ko'rish</a></td></tr>"
        for o in page_items
    ) or "<tr><td colspan=7>Buyurtmalar topilmadi</td></tr>"

    type_opts = "".join(
        f'<option value="{k}" {"selected" if k == o_type else ""}>{esc(v)}</option>'
        for k, v in ORDER_TYPE_TITLES.items()
    )
    status_opts = "".join(
        f'<option value="{s}" {"selected" if s == o_status else ""}>{s}</option>'
        for s in ("pending", "accepted", "rejected")
    )
    body = f"""
    <div class="card">
        <form method="get">
            <label>Turi:</label>
            <select name="type" onchange="this.form.submit()">
                <option value="">— barchasi —</option>{type_opts}
            </select>
            <label>Holati:</label>
            <select name="status" onchange="this.form.submit()">
                <option value="">— barchasi —</option>{status_opts}
            </select>
        </form>
    </div>
    <div class="card"><table>
        <tr><th>ID</th><th>Turi</th><th>Holat</th><th>Bosqich</th><th>TG ID</th><th>Sana</th><th></th></tr>
        {rows}</table>{pager_html(f'/orders?type={o_type}&status={o_status}', page, total_pages)}</div>
    """
    return web_page("Buyurtmalar", body, "/orders")


async def web_order_detail(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    order_id = int(request.match_info["order_id"])
    o = await db_get_order(order_id)
    if not o:
        return web_page("Topilmadi", "<div class='card'>Buyurtma topilmadi.</div>")
    user = await db_get_user(o["tg_id"])
    body_text = format_order_body(o["order_type"], json.loads(o["data"])).replace("\n", "<br>")

    actions = ""
    if o["status"] == "pending":
        actions += f"""
        <form method="post" action="/orders/{order_id}/accept" style="display:inline">
            <button>✅ Qabul qilish</button></form>
        <form method="post" action="/orders/{order_id}/reject" style="display:inline">
            <input type="text" name="reason" placeholder="Rad etish sababi" style="width:220px;display:inline-block">
            <button class="danger">❌ Rad etish</button></form>
        """
    elif o["status"] == "accepted" and o["stage"] in (None, "ishlanmoqda"):
        actions += f"""
        <form method="post" action="/orders/{order_id}/stage/tayyor">
            <button class="warn">🚀 Tayyor deb belgilash (fayl botdan yuboriladi)</button></form>
        """
    elif o["status"] == "accepted" and o["stage"] == "tayyor":
        actions += f"""
        <form method="post" action="/orders/{order_id}/stage/topshirildi">
            <button class="warn">📬 Topshirildi deb belgilash</button></form>
        """

    body = f"""
    <div class="card">
        <p><b>Turi:</b> {esc(ORDER_TYPE_TITLES.get(o['order_type'], o['order_type']))}</p>
        <p><b>Mijoz:</b> {esc(user_display_name(user)) if user else o['tg_id']} (TG ID: {o['tg_id']})</p>
        <p><b>Holat:</b> {esc(o['status'])} &nbsp; <b>Bosqich:</b> {esc(o['stage'] or '—')}</p>
        <p><b>Sana:</b> {esc(o['created_at'])}</p>
        {f"<p><b>Rad etish sababi:</b> {esc(o['reject_reason'])}</p>" if o['reject_reason'] else ""}
        {f"<p><b>Tuzatish so'ralgan:</b> {esc(o['revision_note'])}</p>" if o['revision_note'] else ""}
        <hr style="border-color:#334155">
        <p>{body_text}</p>
    </div>
    <div class="card">{actions or "<p>Bu buyurtma uchun amal talab qilinmaydi.</p>"}
    <p style="color:#64748b;font-size:13px">Eslatma: tayyor faylni mijozga yuborish faqat Telegram bot orqali amalga oshiriladi — "Tayyor" tugmasini bossangiz, botga o'ting va faylni yuboring.</p>
    </div>
    """
    return web_page(f"Buyurtma #{order_id}", body, "/orders")


async def web_order_accept(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    order_id = int(request.match_info["order_id"])
    order = await db_get_order(order_id)
    await db_set_order_status(order_id, "accepted")
    card_num = await db_get_setting("card_number")
    card_owner = await db_get_setting("card_owner")
    await safe_send(
        order["tg_id"],
        f"✅ Xaridingiz tasdiqlandi! Endi to'lovni amalga oshirishingiz mumkin.\n\n"
        f"💳 Karta raqami: {card_num}\n👤 Karta egasi: {card_owner}\n\n"
        f"To'lov qilgach, chek rasmini yoki PDF faylini shu botga yuboring.",
    )
    await get_user_fsm(order["tg_id"]).set_state(UserFlow.awaiting_payment)
    await get_user_fsm(order["tg_id"]).update_data(payment_order_id=order_id)
    await db_log_admin_action(0, "web_accept_order", f"order #{order_id} (web panel)")
    raise web.HTTPFound(f"/orders/{order_id}")


async def web_order_reject(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    order_id = int(request.match_info["order_id"])
    data = await request.post()
    reason = data.get("reason", "").strip() or "Sabab ko'rsatilmagan"
    order = await db_get_order(order_id)
    await db_set_order_status(order_id, "rejected", reason)
    await safe_send(order["tg_id"], f"❌ Xaridingiz admin tomonidan tasdiqlanmadi.\n\nSabab: {reason}")
    await db_log_admin_action(0, "web_reject_order", f"order #{order_id}: {reason} (web panel)")
    raise web.HTTPFound(f"/orders/{order_id}")


async def web_order_stage(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    order_id = int(request.match_info["order_id"])
    stage = request.match_info["stage"]
    if stage not in ("tayyor", "topshirildi"):
        raise web.HTTPFound(f"/orders/{order_id}")
    order = await db_get_order(order_id)
    await db_set_order_stage(order_id, stage)
    if stage == "topshirildi":
        await safe_send(
            order["tg_id"],
            "📬 Buyurtmangiz to'liq topshirildi! Xizmatimizni qanday baholaysiz?",
            reply_markup=order_rating_inline_kb(order_id),
        )
    await db_log_admin_action(0, "web_order_stage", f"order #{order_id} -> {stage} (web panel)")
    raise web.HTTPFound(f"/orders/{order_id}")


# ---------- To'lovlar ----------

async def web_payments(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    page = int(request.query.get("page", 0))
    status_filter = request.query.get("status", "")
    payments = await db_get_payments(status_filter or None)
    page_items, page, total_pages = _paginate(payments, page)
    rows = ""
    for p in page_items:
        user = await db_get_user(p["tg_id"])
        action = (
            f"<form method='post' action='/payments/{p['id']}/confirm'><button>✅ Tasdiqlash</button></form>"
            if p["status"] == "pending" else ""
        )
        rows += (
            f"<tr><td>#{p['id']}</td><td>{esc(user_display_name(user)) if user else p['tg_id']}</td>"
            f"<td>{'#' + str(p['order_id']) if p['order_id'] else '—'}</td>"
            f"<td>{esc(p['status'])}</td><td>{esc(p['created_at'])}</td><td>{action}</td></tr>"
        )
    rows = rows or "<tr><td colspan=6>To'lovlar topilmadi</td></tr>"
    body = f"""
    <div class="card">
        <form method="get">
            <label>Holati:</label>
            <select name="status" onchange="this.form.submit()">
                <option value="">— barchasi —</option>
                <option value="pending" {"selected" if status_filter == "pending" else ""}>pending</option>
                <option value="confirmed" {"selected" if status_filter == "confirmed" else ""}>confirmed</option>
            </select>
        </form>
    </div>
    <div class="card"><table>
        <tr><th>ID</th><th>Mijoz</th><th>Buyurtma</th><th>Holat</th><th>Sana</th><th></th></tr>
        {rows}</table>{pager_html(f'/payments?status={status_filter}', page, total_pages)}</div>
    """
    return web_page("To'lovlar", body, "/payments")


async def web_payment_confirm(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    pid = int(request.match_info["payment_id"])
    p = await db_get_payment(pid)
    await db_confirm_payment(pid)
    if p["order_id"]:
        await db_set_order_stage(p["order_id"], "ishlanmoqda")
        await safe_send(p["tg_id"], "✅ To'lovingiz tasdiqlandi!\n\n🔧 Buyurtmangiz ustida ish boshlandi.")
    else:
        await safe_send(p["tg_id"], "✅ To'lovingiz tasdiqlandi! Tez orada admin siz bilan bog'lanadi.")
    await db_log_admin_action(0, "web_confirm_payment", f"payment #{pid} (web panel)")
    raise web.HTTPFound("/payments")


# ---------- Fikr va shikoyatlar ----------

async def web_feedback(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    page = int(request.query.get("page", 0))
    items = await db_get_feedback_list()
    page_items, page, total_pages = _paginate(items, page)
    rows = ""
    for f in page_items:
        user = await db_get_user(f["tg_id"])
        rows += (
            f"<tr><td>#{f['id']}</td><td>{esc(user_display_name(user)) if user else f['tg_id']}</td>"
            f"<td>{esc(f['kind'])}</td><td>{esc((f['text'] or '')[:80])}</td>"
            f"<td>{'✅' if f['admin_reply'] else '—'}</td>"
            f"<td><a class='btn' href='/feedback/{f['id']}'>Ko'rish</a></td></tr>"
        )
    rows = rows or "<tr><td colspan=6>Fikrlar yo'q</td></tr>"
    body = f"""<div class="card"><table>
        <tr><th>ID</th><th>Foydalanuvchi</th><th>Turi</th><th>Matn</th><th>Javob</th><th></th></tr>
        {rows}</table>{pager_html('/feedback?x=1', page, total_pages)}</div>"""
    return web_page("Fikr va shikoyatlar", body, "/feedback")


async def web_feedback_detail(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    fid = int(request.match_info["feedback_id"])
    f = await db_get_feedback(fid)
    if not f:
        return web_page("Topilmadi", "<div class='card'>Topilmadi.</div>")
    user = await db_get_user(f["tg_id"])
    body = f"""
    <div class="card">
        <p><b>Foydalanuvchi:</b> {esc(user_display_name(user)) if user else f['tg_id']}</p>
        <p><b>Turi:</b> {esc(f['kind'])}</p>
        <p><b>Matn:</b> {esc(f['text'])}</p>
        {f"<p><b>Admin javobi:</b> {esc(f['admin_reply'])}</p>" if f['admin_reply'] else ""}
        <form method="post" action="/feedback/{fid}/reply">
            <textarea name="reply_text" rows="3" placeholder="Javob yozing..."></textarea>
            <button>✅ Javob yuborish</button>
        </form>
    </div>
    """
    return web_page(f"Fikr #{fid}", body, "/feedback")


async def web_feedback_reply(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    fid = int(request.match_info["feedback_id"])
    data = await request.post()
    reply_text = data.get("reply_text", "").strip()
    if reply_text:
        f = await db_get_feedback(fid)
        await db_set_feedback_reply(fid, reply_text)
        await safe_send(f["tg_id"], f"💬 Admin javobi:\n\n{reply_text}")
        await db_log_admin_action(0, "web_feedback_reply", f"feedback #{fid} (web panel)")
    raise web.HTTPFound(f"/feedback/{fid}")


# ---------- Chatlar ----------

async def web_chats(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    page = int(request.query.get("page", 0))
    chat_users = await db_get_chat_users()
    page_items, page, total_pages = _paginate(chat_users, page)
    rows = ""
    for cu in page_items:
        user = await db_get_user(cu["tg_id"])
        name = esc(user_display_name(user)) if user else str(cu["tg_id"])
        rows += (
            f"<tr><td>{name}</td><td>{cu['tg_id']}</td>"
            f"<td><a class='btn' href='/chats/{cu['tg_id']}'>Ochish</a></td></tr>"
        )
    rows = rows or "<tr><td colspan=3>Chatlar yo'q</td></tr>"
    body = f"""<div class="card"><table>
        <tr><th>Foydalanuvchi</th><th>TG ID</th><th></th></tr>
        {rows}</table>{pager_html('/chats?x=1', page, total_pages)}</div>"""
    return web_page("Chatlar", body, "/chats")


async def web_chat_detail(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    tg_id = int(request.match_info["tg_id"])
    user = await db_get_user(tg_id)
    messages = await db_get_messages(tg_id, limit=30)
    msg_rows = "".join(
        f"<p><b>{'👤 Mijoz' if m['direction'] == 'in' else '👨‍💼 Admin'}:</b> {esc(m['text'])} "
        f"<span style='color:#64748b;font-size:12px'>({esc(m['created_at'])})</span></p>"
        for m in reversed(messages)
    ) or "<p>Xabarlar yo'q</p>"
    body = f"""
    <div class="card">
        <p><b>Foydalanuvchi:</b> {esc(user_display_name(user)) if user else tg_id}</p>
        {msg_rows}
        <form method="post" action="/chats/{tg_id}/reply">
            <textarea name="reply_text" rows="3" placeholder="Xabar yozing..."></textarea>
            <button>✅ Yuborish</button>
        </form>
    </div>
    """
    return web_page(f"Chat — {tg_id}", body, "/chats")


async def web_chat_reply(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    tg_id = int(request.match_info["tg_id"])
    data = await request.post()
    reply_text = data.get("reply_text", "").strip()
    if reply_text:
        ok = await safe_send(tg_id, f"👨‍💼 Admin:\n\n{reply_text}")
        if ok:
            await db_add_message(tg_id, "out", reply_text)
        await db_log_admin_action(0, "web_chat_reply", f"to ID:{tg_id} (web panel)")
    raise web.HTTPFound(f"/chats/{tg_id}")


# ---------- O'yin ----------

async def web_game(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    started = await db_get_setting("game_started")
    enabled = await db_get_setting("game_enabled")
    count = await db_game_count()
    participants = await db_game_list_participants()
    rows = ""
    for p in participants:
        user = await db_get_user(p["tg_id"])
        pick_form = (
            f"<form method='post' action='/game/pick/{p['game_number']}' style='display:inline'>"
            f"<button class='warn'>🎯 G'olib deb belgilash</button></form>"
        ) if not p["is_winner"] else "<span class='pill ok'>🏆 G'olib</span>"
        rows += (
            f"<tr><td>#{p['game_number']}</td><td>{esc(user_display_name(user)) if user else p['tg_id']}</td>"
            f"<td>{esc(p['created_at'])}</td><td>{pick_form}</td></tr>"
        )
    rows = rows or "<tr><td colspan=4>Ishtirokchilar yo'q</td></tr>"

    body = f"""
    <div class="card">
        <p>📊 Jami ID olganlar: <b>{count}</b></p>
        <p>▶️ O'yin holati: {"🟢 Boshlangan" if started == "1" else "⚪️ Boshlanmagan"}</p>
        <p>🔌 Ishlash holati: {"🟢 Yoqilgan" if enabled == "1" else "🔴 O'chirilgan"}</p>
        <form method="post" action="/game/toggle" style="display:inline"><button>🔌 Yoqish/O'chirish</button></form>
        <form method="post" action="/game/start" style="display:inline"><button class="warn">▶️ Boshlash</button></form>
        <form method="post" action="/game/end" style="display:inline"><button class="warn">⏹ Tugatish</button></form>
        <form method="post" action="/game/reset" style="display:inline" onsubmit="return confirm('Barcha ID raqamlar o\\'chiriladi. Davom etilsinmi?')">
            <button class="danger">🔄 Restart</button></form>
    </div>
    <div class="card"><h3>👥 Ishtirokchilar</h3><table>
        <tr><th>ID</th><th>Foydalanuvchi</th><th>Sana</th><th></th></tr>{rows}
    </table></div>
    """
    return web_page("Logo o'yini", body, "/game")


async def web_game_toggle(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    current = await db_get_setting("game_enabled")
    await db_set_setting("game_enabled", "0" if current == "1" else "1")
    raise web.HTTPFound("/game")


async def web_game_start(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    await db_set_setting("game_started", "1")
    await db_log_admin_action(0, "web_game_start", "web panel")
    raise web.HTTPFound("/game")


async def web_game_end(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    await db_set_setting("game_started", "0")
    await db_log_admin_action(0, "web_game_end", "web panel")
    raise web.HTTPFound("/game")


async def web_game_reset(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    await db_game_reset()
    await db_log_admin_action(0, "web_game_reset", "web panel")
    raise web.HTTPFound("/game")


async def web_game_pick(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    number = int(request.match_info["number"])
    participant = await db_game_get_by_number(number)
    if participant:
        await db_game_set_winner(number)
        await safe_send(
            participant["tg_id"],
            "🎉 Tabriklaymiz, siz yutdingiz!\n\nSiz bilan tez orada admin bog'lanadi.",
        )
        await db_log_admin_action(0, "web_game_pick", f"#{number} (web panel)")
    raise web.HTTPFound("/game")


# ---------- Xabar yuborish ----------

async def web_broadcast(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    body = """
    <div class="card">
        <form method="post" action="/broadcast/send">
            <textarea name="text" rows="5" placeholder="Xabar matnini yozing..."></textarea>
            <label>Kimlarga:</label>
            <select name="segment">
                <option value="all">👥 Hammaga</option>
                <option value="ordered">📦 Faqat buyurtma berganlarga</option>
            </select>
            <button>🚀 Hozir yuborish</button>
        </form>
    </div>
    """
    return web_page("Xabar yuborish", body, "/broadcast")


async def web_broadcast_send(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    data = await request.post()
    text = data.get("text", "").strip()
    segment = data.get("segment", "all")
    if not text:
        raise web.HTTPFound("/broadcast")
    users = await db_get_users_with_orders() if segment == "ordered" else await db_get_all_users()
    sent = 0
    for u in users:
        if u["is_blocked"]:
            continue
        if await safe_send(u["tg_id"], f"📢 E'lon:\n\n{text}"):
            sent += 1
        await asyncio.sleep(0.03)
    await db_log_admin_action(0, "web_broadcast", f"segment={segment}, sent={sent} (web panel)")
    body = f"<div class='card'>✅ Xabar {sent} ta foydalanuvchiga yuborildi. <a class='btn' href='/broadcast'>Orqaga</a></div>"
    return web_page("Xabar yuborildi", body, "/broadcast")


# ---------- Sozlamalar ----------

async def web_settings(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    rows = ""
    for key in DEFAULT_SETTINGS.keys():
        value = await db_get_setting(key)
        rows += f"""
        <form method="post" action="/settings/update">
            <label>{esc(key)}</label>
            <input type="hidden" name="key" value="{esc(key)}">
            <textarea name="value" rows="2">{esc(value or '')}</textarea>
            <button>💾 Saqlash</button>
        </form>
        """
    body = f"<div class='card'>{rows}</div>"
    return web_page("Sozlamalar", body, "/settings")


async def web_settings_update(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    data = await request.post()
    key = data.get("key", "")
    value = data.get("value", "")
    if key in DEFAULT_SETTINGS:
        await db_set_setting(key, value)
        await db_log_admin_action(0, "web_settings_update", f"{key} (web panel)")
    raise web.HTTPFound("/settings")


# ---------- Adminlar ----------

async def web_admins(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    admins = await db_get_admins()
    super_rows = "".join(f"<tr><td>{aid}</td><td>Super-admin (.env)</td><td>—</td></tr>" for aid in ADMIN_IDS)
    admin_rows = "".join(
        f"<tr><td>{a['tg_id']}</td><td>Oddiy admin</td>"
        f"<td><form method='post' action='/admins/{a['tg_id']}/remove'>"
        f"<button class='danger'>➖ Olib tashlash</button></form></td></tr>"
        for a in admins
    )
    rows = super_rows + admin_rows or "<tr><td colspan=3>Adminlar yo'q</td></tr>"
    body = f"""
    <div class="card"><table><tr><th>TG ID</th><th>Roli</th><th></th></tr>{rows}</table></div>
    <div class="card">
        <form method="post" action="/admins/add">
            <input type="text" name="tg_id" placeholder="Yangi admin Telegram ID">
            <button>➕ Admin qo'shish</button>
        </form>
    </div>
    """
    return web_page("Adminlar", body, "/admins")


async def web_admins_add(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    data = await request.post()
    tg_id_raw = data.get("tg_id", "").strip()
    if tg_id_raw.isdigit():
        new_id = int(tg_id_raw)
        if not is_super_admin(new_id):
            await db_add_admin(new_id, 0)
            await safe_send(new_id, "🎉 Siz MHDV botida admin etib tayinlandingiz! Botga /start yuboring.")
            await db_log_admin_action(0, "web_add_admin", f"ID:{new_id} (web panel)")
    raise web.HTTPFound("/admins")


async def web_admins_remove(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    tg_id = int(request.match_info["tg_id"])
    if not is_super_admin(tg_id):
        await db_remove_admin(tg_id)
        await safe_send(tg_id, "ℹ️ Sizning admin huquqingiz olib tashlandi.")
        await db_log_admin_action(0, "web_remove_admin", f"ID:{tg_id} (web panel)")
    raise web.HTTPFound("/admins")


# ---------- Loglar ----------

async def web_logs(request):
    if not _check_basic_auth(request):
        return _unauthorized()
    logs = await db_get_admin_logs(200)
    rows = "".join(
        f"<tr><td>{esc(l['created_at'])}</td><td>{l['admin_id']}</td>"
        f"<td>{esc(l['action'])}</td><td>{esc(l['details'])}</td></tr>"
        for l in logs
    ) or "<tr><td colspan=4>Loglar yo'q</td></tr>"
    body = f"""<div class="card"><table>
        <tr><th>Vaqt</th><th>Admin ID</th><th>Amal</th><th>Tafsilot</th></tr>{rows}
    </table><p style="color:#64748b;font-size:13px">Admin ID: 0 — web panel orqali bajarilgan amal.</p></div>"""
    return web_page("Adminlar tarixi", body, "/logs")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/miniapp", miniapp)
    app.router.add_get("/miniapp/api/me", miniapp_api_me)
    app.router.add_post("/miniapp/api/orders", miniapp_api_order)
    app.router.add_post("/miniapp/api/orders/{order_id}/revision", miniapp_api_revision)
    app.router.add_post("/miniapp/api/orders/{order_id}/rating", miniapp_api_order_rating)
    app.router.add_post("/miniapp/api/orders/{order_id}/cancel", miniapp_api_order_cancel)
    app.router.add_post("/miniapp/api/payment", miniapp_api_payment)
    app.router.add_post("/miniapp/api/feedback", miniapp_api_feedback)
    app.router.add_post("/miniapp/api/chat", miniapp_api_chat)
    app.router.add_post("/miniapp/api/rate", miniapp_api_rate)
    app.router.add_post("/miniapp/api/game/join", miniapp_api_game_join)
    app.router.add_get("/miniapp/api/admin/overview", miniapp_api_admin_overview)
    app.router.add_post("/miniapp/api/admin/orders/{order_id}/action", miniapp_api_admin_order_action)
    app.router.add_post("/miniapp/api/admin/payments/{payment_id}/confirm", miniapp_api_admin_payment_confirm)
    app.router.add_post("/miniapp/api/admin/users/{tg_id}/toggle", miniapp_api_admin_user_toggle)
    app.router.add_post("/miniapp/api/admin/chat/{tg_id}/reply", miniapp_api_admin_chat_reply)
    app.router.add_post("/miniapp/api/admin/feedback/{feedback_id}/reply", miniapp_api_admin_feedback_reply)
    app.router.add_post("/miniapp/api/admin/broadcast", miniapp_api_admin_broadcast)
    app.router.add_get("/dashboard", dashboard)
    app.router.add_get("/users", web_users)
    app.router.add_get("/users/{tg_id}", web_user_detail)
    app.router.add_post("/users/{tg_id}/toggle-block", web_user_toggle_block)
    app.router.add_get("/orders", web_orders)
    app.router.add_get("/orders/{order_id}", web_order_detail)
    app.router.add_post("/orders/{order_id}/accept", web_order_accept)
    app.router.add_post("/orders/{order_id}/reject", web_order_reject)
    app.router.add_post("/orders/{order_id}/stage/{stage}", web_order_stage)
    app.router.add_get("/payments", web_payments)
    app.router.add_post("/payments/{payment_id}/confirm", web_payment_confirm)
    app.router.add_get("/feedback", web_feedback)
    app.router.add_get("/feedback/{feedback_id}", web_feedback_detail)
    app.router.add_post("/feedback/{feedback_id}/reply", web_feedback_reply)
    app.router.add_get("/chats", web_chats)
    app.router.add_get("/chats/{tg_id}", web_chat_detail)
    app.router.add_post("/chats/{tg_id}/reply", web_chat_reply)
    app.router.add_get("/game", web_game)
    app.router.add_post("/game/toggle", web_game_toggle)
    app.router.add_post("/game/start", web_game_start)
    app.router.add_post("/game/end", web_game_end)
    app.router.add_post("/game/reset", web_game_reset)
    app.router.add_post("/game/pick/{number}", web_game_pick)
    app.router.add_get("/broadcast", web_broadcast)
    app.router.add_post("/broadcast/send", web_broadcast_send)
    app.router.add_get("/settings", web_settings)
    app.router.add_post("/settings/update", web_settings_update)
    app.router.add_get("/admins", web_admins)
    app.router.add_post("/admins/add", web_admins_add)
    app.router.add_post("/admins/{tg_id}/remove", web_admins_remove)
    app.router.add_get("/logs", web_logs)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info(f"Health-check server {PORT}-portda ishga tushdi (Render uchun).")


async def main():
    await init_db()
    await start_web_server()
    if WEBAPP_URL:
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="🚀 Mini-ilova",
                    web_app=WebAppInfo(url=f"{WEBAPP_URL}/miniapp"),
                )
            )
            logger.info(f"Mini-ilova tugmasi sozlandi: {WEBAPP_URL}/miniapp")
        except Exception as e:
            logger.warning(f"Mini-ilova menu tugmasini sozlab bo'lmadi: {e}")
    asyncio.create_task(reminder_loop())
    logger.info("Bot polling boshlandi...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot to'xtatildi.")