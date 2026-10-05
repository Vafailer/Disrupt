import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.integration import IntegrationRejection
from app.models import Capture, Inbox, Item, Note, Outbox, ProductEvent, Reminder, Revision, TelegramIdentity
from app.security import hash_token
from tests.conftest import register
from tests.test_internal import BASE, HEADERS, SERVICE_TOKEN, linked, message

TOKEN = "synthetic-callback-" + "x" * 32


@pytest.fixture
def task(app_factory):
    app = app_factory(internal_api_token=SERVICE_TOKEN)
    with TestClient(app) as client:
        owner = linked(client)
        assert message(client, mode="manual").status_code == 200
        with app.state.sessions() as db:
            note = db.scalar(select(Note))
            item = Item(user_id=owner["id"], note_id=note.id, kind="task", text="Позвонить")
            db.add(item)
            db.flush()
            reminder = Reminder(
                user_id=owner["id"], note_id=note.id, item_id=item.id, scheduled_at=time.time(),
                timezone="Europe/Moscow", text="Позвонить",
            )
            db.add(reminder)
            db.flush()
            delivery = Outbox(
                user_id=owner["id"], reminder_id=reminder.id, bot_id=BASE["bot_id"],
                chat_id=BASE["chat_id"], generation=1, status="sent", authorized_at=time.time(),
                callback_token_hash=hash_token(TOKEN),
            )
            db.add(delivery)
            db.commit()
            ids = {"note": note.id, "item": item.id, "reminder": reminder.id, "delivery": delivery.id}
        yield app, client, ids


def action(client, update_id=3, token=TOKEN, **extra):
    return client.post(
        "/internal/v1/telegram/actions", headers=HEADERS,
        json={
            "bot_id": BASE["bot_id"], "telegram_user_id": BASE["telegram_user_id"],
            "update_id": update_id, "callback_token": token, **extra,
        },
    )


def test_action_commits_revision_event_and_cancellation_once(task):
    app, client, ids = task
    response = action(client)
    assert response.status_code == 200 and response.json() == {"status": "completed"}
    assert action(client).json() == response.json()
    assert action(client, 4).json() == {"status": "already_completed"}
    with app.state.sessions() as db:
        assert db.get(Item, ids["item"]).status == "completed"
        assert db.get(Item, ids["item"]).version == 2
        assert db.get(Note, ids["note"]).version == 2
        reminder = db.get(Reminder, ids["reminder"])
        assert reminder.status == "cancelled" and reminder.generation == 2
        revision = db.scalar(select(Revision).where(Revision.note_id == ids["note"], Revision.version == 2))
        assert revision.snapshot["items"][0]["status"] == "completed"
        events = db.scalars(select(ProductEvent).where(ProductEvent.name == "task_completed")).all()
        assert len(events) == 1 and events[0].channel == "telegram"
    conflict = action(client, token="different")
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "conflict"
    assert action(client, 2).status_code == 409  # Same key is already used by text.


def test_concurrent_actions_do_not_advance_task_more_than_once(task):
    app, client, ids = task
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda update_id: action(client, update_id), range(3, 7)))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    assert sum(r.json()["status"] == "completed" for r in responses) == 1
    with app.state.sessions() as db:
        assert db.get(Note, ids["note"]).version == 2
        assert db.get(Item, ids["item"]).version == 2


@pytest.mark.parametrize("change", ["generation", "cancelled", "pending", "non_task", "wrong_note"])
def test_expired_or_unissued_actions_do_not_change_task(task, change):
    app, client, ids = task
    with app.state.sessions() as db:
        reminder, delivery = db.get(Reminder, ids["reminder"]), db.get(Outbox, ids["delivery"])
        if change == "generation":
            reminder.generation += 1
        elif change == "cancelled":
            reminder.status = "cancelled"
        elif change == "pending":
            delivery.status, delivery.authorized_at = "pending", None
        elif change == "non_task":
            db.get(Item, ids["item"]).kind = "idea"
        else:
            reminder.item_id = None
        db.commit()
    response = action(client)
    assert response.status_code == 409 and response.json()["error"]["code"] == "action_expired"
    assert action(client).json() == response.json()
    with app.state.sessions() as db:
        assert db.get(Item, ids["item"]).status == "open"
        assert db.get(Note, ids["note"]).version == 1


def test_action_owner_and_bot_are_checked(task):
    app, client, ids = task
    with TestClient(app) as other:
        user = register(other, "another")
        with app.state.sessions() as db:
            db.add(TelegramIdentity(
                user_id=user["id"], bot_id=BASE["bot_id"], telegram_user_id=17, chat_id=17,
            ))
            db.commit()
        response = action(other, telegram_user_id=17)
        assert response.status_code == 403 and response.json()["error"]["code"] == "action_forbidden"
    with app.state.sessions() as db:
        assert db.get(Item, ids["item"]).status == "open"
    response = action(client, update_id=4, bot_id=17)
    assert response.status_code == 403 and response.json()["error"]["code"] == "telegram_not_linked"


@pytest.mark.parametrize("token", ["x", "", "x" * 65, "я" * 33])
def test_bad_callback_is_business_refusal(task, token):
    _, client, _ = task
    response = action(client, token=token)
    assert response.status_code in {400, 409}
    assert response.json()["error"]["code"] == "action_expired"


def test_failed_action_rolls_back_all_changes_and_can_retry(task, monkeypatch):
    from app.routes import internal

    app, client, ids = task
    original = internal.save_revision

    def fail(*args):
        raise RuntimeError("private text must not escape")

    monkeypatch.setattr(internal, "save_revision", fail)
    assert action(client).status_code == 503
    with app.state.sessions() as db:
        assert db.get(Item, ids["item"]).status == "open"
        assert db.get(Note, ids["note"]).version == 1
        assert db.get(Reminder, ids["reminder"]).status == "confirmed"
        assert db.scalar(select(func.count()).select_from(Inbox)) == 2
    monkeypatch.setattr(internal, "save_revision", original)
    assert action(client).json() == {"status": "completed"}


def test_business_refusal_rolls_back_partial_capture(task, monkeypatch):
    from app.routes import internal

    app, client, _ = task
    original = internal.capture_text

    def refuse(*args, **kwargs):
        original(*args, **kwargs)
        raise IntegrationRejection(429, "quota_exceeded", "Лимит обработки исчерпан")

    monkeypatch.setattr(internal, "capture_text", refuse)
    response = message(client, update_id=3)
    assert response.status_code == 429 and response.json()["error"]["code"] == "quota_exceeded"
    assert message(client, update_id=3).json() == response.json()
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Capture)) == 1
        assert db.scalar(select(func.count()).select_from(Inbox)) == 3
