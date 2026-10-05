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


def test_structure_migration_keeps_populated_foreign_keys(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "structure.db").as_posix()
    monkeypatch.setenv("NOTES_DATABASE_URL", url)
    monkeypatch.setenv("NOTES_PROVIDER", "mock")
    config = Config("alembic.ini")
    command.upgrade(config, "651eb026c9bd")
    engine, _ = make_database(url)
    with engine.begin() as db:
        db.execute(
            text("INSERT INTO users (id,username,password_hash,live_calls) VALUES ('u','legacy','hash',0)")
        )
        db.execute(text("INSERT INTO categories (id,user_id,name) VALUES ('cat','u','Работа')"))
        db.execute(
            text(
                "INSERT INTO captures (id,user_id,original_text,idempotency_key,created_at) VALUES ('c','u','original','key',1)"
            )
        )
        db.execute(
            text(
                "INSERT INTO notes (id,capture_id,user_id,title,markdown,conclusions,version,provider,created_at,updated_at,category_id) VALUES ('n','c','u','title','original','[]',4,'mock',1,1,'cat')"
            )
        )
        db.execute(
            text(
                "INSERT INTO items (id,user_id,note_id,kind,text,status,version) VALUES ('i','u','n','task','Позвонить','open',3)"
            )
        )
        db.execute(
            text(
                "INSERT INTO reminders VALUES ('r','u','n','i',9999999999,'Europe/Moscow','Позвонить','confirmed',1,1)"
            )
        )
    command.upgrade(config, "head")
    command.check(config)
    for target in ["651eb026c9bd", "head"]:
        if target == "head":
            command.upgrade(config, target)
        else:
            command.downgrade(config, target)
        with engine.connect() as db:
            assert db.execute(text("PRAGMA foreign_key_check")).all() == []
            assert db.execute(text("SELECT version FROM notes WHERE id='n'")).scalar() == 4
            assert db.execute(text("SELECT version FROM items WHERE id='i'")).scalar() == 3
            assert db.execute(text("SELECT item_id FROM reminders WHERE id='r'")).scalar() == "i"
            assert db.execute(text("SELECT original_text FROM captures WHERE id='c'")).scalar() == "original"
    command.check(config)
    engine.dispose()
