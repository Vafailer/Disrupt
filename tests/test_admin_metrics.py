"""Controlled acceptance data, without provider traffic or personal content."""

import csv
import io
import json
import os
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.engine import make_url

from app.admin_metrics import aggregate, safe_cell
from app.analytics import record_event
from app.config import Settings
from app.main import create_app
from app.models import Capture, Job, Note, ProductEvent, ProviderUsage, User, new_id
from tests.conftest import register

KEY = b"synthetic-admin-pseudonym-key-12345"


def stamp(value):
    return datetime.fromisoformat(value + "+03:00").timestamp()


NOW = stamp("2026-10-07T12:00:00")
FILTER = {"from": "2026-10-05", "to": "2026-10-06"}


def admin(client, app):
    account = register(client, "admin_" + uuid.uuid4().hex)
    with app.state.sessions() as db:
        user = db.get(User, account["id"])
        user.role, user.is_test = "admin", True
        db.commit()
    return account


def seed(sessions, *, source=None):
    with sessions() as db:
        def user(name, first, source_name="paid", is_test=False):
            row = User(id=new_id(), username=name + uuid.uuid4().hex, password_hash="synthetic-never-used",
                       created_at=stamp(first), first_login_at=stamp(first), source=source or source_name, is_test=is_test)
            db.add(row)
            db.flush()
            record_event(db, row.id, "registered", row.id, occurred_at=row.created_at)
            record_event(db, row.id, "login", new_id(), occurred_at=row.first_login_at)
            return row

        ai = user("ai_", "2026-10-05T09:00:00")
        manual = user("manual_", "2026-10-06T11:00:00", "organic")
        pending = user("pending_", "2026-10-07T11:00:00", "organic")
        late_test = user("late_test_", "2026-10-05T09:00:00")
        late_test.is_test = True  # Its earlier non-test events must disappear too.
        was_test = user("was_test_", "2026-10-05T09:00:00", is_test=True)
        was_test.is_test = False  # Historical test snapshots must remain excluded.

        def note(owner, mode, channel, at):
            capture = Capture(id=new_id(), user_id=owner.id, original_text="SYNTHETIC PRIVATE TEXT", idempotency_key=new_id(),
                              processing_mode=mode, channel=channel, created_at=stamp(at))
            db.add(capture)
            db.flush()
            row = Note(id=new_id(), capture_id=capture.id, user_id=owner.id, title="SYNTHETIC PRIVATE TITLE",
                       markdown="PRIVATE CONTENT", conclusions=[], provider="mock", created_at=stamp(at), updated_at=stamp(at))
            db.add(row)
            db.flush()
            record_event(db, owner.id, "capture_saved", capture.id, channel, occurred_at=stamp(at))
            # The operation replay is ignored, even when arriving with another timestamp.
            record_event(db, owner.id, "capture_saved", capture.id, channel, occurred_at=stamp(at) + 1)
            return row

        ai_note = note(ai, "ai", "telegram", "2026-10-05T09:02:00")
        manual_note = note(manual, "manual", "web", "2026-10-06T11:01:00")
        record_event(db, ai.id, "note_opened", new_id(), occurred_at=stamp("2026-10-05T09:03:00"), subject_id=ai_note.id)
        record_event(db, ai.id, "structure_confirmed", ai_note.id + ":1", occurred_at=stamp("2026-10-05T09:04:00"), subject_id=ai_note.id)
        record_event(db, ai.id, "note_opened", new_id(), occurred_at=stamp("2026-10-06T09:00:00"), subject_id=ai_note.id)
        record_event(db, ai.id, "search_result_opened", new_id(), occurred_at=stamp("2026-10-06T09:00:01"), subject_id=ai_note.id)
        record_event(db, manual.id, "note_opened", new_id(), occurred_at=stamp("2026-10-06T11:02:00"), subject_id=manual_note.id)
        record_event(db, manual.id, "search_result_opened", new_id(), occurred_at=stamp("2026-10-06T11:03:00"), subject_id=manual_note.id)
        job = Job(id=new_id(), user_id=ai.id, capture_id=ai_note.capture_id, provider="mock", status="succeeded",
                  created_at=stamp("2026-10-05T09:02:00"), finished_at=stamp("2026-10-05T09:02:01"))
        db.add(job)
        record_event(db, ai.id, "processing_completed", job.id, "telegram", occurred_at=job.finished_at)
        session = db.scalar(select(ProductEvent.session_id).where(ProductEvent.user_id == ai.id, ProductEvent.name == "capture_saved"))
        for idx, channel, at, cost in [(1, "telegram", "2026-10-05T09:02:00", Decimal(".25")),
                                       (2, "web", "2026-10-06T09:02:00", None)]:
            db.add(ProviderUsage(id=new_id(), user_id=ai.id, operation_id=job.id if idx == 1 else new_id(),
                                 request_id=new_id(), session_id=session if idx == 1 else new_id(), channel=channel,
                                 kind="llm", model="mock", tariff_version="fixture-only-v1" if cost is not None else None,
                                 status="succeeded" if cost is not None else "failed", input_tokens=100 if cost is not None else None,
                                 output_tokens=50 if cost is not None else None, cache_tokens=0 if cost is not None else None,
                                 cost=cost, latency_ms=100 if cost is not None else 200, occurred_at=stamp(at)))
        record_event(db, ai.id, "processing_failed", new_id(), occurred_at=stamp("2026-10-06T09:02:01"))
        db.commit()
        return {"ai": ai.id, "manual": manual.id, "pending": pending.id, "note": ai_note.id, "job": job.id,
                "private": [ai.username, ai_note.title, ai_note.markdown, "SYNTHETIC PRIVATE TEXT"]}


@pytest.fixture
def populated(app, client, monkeypatch):
    admin(client, app)
    ids = seed(app.state.sessions)
    original = aggregate
    monkeypatch.setattr("app.routes.admin.aggregate", lambda *args, **kwargs: original(*args, **kwargs, now=NOW))
    return ids


def test_controlled_aggregate_with_two_channels_duplicate_test_and_pending(app, client, populated):
    response = client.get("/api/admin/summary", params=FILTER)
    assert response.status_code == 200, response.text
    result = response.json()
    reference = json.loads(Path("docs/fixtures/admin-summary.json").read_text())
    for section in ("cards", "daily", "funnel", "retention", "quality", "generated_at"):
        assert result[section] == reference[section]
    def content_free_values(rows):
        return [{name: value for name, value in row.items() if name != "id" and "pseudonym" not in name} for row in rows]
    assert content_free_values(result["usage"]) == content_free_values(reference["usage"])
    cards = result["cards"]
    assert cards["registrations"] == cards["unique_users"] == cards["new_users"] == cards["dau"] == 2
    assert cards["returning_users"] == 0
    assert cards["ai_activation"] == cards["manual_activation"] == {"numerator": 1, "denominator": 2, "value": 50.0}
    assert cards["activation_pending"] == 0
    assert cards["completed_scenario"] == 2 and cards["returns"] == 1
    assert [row["dau"] for row in result["daily"]] == [1, 2]
    assert [row["users"] for row in result["funnel"]] == [2, 2, 2, 1, 1]
    assert result["retention"] == {"d1": {"numerator": 1, "denominator": 1, "value": 100.0},
                                    "d7": {"numerator": 0, "denominator": 0, "value": None},
                                    "pending_d1": 1, "pending_d7": 2}
    assert result["quality"]["ai_success_rate"]["value"] == 50.0
    costs = result["quality"]["costs"]
    assert costs["dau_sum"] == costs["session_count"] == 3
    assert costs["unknown_usage_calls"] == 1 and costs["llm_calls"] == 2
    assert costs["llm_cost"] is None and costs["known_llm_cost"] == "0.25000000"
    assert costs["input_tokens"] is None and costs["llm_cost_per_dau"] is None
    extended = client.get("/api/admin/summary", params={**FILTER, "to": "2026-10-07"}).json()
    assert extended["cards"]["activation_pending"] == 1
    assert extended["cards"]["ai_activation"]["denominator"] == 2
    assert extended["daily"][-1]["dau"] == 0
    assert extended["cards"]["dau"] == 2  # Last completed day, rather than today's zero.
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ProductEvent).where(ProductEvent.name == "capture_saved")) == 2


def test_source_channel_and_usage_pagination_share_slice_and_preserve_pseudonyms(client, populated):
    all_result = client.get("/api/admin/summary", params=FILTER).json()
    web = client.get("/api/admin/summary", params={**FILTER, "channel": "web"}).json()
    telegram = client.get("/api/admin/summary", params={**FILTER, "channel": "telegram"}).json()
    assert all_result["cards"]["unique_users"] == 2
    assert web["cards"]["unique_users"] + telegram["cards"]["unique_users"] == 3
    assert web["cards"]["ai_activation"]["numerator"] == telegram["cards"]["ai_activation"]["numerator"] == 0
    assert telegram["cards"]["manual_activation"]["denominator"] == 1
    assert telegram["quality"]["costs"]["llm_cost"] == "0.25000000"
    assert telegram["quality"]["costs"]["llm_cost_per_dau"] == "0.25000000"
    organic = client.get("/api/admin/summary", params={**FILTER, "source": "organic"}).json()
    assert organic["cards"]["manual_activation"]["value"] == 100.0
    assert organic["cards"]["ai_activation"]["denominator"] == 1 and organic["usage"] == []
    first = client.get("/api/admin/summary", params={**FILTER, "usage_limit": 1})
    second = client.get("/api/admin/summary", params={**FILTER, "usage_limit": 1, "usage_offset": 1})
    assert first.headers["X-Next-Usage-Offset"] == "1"
    assert "X-Next-Usage-Offset" not in second.headers
    a, b = first.json(), second.json()
    assert a["usage"][0]["user_pseudonym"] == b["usage"][0]["user_pseudonym"]
    assert a["usage"][0]["operation_pseudonym"] != b["usage"][0]["operation_pseudonym"]
    for name in ("cards", "daily", "funnel", "retention", "quality"):
        assert a[name] == b[name] == all_result[name]
    for hidden in populated["private"] + [populated["ai"], populated["job"], populated["note"]]:
        assert hidden not in first.text


def test_json_csv_same_aggregates_and_formula_guard(client, populated):
    summary = client.get("/api/admin/summary", params=FILTER)
    response = client.get("/api/admin/export", params=FILTER)
    assert summary.headers["cache-control"] == response.headers["cache-control"] == "no-store"
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(response.text)))
    table = {(row["section"], row["date_or_step"], row["metric"]): row["value"] for row in rows}

    def verify(section, identity, data, prefix=""):
        for name, value in data.items():
            if isinstance(value, dict):
                verify(section, identity, value, prefix + name + ".")
            else:
                assert table[(section, identity, prefix + name)] == ("" if value is None else str(value))

    data = summary.json()
    for section in ("cards", "retention", "quality"):
        verify(section, "", data[section])
    for section, label in (("daily", "date"), ("funnel", "step")):
        for row in data[section]:
            verify(section, row[label], {key: value for key, value in row.items() if key != label})
    assert all(row["section"] != "usage" for row in rows)
    attack = client.get("/api/admin/export", params={**FILTER, "source": " =HYPERLINK(1)"})
    assert "' =HYPERLINK(1)" in attack.text
    assert "user_pseudonym" not in response.text


@pytest.mark.parametrize("value", ["=SUM(A1)", "+1", "-1", "@A1", "\t=1", " \r\n@A1"])
def test_csv_formula_prefixes(value):
    assert safe_cell(value) == "'" + value
    assert safe_cell(None) == "" and safe_cell(0) == 0


@pytest.mark.parametrize("path", ["/api/admin/summary", "/api/admin/export", "/admin", "/static/admin.js"])
def test_admin_requires_live_server_role(client, app, path):
    response = client.get(path, params=FILTER)
    assert response.status_code == 401 and response.headers["cache-control"] == "no-store"
    account = register(client)
    assert client.get(path, params=FILTER).status_code == 403
    with app.state.sessions() as db:
        db.get(User, account["id"]).role = "admin"
        db.commit()
    assert client.get(path, params=FILTER).status_code in {200, 404, 503}
    with app.state.sessions() as db:
        db.get(User, account["id"]).role = "user"
        db.commit()
    assert client.get(path, params=FILTER).status_code == 403


@pytest.mark.parametrize("change", [
    {"from": "2026-02-30"}, {"from": "2026-10-07"}, {"from": "2025-01-01"},
    {"to": "2026-10-08"}, {"channel": "email"}, {"source": ""},
    {"usage_limit": 0}, {"usage_limit": 101}, {"usage_offset": -1},
])
def test_invalid_filters_fail_without_fabricated_zeroes(client, populated, change):
    response = client.get("/api/admin/summary", params={**FILTER, **change})
    assert response.status_code == 422
    assert "cards" not in response.json()
    assert response.headers["cache-control"] == "no-store"


def test_empty_slice_is_valid_zero_dau_and_null_percentages(client, populated):
    result = client.get("/api/admin/summary", params={"from": "2026-10-01", "to": "2026-10-04"}).json()
    assert len(result["daily"]) == 4 and all(row["dau"] == 0 for row in result["daily"])
    assert result["cards"]["ai_activation"]["value"] is None
    assert result["quality"]["processing_p95_ms"] is None
    assert result["quality"]["costs"]["llm_cost"] == "0.00000000"  # No calls, rather than unknown calls.
    assert result["quality"]["costs"]["cost_per_user"] is None


def test_d7_matured_calendar_cohort_and_background_not_activity(app, client, populated, monkeypatch):
    with app.state.sessions() as db:
        record_event(db, populated["ai"], "task_completed", new_id(), occurred_at=stamp("2026-10-12T23:59:59"))
        record_event(db, populated["manual"], "reminder_sent", new_id(), "telegram", occurred_at=stamp("2026-10-13T12:00:00"))
        db.commit()
    original = aggregate
    monkeypatch.setattr("app.routes.admin.aggregate", lambda *args, **kwargs: original(*args, **kwargs, now=stamp("2026-10-14T00:00:00")))
    result = client.get("/api/admin/summary", params=FILTER).json()
    assert result["retention"]["d7"] == {"numerator": 1, "denominator": 2, "value": 50.0}
    assert result["retention"]["pending_d7"] == 0


def test_legacy_success_without_receipt_remains_unknown_and_midnight_call_not_duplicated(app, client, populated):
    with app.state.sessions() as db:
        receipt = db.scalar(select(ProviderUsage).where(ProviderUsage.operation_id == populated["job"]))
        db.delete(receipt)
        db.commit()
    result = client.get("/api/admin/summary", params=FILTER).json()
    assert result["quality"]["costs"]["unknown_usage_calls"] == 2
    assert result["quality"]["costs"]["llm_calls"] == 2
    assert any(row["model"] == "legacy-untracked" for row in result["usage"])
    with app.state.sessions() as db:
        db.add(ProviderUsage(user_id=populated["ai"], operation_id=populated["job"], request_id=new_id(),
                             kind="llm", channel="telegram", model="mock", status="succeeded",
                             occurred_at=stamp("2026-10-04T23:59:59")))
        db.commit()
    result = client.get("/api/admin/summary", params=FILTER).json()
    assert result["quality"]["costs"]["llm_calls"] == 1


def test_delivery_attempt_outcome_snapshot_survives_late_result_and_period_boundary(app, client, populated):
    with app.state.sessions() as db:
        for operation, at, name, outcome in [
            ("attempt1", "2026-10-05T10:00:00", "reminder_failed", "unknown"),
            ("attempt1", "2026-10-06T10:00:00", "reminder_result", "sent"),
            ("attempt2", "2026-10-06T10:00:00", "reminder_failed", "blocked"),
            ("attempt3", "2026-10-06T10:00:00", "reminder_failed", "retryable"),
        ]:
            record_event(db, populated["ai"], name, operation, "telegram", occurred_at=stamp(at), outcome=outcome)
        db.commit()
    result = client.get("/api/admin/summary", params=FILTER).json()["quality"]
    assert (result["reminder_sent"], result["reminder_unknown"], result["reminder_blocked"], result["reminder_failed"]) == (1, 0, 1, 1)
    day1 = client.get("/api/admin/summary", params={**FILTER, "to": "2026-10-05"}).json()["quality"]
    assert day1["reminder_unknown"] == 1 and day1["reminder_sent"] == 0


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="Local PostgreSQL is not configured")
def test_postgres_admin_snapshot_and_shared_contract(monkeypatch):
    url = os.environ["TEST_POSTGRES_URL"]
    assert make_url(url).host in {"127.0.0.1", "localhost", "::1"}
    monkeypatch.setenv("NOTES_DATABASE_URL", url)
    monkeypatch.setenv("NOTES_PROVIDER", "mock")
    command.upgrade(Config("alembic.ini"), "head")
    command.check(Config("alembic.ini"))
    app = create_app(Settings(database_url=url, auto_worker=False))
    try:
        with TestClient(app) as client:
            admin(client, app)
            source = "pg_admin_" + uuid.uuid4().hex
            ids = seed(app.state.sessions, source=source)
            original = aggregate
            monkeypatch.setattr("app.routes.admin.aggregate", lambda *args, **kwargs: original(*args, **kwargs, now=NOW))
            response = client.get("/api/admin/summary", params={**FILTER, "source": source})
            assert response.status_code == 200, response.text
            assert response.json()["cards"]["unique_users"] == 2
            assert response.json()["cards"]["ai_activation"]["value"] == 50.0
            csv_response = client.get("/api/admin/export", params={**FILTER, "source": source})
            assert csv_response.status_code == 200 and "0.25000000" in csv_response.text
            fired = []

            def concurrent_write(connection, cursor, statement, parameters, context, executemany):
                if fired or not statement.startswith("SELECT users.id, users.first_login_at"):
                    return
                fired.append(True)
                with app.state.sessions() as writer:
                    record_event(writer, ids["ai"], "note_edited", new_id(),
                                 occurred_at=stamp("2026-10-06T12:00:00"), subject_id=ids["note"])
                    writer.commit()

            event.listen(app.state.engine, "after_cursor_execute", concurrent_write)
            try:
                snapshot = client.get("/api/admin/summary", params={**FILTER, "source": source})
            finally:
                event.remove(app.state.engine, "after_cursor_execute", concurrent_write)
            assert fired and snapshot.status_code == 200, snapshot.text
            assert snapshot.json()["quality"]["edited_notes"] == 0
            following = client.get("/api/admin/summary", params={**FILTER, "source": source})
            assert following.json()["quality"]["edited_notes"] == 1
    finally:
        app.state.engine.dispose()


def test_different_owned_notes_cannot_be_mixed_into_activation(app, client, populated):
    with app.state.sessions() as db:
        user = User(id=new_id(), username="mixed_" + uuid.uuid4().hex, password_hash="synthetic", source="mixed",
                    created_at=stamp("2026-10-05T12:00:00"), first_login_at=stamp("2026-10-05T12:00:00"))
        db.add(user)
        db.flush()
        captures, notes = [], []
        for idx in range(2):
            capture = Capture(id=new_id(), user_id=user.id, original_text="Synthetic", idempotency_key=new_id())
            db.add(capture)
            db.flush()
            note = Note(id=new_id(), user_id=user.id, capture_id=capture.id, title="Synthetic", markdown="Synthetic", conclusions=[], provider="mock")
            db.add(note)
            captures.append(capture)
            notes.append(note)
        db.flush()
        record_event(db, user.id, "capture_saved", captures[0].id, occurred_at=stamp("2026-10-05T12:01:00"))
        record_event(db, user.id, "note_opened", new_id(), occurred_at=stamp("2026-10-05T12:02:00"), subject_id=notes[1].id)
        record_event(db, user.id, "structure_confirmed", new_id(), occurred_at=stamp("2026-10-05T12:03:00"), subject_id=notes[1].id)
        db.commit()
    result = client.get("/api/admin/summary", params={**FILTER, "source": "mixed"}).json()
    assert result["cards"]["ai_activation"] == {"numerator": 0, "denominator": 1, "value": 0.0}
    assert [row["users"] for row in result["funnel"]] == [1, 1, 0, 0, 0]


def test_stt_minutes_and_estimate_are_separate_from_measured_cost(app, client, populated):
    with app.state.sessions() as db:
        db.add(ProviderUsage(user_id=populated["ai"], operation_id=new_id(), request_id=new_id(), kind="stt",
                             channel="telegram", model="synthetic", status="succeeded", stt_seconds=90.0,
                             estimated_cost=Decimal("1.5"), occurred_at=stamp("2026-10-05T12:00:00")))
        db.commit()
    result = client.get("/api/admin/summary", params=FILTER).json()
    costs = result["quality"]["costs"]
    assert costs["stt_minutes"] == "1.50000000" and costs["stt_calls"] == 1
    assert costs["stt_cost"] is None and costs["known_stt_cost"] == "0.00000000"
    assert costs["unknown_usage_calls"] == 2
    stt = next(row for row in result["usage"] if row["kind"] == "stt")
    assert stt["estimated_cost"] == "1.50000000" and stt["cost"] is None


def test_moscow_midnight_inclusive_dates_and_foreground_session_boundary(app, client, populated):
    with app.state.sessions() as db:
        for at, channel, name in [("2026-10-06T23:59:59", "web", "note_opened"),
                                  ("2026-10-07T00:29:59", "telegram", "task_completed"),
                                  ("2026-10-07T01:00:00", "telegram", "note_opened")]:
            record_event(db, populated["ai"], name, new_id(), channel, occurred_at=stamp(at), subject_id=populated["note"])
        db.commit()
        events = list(db.scalars(select(ProductEvent).where(ProductEvent.user_id == populated["ai"], ProductEvent.occurred_at >= stamp("2026-10-06T23:59:59")).order_by(ProductEvent.occurred_at)))
        assert events[0].session_id == events[1].session_id != events[2].session_id
    result = client.get("/api/admin/summary", params={**FILTER, "from": "2026-10-07", "to": "2026-10-07"}).json()
    assert result["cards"]["dau"] == result["cards"]["unique_users"] == 1
    assert result["cards"]["returning_users"] == 1 and result["daily"][0]["session_count"] == 3  # Two active sessions and pending user's login.


@pytest.mark.parametrize("kind,error,extra_calls", [("text", "execution_unknown", 1), ("audio", "internal_error", 2), ("audio", "stt_not_configured", 0)])
def test_old_ambiguous_dispatch_cannot_turn_into_known_zero_cost(app, client, populated, kind, error, extra_calls):
    with app.state.sessions() as db:
        capture = Capture(id=new_id(), user_id=populated["ai"], original_text="Synthetic", idempotency_key=new_id(),
                          input_kind=kind, audio_seconds=90.0 if kind == "audio" else None)
        db.add(capture)
        db.flush()
        db.add(Job(user_id=populated["ai"], capture_id=capture.id, provider="mock", status="failed", error_code=error,
                   created_at=stamp("2026-10-05T12:00:00"), finished_at=stamp("2026-10-05T12:01:00")))
        db.commit()
    result = client.get("/api/admin/summary", params=FILTER).json()
    costs = result["quality"]["costs"]
    assert costs["unknown_usage_calls"] == 1 + extra_calls
    assert costs["llm_calls"] == 2 + (1 if extra_calls else 0)
    assert costs["stt_calls"] == (1 if extra_calls == 2 else 0)
    assert costs["stt_cost"] is None if extra_calls == 2 else costs["stt_cost"] == "0.00000000"


def test_event_snapshot_refreshes_test_flag_after_concurrent_account_change(app, client):
    account = register(client)
    with app.state.sessions() as actor:
        cached = actor.get(User, account["id"])
        assert cached.is_test is False
        with app.state.sessions() as writer:
            writer.get(User, account["id"]).is_test = True
            writer.commit()
        operation = new_id()
        record_event(actor, account["id"], "task_completed", operation)
        actor.commit()
    with app.state.sessions() as db:
        snapshot = db.scalar(select(ProductEvent).where(ProductEvent.operation_id == operation))
        assert snapshot.is_test is True
