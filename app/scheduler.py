"""Enqueue confirmed reminders. This process never calls Telegram or an AI provider."""

import argparse
import logging
import signal
import threading
import time

from sqlalchemy import and_, or_, select

from app import crypto
from app.config import Settings
from app.db import make_database
from app.models import Item, Note, Outbox, Reminder, TelegramIdentity
from app.services import lock_account


def ready_reminders(now):
    identity = select(TelegramIdentity.id).where(
        TelegramIdentity.user_id == Reminder.user_id,
        TelegramIdentity.notifications_enabled.is_(True), TelegramIdentity.delivery_status == "available",
    ).exists()
    already_enqueued = select(Outbox.id).where(
        Outbox.reminder_id == Reminder.id, Outbox.generation == Reminder.generation,
    ).exists()
    return (
        select(Reminder.id, Reminder.user_id)
        .join(Note, and_(Note.id == Reminder.note_id, Note.user_id == Reminder.user_id))
        .outerjoin(Item, Item.id == Reminder.item_id)
        .where(
            Reminder.status == "confirmed", Reminder.scheduled_at <= now, identity, ~already_enqueued,
            or_(Reminder.item_id.is_(None), and_(
                Item.user_id == Reminder.user_id, Item.note_id == Reminder.note_id,
                Item.kind == "task", Item.status == "open",
            )),
        ).order_by(Reminder.scheduled_at, Reminder.id)
    )


class Scheduler:
    def __init__(self, sessions):
        self.sessions = sessions

    def run_once(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Scheduler batch must be between 1 and 1000")
        with self.sessions() as db:
            candidates = db.execute(ready_reminders(time.time()).limit(limit)).all()
        created = 0
        for reminder_id, user_id in candidates:
            # One account per transaction. Edits, callbacks and other schedulers use the same lock.
            with self.sessions.begin() as db:
                lock_account(db, user_id)
                eligible = db.execute(ready_reminders(time.time()).where(Reminder.id == reminder_id)).first()
                if eligible is None:
                    continue
                reminder = db.get(Reminder, reminder_id)
                # If several bots are linked, consistently choose the oldest available link.
                identity = db.scalar(select(TelegramIdentity).where(
                    TelegramIdentity.user_id == user_id, TelegramIdentity.notifications_enabled.is_(True),
                    TelegramIdentity.delivery_status == "available",
                ).order_by(TelegramIdentity.created_at, TelegramIdentity.id).limit(1))
                db.add(Outbox(
                    reminder_id=reminder.id, user_id=user_id, generation=reminder.generation,
                    bot_id=identity.bot_id, chat_id=identity.chat_id, status="pending",
                ))
            created += 1
        return created


def main():
    parser = argparse.ArgumentParser(description="Enqueue due Beresta reminders")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    stop = threading.Event()
    for name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(name, lambda *_: stop.set())
    engine = None
    try:
        settings = Settings.from_env()
        crypto.configure_from_settings(settings)
        engine, sessions = make_database(settings.database_url)
        scheduler = Scheduler(sessions)
        while not stop.is_set():
            try:
                count = scheduler.run_once()
            except Exception:
                # Database exceptions may contain connection credentials or private values.
                logging.warning("scheduler_paused database_or_internal_error")
                if args.once:
                    return 1
                stop.wait(5)
                continue
            if args.once:
                return 0
            stop.wait(0.1 if count == 100 else 5)
    except Exception:
        logging.error("scheduler_stopped configuration_or_database_error")
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
