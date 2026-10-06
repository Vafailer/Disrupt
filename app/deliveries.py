"""Transactional delivery permissions. No Telegram calls or scheduler loop."""

import hashlib
import hmac
import json
import secrets
import time

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from app.analytics import record_event
from app.models import Item, Note, Outbox, Reminder, TelegramIdentity
from app.security import hash_token
from app.services import lock_account


def locked_delivery(db, delivery_id):
    if "\x00" in delivery_id:
        raise HTTPException(404, "Доставка не найдена")
    owner = db.scalar(select(Outbox.user_id).where(Outbox.id == delivery_id))
    if owner is None:
        raise HTTPException(404, "Доставка не найдена")
    lock_account(db, owner)
    return db.scalar(select(Outbox).where(Outbox.id == delivery_id).execution_options(populate_existing=True))


def matches_lease(row, body):
    return row.generation == body.generation and row.lease_token_hash is not None and hmac.compare_digest(
        row.lease_token_hash, hash_token(body.lease_token),
    )


def current_target(db, row, now):
    reminder = db.get(Reminder, row.reminder_id)
    if (
        reminder is None or reminder.user_id != row.user_id or reminder.generation != row.generation
        or reminder.status != "confirmed" or reminder.scheduled_at > now
    ):
        return None
    note = db.get(Note, reminder.note_id)
    if note is None or note.user_id != row.user_id:
        return None
    if reminder.item_id is not None:
        item = db.get(Item, reminder.item_id)
        if (
            item is None or item.user_id != row.user_id or item.note_id != note.id
            or item.kind != "task" or item.status != "open"
        ):
            return None
    identity = db.scalar(select(TelegramIdentity).where(
        TelegramIdentity.user_id == row.user_id, TelegramIdentity.bot_id == row.bot_id,
        TelegramIdentity.chat_id == row.chat_id, TelegramIdentity.notifications_enabled.is_(True),
        TelegramIdentity.delivery_status == "available",
    ))
    return (reminder, note) if identity else None


def attempt_id(row):
    return row.id + ":" + (row.lease_token_hash[:16] if row.lease_token_hash else "untracked")


def expire_authorization(db, row):
    row.status, row.error_code = "unknown", "delivery_lease_expired"
    reminder = db.get(Reminder, row.reminder_id)
    if (
        reminder is not None and reminder.user_id == row.user_id
        and reminder.generation == row.generation and reminder.status == "confirmed"
    ):
        reminder.status = "unknown"
    record_event(db, row.user_id, "reminder_failed", attempt_id(row), "telegram")


def claim_deliveries(db, body, settings):
    now = time.time()
    expired = db.execute(select(Outbox.id, Outbox.user_id, Outbox.created_at).where(
        Outbox.bot_id == body.bot_id, Outbox.status == "authorized", Outbox.lease_until <= now,
    ).order_by(Outbox.user_id, Outbox.created_at, Outbox.id).limit(100)).all()
    ready = db.execute(
        select(Outbox.id, Outbox.user_id, Outbox.created_at)
        .join(Reminder, Reminder.id == Outbox.reminder_id)
        .join(Note, and_(Note.id == Reminder.note_id, Note.user_id == Outbox.user_id))
        .join(TelegramIdentity, and_(
            TelegramIdentity.user_id == Outbox.user_id, TelegramIdentity.bot_id == Outbox.bot_id,
            TelegramIdentity.chat_id == Outbox.chat_id,
        ))
        .outerjoin(Item, Item.id == Reminder.item_id)
        .where(
            Outbox.bot_id == body.bot_id, Reminder.user_id == Outbox.user_id,
            Reminder.generation == Outbox.generation, Reminder.status == "confirmed", Reminder.scheduled_at <= now,
            TelegramIdentity.notifications_enabled.is_(True), TelegramIdentity.delivery_status == "available",
            or_(Reminder.item_id.is_(None), and_(
                Item.user_id == Outbox.user_id, Item.note_id == Reminder.note_id, Item.kind == "task", Item.status == "open",
            )),
            or_(
                and_(Outbox.status == "pending", Outbox.authorized_at.is_(None)),
                and_(Outbox.status == "retryable", Outbox.result_hash.is_not(None), Outbox.retry_at <= now),
                and_(Outbox.status == "leased", Outbox.authorized_at.is_(None), Outbox.lease_until <= now),
            ),
        ).order_by(Outbox.user_id, Outbox.created_at, Outbox.id).limit(body.limit)
    ).all()
    results = []
    # All account locks follow the same order, including expired attempts.
    candidates = sorted({(row.user_id, row.created_at, row.id) for row in [*expired, *ready]})
    for _, _, delivery_id in candidates:
        row = locked_delivery(db, delivery_id)
        now = time.time()
        if row.status == "authorized" and row.lease_until <= now:
            expire_authorization(db, row)
            continue
        if not (
            row.status == "pending" and row.authorized_at is None
            or row.status == "retryable" and row.result_hash is not None and row.retry_at <= now
            or row.status == "leased" and row.authorized_at is None and row.lease_until <= now
        ):
            continue
        target = current_target(db, row, now)
        if target is None:
            continue
        reminder, note = target
        lease = secrets.token_urlsafe(32)
        callback = secrets.token_urlsafe(32) if reminder.item_id is not None else None
        row.status, row.lease_token_hash = "leased", hash_token(lease)
        row.lease_until = now + settings.delivery_lease_seconds
        row.authorized_at = row.result_hash = row.retry_at = row.error_code = row.telegram_message_id = None
        row.callback_token_hash = hash_token(callback) if callback else None
        results.append({
            "delivery_id": row.id, "lease_token": lease, "generation": row.generation,
            "chat_id": row.chat_id, "text": reminder.text,
            "note_url": settings.public_origin + "/?capture=" + note.capture_id, "callback_token": callback,
        })
    db.flush()
    return {"items": results}


def authorize_delivery(db, delivery_id, body, settings):
    row = locked_delivery(db, delivery_id)
    now = time.time()
    if (
        not matches_lease(row, body) or row.status != "leased"
        or row.lease_until is None or row.lease_until <= now
    ):
        return {"send": False}
    if current_target(db, row, now) is None:
        row.status, row.error_code = "cancelled", "delivery_invalid"
        return {"send": False}
    row.status, row.authorized_at = "authorized", now
    row.lease_until = now + settings.delivery_lease_seconds
    return {"send": True}


def record_delivery_result(db, delivery_id, body):
    row = locked_delivery(db, delivery_id)
    if not matches_lease(row, body) or row.authorized_at is None:
        raise HTTPException(409, "Результат не относится к разрешённой попытке")
    digest = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
    if row.result_hash is not None:
        if not hmac.compare_digest(row.result_hash, digest):
            raise HTTPException(409, "Для этой попытки уже записан другой результат")
        return {"status": "recorded"}
    if row.status not in {"authorized", "unknown"}:
        raise HTTPException(409, "Попытка не была разрешена")
    row.status, row.result_hash = body.status, digest
    row.telegram_message_id, row.error_code = body.telegram_message_id, body.error_code
    row.retry_at = time.time() + body.retry_after_seconds if body.status == "retryable" else None
    reminder = db.get(Reminder, row.reminder_id)
    if (
        reminder is not None and reminder.user_id == row.user_id
        and reminder.generation == row.generation and reminder.status in {"confirmed", "unknown"}
    ):
        reminder.status = "confirmed" if body.status == "retryable" else body.status
    if body.status == "blocked":
        identity = db.scalar(select(TelegramIdentity).where(
            TelegramIdentity.user_id == row.user_id, TelegramIdentity.bot_id == row.bot_id,
            TelegramIdentity.chat_id == row.chat_id,
        ))
        if identity:
            identity.delivery_status = "blocked"
    record_event(
        db, row.user_id, "reminder_sent" if body.status == "sent" else "reminder_failed", attempt_id(row), "telegram",
    )
    return {"status": "recorded"}
