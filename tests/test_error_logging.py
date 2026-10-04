import json

from fastapi.testclient import TestClient

from app.error_logging import log_error
from app.providers import DEMO_TEXT
from app.worker import Worker
from tests.conftest import register


def test_error_log_omits_exception_message(tmp_path):
    path = tmp_path / "errors.log"
    log_error(
        "job_failed",
        "provider_timeout_unknown",
        log_file=str(path),
        job_id="job-123",
        http_status=504,
        exception=RuntimeError("secret-key and private note"),
    )

    raw = path.read_text(encoding="utf-8")
    record = json.loads(raw)
    assert record["code"] == "provider_timeout_unknown"
    assert record["job_id"] == "job-123"
    assert record["http_status"] == 504
    assert record["exception_type"] == "RuntimeError"
    assert "secret-key" not in raw
    assert "private note" not in raw


def test_worker_failure_writes_safe_record(app_factory, tmp_path):
    class BrokenProvider:
        def structure(self, text):
            raise RuntimeError("private note and secret-key")

    path = tmp_path / "errors.log"
    app = app_factory(error_log_file=str(path))
    with TestClient(app) as client:
        register(client)
        response = client.post(
            "/api/v1/captures/text",
            json={"text": DEMO_TEXT},
            headers={"Idempotency-Key": "logging-test"},
        )
        job_id = response.json()["id"]
        Worker(app.state.sessions, app.state.settings, BrokenProvider()).run_once()
        assert client.get(f"/api/v1/jobs/{job_id}").json()["error_code"] == "internal_error"

    raw = path.read_text(encoding="utf-8")
    assert json.loads(raw)["job_id"] == job_id
    assert json.loads(raw)["source"].startswith("test_error_logging.py:")
    assert "internal_error" in raw
    assert "secret-key" not in raw
    assert DEMO_TEXT not in raw


def test_api_failure_returns_log_id_without_exception_text(app_factory, tmp_path):
    path = tmp_path / "errors.log"
    app = app_factory(error_log_file=str(path))

    @app.get("/test-internal-error")
    def broken_route():
        raise RuntimeError("secret-key and private note")

    with TestClient(app) as client:
        response = client.get("/test-internal-error")

    assert response.status_code == 500
    request_id = response.json()["error_id"]
    raw = path.read_text(encoding="utf-8")
    record = json.loads(raw)
    assert record["request_id"] == request_id
    assert record["event"] == "request_failed"
    assert record["exception_type"] == "RuntimeError"
    assert "secret-key" not in raw + response.text
    assert "private note" not in raw + response.text
