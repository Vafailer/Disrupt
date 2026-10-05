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


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="Local PostgreSQL is not configured")
def test_postgresql_internal_link_concurrent_updates_and_manual(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace

    from sqlalchemy import func, select

    from app.models import Capture, Inbox, Job, ProductEvent

    url = os.environ["TEST_POSTGRES_URL"]
    assert make_url(url).host in {"127.0.0.1", "localhost", "::1"}
    monkeypatch.setenv("NOTES_DATABASE_URL", url)
    monkeypatch.setenv("NOTES_PROVIDER", "mock")
    monkeypatch.setenv("NOTES_ALLOW_LIVE_REQUESTS", "false")
    command.upgrade(Config("alembic.ini"), "head")
    command.check(Config("alembic.ini"))
    secret = "postgres-test-service-" + "a" * 32
    settings = replace(Settings(database_url=url, auto_worker=False), internal_api_token=secret)
    app = create_app(settings)
    base = {"bot_id": 10**12 + uuid.uuid4().int % 10**12, "telegram_user_id": 2**40, "chat_id": 2**40}
    headers = {"Authorization": "Bearer " + secret}
    try:
        with TestClient(app) as client:
            owner = register(client, "pg_internal_" + uuid.uuid4().hex)
            code = client.post("/api/v1/telegram/link-code").json()
            pending = client.post(
                "/internal/v1/telegram/link-request",
                headers=headers,
                json={**base, "update_id": 1, "code": code["code"]},
            )
            assert pending.status_code == 200
            assert (
                client.post("/api/v1/telegram/links/" + code["link_request_id"] + "/confirm").status_code
                == 200
            )
            payload = {**base, "update_id": 2, "text": "Exact original", "processing_mode": "manual"}

            def send(_):
                return client.post("/internal/v1/telegram/updates", headers=headers, json=payload)

            with ThreadPoolExecutor(max_workers=4) as pool:
                responses = list(pool.map(send, range(4)))
            assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
            assert len({r.json()["capture_id"] for r in responses}) == 1
            assert responses[0].json()["job_id"] is None
            with app.state.sessions() as db:
                for model, condition in [
                    (Capture, Capture.user_id == owner["id"]),
                    (
                        ProductEvent,
                        (ProductEvent.user_id == owner["id"]) & (ProductEvent.name == "capture_saved"),
                    ),
                ]:
                    assert db.scalar(select(func.count()).select_from(model).where(condition)) == 1
                assert db.scalar(select(func.count()).select_from(Job).where(Job.user_id == owner["id"])) == 0
                assert (
                    db.scalar(select(func.count()).select_from(Inbox).where(Inbox.bot_id == base["bot_id"]))
                    == 2
                )
    finally:
        app.state.engine.dispose()
