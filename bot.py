# -*- coding: utf-8 -*-
from __future__ import annotations
from zoneinfo import ZoneInfo
from datetime import datetime, timezone

TEHRAN_TZ = ZoneInfo("Asia/Tehran")

def utc_sqlite_to_tehran(ts: str | None) -> str:
    if not ts:
        return "-"
    try:
        # SQLite: "YYYY-MM-DD HH:MM:SS" (UTC)
        dt_utc = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt_utc.astimezone(TEHRAN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        # Fallback if format is slightly different (e.g. ISO) or invalid
        return ts or "-"

# Backward-compatible alias (used by some parts of the code)
to_tehran = utc_sqlite_to_tehran

from dotenv import load_dotenv
load_dotenv()
import sqlite3
import os
import paramiko
import subprocess
import asyncio
import time
from datetime import datetime
from typing import Optional

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, StateFilter
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from utils.ssh_init import init_ssh_files
from db import init, db
from crypto import enc, dec
from ssh import reboot
from states import AddServer, AdminAdd
from monitor import loop as monitor_loop
from checkhost import run_ping_check, CheckHostError


# ---------------- FSM: Log retention ----------------
class LogRetention(StatesGroup):
    days = State()
class BotSettings(StatesGroup):
    waiting_for_ping_int = State()
class EditServer(StatesGroup):
    new_name = State()

# ---------------- Config ----------------
OWNER = int(os.getenv("OWNER_ID") or os.getenv("OWNER") or "0")
BOT_TOKEN = os.getenv("BOT_TOKEN")

BOT_HEADER = (
    "🎛 Server system guard\n"
    "💎 | Version Bot: 1.6\n"
    "🔹 | creator: @farhadasqarii"
)
BOT_NAME = "🎛 Server system guard"

init()
bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ---------------- Role / Users ----------------
def get_role(uid: int) -> str:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT role FROM users WHERE uid=?", (uid,))
    r = cur.fetchone()
    conn.close()
    return r["role"] if r else "viewer"

def ensure_user(uid: int) -> None:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT role FROM users WHERE uid=?", (uid,))
    r = cur.fetchone()
    if not r:
        role = "viewer"
        if OWNER and uid == OWNER:
            role = "owner"
        elif not OWNER:
            # if no OWNER env, first ever user becomes owner
            cur.execute("SELECT uid FROM users WHERE role='owner' LIMIT 1")
            if not cur.fetchone():
                role = "owner"
        cur.execute("INSERT INTO users(uid,role) VALUES (?,?)", (uid, role))
        conn.commit()
    conn.close()


def get_owner_id() -> int:
    if OWNER:
        return OWNER
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT uid FROM users WHERE role='owner' LIMIT 1")
    r = cur.fetchone()
    conn.close()
    return int(r["uid"]) if r else 0


def is_privileged(uid: int) -> bool:
    return get_role(uid) in ("owner", "admin")


# ---------------- Settings (DB) ----------------
def _ensure_settings_table() -> None:
    conn = db()
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT)")
    conn.commit()
    conn.close()


def get_setting(key: str, default: str) -> str:
    _ensure_settings_table()
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT v FROM settings WHERE k=?", (key,))
    r = cur.fetchone()
    conn.close()
    return (r["v"] if r is not None and r["v"] is not None else default)


def set_setting(key: str, value: str) -> None:
    _ensure_settings_table()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO settings(k,v) VALUES (?,?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, value),
    )
    conn.commit()
    conn.close()

def post_add_server_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت به لیست سرورها", callback_data="servers")],
        [InlineKeyboardButton(text="🏠 منوی اصلی", callback_data="home")]
    ])

def get_log_retention_days() -> int:
    try:
        v = int(get_setting("log_retention_days", "7"))
        return max(1, min(365, v))
    except Exception:
        return 7

def get_ping_interval() -> int:
    try:
        # خواندن از تنظیمات دیتابیس، اگر نبود پیش‌فرض 30 ثانیه
        v = int(get_setting("ping_interval", "30"))
        return max(10, v) # اجازه ندهید کمتر از 10 ثانیه شود تا سرور زیر فشار نرود
    except:
        return 30

async def cleanup_logs_once(days: int) -> int:
    """پاک‌سازی همزمان لاگ‌های سیستمی و تاریخچه پایش ایران"""
    conn = db()
    cur = conn.cursor()
    try:
        # ۱. شمارش و حذف لاگ‌های عمومی (logs)
        cur.execute("SELECT COUNT(*) AS c FROM logs WHERE ts < datetime('now', ?)", (f"-{days} day",))
        before = cur.fetchone()["c"]
        cur.execute("DELETE FROM logs WHERE ts < datetime('now', ?)", (f"-{days} day",))
        
        # ۲. حذف تاریخچه پایش (ch_history) - بدون تأثیر در شمارش قبل/بعد
        try:
            cur.execute("DELETE FROM ch_history WHERE ts < datetime('now', ?)", (f"-{days} day",))
        except Exception:
            pass # اگر جدول هنوز ساخته نشده بود
            
        conn.commit()
        return int(before or 0)
    finally:
        conn.close()

async def cleanup_logs_job():
    """پاک‌سازی دوره‌ای (هر ۲۴ ساعت یک‌بار)"""
    while True:
        try:
            # خواندن تعداد روز از تنظیمات پنل (پیش‌فرض ۷ روز)
            days = get_log_retention_days()
            await cleanup_logs_once(days)
            print(f"--- [Maintenance] Auto-cleanup done for {days} days old data. ---")
        except Exception as e:
            print(f"--- [Maintenance Error] {e} ---")
            
        # ۲۴ ساعت انتظار تا اجرای بعدی
        await asyncio.sleep(24 * 60 * 60)

async def get_system_usage(host, port, user, pw):
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        # حتماً باید از تابع dec استفاده کنیم تا رمز واقعی استخراج شود
        try:
            real_password = dec(pw)
        except Exception as e:
            print(f"Decryption Error: {e}")
            real_password = pw # اگر دکود نشد، خود پسورد (که البته در این حالت کار نخواهد کرد)
            
        # اتصال با پسورد واقعی
        ssh.connect(host, port=int(port), username=user, password=real_password, timeout=10)
        
        # اجرای دستورات دریافت منابع
        cmd = "top -bn1 | grep 'Cpu(s)' | awk '{print $2}' && free | grep Mem | awk '{print $3/$2 * 100.0}'"
        stdin, stdout, stderr = ssh.exec_command(cmd)
        res = stdout.read().decode().splitlines()
        ssh.close()
        
        if len(res) >= 2:
            return float(res[0]), float(res[1])
    except Exception as e:
        print(f"SSH Connection Error: {e}")
    return None, None

async def notify_owner_new_viewer(m: types.Message) -> None:
    """Notify owner when a non-admin/non-owner starts the bot."""
    oid = get_owner_id()
    if not oid or oid == m.from_user.id:
        return

    u = m.from_user
    username = f"@{u.username}" if u.username else "(ندارد)"
    text = (
        BOT_HEADER
        + "\n\n🚨 یک کاربر غیرادمین ربات را استارت کرد.\n\n"
        + f"🆔 ID: `{u.id}`\n"
        + f"👤 Name: {u.full_name}\n"
        + f"🔗 Username: {username}"
    )
    try:
        await bot.send_message(oid, text, parse_mode="Markdown")
    except Exception:
        pass


# ---------------- UI helpers ----------------
async def _edit_menu(msg: types.Message, text: str, reply_markup=None, parse_mode: Optional[str] = None):
    """
    Prefer editing the existing message (no new post). Fallback to sending a new message
    if edit is not allowed (rare).
    """
    try:
        await msg.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        return
    except Exception:
        pass
    try:
        await msg.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception:
        pass

def main_kb(role: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📊 داشبورد", callback_data="dashboard")],
        [InlineKeyboardButton(text="📋 سرورها", callback_data="servers")],
    ]
    
    if role == "owner":
        # فقط دکمه تنظیمات کلی را اینجا می‌گذاریم
        rows.append([InlineKeyboardButton(text="🌐 پایش ایران (Check-Host)", callback_data="ch_menu")])
        rows.append([InlineKeyboardButton(text="⚙️ تنظیمات ربات", callback_data="bot_settings")])
    
    return InlineKeyboardMarkup(inline_keyboard=rows)

def settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 مدیریت ادمین‌ها", callback_data="admin_panel")],
        [InlineKeyboardButton(text="🧹 مدیریت لاگ‌ها", callback_data="log_admin")],
        [InlineKeyboardButton(text="⏱ زمان پایش سرورها", callback_data="set_ping_int")],
             [InlineKeyboardButton(text="📜 لاگ‌های سیستم", callback_data="logs")],
        [InlineKeyboardButton(text="🔙 بازگشت به منوی اصلی", callback_data="home")],
   
    ])

def servers_list_kb(servers, role: str) -> InlineKeyboardMarkup:
    # تغییر srv: به status: برای هماهنگی با هندلر جدید
    rows = [[InlineKeyboardButton(text=f"🖥 {s['name']}", callback_data=f"status:{int(s['id'])}")] for s in servers]
    
    if role in ("owner", "admin"):
        rows.append([InlineKeyboardButton(text="➕ افزودن سرور جدید", callback_data="add")])
        
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def status_kb(sid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⚡ پینگ", callback_data=f"test:{sid}"),
            InlineKeyboardButton(text="📊 منابع سرور", callback_data=f"usage:{sid}"), # دکمه جدید
            InlineKeyboardButton(text="📊 آمار", callback_data=f"stats:{sid}")
        ],
        [
            InlineKeyboardButton(text="🔄 ریستارت", callback_data=f"reboot:{sid}"),
            InlineKeyboardButton(text="📝 ویرایش", callback_data=f"edit_name:{sid}"),
            InlineKeyboardButton(text="🗑 حذف", callback_data=f"del:{sid}")
        ],
        [InlineKeyboardButton(text="🔙 بازگشت به لیست", callback_data="servers")]
    ])

def log_admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧹 پاک‌سازی لاگ‌های قدیمی", callback_data="log_cleanup")],
            [InlineKeyboardButton(text="⏱ تنظیم تعداد روز نگهداری", callback_data="log_set_retention")],
            [InlineKeyboardButton(text="📦 آرشیو لاگ‌ها به فایل", callback_data="log_export")],
            [InlineKeyboardButton(text="🔙 بازگشت", callback_data="bot_settings")],
        ]
    )


def log_set_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ لغو", callback_data="cancel_fsm")],
            [InlineKeyboardButton(text="🔙 بازگشت", callback_data="log_admin")],
        ]
    )


def admin_panel_kb(users) -> InlineKeyboardMarkup:
    rows = []
    for u in users:
        uid = int(u["uid"])
        role = u["role"]
        label = "👑 Owner" if role == "owner" else ("🛡 Admin" if role == "admin" else "")
        rows.append([InlineKeyboardButton(text=f"{label} | {uid}", callback_data=f"admin_user:{uid}")])
    rows.append([InlineKeyboardButton(text="➕ افزودن Admin", callback_data="admin_add")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="bot_settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_user_kb(uid: int, role: str) -> InlineKeyboardMarkup:
    rows = []
    if role != "owner":
        rows.append([InlineKeyboardButton(text="🛡 تبدیل به Admin", callback_data=f"setrole:{uid}:admin")])
        rows.append([InlineKeyboardButton(text="❌ حذف کاربر", callback_data=f"rmuser:{uid}")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_add_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ لغو", callback_data="cancel_fsm")],
            [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_panel")],
        ]
    )


def add_server_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            # تغییر callback_data به "servers" برای بازگشت به منوی قبلی
            [InlineKeyboardButton(text="🔙 بازگشت به لیست سرورها", callback_data="servers")],
            [InlineKeyboardButton(text="❌ لغو و منوی اصلی", callback_data="home")],
        ]
    )

def badge(st: str) -> str:
    return "🟢 UP" if st == "UP" else "🔴 DOWN"


# ---------------- Guards ----------------
async def guard_cb(cb: types.CallbackQuery) -> bool:
    ensure_user(cb.from_user.id)
    if not is_privileged(cb.from_user.id):
        try:
            await cb.answer()
        except Exception:
            pass
        return False
    return True


async def guard_msg(m: types.Message) -> bool:
    ensure_user(m.from_user.id)
    if not is_privileged(m.from_user.id):
        return False
    return True


# ---------------- Handlers ----------------
@dp.message(CommandStart())
async def start(m: types.Message, state: FSMContext):
    await state.clear()
    ensure_user(m.from_user.id)
    if not is_privileged(m.from_user.id):
        await notify_owner_new_viewer(m)
        return
    role = get_role(m.from_user.id)
    await m.answer(BOT_HEADER + "\n\n" + BOT_NAME, reply_markup=main_kb(role))


@dp.callback_query(F.data == "home")
async def home(cb: types.CallbackQuery):
    if not await guard_cb(cb):
        return
    role = get_role(cb.from_user.id)
    await _edit_menu(cb.message, BOT_HEADER + "\n\n" + BOT_NAME, reply_markup=main_kb(role))
    await cb.answer()

@dp.callback_query(F.data == "dashboard")
async def dashboard(cb: types.CallbackQuery):
    if not await guard_cb(cb): return
    
    conn = db(); conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    # اولویت با سرورهای آفلاین + مرتب‌سازی بر اساس ID
    cur.execute(
        "SELECT s.name, s.host, ss.last_status "
        "FROM servers s LEFT JOIN server_status ss ON ss.server_id=s.id "
        "ORDER BY CASE WHEN ss.last_status = 'up' THEN 1 ELSE 0 END ASC, s.id DESC"
    )
    rows = cur.fetchall(); conn.close()

    total = len(rows)
    up = sum(1 for r in rows if str(r["last_status"]).lower() == "up")
    down = total - up
    health = (up / total) * 100 if total > 0 else 0

    # هدر گرافیکی
    text = (
        f"<b>🛰 SERVER COMMAND CENTER</b>\n"
        f"<code>──────────────────────────────</code>\n"
        f"<b>📊 SYSTEM HEALTH: {health:.1f}%</b>\n"
        f"<code>🟢 {up:02d} ONLINE  │  🔴 {down:02d} OFFLINE</code>\n"
        f"<code>──────────────────────────────</code>\n"
        f"<b>📍 NODES (Tap IP to Copy):</b>\n"
    )

    for r in rows:
        st = str(r["last_status"]).upper() if r["last_status"] else "DOWN"
        icon = "🔷" if st == "UP" else "🔻"
        
        # تراز کردن نام سرور
        name = (r['name'][:8] + "…") if len(r['name']) > 9 else r['name'].ljust(9)
        
        # ساخت سطر: آیکون | نام | وضعیت | آی‌پی (قابل کپی)
        # استفاده از تگ code برای آی‌پی باعث می‌شود با لمس کپی شود
        text += f"{icon} <code>{name}</code> ➜ <code>{r['host']}</code>\n"

    text += (
        f"<code>──────────────────────────────</code>\n"
        f"<i>🕒 Last Sync: {datetime.now(TEHRAN_TZ).strftime('%H:%M:%S')}</i>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 اسکن مجدد", callback_data="dashboard")],
        [InlineKeyboardButton(text="🔙 بازگشت به خانه", callback_data="home")]
    ])

    await _edit_menu(cb.message, text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()

@dp.callback_query(F.data == "servers")
async def back_to_servers(cb: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if "last_msg_id" in data:
        try:
            # پاک کردن پیامِ "نام را وارد کنید" یا مراحل دیگر
            await cb.bot.delete_message(chat_id=cb.message.chat.id, message_id=data["last_msg_id"])
        except: pass
        
    await state.clear()        
    role = get_role(cb.from_user.id)
    
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id,name,host,port FROM servers ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()

    if not rows:
        kb = []
        if role in ("owner", "admin"):
            kb.append([InlineKeyboardButton(text="➕ افزودن اولین سرور", callback_data="add")])
        kb.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="home")])
        
        await _edit_menu(
            cb.message,
            BOT_HEADER + "\n\n📋 سرورها\n\nهیچ سروری ثبت نشده.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        )
        await cb.answer()
        return

    await _edit_menu(
        cb.message, 
        BOT_HEADER + "\n\n📋 لیست سرورها", 
        reply_markup=servers_list_kb(rows, role)
    )
    await cb.answer()

@dp.callback_query(F.data.startswith("srv:"))
async def server_detail(cb: types.CallbackQuery):
    if not await guard_cb(cb): return
    
    srv_id = int(cb.data.split(":")[1])
    conn = db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM servers WHERE id = ?", (srv_id,))
    srv = cur.fetchone()
    conn.close()

    if not srv:
        await cb.answer("❌ سرور یافت نشد.", show_alert=True)
        return

    # برای هماهنگی با وضعیت واقعی، بهتر است کد این بخش را مشابه هندلر status کنید
    text = (
        f"{BOT_HEADER}\n\n"
        f"🖥 **جزئیات سرور:** {srv['name']}\n"
        f"━━━━━━━━━━━━━━\n"
        f"🌐 **آدرس:** `{srv['host']}`\n"
        f"📡 **وضعیت:** در حال بارگذاری...\n"
        f"━━━━━━━━━━━━━━"
    )

    # به جای تعریف کیبورد تکراری، از تابع اصلی استفاده می‌کنیم
    await _edit_menu(cb.message, text, reply_markup=status_kb(srv_id))
    await cb.answer()

@dp.callback_query(F.data.startswith("stats:"))
async def stats_handler(cb: types.CallbackQuery):
    sid = int(cb.data.split(":")[1])
    conn = db()
    cur = conn.cursor()
    
    # من نام ستون اول را از SELECT حذف کردم و کل ستون‌ها را می‌گیرم تا خطا ندهد
    cur.execute("SELECT * FROM logs WHERE server_id = ? ORDER BY id DESC LIMIT 5", (sid,))
    rows = cur.fetchall()
    conn.close()
    
    txt = "📊 **آخرین گزارشات:**\n\n"
    if not rows:
        txt += "داده‌ای یافت نشد."
    else:
        for r in rows:
            # r[-1] معمولاً زمان و r[1] معمولاً متن لاگ است در اکثر دیتابیس‌ها
            txt += f"🔹 {r[-1]}: {r[1]}\n"
            
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"status:{sid}")]])
    await _edit_menu(cb.message, txt, reply_markup=kb)


@dp.callback_query(F.data.startswith("test:"))
async def test_ping_handler(cb: types.CallbackQuery):
    sid = int(cb.data.split(":")[1])
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT host FROM servers WHERE id=?", (sid,))
    r = cur.fetchone()
    conn.close()
    
    host = r[0]
    # استفاده از فلگ -W 1 (تایم اوت ۱ ثانیه) برای سرعت بیشتر
    check = subprocess.run(['ping', '-c', '1', '-W', '1', host], stdout=subprocess.PIPE)
    
    if check.returncode == 0:
        await cb.answer(f"✅ آنلاین\nپاسخ از {host} دریافت شد.", show_alert=True)
    else:
        await cb.answer(f"❌ آفلاین\nسرور {host} هیچ پاسخی نداد.", show_alert=True)


@dp.callback_query(F.data.startswith("status:"))
async def status(cb: types.CallbackQuery):
    if not await guard_cb(cb): return
    sid = int(cb.data.split(":")[1])

    conn = db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT s.name, s.host, ss.last_status, ss.last_check_ts "
        "FROM servers s "
        "LEFT JOIN server_status ss ON ss.server_id = s.id "
        "WHERE s.id = ?",
        (sid,),
    )
    r = cur.fetchone()
    conn.close()

    if not r:
        await cb.answer("سرور پیدا نشد", show_alert=True)
        return

    # اصلاح وضعیت پایش (تبدیل به حروف کوچک برای مقایسه درست)
    db_status = str(r['last_status']).lower() if r['last_status'] else ""
    if db_status == "up":
        st_text = "✅ آنلاین"
    elif db_status == "down":
        st_text = "❌ آفلاین"
    else:
        st_text = "🔄 در انتظار بررسی"

    # اصلاح زمان به وقت تهران
    last_check_utc = r['last_check_ts']
    last_check_tehran = utc_sqlite_to_tehran(last_check_utc)

    txt = (
        f"<b>{BOT_NAME}</b>\n\n"
        f"🖥 <b>نام سرور:</b> {r['name']}\n"
        f"🌐 <b>آدرس:</b> <code>{r['host']}</code>\n"
        f"📊 <b>وضعیت پایش:</b> {st_text}\n"
        f"⏱ <b>آخرین بررسی:</b> <code>{last_check_tehran}</code>"
    )

    # حتما parse_mode را روی HTML ست کن
    await _edit_menu(cb.message, txt, reply_markup=status_kb(sid), parse_mode="HTML")
    await cb.answer()

@dp.callback_query(F.data == "add")
async def add(cb: types.CallbackQuery, state: FSMContext):
    if not await guard_cb(cb): return
    role = get_role(cb.from_user.id)
    if role not in ("owner", "admin"):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    
    await state.set_state(AddServer.name)
    
    # به جای ریختن خروجی _edit_menu در متغیر، 
    # مستقیماً از cb.message.message_id استفاده می‌کنیم
    await _edit_menu(
        cb.message, 
        BOT_HEADER + "\n\n➕ **افزودن سرور**\n\nنام سرور را ارسال کنید:", 
        reply_markup=add_server_kb()
    )
    
    # ذخیره آیدی پیامی که همین الان ویرایش شد
    await state.update_data(last_msg_id=cb.message.message_id)
    await cb.answer()

@dp.message(AddServer.name)
async def add_name(m: types.Message, state: FSMContext):
    data = await state.get_data()
    last_msg_id = data.get("last_msg_id")
    
    await m.delete() # پاک کردن پیام کاربر
    await state.update_data(name=(m.text or "").strip())
    await state.set_state(AddServer.host)
    
    # ویرایش پیام قبلی ربات به جای ارسال پیام جدید
    await m.bot.edit_message_text(
        chat_id=m.chat.id,
        message_id=last_msg_id,
        text=BOT_HEADER + "\n\n🌐 **مرحله ۲ از ۵**\n\nحالا **IP یا Host** را ارسال کنید:",
        reply_markup=add_server_kb()
    )

@dp.message(AddServer.host)
async def add_host(m: types.Message, state: FSMContext):
    data = await state.get_data()
    await m.delete()
    await state.update_data(host=(m.text or "").strip())
    await state.set_state(AddServer.port)
    
    await m.bot.edit_message_text(
        chat_id=m.chat.id,
        message_id=data.get("last_msg_id"),
        text=BOT_HEADER + "\n\n🔢 **مرحله ۳ از ۵**\n\n**پورت SSH** را بفرستید (پیش‌فرض ۲۲):",
        reply_markup=add_server_kb()
    )

@dp.message(AddServer.port)
async def add_port(m: types.Message, state: FSMContext):
    if not await guard_msg(m): return
    
    data = await state.get_data()
    last_msg_id = data.get("last_msg_id")
    
    # ۱. پردازش عدد پورت
    t = (m.text or "").strip()
    port = int(t) if t.isdigit() else 22
    await state.update_data(port=port)
    
    # ۲. پاک کردن پیام کاربر
    await m.delete()
    
    # ۳. ویرایش پیام قبلی ربات برای مرحله بعد
    await state.set_state(AddServer.user)
    await m.bot.edit_message_text(
        chat_id=m.chat.id,
        message_id=last_msg_id,
        text=BOT_HEADER + "\n\n👤 **مرحله ۴ از ۵**\n\nلطفاً **نام کاربری (Username)** SSH را ارسال کنید:",
        reply_markup=add_server_kb()
    )

@dp.message(AddServer.user)
async def add_user(m: types.Message, state: FSMContext):
    if not await guard_msg(m): return
    
    data = await state.get_data()
    last_msg_id = data.get("last_msg_id")
    
    # ۱. ذخیره یوزرنیم
    await state.update_data(user=(m.text or "").strip())
    
    # ۲. پاک کردن پیام کاربر
    await m.delete()
    
    # ۳. ویرایش پیام قبلی ربات برای مرحله نهایی (پسورد)
    await state.set_state(AddServer.pw)
    await m.bot.edit_message_text(
        chat_id=m.chat.id,
        message_id=last_msg_id,
        text=BOT_HEADER + "\n\n🔑 **مرحله ۵ از ۵**\n\nحالا **رمز عبور (Password)** SSH را بفرستید:\n\n*(این پیام پس از دریافت بلافاصله پاک خواهد شد)*",
        reply_markup=add_server_kb()
    )

@dp.message(AddServer.pw)
async def add_pw(m: types.Message, state: FSMContext):
    data = await state.get_data()
    await m.delete() # حذف پسورد از چت برای امنیت
    
    password = (m.text or "").strip()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO servers(name,host,port,user,pw) VALUES (?,?,?,?,?)",
        (data["name"], data["host"], int(data["port"]), data["user"], enc(password)),
    )
    conn.commit()
    conn.close()
    
    # حذف پیام مراحل قبلی ربات و فرستادن پیام اتمام موفقیت‌آمیز
    await m.bot.delete_message(chat_id=m.chat.id, message_id=data.get("last_msg_id"))
    await state.clear()
    
    await m.answer(BOT_HEADER + f"\n\n✅ سرور **{data['name']}** با موفقیت اضافه شد.", reply_markup=post_add_server_kb())

@dp.callback_query(F.data.startswith("reboot:"))
async def reboot_srv(cb: types.CallbackQuery):
    if not await guard_cb(cb):
        return
    
    # ۱. پاسخ فوری به تلگرام برای جلوگیری از Timeout و از بین رفتن Query ID
    try:
        await cb.answer("دستور ریبوت ارسال شد، لطفا شکیبا باشید...")
    except Exception:
        pass

    role = get_role(cb.from_user.id)
    if role not in ("owner", "admin"):
        return

    sid = int(cb.data.split(":")[1])
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT host,port,user,pw,name FROM servers WHERE id=?", (sid,))
    r = cur.fetchone()
    
    if not r:
        conn.close()
        return

    # ۲. اطلاع‌رسانی در منو که فرآیند شروع شده است
    await _edit_menu(cb.message, BOT_HEADER + f"\n\n⏳ در حال اتصال به {r['name']} و اجرای دستور ریبوت...")
    
    try:
        # فرآیند اصلی ریبوت که ممکن است زمان‌بر باشد
        await reboot((r["host"], int(r["port"]), r["user"], r["pw"]))
        
        cur.execute("INSERT INTO logs(server_id,action,status) VALUES (?,?,?)", (sid, "REBOOT", "SENT"))
        conn.commit()
        
        await _edit_menu(
            cb.message,
            BOT_HEADER + f"\n\n✅ دستور ریبوت با موفقیت به **{r['name']}** ارسال شد.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"status:{sid}")]]
            ),
        )
    except Exception as e:
        cur.execute("INSERT INTO logs(server_id,action,status) VALUES (?,?,?)", (sid, "REBOOT", "ERR"))
        conn.commit()
        await _edit_menu(
            cb.message,
            BOT_HEADER + f"\n\n❌ خطا در فرآیند ریبوت:\n`{e}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"status:{sid}")]]
            ),
        )
    finally:
        conn.close()
        # دیگر نیازی به cb.answer در اینجا نیست چون در خط ۱۰ اجرا شد

@dp.callback_query(F.data.startswith("del:"))
async def delete_confirm(cb: types.CallbackQuery):
    sid = int(cb.data.split(":")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، حذف شود", callback_data=f"force_del:{sid}")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data=f"status:{sid}")] # برگشت به صفحه سرور
    ])
    await _edit_menu(cb.message, "⚠️ **تایید حذف**\nآیا از حذف این سرور مطمئن هستید؟", reply_markup=kb)

@dp.callback_query(F.data.startswith("force_del:"))
async def force_delete(cb: types.CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[1])
    conn = db()
    cur = conn.cursor()
    
    # ۱. حذف از لیست اصلی سرورها
    cur.execute("DELETE FROM servers WHERE id=?", (sid,))
    # ۲. حذف از وضعیت‌های داشبورد
    cur.execute("DELETE FROM server_status WHERE server_id=?", (sid,))
    # ۳. حذف از لیست پایش ایران (نام صحیح جدول شما)
    cur.execute("DELETE FROM checkhost_targets WHERE server_id=?", (sid,))
    
    conn.commit()
    
    # دریافت لیست جدید برای نمایش
    cur.execute("SELECT id, name, host, port FROM servers ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    
    await cb.answer("🗑 سرور و تنظیمات پایش حذف شدند", show_alert=True)
    await state.clear()
    
    role = get_role(cb.from_user.id)
    await _edit_menu(
        cb.message, 
        BOT_HEADER + "\n\n📋 لیست سرورها (به‌روزرسانی شده)", 
        reply_markup=servers_list_kb(rows, role)
    )

@dp.callback_query(F.data.startswith("edit_name:"))
async def edit_name_start(cb: types.CallbackQuery, state: FSMContext):
    srv_id = int(cb.data.split(":")[1])
    await state.update_data(edit_srv_id=srv_id, last_msg_id=cb.message.message_id)
    await state.set_state(EditServer.new_name)
    
    await _edit_menu(cb.message, "📝 نام جدید سرور را ارسال کنید:", reply_markup=add_server_kb())
    await cb.answer()

@dp.message(EditServer.new_name)
async def edit_name_finish(m: types.Message, state: FSMContext):
    data = await state.get_data()
    new_name = m.text.strip()
    await m.delete() # پاک کردن پیام کاربر
    
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE servers SET name = ? WHERE id = ?", (new_name, data['edit_srv_id']))
    conn.commit()
    conn.close()
    
    await m.bot.delete_message(m.chat.id, data['last_msg_id']) # حذف پیام قبلی ربات
    await state.clear()
    await m.answer(f"✅ نام سرور به **{new_name}** تغییر یافت.", reply_markup=post_add_server_kb())

@dp.callback_query(F.data == "admin_panel")
async def admin_panel(cb: types.CallbackQuery):
    if not await guard_cb(cb):
        return
    if get_role(cb.from_user.id) != "owner":
        await cb.answer("فقط Owner دسترسی دارد", show_alert=True)
        return
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT uid,role FROM users ORDER BY role DESC, uid DESC")
    users = cur.fetchall()
    conn.close()
    await _edit_menu(cb.message, BOT_HEADER + "\n\n👥 مدیریت Admin", reply_markup=admin_panel_kb(users))
    await cb.answer()


@dp.callback_query(F.data.startswith("admin_user:"))
async def admin_user(cb: types.CallbackQuery):
    if not await guard_cb(cb):
        return
    if get_role(cb.from_user.id) != "owner":
        await cb.answer("فقط Owner", show_alert=True)
        return
    uid = int(cb.data.split(":")[1])
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT role FROM users WHERE uid=?", (uid,))
    r = cur.fetchone()
    conn.close()
    if not r:
        await cb.answer("کاربر پیدا نشد", show_alert=True)
        return
    await _edit_menu(cb.message, BOT_HEADER + f"\n\n🆔 {uid}\nRole: {r['role']}", reply_markup=admin_user_kb(uid, r["role"]))
    await cb.answer()


@dp.callback_query(F.data.startswith("setrole:"))
async def setrole(cb: types.CallbackQuery):
    if not await guard_cb(cb):
        return
    if get_role(cb.from_user.id) != "owner":
        await cb.answer("فقط Owner", show_alert=True)
        return
    _, uid, newrole = cb.data.split(":")
    uid = int(uid)
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET role=? WHERE uid=? AND role!='owner'", (newrole, uid))
    conn.commit()
    conn.close()
    await _edit_menu(
        cb.message,
        BOT_HEADER + "\n\n✅ Role آپدیت شد.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_panel")]]
        ),
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("rmuser:"))
async def rmuser(cb: types.CallbackQuery):
    if not await guard_cb(cb):
        return
    if get_role(cb.from_user.id) != "owner":
        await cb.answer("فقط Owner", show_alert=True)
        return
    uid = int(cb.data.split(":")[1])
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE uid=? AND role!='owner'", (uid,))
    conn.commit()
    conn.close()
    await _edit_menu(
        cb.message,
        BOT_HEADER + "\n\n🗑 حذف شد.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_panel")]]
        ),
    )
    await cb.answer()

@dp.callback_query(F.data == "admin_add")
async def admin_add(cb: types.CallbackQuery, state: FSMContext):
    if not await guard_cb(cb):
        return
    if get_role(cb.from_user.id) != "owner":
        await cb.answer("فقط Owner", show_alert=True)
        return

    # تنظیم استیت و ذخیره آیدی پیام فعلی
    await state.set_state(AdminAdd.uid)
    await state.update_data(menu_msg_id=cb.message.message_id)
    
    await _edit_menu(
        cb.message, 
        BOT_HEADER + "\n\n🆔 **افزودن ادمین جدید**\n\nلطفاً آیدی عددی کاربر را ارسال کنید:", 
        reply_markup=admin_add_kb()
    )
    await cb.answer()

@dp.message(AdminAdd.uid)
async def admin_add_uid(m: types.Message, state: FSMContext):
    if not await guard_msg(m):
        return
    if get_role(m.from_user.id) != "owner":
        return

    # دریافت آیدی پیام منو از استیت
    data = await state.get_data()
    menu_msg_id = data.get("menu_msg_id")
    
    t = (m.text or "").strip()

    # حذف پیام ارسالی کاربر برای تمیز ماندن چت
    try:
        await m.delete()
    except:
        pass

    # بررسی لغو عملیات
    if t.lower() in ("cancel", "/cancel", "بازگشت"):
        await state.clear()
        conn = db(); cur = conn.cursor()
        cur.execute("SELECT uid,role FROM users ORDER BY role DESC, uid DESC")
        users = cur.fetchall(); conn.close()
        
        await bot.edit_message_text(
            chat_id=m.chat.id, message_id=menu_msg_id,
            text=BOT_HEADER + "\n\n👥 مدیریت Admin",
            reply_markup=admin_panel_kb(users)
        )
        return

    # بررسی عددی بودن
    if not t.isdigit():
        msg_err = await m.answer("⚠️ فقط عدد بفرست.")
        await asyncio.sleep(2); await msg_err.delete()
        return

    uid = int(t)
    conn = db(); cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users(uid,role) VALUES (?,?)", (uid, "admin"))
    cur.execute("UPDATE users SET role='admin' WHERE uid=? AND role!='owner'", (uid,))
    conn.commit(); conn.close()
    
    await state.clear()

    # بروزرسانی لیست ادمین‌ها در همان پیام قبلی
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT uid,role FROM users ORDER BY role DESC, uid DESC")
    users = cur.fetchall(); conn.close()
    
    await bot.edit_message_text(
        chat_id=m.chat.id, 
        message_id=menu_msg_id,
        text=BOT_HEADER + f"\n\n✅ کاربر `{uid}` با موفقیت اضافه شد.\n\n👥 مدیریت Admin", 
        parse_mode="Markdown", 
        reply_markup=admin_panel_kb(users)
    )

@dp.callback_query(F.data == "log_admin")
async def log_admin(cb: types.CallbackQuery):
    if not await guard_cb(cb):
        return
    if get_role(cb.from_user.id) != "owner":
        await cb.answer("فقط Owner", show_alert=True)
        return
    days = get_log_retention_days()
    msg = (
        BOT_HEADER
        + "\n\n🧹 **مدیریت لاگ‌ها**\n"
        + f"⏱ نگهداری فعلی: **{days} روز**\n\n"
        + "گزینه‌ها را انتخاب کنید:"
    )
    await _edit_menu(cb.message, msg, parse_mode="Markdown", reply_markup=log_admin_kb())
    await cb.answer()


@dp.callback_query(F.data == "log_cleanup")
async def log_cleanup(cb: types.CallbackQuery):
    if not await guard_cb(cb):
        return
    if get_role(cb.from_user.id) != "owner":
        await cb.answer("فقط Owner", show_alert=True)
        return
    days = get_log_retention_days()
    await _edit_menu(cb.message, BOT_HEADER + f"\n\n⏳ در حال پاک‌سازی لاگ‌های قدیمی‌تر از {days} روز ...")
    try:
        deleted = await cleanup_logs_once(days)
        msg = BOT_HEADER + f"\n\n✅ پاک‌سازی انجام شد.\n🗑 حذف شد: {deleted} رکورد"
    except Exception as e:
        msg = BOT_HEADER + f"\n\n❌ خطا در پاک‌سازی: {e}"
    await _edit_menu(cb.message, msg, reply_markup=log_admin_kb())
    await cb.answer()


@dp.callback_query(F.data == "log_set_retention")
async def log_set_retention(cb: types.CallbackQuery, state: FSMContext):
    if not await guard_cb(cb):
        return
    if get_role(cb.from_user.id) != "owner":
        await cb.answer("فقط Owner", show_alert=True)
        return
    
    # تنظیم استیت و ذخیره آیدی پیام برای ویرایش در مرحله بعد
    await state.set_state(LogRetention.days)
    await state.update_data(menu_msg_id=cb.message.message_id)
    
    cur = get_log_retention_days()
    
    await _edit_menu(
        cb.message,
        BOT_HEADER + f"\n\n⏱ **تنظیم روزهای نگهداری لاگ‌ها**\n\nتعداد روز را ارسال کنید (1 تا 365).\nوضعیت فعلی: `{cur}` روز",
        reply_markup=log_set_kb(),
    )
    await cb.answer()

@dp.message(LogRetention.days)
async def log_retention_days(m: types.Message, state: FSMContext):
    if not await guard_msg(m):
        return
    if get_role(m.from_user.id) != "owner":
        return

    t = (m.text or "").strip()
    
    # چک کردن عدد و حذف پیام خطا بعد از ۲ ثانیه (برای تمیز ماندن چت)
    if not t.isdigit() or not (1 <= int(t) <= 365):
        msg_err = await m.answer("⚠️ عدد نامعتبر! فقط عدد بین 1 تا 365 بفرست.")
        await asyncio.sleep(2)
        await msg_err.delete() # حذف پیام خطا
        await m.delete()     # حذف پیام اشتباه کاربر
        return

    days = int(t)
    set_setting("log_retention_days", str(days))

    # گرفتن آیدی پیام منو و حذف پیام عدد کاربر
    data = await state.get_data()
    menu_msg_id = data.get("menu_msg_id")
    try:
        await m.delete()
    except:
        pass

    await state.clear()

    # ویرایش همان پیام قبلی به جای ارسال پیام جدید
    new_days = get_log_retention_days()
    msg = (
        BOT_HEADER
        + "\n\n✅ تنظیمات با موفقیت ذخیره شد.\n"
        + "🧹 **مدیریت لاگ‌ها**\n"
        + f"⏱ نگهداری فعلی: **{new_days} روز**\n\n"
        + "گزینه‌ها را انتخاب کنید:"
    )

    try:
        await bot.edit_message_text(
            chat_id=m.chat.id,
            message_id=menu_msg_id,
            text=msg,
            parse_mode="Markdown",
            reply_markup=log_admin_kb()
        )
    except:
        await m.answer(msg, reply_markup=log_admin_kb())

@dp.callback_query(F.data == "log_export")
async def log_export(cb: types.CallbackQuery):
    if not await guard_cb(cb):
        return
    if get_role(cb.from_user.id) != "owner":
        await cb.answer("فقط Owner", show_alert=True)
        return

    days = get_log_retention_days()
    await _edit_menu(cb.message, BOT_HEADER + "\n\n⏳ در حال آماده‌سازی فایل آرشیو لاگ‌ها ...")

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id,ts,server_id,action,status FROM logs ORDER BY id DESC LIMIT 5000")
    rows = cur.fetchall()
    conn.close()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = f"/tmp/server_guard_logs_{ts}.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write("Server system guard logs export\n")
        f.write(f"Export time: {ts}\n")
        f.write(f"Retention setting: {days} days\n")
        f.write(f"Rows: {len(rows)}\n\n")
        for r in rows:
            f.write(f"{r['id']}\t{r['ts']}\tsrv:{r['server_id']}\t{r['action']}\t{r['status']}\n")

    try:
        await bot.send_document(cb.from_user.id, FSInputFile(path), caption="📦 آرشیو لاگ‌ها")
        msg = BOT_HEADER + f"\n\n✅ فایل آرشیو ارسال شد.\n📄 تعداد رکورد: {len(rows)}"
    except Exception as e:
        msg = BOT_HEADER + f"\n\n❌ ارسال فایل ناموفق: {e}"

    await _edit_menu(cb.message, msg, reply_markup=log_admin_kb())
    await cb.answer()


@dp.callback_query(F.data == "logs")
async def logs(cb: types.CallbackQuery):
    if not await guard_cb(cb):
        return
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT server_id,action,status,ts FROM logs ORDER BY id DESC LIMIT 20")
    rows = cur.fetchall()
    conn.close()
    if not rows:
        await _edit_menu(
            cb.message,
            BOT_HEADER + "\n\n📜 لاگ‌ها\n\nخالی است.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data="bot_settings")]]
            ),
        )
        await cb.answer()
        return
    t = "\n".join(
       f"{utc_sqlite_to_tehran(r['ts'])} | srv:{r['server_id']} | {r['action']} | {r['status']}"
       for r in rows
    )


    await _edit_menu(
        cb.message,
        BOT_HEADER + "\n\n📜 لاگ‌ها\n\n" + t,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data="bot_settings")]]
        ),
    )
    await cb.answer()


@dp.callback_query(F.data == "cancel_fsm")
async def cancel_fsm(cb: types.CallbackQuery, state: FSMContext):
    if not await guard_cb(cb):
        return
    await state.clear()
    role = get_role(cb.from_user.id)
    await _edit_menu(cb.message, BOT_HEADER + "\n\nلغو شد.", reply_markup=main_kb(role))
    await cb.answer()



# ---------------- Check-Host (Iran monitoring) ----------------
# Owner-only feature: monitor public reachability from Iran nodes using check-host.net Ping.
# We focus on "4/4" per Iran node: a node is OK only if all 4 pings are OK.


# 1️⃣ نودهای واقعی برای اجرا

CH_IR_NODES = [
    "ir1.node.check-host.net",
    "ir2.node.check-host.net",
    "ir3.node.check-host.net",
    "ir5.node.check-host.net",
    "ir6.node.check-host.net",
    "ir7.node.check-host.net",
    "ir8.node.check-host.net",
]

# 2️⃣ مپ اسم شهر فقط برای نمایش
CH_IR_NODE_LABELS = {
    "ir1.node.check-host.net": "Tehran",
    "ir2.node.check-host.net": "Mashhad",
    "ir3.node.check-host.net": "Shiraz",
    "ir5.node.check-host.net": "Tabriz",
    "ir6.node.check-host.net": "Isfahan",
    "ir7.node.check-host.net": "Tehran",
    "ir8.node.check-host.net": "Tehran",
}
CH_LOCK = asyncio.Lock()


def _ensure_checkhost_tables() -> None:
    conn = db()
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS checkhost_targets (server_id INTEGER PRIMARY KEY)")
    cur.execute(
        "CREATE TABLE IF NOT EXISTS checkhost_state ("
        "server_id INTEGER PRIMARY KEY,"
        "last_status TEXT,"
        "updated_ts DATETIME DEFAULT CURRENT_TIMESTAMP,"
        "auto_status TEXT,"
        "fail_alert_sent INTEGER DEFAULT 0"
        ")"
    )
    # Migrate older DBs (ignore errors if columns already exist)
    for ddl in (
        "ALTER TABLE checkhost_state ADD COLUMN auto_status TEXT",
        "ALTER TABLE checkhost_state ADD COLUMN fail_alert_sent INTEGER DEFAULT 0",
    ):
        try:
            cur.execute(ddl)
        except Exception:
            pass
    cur.execute(
        "CREATE TABLE IF NOT EXISTS checkhost_history ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "ts DATETIME DEFAULT CURRENT_TIMESTAMP,"
        "server_id INTEGER,"
        "host TEXT,"
        "ok_nodes INTEGER,"
        "total_nodes INTEGER,"
        "threshold INTEGER,"
        "status TEXT,"
        "report_link TEXT,"
        "details TEXT"
        ")"
    )
    conn.commit()
    conn.close()


def _ch_get_int(key: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(get_setting(key, str(default)))
        return max(lo, min(hi, v))
    except Exception:
        return default


def ch_nodes_count() -> int:
    # How many Iran nodes we consider (up to len(CH_IR_NODES))
    return _ch_get_int("ch_nodes_count", min(7, len(CH_IR_NODES)), 1, len(CH_IR_NODES))


def ch_threshold() -> int:
    # Threshold is number of nodes that MUST be 4/4 (>= threshold -> OK)
    n = ch_nodes_count()
    return _ch_get_int("ch_threshold", n, 1, n)


def ch_interval_hours() -> int:
    # 0 disables auto-run
    return _ch_get_int("ch_interval_hours", 0, 0, 168)


def ch_fail_confirm_checks() -> int:
    # Total checks (including first). 2 = 1 retry.
    return _ch_get_int("ch_fail_confirm_checks", 2, 1, 5)


def ch_ok_confirm_checks() -> int:
    return _ch_get_int("ch_ok_confirm_checks", 1, 1, 5)


def ch_retry_delay_sec() -> int:
    return _ch_get_int("ch_retry_delay_sec", 20, 0, 600)


# Backward-compatible alias (some older code paths referenced ch_retry_delay())
def ch_retry_delay() -> int:
    return ch_retry_delay_sec()


# Backward-compatible alias (some older code paths referenced ch_nodes_list())
def ch_nodes_list() -> list[str]:
    return _ch_nodes_list()


def ch_silent_mode() -> bool:
    return get_setting("ch_silent", "0") == "1"


def ch_notify_ok() -> bool:
    return get_setting("ch_notify_ok", "1") == "1"


def ch_last_run_utc() -> int:
    return _ch_get_int("ch_last_run_utc", 0, 0, 2_000_000_000)


def ch_set_last_run_utc(ts: int) -> None:
    set_setting("ch_last_run_utc", str(int(ts)))



def ch_get_notify_chat_id() -> int:
    try:
        return int(get_setting("ch_notify_chat_id", "0") or "0")
    except Exception:
        return 0


def ch_set_notify_chat_id(chat_id: int) -> None:
    try:
        set_setting("ch_notify_chat_id", str(int(chat_id)))
    except Exception:
        pass

def ch_get_targets() -> set[int]:
    _ensure_checkhost_tables()
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT server_id FROM checkhost_targets")
    rows = cur.fetchall()
    conn.close()
    return {int(r["server_id"]) for r in rows}


def ch_toggle_target(server_id: int) -> None:
    _ensure_checkhost_tables()
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM checkhost_targets WHERE server_id=?", (server_id,))
    if cur.fetchone():
        cur.execute("DELETE FROM checkhost_targets WHERE server_id=?", (server_id,))
    else:
        cur.execute("INSERT OR IGNORE INTO checkhost_targets(server_id) VALUES (?)", (server_id,))
    conn.commit()
    conn.close()


def ch_get_last_status(server_id: int) -> str:
    _ensure_checkhost_tables()
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT last_status FROM checkhost_state WHERE server_id=?", (server_id,))
    r = cur.fetchone()
    conn.close()
    return (r["last_status"] if r and r["last_status"] else "UNKNOWN")


def ch_set_last_status(server_id: int, status: str) -> None:
    _ensure_checkhost_tables()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO checkhost_state(server_id,last_status) VALUES (?,?) "
        "ON CONFLICT(server_id) DO UPDATE SET last_status=excluded.last_status, updated_ts=CURRENT_TIMESTAMP",
        (server_id, status),
    )
    conn.commit()
    conn.close()



def ch_get_auto_status(server_id: int) -> str:
    _ensure_checkhost_tables()
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT auto_status FROM checkhost_state WHERE server_id=?", (server_id,))
    r = cur.fetchone()
    conn.close()
    return (r["auto_status"] if r and r["auto_status"] else "UNKNOWN")


def ch_set_auto_status(server_id: int, status: str) -> None:
    _ensure_checkhost_tables()
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO checkhost_state(server_id) VALUES (?)", (server_id,))
    cur.execute(
        "UPDATE checkhost_state SET auto_status=?, updated_ts=CURRENT_TIMESTAMP WHERE server_id=?",
        (status, server_id),
    )
    conn.commit()
    conn.close()


def ch_get_fail_alert_sent(server_id: int) -> int:
    _ensure_checkhost_tables()
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT fail_alert_sent FROM checkhost_state WHERE server_id=?", (server_id,))
    r = cur.fetchone()
    conn.close()
    try:
        return int(r["fail_alert_sent"]) if r and r["fail_alert_sent"] is not None else 0
    except Exception:
        return 0


def ch_set_fail_alert_sent(server_id: int, sent: int) -> None:
    _ensure_checkhost_tables()
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO checkhost_state(server_id) VALUES (?)", (server_id,))
    cur.execute("UPDATE checkhost_state SET fail_alert_sent=? WHERE server_id=?", (1 if sent else 0, server_id))
    conn.commit()
    conn.close()

def ch_add_history(
    server_id: int,
    host: str,
    ok_nodes: int,
    total_nodes: int,
    *args,
) -> None:
    """Append a row into checkhost_history.

    Backward/forward compatible with older call-sites.

    Supported signatures:
      - (server_id, host, ok_nodes, total_nodes, threshold, status, link, details)
      - (server_id, host, ok_nodes, total_nodes, threshold, status, link, details, err)
      - (server_id, host, ok_nodes, total_nodes, status, link, details, err)
    """
    # If history feature is disabled, do nothing.
    try:
        if not ch_history_enabled():
            return
    except Exception:
        # If setting function is absent for any reason, keep going (table still safe).
        pass

    threshold = ch_threshold()
    status = ""
    link = ""
    details = ""
    err = ""

    # Parse args defensively (we've had a few different versions of this function).
    if len(args) == 4:
        # status, link, details, err
        status, link, details, err = args
    elif len(args) == 5:
        # threshold, status, link, details, err
        threshold, status, link, details, err = args
    elif len(args) == 3:
        # status, link, details
        status, link, details = args
    elif len(args) >= 1:
        # Best-effort fallback
        # (threshold, status, link, details[, err])
        if len(args) >= 4:
            threshold, status, link, details = args[:4]
            if len(args) >= 5:
                err = args[4]
        else:
            # Unknown form; put everything in details
            details = " | ".join(str(x) for x in args)

    # Normalize threshold to int, even if args were shifted.
    try:
        threshold_i = int(threshold)
    except Exception:
        # If something like "OK"/"FAIL" landed here, keep current setting.
        threshold_i = int(ch_threshold())

    # Append err to details (schema has no separate err column).
    if err:
        details = (details or "") + ("\n\n" if details else "") + f"⚠️ err: {err}"

    _ensure_checkhost_tables()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO checkhost_history(server_id,host,ok_nodes,total_nodes,threshold,status,report_link,details) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (server_id, host, int(ok_nodes), int(total_nodes), threshold_i, str(status), str(link), str(details)),
    )
    # Keep history bounded (last 2000 rows)
    cur.execute(
        "DELETE FROM checkhost_history WHERE id NOT IN (SELECT id FROM checkhost_history ORDER BY id DESC LIMIT 2000)"
    )
    conn.commit()
    conn.close()


def _ch_tehran_now() -> str:
    return datetime.now(tz=ZoneInfo("Asia/Tehran")).strftime("%Y-%m-%d %H:%M:%S")


def _ch_nodes_list() -> list[str]:
    return CH_IR_NODES[: ch_nodes_count()]


def ch_menu_kb() -> InlineKeyboardMarkup:
    # خواندن مقدار واقعی از دیتابیس با کلید صحیح
    # مقدار پیش‌فرض را 6 گذاشتم که اگر دیتابیس خالی بود نشان دهد
    current_interval = get_setting("ch_interval_hours", "6") 

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🖥 انتخاب سرورها", callback_data="ch_targets")],
            [InlineKeyboardButton(text="🌐 تعداد نودهای ایران", callback_data="ch_nodes")],
            [InlineKeyboardButton(text="🚧 آستانه هشدار", callback_data="ch_threshold")],
            
            # نمایش مقدار واقعی (که در دیتابیس شما الان 1 است)
            [InlineKeyboardButton(text=f"⏱️ اجرای خودکار (هر {current_interval} ساعت)", callback_data="ch_interval")],
            
            [InlineKeyboardButton(text="🔁 تایید خطا (تعداد تکرار)", callback_data="ch_fail_confirm")],
            [InlineKeyboardButton(text="⏳ تاخیر بین تکرارها", callback_data="ch_retry_delay")],
            [InlineKeyboardButton(text="✅ تایید رفع مشکل (OK)", callback_data="ch_ok_confirm")],
            [InlineKeyboardButton(text=f"🔔 نوتیفیکیشن: {'خاموش' if ch_silent_mode() else 'روشن'}", callback_data="ch_toggle_silent")],
            [InlineKeyboardButton(text=f"✅ پیام OK: {'روشن' if ch_notify_ok() else 'خاموش'}", callback_data="ch_toggle_ok_notify")],
            [InlineKeyboardButton(text="📜 تاریخچه پایش", callback_data="ch_history")],
            [InlineKeyboardButton(text="⚡ اجرای دستی همین الان", callback_data="ch_run_now")],
            [InlineKeyboardButton(text="🔙 بازگشت", callback_data="home")],
        ]
    )


def _ch_menu_text() -> str:
    n = ch_nodes_count()
    thr = ch_threshold()
    interval = ch_interval_hours()
    targets = len(ch_get_targets())
    fail_checks = ch_fail_confirm_checks()
    ok_checks = ch_ok_confirm_checks()
    delay = ch_retry_delay_sec()
    return (
        BOT_HEADER
        + "\n\n🌐 **پایش ایران (check-host.net)** — فقط Owner\n\n"
        + f"🌐 نودهای ایران: **{n}**\n"
        + f"🚧 آستانه هشدار: کمتر از **{thr}/{n}**\n"
        + (f"⏱️ اجرای خودکار: **غیرفعال**\n" if interval == 0 else f"⏱️ اجرا هر: **{interval} ساعت**\n")
        + f"🔁 تایید خطا: **{fail_checks} چک** | ⏳ تاخیر: **{delay} ثانیه**\n"
        + f"✅ تایید OK: **{ok_checks} چک**\n"
        + f"🔔 نوتیفیکیشن: **{'خاموش' if ch_silent_mode() else 'روشن'}**\n"
        + f"✅ پیام OK: **{'روشن' if ch_notify_ok() else 'خاموش'}**\n"
        + f"🖥 سرورهای انتخاب‌شده: **{targets}**"
    )


async def _owner_only_cb(cb: types.CallbackQuery) -> bool:
    if not await guard_cb(cb):
        return False
    if get_role(cb.from_user.id) != "owner":
        try:
            await cb.answer("فقط Owner", show_alert=True)
        except Exception:
            pass
        return False
    return True


def ch_targets_kb() -> InlineKeyboardMarkup:
    _ensure_checkhost_tables()
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id,name,host FROM servers ORDER BY id DESC")
    servers = cur.fetchall()
    conn.close()

    selected = ch_get_targets()
    rows = []
    for s in servers:
        sid = int(s["id"])
        mark = "✅" if sid in selected else "⬜️"
        rows.append([InlineKeyboardButton(text=f"{mark} {s['name']} ({s['host']})", callback_data=f"ch_tgl:{sid}")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="ch_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ch_nodes_kb() -> InlineKeyboardMarkup:
    opts = [3, 6, 7]
    rows = []
    maxn = len(CH_IR_NODES)
    for v in opts:
        vv = min(v, maxn)
        rows.append([InlineKeyboardButton(text=f"{vv}", callback_data=f"ch_set_nodes:{vv}")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="ch_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ch_threshold_kb() -> InlineKeyboardMarkup:
    n = ch_nodes_count()
    rows = []
    for v in range(1, n + 1):
        label = f"{v}/{n}"
        if v == n:
            label += " (همه OK)"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"ch_set_thr:{v}")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="ch_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ch_interval_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="غیرفعال", callback_data="ch_set_int:0")],
        [InlineKeyboardButton(text="🧪 تست زمان‌بندی (۶۰ ثانیه)", callback_data="ch_set_int:test")], 
        [InlineKeyboardButton(text="1 ساعت", callback_data="ch_set_int:1")],
        [InlineKeyboardButton(text="2 ساعت", callback_data="ch_set_int:2")],
        [InlineKeyboardButton(text="3 ساعت", callback_data="ch_set_int:3")],
        [InlineKeyboardButton(text="4 ساعت", callback_data="ch_set_int:4")],
        [InlineKeyboardButton(text="6 ساعت", callback_data="ch_set_int:6")],
        [InlineKeyboardButton(text="12 ساعت", callback_data="ch_set_int:12")],
        [InlineKeyboardButton(text="24 ساعت", callback_data="ch_set_int:24")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="ch_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ch_fail_confirm_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="1 (بدون تکرار)", callback_data="ch_set_fail:1")],
        [InlineKeyboardButton(text="2 (یکبار تکرار)", callback_data="ch_set_fail:2")],
        [InlineKeyboardButton(text="3 (دو بار تکرار)", callback_data="ch_set_fail:3")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="ch_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ch_retry_delay_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="0 ثانیه", callback_data="ch_set_delay:0")],
        [InlineKeyboardButton(text="15 ثانیه", callback_data="ch_set_delay:15")],
        [InlineKeyboardButton(text="20 ثانیه", callback_data="ch_set_delay:20")],
        [InlineKeyboardButton(text="30 ثانیه", callback_data="ch_set_delay:30")],
        [InlineKeyboardButton(text="60 ثانیه", callback_data="ch_set_delay:60")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="ch_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ch_ok_confirm_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="1", callback_data="ch_set_ok:1")],
        [InlineKeyboardButton(text="2", callback_data="ch_set_ok:2")],
        [InlineKeyboardButton(text="3", callback_data="ch_set_ok:3")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="ch_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "ch_menu")
async def ch_menu(cb: types.CallbackQuery):
    if not await _owner_only_cb(cb):
        return
    ch_set_notify_chat_id(cb.message.chat.id)

    await _edit_menu(cb.message, _ch_menu_text(), parse_mode="Markdown", reply_markup=ch_menu_kb())
    try:
        await cb.answer()
    except Exception:
        pass


@dp.callback_query(F.data == "ch_targets")
async def ch_targets(cb: types.CallbackQuery):
    if not await _owner_only_cb(cb):
        return
    await _edit_menu(cb.message, BOT_HEADER + "\n\n🖥 انتخاب سرورهای پایش:", reply_markup=ch_targets_kb())
    try:
        await cb.answer()
    except Exception:
        pass

@dp.callback_query(F.data.startswith("usage:"))
async def show_usage(cb: types.CallbackQuery):
    sid = int(cb.data.split(":")[1])
    conn = db()
    conn.row_factory = sqlite3.Row # برای دسترسی با نام ستون
    cur = conn.cursor()
    cur.execute("SELECT host, port, user, pw, name FROM servers WHERE id=?", (sid,))
    s = cur.fetchone()
    conn.close()

    if not s:
        await cb.answer("❌ سرور یافت نشد.")
        return

    # ۱. پاسخ به Callback برای برداشتن حالت لودینگ دکمه
    await cb.answer("⏳ در حال دریافت اطلاعات...")

    # ۲. نمایش حالت انتظار در همان پیام قبلی (جلوگیری از تکرار)
    await _edit_menu(cb.message, f"⌛ در حال اتصال به `{s['name']}` و استخراج منابع...")

    # ۳. تلاش برای گرفتن دیتا از SSH
    # نکته: اگر پسورد انکریپت شده است از dec(s['pw']) استفاده کنید
    cpu, ram = await get_system_usage(s['host'], s['port'], s['user'], s['pw'])
    
    if cpu is not None:
        text = (
            f"📊 **مصرف منابع سرور: {s['name']}**\n"
            f"━━━━━━━━━━━━━━\n"
            f"💻 **پردازشگر (CPU):** `{cpu:.1f}%`\n"
            f"🧠 **رم (RAM):** `{ram:.1f}%`\n"
            f"━━━━━━━━━━━━━━\n"
            f"🕒 به‌روزرسانی: {datetime.now().strftime('%H:%M:%S')}"
        )
    else:
        text = (
            f"❌ **خطای اتصال SSH**\n\n"
            f"ربات نتوانست به سرور `{s['name']}` وصل شود.\n"
            f"دسترسی SSH یا یوزرنیم/پسورد را چک کنید."
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 تلاش مجدد (Refresh)", callback_data=f"usage:{sid}")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"status:{sid}")]
    ])
    
    # ۴. ویرایش همان پیام قبلی با نتیجه نهایی
    await _edit_menu(cb.message, text, reply_markup=kb)

@dp.callback_query(F.data.startswith("ch_tgl:"))
async def ch_toggle(cb: types.CallbackQuery):
    if not await _owner_only_cb(cb):
        return
    sid = int(cb.data.split(":")[1])
    ch_toggle_target(sid)
    await _edit_menu(cb.message, BOT_HEADER + "\n\n🖥 انتخاب سرورهای پایش:", reply_markup=ch_targets_kb())
    try:
        await cb.answer()
    except Exception:
        pass


@dp.callback_query(F.data == "ch_nodes")
async def ch_nodes(cb: types.CallbackQuery):
    if not await _owner_only_cb(cb):
        return
    msg = BOT_HEADER + f"\n\n🌐 تعداد نودهای ایران را انتخاب کنید (حداکثر {len(CH_IR_NODES)}):"
    await _edit_menu(cb.message, msg, reply_markup=ch_nodes_kb())
    try:
        await cb.answer()
    except Exception:
        pass


@dp.callback_query(F.data.startswith("ch_set_nodes:"))
async def ch_set_nodes(cb: types.CallbackQuery):
    if not await _owner_only_cb(cb):
        return
    v = int(cb.data.split(":")[1])
    v = max(1, min(len(CH_IR_NODES), v))
    set_setting("ch_nodes_count", str(v))
    # Clamp threshold to new N
    if ch_threshold() > v:
        set_setting("ch_threshold", str(v))
    await _edit_menu(cb.message, _ch_menu_text(), parse_mode="Markdown", reply_markup=ch_menu_kb())
    try:
        await cb.answer("ثبت شد")
    except Exception:
        pass


@dp.callback_query(F.data == "ch_threshold")
async def ch_thr(cb: types.CallbackQuery):
    if not await _owner_only_cb(cb):
        return
    n = ch_nodes_count()
    msg = BOT_HEADER + f"\n\n🚧 آستانه هشدار را انتخاب کنید (اگر کمتر از X/{n} شد هشدار بده):"
    await _edit_menu(cb.message, msg, reply_markup=ch_threshold_kb())
    try:
        await cb.answer()
    except Exception:
        pass


@dp.callback_query(F.data.startswith("ch_set_thr:"))
async def ch_set_thr(cb: types.CallbackQuery):
    if not await _owner_only_cb(cb):
        return
    v = int(cb.data.split(":")[1])
    n = ch_nodes_count()
    v = max(1, min(n, v))
    set_setting("ch_threshold", str(v))
    await _edit_menu(cb.message, _ch_menu_text(), parse_mode="Markdown", reply_markup=ch_menu_kb())
    try:
        await cb.answer("ثبت شد")
    except Exception:
        pass


@dp.callback_query(F.data == "ch_interval")
async def ch_interval(cb: types.CallbackQuery):
    if not await _owner_only_cb(cb):
        return
    msg = BOT_HEADER + "\n\n⏱️ اجرای خودکار را تنظیم کنید:"
    await _edit_menu(cb.message, msg, reply_markup=ch_interval_kb())
    try:
        await cb.answer()
    except Exception:
        pass

@dp.callback_query(F.data.startswith("ch_set_int:"))
async def ch_set_int(cb: types.CallbackQuery):
    if not await _owner_only_cb(cb):
        return
    
    # گرفتن مقدار بعد از دو نقطه
    raw_val = cb.data.split(":")[1]
    
    if raw_val == "test":
        # حالت تست: مقدار را مستقیماً ذخیره می‌کنیم
        set_setting("ch_interval_hours", "test")
        ch_set_last_run_utc(0) # اجرای فوری
        msg = "🧪 حالت تست (60 ثانیه) فعال شد."
    else:
        # حالت عادی: تبدیل به عدد
        v = int(raw_val)
        v = max(0, min(168, v))
        set_setting("ch_interval_hours", str(v))
        if v > 0:
            ch_set_last_run_utc(0) # اجرای فوری
        msg = "✅ تنظیمات زمان‌بندی آپدیت شد."

    # نمایش منوی اصلی بعد از تنظیم
    await _edit_menu(cb.message, _ch_menu_text(), parse_mode="Markdown", reply_markup=ch_menu_kb())
    
    try:
        await cb.answer(msg)
    except Exception:
        pass

@dp.callback_query(F.data == "ch_fail_confirm")
async def ch_fail_confirm(cb: types.CallbackQuery):
    if not await _owner_only_cb(cb):
        return
    msg = BOT_HEADER + "\n\n🔁 تایید خطا: تعداد چک‌های سریع قبل از ارسال هشدار را انتخاب کنید:"
    await _edit_menu(cb.message, msg, reply_markup=ch_fail_confirm_kb())
    try:
        await cb.answer()
    except Exception:
        pass


@dp.callback_query(F.data.startswith("ch_set_fail:"))
async def ch_set_fail(cb: types.CallbackQuery):
    if not await _owner_only_cb(cb):
        return
    v = int(cb.data.split(":")[1])
    set_setting("ch_fail_confirm_checks", str(max(1, min(5, v))))
    await _edit_menu(cb.message, _ch_menu_text(), parse_mode="Markdown", reply_markup=ch_menu_kb())
    try:
        await cb.answer("ثبت شد")
    except Exception:
        pass


@dp.callback_query(F.data == "ch_retry_delay")
async def ch_delay(cb: types.CallbackQuery):
    if not await _owner_only_cb(cb):
        return
    msg = BOT_HEADER + "\n\n⏳ تاخیر بین تکرارها (ثانیه):"
    await _edit_menu(cb.message, msg, reply_markup=ch_retry_delay_kb())
    try:
        await cb.answer()
    except Exception:
        pass


@dp.callback_query(F.data.startswith("ch_set_delay:"))
async def ch_set_delay(cb: types.CallbackQuery):
    if not await _owner_only_cb(cb):
        return
    v = int(cb.data.split(":")[1])
    set_setting("ch_retry_delay_sec", str(max(0, min(600, v))))
    await _edit_menu(cb.message, _ch_menu_text(), parse_mode="Markdown", reply_markup=ch_menu_kb())
    try:
        await cb.answer("ثبت شد")
    except Exception:
        pass


@dp.callback_query(F.data == "ch_ok_confirm")
async def ch_ok_confirm(cb: types.CallbackQuery):
    if not await _owner_only_cb(cb):
        return
    msg = BOT_HEADER + "\n\n✅ تایید رفع مشکل (OK): چند چک OK لازم است تا پیام OK ارسال شود؟"
    await _edit_menu(cb.message, msg, reply_markup=ch_ok_confirm_kb())
    try:
        await cb.answer()
    except Exception:
        pass

@dp.callback_query(F.data.startswith("ch_set_ok:"))
async def ch_set_ok(cb: types.CallbackQuery):
    if not await _owner_only_cb(cb):
        return
    v = int(cb.data.split(":")[1])
    set_setting("ch_ok_confirm_checks", str(max(1, min(5, v))))
    await _edit_menu(cb.message, _ch_menu_text(), parse_mode="Markdown", reply_markup=ch_menu_kb())
    try:
        await cb.answer("ثبت شد")
    except Exception:
        pass


@dp.callback_query(F.data == "ch_toggle_silent")
async def ch_toggle_silent(cb: types.CallbackQuery):
    if not await _owner_only_cb(cb):
        return
    set_setting("ch_silent", "0" if ch_silent_mode() else "1")
    await _edit_menu(cb.message, _ch_menu_text(), parse_mode="Markdown", reply_markup=ch_menu_kb())
    try:
        await cb.answer()
    except Exception:
        pass


@dp.callback_query(F.data == "ch_toggle_ok_notify")
async def ch_toggle_ok_notify(cb: types.CallbackQuery):
    if not await _owner_only_cb(cb):
        return
    set_setting("ch_notify_ok", "0" if ch_notify_ok() else "1")
    await _edit_menu(cb.message, _ch_menu_text(), parse_mode="Markdown", reply_markup=ch_menu_kb())
    try:
        await cb.answer()
    except Exception:
        pass


def _ch_format_report(
    srv: dict,
    host: str,
    ok_nodes: int,
    total_nodes: int,
    threshold: int,
    link: str,
    details: list[str],
    ts_tehran: str = "",
    status_line: str = "",
    status: str = "",
    confirmed_checks: int = 1,
    ok_confirmed_checks: int = 1,
) -> str:
    """Pretty report block for a single server in Persian UI."""
    sep = "──────────────────────────────"
    name = srv.get("name") if isinstance(srv, dict) else srv
    # If srv was passed as just name string in some contexts (legacy)

    # Normalize details: keep each entry on its own line
    details_lines = [d.strip() for d in (details or []) if str(d).strip()]
    if not details_lines:
        details_lines = ["(بدون جزئیات)"]

    block = [
        sep,
        f"🖥 سرور: {name}",
        f"🌐 Host: {host}",
        f"📡 نتیجه نودهای ایران: {ok_nodes}/{total_nodes}",
        f"🚧 آستانه هشدار: کمتر از {threshold}/{total_nodes}",
    ]

    if not ts_tehran:
        ts_tehran = _ch_tehran_now()
    
    if link:
        block.append(f"📎 لینک: {link}")
    
    block.append(f"⏱️ زمان: {ts_tehran} (Asia/Tehran)")

    # Construct status line if not provided
    if not status_line:
        if status == "FAIL":
            status_line = "❌ مشکل"
            if confirmed_checks > 1:
                status_line += f"\n🧾 Fail confirmed after {confirmed_checks} checks"
        else:
            status_line = "✅ OK"
            if ok_confirmed_checks > 1:
                status_line += f"\n🧾 OK confirmed after {ok_confirmed_checks} checks"

    # status_line may include newlines; show it cleanly
    for i, ln in enumerate(str(status_line).splitlines()):
        if i == 0:
            block.append(f"وضعیت: {ln}")
        else:
            block.append(ln)

    block.append("")
    block.append(f"📄 جزئیات ({ok_nodes}/{total_nodes}):")
    block.extend(details_lines)
    block.append(sep)
    return "\n".join(block)

def _ch_notify_targets() -> list[int]:
    owner_id = get_owner_id()
    chat_id = ch_get_notify_chat_id()
    targets: list[int] = []
    if owner_id:
        targets.append(int(owner_id))
    if chat_id and int(chat_id) not in targets:
        targets.append(int(chat_id))
    return targets


async def _ch_send_notify(bot: Bot, text: str) -> bool:
    for cid in _ch_notify_targets():
        try:
            await bot.send_message(cid, text, disable_web_page_preview=True)
            return True
        except Exception:
            continue
    return False

async def _ch_do_one(host: str, nodes: list[str]) -> tuple[int, int, str, list[str], Optional[str]]:
    """Run one check-host ping for selected nodes.

    Returns: (ok_nodes, total_nodes, link, details_lines, err_text)
    """
    try:
        res = await run_ping_check(host, nodes=nodes, max_wait_sec=90, poll_interval_sec=2.0)
    except CheckHostError as e:
        return (0, len(nodes), "", [f"⚠️ خطا: {e}"], str(e))
    except Exception as e:
        return (0, len(nodes), "", [f"⚠️ خطای غیرمنتظره: {e}"], str(e))

    total = res.total_nodes
    ok_nodes = res.ok_nodes
    link = res.permanent_link or ""

    details = []
    for node in CH_IR_NODES:   # ← اینجا باید هم‌سطح با details باشه (۴ space)
        okc = res.per_node_ok.get(node, 0)
        icon = "✅" if okc == res.packets_per_node else "⚠️"
        node_name = CH_IR_NODE_LABELS.get(node, node)   # اسم شهر یا fallback به hostname
        details.append(f"{icon} {node_name}: {okc}/{res.packets_per_node}")

    return (ok_nodes, total, link, details, None)


async def _ch_confirm_fail(host: str, nodes: list[str], threshold: int, checks: int, delay_s: int):
    """
    Confirm FAIL state by re-checking up to `checks` times.
    Returns: (ok_nodes, total_nodes, link, details, status_now, checks_used)
    status_now is "OK" if it recovered during confirmation, else "FAIL".
    """
    last_ok, last_total, last_link, last_details, last_err = 0, len(nodes), "", [], ""
    checks = max(1, int(checks))
    delay_s = max(0, int(delay_s))

    for i in range(1, checks + 1):
        ok_nodes, total_nodes, link, details, err = await _ch_do_one(host, nodes)
        last_ok, last_total, last_link, last_details, last_err = ok_nodes, total_nodes, link, details, err or ""
        status_now = "OK" if ok_nodes >= threshold else "FAIL"
        if status_now == "OK":
            return (ok_nodes, total_nodes, link, details, "OK", i)
        if i < checks and delay_s > 0:
            await asyncio.sleep(delay_s)

    return (last_ok, last_total, last_link, last_details, "FAIL", checks)

async def _ch_confirm_ok(host: str, nodes: list[str], threshold: int, checks: int, delay_s: int):
    """
    Confirm OK (recovery) by requiring `checks` consecutive OK results.
    Returns: (ok_nodes, total_nodes, link, details, status_now, checks_used)
    status_now is "OK" only if all confirmation checks are OK; otherwise "FAIL".
    """
    last_ok, last_total, last_link, last_details, last_err = 0, len(nodes), "", [], ""
    checks = max(1, int(checks))
    delay_s = max(0, int(delay_s))

    for i in range(1, checks + 1):
        ok_nodes, total_nodes, link, details, err = await _ch_do_one(host, nodes)
        last_ok, last_total, last_link, last_details, last_err = ok_nodes, total_nodes, link, details, err or ""
        status_now = "OK" if ok_nodes >= threshold else "FAIL"
        if status_now != "OK":
            # not recovered yet
            return (ok_nodes, total_nodes, link, details, "FAIL", i)
        if i < checks and delay_s > 0:
            await asyncio.sleep(delay_s)

    return (last_ok, last_total, last_link, last_details, "OK", checks)

async def _ch_run_once_and_notify(bot: Bot, manual: bool = False) -> str:
    # ۱. گرفتن تمام آیدی‌ها بدون قید و شرط
    targets = ch_get_targets() 
    
    if not targets:
        return "No Targets Found"

    # اضافه کردن این پرینت برای اطمینان در ترمینال
    print(f"--- [Log] Processing {len(targets)} servers ---")

    nodes = ch_nodes_list()
    threshold = min(ch_threshold(), len(nodes)) if nodes else 0
    lines: list[str] = []
    
    for sid in targets:
        # استخراج اطلاعات سرور از دیتابیس
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT name, host FROM servers WHERE id=?", (sid,))
        srv_info = cur.fetchone()
        conn.close()
        
        if not srv_info:
            continue

        name, host = srv_info["name"], srv_info["host"]

        # انجام عملیات پایش از نودها
        ok_nodes, total_nodes, link, details, err = await _ch_do_one(host, nodes)
        status_now = "OK" if ok_nodes >= threshold else "FAIL"
        
        # ثبت تاریخچه در دیتابیس
        ch_set_last_status(sid, status_now)
        ch_add_history(sid, host, ok_nodes, total_nodes, status_now, link, details, err or "")

        # --- بخش ارسال اعلان اتوماتیک (فقط در اجرای زمان‌بندی شده) ---
        if not manual:
            auto_prev = ch_get_auto_status(sid)
            
            # --- شروع منطق تایید خطا (تکرار و تاخیر) ---
            confirmed_checks = 1
            if status_now == "FAIL":
                # دریافت مقادیر تنظیم شده توسط شما در پنل مدیریت
                fail_checks = ch_fail_confirm_checks() 
                retry_delay = ch_retry_delay_sec()    
                
                # بررسی مجدد: اگر خطا موقتی باشد، اینجا فیلتر می‌شود
                ok_nodes, total_nodes, link, details, status_now, confirmed_checks = await _ch_confirm_fail(
                    host, nodes, threshold, checks=fail_checks, delay_s=retry_delay
                )

            # ثبت وضعیت نهایی در دیتابیس (پس از تایید تکرارها)
            ch_set_auto_status(sid, status_now)
            
            # ارسال اعلان در صورت تایید نهایی خرابی
            if status_now == "FAIL":
                report = _ch_format_report(
                    srv=name, host=host, ok_nodes=ok_nodes, total_nodes=total_nodes, 
                    threshold=threshold, link=link, details=details, status="FAIL",
                    confirmed_checks=confirmed_checks
                )
                try:
                    await bot.send_message(chat_id=OWNER, text=report)
                except:
                    pass
            
            # ارسال اعلان رفع خرابی
            elif auto_prev == "FAIL" and status_now == "OK":
                report = _ch_format_report(
                    srv=name, host=host, ok_nodes=ok_nodes, total_nodes=total_nodes, 
                    threshold=threshold, link=link, details=details, status="OK"
                )
                try:
                    await bot.send_message(chat_id=OWNER, text=report)
                except:
                    pass

        # ساخت گزارش تجمیعی برای پاسخ به دکمه دستی تلگرام
        if manual:
            status_text = "✅ OK" if status_now == "OK" else "❌ FAIL"
            lines.append(_ch_format_report(
                srv=name, host=host, ok_nodes=ok_nodes, total_nodes=total_nodes, 
                threshold=threshold, link=link, details=details, status_line=status_text
            ))
            lines.append("──────────────────────────────")

    # خروجی نهایی
    if manual:
        hdr = BOT_HEADER + "\n\n🌐 پایش ایران (check-host.net)\n\n✅ اجرای دستی"
        return hdr + "\n\n" + "\n".join(lines)
    
    return "OK"

async def checkhost_job(bot: Bot):
    """
    زمان‌بندی پایش مطابق با دکمه‌های پنل مدیریت.
    """
    while True:
        try:
            now = int(time.time())
            
            # ۱. خواندن مقدار تنظیم شده (نام کلید باید دقیقاً ch_interval_hours باشد)
            interval_val = get_setting("ch_interval_hours", "1") 

            # ۲. منطق تشخیص نوع زمان‌بندی
            if interval_val == "test":
                interval_seconds = 60  # حالت تست: ۶۰ ثانیه
                display_time = "60 Seconds (Test Mode)"
            elif interval_val == "0":
                # اگر غیرفعال بود، ۱۰ ثانیه صبر کن و دوباره چک کن (تا اگر کاربر تغییر داد متوجه شویم)
                await asyncio.sleep(10)
                continue
            else:
                # تبدیل ساعت به ثانیه
                try:
                    interval_hours = int(interval_val)
                    if interval_hours <= 0:
                        await asyncio.sleep(10)
                        continue
                    interval_seconds = interval_hours * 3600
                    display_time = f"{interval_hours} Hour(s)"
                except ValueError:
                    # اگر به هر دلیلی مقدار عجیبی در دیتابیس بود
                    interval_seconds = 3600 
                    display_time = "1 Hour (Fallback)"

            # ۳. بررسی زمان آخرین اجرا
            # نکته: برای هماهنگی با بقیه سورس شما، از ch_get_last_run_utc استفاده میکنیم یا کلید معمولی
            last_str = get_setting("ch_last_run_time", "0")
            last = int(last_str)

            if last == 0 or (now - last) >= interval_seconds:
                print(f"--- [Scheduler] Triggering: {display_time} ---")
                
                # بروزرسانی زمان اجرا
                set_setting("ch_last_run_time", str(now))
                
                # اجرای تابع پایش اصلی
                await _ch_run_once_and_notify(bot)
                
        except Exception as e:
            print(f"--- [Scheduler Error] {e} ---")

        # چک کردن وضعیت تنظیمات هر ۱۰ ثانیه
        await asyncio.sleep(10)

@dp.callback_query(F.data == "ch_history")
async def ch_history(cb: types.CallbackQuery):
    if not await _owner_only_cb(cb):
        return
    _ensure_checkhost_tables()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT h.ts, s.name, h.host, h.ok_nodes, h.total_nodes, h.status "
        "FROM checkhost_history h LEFT JOIN servers s ON s.id=h.server_id "
        "ORDER BY h.id DESC LIMIT 20"
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await _edit_menu(cb.message, BOT_HEADER + "\n\n📜 تاریخچه پایش\n\nخالی است.", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data="ch_menu")]]
        ))
        try:
            await cb.answer()
        except Exception:
            pass
        return

    lines = []
    for r in rows:
        ts = to_tehran(r["ts"])  # stored as UTC by sqlite
        name = r["name"] or "(deleted)"
        icon = "✅" if r["status"] == "OK" else "❌"
        lines.append(f"{icon} {ts} | {name} | {r['ok_nodes']}/{r['total_nodes']}")

    msg = BOT_HEADER + "\n\n📜 تاریخچه پایش (آخرین ۲۰ مورد)\n\n" + "\n".join(lines)
    await _edit_menu(cb.message, msg, reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data="ch_menu")]]
    ))
    try:
        await cb.answer()
    except Exception:
        pass


@dp.callback_query(F.data == "ch_run_now")
async def ch_run_now(cb: types.CallbackQuery):
    if not await _owner_only_cb(cb):
        return

    ch_set_notify_chat_id(cb.message.chat.id)
    # ACK quickly (avoid callback timeout)
    try:
        await cb.answer("⏳ در حال اجرا ...")
    except Exception:
        pass

    await _edit_menu(cb.message, BOT_HEADER + "\n\n⏳ در حال اجرای پایش ایران ...")
    summary = await _ch_run_once_and_notify(bot, manual=True)
    await _edit_menu(cb.message, summary, reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data="ch_menu")]]
    ))

# نمایش منوی تنظیمات
@dp.callback_query(F.data == "bot_settings")
async def bot_settings_menu(cb: types.CallbackQuery):
    if get_role(cb.from_user.id) != "owner":
        await cb.answer("دسترسی محدود به مالک ربات است.", show_alert=True)
        return
    await _edit_menu(cb.message, BOT_HEADER + "\n\n⚙️ **تنظیمات مدیریتی ربات:**\nیکی از موارد زیر را انتخاب کنید:", 
                     reply_markup=settings_kb())
    await cb.answer()

# ۱. هندلر درخواست عدد (ویرایش صفحه فعلی به جای ارسال پیام جدید)
@dp.callback_query(F.data == "set_ping_int")
async def ask_ping_interval(cb: types.CallbackQuery, state: FSMContext):
    if get_role(cb.from_user.id) != "owner": 
        return await cb.answer("دسترسی محدود!")
    
    current = get_ping_interval()
    
    # به جای cb.message.answer از edit_text استفاده می‌کنیم
    await cb.message.edit_text(
        f"⏱ **تنظیم زمان پایش سرورها**\n\n"
        f"زمان فعلی: `{current}` ثانیه\n\n"
        f"لطفاً عدد جدید را به **ثانیه** ارسال کنید:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 بازگشت", callback_data="bot_settings")]
        ])
    )
    
    # ذخیره ID پیامی که الان ویرایش کردیم تا بعداً دوباره ویرایشش کنیم
    await state.update_data(msg_id=cb.message.message_id)
    await state.set_state("waiting_for_ping_int")
    await cb.answer()

# ۲. هندلر دریافت عدد، حذف پیام کاربر و آپدیت کردن صفحه تنظیمات
@dp.message(F.text.isdigit(), StateFilter("waiting_for_ping_int"))
async def save_ping_interval(m: types.Message, state: FSMContext):
    val = m.text
    if int(val) < 5:
        # اگر عدد کوچک بود، یک اخطار موقت می‌دهد
        warn = await m.answer("❌ حداقل زمان پایش باید ۵ ثانیه باشد.")
        await asyncio.sleep(2)
        await warn.delete()
        await m.delete()
        return
        
    set_setting("ping_interval", str(val)) # ذخیره در دیتابیس
    
    # گرفتن اطلاعات ذخیره شده در استیت
    data = await state.get_data()
    msg_id = data.get("msg_id")
    
    # حذف عددی که کاربر تایپ کرده بود
    try:
        await m.delete()
    except:
        pass
    
    await state.clear()
    
    # ویرایش همان پیام قبلی و نمایش مقدار جدید (بازگشت به منوی تنظیم زمان)
    await bot.edit_message_text(
        chat_id=m.chat.id,
        message_id=msg_id,
        text=f"✅ با موفقیت ذخیره شد.\n\n⏱ **تنظیم زمان پایش سرورها**\n\n"
             f"زمان فعلی: `{val}` ثانیه\n\n"
             f"در صورت نیاز عدد جدید را ارسال کنید:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 بازگشت به تنظیمات", callback_data="bot_settings")]
        ])
    )
# ---------------- Main ----------------
async def main():
    # Optional: enable daily cleanup
    asyncio.create_task(cleanup_logs_job())
    asyncio.create_task(monitor_loop(bot))
    asyncio.create_task(checkhost_job(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    init_ssh_files()
    asyncio.run(main())