import asyncio
import socket
import os
from datetime import datetime, timezone

from db import db

INT = int(os.getenv("PING_INTERVAL", 30))
BOT_HEADER = "🎛 Server system guard\n💎 | Version Bot: 1.5\n🔹 | creator: @farhadasqarii"

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
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT id, name, host, port FROM servers")
        servers = cur.fetchall()

        for row in servers:
            sid = int(row["id"])
            name = row["name"]
            host = row["host"]
            port = int(row["port"] or 22)

            st = await _check_ssh(host, port=port)

            cur.execute("INSERT INTO logs(server_id, action, status) VALUES (?,?,?)", (sid, "MON", st))

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

            if prev_status and prev_status != st and st == "DOWN":
                cur.execute("SELECT uid FROM users WHERE role IN ('owner','admin')")
                uids = [int(r["uid"]) for r in cur.fetchall()]
                msg = (
                    f"{BOT_HEADER}\n\n"
                    "🔔 **Server DOWN شد!**\n"
                    f"🖥 نام: **{name}**\n"
                    f"🌐 هاست: `{host}:{port}`\n"
                    "Status live: 🔴 DOWN\n"
                    f"⏱ زمان: `{now} UTC`"
                )
                for uid in uids:
                    try:
                        await bot.send_message(uid, msg, parse_mode="Markdown")
                    except Exception:
                        pass
                cur.execute("UPDATE server_status SET last_notified_ts=? WHERE server_id=?", (now, sid))

        conn.commit()
        conn.close()
        await asyncio.sleep(INT)
