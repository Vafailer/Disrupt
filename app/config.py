"""No dotenv autoloading: mock configuration never reads a model credential."""

import os
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./data/notes.db"
    provider: Literal["mock", "cloudru"] = "mock"
    auto_worker: bool = True
    secure_cookies: bool = False
    public_origin: str = "http://127.0.0.1:8000"
    allow_registration: bool = True
    allow_live_requests: bool = False
    cloudru_model: str = ""
    live_call_limit: int = 0
    live_user_call_limit: int = 0
    max_pending_per_user: int = 10
    session_seconds: int = 86400
    lease_seconds: int = 120

    def __post_init__(self):
        if self.provider not in {"mock", "cloudru"}:
            raise ValueError("NOTES_PROVIDER must be mock or cloudru")
        if self.lease_seconds < 90:
            raise ValueError("Worker lease must exceed the provider timeout")
        if self.provider == "cloudru" and (
            not self.allow_live_requests
            or not self.cloudru_model
            or self.live_call_limit <= 0
            or self.live_user_call_limit <= 0
        ):
            raise ValueError("Live mode requires explicit permission, model and positive call limits")

    @classmethod
    def from_env(cls):
        def flag(name, default):
            value = os.environ.get(name, str(default)).lower()
            if value not in {"true", "false"}:
                raise ValueError(f"{name} must be true or false")
            return value == "true"

        return cls(
            database_url=os.environ.get("NOTES_DATABASE_URL", cls.database_url),
            provider=os.environ.get("NOTES_PROVIDER", "mock"),
            auto_worker=flag("NOTES_AUTO_WORKER", True),
            secure_cookies=flag("NOTES_SECURE_COOKIES", False),
            public_origin=os.environ.get("NOTES_PUBLIC_ORIGIN", cls.public_origin).rstrip("/"),
            allow_registration=flag("NOTES_ALLOW_REGISTRATION", True),
            allow_live_requests=flag("NOTES_ALLOW_LIVE_REQUESTS", False),
            cloudru_model=os.environ.get("NOTES_CLOUDRU_MODEL", ""),
            live_call_limit=int(os.environ.get("NOTES_LIVE_CALL_LIMIT", "0")),
            live_user_call_limit=int(os.environ.get("NOTES_LIVE_USER_CALL_LIMIT", "0")),
        )
