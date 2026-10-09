import time
import uuid
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def new_id():
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('user', 'admin')", name="ck_users_role"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    live_calls: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[str] = mapped_column(String(16), default="user", server_default="user")
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    source: Mapped[str] = mapped_column(String(100), default="unknown", server_default="unknown")
    created_at: Mapped[float | None] = mapped_column(Float, nullable=True, default=time.time)
    first_login_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class LoginSession(Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[float] = mapped_column(Float)


class LoginThrottle(Base):
    __tablename__ = "login_throttles"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[float] = mapped_column(Float)
    attempts: Mapped[int] = mapped_column(Integer)


class Capture(Base):
    __tablename__ = "captures"
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    original_text: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    channel: Mapped[str] = mapped_column(String(16), default="web", server_default="web")
    processing_mode: Mapped[str] = mapped_column(String(16), default="ai", server_default="ai")
    input_kind: Mapped[str] = mapped_column(String(16), default="text", server_default="text")
    audio_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    audio_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    audio_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audio_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    audio_media_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class TranscriptRevision(Base):
    __tablename__ = "transcript_revisions"
    __table_args__ = (
        UniqueConstraint("capture_id", "version"),
        CheckConstraint("origin IN ('stt', 'user', 'legacy')", name="ck_transcript_origin"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    capture_id: Mapped[str] = mapped_column(ForeignKey("captures.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    capture_id: Mapped[str] = mapped_column(ForeignKey("captures.id"), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    provider: Mapped[str] = mapped_column(String(16))
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class Note(Base):
    __tablename__ = "notes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    capture_id: Mapped[str] = mapped_column(ForeignKey("captures.id"), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    markdown: Mapped[str] = mapped_column(Text)
    conclusions: Mapped[list] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    provider: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)
    category_id: Mapped[str | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    structure_confirmed_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class Revision(Base):
    __tablename__ = "revisions"
    __table_args__ = (UniqueConstraint("note_id", "version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    note_id: Mapped[str] = mapped_column(ForeignKey("notes.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class ProviderBudget(Base):
    __tablename__ = "provider_budgets"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    reserved_calls: Mapped[int] = mapped_column(Integer, default=0)


class TelegramIdentity(Base):
    __tablename__ = "telegram_identities"
    __table_args__ = (
        UniqueConstraint("bot_id", "telegram_user_id"),
        UniqueConstraint("bot_id", "user_id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    bot_id: Mapped[int] = mapped_column(BigInteger)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    delivery_status: Mapped[str] = mapped_column(String(16), default="available")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class LinkRequest(Base):
    __tablename__ = "link_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="issued")
    bot_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expires_at: Mapped[float] = mapped_column(Float)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("user_id", "name"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (
        CheckConstraint("kind IN ('note', 'idea', 'task', 'goal', 'plan')", name="ck_items_kind"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    note_id: Mapped[str] = mapped_column(ForeignKey("notes.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="open")
    version: Mapped[int] = mapped_column(Integer, default=1)
    due_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    category_id: Mapped[str | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    source_quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_text: Mapped[str | None] = mapped_column(String(300), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class Reminder(Base):
    __tablename__ = "reminders"
    __table_args__ = (
        CheckConstraint("generation >= 1", name="ck_reminders_generation"),
        Index("uq_reminders_user_idempotency", "user_id", "idempotency_key", unique=True),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    note_id: Mapped[str] = mapped_column(ForeignKey("notes.id"))
    item_id: Mapped[str | None] = mapped_column(ForeignKey("items.id"), nullable=True)
    scheduled_at: Mapped[float] = mapped_column(Float, index=True)
    timezone: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="confirmed", index=True)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    confirmed_at: Mapped[float] = mapped_column(Float, default=time.time)
    idempotency_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    creation_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Inbox(Base):
    __tablename__ = "inbox"
    __table_args__ = (UniqueConstraint("bot_id", "update_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    bot_id: Mapped[int] = mapped_column(BigInteger)
    update_id: Mapped[int] = mapped_column(BigInteger)
    payload_hash: Mapped[str] = mapped_column(String(64))
    operation: Mapped[str] = mapped_column(String(32))
    response: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = (UniqueConstraint("reminder_id", "generation"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    reminder_id: Mapped[str] = mapped_column(ForeignKey("reminders.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    bot_id: Mapped[int] = mapped_column(BigInteger)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    generation: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    lease_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    authorized_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    callback_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retry_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    result_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class ProductEvent(Base):
    __tablename__ = "product_events"
    __table_args__ = (UniqueConstraint("user_id", "name", "operation_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    operation_id: Mapped[str] = mapped_column(String(100))
    channel: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(100))
    is_test: Mapped[bool] = mapped_column(Boolean)
    subject_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    app_version: Mapped[str] = mapped_column(String(32), default="integration-v1")
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    occurred_at: Mapped[float] = mapped_column(Float, default=time.time, index=True)


class ProviderUsage(Base):
    __tablename__ = "provider_usage"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    operation_id: Mapped[str] = mapped_column(String(100), index=True)
    request_id: Mapped[str] = mapped_column(String(100), unique=True)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    channel: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(200))
    tariff_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stt_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    occurred_at: Mapped[float] = mapped_column(Float, default=time.time, index=True)


# Register the independent feedback table for Alembic metadata.
from app.feedback_models import Feedback  # noqa: E402, F401
