import asyncio
from datetime import timedelta

from fastapi.testclient import TestClient

from app import db, outcomes
from app.main import create_app
from app.scheduler import Scheduler
from app.tracks import load_track
from tests.conftest import MON_OPEN

SPECS = load_track("docker")
DEADLINE = MON_OPEN + timedelta(hours=1) + outcomes.GRACE


def all_pass(task):
    spec = next(s for s in SPECS if s.id == task)
    return {"task_id": task, "status": "pass", "checks": [{"key": c.key, "passed": True} for c in spec.checks]}


def failing(task):
    spec = next(s for s in SPECS if s.id == task)
    return {"task_id": task, "status": "fail", "checks": [{"key": c.key, "passed": False} for c in spec.checks]}


def outcomes_by_window(settings):
    conn = db.connect(settings.db_path)
    try:
        rows = conn.execute("SELECT window_id, payload FROM events WHERE type = 'outcome_decided' ORDER BY id")
        import json
        return [(r["window_id"], json.loads(r["payload"])["outcome"]) for r in rows]
    finally:
        conn.close()


def outbox(settings):
    conn = db.connect(settings.db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM outbox ORDER BY id")]
    finally:
        conn.close()


def test_schedule_materializes_weekday_windows(scheduled):
    labels = [w["label"] for w in scheduled.post(
        "/schedule", json={"days": ["mon", "tue", "wed", "thu", "fri"], "start": "19:00", "minutes": 60}
    ).json()["windows"]]
    assert labels[0] == "Mon 19 Oct, 7:00-8:00 PM IST"
    assert all(not label.startswith(("Sat", "Sun")) for label in labels)


def test_missed_window_alerts_witness_once_even_if_pass_reruns(witnessed, app, clock, transport, settings):
    sched = app.state.scheduler
    sched.run_pass(MON_OPEN - timedelta(hours=1))  # startup pass, nothing due
    clock.set(MON_OPEN + timedelta(minutes=1))
    assert witnessed.get("/task/current").json()["task"]["id"] == "docker-01-hello-run"
    sched.run_pass(DEADLINE)
    sched.run_pass(DEADLINE + timedelta(seconds=30))
    sched.run_pass(DEADLINE + timedelta(minutes=1))
    assert [o for _, o in outcomes_by_window(settings)] == [outcomes.MISSED]
    misses = [m for m in transport.sent if m.get("template") == "clover_miss_alert"]
    assert len(misses) == 1
    assert misses[0]["to"] == "919876543210"
    assert misses[0]["params"] == ["Vicky", "Run your first container", "Mon 19 Oct, 7:00-8:00 PM IST"]
    assert len([r for r in outbox(settings) if r["kind"] == "miss"]) == 1


def test_pass_in_window_advances_track_without_alert(witnessed, app, clock, transport, settings):
    sched = app.state.scheduler
    sched.run_pass(MON_OPEN - timedelta(hours=1))
    clock.set(MON_OPEN + timedelta(minutes=20))
    r = witnessed.post("/verify", json=all_pass("docker-01-hello-run"))
    assert r.json()["counted"] == "in_window"
    sched.run_pass(DEADLINE)
    assert [o for _, o in outcomes_by_window(settings)] == [outcomes.PASSED]
    assert not [m for m in transport.sent if "template" in m]
    clock.set(MON_OPEN + timedelta(days=1, minutes=1))
    assert witnessed.get("/task/current").json()["task"]["id"] == "docker-02-detached-nginx"


def test_verify_for_wrong_task_is_rejected(scheduled, app, clock):
    app.state.scheduler.run_pass(MON_OPEN - timedelta(hours=1))
    clock.set(MON_OPEN + timedelta(minutes=5))
    assert scheduled.post("/verify", json=all_pass("docker-03-port-mapping")).status_code == 409


def test_late_pass_becomes_passed_late_and_advances(witnessed, app, clock, settings):
    sched = app.state.scheduler
    sched.run_pass(MON_OPEN - timedelta(hours=1))
    sched.run_pass(DEADLINE)  # MISSED, witness alerted
    clock.set(DEADLINE + timedelta(hours=2))
    assert witnessed.post("/verify", json=all_pass("docker-01-hello-run")).json()["counted"] == "late"
    sched.run_pass(clock.now)
    assert [o for _, o in outcomes_by_window(settings)] == [outcomes.MISSED, outcomes.PASSED_LATE]
    clock.set(MON_OPEN + timedelta(days=1, minutes=1))
    assert witnessed.get("/task/current").json()["task"]["id"] == "docker-02-detached-nginx"


def test_two_failed_attempts_send_struggling_once(witnessed, app, clock, transport, settings):
    sched = app.state.scheduler
    sched.run_pass(MON_OPEN - timedelta(hours=1))
    for day in range(3):
        opens = MON_OPEN + timedelta(days=day)
        for minute in (5, 20):  # two failing runs >= 10 min apart: a valid attempt
            clock.set(opens + timedelta(minutes=minute))
            r = witnessed.post("/verify", json=failing("docker-01-hello-run"))
            assert r.status_code == 200, r.text
        sched.run_pass(opens + timedelta(hours=1) + outcomes.GRACE)
    assert [o for _, o in outcomes_by_window(settings)] == [outcomes.ATTEMPTED_FAILED] * 3
    struggling = [m for m in transport.sent if m.get("template") == "clover_struggling"]
    assert len(struggling) == 1
    assert struggling[0]["params"] == ["Vicky", "Run your first container"]
    assert not [m for m in transport.sent if m.get("template") == "clover_miss_alert"]


def test_startup_rule_marks_server_outage_without_alert(witnessed, app, settings, transport, clock):
    app.state.scheduler.run_pass(MON_OPEN - timedelta(hours=1))
    clock.set(MON_OPEN + timedelta(minutes=1))
    witnessed.get("/task/current")  # window opens before the "crash"
    restarted = Scheduler(settings, transport, clock)  # new process: first_pass is True
    restarted.run_pass(DEADLINE + timedelta(hours=3))
    assert [o for _, o in outcomes_by_window(settings)] == [outcomes.SERVER_OUTAGE]
    assert not [m for m in transport.sent if "template" in m]
    wid = outcomes_by_window(settings)[0][0]
    assert witnessed.post(f"/admin/void/{wid}").json()["outcome"] == outcomes.VOID
    # the task carries over
    clock.set(MON_OPEN + timedelta(days=1, minutes=1))
    assert witnessed.get("/task/current").json()["task"]["id"] == "docker-01-hello-run"


def test_after_startup_pass_rule_no_longer_applies(witnessed, app, settings):
    sched = app.state.scheduler
    sched.run_pass(MON_OPEN - timedelta(hours=1))
    sched.run_pass(DEADLINE + timedelta(hours=3))
    assert [o for _, o in outcomes_by_window(settings)] == [outcomes.MISSED]


def test_pause_rules(witnessed, app, clock, transport, settings):
    assert witnessed.post("/pause", json={"date": "2026-10-19"}).status_code == 200
    assert witnessed.post("/pause", json={"date": "2026-10-20"}).status_code == 200
    r = witnessed.post("/pause", json={"date": "2026-10-21"})
    assert r.status_code == 409 and "2 pause days" in r.json()["detail"]
    assert witnessed.post("/pause", json={"date": "2026-10-24"}).status_code == 404  # Saturday
    sched = app.state.scheduler
    sched.run_pass(MON_OPEN - timedelta(hours=1))
    sched.run_pass(MON_OPEN + timedelta(minutes=1))
    assert [o for _, o in outcomes_by_window(settings)] == [outcomes.PAUSED]
    clock.set(MON_OPEN + timedelta(days=2, minutes=1))  # Wednesday, already open
    assert witnessed.post("/pause", json={"date": "2026-10-21"}).status_code == 409
    assert not [m for m in transport.sent if "template" in m]


def test_no_active_witness_records_skipped_alert(scheduled, app, settings):
    sched = app.state.scheduler
    sched.run_pass(MON_OPEN - timedelta(hours=1))
    sched.run_pass(DEADLINE)
    assert outbox(settings) == []
    conn = db.connect(settings.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE type = 'alert_skipped'").fetchone()[0] == 1
    finally:
        conn.close()
    assert scheduled.get("/witness").json()["banner"] == "You are not being witnessed."


def test_lifespan_loop_runs_passes(settings, transport, clock):
    settings.loop_interval_s = 0.01
    app = create_app(settings, transport=transport, clock=clock, start_loop=True)
    with TestClient(app):
        for _ in range(200):
            conn = db.connect(settings.db_path)
            try:
                if db.get_meta(conn, "last_pass_at"):
                    break
            finally:
                conn.close()
            asyncio.run(asyncio.sleep(0.01))
        assert app.state.loop_task is not None and not app.state.loop_task.done()
    assert app.state.loop_task.cancelled() or app.state.loop_task.done()
    assert db.get_meta(db.connect(settings.db_path), "last_pass_at") == db.iso(clock.now)
