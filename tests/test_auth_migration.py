"""The sign-in, privacy and email migration keeps old rows and the outbox rules."""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.db import make_database
from app.models import Outbox, TelegramLogin, User


def test_upgrade_downgrade_round_trip(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "auth.db").as_posix()
    monkeypatch.setenv("NOTES_DATABASE_URL", url)
    monkeypatch.setenv("NOTES_PROVIDER", "mock")
    config = Config("alembic.ini")
    command.upgrade(config, "a7c1e2d3f4b5")
    engine, sessions = make_database(url)
    with engine.begin() as db:
        db.execute(text("INSERT INTO users (id, username, password_hash, live_calls) VALUES ('u','legacy','hash',0)"))
    command.upgrade(config, "head")
    command.check(config)
    with sessions() as db:
        user = db.get(User, "u")
        assert user.is_test is False
        assert user.email is None and user.email_verified_at is None
        assert user.policy_version is None and user.policy_accepted_at is None
        assert user.deletion_requested_at is None
        db.add(Outbox(
            user_id="u", bot_id=1, chat_id=2, generation=1, message_kind="password_reset", message_text="link",
        ))
        db.commit()
    with sessions() as db:  # An outbox row must still point at exactly one target.
        db.add(Outbox(user_id="u", bot_id=1, chat_id=2, generation=1))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        db.add(User(id="v", username="second", password_hash="hash", email="a@example.com"))
        db.add(User(id="w", username="third", password_hash="hash", email="a@example.com"))
        with pytest.raises(IntegrityError):  # One address, one account.
            db.commit()
        db.rollback()
        db.add(User(id="x", username="fourth", password_hash="hash"))
        db.add(User(id="y", username="fifth", password_hash="hash"))
        db.commit()  # Many accounts may have no address.
        assert db.scalars(select(TelegramLogin)).all() == []
    command.downgrade(config, "a7c1e2d3f4b5")
    with engine.connect() as db:
        columns = {column["name"] for column in inspect(db).get_columns("users")}
        assert not columns & {"email", "email_verified_at", "policy_version", "policy_accepted_at", "deletion_requested_at"}
        assert "telegram_logins" not in inspect(db).get_table_names()
        assert "email_verifications" not in inspect(db).get_table_names()
        assert db.execute(text("SELECT count(*) FROM outbox")).scalar() == 0  # Account messages are not kept.
        assert db.execute(text("SELECT username FROM users WHERE id='u'")).scalar() == "legacy"
    command.upgrade(config, "head")
    command.check(config)
    engine.dispose()
