import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.whatsapp import SendError

# Sunday 18 Oct 2026, 10:00 IST. Mon-Fri windows open 19:00 IST = 13:30 UTC.
START = datetime(2026, 10, 18, 4, 30, tzinfo=timezone.utc)
MON_OPEN = datetime(2026, 10, 19, 13, 30, tzinfo=timezone.utc)
ADMIN_SECRET = "s3cret-admin"
APP_SECRET = "wa-app-secret"
WITNESS_PHONE = "919876543210"


class Clock:
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now

    def set(self, dt):
        self.now = dt

    def advance(self, **kw):
        self.now += timedelta(**kw)


class FakeTransport:
    """Fake WhatsApp transport: records sends; `fail_next` raises SendError that many times."""

    def __init__(self):
        self.sent = []
        self.fail_next = 0
        self._n = 0

    def _send(self, record):
        if self.fail_next:
            self.fail_next -= 1
            raise SendError("HTTP 503: unavailable")
        self._n += 1
        record["message_id"] = f"wamid.{self._n}"
        self.sent.append(record)
        return record["message_id"]

    def send_template(self, to, template, params):
        return self._send({"to": to, "template": template, "params": params})

    def send_text(self, to, text):
        return self._send({"to": to, "text": text})


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def transport():
    return FakeTransport()


@pytest.fixture
def settings(tmp_path):
    return Settings(db_path=tmp_path / "clover.db", admin_secret=ADMIN_SECRET,
                    wa_verify_token="verify-me", wa_app_secret=APP_SECRET,
                    wa_business_number="911234567890")


@pytest.fixture
def app(settings, transport, clock):
    return create_app(settings, transport=transport, clock=clock, start_loop=False)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_client(client):
    r = client.post("/auth/init", json={"name": "Vicky"}, headers={"X-Admin-Secret": ADMIN_SECRET})
    assert r.status_code == 200, r.text
    client.headers["Authorization"] = f"Bearer {r.json()['token']}"
    return client


@pytest.fixture
def scheduled(auth_client):
    r = auth_client.post("/schedule", json={"days": ["mon", "tue", "wed", "thu", "fri"],
                                            "start": "19:00", "minutes": 60})
    assert r.status_code == 200, r.text
    return auth_client


def sign(body: bytes, secret=APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def post_webhook(client, payload, secret=APP_SECRET):
    raw = json.dumps(payload).encode()
    return client.post("/wa/webhook", content=raw,
                       headers={"X-Hub-Signature-256": sign(raw, secret), "Content-Type": "application/json"})


def inbound(text, wamid="wamid.in1", phone=WITNESS_PHONE):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
        "messages": [{"from": phone, "id": wamid, "type": "text", "text": {"body": text}}]}}]}]}


def status(message_id, state):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
        "statuses": [{"id": message_id, "status": state, "recipient_id": WITNESS_PHONE}]}}]}]}


@pytest.fixture
def witnessed(scheduled):
    """Schedule set and an active witness (consented through the webhook)."""
    r = scheduled.post("/witness", json={"name": "Anu", "phone": "+91 98765 43210"})
    assert r.status_code == 200, r.text
    code = r.json()["code"]
    assert post_webhook(scheduled, inbound(f"I agree to be Vicky's Clover witness. Code {code}")).json()["results"] == ["consented"]
    return scheduled
