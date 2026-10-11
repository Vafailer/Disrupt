"""Age confirmation, daily sign-up limits per address and the AI ceiling of new accounts. Fakes only."""

import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.limits import daily_state, within_daily_limit
from app.models import LoginThrottle, User
from app.policy import POLICY_VERSION
from app.security import throttle
from tests.conftest import register
from tests.test_auth_telegram import confirm, poll, rows, start, token_of
from tests.test_email_registration import BASE as EMAIL
from tests.test_email_registration import PASSWORD, Mail, begin, code
from tests.test_email_registration import confirm as confirm_code
from tests.test_internal import SERVICE_TOKEN

AGE_TEXT = "18 лет"


def body(**changes):
    return {
        "username": "newuser", "password": "test-only-password-123",
        "accept_policy": True, "policy_version": POLICY_VERSION, "confirm_age": True, **changes,
    }


# Age confirmation


def test_registration_needs_age_confirmation(app, client):
    refused = client.post("/api/v1/auth/register", json=body(confirm_age=False))
    assert refused.status_code == 422 and AGE_TEXT in refused.json()["detail"]
    payload = body()
    del payload["confirm_age"]
    assert client.post("/api/v1/auth/register", json=payload).status_code == 422
    assert rows(app, User) == []
    assert client.post("/api/v1/auth/register", json=body()).status_code == 201


def test_ordinary_login_does_not_need_age_confirmation(client):
    register(client, "grownup")
    client.cookies.clear()
    login = client.post("/api/v1/auth/login", json={"username": "grownup", "password": "test-only-password-123"})
    assert login.status_code == 200


def test_telegram_start_needs_age_confirmation(client):
    refused = start(client, confirm_age=False)
    assert refused.status_code == 422 and AGE_TEXT in refused.json()["detail"]
    missing = client.post(
        "/api/v1/auth/telegram/start", json={"accept_policy": True, "policy_version": POLICY_VERSION},
    )
    assert missing.status_code == 422
    assert start(client).status_code == 201


def test_email_registration_needs_age_confirmation(app_factory):
    mail = Mail()
    app = app_factory(mailer=mail, email_registration_enabled=True)
    with TestClient(app) as client:
        payload = {
            "email": "owner@example.test", "password": PASSWORD,
            "accept_policy": True, "policy_version": POLICY_VERSION,
        }
        refused = client.post(EMAIL + "/registration/start", json=payload)
        assert refused.status_code == 422 and AGE_TEXT in refused.json()["detail"]
        assert mail.sent == []
        assert begin(client).status_code == 202


# Daily limit of new accounts per address


def test_throttle_window_is_configurable_and_default_is_a_minute(app):
    with app.state.sessions() as db:
        throttle(db, "probe-default", limit=1)
        throttle(db, "probe-day", limit=1, window=86400)
        # Pretend 5 minutes passed. The default window opens again, the day window does not.
        db.execute(update(LoginThrottle).values(window_start=time.time() - 300))
        db.commit()
        throttle(db, "probe-default", limit=1)
        with pytest.raises(HTTPException) as limited:
            throttle(db, "probe-day", limit=1, window=86400)
        assert limited.value.status_code == 429


def test_registration_by_username_is_limited_per_address_per_day(app_factory):
    app = app_factory(registrations_per_ip_per_day=2)
    with TestClient(app) as client:
        assert client.post("/api/v1/auth/register", json=body(username="first")).status_code == 201
        assert client.post("/api/v1/auth/register", json=body(username="second")).status_code == 201
        limited = client.post("/api/v1/auth/register", json=body(username="third"))
        assert limited.status_code == 429 and "завтра" in limited.json()["detail"]
        assert len(rows(app, User)) == 2
        # Signing in to an existing account is not a registration.
        login = client.post("/api/v1/auth/login", json={"username": "first", "password": "test-only-password-123"})
        assert login.status_code == 200


def test_new_telegram_accounts_are_limited_but_known_ones_can_still_sign_in(app_factory):
    app = app_factory(internal_api_token=SERVICE_TOKEN, registrations_per_ip_per_day=1)
    with TestClient(app) as client:
        first = start(client)
        assert confirm(client, token_of(first), update_id=2001).status_code == 200
        assert poll(client, first).json()["new_account"] is True
        client.cookies.clear()
        other = start(client)
        assert confirm(client, token_of(other), update_id=2002, telegram_user_id=777, chat_id=777).status_code == 200
        assert poll(client, other).status_code == 429
        assert len(rows(app, User)) == 1
        # The first person is known, so signing in again creates nothing and passes.
        client.cookies.clear()
        again = start(client)
        assert confirm(client, token_of(again), update_id=2003).status_code == 200
        done = poll(client, again)
        assert done.status_code == 200 and done.json()["new_account"] is False


def test_email_accounts_are_limited_per_address_per_day(app_factory):
    mail = Mail()
    app = app_factory(mailer=mail, email_registration_enabled=True, registrations_per_ip_per_day=1)
    with TestClient(app) as client:
        first = begin(client, "one@example.test")
        assert confirm_code(client, first.json()["registration_id"], code(mail)).status_code == 201
        client.cookies.clear()
        second = begin(client, "two@example.test")
        refused = confirm_code(client, second.json()["registration_id"], code(mail))
        assert refused.status_code == 429
        assert len(rows(app, User)) == 1


# AI ceiling of a new account


def test_new_account_gets_the_lower_daily_ceiling(app_factory):
    app = app_factory(daily_unit_limit=30, new_account_unit_limit=10)
    with TestClient(app) as client:
        user = register(client)
        usage = client.get("/api/v1/provider/usage").json()
        assert usage["daily_unit_limit"] == 10 and usage["daily_units_remaining"] == 10
        with app.state.sessions() as db:
            assert within_daily_limit(db, app.state.settings, user["id"], 10)
            assert not within_daily_limit(db, app.state.settings, user["id"], 11)
            db.execute(update(User).where(User.id == user["id"]).values(created_at=time.time() - 25 * 3600))
            db.commit()
            assert within_daily_limit(db, app.state.settings, user["id"], 30)
            assert daily_state(db, app.state.settings, user["id"])["daily_unit_limit"] == 30


@pytest.mark.parametrize(("daily", "ceiling", "expected"), [(30, 0, 30), (0, 10, 0), (5, 10, 5)])
def test_ceiling_is_off_or_never_raises_the_limit(app_factory, daily, ceiling, expected):
    app = app_factory(daily_unit_limit=daily, new_account_unit_limit=ceiling)
    with TestClient(app) as client:
        register(client)
        assert client.get("/api/v1/provider/usage").json()["daily_unit_limit"] == expected


def test_new_account_ceiling_blocks_ai_but_not_manual_saves(app_factory):
    app = app_factory(daily_unit_limit=30, new_account_unit_limit=1)
    with TestClient(app) as client:
        register(client)

        def post(key, mode):
            return client.post(
                "/api/v1/captures/text", json={"text": "Синтетическая мысль", "processing_mode": mode},
                headers={"Idempotency-Key": key},
            )

        assert "ai_limit_exceeded" not in post("a", "ai").json()
        assert post("b", "ai").json()["ai_limit_exceeded"] is True
        assert "ai_limit_exceeded" not in post("c", "manual").json()


def test_users_without_a_creation_time_count_as_old(app_factory):
    app = app_factory(daily_unit_limit=30, new_account_unit_limit=10)
    with TestClient(app) as client:
        user = register(client)
        with app.state.sessions() as db:
            db.execute(update(User).where(User.id == user["id"]).values(created_at=None))
            db.commit()
            assert daily_state(db, app.state.settings, user["id"])["daily_unit_limit"] == 30
            assert db.scalar(select(User.created_at).where(User.id == user["id"])) is None
