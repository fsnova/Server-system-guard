# -*- coding: utf-8 -*-
from __future__ import annotations
from zoneinfo import ZoneInfo
from datetime import datetime, timezone

TEHRAN_TZ = ZoneInfo("Asia/Tehran")

def utc_sqlite_to_tehran(ts: str | None) -> str:
    if not ts:
        return "-"
    # SQLite: "YYYY-MM-DD HH:MM:SS" (UTC)
    dt_utc = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(TEHRAN_TZ).strftime("%Y-%m-%d %H:%M:%S")

from dotenv import load_dotenv
load_dotenv()

import os
import asyncio
from datetime import datetime
from typing import Optional

import aiohttp
import json

from aiogram import Bot, Dispatcher, types, F
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


# ---------------- FSM: Log retention ----------------
class LogRetention(StatesGroup):
    days = State()

# ---------------- FSM: Check-host (Iran monitoring) ----------------
class CHInterval(StatesGroup):
    hours = State()

class CHNodes(StatesGroup):
    count = State()

class CHThreshold(StatesGroup):
    ok_nodes = State()


# ---------------- Config ----------------
OWNER = int(os.getenv("OWNER_ID") or os.getenv("OWNER") or "0")
BOT_TOKEN = os.getenv("BOT_TOKEN")

BOT_HEADER = (
    "🎛 Server system guard\n"
    "💎 | Version Bot: 1.5\n"
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


def get_log_retention_days() -> int:
    try:
        v = int(get_setting("log_retention_days", "7"))
        return max(1, min(365, v))
    except Exception:
        return 7


async def cleanup_logs_once(days: int) -> int:
    """Delete logs older than N days. Returns deleted row count."""
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM logs WHERE ts < datetime('now', ?)", (f"-{days} day",))
    before = cur.fetchone()["c"]
    cur.execute("DELETE FROM logs WHERE ts < datetime('now', ?)", (f"-{days} day",))
    conn.commit()
    conn.close()
    return int(before or 0)


async def cleanup_logs_job():
    """Periodic cleanup (daily)."""
    while True:
        try:
            days = get_log_retention_days()
            await cleanup_logs_once(days)
        except Exception:
            pass
        await asyncio.sleep(24 * 60 * 60)



# ---------------- Check-host.net (Iran monitoring) ----------------

def _ch_get_int(key: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(get_setting(key, str(default)))
        return max(lo, min(hi, v))
    except Exception:
        return default

def ch_nodes_count() -> int:
    # how many Iran nodes to use (we will take the first N available)
    return _ch_get_int("ch_nodes_count", 7, 1, 20)

def ch_threshold_oknodes() -> int:
    # minimum number of nodes that must be 4/4 for OK
    return _ch_get_int("ch_threshold_oknodes", ch_nodes_count(), 1, 20)

def ch_interval_hours() -> int:
    return _ch_get_int("ch_interval_hours", 2, 1, 48)

def ch_selected_servers() -> list[int]:
    raw = get_setting("ch_servers", "")
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return sorted(set(out))

def ch_set_selected_servers(ids: list[int]) -> None:
    set_setting("ch_servers", ",".join(str(i) for i in sorted(set(ids))))

def _ch_last_state_key(sid: int) -> str:
    return f"ch_last_ok_{sid}"

def _ch_get_last_ok(sid: int):
    v = get_setting(_ch_last_state_key(sid), "")
    if v == "1":
        return True
    if v == "0":
        return False
    return None

def _ch_set_last_ok(sid: int, ok: bool) -> None:
    set_setting(_ch_last_state_key(sid), "1" if ok else "0")

async def _ch_fetch_iran_nodes(session: aiohttp.ClientSession) -> list[str]:
    """Fetch Iran nodes list from check-host.net."""
    url = "https://check-host.net/nodes/hosts"
    async with session.get(url, headers={"Accept": "application/json"}) as resp:
        data = await resp.json(content_type=None)
    nodes = data.get("nodes", {}) if isinstance(data, dict) else {}
    iran: list[str] = []
    for node_id, meta in nodes.items():
        try:
            loc = meta.get("location") or []
            if len(loc) >= 1 and str(loc[0]).lower() == "ir":
                iran.append(node_id)
        except Exception:
            continue
    iran.sort()
    return iran

def _ch_parse_node_score(v) -> int:
    """Return 0..4 (OK count) or -1 if still running."""
    if v is None:
        return -1
    try:
        if v == [[None]]:
            return 0
        pings = v[0]
        ok = 0
        for it in pings:
            if isinstance(it, list) and len(it) >= 1 and it[0] == "OK":
                ok += 1
        return int(ok)
    except Exception:
        return 0

async def _ch_ping_once(host: str, nodes: list[str]) -> tuple[dict[str, int], str]:
    """Run one ping via check-host.net and return (scores, permanent_link)."""
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        params = [("host", host), ("max_nodes", str(len(nodes)))]
        for n in nodes:
            params.append(("node", n))

        async with session.get("https://check-host.net/check-ping", params=params, headers={"Accept": "application/json"}) as resp:
            req = await resp.json(content_type=None)

        request_id = req.get("request_id")
        perm = req.get("permanent_link") or ""
        if not request_id:
            raise RuntimeError("check-host: request_id not returned")

        results_url = f"https://check-host.net/check-result/{request_id}"
        node_scores: dict[str, int] = {n: -1 for n in nodes}

        for _ in range(18):  # ~27s
            async with session.get(results_url, headers={"Accept": "application/json"}) as resp:
                res = await resp.json(content_type=None)

            done = True
            if isinstance(res, dict):
                for n in nodes:
                    score = _ch_parse_node_score(res.get(n))
                    node_scores[n] = score
                    if score == -1:
                        done = False

            if done:
                break
            await asyncio.sleep(1.5)

        for n, sc in list(node_scores.items()):
            if sc == -1:
                node_scores[n] = 0

        return node_scores, perm

def _ch_fmt_node_line(node_id: str, score: int) -> str:
    short = node_id.split(".")[0]
    emoji = "✅" if score == 4 else "⚠️"
    return f"{emoji} {short}: {score}/4"

async def _ch_run_for_server(sid: int, owner_id: int, manual: bool = False) -> dict:
    """Run check-host for one server and notify owner if state changed (or manual)."""
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id,name,host FROM servers WHERE id=?", (sid,))
    srv = cur.fetchone()
    conn.close()
    if not srv:
        return {"sid": sid, "error": "server_not_found"}

    host = str(srv["host"]).strip()
    if not host:
        return {"sid": sid, "error": "host_empty"}

    nodes_n = ch_nodes_count()
    thr = ch_threshold_oknodes()

    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        iran_nodes = await _ch_fetch_iran_nodes(session)

    if not iran_nodes:
        return {"sid": sid, "error": "no_iran_nodes"}

    nodes = iran_nodes[: min(nodes_n, len(iran_nodes))]
    thr = max(1, min(thr, len(nodes)))

    scores, perm = await _ch_ping_once(host, nodes)
    ok_nodes = sum(1 for s in scores.values() if s == 4)
    is_ok = ok_nodes >= thr

    last = _ch_get_last_ok(sid)
    notify = (last is None) or (last != is_ok) or manual

    if notify:
        status_line = "✅ OK" if is_ok else "❌ مشکل"
        tehran_time = datetime.now(TEHRAN_TZ).strftime("%Y-%m-%d %H:%M:%S")
        lines = "\n".join(_ch_fmt_node_line(n, scores.get(n, 0)) for n in nodes)

        msg = (
            f"{BOT_HEADER}\n\n"
            f"🌐 پایش ایران (check-host.net)\n"
            f"🖥 سرور: {srv['name']}\n"
            f"🌐 Host: {host}\n"
            f"📡 نتیجه نودهای ایران: {ok_nodes}/{len(nodes)} (نودهای 4/4)\n"
            f"🚧 آستانه هشدار: کمتر از {thr}/{len(nodes)}\n"
            f"📎 لینک: {perm}\n"
            f"⏱ زمان: {tehran_time} (Asia/Tehran)\n"
            f"📄 جزئیات:\n{lines}\n"
            f"وضعیت: {status_line}"
        )
        try:
            await bot.send_message(owner_id, msg)
        except Exception:
            pass

    _ch_set_last_ok(sid, is_ok)
    return {"sid": sid, "ok_nodes": ok_nodes, "total_nodes": len(nodes), "thr": thr, "ok": is_ok}

async def _ch_job_loop():
    # periodic runner
    await asyncio.sleep(10)
    while True:
        try:
            owner_id = get_owner_id()
            if owner_id:
                for sid in ch_selected_servers():
                    try:
                        await _ch_run_for_server(sid, owner_id, manual=False)
                    except Exception:
                        pass
        except Exception:
            pass
        await asyncio.sleep(ch_interval_hours() * 60 * 60)

def ch_menu_kb() -> InlineKeyboardMarkup:
    n = ch_nodes_count()
    thr = min(ch_threshold_oknodes(), n)
    h = ch_interval_hours()
    sel = ch_selected_servers()
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🖥 انتخاب سرورها ({len(sel)})", callback_data="ch_select")],
        [InlineKeyboardButton(text=f"🌐 تعداد نودهای ایران: {n}", callback_data="ch_set_nodes")],
        [InlineKeyboardButton(text=f"🚧 آستانه OK: {thr}", callback_data="ch_set_thr")],
        [InlineKeyboardButton(text=f"⏱ اجرا هر: {h} ساعت", callback_data="ch_set_interval")],
        [InlineKeyboardButton(text="▶️ اجرای دستی همین الان", callback_data="ch_run_now")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="home")],
    ])

def ch_select_kb(servers, selected: set[int]) -> InlineKeyboardMarkup:
    rows = []
    for s in servers:
        sid = int(s["id"])
        mark = "✅" if sid in selected else "☑️"
        rows.append([InlineKeyboardButton(text=f"{mark} {s['name']}", callback_data=f"ch_toggle:{sid}")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="ch_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def ch_cancel_kb(back: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ لغو", callback_data="cancel_fsm")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data=back)],
    ])
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
        [InlineKeyboardButton(text="📊 داشبورد گرافیکی", callback_data="dashboard")],
        [InlineKeyboardButton(text="📋 سرورها", callback_data="servers")],
    ]
    if role in ("owner", "admin"):
        rows.append([InlineKeyboardButton(text="➕ افزودن سرور", callback_data="add")])
    if role == "owner":
        rows.append([InlineKeyboardButton(text="👥 مدیریت Admin از داخل UI", callback_data="admin_panel")])
        rows.append([InlineKeyboardButton(text="🧹 مدیریت لاگ‌ها", callback_data="log_admin")])
        rows.append([InlineKeyboardButton(text="🌐 پایش ایران (check-host.net)", callback_data="ch_menu")])
    rows.append([InlineKeyboardButton(text="📜 لاگ‌ها", callback_data="logs")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def servers_list_kb(servers) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"🖥 {s['name']}", callback_data=f"srv:{int(s['id'])}")] for s in servers]
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def server_actions_kb(role: str, sid: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="Status live (🟢🔴)", callback_data=f"status:{sid}")]]
    if role in ("owner", "admin"):
        rows.append([InlineKeyboardButton(text="🔄 ریبوت", callback_data=f"reboot:{sid}")])
        rows.append([InlineKeyboardButton(text="🗑 حذف", callback_data=f"del:{sid}")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="servers")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def status_kb(sid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"srv:{sid}")],
            [InlineKeyboardButton(text="🏠 خانه", callback_data="home")],
        ]
    )


def log_admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧹 پاک‌سازی لاگ‌های قدیمی", callback_data="log_cleanup")],
            [InlineKeyboardButton(text="⏱ تنظیم تعداد روز نگهداری", callback_data="log_set_retention")],
            [InlineKeyboardButton(text="📦 آرشیو لاگ‌ها به فایل", callback_data="log_export")],
            [InlineKeyboardButton(text="🔙 بازگشت", callback_data="home")],
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
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="home")])
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
            [InlineKeyboardButton(text="❌ لغو", callback_data="cancel_fsm")],
            [InlineKeyboardButton(text="🏠 خانه", callback_data="home")],
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
    if not await guard_cb(cb):
        return
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT s.id, s.name, s.host, s.port, ss.last_status, ss.last_check_ts "
        "FROM servers s "
        "LEFT JOIN server_status ss ON ss.server_id=s.id "
        "ORDER BY s.id DESC"
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await _edit_menu(
            cb.message,
            BOT_HEADER + "\n\n📊 داشبورد\n\nهیچ سروری ثبت نشده.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data="home")]]
            ),
        )
        await cb.answer()
        return

    up = sum(1 for r in rows if (r["last_status"] or "DOWN") == "UP")
    down = len(rows) - up
    bar = "█" * up + "░" * down

    lines = []
    for r in rows:
        st = r["last_status"] or "DOWN"
        lines.append(f"• {badge(st)} | **{r['name']}** (`{r['host']}:{r['port']}`)")

    txt = (
        BOT_HEADER
        + "\n\n📊 **Dashboard (گرافیکی)**\n"
        + f"Status live: 🟢 {up} | 🔴 {down} | مجموع: {len(rows)}\n"
        + f"`{bar}`\n\n"
        + "\n".join(lines)
    )
    await _edit_menu(
        cb.message,
        txt,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data="home")]]
        ),
    )
    await cb.answer()


@dp.callback_query(F.data == "servers")
async def servers(cb: types.CallbackQuery):
    if not await guard_cb(cb):
        return
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id,name,host,port FROM servers ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await _edit_menu(
            cb.message,
            BOT_HEADER + "\n\n📋 سرورها\n\nهیچ سروری ثبت نشده.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data="home")]]
            ),
        )
        await cb.answer()
        return

    await _edit_menu(cb.message, BOT_HEADER + "\n\n📋 سرورها", reply_markup=servers_list_kb(rows))
    await cb.answer()


@dp.callback_query(F.data.startswith("srv:"))
async def srv(cb: types.CallbackQuery):
    if not await guard_cb(cb):
        return
    role = get_role(cb.from_user.id)
    sid = int(cb.data.split(":")[1])

    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT s.name,s.host,s.port,ss.last_status,ss.last_check_ts "
        "FROM servers s "
        "LEFT JOIN server_status ss ON ss.server_id=s.id "
        "WHERE s.id=?",
        (sid,),
    )
    r = cur.fetchone()
    conn.close()
    if not r:
        await cb.answer("سرور پیدا نشد", show_alert=True)
        return

    st = r["last_status"] or "DOWN"
    txt = (
        BOT_HEADER
        + f"\n\n🖥 **{r['name']}**\n"
        + f"🌐 `{r['host']}:{r['port']}`\n"
        + f"Status live: {badge(st)}\n"
        f"⏱ آخرین چک: `{utc_sqlite_to_tehran(r['last_check_ts'])}`"
    )
    await _edit_menu(cb.message, txt, parse_mode="Markdown", reply_markup=server_actions_kb(role, sid))
    await cb.answer()


@dp.callback_query(F.data.startswith("status:"))
async def status(cb: types.CallbackQuery):
    if not await guard_cb(cb):
        return
    sid = int(cb.data.split(":")[1])

    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT s.name,s.host,s.port,ss.last_status,ss.last_check_ts "
        "FROM servers s "
        "LEFT JOIN server_status ss ON ss.server_id=s.id "
        "WHERE s.id=?",
        (sid,),
    )
    r = cur.fetchone()
    conn.close()
    if not r:
        await cb.answer("سرور پیدا نشد", show_alert=True)
        return
    st = r["last_status"] or "DOWN"

    txt = (
        BOT_HEADER
        + f"\n\n🖥 **{r['name']}**\n"
        + f"Status live: {badge(st)}\n"
        f"⏱ آخرین چک: `{utc_sqlite_to_tehran(r['last_check_ts'])}`"
    )
    await _edit_menu(cb.message, txt, parse_mode="Markdown", reply_markup=status_kb(sid))
    await cb.answer()


@dp.callback_query(F.data == "add")
async def add(cb: types.CallbackQuery, state: FSMContext):
    if not await guard_cb(cb):
        return
    role = get_role(cb.from_user.id)
    if role not in ("owner", "admin"):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    await state.set_state(AddServer.name)
    await _edit_menu(cb.message, BOT_HEADER + "\n\n➕ افزودن سرور\n\nنام سرور را ارسال کنید:", reply_markup=add_server_kb())
    await cb.answer()


@dp.message(AddServer.name)
async def add_name(m: types.Message, state: FSMContext):
    if not await guard_msg(m):
        return
    await state.update_data(name=(m.text or "").strip())
    await state.set_state(AddServer.host)
    await m.answer("IP/Host را ارسال کنید: (یا ❌ لغو)")


@dp.message(AddServer.host)
async def add_host(m: types.Message, state: FSMContext):
    if not await guard_msg(m):
        return
    await state.update_data(host=(m.text or "").strip())
    await state.set_state(AddServer.port)
    await m.answer("پورت SSH؟ (پیشفرض 22)")


@dp.message(AddServer.port)
async def add_port(m: types.Message, state: FSMContext):
    if not await guard_msg(m):
        return
    t = (m.text or "").strip()
    port = int(t) if t.isdigit() else 22
    await state.update_data(port=port)
    await state.set_state(AddServer.user)
    await m.answer("یوزرنیم SSH:")


@dp.message(AddServer.user)
async def add_user(m: types.Message, state: FSMContext):
    if not await guard_msg(m):
        return
    await state.update_data(user=(m.text or "").strip())
    await state.set_state(AddServer.pw)
    await m.answer("پسورد SSH:")


@dp.message(AddServer.pw)
async def add_pw(m: types.Message, state: FSMContext):
    if not await guard_msg(m):
        return
    data = await state.get_data()
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO servers(name,host,port,user,pw) VALUES (?,?,?,?,?)",
        (data["name"], data["host"], int(data["port"]), data["user"], enc((m.text or "").strip())),
    )
    conn.commit()
    conn.close()
    await state.clear()
    role = get_role(m.from_user.id)
    await m.answer(BOT_HEADER + "\n\n✅ سرور اضافه شد.", reply_markup=main_kb(role))


@dp.callback_query(F.data.startswith("reboot:"))
async def reboot_srv(cb: types.CallbackQuery):
    if not await guard_cb(cb):
        return
    role = get_role(cb.from_user.id)
    if role not in ("owner", "admin"):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    sid = int(cb.data.split(":")[1])

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT host,port,user,pw,name FROM servers WHERE id=?", (sid,))
    r = cur.fetchone()
    if not r:
        conn.close()
        await cb.answer("سرور پیدا نشد", show_alert=True)
        return

    await _edit_menu(cb.message, BOT_HEADER + f"\n\n⏳ در حال ریبوت {r['name']} ...")
    try:
        await reboot((r["host"], int(r["port"]), r["user"], r["pw"]))
        cur.execute("INSERT INTO logs(server_id,action,status) VALUES (?,?,?)", (sid, "REBOOT", "SENT"))
        conn.commit()
        await _edit_menu(
            cb.message,
            BOT_HEADER + "\n\n✅ انجام شد.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"srv:{sid}")]]
            ),
        )
    except Exception as e:
        cur.execute("INSERT INTO logs(server_id,action,status) VALUES (?,?,?)", (sid, "REBOOT", "ERR"))
        conn.commit()
        await _edit_menu(
            cb.message,
            BOT_HEADER + f"\n\n❌ خطا: {e}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"srv:{sid}")]]
            ),
        )
    finally:
        conn.close()
        await cb.answer()


@dp.callback_query(F.data.startswith("del:"))
async def del_srv(cb: types.CallbackQuery):
    if not await guard_cb(cb):
        return
    role = get_role(cb.from_user.id)
    if role not in ("owner", "admin"):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return
    sid = int(cb.data.split(":")[1])

    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM servers WHERE id=?", (sid,))
    cur.execute("DELETE FROM server_status WHERE server_id=?", (sid,))
    cur.execute("INSERT INTO logs(server_id,action,status) VALUES (?,?,?)", (sid, "DELETE", "OK"))
    conn.commit()
    conn.close()
    await _edit_menu(
        cb.message,
        BOT_HEADER + "\n\n🗑 حذف شد.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data="servers")]]
        ),
    )
    await cb.answer()


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
    await state.set_state(AdminAdd.uid)
    await _edit_menu(cb.message, BOT_HEADER + "\n\n🆔 آیدی عددی کاربر را ارسال کنید:", reply_markup=admin_add_kb())
    await cb.answer()


@dp.message(AdminAdd.uid)
async def admin_add_uid(m: types.Message, state: FSMContext):
    if not await guard_msg(m):
        return
    if get_role(m.from_user.id) != "owner":
        return
    t = (m.text or "").strip()
    if t.lower() in ("cancel", "/cancel", "بازگشت"):
        await state.clear()
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT uid,role FROM users ORDER BY role DESC, uid DESC")
        users = cur.fetchall()
        conn.close()
        await m.answer(BOT_HEADER + "\n\n👥 مدیریت Admin", reply_markup=admin_panel_kb(users))
        return
    if not t.isdigit():
        await m.answer("فقط عدد بفرست. (یا دکمه ❌ لغو)")
        return
    uid = int(t)
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users(uid,role) VALUES (?,?)", (uid, "admin"))
    cur.execute("UPDATE users SET role='admin' WHERE uid=? AND role!='owner'", (uid,))
    conn.commit()
    conn.close()
    await state.clear()

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT uid,role FROM users ORDER BY role DESC, uid DESC")
    users = cur.fetchall()
    conn.close()
    await m.answer(BOT_HEADER + f"\n\n✅ اضافه شد به Admin: `{uid}`", parse_mode="Markdown", reply_markup=admin_panel_kb(users))


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
    await state.set_state(LogRetention.days)
    cur = get_log_retention_days()
    await _edit_menu(
        cb.message,
        BOT_HEADER + f"\n\n⏱ تعداد روز نگهداری لاگ‌ها را ارسال کنید (1 تا 365).\nوضعیت فعلی: {cur} روز",
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
    if not t.isdigit():
        await m.answer("فقط عدد بفرست (1 تا 365). یا از دکمه ❌ لغو استفاده کن.")
        return
    days = int(t)
    if days < 1 or days > 365:
        await m.answer("عدد باید بین 1 تا 365 باشد.")
        return

    set_setting("log_retention_days", str(days))
    await state.clear()

    new_days = get_log_retention_days()
    msg = (
        BOT_HEADER
        + "\n\n🧹 **مدیریت لاگ‌ها**\n"
        + f"⏱ نگهداری فعلی: **{new_days} روز**\n\n"
        + "گزینه‌ها را انتخاب کنید:"
    )
    # Note: this is a Message (not callback). It will send a message if it can't edit.
    await _edit_menu(m, msg, parse_mode="Markdown", reply_markup=log_admin_kb())


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
    cur.execute("SELECT server_id,action,status,ts FROM logs ORDER BY id DESC LIMIT 50")
    rows = cur.fetchall()
    conn.close()
    if not rows:
        await _edit_menu(
            cb.message,
            BOT_HEADER + "\n\n📜 لاگ‌ها\n\nخالی است.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data="home")]]
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
            inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data="home")]]
        ),
    )
    await cb.answer()



# ---------------- Owner UI: Check-host.net ----------------
@dp.callback_query(F.data == "ch_menu")
async def ch_menu(cb: types.CallbackQuery):
    if not await guard_cb(cb):
        return
    if get_role(cb.from_user.id) != "owner":
        await cb.answer("فقط Owner", show_alert=True)
        return
    sel = ch_selected_servers()
    n = ch_nodes_count()
    thr = min(ch_threshold_oknodes(), n)
    msg = (
        BOT_HEADER
        + "\n\n🌐 پایش ایران (check-host.net)\n"
        + f"🌐 نودهای ایران: {n}\n"
        + f"🚧 آستانه هشدار: کمتر از {thr}/{n}\n"
        + f"⏱️ اجرا هر: {ch_interval_hours()} ساعت\n"
        + f"🖥 سرورهای انتخاب‌شده: {len(sel)}"
    )
    await _edit_menu(cb.message, msg, reply_markup=ch_menu_kb())
    await cb.answer()

@dp.callback_query(F.data == "ch_select")
async def ch_select(cb: types.CallbackQuery):
    if not await guard_cb(cb):
        return
    if get_role(cb.from_user.id) != "owner":
        await cb.answer("فقط Owner", show_alert=True)
        return
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id,name FROM servers ORDER BY id DESC")
    servers = cur.fetchall()
    conn.close()
    selected = set(ch_selected_servers())
    await _edit_menu(cb.message, BOT_HEADER + "\n\n🖥 انتخاب سرورها برای پایش ایران:", reply_markup=ch_select_kb(servers, selected))
    await cb.answer()

@dp.callback_query(F.data.startswith("ch_toggle:"))
async def ch_toggle(cb: types.CallbackQuery):
    if not await guard_cb(cb):
        return
    if get_role(cb.from_user.id) != "owner":
        await cb.answer("فقط Owner", show_alert=True)
        return
    sid = int(cb.data.split(":")[1])
    current = set(ch_selected_servers())
    if sid in current:
        current.remove(sid)
    else:
        current.add(sid)
    ch_set_selected_servers(list(current))

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id,name FROM servers ORDER BY id DESC")
    servers = cur.fetchall()
    conn.close()
    await _edit_menu(cb.message, BOT_HEADER + "\n\n🖥 انتخاب سرورها برای پایش ایران:", reply_markup=ch_select_kb(servers, current))
    await cb.answer()

@dp.callback_query(F.data == "ch_set_interval")
async def ch_set_interval(cb: types.CallbackQuery, state: FSMContext):
    if not await guard_cb(cb):
        return
    if get_role(cb.from_user.id) != "owner":
        await cb.answer("فقط Owner", show_alert=True)
        return
    await state.set_state(CHInterval.hours)
    await _edit_menu(cb.message, BOT_HEADER + f"\n\n⏱ عدد ساعت اجرا را ارسال کنید (1 تا 48).\nوضعیت فعلی: {ch_interval_hours()} ساعت", reply_markup=ch_cancel_kb("ch_menu"))
    await cb.answer()

@dp.message(CHInterval.hours)
async def ch_set_interval_msg(m: types.Message, state: FSMContext):
    if not await guard_msg(m):
        return
    if get_role(m.from_user.id) != "owner":
        return
    t = (m.text or "").strip()
    if not t.isdigit():
        await m.answer("فقط عدد 1 تا 48 بفرست.")
        return
    h = max(1, min(48, int(t)))
    set_setting("ch_interval_hours", str(h))
    await state.clear()
    await m.answer(BOT_HEADER + f"\n\n✅ تنظیم شد: اجرا هر {h} ساعت", reply_markup=ch_menu_kb())

@dp.callback_query(F.data == "ch_set_nodes")
async def ch_set_nodes(cb: types.CallbackQuery, state: FSMContext):
    if not await guard_cb(cb):
        return
    if get_role(cb.from_user.id) != "owner":
        await cb.answer("فقط Owner", show_alert=True)
        return
    await state.set_state(CHNodes.count)
    await _edit_menu(cb.message, BOT_HEADER + f"\n\n🌐 تعداد نودهای ایران را ارسال کنید (1 تا 10).\nوضعیت فعلی: {ch_nodes_count()}", reply_markup=ch_cancel_kb("ch_menu"))
    await cb.answer()

@dp.message(CHNodes.count)
async def ch_set_nodes_msg(m: types.Message, state: FSMContext):
    if not await guard_msg(m):
        return
    if get_role(m.from_user.id) != "owner":
        return
    t = (m.text or "").strip()
    if not t.isdigit():
        await m.answer("فقط عدد 1 تا 10 بفرست.")
        return
    n = max(1, min(10, int(t)))
    set_setting("ch_nodes_count", str(n))
    thr = min(ch_threshold_oknodes(), n)
    set_setting("ch_threshold_oknodes", str(thr))
    await state.clear()
    await m.answer(BOT_HEADER + f"\n\n✅ تنظیم شد: تعداد نودهای ایران = {n}", reply_markup=ch_menu_kb())

@dp.callback_query(F.data == "ch_set_thr")
async def ch_set_thr(cb: types.CallbackQuery, state: FSMContext):
    if not await guard_cb(cb):
        return
    if get_role(cb.from_user.id) != "owner":
        await cb.answer("فقط Owner", show_alert=True)
        return
    await state.set_state(CHThreshold.ok_nodes)
    n = ch_nodes_count()
    thr = min(ch_threshold_oknodes(), n)
    await _edit_menu(cb.message, BOT_HEADER + f"\n\n🚧 آستانه OK را ارسال کنید (1 تا {n}).\n(اگر تعداد نودهای 4/4 کمتر از این عدد شود هشدار می‌دهد)\nوضعیت فعلی: {thr}", reply_markup=ch_cancel_kb("ch_menu"))
    await cb.answer()

@dp.message(CHThreshold.ok_nodes)
async def ch_set_thr_msg(m: types.Message, state: FSMContext):
    if not await guard_msg(m):
        return
    if get_role(m.from_user.id) != "owner":
        return
    t = (m.text or "").strip()
    if not t.isdigit():
        await m.answer("فقط عدد بفرست.")
        return
    n = ch_nodes_count()
    thr = max(1, min(n, int(t)))
    set_setting("ch_threshold_oknodes", str(thr))
    await state.clear()
    await m.answer(BOT_HEADER + f"\n\n✅ تنظیم شد: آستانه OK = {thr}/{n}", reply_markup=ch_menu_kb())

@dp.callback_query(F.data == "ch_run_now")
async def ch_run_now(cb: types.CallbackQuery):
    if not await guard_cb(cb):
        return
    if get_role(cb.from_user.id) != "owner":
        await cb.answer("فقط Owner", show_alert=True)
        return
    owner_id = get_owner_id()
    sel = ch_selected_servers()
    if not sel:
        await cb.answer("هیچ سروری انتخاب نشده", show_alert=True)
        return
    await _edit_menu(cb.message, BOT_HEADER + "\n\n⏳ در حال اجرای دستی پایش ایران ...")
    done = 0
    for sid in sel:
        try:
            await _ch_run_for_server(sid, owner_id, manual=True)
            done += 1
        except Exception:
            pass
    await _edit_menu(cb.message, BOT_HEADER + f"\n\n✅ اجرا شد برای {done} سرور.", reply_markup=ch_menu_kb())
    await cb.answer()
@dp.callback_query(F.data == "cancel_fsm")
async def cancel_fsm(cb: types.CallbackQuery, state: FSMContext):
    if not await guard_cb(cb):
        return
    await state.clear()
    role = get_role(cb.from_user.id)
    await _edit_menu(cb.message, BOT_HEADER + "\n\nلغو شد.", reply_markup=main_kb(role))
    await cb.answer()


# ---------------- Main ----------------
async def main():
    # Optional: enable daily cleanup
    asyncio.create_task(cleanup_logs_job())
    asyncio.create_task(_ch_job_loop())
    asyncio.create_task(monitor_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    init_ssh_files()
    asyncio.run(main())
