import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from app.models import Item, Outbox, ProductEvent, Reminder, TelegramIdentity
from app.scheduler import Scheduler
from tests.test_reminders import add, payload
from tests.test_reminders import reminders as reminders


def make_due(app, client, ids, *, key="due", item=True):
    created = add(client, ids, payload(ids["item"] if item else None), key).json()
    with app.state.sessions.begin() as db:
        db.get(Reminder, created["id"]).scheduled_at = time.time() - 1
    return created["id"]


def test_scheduler_concurrent_runs_and_restart_do_not_duplicate(reminders):
    app, client, ids = reminders
    reminder_id = make_due(app, client, ids)
    scheduler = Scheduler(app.state.sessions)
    with ThreadPoolExecutor(max_workers=4) as pool:
        counts = list(pool.map(lambda _: scheduler.run_once(), range(4)))
    assert sum(counts) == 1
    assert Scheduler(app.state.sessions).run_once() == 0
    with app.state.sessions() as db:
        row = db.scalar(select(Outbox))
        link = db.scalar(select(TelegramIdentity))
        assert (row.reminder_id, row.user_id, row.generation, row.status) == (reminder_id, ids["user"], 1, "pending")
        assert (row.bot_id, row.chat_id) == (link.bot_id, link.chat_id)
        assert row.lease_token_hash is None and row.authorized_at is None
        assert db.scalar(select(func.count()).select_from(ProductEvent).where(ProductEvent.name == "reminder_sent")) == 0


@pytest.mark.parametrize("change", ["future", "cancelled", "completed", "idea", "disabled", "blocked", "unlinked"])
def test_scheduler_skips_ineligible_reminders(reminders, change):
    app, client, ids = reminders
    reminder_id = make_due(app, client, ids)
    with app.state.sessions.begin() as db:
        if change == "future":
            db.get(Reminder, reminder_id).scheduled_at = time.time() + 3600
        elif change == "cancelled":
            db.get(Reminder, reminder_id).status = "cancelled"
        elif change == "completed":
            db.get(Item, ids["item"]).status = "completed"
        elif change == "idea":
            db.get(Item, ids["item"]).kind = "idea"
        else:
            link = db.scalar(select(TelegramIdentity))
            if change == "disabled":
                link.notifications_enabled = False
            elif change == "blocked":
                link.delivery_status = "blocked"
            else:
                db.delete(link)
    assert Scheduler(app.state.sessions).run_once() == 0
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Outbox)) == 0


def test_scheduler_rechecks_target_after_taking_account_lock(reminders, monkeypatch):
    import app.scheduler as module

    app, client, ids = reminders
    make_due(app, client, ids)
    original_lock = module.lock_account

    def completed_between_scan_and_lock(db, user_id):
        original_lock(db, user_id)
        db.get(Item, ids["item"]).status = "completed"
        db.flush()

    monkeypatch.setattr(module, "lock_account", completed_between_scan_and_lock)
    assert Scheduler(app.state.sessions).run_once() == 0


@pytest.mark.parametrize("target", ["note", "item"])
def test_scheduler_refuses_cross_account_targets(reminders, target):
    from app.models import Note, User

    app, client, ids = reminders
    make_due(app, client, ids)
    with app.state.sessions.begin() as db:
        foreign = User(username="foreign_scheduler", password_hash="synthetic")
        db.add(foreign)
        db.flush()
        row = db.get(Note, ids["note"]) if target == "note" else db.get(Item, ids["item"])
        row.user_id = foreign.id
    assert Scheduler(app.state.sessions).run_once() == 0


def test_old_outbox_and_ineligible_rows_do_not_starve_new_generation(reminders):
    from tests.test_reminders import future

    app, client, ids = reminders
    reminder_id = make_due(app, client, ids, item=False)
    scheduler = Scheduler(app.state.sessions)
    assert scheduler.run_once(limit=1) == 1
    # Multiple already-enqueued generations cannot fill the scan limit forever.
    second_id = make_due(app, client, ids, key="second", item=False)
    assert scheduler.run_once(limit=1) == 1
    assert client.patch("/api/v1/reminders/" + reminder_id, json={
        "scheduled_at": future(), "timezone": "Europe/Moscow", "text": "Новая дата", "generation": 1,
    }).status_code == 200
    with app.state.sessions.begin() as db:
        db.get(Reminder, reminder_id).scheduled_at = time.time() - 1
    assert scheduler.run_once(limit=1) == 1
    with app.state.sessions() as db:
        rows = db.scalars(select(Outbox).where(Outbox.reminder_id == reminder_id).order_by(Outbox.generation)).all()
        assert [(row.generation, row.status) for row in rows] == [(1, "cancelled"), (2, "pending")]
        assert db.scalar(select(func.count()).select_from(Outbox).where(Outbox.reminder_id == second_id)) == 1
