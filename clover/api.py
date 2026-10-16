"""HTTP client for the Clover server, plus local config in ~/.clover (or $CLOVER_HOME)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx


def home() -> Path:
    return Path(os.environ.get("CLOVER_HOME") or Path.home() / ".clover")


def load_config() -> dict:
    p = home() / "config.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_config(cfg: dict) -> None:
    home().mkdir(parents=True, exist_ok=True)
    (home() / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")


class Offline(Exception):
    """The server could not be reached."""


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class Api:
    def __init__(self, server: str, token: str | None = None, client: httpx.Client | None = None):
        self.client = client or httpx.Client(base_url=server, timeout=15.0)
        if client is not None and server:
            self.client.base_url = server
        if token:
            self.client.headers["Authorization"] = f"Bearer {token}"

    @classmethod
    def from_config(cls, client: httpx.Client | None = None) -> "Api":
        cfg = load_config()
        if not cfg.get("server") or not cfg.get("token"):
            raise ApiError(0, "not set up; run `clover init --server URL --admin-secret SECRET`")
        return cls(cfg["server"], cfg["token"], client)

    def request(self, method: str, path: str, **kw) -> dict:
        try:
            r = self.client.request(method, path, **kw)
        except httpx.TransportError as e:
            raise Offline(str(e)) from e
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise ApiError(r.status_code, str(detail))
        return r.json()

    def init(self, name: str, admin_secret: str) -> dict:
        return self.request("POST", "/auth/init", json={"name": name}, headers={"X-Admin-Secret": admin_secret})

    def schedule(self, days: list[str], start: str, minutes: int) -> dict:
        return self.request("POST", "/schedule", json={"days": days, "start": start, "minutes": minutes})

    def pause(self, day: str) -> dict:
        return self.request("POST", "/pause", json={"date": day})

    def witness_add(self, name: str, phone: str) -> dict:
        return self.request("POST", "/witness", json={"name": name, "phone": phone})

    def witness_status(self) -> dict:
        return self.request("GET", "/witness")

    def current_task(self) -> dict:
        return self.request("GET", "/task/current")

    def verify(self, result: dict) -> dict:
        return self.request("POST", "/verify", json=result)

    def admin_outbox(self) -> dict:
        return self.request("GET", "/admin/outbox")

    def admin_void(self, window_id: int) -> dict:
        return self.request("POST", f"/admin/void/{window_id}")
