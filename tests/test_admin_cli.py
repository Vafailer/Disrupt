"""Administrator accounts from the server shell, and the test-account mark."""

import getpass
import re
import time

import pytest
from sqlalchemy import func, select

from app import admin_cli, totp
from app.admin_models import AdminAccount, AdminAudit, AdminSession
from app.analytics import record_event
from app.models import ProductEvent, User
from tests.conftest import admin_login, register

PASSWORD = "synthetic-cli-password-9"


@pytest.fixture
def typed(monkeypatch):
    """Answers for the hidden password prompts."""
    answers = []
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": answers.pop(0))
    return answers


def run(*argv):
    return admin_cli.main(list(argv))


def create(typed, capsys, name="owner"):
    typed.extend([PASSWORD, PASSWORD])
    run("create", name)
    out = capsys.readouterr().out
    return re.search(r"Секрет base32: ([A-Z2-7]+)", out).group(1), out


def confirmed(typed, capsys, name="owner"):
    secret, _ = create(typed, capsys, name)
    run("confirm", name, totp.code_at(secret, time.time()))
    capsys.readouterr()
    return secret


def account(app, name="owner"):
    with app.state.sessions() as db:
        return db.scalar(select(AdminAccount).where(AdminAccount.username == name))


def actions(app):
    with app.state.sessions() as db:
        return [(row.action, row.target, row.details) for row in
                db.scalars(select(AdminAudit).order_by(AdminAudit.created_at, AdminAudit.id))]


def wrong_code(secret):
    near = {totp.code_at(secret, time.time() + offset) for offset in (-60, -30, 0, 30, 60)}
    return next(code for code in ("000000", "111111", "222222") if code not in near)


def test_create_shows_the_secret_once_and_waits_for_confirmation(app, typed, capsys, private_client):
    secret, out = create(typed, capsys)
    assert f"otpauth://totp/beresta%3Aowner?secret={secret}&issuer=beresta" in out
    assert PASSWORD not in out
    row = account(app)
    assert row.totp_secret == secret and row.totp_enabled is False and row.disabled is False
    assert row.password_hash.startswith("scrypt$") and PASSWORD not in row.password_hash
    assert actions(app) == [("admin_created", "owner", {"via": "cli"})]
    # Not usable before the first code is confirmed.
    assert admin_login(app, private_client, secret, password=PASSWORD).status_code == 401
    with pytest.raises(SystemExit):
        run("confirm", "owner", wrong_code(secret))
    assert account(app).totp_enabled is False and actions(app)[-1][0] == "admin_confirm_failed"
    code = totp.code_at(secret, time.time())
    run("confirm", "owner", code)
    assert "Готово" in capsys.readouterr().out
    assert account(app).totp_enabled is True and account(app).last_totp_step is not None
    assert actions(app)[-1][0] == "admin_confirmed"
    with pytest.raises(SystemExit):
        run("confirm", "owner", code)  # Nothing left to confirm.
    # The code used for confirmation cannot also open the first session.
    assert admin_login(app, private_client, secret, password=PASSWORD, code=code, fresh=False).status_code == 401
    later = totp.code_at(secret, time.time() + 30)
    assert admin_login(app, private_client, secret, password=PASSWORD, code=later, fresh=False).status_code == 200


def test_create_refuses_weak_mismatched_passwords_and_odd_names(app, typed, capsys):
    for name, answers in [("owner", ["short", "short"]), ("owner", ["a" * 12, "b" * 12]), ("no spaces", []), ("ab", [])]:
        typed[:] = answers
        with pytest.raises(SystemExit) as refusal:
            run("create", name)
        assert refusal.value.code == 2
    err = capsys.readouterr().err
    assert "Пароль должен быть" in err and "Пароли не совпали" in err and "Имя от 3" in err
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(AdminAccount)) == 0
    create(typed, capsys)
    typed.extend([PASSWORD, PASSWORD])
    with pytest.raises(SystemExit) as duplicate:
        run("create", "owner")
    assert duplicate.value.code == 2 and "уже есть" in capsys.readouterr().err


def test_names_are_lower_case_and_unique(app, typed, capsys):
    create(typed, capsys, "Owner")
    assert account(app, "owner") is not None
    typed.extend([PASSWORD, PASSWORD])
    with pytest.raises(SystemExit):
        run("create", "OWNER")


def test_disable_closes_sessions_and_enable_restores_access(app, typed, capsys, private_client):
    secret = confirmed(typed, capsys)
    assert admin_login(app, private_client, secret, password=PASSWORD).status_code == 200
    run("disable", "owner")
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(AdminSession)) == 0
    assert private_client.get("/admin-api/v1/me").status_code == 401
    assert admin_login(app, private_client, secret, password=PASSWORD).status_code == 401
    run("enable", "owner")
    assert admin_login(app, private_client, secret, password=PASSWORD).status_code == 200
    assert [name for name, _, _ in actions(app) if name.startswith("admin_") and "login" not in name][-2:] == [
        "admin_disabled", "admin_enabled"]
    with pytest.raises(SystemExit):
        run("disable", "nobody")


def test_enable_clears_a_lockout(app, typed, capsys, private_client):
    secret = confirmed(typed, capsys)
    for _ in range(5):
        admin_login(app, private_client, secret, password="wrong-password-value")
    assert account(app).locked_until is not None
    run("enable", "owner")
    assert account(app).locked_until is None
    assert admin_login(app, private_client, secret, password=PASSWORD).status_code == 200


def test_reset_2fa_gives_a_new_secret_and_ends_everything_old(app, typed, capsys, private_client):
    old = confirmed(typed, capsys)
    assert admin_login(app, private_client, old, password=PASSWORD).status_code == 200
    run("reset-2fa", "owner")
    out = capsys.readouterr().out
    new = re.search(r"Секрет base32: ([A-Z2-7]+)", out).group(1)
    assert new != old and "otpauth://" in out
    row = account(app)
    assert row.totp_secret == new and row.totp_enabled is False and row.last_totp_step is None
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(AdminSession)) == 0
    assert private_client.get("/admin-api/v1/me").status_code == 401
    assert admin_login(app, private_client, old, password=PASSWORD).status_code == 401
    assert admin_login(app, private_client, new, password=PASSWORD).status_code == 401  # Not confirmed yet.
    run("confirm", "owner", totp.code_at(new, time.time()))
    assert admin_login(app, private_client, new, password=PASSWORD).status_code == 200
    assert admin_login(app, private_client, old, password=PASSWORD).status_code == 401
    assert "admin_2fa_reset" in [name for name, _, _ in actions(app)]


def test_list_shows_state_but_no_secrets(app, typed, capsys):
    secret = confirmed(typed, capsys, "first")
    other, _ = create(typed, capsys, "second")
    run("disable", "first")
    capsys.readouterr()
    run("list")
    out = capsys.readouterr().out
    assert re.search(r"first\tотключён\t", out) and re.search(r"second\tждёт подтверждения кода\t", out)
    for hidden in (secret, other, "scrypt", PASSWORD):
        assert hidden not in out
    run("enable", "first")
    capsys.readouterr()
    run("list")
    assert re.search(r"first\tактивен\tпоследний вход не входил", capsys.readouterr().out)


def test_mark_and_unmark_test_user_change_how_new_events_are_stored(app, client, capsys):
    person = register(client, "Team.Member")
    run("mark-test-user", "TEAM.member")
    assert "исключён" in capsys.readouterr().out
    with app.state.sessions() as db:
        assert db.get(User, person["id"]).is_test is True
        record_event(db, person["id"], "task_completed", "op-1")
        db.commit()
        assert db.scalar(select(ProductEvent.is_test).where(ProductEvent.operation_id == "op-1")) is True
    run("unmark-test-user", "team.member")
    with app.state.sessions() as db:
        assert db.get(User, person["id"]).is_test is False
        record_event(db, person["id"], "task_completed", "op-2")
        db.commit()
        assert db.scalar(select(ProductEvent.is_test).where(ProductEvent.operation_id == "op-2")) is False
    assert [(name, target, details) for name, target, details in actions(app)] == [
        ("user_marked_test", "team.member", {"via": "cli", "is_test": True}),
        ("user_unmarked_test", "team.member", {"via": "cli", "is_test": False}),
    ]
    with pytest.raises(SystemExit):
        run("mark-test-user", "nobody")


def test_every_command_leaves_an_audit_entry_without_secrets(app, client, typed, capsys):
    register(client, "colleague")
    secret = confirmed(typed, capsys)
    run("list")
    run("disable", "owner")
    run("enable", "owner")
    run("reset-2fa", "owner")
    run("mark-test-user", "colleague")
    run("unmark-test-user", "colleague")
    rows = actions(app)
    assert {name for name, _, _ in rows} == {
        "admin_created", "admin_confirmed", "admin_list", "admin_disabled", "admin_enabled", "admin_2fa_reset",
        "user_marked_test", "user_unmarked_test"}
    assert all(details["via"] == "cli" for _, _, details in rows)
    assert PASSWORD not in repr(rows) and secret not in repr(rows)
    with app.state.sessions() as db:
        assert {row.admin_id for row in db.scalars(select(AdminAudit))} == {None}


def test_a_subcommand_is_required():
    for argv in ([], ["unknown"], ["create"], ["confirm", "owner"]):
        with pytest.raises(SystemExit) as exit_info:
            admin_cli.main(argv)
        assert exit_info.value.code == 2
