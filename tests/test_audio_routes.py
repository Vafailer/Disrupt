"""Run with the pinned audio core checkout until the branches are integrated."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

pytest.importorskip("app.audio_contracts", reason="Requires the separately published audio core")

from app.audio_storage import AudioStorage  # noqa: E402
from app.models import Capture, Inbox, Job  # noqa: E402
from app.routes.audio import build_router  # noqa: E402
from app.services import create_audio_capture  # noqa: E402
from tests.conftest import register  # noqa: E402
from tests.test_audio_storage import wav  # noqa: E402
from tests.test_internal import BASE, SERVICE_TOKEN, linked  # noqa: E402


@pytest.fixture
def audio_app(app_factory, tmp_path):
    app = app_factory(internal_api_token=SERVICE_TOKEN)
    storage = AudioStorage(tmp_path / "audio")
    def database():
        with app.state.sessions() as db:
            yield db
    app.include_router(build_router(database, app.state.settings, storage, create_audio_capture))
    return app, storage


def web(client, data=None, key="audio1", **kw):
    return client.post("/api/v1/captures/audio", headers={"Idempotency-Key": key},
                       files={"audio": ("untrusted.txt", wav() if data is None else data, "text/plain")},
                       data={"processing_mode": "ai"}, **kw)


def telegram(client, data=None, update=2):
    return client.post("/internal/v1/telegram/voice", headers={"Authorization": "Bearer " + SERVICE_TOKEN},
                       data={**BASE, "update_id": update, "processing_mode": "ai"},
                       files={"audio": ("anything", wav() if data is None else data)})


def test_web_replay_conflict_download_and_access(audio_app, monkeypatch):
    app, storage = audio_app
    with TestClient(app) as client:
        register(client)
        saved = web(client)
        assert saved.status_code == 202, saved.text
        result = saved.json()
        def forbidden(*_):
            raise AssertionError("Replay must not decode")
        monkeypatch.setattr(storage, "inspect", forbidden)
        assert web(client).json() == result
        assert web(client, wav(0.2)).status_code == 409
        with app.state.sessions() as db:
            capture = db.scalar(select(Capture))
            capture_id = capture.id
            assert len(db.scalars(select(Job)).all()) == 1
        download = client.get(f"/api/v1/captures/{capture_id}/audio")
        assert download.content == wav()
        assert download.headers["cache-control"] == "no-store"
        assert download.headers["x-content-type-options"] == "nosniff"
        client.cookies.clear()
        assert client.get(f"/api/v1/captures/{capture_id}/audio").status_code == 401
        register(client, username="other")
        assert client.get(f"/api/v1/captures/{capture_id}/audio").status_code == 404


def test_telegram_replay_and_cross_route_conflict(audio_app, monkeypatch):
    app, storage = audio_app
    with TestClient(app) as client:
        linked(client)
        saved = telegram(client)
        assert saved.status_code == 200, saved.text
        def forbidden(*_):
            raise AssertionError("Replay must not decode")
        monkeypatch.setattr(storage, "inspect", forbidden)
        assert telegram(client).json() == saved.json()
        changed = telegram(client, wav(0.2))
        assert changed.status_code == 409 and changed.json()["error"]["code"] == "conflict"
        text = client.post("/internal/v1/telegram/updates", headers={"Authorization": "Bearer " + SERVICE_TOKEN},
                           json={**BASE, "update_id": 2, "text": "text"})
        assert text.status_code == 409
        with app.state.sessions() as db:
            assert len(db.scalars(select(Capture)).all()) == 1


def test_auth_before_form_or_storage(audio_app, monkeypatch):
    app, storage = audio_app
    def forbidden(*_):
        raise AssertionError("Unauthorized input must not reach storage")
    monkeypatch.setattr(storage, "stage", forbidden)
    with TestClient(app) as client:
        assert web(client).status_code == 401
        response = client.post("/internal/v1/telegram/voice", content=b"not multipart")
        assert response.status_code == 401
        register(client)
        client.headers.pop("X-CSRF-Token")
        assert web(client).status_code == 403


@pytest.mark.parametrize("payload,status", [(b"", 400), (b"broken", 400), (b"x" * (10*1024*1024+1), 413)])
def test_refusals_do_not_create_capture(audio_app, payload, status):
    app, storage = audio_app
    with TestClient(app) as client:
        linked(client)
        assert telegram(client, payload).status_code == status
        with app.state.sessions() as db:
            assert not db.scalars(select(Capture)).all()
        assert not list(storage.root.iterdir())


def test_invalid_fields_and_missing_key(audio_app):
    app, _ = audio_app
    with TestClient(app) as client:
        register(client)
        response = client.post("/api/v1/captures/audio", files={"audio": ("test", wav())}, data={"processing_mode": "ai"})
        assert response.status_code == 422
        for fields in [[("processing_mode", (None, "ai")), ("audio", ("a", wav())), ("extra", (None, "x"))],
                       [("processing_mode", (None, "ai")), ("audio", ("a", wav())), ("audio", ("b", wav()))]]:
            response = client.post("/api/v1/captures/audio", headers={"Idempotency-Key": "a"}, files=fields)
            assert response.status_code in {400, 422}


def test_queue_refusal_is_retryable_and_keeps_no_inbox(audio_app, monkeypatch):
    app, storage = audio_app
    with TestClient(app) as client:
        linked(client)
        # Fill the account's queue without altering immutable Settings.
        for index in range(app.state.settings.max_pending_per_user):
            client.post("/internal/v1/telegram/updates", headers={"Authorization": "Bearer " + SERVICE_TOKEN},
                        json={**BASE, "update_id": index+100, "text": "queue"})
        response = telegram(client)
        assert response.status_code == 429, response.text
        with app.state.sessions() as db:
            assert db.scalar(select(Inbox).where(Inbox.update_id == 2)) is None
        # A normal queue retry must not consume disk with orphan originals.
        assert telegram(client).status_code == 429
        assert not list(storage.root.iterdir())


def test_publish_survives_database_failure(audio_app, monkeypatch):
    app, storage = audio_app
    with TestClient(app, raise_server_exceptions=False) as client:
        register(client)
        session_class = app.state.sessions.class_
        def fail(_):
            raise RuntimeError("synthetic commit failure")
        monkeypatch.setattr(session_class, "commit", fail)
        assert web(client).status_code == 500
        originals = list(storage.root.glob("*.audio"))
        assert len(originals) == 1 and originals[0].read_bytes() == wav()
        with app.state.sessions() as db:
            assert not db.scalars(select(Capture)).all()


def test_missing_original_returns_503(audio_app):
    app, storage = audio_app
    with TestClient(app) as client:
        register(client)
        assert web(client).status_code == 202
        with app.state.sessions() as db:
            capture = db.scalar(select(Capture))
            capture_id = capture.id
            (storage.root / capture.audio_key).unlink()
        assert client.get(f"/api/v1/captures/{capture_id}/audio").status_code == 503


def test_stt_failure_preserves_download(audio_app):
    from app.providers import ProviderError
    from app.worker import Worker
    app, storage = audio_app
    class FailedSpeech:
        def transcribe(self, source, *, media_type):
            assert source.read() == wav()
            raise ProviderError("stt_failed")
    with TestClient(app) as client:
        register(client)
        job = web(client).json()
        worker = Worker(app.state.sessions, app.state.settings, audio_storage=AudioStorage(storage.root, read_only=True),
                        speech_provider=FailedSpeech())
        assert worker.run_once()
        state = client.get("/api/v1/jobs/" + job["id"]).json()
        assert state["status"] == "failed"
        with app.state.sessions() as db:
            capture = db.scalar(select(Capture))
            assert capture.transcript is None
            assert client.get(f"/api/v1/captures/{capture.id}/audio").content == wav()


def test_postgres_concurrent_audio(tmp_path, monkeypatch):
    import os
    from concurrent.futures import ThreadPoolExecutor
    from uuid import uuid4

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    from app.config import Settings
    from app.main import create_app
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Local PostgreSQL is not configured")
    assert make_url(url).host in {"localhost", "127.0.0.1", "::1"}
    # Each concurrency test owns a schema; its queued jobs must not affect other workers.
    schema = "audio_" + uuid4().hex
    admin_engine = create_engine(url)
    with admin_engine.begin() as connection:
        connection.execute(text('CREATE SCHEMA "' + schema + '"'))
    url = make_url(url).update_query_dict({"options": "-csearch_path=" + schema}).render_as_string(hide_password=False)
    monkeypatch.setenv("NOTES_DATABASE_URL", url)
    monkeypatch.setenv("NOTES_PROVIDER", "mock")
    command.upgrade(Config("alembic.ini"), "head")
    app = create_app(Settings(database_url=url, auto_worker=False, internal_api_token=SERVICE_TOKEN))
    storage = AudioStorage(tmp_path / "postgres-audio")
    def database():
        with app.state.sessions() as db:
            yield db
    app.include_router(build_router(database, app.state.settings, storage, create_audio_capture))
    try:
        with TestClient(app) as client:
            user = register(client, username="audio_" + uuid4().hex)
            base = {**BASE, "bot_id": uuid4().int % 10**12 + 1}
            code = client.post("/api/v1/telegram/link-code").json()
            pending = client.post("/internal/v1/telegram/link-request", headers={"Authorization": "Bearer " + SERVICE_TOKEN},
                                  json={**base, "update_id": 1, "code": code["code"]})
            assert pending.status_code == 200, pending.text
            assert client.post("/api/v1/telegram/links/" + code["link_request_id"] + "/confirm").status_code == 200
            def send_web(_):
                return web(client)
            def send_telegram(_):
                return client.post("/internal/v1/telegram/voice", headers={"Authorization": "Bearer " + SERVICE_TOKEN},
                                   data={**base, "update_id": 2, "processing_mode": "ai"}, files={"audio": ("test", wav())})
            for send, status in [(send_web, 202), (send_telegram, 200)]:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    responses = list(pool.map(send, range(4)))
                assert [r.status_code for r in responses] == [status]*4, [r.text for r in responses]
                assert all(r.json() == responses[0].json() for r in responses)
            with app.state.sessions() as db:
                assert len(db.scalars(select(Capture).where(Capture.user_id == user["id"])).all()) == 2
            assert len(list(storage.root.glob("*.audio"))) == 2
    finally:
        app.state.engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        admin_engine.dispose()
