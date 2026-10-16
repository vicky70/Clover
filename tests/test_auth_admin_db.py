"""T7 (SQLite concurrency), T8 (admin-secret init), T10 (last-pass visibility)."""
import threading
from datetime import timedelta

from fastapi.testclient import TestClient

from app import db
from app.config import Settings
from app.main import create_app
from tests.conftest import ADMIN_SECRET, MON_OPEN, inbound, post_webhook


# --- T8 ----------------------------------------------------------------------

def test_init_without_secret_is_403(client):
    assert client.post("/auth/init", json={"name": "V"}).status_code == 403
    assert client.post("/auth/init", json={"name": "V"}, headers={"X-Admin-Secret": "nope"}).status_code == 403


def test_init_refused_when_server_has_no_secret(tmp_path, transport, clock):
    app = create_app(Settings(db_path=tmp_path / "x.db"), transport=transport, clock=clock, start_loop=False)
    with TestClient(app) as c:
        assert c.post("/auth/init", json={"name": "V"}, headers={"X-Admin-Secret": ""}).status_code == 403


def test_init_with_secret_issues_working_token(client, settings):
    r = client.post("/auth/init", json={"name": "Vicky"}, headers={"X-Admin-Secret": ADMIN_SECRET})
    token = r.json()["token"]
    assert client.get("/witness", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert client.get("/witness", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/witness").status_code == 401
    conn = db.connect(settings.db_path)
    try:  # only the hash is stored
        assert conn.execute("SELECT COUNT(*) FROM devices WHERE token_hash = ?", (token,)).fetchone()[0] == 0
    finally:
        conn.close()


# --- T7 ----------------------------------------------------------------------

def test_wal_and_busy_timeout(settings, app):
    conn = db.connect(settings.db_path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        idx = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert {"idx_events_window", "idx_outbox_sent"} <= idx
    finally:
        conn.close()


def test_concurrent_webhook_and_run_pass_do_not_lock(witnessed, app):
    errors = []
    sched = app.state.scheduler

    def passes():
        try:
            for i in range(30):
                sched.run_pass(MON_OPEN + timedelta(minutes=i * 5))
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)

    def hooks():
        try:
            for i in range(30):
                r = post_webhook(witnessed, inbound("hello", wamid=f"c{i}"))
                assert r.status_code == 200, r.text
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=passes), threading.Thread(target=hooks)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors


# --- T10 ---------------------------------------------------------------------

def test_admin_outbox_shows_stale_last_pass(auth_client, app, clock):
    assert auth_client.get("/admin/outbox").json()["loop_stale"] is True  # never ran
    app.state.scheduler.run_pass(clock.now)
    fresh = auth_client.get("/admin/outbox").json()
    assert fresh["loop_stale"] is False and fresh["last_pass_at"] == db.iso(clock.now)
    clock.advance(minutes=10)  # loop "stopped": no passes for 10 min
    stale = auth_client.get("/admin/outbox").json()
    assert stale["loop_stale"] is True and stale["last_pass_age_s"] == 600
