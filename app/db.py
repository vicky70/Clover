"""SQLite storage. WAL mode, busy_timeout=5000, short BEGIN IMMEDIATE write transactions.

Times are stored as UTC ISO-8601 strings (IST in, UTC stored, IST out).
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

IST = timezone(timedelta(hours=5, minutes=30), "IST")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    track TEXT NOT NULL DEFAULT 'docker',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedules (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    weekdays TEXT NOT NULL,          -- JSON list of ints, Monday=0
    start_local TEXT NOT NULL,       -- 'HH:MM' IST
    minutes INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS windows (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    opens_at TEXT NOT NULL,
    closes_at TEXT NOT NULL,
    task_id TEXT,                    -- assigned when the window opens
    UNIQUE (user_id, opens_at)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    window_id INTEGER,
    type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL         -- server receipt time
);
CREATE INDEX IF NOT EXISTS idx_events_window ON events(window_id);
CREATE TABLE IF NOT EXISTS witnesses (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    phone TEXT NOT NULL,             -- digits only, with country code
    code TEXT NOT NULL,
    state TEXT NOT NULL,             -- invited | active | opted_out | undeliverable
    delivery_failures INTEGER NOT NULL DEFAULT 0,
    invited_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    window_id INTEGER,
    kind TEXT NOT NULL,              -- miss | struggling | consent_ack
    witness_id INTEGER NOT NULL REFERENCES witnesses(id),
    params TEXT NOT NULL DEFAULT '{}',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    message_id TEXT,
    sent_at TEXT,
    skipped_reason TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (window_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_outbox_sent ON outbox(sent_at);
CREATE INDEX IF NOT EXISTS idx_outbox_message ON outbox(message_id);
CREATE TABLE IF NOT EXISTS processed_webhooks (
    key TEXT PRIMARY KEY,            -- wamid, or wamid:status for statuses
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def connect(path: str | Path) -> sqlite3.Connection:
    # isolation_level=None: we issue BEGIN IMMEDIATE ourselves, so a write never
    # has to upgrade a read lock (the case busy_timeout can't help with).
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(path: str | Path) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def add_event(conn: sqlite3.Connection, user_id: int, type_: str, now: datetime,
              window_id: int | None = None, **payload: Any) -> int:
    cur = conn.execute(
        "INSERT INTO events (user_id, window_id, type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, window_id, type_, json.dumps(payload), iso(now)),
    )
    return cur.lastrowid


def window_events(conn: sqlite3.Connection, window_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM events WHERE window_id = ? ORDER BY id", (window_id,)
    ).fetchall()
    return [_event(r) for r in rows]


def _event(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "type": row["type"],
        "payload": json.loads(row["payload"]),
        "created_at": parse(row["created_at"]),
    }


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
