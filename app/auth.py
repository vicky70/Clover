"""Device tokens. Slice 1: `clover init` trades the server's admin secret for one (R3-25)."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime

from app import db


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def admin_secret_ok(configured: str, given: str | None) -> bool:
    # An unset server secret refuses everyone rather than accepting an empty one.
    return bool(configured) and given is not None and hmac.compare_digest(configured.encode(), given.encode())


def init_device(conn: sqlite3.Connection, name: str, now: datetime) -> tuple[int, str]:
    """Create the user on first init (Slice 1 has one student) and issue a device token."""
    with db.tx(conn):
        row = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        if row:
            user_id = row["id"]
        else:
            user_id = conn.execute("INSERT INTO users (name, created_at) VALUES (?, ?)",
                                   (name, db.iso(now))).lastrowid
        token = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO devices (token_hash, user_id, created_at) VALUES (?, ?, ?)",
                     (hash_token(token), user_id, db.iso(now)))
    return user_id, token


def user_for_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT u.* FROM devices d JOIN users u ON u.id = d.user_id WHERE d.token_hash = ?",
        (hash_token(token),),
    ).fetchone()
