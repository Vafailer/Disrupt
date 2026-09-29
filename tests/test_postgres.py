"""Optional smoke test against an isolated, local CI PostgreSQL database."""

import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

from app.config import Settings
from app.main import create_app
from app.worker import Worker
from tests.conftest import register
from tests.test_notes import submit


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="Local PostgreSQL is not configured")
def test_postgresql_migration_and_note_roundtrip(monkeypatch):
    url = os.environ["TEST_POSTGRES_URL"]
    assert make_url(url).host in {"127.0.0.1", "localhost", "::1"}
    monkeypatch.setenv("NOTES_DATABASE_URL", url)
    monkeypatch.setenv("NOTES_PROVIDER", "mock")
    monkeypatch.setenv("NOTES_ALLOW_LIVE_REQUESTS", "false")
    command.upgrade(Config("alembic.ini"), "head")
    command.check(Config("alembic.ini"))
    settings = Settings(database_url=url, auto_worker=False)
    app = create_app(settings)
    with TestClient(app) as client:
        register(client, "pg_" + uuid.uuid4().hex)
        job = submit(client).json()
        assert Worker(app.state.sessions, settings).run_once()
        result = client.get("/api/v1/jobs/" + job["id"]).json()
        assert result["status"] == "succeeded"
        path = "/api/v1/notes/" + result["note_id"]
        assert (
            client.patch(path, json={"version": 1, "title": "Правка", "markdown": "## Мысль"}).status_code
            == 200
        )
        assert client.patch(path, json={"version": 1, "title": "Старая", "markdown": "x"}).status_code == 409
