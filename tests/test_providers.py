import json
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import select

from app.config import Settings
from app.models import Job, ProviderBudget, User
from app.providers import CloudRuProvider, MockProvider, ProviderError, make_provider
from app.worker import Worker
from tests.conftest import register
from tests.test_notes import submit

RESULT = {"title": "Мысль", "markdown": "## Мысль\n\nТекст", "conclusions": []}


def completion(content=None, finish="stop"):
    return {"choices": [{"finish_reason": finish, "message": {"content": json.dumps(content or RESULT)}}]}


def test_cloudru_contract_uses_fake_transport_only():
    requests = []

    def handler(request):
        requests.append(request)
        assert str(request.url) == CloudRuProvider.endpoint
        assert request.headers["Authorization"] == "Bearer fake-test-key"
        body = json.loads(request.content)
        assert body["model"] == "test-model"
        assert body["messages"][1] == {"role": "user", "content": "Мысль"}
        assert body["max_tokens"] == 4000
        assert body["response_format"] == {"type": "json_object"}
        return httpx.Response(200, json=completion())

    provider = CloudRuProvider("fake-test-key", "test-model", transport=httpx.MockTransport(handler))
    assert not requests  # Initialization does not discover models or validate credentials.
    assert provider.structure("Мысль").title == "Мысль"
    assert len(requests) == 1


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "provider_auth"),
        (403, "provider_auth"),
        (429, "provider_rate_limit"),
        (500, "provider_http_error"),
        (302, "provider_http_error"),
    ],
)
def test_cloud_errors_sanitized_and_not_retried(status, code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, text="sensitive-provider-error", headers={"Location": "https://evil.example"}
        )

    provider = CloudRuProvider("fake-test-key", "test-model", transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as exc:
        provider.structure("Мысль")
    assert exc.value.code == code
    assert "sensitive" not in str(exc.value)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "payload,code",
    [
        ({"choices": []}, "provider_invalid_response"),
        (completion({**RESULT, "user_id": "attacker"}), "provider_invalid_response"),
        (completion(finish="length"), "provider_incomplete_response"),
        (
            completion(
                {**RESULT, "conclusions": [{"text": "Вывод", "source_quote": "не существующая цитата"}]}
            ),
            "ungrounded_quote",
        ),
    ],
)
def test_invalid_model_output(payload, code):
    provider = CloudRuProvider(
        "fake-test-key",
        "test-model",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )
    with pytest.raises(ProviderError) as exc:
        provider.structure("Мысль")
    assert exc.value.code == code


def test_timeout_is_ambiguous_and_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ReadTimeout("secret in exception", request=request)

    provider = CloudRuProvider("fake-test-key", "test-model", transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError, match="provider_timeout_unknown"):
        provider.structure("Текст")
    assert len(calls) == 1


def test_response_size_limit():
    provider = CloudRuProvider(
        "fake-test-key",
        "test-model",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 256001)),
    )
    with pytest.raises(ProviderError, match="provider_response_too_large"):
        provider.structure("Текст")


def test_mock_does_not_read_any_real_key(monkeypatch):
    import os

    get = os.environ.get

    def guarded_get(name, default=None):
        if "KEY" in name or "TOKEN" in name:
            raise AssertionError("Credential must not be read in mock mode")
        return get(name, default)

    monkeypatch.setenv("NOTES_PROVIDER", "mock")
    monkeypatch.setattr(os.environ, "get", guarded_get)
    settings = Settings.from_env()
    assert isinstance(make_provider(settings), MockProvider)


def test_live_requires_opt_in_and_budgets():
    with pytest.raises(ValueError, match="Live mode requires"):
        Settings(provider="cloudru")
    with pytest.raises(ValueError, match="Live mode requires"):
        Settings(provider="cloudru", allow_live_requests=True, cloudru_model="test-model")


def test_budget_reservation_is_durable_and_capped(app, client):
    # Offline budget simulation: injected provider, no credential lookup or HTTP client.
    user = register(client)
    jobs = [submit(client, key=f"budget-{i}").json() for i in range(2)]
    with app.state.sessions() as db:
        for item in jobs:
            db.get(Job, item["id"]).provider = "cloudru"
        db.commit()
    settings = replace(
        app.state.settings,
        provider="cloudru",
        allow_live_requests=True,
        cloudru_model="test-model",
        live_call_limit=1,
        live_user_call_limit=1,
    )
    worker = Worker(app.state.sessions, settings, MockProvider())
    assert worker.run_once()
    assert worker.run_once()
    states = [client.get("/api/v1/jobs/" + j["id"]).json() for j in jobs]
    assert sorted(s["status"] for s in states) == ["failed", "succeeded"]
    assert next(s for s in states if s["status"] == "failed")["error_code"] == "budget_exhausted"
    with app.state.sessions() as db:
        assert db.get(ProviderBudget, "cloudru").reserved_calls == 1
        assert db.get(User, user["id"]).live_calls == 1


def test_mock_worker_does_not_consume_cloud_queue(app, client):
    register(client)
    job = submit(client).json()
    with app.state.sessions() as db:
        db.get(Job, job["id"]).provider = "cloudru"
        db.commit()
    assert not Worker(app.state.sessions, app.state.settings).run_once()
    with app.state.sessions() as db:
        assert db.scalar(select(Job)).status == "queued"


def test_real_transport_is_blocked_by_test_suite():
    with pytest.raises(AssertionError, match="network"):
        httpx.get("https://foundation-models.api.cloud.ru/v1/models")
