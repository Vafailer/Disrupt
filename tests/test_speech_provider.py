import io
from dataclasses import replace

import httpx
import pytest

from app.config import PROGRAM_BASE_URL, Settings
from app.providers import ProviderError, make_provider
from app.speech import CloudRuSpeechProvider, make_speech_provider


def candidate(handler):
    return CloudRuSpeechProvider("synthetic-secret", "synthetic-stt", base_url=PROGRAM_BASE_URL,
                                 transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("media,extension", [("audio/ogg", "ogg"), ("audio/wav", "wav"), ("audio/webm", "webm")])
def test_multipart_original_and_one_dispatch(media, extension):
    calls = []
    def handler(request):
        calls.append(request)
        assert str(request.url) == PROGRAM_BASE_URL + "/audio/transcriptions"
        assert request.headers["Authorization"] == "Bearer synthetic-secret"
        assert b"synthetic original bytes" in request.content
        assert ("recording." + extension).encode() in request.content
        assert b"synthetic-stt" in request.content
        return httpx.Response(200, json={"text": "Тестовая расшифровка"})
    source = io.BytesIO(b"synthetic original bytes")
    assert candidate(handler).transcribe(source, media_type=media) == "Тестовая расшифровка"
    assert source.getvalue() == b"synthetic original bytes" and len(calls) == 1


@pytest.mark.parametrize("status,code", [(302, "stt_http_302"), (401, "stt_auth"), (429, "stt_rate_limit"),
                                        (503, "stt_unavailable")])
def test_no_redirect_retry_or_sensitive_error(status, code):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="private provider response", headers={"Location": "https://evil.invalid"})
    with pytest.raises(ProviderError, match=code) as error:
        candidate(handler).transcribe(io.BytesIO(b"fixture"), media_type="audio/ogg")
    assert len(calls) == 1 and "private" not in str(error.value)


@pytest.mark.parametrize("text", [None, "", " ", "a\x00b", "a" * 12001])
def test_invalid_transcript(text):
    with pytest.raises(ProviderError, match="stt_invalid_response"):
        candidate(lambda _: httpx.Response(200, json={"text": text})).transcribe(io.BytesIO(b"fixture"), media_type="audio/wav")


def test_timeout_no_retry():
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("sensitive details")
    with pytest.raises(ProviderError, match="stt_timeout_unknown"):
        candidate(handler).transcribe(io.BytesIO(b"fixture"), media_type="audio/webm")
    assert len(calls) == 1


def test_disabled_providers_do_not_read_secret(monkeypatch):
    settings = Settings(provider="cloudru", auto_worker=False, cloudru_model="synthetic-model",
                        live_call_limit=1, live_user_call_limit=1)
    monkeypatch.setenv("NOTES_CLOUDRU_API_KEY_FILE", "/must-not-be-read")
    with pytest.raises(ValueError, match="disabled"):
        make_provider(settings)
    assert make_speech_provider(settings) is None
    monkeypatch.setenv("NOTES_STT_ENABLED", "true")
    assert make_speech_provider(Settings()) is None
    with pytest.raises(ValueError, match="permission"):
        make_speech_provider(settings)
    with pytest.raises(ValueError, match="protocol"):
        make_speech_provider(replace(settings, allow_live_requests=True))


@pytest.mark.parametrize("limit,expected", [(1, "failed"), (2, "succeeded")])
def test_stt_and_llm_have_separate_durable_slots(app, client, limit, expected):
    from sqlalchemy import select

    from app.models import Capture, Job, ProviderBudget, ProviderUsage
    from app.providers import DEMO_TEXT, MockProvider
    from app.worker import Worker
    from tests.test_audio_core import Reader, enqueue

    job_id, capture_id, key = enqueue(app, client)
    settings = replace(app.state.settings, provider="cloudru", allow_live_requests=True,
                       cloudru_model="synthetic-model", live_call_limit=limit, live_user_call_limit=limit)
    with app.state.sessions() as db:
        db.get(Job, job_id).provider = "cloudru"
        db.commit()
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"text": DEMO_TEXT})
    worker = Worker(app.state.sessions, settings, provider=MockProvider(), audio_storage=Reader(key),
                    speech_provider=candidate(handler))
    assert worker.run_once() and not worker.run_once()
    with app.state.sessions() as db:
        assert db.get(Job, job_id).status == expected
        assert db.get(Capture, capture_id).transcript == DEMO_TEXT
        assert db.get(ProviderBudget, "cloudru").reserved_calls == limit
        receipt = db.scalar(select(ProviderUsage).where(ProviderUsage.kind == "stt"))
        assert receipt.model == "synthetic-stt" and receipt.status == "succeeded"
        if limit == 1:
            assert db.get(Job, job_id).error_code == "budget_exhausted"
    assert len(calls) == 1


def test_invalid_audio_and_large_response_are_bounded():
    def forbidden(_):
        raise AssertionError("must reject audio before HTTP")
    for audio, media in [(b"", "audio/wav"), (b"fixture", "video/mp4"), (b"x" * (10 * 1024 * 1024 + 1), "audio/ogg")]:
        with pytest.raises(ProviderError, match="audio_invalid"):
            candidate(forbidden).transcribe(io.BytesIO(audio), media_type=media)
    with pytest.raises(ProviderError, match="stt_response_too_large"):
        candidate(lambda _: httpx.Response(200, content=b"x" * 256001)).transcribe(io.BytesIO(b"fixture"), media_type="audio/wav")
