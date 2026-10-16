from datetime import timedelta

from app import db
from tests.conftest import inbound, post_webhook, status


def test_handshake(client):
    ok = client.get("/wa/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                           "hub.challenge": "12345"})
    assert ok.status_code == 200 and ok.text == "12345"
    bad = client.get("/wa/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope",
                                            "hub.challenge": "12345"})
    assert bad.status_code == 403


def test_invalid_signature_is_rejected(scheduled):
    assert post_webhook(scheduled, inbound("STOP"), secret="wrong").status_code == 403
    r = scheduled.post("/wa/webhook", content=b"{}")
    assert r.status_code == 403


def test_signature_is_checked_on_raw_bytes(scheduled):
    # Re-serialized JSON would differ (spacing, escaping); only the raw body verifies.
    from tests.conftest import sign
    raw = b'{"entry":[],  "note":"caf\\u00e9"}'
    assert scheduled.post("/wa/webhook", content=raw, headers={"X-Hub-Signature-256": sign(raw)}).status_code == 200


def test_consent_code_activates_and_queues_ack(scheduled, app, transport):
    inv = scheduled.post("/witness", json={"name": "Anu", "phone": "+91 98765 43210"}).json()
    assert inv["link"].startswith("https://wa.me/911234567890?text=I%20agree%20to%20be%20Vicky")
    assert scheduled.get("/witness").json()["state"] == "invited"
    assert post_webhook(scheduled, inbound("hi", wamid="w1")).json()["results"] == ["code_mismatch"]
    assert post_webhook(scheduled, inbound(inv["text"], wamid="w2")).json()["results"] == ["consented"]
    assert scheduled.get("/witness").json() | {"invited_at": None} == {
        "name": "Anu", "state": "active", "invited_at": None, "banner": None}
    app.state.scheduler.run_pass()
    assert transport.sent[-1]["text"].startswith("Thanks Anu. You are now Vicky's Clover witness")


def test_consent_from_another_phone_is_ignored(scheduled):
    inv = scheduled.post("/witness", json={"name": "Anu", "phone": "+91 98765 43210"}).json()
    r = post_webhook(scheduled, inbound(inv["text"], phone="919999999999"))
    assert r.json()["results"] == ["unknown_sender"]
    assert scheduled.get("/witness").json()["state"] == "invited"


def test_duplicate_delivery_is_processed_once(scheduled, settings):
    inv = scheduled.post("/witness", json={"name": "Anu", "phone": "+91 98765 43210"}).json()
    payload = inbound(inv["text"], wamid="same")
    assert post_webhook(scheduled, payload).json()["results"] == ["consented"]
    assert post_webhook(scheduled, payload).json()["results"] == ["duplicate"]
    conn = db.connect(settings.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE type = 'witness_consented'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM outbox WHERE kind = 'consent_ack'").fetchone()[0] == 1
    finally:
        conn.close()


def test_stop_opts_out(witnessed):
    assert post_webhook(witnessed, inbound("stop.", wamid="w9")).json()["results"] == ["opted_out"]
    assert witnessed.get("/witness").json()["state"] == "opted_out"


def test_invite_expires_after_7_days(scheduled, clock):
    inv = scheduled.post("/witness", json={"name": "Anu", "phone": "+91 98765 43210"}).json()
    clock.advance(days=7, minutes=1)
    assert scheduled.get("/witness").json()["state"] == "expired"
    assert post_webhook(scheduled, inbound(inv["text"])).json()["results"] == ["expired"]
    # a fresh invite is allowed once the old one expired
    assert scheduled.post("/witness", json={"name": "Anu", "phone": "+91 98765 43210"}).status_code == 200


def test_second_invite_while_active_is_refused(witnessed):
    assert witnessed.post("/witness", json={"name": "B", "phone": "+91 90000 00000"}).status_code == 409


def test_two_delivery_failures_in_a_row_make_witness_undeliverable(witnessed, app, transport):
    app.state.scheduler.run_pass()  # sends consent ack -> wamid.1
    mid = transport.sent[-1]["message_id"]
    assert post_webhook(witnessed, status(mid, "failed")).json()["results"] == ["failed"]
    assert post_webhook(witnessed, status(mid, "delivered")).json()["results"] == ["delivered"]  # resets
    assert post_webhook(witnessed, status(mid, "failed")).json()["results"] == ["duplicate"]
    conn_status = witnessed.get("/witness").json()["state"]
    assert conn_status == "active"


def test_undeliverable_after_two_consecutive_failures(witnessed, app, settings, clock):
    from app import whatsapp
    conn = db.connect(settings.db_path)
    try:
        wid = conn.execute("SELECT id FROM witnesses").fetchone()["id"]
        whatsapp.enqueue(conn, clock(), window_id=1, kind="miss", witness_id=wid, params={"slots": ["a", "b", "c"]})
    finally:
        conn.close()
    app.state.scheduler.run_pass(clock.now + timedelta(seconds=1))
    conn = db.connect(settings.db_path)
    try:
        ids = [r["message_id"] for r in conn.execute("SELECT message_id FROM outbox ORDER BY id")]
    finally:
        conn.close()
    assert post_webhook(witnessed, status(ids[0], "failed")).json()["results"] == ["failed"]
    assert post_webhook(witnessed, status(ids[1], "failed")).json()["results"] == ["undeliverable"]
    assert witnessed.get("/witness").json()["state"] == "undeliverable"
