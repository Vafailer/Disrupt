"""Dispatch receipts with synthetic providers and HTTP substitutions only."""

import json
import time
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import select

from app.models import Capture, Job, ProductEvent, ProviderUsage
from app.providers import DEMO_TEXT, CloudRuProvider
from app.speech import MockSpeechProvider
from app.usage import Tokens, response_tokens
from app.worker import Worker
from tests.conftest import register
from tests.test_audio_core import Reader, create
from tests.test_notes import submit


def test_mock_dispatch_receipt_is_committed_before_call_and_never_repeated(app, client):
    account = register(client)
    job = submit(client).json()

    class Provider:
        def structure(self, text, *, categories=()):
            from app.providers import MockProvider
            with app.state.sessions() as db:
                receipt = db.scalar(select(ProviderUsage))
                assert receipt.status == "unknown" and receipt.operation_id == job["id"]
                assert db.get(Capture, db.get(Job, job["id"]).capture_id).original_text == text
            return MockProvider().structure(text, categories=categories)

    worker = Worker(app.state.sessions, app.state.settings, Provider())
    assert worker.run_once() and not worker.run_once()
    with app.state.sessions() as db:
        receipt = db.scalar(select(ProviderUsage))
        event = db.scalar(select(ProductEvent).where(ProductEvent.user_id == account["id"], ProductEvent.name == "capture_saved"))
        assert receipt.session_id == event.session_id and receipt.channel == "web"
        assert receipt.status == "succeeded" and receipt.cost is None
        assert receipt.latency_ms >= 0
        assert len(list(db.scalars(select(ProviderUsage)))) == 1


def test_builtin_mock_explicitly_has_no_billing_not_fabricated_live_tokens(app, client):
    register(client)
    submit(client)
    assert Worker(app.state.sessions, app.state.settings).run_once()
    with app.state.sessions() as db:
        receipt = db.scalar(select(ProviderUsage))
        assert receipt.model == "mock" and receipt.tariff_version == "mock-no-billing-v1"
        assert receipt.cost == 0 and receipt.input_tokens == receipt.output_tokens == receipt.cache_tokens == 0


@pytest.mark.parametrize("invalid", [False, True])
def test_transport_usage_saved_even_when_model_result_is_invalid(app, client, invalid):
    register(client)
    submit(client)
    calls = []

    def transport(request):
        calls.append(request)
        data = {"usage": {"prompt_tokens": 120, "completion_tokens": 60, "prompt_tokens_details": {"cached_tokens": 20}},
                "choices": [{"finish_reason": "stop", "message": {"content": "{}" if invalid else json.dumps({
                    "title": "Тест", "markdown": "Тест", "conclusions": [], "items": []})}}]}
        return httpx.Response(200, json=data)

    settings = replace(app.state.settings, cloudru_model="synthetic-model")
    provider = CloudRuProvider("fake-test-key", "synthetic-model", transport=httpx.MockTransport(transport))
    assert Worker(app.state.sessions, settings, provider).run_once()
    assert len(calls) == 1
    with app.state.sessions() as db:
        receipt = db.scalar(select(ProviderUsage))
        assert (receipt.input_tokens, receipt.output_tokens, receipt.cache_tokens) == (120, 60, 20)
        assert receipt.cost is None and receipt.estimated_cost is None and receipt.tariff_version is None
        assert receipt.status == ("failed" if invalid else "succeeded")
        assert receipt.model == "synthetic-model"


def test_timeout_receipt_keeps_unknown_result_without_automatic_retry(app, client):
    register(client)
    submit(client)
    calls = []

    def transport(request):
        calls.append(1)
        raise httpx.ReadTimeout("SYNTHETIC PRIVATE ERROR", request=request)

    provider = CloudRuProvider("fake-test-key", "test-model", transport=httpx.MockTransport(transport))
    worker = Worker(app.state.sessions, app.state.settings, provider)
    assert worker.run_once() and not worker.run_once() and calls == [1]
    with app.state.sessions() as db:
        receipt = db.scalar(select(ProviderUsage))
        assert receipt.status == "unknown" and receipt.input_tokens is None and receipt.cost is None
        assert db.scalar(select(Job.error_code)) == "provider_timeout_unknown"


def test_crash_after_receipt_does_not_re_dispatch_after_lease(app, client):
    register(client)
    job = submit(client).json()
    worker = Worker(app.state.sessions, app.state.settings)
    assert worker.claim() == job["id"]
    row_id = worker.usage.start(job["id"], "llm", worker.provider)
    with app.state.sessions() as db:
        db.get(Job, job["id"]).lease_until = time.time() - 1
        db.commit()
    assert not worker.run_once()
    with app.state.sessions() as db:
        assert db.get(ProviderUsage, row_id).status == "unknown"
        assert db.get(Job, job["id"]).error_code == "execution_unknown"


def test_no_stt_configuration_does_not_create_fake_llm_or_stt_call(app, client):
    account = register(client)
    with app.state.sessions() as db:
        job = create(db, account["id"], app.state.settings)
        db.commit()
        job_id = job.id
    assert Worker(app.state.sessions, app.state.settings).run_once()
    with app.state.sessions() as db:
        assert db.get(Job, job_id).error_code == "stt_not_configured"
        assert list(db.scalars(select(ProviderUsage))) == []


def test_stt_and_llm_separate_receipts_preserve_transcript(app, client):
    account = register(client)
    with app.state.sessions() as db:
        job = create(db, account["id"], app.state.settings, channel="telegram")
        db.commit()
        key, job_id, capture_id = db.get(Capture, job.capture_id).audio_key, job.id, job.capture_id
    speech = MockSpeechProvider(DEMO_TEXT)
    assert Worker(app.state.sessions, app.state.settings, audio_storage=Reader(key), speech_provider=speech).run_once()
    with app.state.sessions() as db:
        receipts = {row.kind: row for row in db.scalars(select(ProviderUsage))}
        assert receipts.keys() == {"stt", "llm"}
        assert receipts["stt"].stt_seconds == 1.0 and receipts["stt"].cost == 0
        assert all(row.operation_id == job_id and row.channel == "telegram" for row in receipts.values())
        assert db.get(Capture, capture_id).transcript == DEMO_TEXT and speech.calls == 1


@pytest.mark.parametrize("usage,expected", [
    (None, Tokens()),
    ({"prompt_tokens": True, "completion_tokens": -1}, Tokens()),
    ({"prompt_tokens": 1.5, "completion_tokens": "2"}, Tokens()),
    ({"prompt_tokens": 10, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 11}}, Tokens(10, 2, None)),
    ({"prompt_tokens": 10, "completion_tokens": 2}, Tokens(10, 2, None)),
    ({"prompt_tokens": 10, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 0}}, Tokens(10, 2, 0)),
    ({"prompt_tokens": 2**31}, Tokens()),
])
def test_missing_and_invalid_usage_fields_remain_unknown(usage, expected):
    assert response_tokens({"usage": usage}) == expected
    assert response_tokens([]) == Tokens()
