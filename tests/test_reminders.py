import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import Item, Note, Outbox, ProductEvent, Reminder, TelegramIdentity
from app.security import hash_token
from tests.conftest import register
from tests.test_internal import BASE, HEADERS, SERVICE_TOKEN, linked, message


def future():
    return datetime.fromtimestamp(time.time() + 3600, UTC).isoformat()


def payload(item_id=None):
    return {"item_id": item_id, "scheduled_at": future(), "timezone": "Europe/Moscow", "text": "Позвонить"}


@pytest.fixture
def reminders(app_factory):
    app = app_factory(internal_api_token=SERVICE_TOKEN)
    with TestClient(app) as client:
        owner = linked(client)
        assert message(client, mode="manual").status_code == 200
        with app.state.sessions() as db:
            note = db.scalar(select(Note).where(Note.user_id == owner["id"]))
            item = Item(user_id=owner["id"], note_id=note.id, kind="task", text="Позвонить")
            db.add(item)
            db.commit()
            ids = {"user": owner["id"], "note": note.id, "item": item.id}
        yield app, client, ids


def add(client, ids, body=None, key="reminder-create"):
    return client.post(
        "/api/v1/notes/" + ids["note"] + "/reminders",
        json=payload(ids["item"]) if body is None else body, headers={"Idempotency-Key": key},
    )


def due_delivery(app, client, ids, *, item=True):
    body = payload(ids["item"] if item else None)
    created = add(client, ids, body)
    assert created.status_code == 201, created.text
    reminder_id = created.json()["id"]
    with app.state.sessions() as db:
        db.get(Reminder, reminder_id).scheduled_at = time.time() - 1
        # The future scheduler owns enqueueing. Simulate its one durable Outbox here.
        row = Outbox(
            user_id=ids["user"], reminder_id=reminder_id, bot_id=BASE["bot_id"],
            chat_id=BASE["chat_id"], generation=1,
        )
        db.add(row)
        db.commit()
        return {**ids, "reminder": reminder_id, "delivery": row.id}


def claim(client, **extra):
    return client.post("/internal/v1/deliveries/claim", headers=HEADERS, json={"bot_id": BASE["bot_id"], "limit": 10, **extra})


def auth(client, item, **extra):
    return client.post(
        "/internal/v1/deliveries/" + item["delivery_id"] + "/authorize", headers=HEADERS,
        json={"lease_token": item["lease_token"], "generation": item["generation"], **extra},
    )


def result(client, item, status="sent", **extra):
    return client.post(
        "/internal/v1/deliveries/" + item["delivery_id"] + "/result", headers=HEADERS,
        json={
            "lease_token": item["lease_token"], "generation": item["generation"], "status": status,
            "telegram_message_id": 123 if status == "sent" else None,
            "error_code": None if status == "sent" else "send_failed",
            "retry_after_seconds": 60 if status == "retryable" else None, **extra,
        },
    )


def test_create_repeat_edit_and_cancel_use_generations(reminders):
    app, client, ids = reminders
    body = payload(ids["item"])
    first = add(client, ids, body)
    assert first.status_code == 201, first.text
    assert add(client, ids, body).json() == first.json()
    assert add(client, ids, {**body, "text": "Другой смысл"}).status_code == 409
    reminder_id = first.json()["id"]
    path = "/api/v1/reminders/" + reminder_id
    changed = client.patch(path, json={**payload(), "generation": 1, "text": "Позвонить позже"})
    # item_id is immutable and not an edit field.
    assert changed.status_code == 422
    edit = {k: v for k, v in payload().items() if k != "item_id"}
    changed = client.patch(path, json={**edit, "generation": 1, "text": "Позвонить позже"})
    assert changed.status_code == 200 and changed.json()["generation"] == 2
    assert client.patch(path, json={**edit, "generation": 1}).status_code == 409
    assert add(client, ids, body).json()["text"] == "Позвонить позже"  # A replay never undoes edits.
    cancelled = client.post(path + "/cancel", json={"generation": 2})
    assert cancelled.json()["status"] == "cancelled" and cancelled.json()["generation"] == 3
    assert client.post(path + "/cancel", json={"generation": 2}).json() == cancelled.json()
    assert client.get("/api/v1/notes/" + ids["note"] + "/reminders").json() == [cancelled.json()]
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Reminder)) == 1
        assert db.scalar(select(func.count()).select_from(ProductEvent).where(ProductEvent.name == "reminder_confirmed")) == 2


@pytest.mark.parametrize("change", [
    {"scheduled_at": "2027-10-06T12:00:00"}, {"scheduled_at": "2027-10-06"},
    {"scheduled_at": "2020-10-06T12:00:00Z"}, {"timezone": "not/a/timezone"},
    {"timezone": "../Europe/Moscow"}, {"text": "  "}, {"text": "a\x00b"},
    {"text": "x" * 1001}, {"user_id": "foreign"},
])
def test_invalid_confirmation_creates_no_reminder(reminders, change):
    app, client, ids = reminders
    assert add(client, ids, {**payload(ids["item"]), **change}).status_code == 422
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Reminder)) == 0


@pytest.mark.parametrize("change", ["completed", "idea", "wrong_note"])
def test_reminder_target_must_be_an_owned_open_task(reminders, change):
    app, client, ids = reminders
    with app.state.sessions() as db:
        item = db.get(Item, ids["item"])
        if change == "completed":
            item.status = "completed"
        elif change == "idea":
            item.kind = "idea"
        else:
            item.note_id = "missing"
            # Test the request with an unknown ID instead of violating a foreign key.
            db.rollback()
        if change != "wrong_note":
            db.commit()
    body = payload("missing" if change == "wrong_note" else ids["item"])
    assert add(client, ids, body).status_code == (404 if change == "wrong_note" else 409)


def test_public_reminder_auth_csrf_origin_and_ownership(reminders):
    app, client, ids = reminders
    created = add(client, ids).json()
    path = "/api/v1/reminders/" + created["id"]
    assert client.post(path + "/cancel", json={"generation": 1}, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post(path + "/cancel", json={"generation": 1}, headers={"Origin": "https://foreign.invalid"}).status_code == 403
    with TestClient(app) as other:
        assert other.get(path).status_code == 401
        register(other, "another")
        assert other.get(path).status_code == 404
        assert other.post(path + "/cancel", json={"generation": 1}).status_code == 404
        assert add(other, ids).status_code == 404
        assert other.get("/api/v1/notes/" + ids["note"] + "/reminders").status_code == 404


def test_claim_authorize_result_and_callback_are_one_safe_flow(reminders):
    app, client, ids = reminders
    ids = due_delivery(app, client, ids)
    response = claim(client)
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["delivery_id"] == ids["delivery"] and item["text"] == "Позвонить"
    assert item["note_url"].startswith(app.state.settings.public_origin + "/?capture=")
    assert claim(client).json() == {"items": []}
    with app.state.sessions() as db:
        row = db.get(Outbox, ids["delivery"])
        assert row.lease_token_hash == hash_token(item["lease_token"])
        assert row.callback_token_hash == hash_token(item["callback_token"])
    assert auth(client, item).json() == {"send": True}
    assert auth(client, item).json() == {"send": False}
    assert result(client, item).json() == {"status": "recorded"}
    assert result(client, item).json() == {"status": "recorded"}
    assert result(client, item, "unknown").status_code == 409
    assert claim(client).json() == {"items": []}
    clicked = client.post("/internal/v1/telegram/actions", headers=HEADERS, json={
        "bot_id": BASE["bot_id"], "telegram_user_id": BASE["telegram_user_id"],
        "update_id": 3, "callback_token": item["callback_token"],
    })
    assert clicked.json() == {"status": "completed"}
    with app.state.sessions() as db:
        assert db.get(Item, ids["item"]).status == "completed"
        assert db.get(Reminder, ids["reminder"]).status == "cancelled"
        assert db.get(Outbox, ids["delivery"]).status == "sent"
        assert db.scalar(select(func.count()).select_from(ProductEvent).where(ProductEvent.name == "reminder_sent")) == 1


def test_internal_auth_and_bot_scope(reminders):
    app, client, ids = reminders
    due_delivery(app, client, ids)
    assert client.post("/internal/v1/deliveries/claim", json={"bot_id": BASE["bot_id"], "limit": 10}).status_code == 401
    assert claim(client, bot_id=10).json() == {"items": []}
    assert len(claim(client).json()["items"]) == 1


def test_note_reminder_has_no_completion_button(reminders):
    app, client, ids = reminders
    due_delivery(app, client, ids, item=False)
    assert claim(client).json()["items"][0]["callback_token"] is None


@pytest.mark.parametrize("change", ["notifications", "blocked", "chat", "generation", "completed", "future"])
def test_claim_filters_ineligible_recipient_or_target(reminders, change):
    app, client, ids = reminders
    ids = due_delivery(app, client, ids)
    with app.state.sessions() as db:
        identity = db.scalar(select(TelegramIdentity))
        if change == "notifications":
            identity.notifications_enabled = False
        elif change == "blocked":
            identity.delivery_status = "blocked"
        elif change == "chat":
            identity.chat_id += 1
        elif change == "generation":
            db.get(Reminder, ids["reminder"]).generation += 1
        elif change == "completed":
            db.get(Item, ids["item"]).status = "completed"
        else:
            db.get(Reminder, ids["reminder"]).scheduled_at = time.time() + 100
        db.commit()
    assert claim(client).json() == {"items": []}


def test_expired_unapproved_lease_can_be_reclaimed_but_old_token_cannot_send(reminders):
    app, client, ids = reminders
    ids = due_delivery(app, client, ids)
    old = claim(client).json()["items"][0]
    with app.state.sessions() as db:
        db.get(Outbox, ids["delivery"]).lease_until = time.time() - 1
        db.commit()
    fresh = claim(client).json()["items"][0]
    assert fresh["lease_token"] != old["lease_token"] and fresh["callback_token"] != old["callback_token"]
    assert auth(client, old).json() == {"send": False}
    assert auth(client, fresh).json() == {"send": True}


def test_expired_authorized_attempt_becomes_unknown_without_retry_and_accepts_late_result(reminders):
    app, client, ids = reminders
    ids = due_delivery(app, client, ids)
    item = claim(client).json()["items"][0]
    assert auth(client, item).json() == {"send": True}
    with app.state.sessions() as db:
        db.get(Outbox, ids["delivery"]).lease_until = time.time() - 1
        db.commit()
    assert claim(client).json() == {"items": []}
    with app.state.sessions() as db:
        assert db.get(Outbox, ids["delivery"]).status == "unknown"
        assert db.get(Reminder, ids["reminder"]).status == "unknown"
    assert auth(client, item).json() == {"send": False}
    assert result(client, item).json() == {"status": "recorded"}
    assert client.get("/api/v1/reminders/" + ids["reminder"]).json()["status"] == "sent"


@pytest.mark.parametrize("authorized", [False, True])
def test_edit_invalidates_old_delivery_and_late_result_does_not_change_new_plan(reminders, authorized):
    app, client, ids = reminders
    ids = due_delivery(app, client, ids)
    item = claim(client).json()["items"][0]
    if authorized:
        assert auth(client, item).json() == {"send": True}
    edit = {k: v for k, v in payload().items() if k != "item_id"}
    response = client.patch("/api/v1/reminders/" + ids["reminder"], json={**edit, "generation": 1})
    assert response.status_code == 200 and response.json()["generation"] == 2
    assert auth(client, item).json() == {"send": False}
    if authorized:
        assert result(client, item).status_code == 200
    else:
        assert result(client, item).status_code == 409
    with app.state.sessions() as db:
        reminder = db.get(Reminder, ids["reminder"])
        assert reminder.status == "confirmed" and reminder.generation == 2


@pytest.mark.parametrize("status", ["retryable", "blocked", "unknown"])
def test_results_control_retry_and_block_recipient(reminders, status):
    app, client, ids = reminders
    ids = due_delivery(app, client, ids)
    item = claim(client).json()["items"][0]
    assert result(client, item, status).status_code == 409  # No permission yet.
    assert auth(client, item).json() == {"send": True}
    assert result(client, item, status).status_code == 200
    assert claim(client).json() == {"items": []}
    with app.state.sessions() as db:
        row = db.get(Outbox, ids["delivery"])
        if status == "retryable":
            row.retry_at = time.time() - 1
        if status == "blocked":
            assert db.scalar(select(TelegramIdentity)).delivery_status == "blocked"
        db.commit()
    again = claim(client).json()["items"]
    assert bool(again) == (status == "retryable")
    if again:
        assert again[0]["lease_token"] != item["lease_token"]
        assert result(client, item, status).status_code == 409
        assert auth(client, again[0]).json() == {"send": True}


def test_concurrent_claims_and_authorizations_allow_only_one_sender(reminders):
    app, client, ids = reminders
    due_delivery(app, client, ids)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: claim(client), range(4)))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    items = [item for response in responses for item in response.json()["items"]]
    assert len(items) == 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        permissions = list(pool.map(lambda _: auth(client, items[0]), range(4)))
    assert sum(r.json()["send"] for r in permissions) == 1


def test_failed_commit_rolls_back_permission_and_allows_safe_retry(reminders, monkeypatch):
    from sqlalchemy.orm import Session

    app, client, ids = reminders
    due_delivery(app, client, ids)
    item = claim(client).json()["items"][0]
    commit = Session.commit

    def fail(_):
        raise RuntimeError("private data must not appear in the response")

    monkeypatch.setattr(Session, "commit", fail)
    response = auth(client, item)
    assert response.status_code == 503 and "private data" not in response.text
    monkeypatch.setattr(Session, "commit", commit)
    assert auth(client, item).json() == {"send": True}


@pytest.mark.parametrize("authorized", [False, True])
def test_cancel_prevents_send_or_preserves_unknown_attempt(reminders, authorized):
    app, client, ids = reminders
    ids = due_delivery(app, client, ids)
    item = claim(client).json()["items"][0]
    if authorized:
        assert auth(client, item).json() == {"send": True}
    cancelled = client.post("/api/v1/reminders/" + ids["reminder"] + "/cancel", json={"generation": 1})
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["previous_attempt_unknown"] is authorized
    assert auth(client, item).json() == {"send": False}
    with app.state.sessions() as db:
        assert db.get(Outbox, ids["delivery"]).status == ("unknown" if authorized else "cancelled")
    assert result(client, item).status_code == (200 if authorized else 409)
    assert client.get("/api/v1/reminders/" + ids["reminder"]).json()["status"] == "cancelled"
    assert claim(client).json() == {"items": []}
    clicked = client.post("/internal/v1/telegram/actions", headers=HEADERS, json={
        "bot_id": BASE["bot_id"], "telegram_user_id": BASE["telegram_user_id"],
        "update_id": 3, "callback_token": item["callback_token"],
    })
    assert clicked.status_code == 409
    with app.state.sessions() as db:
        assert db.get(Item, ids["item"]).status == "open"


@pytest.mark.parametrize("change", [
    {"telegram_message_id": None}, {"error_code": "send_failed"}, {"retry_after_seconds": 30},
    {"status": "retryable", "telegram_message_id": None, "retry_after_seconds": None},
    {"status": "unknown", "telegram_message_id": 123},
    {"status": "unknown", "telegram_message_id": None, "error_code": "private text\nnot a code"},
])
def test_inconsistent_or_sensitive_results_are_rejected(reminders, change):
    app, client, ids = reminders
    due_delivery(app, client, ids)
    item = claim(client).json()["items"][0]
    assert auth(client, item).json() == {"send": True}
    response = result(client, item, **change)
    assert response.status_code == 422 and "private text" not in response.text


def test_wrong_lease_and_generation_fail_closed(reminders):
    app, client, ids = reminders
    due_delivery(app, client, ids)
    item = claim(client).json()["items"][0]
    assert auth(client, item, lease_token="different" * 5).json() == {"send": False}
    assert auth(client, item, generation=2).json() == {"send": False}
    assert result(client, item, lease_token="different" * 5).status_code == 409
    assert auth(client, item).json() == {"send": True}


def test_result_commit_failure_is_retryable_without_a_second_authorization(reminders, monkeypatch):
    from sqlalchemy.orm import Session

    app, client, ids = reminders
    ids = due_delivery(app, client, ids)
    item = claim(client).json()["items"][0]
    assert auth(client, item).json() == {"send": True}
    original = Session.commit

    def fail(_):
        raise RuntimeError("synthetic commit failure")

    monkeypatch.setattr(Session, "commit", fail)
    assert result(client, item).status_code == 503
    monkeypatch.setattr(Session, "commit", original)
    assert auth(client, item).json() == {"send": False}
    assert result(client, item).json() == {"status": "recorded"}
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ProductEvent).where(ProductEvent.name == "reminder_sent")) == 1


def test_unknown_authorization_commit_never_grants_a_second_send(reminders, monkeypatch):
    from sqlalchemy.orm import Session

    app, client, ids = reminders
    due_delivery(app, client, ids)
    item = claim(client).json()["items"][0]
    original = Session.commit

    def lost_response(session):
        original(session)
        raise RuntimeError("synthetic failure after commit")

    monkeypatch.setattr(Session, "commit", lost_response)
    assert auth(client, item).status_code == 503
    monkeypatch.setattr(Session, "commit", original)
    assert auth(client, item).json() == {"send": False}
    assert claim(client).json() == {"items": []}


def test_creation_commit_with_lost_response_replays_without_duplicate(reminders, monkeypatch):
    from sqlalchemy.orm import Session

    app, client, ids = reminders
    body = payload(ids["item"])
    original = Session.commit

    def lost_response(session):
        original(session)
        raise RuntimeError("synthetic failure after commit")

    monkeypatch.setattr(Session, "commit", lost_response)
    assert add(client, ids, body).status_code == 500
    monkeypatch.setattr(Session, "commit", original)
    assert add(client, ids, body).status_code == 201
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Reminder)) == 1
        assert db.scalar(select(func.count()).select_from(ProductEvent).where(ProductEvent.name == "reminder_confirmed")) == 1
