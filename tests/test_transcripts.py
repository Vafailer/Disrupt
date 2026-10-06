import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url

from app.config import Settings
from app.main import create_app
from app.models import Capture, Job, Note, ProductEvent, TranscriptRevision
from app.providers import DEMO_TEXT
from app.speech import MockSpeechProvider
from app.worker import Worker
from tests.conftest import register
from tests.test_audio_storage import wav


def upload(app, client, *, process=False):
    register(client)
    response = client.post(
        "/api/v1/captures/audio", headers={"Idempotency-Key": "transcript-test"},
        data={"processing_mode": "ai"}, files={"audio": ("synthetic.wav", wav())},
    )
    assert response.status_code == 202, response.text
    result = response.json()
    if process:
        Worker(app.state.sessions, app.state.settings, audio_storage=app.state.audio_storage,
               speech_provider=MockSpeechProvider(DEMO_TEXT)).run_once()
    return result["capture_id"], result["id"]


def patch(client, capture_id, *, version=1, text="Исправленная расшифровка"):
    return client.patch(f"/api/v1/captures/{capture_id}/transcript", json={"version": version, "text": text})


def test_edit_keeps_audio_initial_transcript_note_and_job(app, client):
    capture_id, job_id = upload(app, client, process=True)
    read = client.get(f"/api/v1/captures/{capture_id}").json()
    assert read["transcript_origin"] == "stt" and read["transcript"] == DEMO_TEXT
    before_note = client.get("/api/v1/notes/" + read["note_id"]).json()
    assert before_note["input_kind"] == "audio" and before_note["transcript_version"] == 1
    saved = patch(client, capture_id, text="  Исправленная расшифровка\n")
    assert saved.status_code == 200, saved.text
    assert saved.json()["transcript"] == "  Исправленная расшифровка\n"
    assert saved.json()["transcript_version"] == 2 and saved.json()["transcript_origin"] == "user"
    history = client.get(f"/api/v1/captures/{capture_id}/transcript-revisions").json()
    assert [(row["version"], row["origin"]) for row in history] == [(2, "user"), (1, "stt")]
    assert history[1]["text"] == DEMO_TEXT
    with app.state.sessions() as db:
        capture = db.get(Capture, capture_id)
        assert capture.original_text == DEMO_TEXT
        assert db.get(Job, job_id).status == "succeeded"
        assert db.scalar(select(func.count()).select_from(Job)) == 1
        assert db.get(Note, read["note_id"]).version == before_note["version"]
        assert db.get(Note, read["note_id"]).markdown == before_note["markdown"]
        event = db.scalar(select(ProductEvent).where(ProductEvent.name == "transcript_edited"))
        assert event.operation_id == f"{capture_id}:2"
    assert client.get(f"/api/v1/captures/{capture_id}/audio").content == wav()
    assert patch(client, capture_id).status_code == 409
    assert patch(client, capture_id, version=2, text="  Исправленная расшифровка\n").status_code == 200
    assert len(client.get(f"/api/v1/captures/{capture_id}/transcript-revisions").json()) == 2


def test_manual_transcript_after_missing_stt_never_requeues(app, client):
    capture_id, job_id = upload(app, client)
    Worker(app.state.sessions, app.state.settings, audio_storage=app.state.audio_storage).run_once()
    response = patch(client, capture_id)
    assert response.status_code == 200, response.text
    assert response.json()["original_text"] == "" and response.json()["note_id"] is None
    assert response.json()["job"]["error_code"] == "stt_not_configured"
    with app.state.sessions() as db:
        assert db.get(Job, job_id).status == "failed"
        assert db.scalar(select(func.count()).select_from(Note)) == 0
        assert len(db.scalars(select(TranscriptRevision)).all()) == 1
    assert client.get(f"/api/v1/captures/{capture_id}/audio").content == wav()


@pytest.mark.parametrize("status", ["queued", "running"])
def test_cannot_edit_during_processing(app, client, status):
    capture_id, job_id = upload(app, client)
    with app.state.sessions() as db:
        db.get(Job, job_id).status = status
        db.commit()
    assert patch(client, capture_id).status_code == 409
    with app.state.sessions() as db:
        assert db.get(Capture, capture_id).transcript is None
        assert not db.scalars(select(TranscriptRevision)).all()


def test_transcript_requires_owner_csrf_and_audio(app, client):
    capture_id, _ = upload(app, client, process=True)
    token = client.headers.pop("X-CSRF-Token")
    assert patch(client, capture_id).status_code == 403
    client.headers["X-CSRF-Token"] = token
    capture = client.post("/api/v1/captures/text", headers={"Idempotency-Key": "text"}, json={"text": "Исходник"})
    assert patch(client, capture.json()["capture_id"]).status_code == 404
    client.cookies.clear()
    assert patch(client, capture_id).status_code == 401
    assert client.get(f"/api/v1/captures/{capture_id}/transcript-revisions").status_code == 401
    register(client, "other")
    assert patch(client, capture_id).status_code == 404
    assert client.get(f"/api/v1/captures/{capture_id}/transcript-revisions").status_code == 404
    assert client.post(f"/api/v1/captures/{capture_id}/original-opened", json={"operation_id": str(uuid4())}).status_code == 404


@pytest.mark.parametrize("payload", [
    {"version": 1, "text": " "}, {"version": 1, "text": "a\0b"},
    {"version": 1, "text": "x" * 12001}, {"version": 0, "text": "Текст"},
    {"version": True, "text": "Текст"}, {"version": 1, "text": "Текст", "user_id": "forged"},
])
def test_invalid_edits_leave_history_untouched(app, client, payload):
    capture_id, _ = upload(app, client, process=True)
    assert client.patch(f"/api/v1/captures/{capture_id}/transcript", json=payload).status_code == 422
    assert len(client.get(f"/api/v1/captures/{capture_id}/transcript-revisions").json()) == 1


def test_reads_do_not_record_activity_and_opened_is_idempotent(app, client):
    capture_id, _ = upload(app, client, process=True)
    for _ in range(2):
        client.get(f"/api/v1/captures/{capture_id}")
        client.get(f"/api/v1/captures/{capture_id}/transcript-revisions")
    with app.state.sessions() as db:
        assert db.scalar(select(ProductEvent).where(ProductEvent.name == "original_opened")) is None
    operation = str(uuid4())
    for _ in range(2):
        assert client.post(f"/api/v1/captures/{capture_id}/original-opened", json={"operation_id": operation}).status_code == 204
    with app.state.sessions() as db:
        assert len(db.scalars(select(ProductEvent).where(ProductEvent.name == "original_opened")).all()) == 1


def test_postgres_concurrent_transcript_edits(tmp_path, monkeypatch):
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Local PostgreSQL is not configured")
    assert make_url(url).host in {"localhost", "127.0.0.1", "::1"}
    schema = "transcripts_" + uuid4().hex
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text('CREATE SCHEMA "' + schema + '"'))
    isolated = make_url(url).update_query_dict({"options": "-csearch_path=" + schema}).render_as_string(hide_password=False)
    monkeypatch.setenv("NOTES_DATABASE_URL", isolated)
    monkeypatch.setenv("NOTES_PROVIDER", "mock")
    command.upgrade(Config("alembic.ini"), "head")
    app = create_app(Settings(database_url=isolated, auto_worker=False, audio_storage_path=str(tmp_path / "audio")))
    try:
        with TestClient(app) as client:
            capture_id, _ = upload(app, client, process=True)
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda index: patch(client, capture_id, text=f"Правка {index}"), range(4)))
            assert sorted(r.status_code for r in results) == [200, 409, 409, 409], [r.text for r in results]
            history = client.get(f"/api/v1/captures/{capture_id}/transcript-revisions").json()
            assert [row["version"] for row in history] == [2, 1]
            assert client.get(f"/api/v1/captures/{capture_id}/audio").content == wav()
    finally:
        app.state.engine.dispose()
        with engine.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        engine.dispose()
