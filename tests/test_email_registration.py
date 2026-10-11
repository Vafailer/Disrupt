"""Email registration uses fake mail only; no external SMTP or model requests."""

import re
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.mailer import MailError
from app.models import EmailRegistrationPending as Pending
from app.models import ProviderUsage, User
from app.policy import POLICY_VERSION

PASSWORD = "synthetic-email-password-1"
BASE = "/api/v1/auth/email"


class Mail:
    enabled = True

    def __init__(self):
        self.sent = []
        self.fail = False

    def send(self, to, subject, body):
        if self.fail:
            raise MailError("synthetic transport failure")
        self.sent.append((to, subject, body))


@pytest.fixture
def stack(app_factory):
    mail = Mail()
    app = app_factory(mailer=mail, email_registration_enabled=True)
    with TestClient(app) as client:
        yield app, client, mail


def begin(client, email="owner@example.test"):
    return client.post(BASE + "/registration/start", json={
        "email": email, "password": PASSWORD, "accept_policy": True, "policy_version": POLICY_VERSION,
        "confirm_age": True,
    })


def code(mail):
    return re.search(r"код регистрации: ([0-9]{6})", mail.sent[-1][2]).group(1)


def confirm(client, registration_id, value):
    return client.post(BASE + "/registration/confirm", json={"registration_id": registration_id, "code": value})


def test_account_exists_only_after_code_and_both_logins_use_same_id(stack):
    app, client, mail = stack
    started = begin(client, "Owner@Example.Test")
    assert started.status_code == 202
    challenge = started.json()["registration_id"]
    assert "httponly" in started.headers["set-cookie"].lower()
    assert code(mail) not in started.text and PASSWORD not in started.text
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 0
        row = db.get(Pending, challenge)
        assert row.email == "owner@example.test"
        assert row.password_hash.startswith("scrypt$") and PASSWORD not in row.password_hash
        assert row.code_hash != code(mail) and row.binding_hash != client.cookies.get("notes_email_registration")
        assert db.scalar(select(func.count()).select_from(ProviderUsage)) == 0
    result = confirm(client, challenge, code(mail))
    assert result.status_code == 201
    user_id = result.json()["id"]
    username = result.json()["username"]
    with app.state.sessions() as db:
        user = db.get(User, user_id)
        assert user.email == "owner@example.test" and user.email_verified_at is not None
        row = db.get(Pending, challenge)
        assert row.used_at and not row.code_hash and not row.binding_hash and not row.password_hash
    assert confirm(client, challenge, code(mail)).status_code == 400
    login = client.post(BASE + "/login", json={"email": "OWNER@example.test", "password": PASSWORD})
    assert login.status_code == 200 and login.json()["id"] == user_id
    legacy_login = client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD})
    assert legacy_login.status_code == 200 and legacy_login.json()["id"] == user_id


def test_other_browser_cannot_consume_code(stack):
    app, client, mail = stack
    challenge = begin(client).json()["registration_id"]
    with TestClient(app) as other:
        assert confirm(other, challenge, code(mail)).status_code == 400
    assert confirm(client, challenge, code(mail)).status_code == 201


def test_leading_zero_code_is_supported(stack, monkeypatch):
    monkeypatch.setattr("app.routes.email_auth.secrets.randbelow", lambda maximum: 1)
    _, client, mail = stack
    challenge = begin(client).json()["registration_id"]
    assert code(mail) == "000001"
    assert confirm(client, challenge, code(mail)).status_code == 201


def test_five_wrong_attempts_retire_challenge(stack):
    app, client, mail = stack
    challenge = begin(client).json()["registration_id"]
    wrong = "000000" if code(mail) != "000000" else "111111"
    for _ in range(5):
        assert confirm(client, challenge, wrong).status_code == 400
    assert confirm(client, challenge, code(mail)).status_code == 400
    with app.state.sessions() as db:
        row = db.get(Pending, challenge)
        assert row.attempts == 5 and row.used_at and not row.password_hash
        assert db.scalar(select(func.count()).select_from(User)) == 0


def test_expired_code_cannot_register(stack):
    app, client, mail = stack
    challenge = begin(client).json()["registration_id"]
    with app.state.sessions() as db:
        db.get(Pending, challenge).expires_at = time.time() - 1
        db.commit()
    assert confirm(client, challenge, code(mail)).status_code == 400


def test_resend_waits_and_retires_old_code(stack):
    app, client, mail = stack
    first = begin(client).json()["registration_id"]
    old_code = code(mail)
    assert begin(client).status_code == 429 and len(mail.sent) == 1
    with app.state.sessions() as db:
        db.get(Pending, first).created_at = time.time() - 61
        db.commit()
    second = begin(client)
    assert second.status_code == 202 and len(mail.sent) == 2
    assert confirm(client, first, old_code).status_code == 400
    assert confirm(client, second.json()["registration_id"], code(mail)).status_code == 201


def test_failed_mail_never_creates_or_confirms_account(stack):
    app, client, mail = stack
    mail.fail = True
    result = begin(client)
    assert result.status_code == 503 and "synthetic" not in result.text
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 0
        row = db.scalar(select(Pending))
        assert row.used_at and not row.code_hash and not row.password_hash


def test_existing_email_is_never_replaced(stack):
    app, client, mail = stack
    challenge = begin(client).json()["registration_id"]
    owner = confirm(client, challenge, code(mail)).json()["id"]
    with app.state.sessions() as db:
        db.get(Pending, challenge).created_at = time.time() - 61
        db.commit()
    repeated = begin(client)
    assert repeated.status_code == 202 and len(mail.sent) == 1
    assert confirm(client, repeated.json()["registration_id"], code(mail)).status_code == 400
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 1
        assert db.get(User, owner).email == "owner@example.test"


def test_disabled_feature_and_disabled_mail_send_nothing(app_factory):
    mail = Mail()
    app = app_factory(mailer=mail)
    with TestClient(app) as client:
        assert client.get(BASE + "/options").json() == {"login_enabled": False, "registration_enabled": False}
        assert begin(client).status_code == 503
    mail.enabled = False
    app = app_factory(mailer=mail, email_registration_enabled=True)
    with TestClient(app) as client:
        assert client.get(BASE + "/options").json() == {"login_enabled": True, "registration_enabled": False}
        assert begin(client).status_code == 503
    assert mail.sent == []


def test_username_registration_cannot_bypass_email_confirmation(stack):
    _, client, _ = stack
    result = client.post("/api/v1/auth/register", json={
        "username": "unverified", "password": PASSWORD, "accept_policy": True, "policy_version": POLICY_VERSION,
        "confirm_age": True,
    })
    assert result.status_code == 403


def test_registration_checks_consent_and_origin(stack):
    _, client, mail = stack
    payload = {"email": "owner@example.test", "password": PASSWORD}
    assert client.post(BASE + "/registration/start", json=payload).status_code == 422
    payload.update(accept_policy=True, policy_version=POLICY_VERSION)
    assert client.post(BASE + "/registration/start", json=payload).status_code == 422  # No age confirmation
    payload.update(confirm_age=True)
    assert client.post(BASE + "/registration/start", json=payload, headers={"Origin": "https://evil.invalid"}).status_code == 403
    assert mail.sent == []
