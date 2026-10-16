import json
from datetime import timedelta

import httpx

from app import db, whatsapp
from app.config import Settings
from tests.conftest import MON_OPEN
from tests.test_scheduler import DEADLINE, outbox


def test_5xx_is_retried_with_backoff_until_message_id(witnessed, app, transport, settings):
    sched = app.state.scheduler
    sched.run_pass(MON_OPEN - timedelta(hours=1))
    transport.fail_next = 2
    sched.run_pass(DEADLINE)
    row = [r for r in outbox(settings) if r["kind"] == "miss"][0]
    assert row["sent_at"] is None and row["attempts"] == 1 and "503" in row["last_error"]
    sched.run_pass(DEADLINE + timedelta(seconds=10))  # before backoff: not retried
    assert [r for r in outbox(settings) if r["kind"] == "miss"][0]["attempts"] == 1
    sched.run_pass(DEADLINE + timedelta(seconds=31))  # retry #2 fails
    sched.run_pass(DEADLINE + timedelta(minutes=5))   # retry #3 succeeds
    row = [r for r in outbox(settings) if r["kind"] == "miss"][0]
    assert row["sent_at"] and row["message_id"].startswith("wamid.") and row["attempts"] == 3
    assert len([m for m in transport.sent if m.get("template") == "clover_miss_alert"]) == 1


def test_enqueue_is_unique_per_window_and_kind(witnessed, settings, clock):
    conn = db.connect(settings.db_path)
    try:
        wid = conn.execute("SELECT id FROM witnesses").fetchone()["id"]
        win = conn.execute("SELECT id FROM windows ORDER BY opens_at LIMIT 1").fetchone()["id"]
        assert whatsapp.enqueue(conn, clock(), window_id=win, kind="miss", witness_id=wid, params={"slots": []})
        assert not whatsapp.enqueue(conn, clock(), window_id=win, kind="miss", witness_id=wid, params={"slots": []})
        assert whatsapp.enqueue(conn, clock(), window_id=win, kind="struggling", witness_id=wid, params={"slots": []})
    finally:
        conn.close()


def test_rows_for_opted_out_witness_are_skipped_not_sent(witnessed, app, transport, settings):
    from tests.conftest import inbound, post_webhook
    assert post_webhook(witnessed, inbound("STOP", wamid="wamid.stop")).json()["results"] == ["opted_out"]
    sched = app.state.scheduler
    sched.run_pass(MON_OPEN - timedelta(hours=1))
    sched.run_pass(DEADLINE)
    assert not [m for m in transport.sent if "template" in m]


def test_cloud_api_transport_request_shape():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"messaging_product": "whatsapp", "messages": [{"id": "wamid.ABC"}]})

    s = Settings(wa_access_token="tok", wa_phone_number_id="1234")
    t = whatsapp.CloudApiTransport(s, httpx.Client(transport=httpx.MockTransport(handler)))
    assert t.send_template("919876543210", "clover_miss_alert", ["Vicky", "Task", "Mon"]) == "wamid.ABC"
    assert seen["url"] == "https://graph.facebook.com/v22.0/1234/messages"
    assert seen["auth"] == "Bearer tok"
    assert seen["body"]["template"]["components"][0]["parameters"][1] == {"type": "text", "text": "Task"}


def test_cloud_api_transport_maps_errors_to_send_error():
    for response in (httpx.Response(503, text="busy"), httpx.Response(200, json={"oops": 1})):
        t = whatsapp.CloudApiTransport(Settings(wa_access_token="t", wa_phone_number_id="1"),
                                       httpx.Client(transport=httpx.MockTransport(lambda r, resp=response: resp)))
        try:
            t.send_text("91", "hi")
        except whatsapp.SendError:
            continue
        raise AssertionError("expected SendError")
