import io
from contextlib import contextmanager
from dataclasses import replace

import pytest
from sqlalchemy import func, select

from app.models import ProviderBudget, ProviderUsage, User
from app.providers import ProviderError
from app.stt_readiness import main
from tests.test_audio_core import enqueue


def prepare(app, client, monkeypatch, *, error=None, limit=20):
    _, capture_id, _ = enqueue(app, client)
    settings = replace(app.state.settings, provider="cloudru", allow_live_requests=True,
                       cloudru_model="synthetic-model", live_call_limit=100, live_user_call_limit=limit)
    monkeypatch.setattr("app.stt_readiness.Settings.from_env", lambda: settings)
    monkeypatch.setattr("app.stt_readiness.make_database", lambda _: (app.state.engine, app.state.sessions))
    @contextmanager
    def original(_):
        yield io.BytesIO(b"synthetic-original")
    monkeypatch.setattr("app.stt_readiness.AudioStorage", lambda *args, **kwargs: type("Reader", (), {
        "open_original": staticmethod(original),
    })())
    monkeypatch.setattr("app.stt_readiness.normalized_wav", lambda data, _: b"synthetic-WAV-copy")
    monkeypatch.setenv("NOTES_CLOUDRU_API_KEY_FILE", "/synthetic-secret-file")
    monkeypatch.setattr("app.stt_readiness.read_secret_file", lambda _: "synthetic-private-key")
    calls = []
    class FakeProvider:
        def __init__(self, key, model, **kwargs):
            assert key == "synthetic-private-key"
        def transcribe(self, source, *, media_type):
            assert source.read() == b"synthetic-WAV-copy" and media_type == "audio/wav"
            calls.append(1)
            # Reservation and unknown receipt must exist before any dispatch.
            with app.state.sessions() as db:
                assert db.get(ProviderBudget, "cloudru").reserved_calls == 1
                assert db.scalar(select(ProviderUsage)).status == "unknown"
            if error:
                raise ProviderError(error)
            return "private synthetic transcript"
    monkeypatch.setattr("app.stt_readiness.CloudRuSpeechProvider", FakeProvider)
    monkeypatch.setattr("sys.argv", ["stt_readiness", "--capture-id", capture_id,
                                     "--model", "synthetic-stt", "--allow-live-probe"])
    return calls


@pytest.mark.parametrize("error,status", [(None,"succeeded"), ("stt_http_400","failed"),
                                        ("stt_timeout_unknown","unknown")])
def test_probe_accounting_is_durable_and_no_key_or_transcript_is_printed(app, client, monkeypatch, capsys, error, status):
    calls = prepare(app, client, monkeypatch, error=error)
    assert main() == (0 if error is None else 1)
    with app.state.sessions() as db:
        assert db.scalar(select(ProviderUsage)).status == status
        assert db.get(ProviderBudget, "cloudru").reserved_calls == 1
        assert db.scalar(select(User)).live_calls == 1
    with pytest.raises(SystemExit, match="automatic repeat refused"):
        main()
    output = capsys.readouterr()
    assert "private" not in output.out + output.err and len(calls) == 1


def test_probe_at_user_limit_rolls_back_receipt_and_global_counter(app, client, monkeypatch):
    calls = prepare(app, client, monkeypatch, limit=1)
    with app.state.sessions.begin() as db:
        db.scalar(select(User)).live_calls = 1
    with pytest.raises(ProviderError, match="budget_exhausted"):
        main()
    with app.state.sessions() as db:
        assert db.get(ProviderBudget, "cloudru").reserved_calls == 0
        assert db.scalar(select(func.count()).select_from(ProviderUsage)) == 0
    assert calls == []
