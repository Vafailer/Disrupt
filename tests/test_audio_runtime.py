"""Check application and process wiring, not a separately mounted test router."""

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import main, worker
from app.audio_storage import AudioStorageUnavailable
from app.models import Capture, Job
from app.providers import DEMO_TEXT, MockProvider
from app.speech import MockSpeechProvider
from tests.conftest import register
from tests.test_audio_storage import wav


def upload(client):
    return client.post(
        "/api/v1/captures/audio", headers={"Idempotency-Key": "runtime-audio"},
        files={"audio": ("synthetic.wav", wav(), "audio/wav")}, data={"processing_mode": "ai"},
    )


def test_app_worker_reads_original_and_commits_transcript_before_llm(app_factory, monkeypatch):
    workers = []
    speech = MockSpeechProvider(DEMO_TEXT)
    inputs = []

    class TrackingWorker(worker.Worker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            workers.append(self)

    class LLM(MockProvider):
        def structure(self, text, *, categories=()):
            with app.state.sessions() as db:
                capture = db.scalar(select(Capture).where(Capture.input_kind == "audio"))
                assert capture.transcript == capture.original_text == text == DEMO_TEXT
                assert (app.state.audio_storage.root / capture.audio_key).read_bytes() == wav()
            inputs.append(text)
            return super().structure(text, categories=categories)

    monkeypatch.setattr(main, "Worker", TrackingWorker)
    app = app_factory(auto_worker=True, llm_provider=LLM(), speech_provider=speech)
    with TestClient(app) as client:
        register(client)
        assert len(workers) == 1
        reader = workers[0].audio_storage
        assert reader.read_only and reader.root == app.state.audio_storage.root
        with pytest.raises(AudioStorageUnavailable):
            reader._writable()
        saved = upload(client)
        assert saved.status_code == 202, saved.text
        job_id = saved.json()["id"]
        deadline = time.monotonic() + 5
        while True:
            state = client.get("/api/v1/jobs/" + job_id).json()
            if state["status"] in {"succeeded", "failed"} or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        assert state["status"] == "succeeded", state
        assert speech.calls == 1 and inputs == [DEMO_TEXT]
        with app.state.sessions() as db:
            capture = db.get(Capture, db.get(Job, job_id).capture_id)
            assert client.get(f"/api/v1/captures/{capture.id}/audio").content == wav()
        assert upload(client).json()["id"] == job_id
        assert speech.calls == 1


def test_process_worker_uses_configured_read_only_storage(app_factory, monkeypatch):
    app = app_factory()
    registered_workers = []

    class ProcessWorker:
        def __init__(self, sessions, settings, **dependencies):
            registered_workers.append((sessions, settings, dependencies))

        def run_once(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(worker.Settings, "from_env", lambda: app.state.settings)
    monkeypatch.setattr(worker, "Worker", ProcessWorker)
    worker.main()
    assert len(registered_workers) == 1
    _, settings, dependencies = registered_workers[0]
    assert settings is app.state.settings
    reader = dependencies["audio_storage"]
    assert reader.read_only and reader.root == app.state.audio_storage.root
    with pytest.raises(AudioStorageUnavailable):
        reader._writable()


def test_default_runtime_does_not_invent_transcription(app_factory):
    app = app_factory()
    with TestClient(app) as client:
        register(client)
        saved = upload(client)
        assert saved.status_code == 202, saved.text
        worker.Worker(app.state.sessions, app.state.settings, audio_storage=app.state.audio_storage).run_once()
        with app.state.sessions() as db:
            job = db.get(Job, saved.json()["id"])
            capture = db.get(Capture, job.capture_id)
            assert job.status == "failed" and job.error_code == "stt_not_configured"
            assert capture.transcript is None and capture.original_text == ""
            assert client.get(f"/api/v1/captures/{capture.id}/audio").content == wav()
