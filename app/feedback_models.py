"""User-submitted support requests. Never includes note content automatically."""
import time
import uuid

from sqlalchemy import CheckConstraint, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Feedback(Base):
    __tablename__ = 'feedback'
    __table_args__ = (
        UniqueConstraint('user_id', 'idempotency_key', name='uq_feedback_user_key'),
        CheckConstraint("kind IN ('bug','idea','question','other')", name='ck_feedback_kind'),
        CheckConstraint("status IN ('new','in_progress','resolved')", name='ck_feedback_status'),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(100))
    payload_hash: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    subject: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text)
    steps: Mapped[str] = mapped_column(Text)
    expected: Mapped[str] = mapped_column(Text)
    contact: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default='new')
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[float] = mapped_column(Float, default=time.time, index=True)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)
