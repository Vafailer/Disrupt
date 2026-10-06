import hashlib
import io
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.integration import IntegrationRejection
from app.models import Capture, Inbox, Job, Note, ProductEvent, new_id
from app.providers import DEMO_TEXT, MockProvider, ProviderError
from app.routes.internal import process_update
from app.schemas import TelegramText
from app.services import capture_text, create_audio_capture
from app.speech import MockSpeechProvider
from app.worker import Worker
from tests.conftest import register

ORIGINAL = b"synthetic core fixture, not a decoder test"


@dataclass(frozen=True)
class Info:
    sha256: str = hashlib.sha256(ORIGINAL).hexdigest()
    byte_count: int = len(ORIGINAL)
    duration_seconds: float = 1.0
    media_type: str = "audio/ogg"


@dataclass(frozen=True)
class Stored:
    key: str
    info: Info = Info()


class Reader:
    def __init__(self, key):
        self.files = {key: ORIGINAL}
        self.opened = []

    @contextmanager
    def open_original(self, key):
        self.opened.append(key)
        with io.BytesIO(self.files[key]) as source:
            yield source


class SpyLLM(MockProvider):
    def __init__(self, error=None):
        self.inputs = []
        self.error = error

    def structure(self, text, *, categories=()):
        self.inputs.append(text)
        if self.error:
            raise ProviderError(self.error)
        return super().structure(text, categories=categories)


def create(db, user_id, settings, *, capture_id=None, info=Info(), key="audio-request", channel="web"):
    capture_id = capture_id or new_id()
    return create_audio_capture(
        db, capture_id=capture_id, user_id=user_id, idempotency_key=key,
        stored_audio=Stored(f"{capture_id}.audio", info), channel=channel, settings=settings,
    )


def enqueue(app, client, info=Info()):
    user = register(client)
    with app.state.sessions() as db:
        job = create(db, user["id"], app.state.settings, info=info)
        db.commit()
        return job.id, job.capture_id, f"{job.capture_id}.audio"


def count(db, model):
    statement = select(func.count()).select_from(model)
    if model is ProductEvent:
        statement = statement.where(ProductEvent.name == "capture_saved")
    return db.scalar(statement)


def test_audio_creation_uses_caller_transaction_and_preserves_published_id(app, client):
    user = register(client)
    capture_id = new_id()
    with app.state.sessions() as db:
        job = create(db, user["id"], app.state.settings, capture_id=capture_id, channel="telegram")
        assert job.capture_id == capture_id
        capture = db.get(Capture, capture_id)
        assert capture.input_kind == "audio" and capture.processing_mode == "ai"
        assert capture.original_text == "" and capture.transcript is None
        assert capture.audio_key == f"{capture_id}.audio" and capture.audio_sha256 == Info().sha256
        assert capture.audio_bytes == len(ORIGINAL) and capture.audio_media_type == "audio/ogg"
        assert count(db, ProductEvent) == 1
        db.rollback()
    with app.state.sessions() as db:
        assert count(db, Capture) == count(db, Job) == count(db, ProductEvent) == 0


def test_identical_repeat_survives_stt_and_conflicting_bytes_fail(app, client):
    job_id, capture_id, key = enqueue(app, client)
    Worker(
        app.state.sessions, app.state.settings, audio_storage=Reader(key),
        speech_provider=MockSpeechProvider(DEMO_TEXT),
    ).run_once()
    with app.state.sessions() as db:
        user_id = db.get(Job, job_id).user_id
        repeat = create(db, user_id, app.state.settings)
        assert repeat.id == job_id and repeat.capture_id == capture_id
        assert db.get(Capture, capture_id).original_text == DEMO_TEXT
        assert count(db, Capture) == count(db, Job) == count(db, ProductEvent) == 1
        with pytest.raises(IntegrationRejection) as error:
            create(db, user_id, app.state.settings, info=replace(Info(), sha256="0" * 64))
        assert error.value.status_code == 409 and error.value.code == "conflict"


@pytest.mark.parametrize("audio_first", [True, False])
def test_text_and_audio_cannot_share_idempotency_key(app, client, audio_first):
    user = register(client)
    with app.state.sessions() as db:
        if audio_first:
            create(db, user["id"], app.state.settings, key="shared")
            db.commit()
            with pytest.raises(HTTPException) as error:
                capture_text(db, user["id"], "", "shared", app.state.settings)
        else:
            capture_text(db, user["id"], "", "shared", app.state.settings)
            with pytest.raises(HTTPException) as error:
                create(db, user["id"], app.state.settings, key="shared")
        assert error.value.status_code == 409
        assert count(db, Capture) == count(db, Job) == 1


def test_same_key_is_scoped_to_owner(app, client):
    first = register(client, "first")
    second = register(client, "second")
    with app.state.sessions() as db:
        a = create(db, first["id"], app.state.settings)
        b = create(db, second["id"], app.state.settings)
        db.commit()
        assert a.id != b.id and count(db, Capture) == 2


def test_queue_limit_is_temporary_and_duplicate_bypasses_limit(app, client):
    user = register(client)
    settings = replace(app.state.settings, max_pending_per_user=1)
    with app.state.sessions() as db:
        first = create(db, user["id"], settings)
        db.commit()
        assert create(db, user["id"], settings).id == first.id
        with pytest.raises(HTTPException) as error:
            create(db, user["id"], settings, key="second")
        assert error.value.status_code == 429 and not isinstance(error.value, IntegrationRejection)
        db.rollback()
        assert count(db, Capture) == count(db, Job) == count(db, ProductEvent) == 1


@pytest.mark.parametrize(
    "info,status",
    [
        (replace(Info(), byte_count=0), 422),
        (replace(Info(), byte_count=True), 422),
        (replace(Info(), byte_count=10 * 1024 * 1024 + 1), 413),
        (replace(Info(), duration_seconds=180.001), 413),
        (replace(Info(), duration_seconds=0), 422),
        (replace(Info(), duration_seconds=float("nan")), 422),
        (replace(Info(), duration_seconds=float("inf")), 422),
        (replace(Info(), media_type="video/webm"), 422),
        (replace(Info(), sha256="bad"), 422),
    ],
)
def test_invalid_storage_metadata_creates_nothing(app, client, info, status):
    user = register(client)
    with app.state.sessions() as db:
        with pytest.raises(IntegrationRejection) as error:
            create(db, user["id"], app.state.settings, info=info)
        assert error.value.status_code == status
        assert count(db, Capture) == count(db, Job) == count(db, ProductEvent) == 0


@pytest.mark.parametrize("media_type", ["audio/ogg", "audio/wav", "audio/webm"])
def test_exact_limits_and_initial_formats_are_accepted(app, client, media_type):
    user = register(client)
    with app.state.sessions() as db:
        job = create(
            db, user["id"], app.state.settings,
            info=replace(Info(), byte_count=10 * 1024 * 1024, duration_seconds=180, media_type=media_type),
        )
        assert db.get(Capture, job.capture_id).audio_media_type == media_type


def test_storage_key_must_match_server_capture_uuid(app, client):
    user = register(client)
    with app.state.sessions() as db:
        with pytest.raises(IntegrationRejection) as error:
            create_audio_capture(
                db, capture_id=new_id(), user_id=user["id"], idempotency_key="key",
                stored_audio=Stored("../other.audio"), channel="web", settings=app.state.settings,
            )
        assert error.value.status_code == 422
        assert count(db, Capture) == 0


def test_inbox_refusal_rolls_back_audio_job_and_event_together(app, client):
    user = register(client)
    body = TelegramText(bot_id=10, update_id=20, telegram_user_id=30, chat_id=30, text="test")
    with app.state.sessions() as db:
        def refuse(_):
            create(db, user["id"], app.state.settings)
            raise IntegrationRejection(400, "unsupported_audio", "Некорректное аудио")

        with pytest.raises(IntegrationRejection):
            process_update(db, body, "voice", refuse)
    with app.state.sessions() as db:
        assert count(db, Inbox) == 1
        assert count(db, Capture) == count(db, Job) == count(db, ProductEvent) == 0


def test_worker_transcribes_once_before_llm_and_retains_original(app, client):
    job_id, capture_id, key = enqueue(app, client)
    storage, speech, llm = Reader(key), MockSpeechProvider(DEMO_TEXT), SpyLLM()
    worker = Worker(
        app.state.sessions, app.state.settings, llm, audio_storage=storage, speech_provider=speech,
    )
    assert worker.run_once() and not worker.run_once()
    assert speech.calls == 1 and llm.inputs == [DEMO_TEXT]
    assert storage.files[key] == ORIGINAL and storage.opened == [key]
    with app.state.sessions() as db:
        capture = db.get(Capture, capture_id)
        assert capture.transcript == capture.original_text == DEMO_TEXT and capture.transcript_version == 1
        assert capture.audio_key == key and capture.audio_sha256 == Info().sha256
        assert db.get(Job, job_id).status == "succeeded" and count(db, Note) == 1


def test_llm_failure_keeps_durable_transcript(app, client):
    job_id, capture_id, key = enqueue(app, client)
    storage = Reader(key)
    worker = Worker(
        app.state.sessions, app.state.settings, SpyLLM("provider_timeout_unknown"),
        audio_storage=storage, speech_provider=MockSpeechProvider(DEMO_TEXT),
    )
    assert worker.run_once() and not worker.run_once()
    with app.state.sessions() as db:
        assert db.get(Job, job_id).error_code == "provider_timeout_unknown"
        assert db.get(Capture, capture_id).transcript == DEMO_TEXT
        assert db.get(Capture, capture_id).original_text == DEMO_TEXT
        assert count(db, Note) == 0
    assert storage.files[key] == ORIGINAL


@pytest.mark.parametrize("mode", ["unconfigured", "failed", "missing_storage", "missing_file"])
def test_stt_failure_never_sends_empty_text_to_llm_or_removes_audio(app, client, mode):
    class Failing:
        def transcribe(self, source, *, media_type):
            raise ProviderError("stt_timeout_unknown")

    class Missing:
        def open_original(self, key):
            raise FileNotFoundError("private filename must not appear in logs")

    job_id, capture_id, key = enqueue(app, client)
    storage, llm = Reader(key), SpyLLM()
    actual_storage = {"missing_storage": None, "missing_file": Missing()}.get(mode, storage)
    speech = {"unconfigured": None, "failed": Failing()}.get(mode, MockSpeechProvider(DEMO_TEXT))
    worker = Worker(
        app.state.sessions, app.state.settings, llm, audio_storage=actual_storage, speech_provider=speech,
    )
    assert worker.run_once() and not worker.run_once()
    with app.state.sessions() as db:
        job = db.get(Job, job_id)
        assert job.status == "failed" and job.error_code == {
            "unconfigured": "stt_not_configured", "failed": "stt_timeout_unknown",
            "missing_storage": "audio_storage_unavailable", "missing_file": "audio_storage_unavailable",
        }[mode]
        capture = db.get(Capture, capture_id)
        assert capture.original_text == "" and capture.transcript is None and capture.audio_key == key
        assert count(db, Note) == 0
    assert llm.inputs == [] and storage.files[key] == ORIGINAL


@pytest.mark.parametrize("text", [None, " \n ", "a\x00b", "x" * 12001])
def test_invalid_stt_response_is_not_saved_or_structured(app, client, text):
    job_id, capture_id, key = enqueue(app, client)
    llm = SpyLLM()
    Worker(
        app.state.sessions, app.state.settings, llm, audio_storage=Reader(key),
        speech_provider=MockSpeechProvider(text),
    ).run_once()
    with app.state.sessions() as db:
        assert db.get(Job, job_id).error_code == "stt_invalid_response"
        assert db.get(Capture, capture_id).transcript is None
    assert llm.inputs == []


def test_late_stt_cannot_save_transcript_or_start_llm(app, client):
    job_id, capture_id, key = enqueue(app, client)

    class Late:
        def transcribe(self, source, *, media_type):
            with app.state.sessions() as db:
                db.get(Job, job_id).lease_until = time.time() - 1
                db.commit()
            return DEMO_TEXT

    llm = SpyLLM()
    Worker(
        app.state.sessions, app.state.settings, llm, audio_storage=Reader(key), speech_provider=Late(),
    ).run_once()
    with app.state.sessions() as db:
        assert db.get(Job, job_id).error_code == "execution_unknown"
        assert db.get(Capture, capture_id).transcript is None
        assert count(db, Note) == 0
    assert llm.inputs == []


def test_durable_transcript_is_reused_without_second_stt(app, client):
    job_id, capture_id, key = enqueue(app, client)
    with app.state.sessions() as db:
        capture = db.get(Capture, capture_id)
        capture.transcript = capture.original_text = DEMO_TEXT
        db.commit()
    storage, speech, llm = Reader(key), MockSpeechProvider("Should not be used"), SpyLLM()
    Worker(
        app.state.sessions, app.state.settings, llm, audio_storage=storage, speech_provider=speech,
    ).run_once()
    assert storage.opened == [] and speech.calls == 0 and llm.inputs == [DEMO_TEXT]
    with app.state.sessions() as db:
        assert db.get(Job, job_id).status == "succeeded"
