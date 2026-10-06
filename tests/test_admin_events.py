"""Reminder opening is an owned, explicit action, never a polling side effect."""

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import ProductEvent, new_id
from tests.conftest import register
from tests.test_reminders import auth, claim, due_delivery, result
from tests.test_reminders import reminders as reminders


def test_reminder_link_opened_event_checks_delivery_owner_csrf_and_replay(reminders):
    app, client, ids = reminders
    ids = due_delivery(app, client, ids)
    item = claim(client).json()["items"][0]
    assert "&reminder=" + ids["reminder"] in item["note_url"]
    path = "/api/v1/notes/" + ids["note"]
    body = {"operation_id": new_id(), "reminder_id": ids["reminder"]}
    assert client.post(path + "/opened", json=body).status_code == 404  # Not sent/unknown yet.
    assert auth(client, item).json() == {"send": True}
    assert result(client, item).status_code == 200
    assert client.get(path).status_code == 200
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ProductEvent).where(ProductEvent.name == "reminder_opened")) == 0
    assert client.post(path + "/opened", json=body, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post(path + "/opened", json=body, headers={"Origin": "https://foreign.invalid"}).status_code == 403
    assert client.post(path + "/opened", json={**body, "reminder_id": new_id()}).status_code == 404
    with TestClient(app) as other:
        register(other, "other")
        assert other.post(path + "/opened", json=body).status_code == 404
    for _ in range(2):
        assert client.post(path + "/opened", json=body).status_code == 204
    with app.state.sessions() as db:
        rows = list(db.scalars(select(ProductEvent).where(ProductEvent.name == "reminder_opened")))
        assert len(rows) == 1 and rows[0].subject_id == ids["note"] and rows[0].user_id == ids["user"]
        snapshots = list(db.scalars(select(ProductEvent).where(ProductEvent.name == "reminder_result")))
        assert len(snapshots) == 1 and snapshots[0].outcome == "sent"
