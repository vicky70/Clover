"""FastAPI app. Run with a single worker: `uvicorn app.main:app --workers 1`."""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, timedelta
from typing import Callable, Iterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app import auth, db, outcomes, scheduler, whatsapp, witness
from app.config import Settings

log = logging.getLogger("clover.api")

MAX_PAUSES_PER_WEEK = 2
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class InitBody(BaseModel):
    name: str = Field(min_length=1, max_length=40)


class ScheduleBody(BaseModel):
    days: list[str]
    start: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    minutes: int = Field(ge=15, le=240)


class PauseBody(BaseModel):
    date: date


class WitnessBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    phone: str


class CheckResult(BaseModel):
    key: str
    passed: bool
    detail: str = ""


class VerifyBody(BaseModel):
    task_id: str
    status: str = Field(pattern=r"^(pass|fail|environment_error)$")
    checks: list[CheckResult] = []
    client_created_at: str | None = None   # informational only; never used for timing
    error: str | None = None


def create_app(settings: Settings | None = None, transport: whatsapp.Transport | None = None,
               clock: Callable[[], datetime] = scheduler.utcnow, start_loop: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    db.init(settings.db_path)
    transport = transport or whatsapp.make_transport(settings)
    sched = scheduler.Scheduler(settings, transport, clock)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(sched.loop()) if start_loop else None
        app.state.loop_task = task  # strong reference so it isn't garbage-collected
        yield
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Clover", lifespan=lifespan)
    app.state.settings = settings
    app.state.scheduler = sched

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = db.connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def get_user(authorization: str | None = Header(None),
                 conn: sqlite3.Connection = Depends(get_conn)) -> sqlite3.Row:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing bearer token; run `clover init`")
        user = auth.user_for_token(conn, authorization[len("Bearer "):])
        if user is None:
            raise HTTPException(401, "unknown device token")
        return user

    # --- auth -----------------------------------------------------------------

    @app.post("/auth/init")
    def init(body: InitBody, x_admin_secret: str | None = Header(None),
             conn: sqlite3.Connection = Depends(get_conn)):
        if not auth.admin_secret_ok(settings.admin_secret, x_admin_secret):
            raise HTTPException(403, "admin secret required")
        user_id, token = auth.init_device(conn, body.name, clock())
        return {"user_id": user_id, "token": token}

    # --- schedule and pause ---------------------------------------------------

    @app.post("/schedule")
    def set_schedule(body: ScheduleBody, user=Depends(get_user), conn=Depends(get_conn)):
        try:
            weekdays = sorted({WEEKDAYS.index(d.lower()[:3]) for d in body.days})
        except ValueError:
            raise HTTPException(422, f"days must be from {WEEKDAYS}")
        now = clock()
        with db.tx(conn):
            conn.execute(
                "INSERT INTO schedules (user_id, weekdays, start_local, minutes, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET weekdays = excluded.weekdays, "
                "start_local = excluded.start_local, minutes = excluded.minutes, updated_at = excluded.updated_at",
                (user["id"], json.dumps(weekdays), body.start, body.minutes, db.iso(now)),
            )
            # Future windows that haven't opened and have no events are rebuilt from the new schedule.
            conn.execute(
                "DELETE FROM windows WHERE user_id = ? AND task_id IS NULL AND opens_at > ? "
                "AND id NOT IN (SELECT window_id FROM events WHERE window_id IS NOT NULL)",
                (user["id"], db.iso(now)),
            )
            db.add_event(conn, user["id"], "schedule_set", now, weekdays=weekdays,
                         start=body.start, minutes=body.minutes)
        scheduler.materialize(conn, user["id"], now)
        return {"windows": _upcoming(conn, user["id"], now)}

    @app.post("/pause")
    def pause(body: PauseBody, user=Depends(get_user), conn=Depends(get_conn)):
        now = clock()
        w = next((w for w in conn.execute("SELECT * FROM windows WHERE user_id = ? ORDER BY opens_at",
                                          (user["id"],))
                  if db.parse(w["opens_at"]).astimezone(db.IST).date() == body.date), None)
        if w is None:
            raise HTTPException(404, f"no scheduled window on {body.date}")
        if db.parse(w["opens_at"]) <= now:
            raise HTTPException(409, "a pause must be declared before the window opens")
        if scheduler.is_paused(conn, w):
            return {"window_id": w["id"], "paused": True}
        monday = body.date - timedelta(days=body.date.weekday())
        week = [r for r in conn.execute("SELECT * FROM windows WHERE user_id = ?", (user["id"],))
                if monday <= db.parse(r["opens_at"]).astimezone(db.IST).date() < monday + timedelta(days=7)]
        if sum(scheduler.is_paused(conn, r) for r in week) >= MAX_PAUSES_PER_WEEK:
            raise HTTPException(409, f"at most {MAX_PAUSES_PER_WEEK} pause days per week")
        with db.tx(conn):
            db.add_event(conn, user["id"], "pause_declared", now, window_id=w["id"], date=str(body.date))
        return {"window_id": w["id"], "paused": True}

    # --- witness --------------------------------------------------------------

    @app.post("/witness")
    def add_witness(body: WitnessBody, user=Depends(get_user), conn=Depends(get_conn)):
        try:
            return witness.invite(conn, user, body.name, body.phone, settings.wa_business_number, clock())
        except witness.WitnessError as e:
            raise HTTPException(409, str(e))

    @app.get("/witness")
    def witness_status(user=Depends(get_user), conn=Depends(get_conn)):
        w = witness.current(conn, user["id"])
        if w is None:
            return {"state": None, "banner": "You are not being witnessed."}
        state = witness.display_state(w, clock())
        return {"name": w["name"], "state": state, "invited_at": w["invited_at"],
                "banner": None if state == "active" else "You are not being witnessed."}

    # --- tasks and verify -----------------------------------------------------

    @app.get("/task/current")
    def current_task(user=Depends(get_user), conn=Depends(get_conn)):
        now = clock()
        specs = scheduler.specs_for(settings, user["track"])
        scheduler.open_due(conn, user["id"], specs, now)
        w = _active_window(conn, user["id"], now)
        if w is not None and w["task_id"]:
            spec = next(s for s in specs if s.id == w["task_id"])
            return {"window": _window_info(w), "task": spec.to_dict()}
        spec = scheduler.next_task(conn, user["id"], specs)
        nxt = conn.execute("SELECT * FROM windows WHERE user_id = ? AND opens_at > ? ORDER BY opens_at LIMIT 1",
                           (user["id"], db.iso(now))).fetchone()
        return {"window": None, "next_window": _window_info(nxt) if nxt else None,
                "task": spec.to_dict() if spec else None}

    @app.post("/verify")
    def verify(body: VerifyBody, user=Depends(get_user), conn=Depends(get_conn)):
        now = clock()  # server receipt time is the only time that counts
        specs = scheduler.specs_for(settings, user["track"])
        scheduler.open_due(conn, user["id"], specs, now)
        w, counted = _active_window(conn, user["id"], now), "in_window"
        if w is None:
            w, counted = _late_window(conn, user["id"], now), "late"
        if w is None or not w["task_id"]:
            with db.tx(conn):
                db.add_event(conn, user["id"], "verify_attempted", now, counted="no_window",
                             **body.model_dump())
            raise HTTPException(409, "no open window to count this run for")
        if body.task_id != w["task_id"]:
            raise HTTPException(409, f"this window's task is {w['task_id']}")
        with db.tx(conn):
            db.add_event(conn, user["id"], "verify_attempted", now, window_id=w["id"], counted=counted,
                         **body.model_dump())
        return {"window_id": w["id"], "received_at": db.iso(now), "counted": counted}

    # --- WhatsApp webhook (eng D5) ---------------------------------------------

    @app.get("/wa/webhook")
    def wa_verify(mode: str = Query(None, alias="hub.mode"),
                  token: str = Query(None, alias="hub.verify_token"),
                  challenge: str = Query("", alias="hub.challenge")):
        if mode == "subscribe" and settings.wa_verify_token and token == settings.wa_verify_token:
            return PlainTextResponse(challenge)
        raise HTTPException(403, "verify token mismatch")

    @app.post("/wa/webhook")
    async def wa_receive(request: Request):
        raw = await request.body()  # HMAC is over the raw bytes, never re-serialized JSON
        if not whatsapp.signature_valid(settings.wa_app_secret, raw,
                                        request.headers.get("X-Hub-Signature-256")):
            raise HTTPException(403, "bad signature")
        try:
            payload = json.loads(raw)
        except ValueError:
            raise HTTPException(400, "invalid JSON")
        results = await asyncio.to_thread(_process_webhook, payload)
        return {"ok": True, "results": results}

    def _process_webhook(payload: dict) -> list[str]:
        now = clock()
        results = []
        conn = db.connect(settings.db_path)
        try:
            for entry in payload.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    for m in value.get("messages", []):
                        if not _first_time(conn, m.get("id", ""), now):
                            results.append("duplicate")
                            continue
                        text = (m.get("text") or {}).get("body", "") if m.get("type") == "text" else ""
                        results.append(witness.handle_message(conn, now, m.get("from", ""), text, m["id"]))
                    for s in value.get("statuses", []):
                        if not _first_time(conn, f"{s.get('id')}:{s.get('status')}", now):
                            results.append("duplicate")
                            continue
                        results.append(witness.handle_status(conn, now, s.get("id", ""), s.get("status", "")))
        finally:
            conn.close()
        return results

    def _first_time(conn: sqlite3.Connection, key: str, now: datetime) -> bool:
        if not key:
            return False
        with db.tx(conn):
            cur = conn.execute("INSERT OR IGNORE INTO processed_webhooks (key, received_at) VALUES (?, ?)",
                               (key, db.iso(now)))
        return cur.rowcount == 1

    # --- admin ------------------------------------------------------------------

    @app.get("/admin/outbox")
    def admin_outbox(user=Depends(get_user), conn=Depends(get_conn)):
        now = clock()
        last = db.get_meta(conn, "last_pass_at")
        age = (now - db.parse(last)).total_seconds() if last else None
        rows = conn.execute(
            "SELECT o.id, o.window_id, o.kind, o.attempts, o.message_id, o.sent_at, o.skipped_reason, "
            "o.last_error, o.next_attempt_at, w.name AS witness FROM outbox o "
            "JOIN witnesses w ON w.id = o.witness_id ORDER BY o.id DESC LIMIT 50"
        ).fetchall()
        return {
            "pass_started_at": db.get_meta(conn, "pass_started_at"),
            "last_pass_at": last,
            "last_pass_age_s": age,
            "loop_stale": age is None or age > 3 * settings.loop_interval_s,
            "rows": [dict(r) for r in rows],
        }

    @app.post("/admin/void/{window_id}")
    def admin_void(window_id: int, user=Depends(get_user), conn=Depends(get_conn)):
        w = conn.execute("SELECT * FROM windows WHERE id = ? AND user_id = ?", (window_id, user["id"])).fetchone()
        if w is None:
            raise HTTPException(404, "no such window")
        current = scheduler.outcome_of(conn, window_id)
        if current != outcomes.SERVER_OUTAGE:
            raise HTTPException(409, f"only SERVER_OUTAGE windows can be voided (this one is {current})")
        with db.tx(conn):
            db.add_event(conn, user["id"], "outcome_decided", clock(), window_id=window_id,
                         outcome=outcomes.VOID, supersedes=current)
        return {"window_id": window_id, "outcome": outcomes.VOID}

    return app


def _window_info(w: sqlite3.Row) -> dict:
    return {"id": w["id"], "opens_at": w["opens_at"], "closes_at": w["closes_at"],
            "label": scheduler.fmt_window(w)}


def _upcoming(conn, user_id: int, now: datetime) -> list[dict]:
    return [_window_info(w) for w in conn.execute(
        "SELECT * FROM windows WHERE user_id = ? AND opens_at > ? ORDER BY opens_at LIMIT 7",
        (user_id, db.iso(now)))]


def _active_window(conn, user_id: int, now: datetime) -> sqlite3.Row | None:
    """Window that is open, or closed but still inside its grace period."""
    for w in conn.execute("SELECT * FROM windows WHERE user_id = ? AND opens_at <= ? ORDER BY opens_at DESC LIMIT 2",
                          (user_id, db.iso(now))):
        if now <= db.parse(w["closes_at"]) + outcomes.GRACE:
            return w
    return None


def _late_window(conn, user_id: int, now: datetime) -> sqlite3.Row | None:
    """Most recent past window a late pass can still make up (MISSED / ATTEMPTED_FAILED / undecided)."""
    w = conn.execute(
        "SELECT * FROM windows WHERE user_id = ? AND opens_at <= ? AND task_id IS NOT NULL AND task_id != '' "
        "ORDER BY opens_at DESC LIMIT 1", (user_id, db.iso(now))).fetchone()
    if w is None or scheduler.outcome_of(conn, w["id"]) not in (None, *outcomes.LATE_UPGRADABLE):
        return None
    until = scheduler.late_until(conn, w)
    return w if until is None or now < until else None


def get_app() -> FastAPI:
    """Entry point: `uvicorn app.main:get_app --factory --workers 1`."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()
