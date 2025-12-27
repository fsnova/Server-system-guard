import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "data", "database.sqlite")

DB = os.getenv("DB_PATH") or DEFAULT_DB


def db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users(
            uid INTEGER UNIQUE,
            role TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS servers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            host TEXT NOT NULL,
            port INTEGER NOT NULL DEFAULT 22,
            user TEXT NOT NULL,
            pw TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id INTEGER,
            action TEXT,
            status TEXT,
            ts DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS server_status(
            server_id INTEGER PRIMARY KEY,
            last_status TEXT,
            last_check_ts DATETIME,
            last_change_ts DATETIME,
            last_notified_ts DATETIME
        )
    """)

    # ? settings table for log retention etc.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings(
            k TEXT PRIMARY KEY,
            v TEXT
        )
    """)

    conn.commit()
    conn.close()
