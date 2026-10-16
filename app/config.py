from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    db_path: Path = Path("clover.db")
    tracks_dir: Path | None = None
    admin_secret: str = ""            # empty: `clover init` is always refused
    wa_verify_token: str = ""         # GET /wa/webhook handshake
    wa_app_secret: str = ""           # HMAC key for X-Hub-Signature-256
    wa_access_token: str = ""         # empty: log transport (founder forwards by hand)
    wa_phone_number_id: str = ""
    wa_business_number: str = ""      # digits, used in wa.me consent links
    wa_api_version: str = "v22.0"
    wa_language: str = "en"
    templates: dict[str, str] = field(default_factory=lambda: {
        "miss": "clover_miss_alert",
        "struggling": "clover_struggling",
    })
    loop_interval_s: float = 30.0

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        s = cls(
            db_path=Path(env("CLOVER_DB", "clover.db")),
            admin_secret=env("CLOVER_ADMIN_SECRET", ""),
            wa_verify_token=env("CLOVER_WA_VERIFY_TOKEN", ""),
            wa_app_secret=env("CLOVER_WA_APP_SECRET", ""),
            wa_access_token=env("CLOVER_WA_ACCESS_TOKEN", ""),
            wa_phone_number_id=env("CLOVER_WA_PHONE_NUMBER_ID", ""),
            wa_business_number=env("CLOVER_WA_BUSINESS_NUMBER", ""),
            wa_api_version=env("CLOVER_WA_API_VERSION", "v22.0"),
        )
        if env("CLOVER_TRACKS_DIR"):
            s.tracks_dir = Path(env("CLOVER_TRACKS_DIR"))
        return s
