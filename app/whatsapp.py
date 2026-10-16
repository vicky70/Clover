"""WhatsApp Cloud API: outbound transport, the outbox (eng D7), and webhook signature checks.

Outbox delivery is at-least-once: a row is enqueued once per (window_id, kind) and
retried with backoff until Meta returns a message id. A crash between the send and
recording the id can produce one duplicate; nothing can be lost.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Protocol

import httpx

from app import db
from app.config import Settings

log = logging.getLogger("clover.whatsapp")

BACKOFF_BASE = timedelta(seconds=30)
BACKOFF_CAP = timedelta(minutes=30)


class SendError(Exception):
    """Send failed; the outbox will retry."""


class Transport(Protocol):
    def send_template(self, to: str, template: str, params: list[str]) -> str: ...
    def send_text(self, to: str, text: str) -> str: ...


class LogTransport:
    """Used until templates are approved: the founder forwards alerts by hand from the log."""

    def send_template(self, to: str, template: str, params: list[str]) -> str:
        log.warning("FORWARD BY HAND to +%s: template=%s params=%s", to, template, params)
        return f"log-{uuid.uuid4()}"

    def send_text(self, to: str, text: str) -> str:
        log.warning("FORWARD BY HAND to +%s: %s", to, text)
        return f"log-{uuid.uuid4()}"


class CloudApiTransport:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.s = settings
        self.client = client or httpx.Client(timeout=10.0)
        self.url = f"https://graph.facebook.com/{settings.wa_api_version}/{settings.wa_phone_number_id}/messages"

    def _post(self, body: dict) -> str:
        try:
            r = self.client.post(self.url, json=body,
                                 headers={"Authorization": f"Bearer {self.s.wa_access_token}"})
        except httpx.HTTPError as e:
            raise SendError(f"transport: {e}") from e
        if r.status_code >= 400:
            raise SendError(f"HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json()["messages"][0]["id"]
        except (ValueError, KeyError, IndexError) as e:
            raise SendError(f"no message id in response: {r.text[:300]}") from e

    def send_template(self, to: str, template: str, params: list[str]) -> str:
        return self._post({
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": template,
                "language": {"code": self.s.wa_language},
                "components": [{"type": "body",
                                "parameters": [{"type": "text", "text": p} for p in params]}],
            },
        })

    def send_text(self, to: str, text: str) -> str:
        return self._post({"messaging_product": "whatsapp", "to": to, "type": "text",
                           "text": {"body": text}})


def make_transport(settings: Settings) -> Transport:
    if settings.wa_access_token and settings.wa_phone_number_id:
        return CloudApiTransport(settings)
    return LogTransport()


# --- outbox -----------------------------------------------------------------

def enqueue(conn: sqlite3.Connection, now: datetime, *, window_id: int | None, kind: str,
            witness_id: int, params: dict) -> bool:
    """Insert an outbox row. Returns False if (window_id, kind) was already enqueued."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO outbox (window_id, kind, witness_id, params, next_attempt_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (window_id, kind, witness_id, json.dumps(params), db.iso(now), db.iso(now)),
    )
    return cur.rowcount == 1


def _backoff(attempts: int) -> timedelta:
    return min(BACKOFF_BASE * (2 ** max(attempts - 1, 0)), BACKOFF_CAP)


def send_due(conn: sqlite3.Connection, transport: Transport, settings: Settings, now: datetime) -> int:
    """Send every unsent, due outbox row. Returns how many were sent."""
    rows = conn.execute(
        "SELECT o.*, w.phone, w.state FROM outbox o JOIN witnesses w ON w.id = o.witness_id "
        "WHERE o.sent_at IS NULL AND o.skipped_reason IS NULL AND o.next_attempt_at <= ? ORDER BY o.id",
        (db.iso(now),),
    ).fetchall()
    sent = 0
    for row in rows:
        if row["state"] != "active":
            with db.tx(conn):
                conn.execute("UPDATE outbox SET skipped_reason = ? WHERE id = ?",
                             (f"witness {row['state']}", row["id"]))
            continue
        params = json.loads(row["params"])
        # The network call happens outside any transaction.
        try:
            if row["kind"] == "consent_ack":
                message_id = transport.send_text(row["phone"], params["text"])
            else:
                template = settings.templates[row["kind"]]
                message_id = transport.send_template(row["phone"], template, params["slots"])
        except SendError as e:
            attempts = row["attempts"] + 1
            with db.tx(conn):
                conn.execute(
                    "UPDATE outbox SET attempts = ?, last_error = ?, next_attempt_at = ? WHERE id = ?",
                    (attempts, str(e)[:500], db.iso(now + _backoff(attempts)), row["id"]),
                )
            log.warning("outbox %s send failed (attempt %s): %s", row["id"], attempts, e)
            continue
        with db.tx(conn):
            conn.execute(
                "UPDATE outbox SET attempts = attempts + 1, message_id = ?, sent_at = ?, last_error = NULL "
                "WHERE id = ?",
                (message_id, db.iso(now), row["id"]),
            )
        sent += 1
    return sent


# --- webhook signature --------------------------------------------------------

def signature_valid(app_secret: str, raw_body: bytes, header: str | None) -> bool:
    """X-Hub-Signature-256 = 'sha256=' + hex HMAC-SHA256(app_secret, raw body)."""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])
