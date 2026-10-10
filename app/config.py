"""No dotenv autoloading: mock configuration never reads a model credential."""

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROGRAM_BASE_URL = "https://shared1.multitool.works:4000/v1"
OFFICIAL_BASE_URL = "https://foundation-models.api.cloud.ru/v1"
ALLOWED_MODEL_BASE_URLS = {PROGRAM_BASE_URL, OFFICIAL_BASE_URL}
LLM_READ_TIMEOUT_SECONDS = 90


def read_secret_file(path: str, *, maximum: int = 16384) -> str:
    value = Path(path).read_text(encoding="utf-8")
    if len(value) > maximum:
        raise ValueError("Secret file is too large")
    value = value.strip("\r\n")
    if not value or "\x00" in value:
        raise ValueError("Secret file is empty or invalid")
    return value


DEFAULT_ADMIN_NETWORKS = "127.0.0.1/32,::1/128,10.77.0.0/24"


def parse_networks(value: str):
    """Comma separated CIDRs. A catch-all network would defeat the private-network rule."""
    networks = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        network = ipaddress.ip_network(part, strict=False)
        if network.prefixlen == 0:
            raise ValueError("NOTES_ADMIN_ALLOWED_NETWORKS must not contain a catch-all network")
        networks.append(network)
    if not networks:
        raise ValueError("NOTES_ADMIN_ALLOWED_NETWORKS must list at least one network")
    return tuple(networks)


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
    cloudru_base_url: str = PROGRAM_BASE_URL
    live_call_limit: int = 0
    live_user_call_limit: int = 0
    max_pending_per_user: int = 10
    daily_unit_limit: int = 30
    text_unit_cost: int = 1
    audio_unit_base: int = 2
    audio_unit_per_minute: int = 1
    limit_timezone: str = "Europe/Moscow"
    session_seconds: int = 86400
    lease_seconds: int = 120
    delivery_lease_seconds: int = 60
    error_log_file: str = "data/errors.log"
    audio_storage_path: str = "data/audio"
    audio_ffmpeg_path: str = "ffmpeg"
    audio_ffprobe_path: str = "ffprobe"
    analytics_pseudonym_key: str = field(default="", repr=False)
    internal_api_token: str = field(default="", repr=False)
    admin_allowed_networks: str = DEFAULT_ADMIN_NETWORKS
    admin_cookie_secure: bool = True
    # Шифрование содержимого на диске. Подробности в docs/encryption-v1.md. Файлы ключей читает app.crypto.
    data_encryption: str = "off"
    data_key_file: str = ""
    data_old_key_files: str = ""

    telegram_bot_username: str = "beresta_ru_bot"
    # Mail stays off until the owner configures an SMTP account. See docs/auth-telegram-v1.md.
    mail_enabled: bool = False
    email_registration_enabled: bool = False
    # smtp_bz sends over HTTPS (SMTP ports are closed at the VPS provider); smtp is plain STARTTLS.
    mail_transport: Literal["smtp_bz", "smtp"] = "smtp_bz"
    smtp_bz_api_key_file: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password_file: str = ""
    mail_from: str = ""

    def __post_init__(self):
        parse_networks(self.admin_allowed_networks)
        if self.data_encryption not in {"off", "required"}:
            raise ValueError("NOTES_DATA_ENCRYPTION must be off or required")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", self.telegram_bot_username):
            raise ValueError("NOTES_TELEGRAM_BOT_USERNAME must be a Telegram bot username without @")
        if self.mail_transport not in {"smtp_bz", "smtp"}:
            raise ValueError("NOTES_MAIL_TRANSPORT must be smtp_bz or smtp")
        if self.mail_enabled and not self.mail_from:
            raise ValueError("Mail needs NOTES_MAIL_FROM")
        if self.mail_enabled and self.mail_transport == "smtp" and not self.smtp_host:
            raise ValueError("SMTP mail needs NOTES_SMTP_HOST")
        if self.mail_enabled and self.mail_transport == "smtp_bz" and not self.smtp_bz_api_key_file:
            raise ValueError("SMTP.BZ mail needs NOTES_SMTP_BZ_API_KEY_FILE")
        if not 1 <= self.smtp_port <= 65535:
            raise ValueError("NOTES_SMTP_PORT must be a valid port")
        if self.analytics_pseudonym_key and len(self.analytics_pseudonym_key) < 32:
            raise ValueError("Analytics pseudonym key must contain at least 32 characters")
        if not 30 <= self.delivery_lease_seconds <= 300:
            raise ValueError("Delivery lease must be between 30 and 300 seconds")
        if self.internal_api_token and len(self.internal_api_token) < 32:
            raise ValueError("Internal API secret must contain at least 32 characters")
        if min(self.daily_unit_limit, self.text_unit_cost, self.audio_unit_base, self.audio_unit_per_minute) < 0:
            raise ValueError("Daily unit limit and unit costs must not be negative")
        try:
            ZoneInfo(self.limit_timezone)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ValueError("NOTES_LIMIT_TIMEZONE must be a known IANA time zone") from None
        if self.provider not in {"mock", "cloudru"}:
            raise ValueError("NOTES_PROVIDER must be mock or cloudru")
        if self.lease_seconds < LLM_READ_TIMEOUT_SECONDS + 30:
            raise ValueError("Worker lease must exceed the provider timeout")
        if self.provider == "cloudru" and (
            not self.cloudru_model
            or self.live_call_limit <= 0
            or self.live_user_call_limit <= 0
        ):
            raise ValueError("Live mode requires model and positive call limits")
        if self.provider == "cloudru" and self.cloudru_base_url.rstrip("/") not in ALLOWED_MODEL_BASE_URLS:
            raise ValueError("Model base URL must be an approved HTTPS endpoint")

    @classmethod
    def from_env(cls):
        def flag(name, default):
            value = os.environ.get(name, str(default)).lower()
            if value not in {"true", "false"}:
                raise ValueError(f"{name} must be true or false")
            return value == "true"

        database_url = os.environ.get("NOTES_DATABASE_URL")
        password_file = os.environ.get("NOTES_DATABASE_PASSWORD_FILE")
        if database_url and password_file:
            raise ValueError("Use either NOTES_DATABASE_URL or NOTES_DATABASE_PASSWORD_FILE")
        if password_file:
            password = quote_plus(read_secret_file(password_file, maximum=4096))
            user = quote_plus(os.environ.get("NOTES_DATABASE_USER", "notes"))
            host = os.environ.get("NOTES_DATABASE_HOST", "db")
            port = int(os.environ.get("NOTES_DATABASE_PORT", "5432"))
            name = quote_plus(os.environ.get("NOTES_DATABASE_NAME", "notes"))
            database_url = f"postgresql+psycopg://{user}:{password}@{host}:{port}/{name}"

        internal_token_file = os.environ.get("NOTES_INTERNAL_API_TOKEN_FILE")
        internal_token = os.environ.get("NOTES_INTERNAL_API_TOKEN", "")
        if internal_token_file and internal_token:
            raise ValueError("Use either NOTES_INTERNAL_API_TOKEN or NOTES_INTERNAL_API_TOKEN_FILE")

        analytics_file = os.environ.get("NOTES_ANALYTICS_PSEUDONYM_KEY_FILE")
        analytics_key = os.environ.get("NOTES_ANALYTICS_PSEUDONYM_KEY", "")
        if analytics_file and analytics_key:
            raise ValueError("Use either NOTES_ANALYTICS_PSEUDONYM_KEY or NOTES_ANALYTICS_PSEUDONYM_KEY_FILE")

        return cls(
            analytics_pseudonym_key=(read_secret_file(analytics_file, maximum=4096) if analytics_file else analytics_key),
            internal_api_token=(
                read_secret_file(internal_token_file, maximum=4096) if internal_token_file else internal_token
            ),
            database_url=database_url or cls.database_url,
            provider=os.environ.get("NOTES_PROVIDER", "mock"),
            auto_worker=flag("NOTES_AUTO_WORKER", True),
            secure_cookies=flag("NOTES_SECURE_COOKIES", False),
            admin_allowed_networks=os.environ.get("NOTES_ADMIN_ALLOWED_NETWORKS", DEFAULT_ADMIN_NETWORKS),
            admin_cookie_secure=flag("NOTES_ADMIN_COOKIE_SECURE", True),
            data_encryption=os.environ.get("NOTES_DATA_ENCRYPTION", cls.data_encryption),
            data_key_file=os.environ.get("NOTES_DATA_KEY_FILE", ""),
            data_old_key_files=os.environ.get("NOTES_DATA_OLD_KEY_FILES", ""),
            public_origin=os.environ.get("NOTES_PUBLIC_ORIGIN", cls.public_origin).rstrip("/"),
            allow_registration=flag("NOTES_ALLOW_REGISTRATION", True),
            allow_live_requests=flag("NOTES_ALLOW_LIVE_REQUESTS", False),
            cloudru_model=os.environ.get("NOTES_CLOUDRU_MODEL", ""),
            cloudru_base_url=os.environ.get("NOTES_CLOUDRU_BASE_URL", PROGRAM_BASE_URL),
            live_call_limit=int(os.environ.get("NOTES_LIVE_CALL_LIMIT", "0")),
            live_user_call_limit=int(os.environ.get("NOTES_LIVE_USER_CALL_LIMIT", "0")),
            daily_unit_limit=int(os.environ.get("NOTES_DAILY_UNIT_LIMIT", str(cls.daily_unit_limit))),
            text_unit_cost=int(os.environ.get("NOTES_TEXT_UNIT_COST", str(cls.text_unit_cost))),
            audio_unit_base=int(os.environ.get("NOTES_AUDIO_UNIT_BASE", str(cls.audio_unit_base))),
            audio_unit_per_minute=int(os.environ.get("NOTES_AUDIO_UNIT_PER_MINUTE", str(cls.audio_unit_per_minute))),
            limit_timezone=os.environ.get("NOTES_LIMIT_TIMEZONE", cls.limit_timezone),
            error_log_file=os.environ.get("NOTES_ERROR_LOG_FILE", "data/errors.log"),
            delivery_lease_seconds=int(os.environ.get("NOTES_DELIVERY_LEASE_SECONDS", "60")),
            audio_storage_path=os.environ.get("NOTES_AUDIO_STORAGE_PATH", cls.audio_storage_path),
            audio_ffmpeg_path=os.environ.get("NOTES_AUDIO_FFMPEG_PATH", cls.audio_ffmpeg_path),
            audio_ffprobe_path=os.environ.get("NOTES_AUDIO_FFPROBE_PATH", cls.audio_ffprobe_path),
            telegram_bot_username=os.environ.get("NOTES_TELEGRAM_BOT_USERNAME", cls.telegram_bot_username),
            mail_enabled=flag("NOTES_MAIL_ENABLED", False),
            email_registration_enabled=flag("NOTES_EMAIL_REGISTRATION_ENABLED", False),
            mail_transport=os.environ.get("NOTES_MAIL_TRANSPORT", "smtp_bz"),
            smtp_bz_api_key_file=os.environ.get("NOTES_SMTP_BZ_API_KEY_FILE", ""),
            smtp_host=os.environ.get("NOTES_SMTP_HOST", ""),
            smtp_port=int(os.environ.get("NOTES_SMTP_PORT", "587")),
            smtp_user=os.environ.get("NOTES_SMTP_USER", ""),
            smtp_password_file=os.environ.get("NOTES_SMTP_PASSWORD_FILE", ""),
            mail_from=os.environ.get("NOTES_MAIL_FROM", ""),
        )
