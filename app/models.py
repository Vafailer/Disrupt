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
    UniqueConstraint,
    false,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.encrypted_types import EncryptedJSON, EncryptedText


def new_id():
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'admin')", name="ck_users_role"),
        Index("uq_users_email", "email", unique=True),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    live_calls: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[str] = mapped_column(String(16), default="user", server_default="user")
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    source: Mapped[str] = mapped_column(String(100), default="unknown", server_default="unknown")
    created_at: Mapped[float | None] = mapped_column(Float, nullable=True, default=time.time)
    first_login_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    assistant_recommendations_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(),
    )
    # Always stored lowercased. Empty until the owner of the address confirms it.
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    email_verified_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    policy_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    policy_accepted_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    deletion_requested_at: Mapped[float | None] = mapped_column(Float, nullable=True)


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
    original_text: Mapped[str] = mapped_column(EncryptedText("captures.original_text"))
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
    transcript: Mapped[str | None] = mapped_column(EncryptedText("captures.transcript"), nullable=True)
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
    text: Mapped[str] = mapped_column(EncryptedText("transcript_revisions.text"))
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
    title: Mapped[str] = mapped_column(EncryptedText("notes.title"))
    markdown: Mapped[str] = mapped_column(EncryptedText("notes.markdown"))
    conclusions: Mapped[list] = mapped_column(EncryptedJSON("notes.conclusions"))
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
    snapshot: Mapped[dict] = mapped_column(EncryptedJSON("revisions.snapshot"))
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
    text: Mapped[str] = mapped_column(EncryptedText("items.text"))
    status: Mapped[str] = mapped_column(String(16), default="open")
    version: Mapped[int] = mapped_column(Integer, default=1)
    due_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    category_id: Mapped[str | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    source_quote: Mapped[str | None] = mapped_column(EncryptedText("items.source_quote"), nullable=True)
    due_text: Mapped[str | None] = mapped_column(EncryptedText("items.due_text"), nullable=True)
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
    text: Mapped[str] = mapped_column(EncryptedText("reminders.text"))
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
    response: Mapped[dict] = mapped_column(EncryptedJSON("inbox.response"))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        UniqueConstraint("reminder_id", "generation"),
        UniqueConstraint("job_id", name="uq_outbox_processing_job"),
        CheckConstraint(
            "(reminder_id IS NOT NULL AND job_id IS NULL AND message_kind IS NULL) OR "
            "(reminder_id IS NULL AND job_id IS NOT NULL AND message_kind IS NULL) OR "
            "(reminder_id IS NULL AND job_id IS NULL AND message_kind IS NOT NULL)",
            name="ck_outbox_target",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    reminder_id: Mapped[str | None] = mapped_column(ForeignKey("reminders.id"), index=True, nullable=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
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
    # Account messages (for example a password reset link). The text is erased once the attempt is final.
    message_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    message_text: Mapped[str | None] = mapped_column(EncryptedText("outbox.message_text"), nullable=True)


class TelegramLogin(Base):
    """One browser sign-in (or deletion confirmation) answered by the bot. Only hashes are stored."""

    __tablename__ = "telegram_logins"
    __table_args__ = (
        CheckConstraint("purpose IN ('login', 'delete')", name="ck_telegram_logins_purpose"),
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'consumed')", name="ck_telegram_logins_status",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    purpose: Mapped[str] = mapped_column(String(16), default="login", server_default="login")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    binding_hash: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    policy_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    bot_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    telegram_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[float] = mapped_column(Float)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    confirmed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    consumed_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class EmailVerification(Base):
    """A one-time token for confirming an address or resetting a password. Only the hash is stored."""

    __tablename__ = "email_verifications"
    __table_args__ = (
        CheckConstraint("purpose IN ('verify', 'reset')", name="ck_email_verifications_purpose"),
        CheckConstraint("channel IN ('email', 'telegram')", name="ck_email_verifications_channel"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    purpose: Mapped[str] = mapped_column(String(16))
    channel: Mapped[str] = mapped_column(String(16), default="email", server_default="email")
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[float] = mapped_column(Float)
    used_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class EmailRegistrationPending(Base):
    __tablename__ = "email_registration_pending"
    __table_args__ = (CheckConstraint("attempts >= 0 AND attempts <= 5", name="ck_email_registration_attempts"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(254), index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    binding_hash: Mapped[str] = mapped_column(String(64))
    code_hash: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(String(32))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    expires_at: Mapped[float] = mapped_column(Float)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    used_at: Mapped[float | None] = mapped_column(Float, nullable=True)


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


class AssistantRequest(Base):
    """One AI assistant action (ask, recommend, digest). The worker leases it like a job."""

    __tablename__ = "assistant_requests"
    __table_args__ = (
        CheckConstraint("kind IN ('ask', 'recommend', 'digest')", name="ck_assistant_requests_kind"),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_assistant_requests_status",
        ),
        UniqueConstraint("user_id", "idempotency_key", name="uq_assistant_requests_user_key"),
        Index("ix_assistant_requests_user_created", "user_id", "created_at"),
        Index("ix_assistant_requests_status", "status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="queued")
    question: Mapped[str | None] = mapped_column(EncryptedText("assistant_requests.question"), nullable=True)
    days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_note_ids: Mapped[list] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(EncryptedJSON("assistant_requests.result"), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    units: Mapped[int] = mapped_column(Integer, default=1)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    started_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)


# Register the independent feedback and admin tables for Alembic metadata.
from app.admin_models import AdminAccount, AdminAudit, AdminSession  # noqa: E402, F401
from app.feedback_models import Feedback  # noqa: E402, F401
