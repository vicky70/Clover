"""The one scheduler (eng D4): `run_pass(now)` every 30s from a FastAPI lifespan task.

Each pass is idempotent and holds no state that matters only in memory:
  1. materialize upcoming windows from the schedule
  2. open due windows (assign the next task in track order)
  3. decide windows past close + grace; on the first pass after a start, apply the
     D1 outage rule; upgrade late passes to PASSED_LATE
  4. enqueue alerts (unique per window and kind) and send due outbox rows
  5. record last_pass_at, so a hung loop is visible in `clover admin outbox`
Run uvicorn with a single worker.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, time, timedelta, timezone
from typing import Callable

from app import db, outcomes, whatsapp, witness
from app.config import Settings
from app.outcomes import Window
from app.tracks import TaskSpec, load_track, DEFAULT_TRACKS_DIR

log = logging.getLogger("clover.scheduler")

HORIZON_DAYS = 14
LATE_LOOKBACK = timedelta(days=14)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- queries shared with the API ----------------------------------------------

def specs_for(settings: Settings, skill: str) -> list[TaskSpec]:
    return load_track(skill, settings.tracks_dir or DEFAULT_TRACKS_DIR)


def outcome_of(conn: sqlite3.Connection, window_id: int) -> str | None:
    row = conn.execute(
        "SELECT payload FROM events WHERE window_id = ? AND type = 'outcome_decided' ORDER BY id DESC LIMIT 1",
        (window_id,),
    ).fetchone()
    return json.loads(row["payload"])["outcome"] if row else None


def is_paused(conn: sqlite3.Connection, w: sqlite3.Row) -> bool:
    return conn.execute(
        "SELECT 1 FROM events WHERE window_id = ? AND type = 'pause_declared' AND created_at < ?",
        (w["id"], w["opens_at"]),
    ).fetchone() is not None


def to_window(w: sqlite3.Row, specs: list[TaskSpec]) -> Window:
    spec = next((s for s in specs if s.id == w["task_id"]), None)
    return Window(
        id=w["id"], opens_at=db.parse(w["opens_at"]), closes_at=db.parse(w["closes_at"]),
        task_id=w["task_id"],
        check_keys=frozenset(c.key for c in spec.checks) if spec else frozenset(),
        unique_keys=spec.unique_keys if spec else frozenset(),
    )


def next_task(conn: sqlite3.Connection, user_id: int, specs: list[TaskSpec]) -> TaskSpec | None:
    """First task in track order without a PASSED / PASSED_LATE window."""
    passed = set()
    for w in conn.execute("SELECT id, task_id FROM windows WHERE user_id = ? AND task_id IS NOT NULL", (user_id,)):
        if outcome_of(conn, w["id"]) in outcomes.PASSING:
            passed.add(w["task_id"])
    return next((s for s in specs if s.id not in passed), None)


def late_until(conn: sqlite3.Connection, w: sqlite3.Row) -> datetime | None:
    """When the next non-paused window after `w` opens (None: no such window yet)."""
    for nxt in conn.execute("SELECT * FROM windows WHERE user_id = ? AND opens_at > ? ORDER BY opens_at",
                            (w["user_id"], w["opens_at"])):
        if not is_paused(conn, nxt):
            return db.parse(nxt["opens_at"])
    return None


def window_times(day, start_local: str, minutes: int) -> tuple[datetime, datetime]:
    hh, mm = (int(x) for x in start_local.split(":"))
    opens = datetime.combine(day, time(hh, mm), tzinfo=db.IST).astimezone(timezone.utc)
    return opens, opens + timedelta(minutes=minutes)


def fmt_window(w: sqlite3.Row) -> str:
    o = db.parse(w["opens_at"]).astimezone(db.IST)
    c = db.parse(w["closes_at"]).astimezone(db.IST)
    def clock(t: datetime) -> str:
        return f"{t:%I:%M}".lstrip("0")
    return f"{o:%a} {o.day} {o:%b}, {clock(o)}-{clock(c)} {c:%p} IST"


# --- the pass -----------------------------------------------------------------

def materialize(conn: sqlite3.Connection, user_id: int, now: datetime) -> None:
    sched = conn.execute("SELECT * FROM schedules WHERE user_id = ?", (user_id,)).fetchone()
    if sched is None:
        return
    weekdays = set(json.loads(sched["weekdays"]))
    today = now.astimezone(db.IST).date()
    with db.tx(conn):
        for i in range(HORIZON_DAYS):
            day = today + timedelta(days=i)
            if day.weekday() not in weekdays:
                continue
            opens, closes = window_times(day, sched["start_local"], sched["minutes"])
            if opens <= now:
                continue  # never create windows in the past
            conn.execute("INSERT OR IGNORE INTO windows (user_id, opens_at, closes_at) VALUES (?, ?, ?)",
                         (user_id, db.iso(opens), db.iso(closes)))


def open_due(conn: sqlite3.Connection, user_id: int, specs: list[TaskSpec], now: datetime) -> None:
    due = conn.execute(
        "SELECT * FROM windows WHERE user_id = ? AND task_id IS NULL AND opens_at <= ? ORDER BY opens_at",
        (user_id, db.iso(now)),
    ).fetchall()
    for w in due:
        if is_paused(conn, w):
            task = None
        else:
            task = next_task(conn, user_id, specs)
            if task is None:
                continue  # track complete
        with db.tx(conn):
            conn.execute("UPDATE windows SET task_id = ? WHERE id = ? AND task_id IS NULL",
                         (task.id if task else "", w["id"]))
            db.add_event(conn, user_id, "window_opened", now, window_id=w["id"],
                         task_id=task.id if task else None)


def decide_due(conn: sqlite3.Connection, user: sqlite3.Row, specs: list[TaskSpec], now: datetime,
               *, startup: bool) -> list[tuple[int, str]]:
    decided = []
    rows = conn.execute(
        "SELECT * FROM windows WHERE user_id = ? AND task_id IS NOT NULL AND opens_at <= ? AND opens_at >= ? "
        "ORDER BY opens_at",
        (user["id"], db.iso(now), db.iso(now - LATE_LOOKBACK)),
    ).fetchall()
    for w in rows:
        current = outcome_of(conn, w["id"])
        if current is not None and current not in outcomes.LATE_UPGRADABLE:
            continue
        window = to_window(w, specs)
        events = db.window_events(conn, w["id"])
        result = outcomes.decide(window, events, now, startup=startup,
                                 late_until=late_until(conn, w) if current else None)
        if result is None:
            continue
        with db.tx(conn):
            if current is None:
                db.add_event(conn, user["id"], "window_closed", now, window_id=w["id"])
            db.add_event(conn, user["id"], "outcome_decided", now, window_id=w["id"],
                         outcome=result, supersedes=current)
            _enqueue_alerts(conn, user, w, result, specs, now)
        decided.append((w["id"], result))
    return decided


def _enqueue_alerts(conn, user, w, result, specs, now) -> None:
    if result not in (outcomes.MISSED, outcomes.ATTEMPTED_FAILED):
        return
    title = next((s.title for s in specs if s.id == w["task_id"]), w["task_id"])
    kind = None
    if result == outcomes.MISSED:
        kind, slots = "miss", [user["name"], title, fmt_window(w)]
    else:
        history = [outcome_of(conn, r["id"]) for r in conn.execute(
            "SELECT id FROM windows WHERE user_id = ? AND task_id = ? AND opens_at <= ? ORDER BY opens_at",
            (user["id"], w["task_id"], w["opens_at"]))]
        already = conn.execute(
            "SELECT 1 FROM outbox o JOIN windows w ON w.id = o.window_id "
            "WHERE o.kind = 'struggling' AND w.user_id = ? AND w.task_id = ?",
            (user["id"], w["task_id"]),
        ).fetchone()
        if outcomes.is_struggling([h for h in history if h]) and not already:
            kind, slots = "struggling", [user["name"], title]
    if kind is None:
        return
    wit = witness.active_witness(conn, user["id"])
    if wit is None:
        db.add_event(conn, user["id"], "alert_skipped", now, window_id=w["id"], kind=kind,
                     reason="no active witness")
        return
    whatsapp.enqueue(conn, now, window_id=w["id"], kind=kind, witness_id=wit["id"],
                     params={"slots": slots})


class Scheduler:
    def __init__(self, settings: Settings, transport: whatsapp.Transport,
                 clock: Callable[[], datetime] = utcnow):
        self.settings = settings
        self.transport = transport
        self.clock = clock
        self.first_pass = True

    def run_pass(self, now: datetime | None = None) -> dict:
        now = now or self.clock()
        conn = db.connect(self.settings.db_path)
        summary = {"decided": [], "sent": 0, "startup": self.first_pass}
        try:
            # A start without a matching finish means a pass is stuck mid-way; a stale
            # finish with no newer start means the loop itself stopped.
            with db.tx(conn):
                db.set_meta(conn, "pass_started_at", db.iso(now))
            for user in conn.execute("SELECT * FROM users").fetchall():
                specs = specs_for(self.settings, user["track"])
                materialize(conn, user["id"], now)
                open_due(conn, user["id"], specs, now)
                summary["decided"] += decide_due(conn, user, specs, now, startup=self.first_pass)
            summary["sent"] = whatsapp.send_due(conn, self.transport, self.settings, now)
            with db.tx(conn):
                db.set_meta(conn, "last_pass_at", db.iso(now))
        finally:
            conn.close()
        self.first_pass = False
        log.info("pass ok at %s: %s", db.iso(now), summary)
        return summary

    async def loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.run_pass)
            except Exception:
                log.exception("run_pass failed")
            await asyncio.sleep(self.settings.loop_interval_s)
