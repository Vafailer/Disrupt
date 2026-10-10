"""Separate administrator identity, authenticator codes, lockout, network gate and audit."""

import json
import re
import time
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import totp
from app.admin_models import AdminAccount, AdminAudit, AdminSession
from app.config import Settings, parse_networks
from app.db import make_database
from app.models import User
from tests.conftest import ADMIN_ADDRESS, ADMIN_PASSWORD, admin_login, make_admin, register

ADMIN_PAGES = ["/admin", "/static/admin.html", "/static/admin.js", "/static/admin.css", "/static/admin.auth.js",
               "/static/admin.feedback.js", "/static/admin.tools.js"]
ADMIN_API = ["/admin-api/v1/me", "/admin-api/v1/summary", "/admin-api/v1/export", "/admin-api/v1/export.zip",
             "/admin-api/v1/feedback", "/admin-api/v1/audit"]


def at(app, address, **kwargs):
    return TestClient(app, client=address, **kwargs)


def audit_actions(app):
    with app.state.sessions() as db:
        return [row.action for row in db.scalars(select(AdminAudit).order_by(AdminAudit.created_at, AdminAudit.id))]


# Network gate.

@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "10.77.0.1", "10.77.0.254", "::ffff:10.77.0.9"])
def test_private_addresses_reach_the_admin_page_assets_and_api(app, host):
    with at(app, (host, 50000)) as client:
        for path in ADMIN_PAGES:
            assert client.get(path).status_code == 200, path
        assert client.get("/admin-api/v1/me").status_code == 401  # Reachable, only not signed in.


@pytest.mark.parametrize("host", ["testclient", "10.77.1.1", "10.78.0.1", "192.168.1.5", "8.8.8.8", "2001:db8::1", "not-an-ip"])
def test_other_addresses_get_404_everywhere_in_the_admin(app, host):
    with at(app, (host, 50000)) as client:
        missing = client.get("/no-such-page")
        assert missing.status_code == 404
        for path in ADMIN_PAGES + ADMIN_API + ["/admin/", "/admin/extra", "/admin-api", "/admin-api/v1/nothing"]:
            response = client.get(path)
            # Same answer as for a page that does not exist, so the admin cannot be discovered.
            assert response.status_code == 404 and response.json() == missing.json(), path
        for method in ("post", "patch", "put", "delete"):
            assert client.request(method.upper(), "/admin-api/v1/login", json={}).status_code == 404
        assert client.get("/health").status_code == 200
        assert client.get("/static/styles.css").status_code == 200  # The public site is untouched.


@pytest.mark.parametrize("header", ["X-Forwarded-For", "Forwarded", "X-Real-IP", "X-Forwarded-Host", "X-Forwarded-Proto"])
def test_requests_through_a_proxy_are_rejected_even_from_an_allowed_address(app, header):
    # The public Caddy always adds such a header, and it connects from a private Docker address.
    with at(app, ADMIN_ADDRESS) as client:
        for path in ADMIN_PAGES + ADMIN_API:
            assert client.get(path, headers={header: "10.77.0.5"}).status_code == 404, path
        assert client.post("/admin-api/v1/login", json={}, headers={header: "1.2.3.4"}).status_code == 404
        assert client.get("/admin", headers={"X-Unrelated": "1"}).status_code == 200


def test_allowed_networks_come_from_settings(app_factory):
    custom = app_factory(admin_allowed_networks="192.168.50.0/24, 2001:db8::/32")
    with at(custom, ("192.168.50.7", 1)) as inside, at(custom, ADMIN_ADDRESS) as old, at(custom, ("2001:db8::5", 1)) as v6:
        assert inside.get("/admin").status_code == 200 and v6.get("/admin").status_code == 200
        assert old.get("/admin").status_code == 404


@pytest.mark.parametrize("value", ["", " , ", "0.0.0.0/0", "::/0", "10.77.0.0/24,0.0.0.0/0", "nonsense", "10.77.0.0/33"])
def test_unsafe_network_settings_are_refused(value):
    with pytest.raises(ValueError):
        Settings(admin_allowed_networks=value)


def test_network_and_cookie_settings_from_environment(monkeypatch):
    assert Settings().admin_cookie_secure is True
    assert [str(n) for n in parse_networks(Settings().admin_allowed_networks)] == ["127.0.0.1/32", "::1/128", "10.77.0.0/24"]
    monkeypatch.setenv("NOTES_ADMIN_COOKIE_SECURE", "false")
    monkeypatch.setenv("NOTES_ADMIN_ALLOWED_NETWORKS", "10.77.0.0/24")
    settings = Settings.from_env()
    assert settings.admin_cookie_secure is False and settings.admin_allowed_networks == "10.77.0.0/24"
    monkeypatch.setenv("NOTES_ADMIN_COOKIE_SECURE", "maybe")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_admin_responses_are_never_cached_or_framed(app, admin_client):
    with at(app, ("8.8.8.8", 1)) as outside:
        responses = [admin_client.get(path) for path in ADMIN_PAGES + ADMIN_API] + [
            admin_client.get("/admin-api/v1/summary", params={"from": "bad"}), outside.get("/admin"),
            admin_client.post("/admin-api/v1/login", json={}),
        ]
    for response in responses:
        assert response.headers["cache-control"] == "no-store", response.url
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-content-type-options"] == "nosniff"


def test_admin_pages_do_not_use_inline_code_or_styles():
    html = Path("app/static/admin.html").read_text(encoding="utf-8")
    assert "style=" not in html and "<style" not in html and "onclick" not in html
    assert all(" src=" in tag for tag in re.findall(r"<script[^>]*>", html))
    assert "Доступ только из приватной сети" in html


# Sign-in.

def test_login_sets_a_strict_http_only_cookie_and_returns_a_csrf_token(app, private_client, admin_secret):
    response = admin_login(app, private_client, admin_secret)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "beresta_admin=" in cookie and "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert "secure" not in cookie  # Test settings allow plain HTTP.
    me = private_client.get("/admin-api/v1/me")
    assert me.status_code == 200 and me.json()["username"] == "owner"
    assert me.json()["csrf_token"] == response.json()["csrf_token"]
    dump = response.text + me.text
    for secret in (admin_secret, ADMIN_PASSWORD, "scrypt$"):
        assert secret not in dump
    assert "notes_session" not in response.headers["set-cookie"]


def test_cookie_is_secure_by_default(app_factory):
    app = app_factory(admin_cookie_secure=True)
    secret = make_admin(app)
    with at(app, ADMIN_ADDRESS) as client:
        response = client.post("/admin-api/v1/login", json={
            "username": "owner", "password": ADMIN_PASSWORD, "totp": totp.code_at(secret, time.time()),
        })
        assert response.status_code == 200
        assert "secure" in response.headers["set-cookie"].lower().replace("; ", ";").split(";")


def test_every_login_failure_gives_the_same_answer(app, private_client, admin_secret):
    make_admin(app, "unconfirmed", confirmed=False)
    disabled = make_admin(app, "stopped")
    with app.state.sessions() as db:
        db.scalar(select(AdminAccount).where(AdminAccount.username == "stopped")).disabled = True
        db.commit()
    good = totp.code_at(admin_secret, time.time())
    attempts = [
        ("owner", "wrong-password-value", good),
        ("owner", ADMIN_PASSWORD, "000000" if good != "000000" else "111111"),
        ("owner", ADMIN_PASSWORD, "abc"),
        ("owner", ADMIN_PASSWORD, ""),
        ("nobody", ADMIN_PASSWORD, good),
        ("", "", ""),
        ("unconfirmed", ADMIN_PASSWORD, "123456"),
        ("stopped", ADMIN_PASSWORD, totp.code_at(disabled, time.time())),
        ("x" * 500, ADMIN_PASSWORD, good),
    ]
    answers = set()
    for username, password, code in attempts:
        response = private_client.post("/admin-api/v1/login", json={"username": username, "password": password, "totp": code})
        assert response.status_code == 401 and "set-cookie" not in response.headers
        answers.add(response.text)
    assert len(answers) == 1
    assert "beresta_admin" not in private_client.cookies
    # Nothing in the audit trail keeps the typed name, password or code.
    with app.state.sessions() as db:
        rows = list(db.scalars(select(AdminAudit)))
    dump = json.dumps([[row.action, row.target, row.ip, row.details] for row in rows])
    for typed in ("nobody", "wrong-password-value", good, "x" * 50):
        assert typed not in dump
    assert {row.action for row in rows} == {"login_failed"}


def test_five_failures_lock_the_account_for_fifteen_minutes(app, private_client, admin_secret):
    for _ in range(5):
        assert admin_login(app, private_client, admin_secret, password="wrong-password-value").status_code == 401
    # Even the right password and a fresh code are refused while locked, with the same message.
    locked = admin_login(app, private_client, admin_secret)
    assert locked.status_code == 401 and locked.json() == {"detail": "Не удалось войти. Проверьте данные."}
    with app.state.sessions() as db:
        account = db.scalar(select(AdminAccount))
        assert 14 * 60 < account.locked_until - time.time() <= 15 * 60
        account.locked_until = time.time() - 1  # The lock has run out.
        db.commit()
    assert admin_login(app, private_client, admin_secret).status_code == 200
    with app.state.sessions() as db:
        account = db.scalar(select(AdminAccount))
        assert account.failed_attempts == 0 and account.locked_until is None and account.last_login_at
    actions = audit_actions(app)
    assert actions.count("login_failed") == 6 and actions.count("account_locked") == 1
    assert actions[-1] == "login_success"


def test_failures_below_the_limit_are_forgiven_after_a_success(app, private_client, admin_secret):
    for _ in range(4):
        admin_login(app, private_client, admin_secret, password="wrong-password-value")
    assert admin_login(app, private_client, admin_secret).status_code == 200
    for _ in range(3):
        admin_login(app, private_client, admin_secret, password="wrong-password-value")
    assert admin_login(app, private_client, admin_secret).status_code == 200


def test_login_attempts_from_one_address_are_rate_limited(app, private_client):
    codes = [private_client.post("/admin-api/v1/login", json={"username": "x%d" % i, "password": "p", "totp": "000000"}).status_code
             for i in range(12)]
    assert codes[:10] == [401] * 10 and codes[10:] == [429, 429]


def test_a_code_cannot_be_used_twice_and_neighbouring_steps_work(app, private_client, admin_secret):
    code = totp.code_at(admin_secret, time.time())
    assert admin_login(app, private_client, admin_secret, code=code).status_code == 200
    assert private_client.post("/admin-api/v1/logout", headers={"X-CSRF-Token": private_client.headers["X-CSRF-Token"]}).status_code == 204
    assert admin_login(app, private_client, admin_secret, code=code, fresh=False).status_code == 401
    later = totp.code_at(admin_secret, time.time() + 30)
    assert admin_login(app, private_client, admin_secret, code=later, fresh=False).status_code == 200
    # A code from before the accepted step stays dead.
    earlier = totp.code_at(admin_secret, time.time() - 30)
    private_client.post("/admin-api/v1/logout", headers={"X-CSRF-Token": private_client.headers["X-CSRF-Token"]})
    assert admin_login(app, private_client, admin_secret, code=earlier, fresh=False).status_code == 401
    # Far-away codes fail even with a fresh counter.
    assert admin_login(app, private_client, admin_secret, code=totp.code_at(admin_secret, time.time() + 300)).status_code == 401


# Sessions.

def test_session_limits_absolute_eight_hours_and_idle_thirty_minutes(app, admin_client):
    with app.state.sessions() as db:
        session = db.scalar(select(AdminSession))
        assert 7.9 * 3600 < session.expires_at - time.time() <= 8 * 3600
        assert session.ip == ADMIN_ADDRESS[0] and len(session.token_hash) == 64 and len(session.csrf_hash) == 64
    cookie = admin_client.cookies["beresta_admin"]
    with app.state.sessions() as db:
        assert cookie not in {row.token_hash for row in db.scalars(select(AdminSession))}  # Only a hash is stored.
        session = db.scalar(select(AdminSession))
        session.last_seen_at = time.time() - 120
        db.commit()
    assert admin_client.get("/admin-api/v1/me").status_code == 200
    with app.state.sessions() as db:
        assert time.time() - db.scalar(select(AdminSession)).last_seen_at < 30  # Activity extends the idle window.
        db.scalar(select(AdminSession)).last_seen_at = time.time() - 31 * 60
        db.commit()
    assert admin_client.get("/admin-api/v1/me").status_code == 401
    with app.state.sessions() as db:
        assert db.scalar(select(AdminSession)) is None
    secret = db_secret(app)
    assert admin_login(app, admin_client, secret).status_code == 200
    with app.state.sessions() as db:
        db.scalar(select(AdminSession)).expires_at = time.time() - 1
        db.commit()
    assert admin_client.get("/admin-api/v1/me").status_code == 401


def db_secret(app):
    with app.state.sessions() as db:
        return db.scalar(select(AdminAccount)).totp_secret


def test_logout_needs_csrf_ends_the_session_and_is_audited(app, admin_client):
    csrf = admin_client.headers.pop("X-CSRF-Token")
    assert admin_client.post("/admin-api/v1/logout").status_code == 403
    assert admin_client.post("/admin-api/v1/logout", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert admin_client.get("/admin-api/v1/me").status_code == 200
    foreign = admin_client.post("/admin-api/v1/logout", headers={"X-CSRF-Token": csrf, "Origin": "https://evil.invalid"})
    assert foreign.status_code == 403
    response = admin_client.post("/admin-api/v1/logout", headers={"X-CSRF-Token": csrf, "Origin": "http://testserver"})
    assert response.status_code == 204
    assert "beresta_admin" not in admin_client.cookies
    assert admin_client.get("/admin-api/v1/me").status_code == 401
    assert audit_actions(app)[-2:] == ["login_success", "logout"]
    with app.state.sessions() as db:
        assert db.scalar(select(AdminSession)) is None


def test_stolen_cookie_still_needs_the_csrf_token_for_changes(app, admin_client):
    cookie = admin_client.cookies["beresta_admin"]
    with at(app, ADMIN_ADDRESS) as other:
        other.cookies.set("beresta_admin", cookie)
        assert other.get("/admin-api/v1/me").status_code == 200
        assert other.patch("/admin-api/v1/feedback/x", json={"status": "new", "version": 1}).status_code == 403
    with at(app, ("8.8.8.8", 1)) as outside:  # The same cookie is useless outside the private network.
        outside.cookies.set("beresta_admin", cookie)
        assert outside.get("/admin-api/v1/me").status_code == 404


def test_disabling_the_account_ends_open_sessions(app, admin_client):
    with app.state.sessions() as db:
        db.scalar(select(AdminAccount)).disabled = True
        db.commit()
    assert admin_client.get("/admin-api/v1/me").status_code == 401
    assert admin_client.get("/admin-api/v1/summary", params={"from": "2026-10-01", "to": "2026-10-02"}).status_code == 401


# Regular users are not administrators.

def test_a_user_with_the_old_admin_role_gets_nothing(app, client, private_client):
    account = register(private_client)
    with app.state.sessions() as db:
        db.get(User, account["id"]).role = "admin"  # Possible in the database, meaningless to the code.
        db.commit()
    assert "notes_session" in private_client.cookies
    for path in ADMIN_API:
        assert private_client.get(path).status_code == 401, path
    assert private_client.patch("/admin-api/v1/feedback/x", json={"status": "new", "version": 1}).status_code in {401, 403}
    assert private_client.post("/admin-api/v1/logout").status_code in {401, 403}
    # From a user address the admin does not exist at all, role or not.
    register(client, "plain")
    for path in ADMIN_PAGES + ADMIN_API:
        assert client.get(path).status_code == 404


@pytest.mark.parametrize("path", [
    "/api/admin/summary", "/api/admin/export", "/api/admin/feedback", "/api/admin/feedback/abc", "/api/admin/export.zip",
])
def test_old_admin_routes_are_gone_for_everyone(app, client, admin_client, path):
    register(client)
    for caller in (client, admin_client):
        assert caller.get(path, params={"from": "2026-10-01", "to": "2026-10-02"}).status_code == 404
        assert caller.patch(path, json={"status": "new", "version": 1}).status_code == 404


def test_no_code_path_grants_admin_through_the_user_role():
    assert not Path("app/admin.py").exists()
    from app import security
    assert not hasattr(security, "require_admin")
    sources = "\n".join(path.read_text(encoding="utf-8") for path in Path("app").rglob("*.py"))
    assert 'role == "admin"' not in sources and ".role = " not in sources and "role=\"admin\"" not in sources
    from app.admin_cli import build_parser
    assert "role" not in build_parser().format_help()


# Audit log.

def test_audit_log_lists_actions_newest_first_without_secrets(app, admin_client, admin_secret):
    admin_login(app, admin_client, admin_secret, password="wrong-password-value")
    rows = admin_client.get("/admin-api/v1/audit").json()
    assert [row["action"] for row in rows][:2] == ["login_failed", "login_success"]
    assert rows[1]["admin"] == "owner" and rows[1]["ip"] == ADMIN_ADDRESS[0]
    assert rows[0]["details"] == {"reason": "credentials"}
    assert set(rows[0]) == {"id", "created_at", "admin", "action", "target", "ip", "details"}
    dump = json.dumps(rows)
    assert ADMIN_PASSWORD not in dump and "wrong-password-value" not in dump and admin_secret not in dump
    assert admin_client.get("/admin-api/v1/audit", params={"limit": 101}).status_code == 422
    assert len(admin_client.get("/admin-api/v1/audit", params={"limit": 1}).json()) == 1


def test_audit_view_is_limited_to_the_last_hundred_entries(app, admin_client):
    with app.state.sessions() as db:
        for index in range(130):
            db.add(AdminAudit(action="x%d" % index, created_at=time.time() - 1000 + index, details={}))
        db.commit()
    rows = admin_client.get("/admin-api/v1/audit").json()
    assert len(rows) == 100
    assert [rows[0]["action"], rows[1]["action"]] == ["login_success", "x129"]


# Migration.

def test_migration_demotes_old_admin_users_and_creates_empty_admin_tables(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "admins.db").as_posix()
    monkeypatch.setenv("NOTES_DATABASE_URL", url)
    monkeypatch.setenv("NOTES_PROVIDER", "mock")
    config = Config("alembic.ini")
    command.upgrade(config, "f3a82c91d507")
    engine, sessions = make_database(url)
    with engine.begin() as db:
        for name, role in (("old_admin", "admin"), ("regular", "user")):
            db.execute(text("INSERT INTO users (id, username, password_hash, live_calls, role) VALUES (:n, :n, 'hash', 0, :r)"),
                       {"n": name, "r": role})
    command.upgrade(config, "head")
    command.check(config)
    with sessions() as db:
        assert {u.username: u.role for u in db.scalars(select(User))} == {"old_admin": "user", "regular": "user"}
        assert db.scalars(select(AdminAccount)).all() == [] and db.scalars(select(AdminAudit)).all() == []
    command.downgrade(config, "f3a82c91d507")
    with engine.connect() as db:
        assert db.execute(text("SELECT COUNT(*) FROM users")).scalar() == 2
    command.upgrade(config, "head")
    command.check(config)
    engine.dispose()
