"""Sign-in through Telegram, recovery, consent and deletion. Telegram and mail are always fakes."""

import asyncio
import json
import os
import re
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import (
    EmailVerification,
    LoginSession,
    Outbox,
    ProductEvent,
    TelegramIdentity,
    TelegramLogin,
    User,
)
from app.policy import POLICY_VERSION
from app.security import hash_token, verify_password
from telegram_adapter.bot import Bot
from telegram_adapter.clients import CoreClient
from telegram_adapter.delivery import DeliverySender
from telegram_adapter.state import OffsetStore
from tests.conftest import register
from tests.telegram_adapter.test_delivery import clients, journal_for, settings_for, success
from tests.test_internal import BASE, HEADERS, SERVICE_TOKEN, linked
from tests.test_reminders import auth, claim, result

NEW_PASSWORD = "brand-new-password-456"


@pytest.fixture
def service(app_factory):
    app = app_factory(internal_api_token=SERVICE_TOKEN)
    with TestClient(app) as client:
        yield app, client


def start(client, **changes):
    body = {"accept_policy": True, "policy_version": POLICY_VERSION, **changes}
    return client.post("/api/v1/auth/telegram/start", json=body)


def token_of(started):
    link = started.json()["deep_link"]
    assert link.startswith("https://t.me/beresta_ru_bot?start=login_")
    return link.rsplit("login_", 1)[1]


def confirm(client, token, update_id=1000, **extra):
    return client.post(
        "/internal/v1/telegram/login-confirm", headers=HEADERS,
        json={**BASE, "update_id": update_id, "token": token, **extra},
    )


def poll(client, started):
    return client.get("/api/v1/auth/telegram/status/" + started.json()["login_id"])


def sign_in(client, **extra):
    """Start in a browser, press the link in the bot, finish in the browser."""
    started = start(client)
    assert started.status_code == 201, started.text
    assert confirm(client, token_of(started), **extra).status_code == 200
    return started, poll(client, started)


def rows(app, model, *conditions):
    with app.state.sessions() as db:
        return db.scalars(select(model).where(*conditions)).all()


# Consent


def test_registration_requires_current_consent(app, client):
    body = {"username": "newuser", "password": "test-only-password-123"}
    for extra in (
        {},
        {"accept_policy": False, "policy_version": POLICY_VERSION},
        {"accept_policy": True},
        {"accept_policy": True, "policy_version": "1999-01-01"},
    ):
        response = client.post("/api/v1/auth/register", json={**body, **extra})
        assert response.status_code == 422, extra
        assert "олитик" in response.json()["detail"]
    assert rows(app, User) == []
    ok = client.post(
        "/api/v1/auth/register", json={**body, "accept_policy": True, "policy_version": POLICY_VERSION},
    )
    assert ok.status_code == 201 and ok.json()["policy_current"] is True
    user = rows(app, User)[0]
    assert user.policy_version == POLICY_VERSION and user.policy_accepted_at is not None


def test_existing_user_accepts_the_policy_once(app, client):
    account = register(client)
    with app.state.sessions.begin() as db:  # An account made before the policy existed.
        user = db.get(User, account["id"])
        user.policy_version = user.policy_accepted_at = None
    assert client.get("/api/v1/auth/me").json()["policy_current"] is False
    login = client.post("/api/v1/auth/login", json={"username": "tester", "password": "test-only-password-123"})
    assert login.json()["policy_current"] is False  # Nothing else is blocked.
    client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    assert client.get("/api/v1/notes").status_code == 200
    assert client.post("/api/v1/account/accept-policy", json={"policy_version": "old"}).status_code == 409
    wrong = client.post(
        "/api/v1/account/accept-policy", json={"policy_version": POLICY_VERSION}, headers={"X-CSRF-Token": "wrong"},
    )
    assert wrong.status_code == 403
    accepted = client.post("/api/v1/account/accept-policy", json={"policy_version": POLICY_VERSION})
    assert accepted.status_code == 200
    assert client.get("/api/v1/auth/me").json()["policy_current"] is True
    assert rows(app, User)[0].policy_accepted_at is not None


def test_privacy_page_is_served_and_versions_agree(client):
    page = client.get("/privacy")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "[Оператор: ФИО/ИП, контакт]" in page.text
    assert f'data-policy-version="{POLICY_VERSION}"' in page.text
    assert page.text.index("Черновик, требует проверки юристом") < page.text.index("<html")
    assert "<script" not in page.text and "style=" not in page.text
    assert f'data-policy-version="{POLICY_VERSION}"' in client.get("/").text


# Sign in through the bot


def test_start_needs_consent_and_stores_only_hashes(app, client):
    for changes in ({"accept_policy": False}, {"policy_version": "old"}, {"policy_version": None}):
        assert start(client, **changes).status_code == 422
    assert client.post("/api/v1/auth/telegram/start", json={}).status_code == 422
    assert rows(app, TelegramLogin) == []
    started = start(client)
    assert started.status_code == 201
    body = started.json()
    token = token_of(started)
    assert len(token) == 43 and set(body) == {"login_id", "deep_link", "expires_at"}
    cookie = started.headers["set-cookie"]
    assert "notes_tg_login=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    binding = client.cookies.get("notes_tg_login")
    row = rows(app, TelegramLogin)[0]
    assert row.id == body["login_id"] and row.status == "pending" and row.purpose == "login"
    assert row.token_hash == hash_token(token) != token
    assert row.binding_hash == hash_token(binding) != binding
    assert 290 < row.expires_at - time.time() <= 300


def test_start_is_rate_limited_per_address(client):
    assert all(start(client).status_code == 201 for _ in range(10))
    limited = start(client)
    assert limited.status_code == 429 and limited.headers["Retry-After"] == "60"


def test_start_rejects_foreign_origin(client):
    body = {"accept_policy": True, "policy_version": POLICY_VERSION}
    response = client.post("/api/v1/auth/telegram/start", json=body, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_new_telegram_account_gets_a_session_and_a_link(service):
    app, client = service
    started = start(client)
    assert poll(client, started).json()["status"] == "pending"
    answer = confirm(client, token_of(started), telegram_username="Ivan_Petrov", first_name="Иван")
    assert answer.status_code == 200
    assert answer.json() == {"status": "confirmed", "purpose": "login", "new_account": True}
    done = poll(client, started)
    assert done.status_code == 200, done.text
    data = done.json()
    assert data["status"] == "ok" and data["new_account"] is True
    assert data["username"] == "ivan_petrov" and data["policy_current"] is True and data["csrf_token"]
    assert "notes_session=" in done.headers["set-cookie"] and "notes_tg_login" in done.headers["set-cookie"]
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200 and me.json()["id"] == data["id"]
    with app.state.sessions() as db:
        user = db.get(User, data["id"])
        assert user.policy_version == POLICY_VERSION and user.is_test is False
        identity = db.scalar(select(TelegramIdentity).where(TelegramIdentity.user_id == user.id))
        assert (identity.bot_id, identity.telegram_user_id, identity.chat_id) == (
            BASE["bot_id"], BASE["telegram_user_id"], BASE["chat_id"],
        )
        events = {(e.name, e.outcome) for e in db.scalars(select(ProductEvent).where(ProductEvent.user_id == user.id))}
        assert {("registered", "telegram"), ("login", "telegram"), ("telegram_linked", None)} <= events
        assert not verify_password("anything-at-all-123", user.password_hash)
        assert db.get(TelegramLogin, started.json()["login_id"]).status == "consumed"
    password = client.post("/api/v1/auth/login", json={"username": "ivan_petrov", "password": "anything-at-all-123"})
    assert password.status_code == 401
    # The link also serves the existing bot features.
    assert client.get("/api/v1/telegram/links").json()["identities"][0]["telegram_user_id"] == BASE["telegram_user_id"]


@pytest.mark.parametrize("handle", [None, "ab"])
def test_username_falls_back_to_the_telegram_id(service, handle):
    app, client = service
    _, done = sign_in(client, telegram_username=handle)
    assert done.json()["username"] == "tg" + str(BASE["telegram_user_id"])


def test_taken_username_gets_a_unique_suffix(service):
    app, client = service
    with TestClient(app) as other:
        register(other, "ivan_petrov")
    _, done = sign_in(client, telegram_username="Ivan_Petrov")
    name = done.json()["username"]
    assert re.fullmatch(r"ivan_petrov_[0-9a-f]{4}", name)
    assert len(rows(app, User)) == 2


def test_existing_identity_signs_in_to_its_account(service):
    app, client = service
    account = linked(client)
    with app.state.sessions.begin() as db:  # An account made before the policy existed.
        user = db.get(User, account["id"])
        user.policy_version = user.policy_accepted_at = None
    with TestClient(app) as browser:
        started, done = sign_in(browser)
        assert done.status_code == 200
        assert done.json()["id"] == account["id"] and done.json()["new_account"] is False
        assert browser.get("/api/v1/auth/me").json()["username"] == "tester"
    assert len(rows(app, User)) == 1
    # The consent given before the Telegram sign-in is recorded for the existing account.
    assert rows(app, User)[0].policy_version == POLICY_VERSION and done.json()["policy_current"] is True


def test_only_the_starting_browser_can_finish(service):
    app, client = service
    started = start(client)
    assert confirm(client, token_of(started)).status_code == 200
    with TestClient(app) as stranger:
        assert poll(stranger, started).status_code == 404
        own = start(stranger)
        assert poll(stranger, started).status_code == 404  # Another login's cookie does not help.
        assert stranger.get("/api/v1/auth/telegram/status/" + own.json()["login_id"]).json()["status"] == "pending"
    with TestClient(app) as forger:
        forger.cookies.set("notes_tg_login", "x" * 43, path="/api/v1/auth/telegram")
        assert poll(forger, started).status_code == 404
    assert rows(app, User) == []
    assert poll(client, started).json()["status"] == "ok"  # The owner is unaffected.


def test_expired_login_is_never_completed(service):
    app, client = service
    started = start(client)
    token = token_of(started)
    with app.state.sessions.begin() as db:
        db.get(TelegramLogin, started.json()["login_id"]).expires_at = time.time() - 1
    assert poll(client, started).json() == {"status": "expired"}
    late = confirm(client, token)
    assert late.status_code == 409 and late.json()["error"]["code"] == "login_expired"
    assert rows(app, User) == []
    # Confirmed in time but opened too late.
    again = start(client)
    assert confirm(client, token_of(again), update_id=2).status_code == 200
    with app.state.sessions.begin() as db:
        db.get(TelegramLogin, again.json()["login_id"]).expires_at = time.time() - 1
    assert poll(client, again).json() == {"status": "expired"}
    assert rows(app, User) == []


def test_login_is_single_use_and_replay_safe(service):
    app, client = service
    started = start(client)
    token = token_of(started)
    first = confirm(client, token, update_id=7)
    assert first.status_code == 200
    assert confirm(client, token, update_id=7).json() == first.json()  # The same update is replayed, not repeated.
    other = confirm(client, token, update_id=8)
    assert other.status_code == 409 and other.json()["error"]["code"] == "login_used"
    binding = client.cookies.get("notes_tg_login")
    assert poll(client, started).json()["status"] == "ok"
    # A copy of the cookie and of the id cannot sign in a second time.
    client.cookies.set("notes_tg_login", binding, path="/api/v1/auth/telegram")
    replay = poll(client, started)
    assert replay.status_code == 409
    assert "notes_session" not in replay.headers.get("set-cookie", "")
    assert confirm(client, token, update_id=9).status_code == 409
    assert len(rows(app, User)) == 1 and len(rows(app, LoginSession)) == 1


def test_unknown_or_malformed_token_is_refused(service):
    _, client = service
    unknown = confirm(client, "A" * 43)
    assert unknown.status_code == 409 and unknown.json()["error"]["code"] == "invalid_login"
    assert confirm(client, "short", update_id=2).status_code == 422
    assert confirm(client, "A" * 42 + "!", update_id=3).status_code == 422
    assert client.post(
        "/internal/v1/telegram/login-confirm", json={**BASE, "update_id": 4, "token": "A" * 43},
    ).status_code == 401
    group = confirm(client, "A" * 43, update_id=5, chat_id=BASE["telegram_user_id"] + 1)
    assert group.status_code == 403  # Only private chats.


def test_polling_is_rate_limited(service):
    _, client = service
    started = start(client)
    codes = [poll(client, started).status_code for _ in range(121)]
    assert codes[:120] == [200] * 120 and codes[120] == 429


def test_long_login_id_is_not_found(client):
    assert client.get("/api/v1/auth/telegram/status/" + "a" * 200).status_code == 404


def test_password_login_still_works_and_reports_the_policy(client):
    register(client)
    assert client.post("/api/v1/auth/logout").status_code == 204
    ok = client.post("/api/v1/auth/login", json={"username": "tester", "password": "test-only-password-123"})
    assert ok.status_code == 200 and ok.json()["policy_current"] is True


def test_bot_and_browser_end_to_end(service, tmp_path):
    app, client = service
    started = start(client)
    token = token_of(started)
    settings, replies = settings_for(tmp_path), []

    def handler(request):
        replies.append(json.loads(request.content))
        return success(request)

    async def scenario():
        core_http, telegram_http, core, telegram = await clients(app, settings, handler)
        async with core_http, telegram_http:
            bot = Bot(settings, core, telegram, OffsetStore(settings.state_file, settings.bot_id))
            await bot.handle({"update_id": 31, "message": {
                "from": {"id": BASE["telegram_user_id"], "is_bot": False, "username": "Ivan_Petrov"},
                "chat": {"id": BASE["chat_id"], "type": "private"}, "text": "/start login_" + token,
            }})

    asyncio.run(scenario())
    assert len(replies) == 1 and "аккаунт" in replies[0]["text"]
    done = poll(client, started)
    assert done.json()["username"] == "ivan_petrov"


# Account deletion


def test_deletion_with_password(app, client):
    account = register(client)
    wrong = client.post("/api/v1/account/delete-request", json={"password": "wrong-password-123"})
    assert wrong.status_code == 403 and client.get("/api/v1/auth/me").status_code == 200
    assert client.post("/api/v1/account/delete-request", json={}).status_code == 422
    done = client.post("/api/v1/account/delete-request", json={"password": "test-only-password-123"})
    assert done.status_code == 200 and done.json() == {"status": "requested"}
    assert client.get("/api/v1/auth/me").status_code == 401
    with app.state.sessions() as db:
        user = db.get(User, account["id"])
        assert user.deletion_requested_at is not None
        assert db.scalars(select(LoginSession).where(LoginSession.user_id == user.id)).all() == []
        assert db.scalar(select(func.count()).select_from(ProductEvent).where(
            ProductEvent.name == "deletion_requested")) == 1
    login = client.post("/api/v1/auth/login", json={"username": "tester", "password": "test-only-password-123"})
    assert login.status_code == 200 and login.json()["deletion_requested"] is True
    client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    assert client.post("/api/v1/account/delete-request/cancel").json() == {"status": "cancelled"}
    assert rows(app, User)[0].deletion_requested_at is None


def test_deletion_password_attempts_are_limited(client):
    register(client)
    codes = [
        client.post("/api/v1/account/delete-request", json={"password": "wrong-password-123"}).status_code
        for _ in range(6)
    ]
    assert codes == [403] * 5 + [429]


def test_deletion_needs_a_session_and_csrf(client):
    assert client.post("/api/v1/account/delete-request", json={"password": "x"}).status_code == 401
    register(client)
    denied = client.post(
        "/api/v1/account/delete-request", json={"password": "test-only-password-123"},
        headers={"X-CSRF-Token": "wrong"},
    )
    assert denied.status_code == 403


def test_deletion_confirmed_in_telegram(service):
    app, client = service
    account = linked(client)
    started = client.post("/api/v1/account/delete-request/telegram")
    assert started.status_code == 201
    token, check = token_of(started), "/api/v1/account/delete-request/telegram/" + started.json()["login_id"]
    assert client.post(check).json()["status"] == "pending"
    stranger = confirm(client, token, update_id=5, telegram_user_id=7, chat_id=7)
    assert stranger.status_code == 403 and stranger.json()["error"]["code"] == "login_forbidden"
    with TestClient(app) as other:
        register(other, "other")
        assert other.post(check).status_code == 404
    owner = confirm(client, token, update_id=6)
    assert owner.json() == {"status": "confirmed", "purpose": "delete", "new_account": False}
    assert client.post(check).json() == {"status": "requested"}
    assert client.get("/api/v1/auth/me").status_code == 401
    assert rows(app, User, User.id == account["id"])[0].deletion_requested_at is not None


def test_deletion_in_telegram_needs_a_linked_account(client):
    register(client)
    assert client.post("/api/v1/account/delete-request/telegram").status_code == 409


def test_a_login_link_cannot_confirm_a_deletion(service):
    app, client = service
    linked(client)
    with TestClient(app) as browser:
        started = start(browser)
        assert confirm(browser, token_of(started)).status_code == 200
        check = "/api/v1/account/delete-request/telegram/" + started.json()["login_id"]
    assert client.post(check).status_code == 404  # Not a deletion request.
    assert rows(app, User)[0].deletion_requested_at is None


# Password recovery


def request_reset(client, username="tester"):
    return client.post("/api/v1/auth/recovery/telegram", json={"username": username})


def reset_token(text):
    return re.search(r"/#reset=([A-Za-z0-9_-]+)", text).group(1)


def reset_password(client, token, password=NEW_PASSWORD):
    return client.post("/api/v1/auth/password-reset", json={"token": token, "password": password})


def test_recovery_answer_is_the_same_for_every_username(service):
    app, client = service
    linked(client)
    with TestClient(app) as other:
        register(other, "no_telegram")
    with TestClient(app) as anonymous:
        answers = [request_reset(anonymous, name) for name in ("tester", "no_telegram", "nobody_here", "Ёжик")]
    assert all(answer.status_code == 200 for answer in answers)
    assert len({answer.text for answer in answers}) == 1
    assert "Telegram" in answers[0].json()["message"]
    with app.state.sessions() as db:
        queued = db.scalars(select(Outbox).where(Outbox.message_kind.is_not(None))).all()
        assert len(queued) == 1 and queued[0].message_kind == "password_reset" and queued[0].status == "pending"
        assert queued[0].user_id == rows(app, User, User.username == "tester")[0].id
        assert (queued[0].bot_id, queued[0].chat_id) == (BASE["bot_id"], BASE["chat_id"])
        assert queued[0].job_id is None and queued[0].reminder_id is None
        stored = db.scalars(select(EmailVerification)).all()
        assert len(stored) == 1 and stored[0].channel == "telegram" and stored[0].purpose == "reset"
        token = reset_token(queued[0].message_text)
        assert stored[0].token_hash == hash_token(token) and token not in stored[0].token_hash
        assert 890 < stored[0].expires_at - time.time() <= 900


def test_reset_link_arrives_works_once_and_ends_every_session(service):
    app, client = service
    linked(client)  # This browser is signed in.
    with TestClient(app) as other_device:
        login = other_device.post("/api/v1/auth/login", json={"username": "tester", "password": "test-only-password-123"})
        assert login.status_code == 200
    with TestClient(app) as anonymous:
        assert request_reset(anonymous).status_code == 200
        item = claim(client).json()["items"][0]
        assert item["callback_token"] is None and item["note_url"] == app.state.settings.public_origin + "/"
        assert auth(client, item).json() == {"send": True}
        assert result(client, item).status_code == 200
        token = reset_token(item["text"])
        assert "15 минут" in item["text"]
        with app.state.sessions() as db:
            sent = db.scalar(select(Outbox).where(Outbox.message_kind.is_not(None)))
            assert sent.status == "sent" and sent.message_text is None  # No one-time link is kept.
        assert claim(client).json() == {"items": []}
        assert reset_password(anonymous, token, "short").status_code == 422
        assert reset_password(anonymous, token).status_code == 200
        assert reset_password(anonymous, token, "another-password-789").status_code == 400
    assert client.get("/api/v1/auth/me").status_code == 401
    with app.state.sessions() as db:
        assert db.scalars(select(LoginSession)).all() == []
    with TestClient(app) as fresh:
        old = fresh.post("/api/v1/auth/login", json={"username": "tester", "password": "test-only-password-123"})
        assert old.status_code == 401
        new = fresh.post("/api/v1/auth/login", json={"username": "tester", "password": NEW_PASSWORD})
        assert new.status_code == 200


def test_bad_expired_and_replaced_links_are_refused(service):
    app, client = service
    linked(client)
    with TestClient(app) as anonymous:
        assert reset_password(anonymous, "x" * 43).status_code == 400
        request_reset(anonymous)
        request_reset(anonymous)
        first, second = [
            reset_token(row.message_text)
            for row in sorted(rows(app, Outbox, Outbox.message_kind.is_not(None)), key=lambda r: r.created_at)
        ]
        assert reset_password(anonymous, first).status_code == 400  # A newer link replaced it.
        with app.state.sessions.begin() as db:
            db.scalar(select(EmailVerification).where(EmailVerification.token_hash == hash_token(second))).expires_at = (
                time.time() - 1
            )
        expired = reset_password(anonymous, second)
        assert expired.status_code == 400 and NEW_PASSWORD not in expired.text
    assert client.post("/api/v1/auth/login", json={"username": "tester", "password": NEW_PASSWORD}).status_code == 401


def test_recovery_is_throttled_by_username_and_address(service):
    app, client = service
    linked(client)
    with TestClient(app) as anonymous:
        assert [request_reset(anonymous).status_code for _ in range(4)] == [200, 200, 200, 429]
        assert request_reset(anonymous, "other_name").status_code == 200
        codes = [request_reset(anonymous, f"user_{n}").status_code for n in range(10)]
        assert 429 in codes
    assert len(rows(app, Outbox, Outbox.message_kind.is_not(None))) == 3


def test_reset_attempts_are_throttled_by_address(client):
    codes = [reset_password(client, f"{n:043d}").status_code for n in range(12)]
    assert codes[:10] == [400] * 10 and codes[10:] == [429, 429]


def test_hourly_cap_stops_message_floods(service):
    app, client = service
    account = linked(client)
    with app.state.sessions.begin() as db:
        for n in range(5):
            db.add(EmailVerification(
                user_id=account["id"], purpose="reset", channel="telegram", token_hash=f"{n:064d}",
                expires_at=time.time() - 10, used_at=time.time() - 10, created_at=time.time() - 60,
            ))
    with TestClient(app) as anonymous:
        assert request_reset(anonymous).status_code == 200
    assert rows(app, Outbox, Outbox.message_kind.is_not(None)) == []


@pytest.mark.parametrize("change", ["blocked", "unlinked", "stale"])
def test_reset_message_is_dropped_when_the_chat_is_gone_or_the_link_is_old(service, change):
    app, client = service
    linked(client)
    with TestClient(app) as anonymous:
        request_reset(anonymous)
    with app.state.sessions.begin() as db:
        if change == "blocked":
            db.scalar(select(TelegramIdentity)).delivery_status = "blocked"
        elif change == "unlinked":
            db.delete(db.scalar(select(TelegramIdentity)))
        else:
            db.scalar(select(Outbox)).created_at = time.time() - 1000
    assert claim(client).json() == {"items": []}
    with app.state.sessions() as db:
        row = db.scalar(select(Outbox))
        assert row.message_text is None and row.status == "cancelled"


def test_recovery_does_not_message_a_blocked_chat(service):
    app, client = service
    linked(client)
    with app.state.sessions.begin() as db:
        db.scalar(select(TelegramIdentity)).delivery_status = "blocked"
    with TestClient(app) as anonymous:
        assert request_reset(anonymous).status_code == 200
    assert rows(app, Outbox, Outbox.message_kind.is_not(None)) == []


def test_unknown_send_result_erases_the_link_and_sends_nothing_twice(service):
    app, client = service
    linked(client)
    with TestClient(app) as anonymous:
        request_reset(anonymous)
    item = claim(client).json()["items"][0]
    assert auth(client, item).json()["send"]
    assert result(client, item, "unknown").status_code == 200
    assert claim(client).json() == {"items": []}
    assert rows(app, Outbox)[0].message_text is None
    with app.state.sessions() as db:  # Delivery of account messages is not product activity.
        assert db.scalar(select(func.count()).select_from(ProductEvent).where(
            ProductEvent.name.like("processing_reply%") | ProductEvent.name.like("reminder_%"))) == 0


def test_retryable_result_keeps_the_message_for_a_resend(service):
    app, client = service
    linked(client)
    with TestClient(app) as anonymous:
        request_reset(anonymous)
    item = claim(client).json()["items"][0]
    assert auth(client, item).json()["send"]
    assert result(client, item, "retryable", retry_after_seconds=0).status_code == 200
    again = claim(client).json()["items"][0]
    assert again["delivery_id"] == item["delivery_id"] and again["text"] == item["text"]


@pytest.mark.skipif(os.name != "posix", reason="Durable Telegram journal requires POSIX directory fsync")
def test_real_adapter_delivers_the_reset_link_once(service, tmp_path):
    app, client = service
    linked(client)
    with TestClient(app) as anonymous:
        request_reset(anonymous)
    settings, sends = settings_for(tmp_path), []

    def handler(request):
        sends.append(json.loads(request.content))
        return success(request)

    async def scenario():
        core_http, telegram_http, core, telegram = await clients(app, settings, handler)
        async with core_http, telegram_http:
            sender = DeliverySender(settings, core, telegram, journal_for(settings))
            assert await sender.run_once()
            assert not await sender.run_once()

    asyncio.run(scenario())
    assert len(sends) == 1 and sends[0]["chat_id"] == BASE["chat_id"]
    assert sends[0]["text"].startswith("Сброс пароля в beresta.") and "parse_mode" not in sends[0]
    assert sends[0]["reply_markup"]["inline_keyboard"] == [[
        {"text": "Открыть Beresta", "url": app.state.settings.public_origin + "/"}
    ]]
    assert reset_token(sends[0]["text"])


def test_telegram_account_can_set_a_password_through_recovery(service):
    app, client = service
    _, done = sign_in(client)
    name = done.json()["username"]
    with TestClient(app) as anonymous:
        assert request_reset(anonymous, name).status_code == 200
        item = claim(client).json()["items"][0]
        assert reset_password(anonymous, reset_token(item["text"])).status_code == 200
        assert anonymous.post("/api/v1/auth/login", json={"username": name, "password": NEW_PASSWORD}).status_code == 200


def test_core_client_accepts_reset_delivery_shape(tmp_path):
    settings = settings_for(tmp_path)
    sender = DeliverySender(settings, CoreClient(None, settings), None, None)
    item = {
        "delivery_id": "6f1a1d84-0b0e-4b53-a3d1-1ab2c3d4e5f6", "lease_token": "A" * 43, "generation": 1,
        "chat_id": BASE["chat_id"], "text": "Сброс пароля в beresta.\n\nhttp://127.0.0.1:8000/#reset=" + "B" * 43,
        "note_url": "http://127.0.0.1:8000/", "callback_token": None,
    }
    sender.validate_claim(item)
