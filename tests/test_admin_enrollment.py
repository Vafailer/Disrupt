"""Synthetic enrollment only. The authenticator key must never reach process output."""

import io
import json
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import totp
from app.admin_models import AdminAccount
from scripts import admin_enroll_receiver as receiver

PASSWORD = "synthetic-enrollment-password-1"


def invoke(monkeypatch, command, payload, username="owner"):
    monkeypatch.setattr(receiver.sys, "argv", ["receiver", command, username])
    monkeypatch.setattr(receiver.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode())))
    return receiver.main()


def test_private_export_and_confirm(app, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(receiver, "EXPORT_DIRECTORY", tmp_path)
    assert invoke(monkeypatch, "create", {"password": PASSWORD}) == 0
    with app.state.sessions() as db:
        admin = db.scalar(select(AdminAccount).where(AdminAccount.username == "owner"))
        secret = admin.totp_secret
        assert not admin.totp_enabled
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert PASSWORD not in output.out + output.err
    bundle = tmp_path / "owner.txt"
    assert secret in bundle.read_text() and "otpauth://" in bundle.read_text()
    assert bundle.stat().st_mode & 0o777 == 0o600
    assert invoke(monkeypatch, "confirm", {"code": totp.code_at(secret, time.time())}) == 0
    with app.state.sessions() as db:
        assert db.scalar(select(AdminAccount)).totp_enabled
    assert secret not in capsys.readouterr().out


def test_existing_export_is_not_overwritten(app, monkeypatch, tmp_path):
    monkeypatch.setattr(receiver, "EXPORT_DIRECTORY", tmp_path)
    target = tmp_path / "owner.txt"
    target.write_text("synthetic previous bundle")
    with pytest.raises(FileExistsError):
        invoke(monkeypatch, "create", {"password": PASSWORD})
    assert target.read_text() == "synthetic previous bundle"
    with app.state.sessions() as db:
        assert db.scalar(select(AdminAccount)) is None


def test_failed_creation_removes_only_empty_reservation(app, monkeypatch, tmp_path):
    monkeypatch.setattr(receiver, "EXPORT_DIRECTORY", tmp_path)
    invoke(monkeypatch, "create", {"password": PASSWORD})
    (tmp_path / "owner.txt").unlink()
    with pytest.raises(SystemExit):
        invoke(monkeypatch, "create", {"password": PASSWORD})
    assert not (tmp_path / "owner.txt").exists()
    with app.state.sessions() as db:
        assert db.scalar(select(AdminAccount)) is not None


@pytest.mark.parametrize("command,payload,username", [
    ("create", {"password": "short"}, "owner"),
    ("create", {"password": PASSWORD}, "../owner"),
    ("confirm", {"code": "not a code"}, "owner"),
])
def test_invalid_enrollment_input_is_rejected(app, monkeypatch, tmp_path, command, payload, username):
    monkeypatch.setattr(receiver, "EXPORT_DIRECTORY", tmp_path)
    with pytest.raises(SystemExit):
        invoke(monkeypatch, command, payload, username)
    assert not list(tmp_path.glob("*.txt"))
