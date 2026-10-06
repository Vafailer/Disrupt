from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select

from app.models import Job, Outbox, ProductEvent, Reminder
from app.reminder_time import resolve_reminder_time
from app.schemas import ReminderTimeRequest
from tests.conftest import register
from tests.test_internal import BASE
from tests.test_reminders import reminders as reminders

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def resolve(local_time, timezone, now=NOW):
    return resolve_reminder_time(ReminderTimeRequest(local_time=local_time, timezone=timezone), now=now)


@pytest.mark.parametrize("zone,local,utc", [
    ("Europe/Moscow", "2027-10-08T12:00", "2027-10-08T09:00:00+00:00"),
    ("Asia/Kathmandu", "2027-10-08T12:00:30", "2027-10-08T06:15:30+00:00"),
    ("UTC", "2027-10-08T12:00", "2027-10-08T12:00:00+00:00"),
])
def test_local_time_uses_selected_zone_and_keeps_seconds(zone, local, utc):
    result = resolve(local, zone)
    assert not result["ambiguous"] and len(result["choices"]) == 1
    assert result["choices"][0]["scheduled_at"] == utc
    assert result["choices"][0]["is_future"]


@pytest.mark.parametrize("zone,local,difference", [
    ("Europe/Berlin", "2027-10-31T02:30", 3600),
    ("Australia/Lord_Howe", "2027-04-04T01:45", 1800),
])
def test_ambiguous_time_returns_both_instants_without_choosing(zone, local, difference):
    result = resolve(local, zone)
    assert result["ambiguous"] and len(result["choices"]) == 2
    choices = result["choices"]
    dates = [datetime.fromisoformat(choice["scheduled_at"]) for choice in choices]
    assert (dates[1] - dates[0]).total_seconds() == difference
    assert choices[0]["utc_offset"] != choices[1]["utc_offset"]


@pytest.mark.parametrize("zone,local", [
    ("Europe/Berlin", "2027-03-28T02:30"),
    ("Australia/Lord_Howe", "2027-10-03T02:15"),
    ("Pacific/Apia", "2011-12-30T12:00"),
])
def test_clock_gap_is_rejected_instead_of_shifted(zone, local):
    with pytest.raises(HTTPException) as caught:
        resolve(local, zone)
    assert caught.value.status_code == 422 and "Такого местного времени нет" in caught.value.detail


def test_fold_with_one_past_occurrence_still_requires_a_choice():
    result = resolve("2027-10-31T02:30", "Europe/Berlin", datetime(2027, 10, 31, 1, tzinfo=UTC))
    assert result["ambiguous"]
    assert [choice["is_future"] for choice in result["choices"]] == [False, True]


@pytest.mark.parametrize("local", ["2020-01-01T12:00", "2027-02-30T12:00", "2027-10-08T25:00"])
def test_past_or_invalid_calendar_time_is_rejected(local):
    with pytest.raises(HTTPException) as caught:
        resolve(local, "UTC")
    assert caught.value.status_code == 422


@pytest.mark.parametrize("local,zone", [
    ("2027-10-08", "UTC"), ("2027-10-08T12:00Z", "UTC"),
    ("2027-10-08T12:00+03:00", "UTC"), ("2027-10-08T12:00", "../Europe/Moscow"),
    ("2027-10-08T12:00", "not/a/zone"),
])
def test_resolver_requires_wall_time_and_an_iana_zone(local, zone):
    with pytest.raises(ValidationError):
        ReminderTimeRequest(local_time=local, timezone=zone)


def test_time_preview_requires_session_csrf_origin_and_creates_nothing(client, app):
    path, body = "/api/v1/reminders/resolve-time", {"local_time": "2090-10-08T12:00", "timezone": "Europe/Moscow"}
    assert client.post(path, json=body).status_code == 401
    register(client)
    assert client.post(path, json=body, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post(path, json=body, headers={"Origin": "https://foreign.invalid"}).status_code == 403
    with app.state.sessions() as db:
        events = db.scalar(select(func.count()).select_from(ProductEvent))
    response = client.post(path, json=body)
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    assert response.json()["choices"][0]["scheduled_at"] == "2090-10-08T09:00:00+00:00"
    with app.state.sessions() as db:
        for model in (Reminder, Outbox, Job):
            assert db.scalar(select(func.count()).select_from(model)) == 0
        assert db.scalar(select(func.count()).select_from(ProductEvent)) == events


def test_preview_choices_can_be_confirmed_without_browser_timezone(reminders):
    app, client, ids = reminders
    preview = client.post("/api/v1/reminders/resolve-time", json={
        "local_time": "2090-10-08T12:00", "timezone": "Europe/Moscow",
    }).json()
    response = client.post("/api/v1/notes/" + ids["note"] + "/reminders", headers={"Idempotency-Key": "preview"}, json={
        "scheduled_at": preview["choices"][0]["scheduled_at"], "timezone": preview["timezone"],
        "text": "Вернуться к задаче", "item_id": ids["item"],
    })
    assert response.status_code == 201
    assert response.json()["scheduled_at"] == "2090-10-08T09:00:00+00:00"
    link = client.get("/api/v1/telegram/links").json()["identities"][0]
    assert link == {"bot_id": BASE["bot_id"], "telegram_user_id": BASE["telegram_user_id"],
                    "notifications_enabled": True, "delivery_status": "available"}
