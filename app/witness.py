"""Witness onboarding and state: invited -> active | opted_out | undeliverable.

Clover never messages a witness first. Consent is the witness's own message
(with the invite code) to the business number, sent from the registered phone.
"""
from __future__ import annotations

import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from urllib.parse import quote

from app import db, whatsapp

INVITE_TTL = timedelta(days=7)
UNDELIVERABLE_AFTER = 2
STOP_WORDS = {"STOP", "UNSUBSCRIBE"}


class WitnessError(ValueError):
    pass


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 10:
        raise WitnessError("phone must include the country code, e.g. +91 98765 43210")
    return digits


def consent_text(student: str, code: str) -> str:
    return f"I agree to be {student}'s Clover witness. Code {code}"


def display_state(row: sqlite3.Row, now: datetime) -> str:
    if row["state"] == "invited" and now - db.parse(row["invited_at"]) > INVITE_TTL:
        return "expired"
    return row["state"]


def current(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM witnesses WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                        (user_id,)).fetchone()


def active_witness(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM witnesses WHERE user_id = ? AND state = 'active' ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()


def invite(conn: sqlite3.Connection, user: sqlite3.Row, name: str, phone: str,
           business_number: str, now: datetime) -> dict:
    phone = normalize_phone(phone)
    existing = current(conn, user["id"])
    if existing and display_state(existing, now) in ("invited", "active"):
        raise WitnessError(f"{existing['name']} is already {display_state(existing, now)}; "
                           "removing a witness is an admin action in Slice 1")
    code = f"{secrets.randbelow(10000):04d}"
    with db.tx(conn):
        wid = conn.execute(
            "INSERT INTO witnesses (user_id, name, phone, code, state, invited_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'invited', ?, ?)",
            (user["id"], name, phone, code, db.iso(now), db.iso(now)),
        ).lastrowid
        db.add_event(conn, user["id"], "witness_invited", now, witness_id=wid)
    text = consent_text(user["name"], code)
    link = f"https://wa.me/{business_number}?text={quote(text)}" if business_number else None
    return {"witness_id": wid, "state": "invited", "code": code, "text": text, "link": link,
            "expires_at": db.iso(now + INVITE_TTL)}


def _set_state(conn, w: sqlite3.Row, state: str, now: datetime, event: str, **payload) -> None:
    conn.execute("UPDATE witnesses SET state = ?, updated_at = ? WHERE id = ?", (state, db.iso(now), w["id"]))
    db.add_event(conn, w["user_id"], event, now, witness_id=w["id"], **payload)


def handle_message(conn: sqlite3.Connection, now: datetime, from_phone: str, text: str, wamid: str) -> str:
    """Apply one inbound message. Returns what happened (for logs and tests)."""
    w = conn.execute("SELECT * FROM witnesses WHERE phone = ? ORDER BY id DESC LIMIT 1",
                     (re.sub(r"\D", "", from_phone),)).fetchone()
    if w is None:
        return "unknown_sender"
    with db.tx(conn):
        db.add_event(conn, w["user_id"], "inbound_message", now, wamid=wamid, witness_id=w["id"])
        word = re.sub(r"[^A-Za-z]", "", text).upper()
        if word in STOP_WORDS:
            if w["state"] != "opted_out":
                _set_state(conn, w, "opted_out", now, "witness_opted_out")
            return "opted_out"
        if w["state"] != "invited":
            return "ignored"
        if display_state(w, now) == "expired":
            return "expired"
        if w["code"] not in re.findall(r"\d{4}", text):
            return "code_mismatch"
        _set_state(conn, w, "active", now, "witness_consented")
        student = conn.execute("SELECT name FROM users WHERE id = ?", (w["user_id"],)).fetchone()["name"]
        whatsapp.enqueue(conn, now, window_id=None, kind="consent_ack", witness_id=w["id"], params={
            "text": (f"Thanks {w['name']}. You are now {student}'s Clover witness. You'll get a message "
                     f"if {student} misses a scheduled task, and a weekly summary. Reply STOP anytime to stop."),
        })
    return "consented"


def handle_status(conn: sqlite3.Connection, now: datetime, message_id: str, status: str) -> str:
    """Delivery status for one of our messages. Two failures in a row -> undeliverable."""
    row = conn.execute(
        "SELECT w.* FROM outbox o JOIN witnesses w ON w.id = o.witness_id WHERE o.message_id = ?",
        (message_id,),
    ).fetchone()
    if row is None:
        return "unknown_message"
    with db.tx(conn):
        if status == "failed":
            failures = row["delivery_failures"] + 1
            conn.execute("UPDATE witnesses SET delivery_failures = ? WHERE id = ?", (failures, row["id"]))
            if failures >= UNDELIVERABLE_AFTER and row["state"] == "active":
                _set_state(conn, row, "undeliverable", now, "witness_undeliverable", message_id=message_id)
                return "undeliverable"
            return "failed"
        if status in ("delivered", "read"):
            conn.execute("UPDATE witnesses SET delivery_failures = 0 WHERE id = ?", (row["id"],))
    return status
