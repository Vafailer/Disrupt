import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(repr=False)
    service_token: str = field(repr=False)
    core_url: str
    web_url: str
    state_file: Path

    @property
    def bot_id(self) -> int:
        return int(self.bot_token.split(":", 1)[0])

    def __post_init__(self):
        if not re.fullmatch(r"[1-9][0-9]*:[A-Za-z0-9_-]{20,}", self.bot_token):
            raise ValueError("Invalid Telegram token configuration")
        if len(self.service_token) < 32 or any(c.isspace() for c in self.service_token):
            raise ValueError("Service token must contain at least 32 non-whitespace characters")
        for url, public in [(self.core_url, False), (self.web_url, True)]:
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
            ):
                raise ValueError("Configure an origin without credentials, path, query or fragment")
            if public and parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
                raise ValueError("Public web origin must use HTTPS")

    @classmethod
    def from_env(cls):
        # Fail before even reading secrets unless the operator explicitly enables this process.
        if os.environ.get("BERESTA_BOT_NETWORK_ENABLED") != "true":
            raise ValueError("Network is disabled; use --demo or explicitly enable the bot")

        def secret(name):
            path = os.environ.get(name)
            if not path:
                raise ValueError(f"Missing secret file setting {name}")
            return Path(path).read_text().strip()

        return cls(
            bot_token=secret("BERESTA_BOT_TOKEN_FILE"),
            service_token=secret("BERESTA_SERVICE_TOKEN_FILE"),
            core_url=os.environ.get("BERESTA_CORE_URL", "http://api:8000").rstrip("/"),
            web_url=os.environ.get("BERESTA_WEB_URL", "").rstrip("/"),
            state_file=Path(os.environ.get("BERESTA_BOT_STATE_FILE", "data/telegram-offset.json")),
        )
