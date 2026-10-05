"""Server-authored, content-free events. No commits: events follow the business transaction."""

import time

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.models import ProductEvent, User, new_id

BACKGROUND_EVENTS = {"processing_completed", "processing_failed", "reminder_sent", "reminder_failed"}


def record_event(db, user_id, name, operation_id, channel="web", *, occurred_at=None):
    now = time.time() if occurred_at is None else occurred_at
    # An UPDATE locks the account on both supported engines and serializes session assignment.
    # Background processing must not extend a human's analytical session.
    db.execute(update(User).where(User.id == user_id).values(source=User.source))
    user = db.get(User, user_id)
    previous = db.scalar(
        select(ProductEvent)
        .where(ProductEvent.user_id == user_id, ProductEvent.name.not_in(BACKGROUND_EVENTS))
        .order_by(ProductEvent.occurred_at.desc(), ProductEvent.id)
        .limit(1)
    )
    session_id = previous.session_id if previous and 0 <= now - previous.occurred_at <= 1800 else new_id()
    insert = sqlite_insert if db.bind.dialect.name == "sqlite" else pg_insert
    db.execute(
        insert(ProductEvent)
        .values(
            id=new_id(),
            user_id=user_id,
            name=name,
            operation_id=operation_id,
            channel=channel,
            source=user.source,
            is_test=user.is_test,
            session_id=session_id,
            occurred_at=now,
            app_version="integration-v1",
        )
        .on_conflict_do_nothing(index_elements=["user_id", "name", "operation_id"])
    )
