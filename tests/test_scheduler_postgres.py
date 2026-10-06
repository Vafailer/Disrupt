import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from app.config import Settings
from app.db import make_database
from app.models import Outbox, Reminder, TelegramIdentity, User
from app.scheduler import Scheduler
from app.services import capture_text, lock_account


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="Local PostgreSQL is not configured")
def test_postgres_scheduler_duplicates_and_cancellation_race(monkeypatch):
    url = os.environ["TEST_POSTGRES_URL"]
    assert make_url(url).host in {"127.0.0.1", "localhost", "::1"}
    monkeypatch.setenv("NOTES_DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine, sessions = make_database(url)
    try:
        with sessions.begin() as db:
            user = User(username="pg_scheduler_" + uuid.uuid4().hex, password_hash="synthetic")
            db.add(user)
            db.flush()
            user_id = user.id
            note = capture_text(db, user_id, "Synthetic note", "note", Settings(database_url=url),
                                processing_mode="manual", commit=False)
            db.add(TelegramIdentity(user_id=user_id, bot_id=12345, telegram_user_id=uuid.uuid4().int % 2**50,
                                    chat_id=123))
            reminder = Reminder(user_id=user_id, note_id=note.id, scheduled_at=time.time() - 1,
                                timezone="Europe/Moscow", text="Synthetic reminder")
            db.add(reminder)
            db.flush()
            reminder_id = reminder.id
        scheduler = Scheduler(sessions)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: scheduler.run_once(), range(4)))
        with sessions() as db:
            assert db.scalar(select(func.count()).select_from(Outbox).where(Outbox.reminder_id == reminder_id)) == 1
        # Move to a fresh generation, then race cancellation against several schedulers.
        with sessions.begin() as db:
            lock_account(db, user_id)
            db.get(Reminder, reminder_id).generation = 2

        def cancel():
            from app.services import cancel_reminder_deliveries

            with sessions.begin() as db:
                lock_account(db, user_id)
                cancel_reminder_deliveries(db, user_id, [reminder_id])
                row = db.get(Reminder, reminder_id)
                row.status, row.generation = "cancelled", 3

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(cancel), *[pool.submit(scheduler.run_once) for _ in range(3)]]
            for future in futures:
                future.result()
        with sessions() as db:
            rows = db.scalars(select(Outbox).where(Outbox.reminder_id == reminder_id)).all()
            assert len(rows) <= 2 and all(row.status == "cancelled" for row in rows)
            assert db.get(Reminder, reminder_id).generation == 3
    finally:
        engine.dispose()
