"""Mail is a scaffold: off by default, covered here with a fake mailer and a fake SMTP server."""

import re
import smtplib

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.mailer import (
    SMTP_BZ_SEND_URL,
    DisabledMailer,
    MailDisabled,
    MailError,
    SmtpBzMailer,
    SmtpMailer,
    build_mailer,
)
from app.models import EmailVerification, User
from tests.conftest import register

PASSWORD = "test-only-password-123"


class FakeMailer:
    enabled = True

    def __init__(self, failing=False):
        self.sent, self.failing = [], failing

    def send(self, to, subject, body):
        if self.failing:
            raise MailError("Не удалось отправить письмо")
        self.sent.append((to, subject, body))


def link_token(body, kind):
    return re.search(rf"/#{kind}=([A-Za-z0-9_-]+)", body).group(1)


def test_mail_is_off_by_default(app):
    assert isinstance(app.state.mailer, DisabledMailer) and app.state.mailer.enabled is False
    assert Settings().mail_enabled is False


def test_disabled_mailer_refuses_with_a_clear_error():
    with pytest.raises(MailDisabled, match="Почта пока не подключена"):
        DisabledMailer().send("someone@example.com", "Тема", "Текст")


def test_disabled_mail_gives_503_and_stores_nothing(app, client):
    register(client)
    for path, body in (
        ("/api/v1/account/email", {"email": "user@example.com"}),
        ("/api/v1/auth/recovery/email", {"email": "user@example.com"}),
    ):
        response = client.post(path, json=body)
        assert response.status_code == 503, path
        assert response.json()["detail"] == "Почта пока не подключена"
    with app.state.sessions() as db:
        assert db.scalars(select(EmailVerification)).all() == []
        assert db.scalar(select(User)).email is None


def test_email_is_validated_and_lowercased(app_factory):
    mailer = FakeMailer()
    with TestClient(app_factory(mailer=mailer)) as client:
        register(client)
        for bad in ("nope", "a@b", "a b@example.com", "@example.com", "x" * 250 + "@example.com"):
            assert client.post("/api/v1/account/email", json={"email": bad}).status_code == 422, bad
        assert client.post("/api/v1/account/email", json={"email": "  User@Example.COM "}).status_code == 200
    assert mailer.sent[0][0] == "user@example.com"


def test_verification_flow_with_a_fake_mailer(app_factory):
    mailer = FakeMailer()
    app = app_factory(mailer=mailer)
    with TestClient(app) as client:
        account = register(client)
        sent = client.post("/api/v1/account/email", json={"email": "user@example.com"})
        assert sent.status_code == 200 and sent.json() == {"status": "sent"}
        to, subject, body = mailer.sent[-1]
        assert to == "user@example.com" and "Подтвердите" in subject
        token = link_token(body, "verify-email")
        with app.state.sessions() as db:  # The address is not attached until its owner confirms it.
            user = db.get(User, account["id"])
            assert user.email is None and user.email_verified_at is None
            stored = db.scalar(select(EmailVerification))
            assert stored.purpose == "verify" and stored.email == "user@example.com"
            assert stored.token_hash != token and len(stored.token_hash) == 64
        assert client.post("/api/v1/account/email/verify", json={"token": "x" * 43}).status_code == 400
        done = client.post("/api/v1/account/email/verify", json={"token": token})
        assert done.status_code == 200 and done.json() == {"status": "verified", "email": "user@example.com"}
        assert client.post("/api/v1/account/email/verify", json={"token": token}).status_code == 400
        assert client.post("/api/v1/account/email", json={"email": "user@example.com"}).status_code == 409
        with TestClient(app) as other:  # Someone else cannot take a confirmed address.
            register(other, "other")
            assert other.post("/api/v1/account/email", json={"email": "USER@example.com"}).status_code == 200
            taken = other.post("/api/v1/account/email/verify", json={"token": link_token(mailer.sent[-1][2], "verify-email")})
            assert taken.status_code == 409
            # Nor can someone else use another person's token.
            assert other.post("/api/v1/account/email/verify", json={"token": token}).status_code == 400
    with app.state.sessions() as db:
        user = db.get(User, account["id"])
        assert user.email == "user@example.com" and user.email_verified_at is not None
        assert db.scalar(select(User).where(User.username == "other")).email is None


def test_verification_needs_a_session_csrf_and_a_fresh_token(app_factory):
    mailer = FakeMailer()
    app = app_factory(mailer=mailer)
    with TestClient(app) as client:
        assert client.post("/api/v1/account/email", json={"email": "a@example.com"}).status_code == 401
        register(client)
        denied = client.post(
            "/api/v1/account/email", json={"email": "a@example.com"}, headers={"X-CSRF-Token": "wrong"},
        )
        assert denied.status_code == 403
        client.post("/api/v1/account/email", json={"email": "a@example.com"})
        client.post("/api/v1/account/email", json={"email": "b@example.com"})
        old, new = (link_token(item[2], "verify-email") for item in mailer.sent)
        assert client.post("/api/v1/account/email/verify", json={"token": old}).status_code == 400  # Replaced.
        assert client.post("/api/v1/account/email/verify", json={"token": new}).json()["email"] == "b@example.com"


def test_mail_failure_is_reported_without_the_address(app_factory):
    app = app_factory(mailer=FakeMailer(failing=True))
    with TestClient(app) as client:
        register(client)
        response = client.post("/api/v1/account/email", json={"email": "user@example.com"})
    assert response.status_code == 503 and "user@example.com" not in response.text


def test_email_reset_uses_the_same_token_flow(app_factory):
    mailer = FakeMailer()
    app = app_factory(mailer=mailer)
    with TestClient(app) as client:
        account = register(client)
        client.post("/api/v1/account/email", json={"email": "user@example.com"})
        # An unconfirmed address cannot reset anything.
        with TestClient(app) as anonymous:
            anonymous.post("/api/v1/auth/recovery/email", json={"email": "user@example.com"})
        assert len(mailer.sent) == 1
        client.post("/api/v1/account/email/verify", json={"token": link_token(mailer.sent[0][2], "verify-email")})
        with TestClient(app) as anonymous:
            known = anonymous.post("/api/v1/auth/recovery/email", json={"email": "USER@example.com"})
            unknown = anonymous.post("/api/v1/auth/recovery/email", json={"email": "nobody@example.com"})
            assert known.status_code == unknown.status_code == 200 and known.json() == unknown.json()
            assert len(mailer.sent) == 2 and mailer.sent[1][0] == "user@example.com"
            token = link_token(mailer.sent[1][2], "reset")
            with app.state.sessions() as db:
                reset = db.scalars(select(EmailVerification).where(EmailVerification.purpose == "reset")).one()
                assert reset.channel == "email" and 890 < reset.expires_at - reset.created_at < 910
            done = anonymous.post(
                "/api/v1/auth/password-reset", json={"token": token, "password": "brand-new-password-456"},
            )
            assert done.status_code == 200
            assert anonymous.post(
                "/api/v1/auth/password-reset", json={"token": token, "password": "another-password-789"},
            ).status_code == 400
            login = anonymous.post(
                "/api/v1/auth/login", json={"username": "tester", "password": "brand-new-password-456"},
            )
            assert login.status_code == 200 and login.json()["id"] == account["id"]
        assert client.get("/api/v1/auth/me").status_code == 401  # Every older session ended.


def test_email_reset_is_throttled(app_factory):
    app = app_factory(mailer=FakeMailer())
    with TestClient(app) as client:
        codes = [client.post("/api/v1/auth/recovery/email", json={"email": "user@example.com"}).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_mail_settings_are_checked(tmp_path):
    with pytest.raises(ValueError, match="MAIL_FROM"):
        Settings(mail_enabled=True)
    with pytest.raises(ValueError, match="SMTP_HOST"):
        Settings(mail_enabled=True, mail_transport="smtp", mail_from="noreply@example.test")
    with pytest.raises(ValueError, match="SMTP_BZ_API_KEY_FILE"):
        Settings(mail_enabled=True, mail_from="noreply@example.test")
    with pytest.raises(ValueError, match="MAIL_TRANSPORT"):
        Settings(mail_transport="carrier-pigeon")
    with pytest.raises(ValueError, match="PORT"):
        Settings(smtp_port=0)
    password_file = tmp_path / "smtp-password"
    password_file.write_text("fake-smtp-password\n")
    settings = Settings(
        mail_enabled=True, mail_transport="smtp", smtp_host="smtp.example.test", smtp_port=2525, smtp_user="mailer",
        smtp_password_file=str(password_file), mail_from="beresta <noreply@example.test>",
    )
    mailer = build_mailer(settings)
    assert isinstance(mailer, SmtpMailer) and mailer.enabled is True and mailer.password == "fake-smtp-password"
    assert "fake-smtp-password" not in repr(settings)
    assert isinstance(build_mailer(Settings()), DisabledMailer)


def test_smtp_mailer_uses_starttls_then_login_and_never_leaks_the_address(monkeypatch):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context):
            calls.append(("starttls", context is not None))

        def login(self, user, password):
            calls.append(("login", user, password))

        def send_message(self, message):
            calls.append(("send", message["From"], message["To"], message["Subject"], message.get_content().strip()))

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    mailer = SmtpMailer("smtp.example.test", 587, "mailer", "fake-pass", "noreply@example.test")
    mailer.send("user@example.com", "Тема", "Текст письма")
    assert [call[0] for call in calls] == ["connect", "starttls", "login", "send"]
    assert calls[0][1:3] == ("smtp.example.test", 587)
    assert calls[3][1:] == ("noreply@example.test", "user@example.com", "Тема", "Текст письма")

    class Broken(FakeSMTP):
        def send_message(self, message):
            raise smtplib.SMTPRecipientsRefused({message["To"]: (550, b"user@example.com rejected")})

    monkeypatch.setattr(smtplib, "SMTP", Broken)
    with pytest.raises(MailError) as caught:
        mailer.send("user@example.com", "Тема", "Текст письма")
    assert "user@example.com" not in str(caught.value) and caught.value.__cause__ is None
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    SmtpMailer("smtp.example.test", 587, "", "", "noreply@example.test").send("a@example.com", "Тема", "Текст")
    assert ("login", "", "") not in calls


def test_smtp_bz_settings_read_the_key_from_a_file(tmp_path):
    key_file = tmp_path / "smtp-bz-key"
    key_file.write_text("fake-smtp-bz-key\n")
    settings = Settings(mail_enabled=True, smtp_bz_api_key_file=str(key_file), mail_from="noreply@example.test")
    mailer = build_mailer(settings)
    assert isinstance(mailer, SmtpBzMailer) and mailer.api_key == "fake-smtp-bz-key"
    assert "fake-smtp-bz-key" not in repr(settings)


def test_smtp_bz_mailer_posts_one_multipart_form_over_https():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"result": True})

    mailer = SmtpBzMailer("fake-key", "noreply@example.test", transport=httpx.MockTransport(handler))
    mailer.send("user@example.com", "Тема", "Строка 1\n<b>Строка 2</b>")
    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == SMTP_BZ_SEND_URL and request.method == "POST"
    assert request.headers["authorization"] == "fake-key"
    assert request.headers["content-type"].startswith("multipart/form-data")
    body = request.content.decode()
    for name, value in (
        ("from", "noreply@example.test"), ("to", "user@example.com"), ("subject", "Тема"),
        ("html", "<p>Строка 1<br>&lt;b&gt;Строка 2&lt;/b&gt;</p>"), ("text", "Строка 1\n<b>Строка 2</b>"),
    ):
        assert f'name="{name}"\r\n\r\n{value}\r\n' in body, name


@pytest.mark.parametrize("outcome", [400, 401, 500, "network"])
def test_smtp_bz_failures_never_leak_the_address_or_the_reply(outcome):
    def handler(request):
        if outcome == "network":
            raise httpx.ConnectError("user@example.com unreachable")
        return httpx.Response(outcome, json={"error": "bad address user@example.com"})

    mailer = SmtpBzMailer("fake-key", "noreply@example.test", transport=httpx.MockTransport(handler))
    with pytest.raises(MailError) as error:
        mailer.send("user@example.com", "Тема", "Текст")
    assert "user@example.com" not in str(error.value) and error.value.__cause__ is None
