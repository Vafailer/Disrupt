"""Upgrade a populated old schema, not just an empty development database."""

from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from app.db import make_database
from app.models import Capture, LoginSession, Note, ProductEvent, Revision, User


def test_existing_records_survive_upgrade_and_downgrade(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "legacy.db").as_posix()
    monkeypatch.setenv("NOTES_DATABASE_URL", url)
    monkeypatch.setenv("NOTES_PROVIDER", "mock")
    config = Config("alembic.ini")
    command.upgrade(config, "8f80bf2b83ca")
    engine, sessions = make_database(url)
    with engine.begin() as db:
        db.execute(
            text("INSERT INTO users (id, username, password_hash, live_calls) VALUES ('u','legacy','hash',0)")
        )
        db.execute(text("INSERT INTO sessions VALUES ('token','u','csrf',9999999999)"))
        db.execute(text("INSERT INTO captures VALUES ('c','u','original','key',1)"))
        db.execute(text("INSERT INTO notes VALUES ('n','c','u','title','original','[]',1,'mock',1,1)"))
        db.execute(text("INSERT INTO revisions VALUES ('r','n',1,'{}',1)"))
    command.upgrade(config, "head")
    command.check(config)
    with sessions() as db:
        user = db.get(User, "u")
        assert user.role == "user" and user.is_test is False
        assert user.created_at is None and user.first_login_at is None
        assert db.get(Capture, "c").original_text == "original"
        assert db.get(Capture, "c").processing_mode == "ai"
        assert db.get(Capture, "c").channel == "web"
        assert db.get(Note, "n").version == 1
        assert db.get(LoginSession, "token").csrf_token == "csrf"
        assert db.get(Revision, "r").version == 1
        assert db.scalars(select(ProductEvent)).all() == []  # Do not invent historical metrics.
    command.downgrade(config, "8f80bf2b83ca")
    with engine.connect() as db:
        assert db.execute(text("SELECT original_text FROM captures")).scalar() == "original"
        assert db.execute(text("PRAGMA foreign_key_check")).all() == []
    command.upgrade(config, "head")
    command.check(config)
    engine.dispose()
