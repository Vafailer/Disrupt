import signal
from types import SimpleNamespace

import pytest

from app import worker
from app.config import Settings


@pytest.mark.parametrize("termination_signal", [signal.SIGTERM, signal.SIGINT])
def test_shutdown_finishes_current_attempt_without_claiming_another(monkeypatch, termination_signal):
    calls, handlers = [], {}
    def old_handler(*args):
        pass
    def register(sig, handler):
        handlers[sig] = handler
        return old_handler
    class ActiveWorker:
        def __init__(self, *args, **kwargs):
            pass
        def budget_available(self):
            return True
        def run_once(self):
            calls.append("attempt_started")
            handlers[termination_signal](termination_signal, None)
            calls.append("attempt_finished")
            return True
    monkeypatch.setattr(worker.Settings, "from_env", lambda: Settings())
    monkeypatch.setattr(worker, "make_database", lambda _: (SimpleNamespace(dispose=lambda: calls.append("closed")), None))
    monkeypatch.setattr(worker, "AudioStorage", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "make_speech_provider", lambda _: None)
    monkeypatch.setattr(worker, "Worker", ActiveWorker)
    monkeypatch.setattr(worker.signal, "signal", register)
    worker.main()
    assert calls == ["attempt_started", "attempt_finished", "closed"]
    assert handlers[signal.SIGTERM] is old_handler and handlers[signal.SIGINT] is old_handler
