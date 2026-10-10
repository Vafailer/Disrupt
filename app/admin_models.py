"""Administrator identities. They are separate from regular users and never share their tables."""
import time
import uuid

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def new_id():
    return str(uuid.uuid4())


class AdminAccount(Base):
    __tablename__ = "admin_accounts"
    __table_args__ = (UniqueConstraint("username", name="uq_admin_accounts_username"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64))
    password_hash: Mapped[str] = mapped_column(String(256))
    # Shown once by the CLI and never returned by any API.
    totp_secret: Mapped[str] = mapped_column(String(64))
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    last_totp_step: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    last_login_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    locked_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())


class AdminSession(Base):
    __tablename__ = "admin_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    admin_id: Mapped[str] = mapped_column(ForeignKey("admin_accounts.id"), index=True)
    created_at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float)
    last_seen_at: Mapped[float] = mapped_column(Float)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    ip: Mapped[str] = mapped_column(String(64))


class AdminAudit(Base):
    """Who did what. Never holds passwords, codes, tokens or user content."""

    __tablename__ = "admin_audit_log"
    __table_args__ = (Index("ix_admin_audit_log_created_at", "created_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    admin_id: Mapped[str | None] = mapped_column(ForeignKey("admin_accounts.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
