import socket
import threading
import time
from dataclasses import replace

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import totp
from app.admin_models import AdminAccount
from app.config import Settings
from app.main import create_app
from app.policy import POLICY_VERSION
from app.security import hash_password

# A client inside the default private network. The default TestClient address ("testclient") is outside it.
ADMIN_ADDRESS = ("10.77.0.5", 50000)
ADMIN_PASSWORD = "synthetic-admin-password-1"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    # Mock tests must not initialize a workstation's proxy or require its SOCKS extras.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)

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
    # Tests talk to the app over plain HTTP, so the admin cookie cannot be Secure here.
    settings = Settings(
        database_url=database_url, auto_worker=False, audio_storage_path=str(tmp_path / "audio"),
        admin_cookie_secure=False,
    )
    apps = []

    def factory(*, llm_provider=None, audio_storage=None, speech_provider=None, mailer=None, **changes):
        app = create_app(
            replace(settings, **changes), llm_provider, audio_storage=audio_storage, speech_provider=speech_provider,
            mailer=mailer,
        )
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
        "/api/v1/auth/register",
        json={
            "username": username, "password": "test-only-password-123",
            "accept_policy": True, "policy_version": POLICY_VERSION,
        },
    )
    assert response.status_code == 201, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response.json()


def make_admin(app, username="owner", *, confirmed=True, password=ADMIN_PASSWORD):
    """Create an administrator directly in the database. Returns its authenticator secret."""
    secret = totp.new_secret()
    with app.state.sessions() as db:
        db.add(AdminAccount(
            username=username, password_hash=hash_password(password), totp_secret=secret, totp_enabled=confirmed,
        ))
        db.commit()
    return secret


def admin_login(app, client, secret, username="owner", password=ADMIN_PASSWORD, *, code=None, fresh=True):
    """Sign in. `fresh` forgets the last used time step, so a test can sign in again within 30 seconds."""
    if fresh:
        with app.state.sessions() as db:
            account = db.scalar(select(AdminAccount).where(AdminAccount.username == username))
            if account is not None:
                account.last_totp_step = None
                db.commit()
    response = client.post("/admin-api/v1/login", json={
        "username": username, "password": password, "totp": code or totp.code_at(secret, time.time()),
    })
    if response.status_code == 200:
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response


@pytest.fixture
def private_client(app):
    with TestClient(app, client=ADMIN_ADDRESS) as client:
        yield client


@pytest.fixture
def admin_secret(app):
    return make_admin(app)


@pytest.fixture
def admin_client(app, private_client, admin_secret):
    """A private-network client signed in as the administrator `owner`."""
    response = admin_login(app, private_client, admin_secret)
    assert response.status_code == 200, response.text
    return private_client
