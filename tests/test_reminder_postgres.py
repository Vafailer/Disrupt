import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from app.config import Settings
from app.main import create_app
from app.models import Item, Outbox, ProductEvent, Reminder, TelegramIdentity
from tests.conftest import register
from tests.test_internal import SERVICE_TOKEN
from tests.test_reminders import future


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="Local PostgreSQL is not configured")
def test_postgres_reminder_creation_claim_authorize_result_and_edit_races(monkeypatch):
    url = os.environ["TEST_POSTGRES_URL"]
    assert make_url(url).host in {"127.0.0.1", "localhost", "::1"}
    monkeypatch.setenv("NOTES_DATABASE_URL", url)
    monkeypatch.setenv("NOTES_PROVIDER", "mock")
    monkeypatch.setenv("NOTES_ALLOW_LIVE_REQUESTS", "false")
    command.upgrade(Config("alembic.ini"), "head")
    command.check(Config("alembic.ini"))
    app = create_app(Settings(database_url=url, auto_worker=False, internal_api_token=SERVICE_TOKEN))
    headers = {"Authorization": "Bearer " + SERVICE_TOKEN}
    bot_id = 10**12 + uuid.uuid4().int % 10**12
    try:
        with TestClient(app) as client:
            owner = register(client, "pg_reminder_" + uuid.uuid4().hex)
            note = client.post("/api/v1/captures/text", headers={"Idempotency-Key": "note"}, json={
                "text": "Synthetic reminder test", "processing_mode": "manual",
            }).json()
            note_id = note["note_id"]
            task = client.post("/api/v1/notes/" + note_id + "/items", json={
                "version": 1, "kind": "task", "text": "Synthetic task",
            }).json()["items"][0]
            with app.state.sessions() as db:
                db.add(TelegramIdentity(user_id=owner["id"], bot_id=bot_id, telegram_user_id=123, chat_id=123))
                db.commit()
            body = {"item_id": task["id"], "scheduled_at": future(), "timezone": "Europe/Moscow", "text": "Synthetic"}

            def create(_):
                return client.post("/api/v1/notes/" + note_id + "/reminders", json=body,
                                   headers={"Idempotency-Key": "create-reminder"})

            with ThreadPoolExecutor(max_workers=4) as pool:
                created = list(pool.map(create, range(4)))
            assert all(r.status_code == 201 for r in created), [r.text for r in created]
            assert len({r.json()["id"] for r in created}) == 1
            reminder_id = created[0].json()["id"]

            def enqueue():
                with app.state.sessions() as db:
                    reminder = db.get(Reminder, reminder_id)
                    reminder.scheduled_at = time.time() - 1
                    row = Outbox(user_id=owner["id"], reminder_id=reminder_id, bot_id=bot_id,
                                 chat_id=123, generation=reminder.generation)
                    db.add(row)
                    db.commit()
                    return row.id

            delivery_id = enqueue()

            def claim(_):
                return client.post("/internal/v1/deliveries/claim", headers=headers, json={"bot_id": bot_id, "limit": 10})

            with ThreadPoolExecutor(max_workers=4) as pool:
                claims = list(pool.map(claim, range(4)))
            assert all(r.status_code == 200 for r in claims), [r.text for r in claims]
            items = [item for response in claims for item in response.json()["items"]]
            assert len(items) == 1
            lease = {k: items[0][k] for k in ("lease_token", "generation")}
            path = "/internal/v1/deliveries/" + delivery_id

            def authorize(_):
                return client.post(path + "/authorize", headers=headers, json=lease)

            with ThreadPoolExecutor(max_workers=4) as pool:
                permissions = list(pool.map(authorize, range(4)))
            assert all(r.status_code == 200 for r in permissions)
            assert sum(r.json()["send"] for r in permissions) == 1
            final = {**lease, "status": "sent", "telegram_message_id": 100,
                     "error_code": None, "retry_after_seconds": None}

            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: client.post(path + "/result", headers=headers, json=final), range(4)))
            assert all(r.status_code == 200 for r in results), [r.text for r in results]
            with app.state.sessions() as db:
                assert db.scalar(select(func.count()).select_from(Reminder).where(Reminder.user_id == owner["id"])) == 1
                assert db.scalar(select(func.count()).select_from(ProductEvent).where(
                    ProductEvent.user_id == owner["id"], ProductEvent.name == "reminder_sent",
                )) == 1

            edit = {"scheduled_at": future(), "timezone": "Europe/Moscow", "text": "New plan", "generation": 1}
            assert client.patch("/api/v1/reminders/" + reminder_id, json=edit).status_code == 200
            next_delivery = enqueue()
            item = claim(0).json()["items"][0]
            next_lease = {k: item[k] for k in ("lease_token", "generation")}
            # Both operations take the account lock, so either edit wins or authorize wins.
            with ThreadPoolExecutor(max_workers=2) as pool:
                permission = pool.submit(client.post, "/internal/v1/deliveries/" + next_delivery + "/authorize",
                                         headers=headers, json=next_lease)
                edited = pool.submit(client.patch, "/api/v1/reminders/" + reminder_id,
                                     json={**edit, "generation": 2})
                permission, edited = permission.result(), edited.result()
            assert permission.status_code == edited.status_code == 200
            with app.state.sessions() as db:
                assert db.get(Reminder, reminder_id).generation == 3
                assert db.get(Reminder, reminder_id).status == "confirmed"
                assert db.get(Outbox, next_delivery).status == ("unknown" if permission.json()["send"] else "cancelled")
                assert db.get(Item, task["id"]).status == "open"
    finally:
        app.state.engine.dispose()
