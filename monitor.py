# -*- coding: utf-8 -*-
import asyncio
import socket
import os
from datetime import datetime, timezone
from db import db

# هدر ربات با ایموجی‌های استاندارد
BOT_HEADER = "🎛 Server system guard\n💎 | Version Bot: 1.6\n🔹 | creator: @farhadasqarii"

def _utcnow_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

async def _check_ssh(host: str, port: int = 22, timeout: float = 3.0) -> str:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return "UP"
    except Exception:
        return "DOWN"
    finally:
        try:
            s.close()
        except Exception:
            pass

async def loop(bot):
    while True:
        try:
            from bot import get_ping_interval
            interval = get_ping_interval()
        except Exception:
            interval = 30

        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT id, name, host, port FROM servers")
        servers = cur.fetchall()

        for row in servers:
            sid = int(row["id"])
            name = row["name"]
            host = row["host"]
            port = int(row["port"] or 22)

            # چک کردن وضعیت سرور
            st = await _check_ssh(host, port=port)

            # ثبت در لاگ
            cur.execute("INSERT INTO logs(server_id, action, status) VALUES (?,?,?)", (sid, "MON", st))

            # بررسی وضعیت قبلی
            cur.execute("SELECT last_status FROM server_status WHERE server_id=?", (sid,))
            prev = cur.fetchone()
            prev_status = prev["last_status"] if prev else None

            now = _utcnow_str()

            if not prev:
                cur.execute(
                    "INSERT INTO server_status(server_id,last_status,last_check_ts,last_change_ts) VALUES (?,?,?,?)",
                    (sid, st, now, now),
                )
            else:
                if prev_status != st:
                    cur.execute(
                        "UPDATE server_status SET last_status=?, last_check_ts=?, last_change_ts=? WHERE server_id=?",
                        (st, now, now, sid),
                    )
                else:
                    cur.execute("UPDATE server_status SET last_check_ts=? WHERE server_id=?", (now, sid))

            # ارسال اعلان در صورت تغییر وضعیت
            if prev_status and prev_status != st:
                cur.execute("SELECT uid FROM users WHERE role IN ('owner','admin')")
                uids = [int(r["uid"]) for r in cur.fetchall()]
                
                if st == "DOWN":
                    status_emoji = "🚨"
                    status_text = "DOWN (قطع شده)"
                else:
                    status_emoji = "✅"
                    status_text = "UP (متصل شد)"

                msg = (
                    f"{BOT_HEADER}\n\n"
                    f"{status_emoji} **تغییر وضعیت سرور**\n"
                    f"🔹 نام سرور: **{name}**\n"
                    f"🌐 آدرس: `{host}:{port}`\n"
                    f"📊 وضعیت فعلی: **{status_text}**\n"
                    f"⏰ زمان: `{now} UTC`"
                )

                for uid in uids:
                    try:
                        await bot.send_message(uid, msg, parse_mode="Markdown")
                    except Exception:
                        pass
                
                cur.execute("UPDATE server_status SET last_notified_ts=? WHERE server_id=?", (now, sid))

        conn.commit()
        conn.close()
        await asyncio.sleep(interval)