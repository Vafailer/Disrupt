import socket
import threading
from dataclasses import replace

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Real network access is forbidden in tests")

    # Windows implements asyncio's internal socketpair over loopback TCP.
    # Allow only that scoped internal call, never general loopback/provider traffic.
    original_pair, original_connect = socket.socketpair, socket.socket.connect
    internal = threading.local()

    def socketpair(*args, **kwargs):
        internal.allowed = True
        try:
            return original_pair(*args, **kwargs)
        finally:
            internal.allowed = False

    def connect(sock, address):
        if getattr(internal, "allowed", False) and address[0] in {"127.0.0.1", "::1"}:
            return original_connect(sock, address)
        return forbidden()

    monkeypatch.setattr(socket, "socketpair", socketpair)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)


@pytest.fixture
def app_factory(tmp_path, monkeypatch):
    database_url = "sqlite:///" + (tmp_path / "test.db").as_posix()
    monkeypatch.setenv("NOTES_DATABASE_URL", database_url)
    monkeypatch.setenv("NOTES_PROVIDER", "mock")
    monkeypatch.setenv("NOTES_ALLOW_LIVE_REQUESTS", "false")
    command.upgrade(Config("alembic.ini"), "head")
    settings = Settings(database_url=database_url, auto_worker=False)
    apps = []

    def factory(**changes):
        app = create_app(replace(settings, **changes))
        apps.append(app)
        return app

    yield factory
    for app in apps:
        app.state.engine.dispose()


@pytest.fixture
def app(app_factory):
    return app_factory()


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


def register(client, username="tester"):
    response = client.post(
        "/api/v1/auth/register", json={"username": username, "password": "test-only-password-123"}
    )
    assert response.status_code == 201, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response.json()
