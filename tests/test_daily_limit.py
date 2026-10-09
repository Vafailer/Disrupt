"""Daily AI allowance in units. Synthetic data, no network, no real provider."""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.engine import make_url

from app.audio_storage import AudioStorage
from app.config import Settings
from app.limits import capture_cost, daily_state, day_window, telegram_state, units_used, within_daily_limit
from app.main import create_app
from app.models import Capture, Job, Note, ProviderBudget, User
from app.providers import MockProvider
from app.worker import Worker
from tests.conftest import register
from tests.test_audio_routes import telegram, web
from tests.test_audio_storage import wav
from tests.test_internal import SERVICE_TOKEN, linked, message

MOSCOW = ZoneInfo("Europe/Moscow")


def moscow(*parts):
    return datetime(*parts, tzinfo=MOSCOW).timestamp()


def text_post(client, key, mode="ai", text="Синтетическая мысль"):
    return client.post(
        "/api/v1/captures/text", json={"text": text, "processing_mode": mode}, headers={"Idempotency-Key": key},
    )


def count(app, model, *where):
    with app.state.sessions() as db:
        return db.scalar(select(func.count()).select_from(model).where(*where))


@pytest.mark.parametrize(("kind", "seconds", "units"), [
    ("text", None, 1),
    ("audio", 0.1, 2),
    ("audio", 40, 2),
    ("audio", 59.9, 2),
    ("audio", 60, 3),
    ("audio", 90, 3),
    ("audio", 119.99, 3),
    ("audio", 120, 4),
    ("audio", 180, 5),
    ("audio", None, 2),
])
def test_cost_formula_with_default_numbers(kind, seconds, units):
    assert capture_cost(Settings(), kind, seconds) == units


def test_cost_formula_follows_settings():
    settings = Settings(text_unit_cost=3, audio_unit_base=5, audio_unit_per_minute=2)
    assert capture_cost(settings, "text") == 3
    assert capture_cost(settings, "audio", 130) == 9


def test_defaults_and_environment(monkeypatch):
    settings = Settings()
    assert (settings.daily_unit_limit, settings.text_unit_cost) == (30, 1)
    assert (settings.audio_unit_base, settings.audio_unit_per_minute) == (2, 1)
    assert settings.limit_timezone == "Europe/Moscow"
    for name, value in {
        "NOTES_DAILY_UNIT_LIMIT": "0", "NOTES_TEXT_UNIT_COST": "2", "NOTES_AUDIO_UNIT_BASE": "4",
        "NOTES_AUDIO_UNIT_PER_MINUTE": "3", "NOTES_LIMIT_TIMEZONE": "Asia/Yekaterinburg",
    }.items():
        monkeypatch.setenv(name, value)
    loaded = Settings.from_env()
    assert (loaded.daily_unit_limit, loaded.text_unit_cost, loaded.audio_unit_base) == (0, 2, 4)
    assert (loaded.audio_unit_per_minute, loaded.limit_timezone) == (3, "Asia/Yekaterinburg")


@pytest.mark.parametrize("changes", [
    {"limit_timezone": "Mars/Base"}, {"daily_unit_limit": -1}, {"text_unit_cost": -1},
    {"audio_unit_base": -1}, {"audio_unit_per_minute": -1},
])
def test_invalid_numbers_and_zone_are_refused(changes):
    with pytest.raises(ValueError, match="NOTES_LIMIT_TIMEZONE|must not be negative"):
        Settings(**changes)


def test_day_boundary_is_local_midnight_in_moscow():
    settings = Settings()
    before = day_window(settings, moscow(2026, 10, 9, 23, 59, 59))
    after = day_window(settings, moscow(2026, 10, 10, 0, 0, 0))
    assert before[0].isoformat() == "2026-10-09T00:00:00+03:00"
    assert before[1].isoformat() == after[0].isoformat() == "2026-10-10T00:00:00+03:00"
    assert after[1].isoformat() == "2026-10-11T00:00:00+03:00"
    # 21:00 UTC is already the next day in Moscow, 20:59 UTC is not.
    utc = ZoneInfo("UTC")
    assert day_window(settings, datetime(2026, 10, 9, 20, 59, tzinfo=utc).timestamp())[1].day == 10
    assert day_window(settings, datetime(2026, 10, 9, 21, 0, tzinfo=utc).timestamp())[1].day == 11


def test_units_count_only_today_and_only_ai(app, client):
    user = register(client)

    def add(key, created, mode="ai", kind="text", seconds=None):
        return Capture(
            user_id=user["id"], original_text="x", idempotency_key=key, created_at=created,
            processing_mode=mode, input_kind=kind, audio_seconds=seconds,
        )

    now = moscow(2026, 10, 10, 0, 30)
    with app.state.sessions() as db:
        db.add_all([
            add("yesterday-late", moscow(2026, 10, 9, 23, 59, 59)),
            add("today-first", moscow(2026, 10, 10, 0, 0, 0)),
            add("today-audio", moscow(2026, 10, 10, 0, 10), kind="audio", seconds=90),
            add("today-manual", moscow(2026, 10, 10, 0, 20), mode="manual"),
            add("tomorrow", moscow(2026, 10, 11, 0, 0, 1)),
        ])
        db.commit()
        settings = app.state.settings
        assert units_used(db, settings, user["id"], now) == 1 + 3
        assert units_used(db, settings, user["id"], moscow(2026, 10, 9, 23, 59, 59)) == 1
        assert within_daily_limit(db, settings, user["id"], 26, now)
        assert not within_daily_limit(db, settings, user["id"], 27, now)
        state = daily_state(db, settings, user["id"], now)
    assert state["daily_units_used"] == 4 and state["daily_units_remaining"] == 26
    assert state["limit_resets_at"] == "2026-10-11T00:00:00+03:00"


def test_web_text_over_limit_is_saved_as_manual_without_job(app_factory):
    app = app_factory(daily_unit_limit=2)
    with TestClient(app) as client:
        register(client)
        first, second = text_post(client, "a"), text_post(client, "b")
        assert first.status_code == second.status_code == 202
        assert first.json()["id"] and "ai_limit_exceeded" not in first.json()
        limited = text_post(client, "c", text="  Точный текст\nсвернуть нельзя  ")
        assert limited.status_code == 202, limited.text
        body = limited.json()
        assert body["job_id"] is None and body["status"] == "saved" and body["ai_limit_exceeded"] is True
        note = client.get("/api/v1/notes/" + body["note_id"]).json()
        assert note["provider"] == "manual" and note["original_text"] == "  Точный текст\nсвернуть нельзя  "
        assert text_post(client, "c", text="  Точный текст\nсвернуть нельзя  ").json() == body  # Replay
        assert text_post(client, "c", text="Другой текст").status_code == 409
        # An honest manual save is never flagged and never costs units.
        manual = text_post(client, "d", mode="manual").json()
        assert "ai_limit_exceeded" not in manual
        usage = client.get("/api/v1/provider/usage").json()
        assert usage["daily_units_used"] == 2 and usage["daily_units_remaining"] == 0
    assert count(app, Job) == 2 and count(app, Capture) == 4
    with app.state.sessions() as db:
        assert db.scalar(select(Capture.processing_mode).where(Capture.idempotency_key == "c")) == "manual"


def test_limit_is_per_user(app_factory):
    app = app_factory(daily_unit_limit=1)
    with TestClient(app) as first, TestClient(app) as second:
        register(first, "one")
        register(second, "two")
        assert text_post(first, "a").json()["id"]
        assert text_post(first, "b").json()["ai_limit_exceeded"] is True
        assert text_post(second, "a").json()["id"]


def test_limit_zero_disables_the_daily_check(app_factory):
    app = app_factory(daily_unit_limit=0)
    with TestClient(app) as client:
        register(client)
        for index in range(6):
            assert "ai_limit_exceeded" not in text_post(client, f"k{index}").json()
        usage = client.get("/api/v1/provider/usage").json()
    assert usage["daily_unit_limit"] == 0 and usage["daily_units_used"] == 6
    assert usage["daily_units_remaining"] is None
    assert count(app, Job) == 6


def test_usage_endpoint_keeps_old_keys_and_adds_daily_ones(app_factory):
    app = app_factory(daily_unit_limit=10, audio_unit_per_minute=2)
    with TestClient(app) as client:
        register(client)
        text_post(client, "a")
        usage = client.get("/api/v1/provider/usage").json()
    assert usage["provider"] == "mock" and usage["simulation"] is True
    assert {k: usage[k] for k in (
        "daily_unit_limit", "daily_units_used", "daily_units_remaining",
        "text_unit_cost", "audio_unit_base", "audio_unit_per_minute",
    )} == {
        "daily_unit_limit": 10, "daily_units_used": 1, "daily_units_remaining": 9,
        "text_unit_cost": 1, "audio_unit_base": 2, "audio_unit_per_minute": 2,
    }
    reset = datetime.fromisoformat(usage["limit_resets_at"])
    assert (reset.hour, reset.minute, reset.second) == (0, 0, 0)
    assert reset.utcoffset().total_seconds() == 3 * 3600
    assert reset > datetime.now(MOSCOW)


def test_live_caps_are_unchanged_and_downgrade_spends_no_slot(app_factory):
    app = app_factory(
        llm_provider=MockProvider(), provider="cloudru", allow_live_requests=True, cloudru_model="synthetic",
        live_call_limit=5, live_user_call_limit=3, daily_unit_limit=1,
    )
    with TestClient(app) as client:
        user = register(client)
        assert text_post(client, "a").json()["id"]
        assert text_post(client, "b").json()["ai_limit_exceeded"] is True
        usage = client.get("/api/v1/provider/usage").json()
        assert usage["global_limit"] == 5 and usage["user_limit"] == 3 and usage["global_used"] == 0
        assert usage["daily_units_used"] == 1 and usage["model"] == "synthetic"
        worker = Worker(app.state.sessions, app.state.settings, MockProvider())
        assert worker.run_once() and not worker.run_once()
    with app.state.sessions() as db:
        assert db.get(ProviderBudget, "cloudru").reserved_calls == 1
        assert db.get(User, user["id"]).live_calls == 1
        assert db.scalar(select(Job.status)) == "succeeded"


def test_daily_room_does_not_lift_the_user_cap(app_factory):
    app = app_factory(
        llm_provider=MockProvider(), provider="cloudru", allow_live_requests=True, cloudru_model="synthetic",
        live_call_limit=5, live_user_call_limit=1, daily_unit_limit=30,
    )
    with TestClient(app) as client:
        register(client)
        first, second = text_post(client, "a").json(), text_post(client, "b").json()
        assert "ai_limit_exceeded" not in second
        worker = Worker(app.state.sessions, app.state.settings, MockProvider())
        assert worker.run_once() and worker.run_once()
        states = {client.get("/api/v1/jobs/" + j["id"]).json()["status"] for j in (first, second)}
        assert states == {"succeeded", "failed"}


def test_limit_state_helper_matches_daily_state(app_factory):
    app = app_factory(daily_unit_limit=5)
    with TestClient(app) as client:
        register(client)
        text_post(client, "a")
    with app.state.sessions() as db:
        user_id = db.scalar(select(User.id))
        settings = app.state.settings
        state = telegram_state(db, settings, user_id)
        assert state["ai_units_remaining"] == 4
        assert state["ai_limit_resets_at"] == daily_state(db, settings, user_id)["limit_resets_at"]
    off = app_factory(daily_unit_limit=0)
    with off.state.sessions() as db:
        assert telegram_state(db, off.state.settings, "any") == {"ai_units_remaining": None, "ai_limit_resets_at": None}


@pytest.fixture
def limited_audio(app_factory, tmp_path):
    def make(**changes):
        return app_factory(internal_api_token=SERVICE_TOKEN, audio_storage=AudioStorage(tmp_path / "audio"), **changes)

    return make


def test_web_audio_cost_counts_full_minutes_then_downgrades(limited_audio):
    app = limited_audio(daily_unit_limit=4)
    with TestClient(app) as client:
        register(client)
        long = web(client, wav(61), key="long")  # 2 + 1 = 3 units
        assert long.status_code == 202 and long.json()["id"]
        assert client.get("/api/v1/provider/usage").json()["daily_units_used"] == 3
        small = wav(0.1)  # 2 units, 3 + 2 > 4
        limited = web(client, small, key="short")
        assert limited.status_code == 202, limited.text
        body = limited.json()
        assert body["ai_limit_exceeded"] is True and body["id"] is None and body["job_id"] is None
        assert body["status"] == "saved" and body["note_id"]
        assert web(client, small, key="short").json() == body  # Replay
        assert web(client, wav(0.2), key="short").status_code == 409
        with app.state.sessions() as db:
            capture = db.scalar(select(Capture).where(Capture.idempotency_key == "short"))
            capture_id, mode = capture.id, capture.processing_mode
            assert db.scalar(select(Note.provider).where(Note.capture_id == capture_id)) == "manual"
            assert db.scalar(select(func.count()).select_from(Job).where(Job.capture_id == capture_id)) == 0
        assert mode == "manual"
        assert client.get(f"/api/v1/captures/{capture_id}/audio").content == small
        assert client.get(f"/api/v1/captures/{capture_id}").json()["job"] is None
        assert client.get("/api/v1/provider/usage").json()["daily_units_used"] == 3
    assert count(app, Job) == 1


def test_telegram_text_over_limit_is_saved_without_ai(limited_audio):
    app = limited_audio(daily_unit_limit=1)
    with TestClient(app) as client:
        linked(client)
        first = message(client, update_id=2)
        assert first.status_code == 200 and first.json()["job_id"] and first.json()["ai_limit_exceeded"] is False
        limited = message(client, update_id=3, text="Второй текст")
        assert limited.status_code == 200, limited.text
        assert limited.json()["job_id"] is None and limited.json()["ai_limit_exceeded"] is True
        assert message(client, update_id=3, text="Второй текст").json() == limited.json()
        manual = message(client, update_id=4, text="Ручной", mode="manual").json()
        assert manual["ai_limit_exceeded"] is False
    assert count(app, Job) == 1
    with app.state.sessions() as db:
        saved = db.scalar(select(Capture).where(Capture.id == limited.json()["capture_id"]))
        assert (saved.processing_mode, saved.original_text) == ("manual", "Второй текст")


def test_telegram_text_reports_units_left_and_reset(limited_audio):
    app = limited_audio(daily_unit_limit=2)
    with TestClient(app) as client:
        linked(client)
        first = message(client, update_id=2).json()
        assert first["ai_units_remaining"] == 1
        reset = datetime.fromisoformat(first["ai_limit_resets_at"])
        assert (reset.hour, reset.minute) == (0, 0) and reset > datetime.now(MOSCOW)
        message(client, update_id=3, text="Второй")
        limited = message(client, update_id=4, text="Третий").json()
        assert limited["ai_limit_exceeded"] is True and limited["ai_units_remaining"] == 0
        assert limited["ai_limit_resets_at"] == first["ai_limit_resets_at"]
        manual = message(client, update_id=5, text="Ручной", mode="manual").json()
        assert manual["ai_units_remaining"] == 0


def test_telegram_text_keys_are_null_when_limit_is_off(limited_audio):
    app = limited_audio(daily_unit_limit=0)
    with TestClient(app) as client:
        linked(client)
        body = message(client, update_id=2).json()
    assert body["ai_units_remaining"] is None and body["ai_limit_resets_at"] is None


def test_telegram_voice_reports_units_left_and_reset(limited_audio):
    app = limited_audio(daily_unit_limit=5)
    with TestClient(app) as client:
        linked(client)
        first = telegram(client, wav(0.1), update=2).json()  # 2 units
        assert first["ai_units_remaining"] == 3 and first["ai_limit_resets_at"]
        telegram(client, wav(0.2), update=3)  # 2 units
        limited = telegram(client, wav(0.3), update=4).json()  # 2 units, does not fit
        assert limited["ai_limit_exceeded"] is True and limited["ai_units_remaining"] == 1
    off = limited_audio(daily_unit_limit=0)
    with TestClient(off) as client:
        linked(client)
        body = telegram(client, wav(0.1), update=2).json()
    assert body["ai_units_remaining"] is None and body["ai_limit_resets_at"] is None


def test_telegram_voice_over_limit_is_saved_without_ai(limited_audio):
    app = limited_audio(daily_unit_limit=2)
    with TestClient(app) as client:
        linked(client)
        first = telegram(client, wav(0.1), update=2)
        assert first.status_code == 200 and first.json()["job_id"]
        assert "ai_limit_exceeded" not in first.json()
        limited = telegram(client, wav(0.2), update=3)
        assert limited.status_code == 200, limited.text
        body = limited.json()
        assert body["job_id"] is None and body["ai_limit_exceeded"] is True and body["status"] == "saved"
        assert body["note_url"].endswith("?capture=" + body["capture_id"])
        assert telegram(client, wav(0.2), update=3).json() == body
        with app.state.sessions() as db:
            assert db.scalar(select(Capture.processing_mode).where(Capture.id == body["capture_id"])) == "manual"
    assert count(app, Job) == 1 and count(app, Capture, Capture.input_kind == "audio") == 2


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="Local PostgreSQL is not configured")
def test_postgres_concurrent_captures_do_not_pass_the_limit(monkeypatch):
    url = os.environ["TEST_POSTGRES_URL"]
    assert make_url(url).host in {"127.0.0.1", "localhost", "::1"}
    monkeypatch.setenv("NOTES_DATABASE_URL", url)
    monkeypatch.setenv("NOTES_PROVIDER", "mock")
    command.upgrade(Config("alembic.ini"), "head")
    app = create_app(Settings(database_url=url, auto_worker=False, daily_unit_limit=2))
    try:
        with TestClient(app) as client:
            owner = register(client, "pg_daily_" + uuid.uuid4().hex)

            def send(index):
                return text_post(client, f"race-{index}")

            with ThreadPoolExecutor(max_workers=4) as pool:
                responses = list(pool.map(send, range(6)))
            assert all(r.status_code == 202 for r in responses), [r.text for r in responses]
            limited = [r for r in responses if r.json().get("ai_limit_exceeded")]
            assert len(limited) == 4  # The account lock serializes the check: no overshoot.
            with app.state.sessions() as db:
                assert db.scalar(select(func.count()).select_from(Job).where(Job.user_id == owner["id"])) == 2
                # The database is shared with other PostgreSQL tests: do not leave queued jobs for their workers.
                db.execute(delete(Job).where(Job.user_id == owner["id"]))
                db.commit()
    finally:
        app.state.engine.dispose()
