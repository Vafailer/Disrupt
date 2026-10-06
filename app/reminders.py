"""User-confirmed reminders. The scheduler and Telegram sender live elsewhere."""

import hashlib
import json
import re
import time
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select

from app.analytics import record_event
from app.models import Item, Outbox, Reminder
from app.services import cancel_reminder_deliveries, lock_account, owned_note


def reminder_view(db, reminder):
    delivery = db.scalar(select(Outbox.status).where(
        Outbox.reminder_id == reminder.id, Outbox.user_id == reminder.user_id,
        Outbox.generation == reminder.generation,
    ))
    previous_unknown = db.scalar(select(Outbox.id).where(
        Outbox.reminder_id == reminder.id, Outbox.user_id == reminder.user_id,
        Outbox.generation < reminder.generation, Outbox.status == "unknown",
        Outbox.authorized_at.is_not(None),
    ).limit(1)) is not None
    return {
        "id": reminder.id, "note_id": reminder.note_id, "item_id": reminder.item_id,
        "scheduled_at": datetime.fromtimestamp(reminder.scheduled_at, UTC).isoformat(),
        "timezone": reminder.timezone, "text": reminder.text, "status": reminder.status,
        "generation": reminder.generation,
        "confirmed_at": datetime.fromtimestamp(reminder.confirmed_at, UTC).isoformat(),
        "delivery_status": delivery,
        "previous_attempt_unknown": previous_unknown,
    }


def owned_reminder(db, reminder_id, user_id):
    if "\x00" in reminder_id:
        raise HTTPException(404, "Напоминание не найдено")
    row = db.scalar(select(Reminder).where(Reminder.id == reminder_id, Reminder.user_id == user_id))
    if row is None:
        raise HTTPException(404, "Напоминание не найдено")
    return row


def validate_target(db, note_id, item_id, user_id):
    owned_note(db, note_id, user_id)
    if item_id is not None:
        if "\x00" in item_id:
            raise HTTPException(404, "Задача не найдена")
        item = db.scalar(select(Item).where(
            Item.id == item_id, Item.note_id == note_id, Item.user_id == user_id,
        ))
        if item is None:
            raise HTTPException(404, "Задача не найдена")
        if item.kind != "task" or item.status != "open":
            raise HTTPException(409, "Напоминание можно назначить только открытой задаче")


def future_time(value):
    scheduled_at = datetime.fromisoformat(value).timestamp()
    if scheduled_at <= time.time():
        raise HTTPException(422, "Выберите точное время в будущем")
    return scheduled_at


def create_reminder(db, user_id, note_id, body, key):
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", key):
        raise HTTPException(422, "Некорректный Idempotency-Key")
    lock_account(db, user_id)
    owned_note(db, note_id, user_id)
    digest = hashlib.sha256(json.dumps(
        {"note_id": note_id, **body.model_dump()}, sort_keys=True, ensure_ascii=False,
    ).encode()).hexdigest()
    previous = db.scalar(select(Reminder).where(
        Reminder.user_id == user_id, Reminder.idempotency_key == key,
    ))
    if previous:
        if previous.creation_hash != digest:
            raise HTTPException(409, "Этот Idempotency-Key уже использован для другого напоминания")
        return previous
    validate_target(db, note_id, body.item_id, user_id)
    row = Reminder(
        user_id=user_id, note_id=note_id, item_id=body.item_id,
        scheduled_at=future_time(body.scheduled_at), timezone=body.timezone, text=body.text,
        idempotency_key=key, creation_hash=digest,
    )
    db.add(row)
    db.flush()
    record_event(db, user_id, "reminder_confirmed", f"{row.id}:1", subject_id=row.note_id)
    return row


def edit_reminder(db, user_id, reminder_id, body):
    lock_account(db, user_id)
    row = owned_reminder(db, reminder_id, user_id)
    if row.generation != body.generation:
        raise HTTPException(409, "Напоминание уже изменено. Обновите его")
    validate_target(db, row.note_id, row.item_id, user_id)
    scheduled_at = future_time(body.scheduled_at)
    cancel_reminder_deliveries(db, user_id, [row.id])
    row.scheduled_at, row.timezone, row.text = scheduled_at, body.timezone, body.text
    row.status, row.confirmed_at = "confirmed", time.time()
    row.generation += 1
    record_event(db, user_id, "reminder_confirmed", f"{row.id}:{row.generation}", subject_id=row.note_id)
    return row


def cancel_reminder(db, user_id, reminder_id, generation):
    lock_account(db, user_id)
    row = owned_reminder(db, reminder_id, user_id)
    if row.status == "cancelled" and row.generation in {generation, generation + 1}:
        return row
    if row.generation != generation:
        raise HTTPException(409, "Напоминание уже изменено. Обновите его")
    cancel_reminder_deliveries(db, user_id, [row.id])
    row.status = "cancelled"
    row.generation += 1
    record_event(db, user_id, "reminder_cancelled", f"{row.id}:{row.generation}")
    return row
