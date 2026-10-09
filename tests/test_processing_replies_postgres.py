import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from app.config import Settings
from app.contracts import AuthorizeRequest, ClaimRequest
from app.db import make_database
from app.deliveries import authorize_delivery, claim_deliveries
from app.models import Note, Outbox, TelegramIdentity, User
from app.services import capture_text


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="Local PostgreSQL is not configured")
def test_postgres_processing_reply_has_one_claim_and_authorization(monkeypatch):
    url = os.environ["TEST_POSTGRES_URL"]
    assert make_url(url).host in {"127.0.0.1", "localhost", "::1"}
    monkeypatch.setenv("NOTES_DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine, sessions = make_database(url)
    settings = Settings(database_url=url)
    bot_id, chat_id = uuid.uuid4().int % 2**50 + 1, uuid.uuid4().int % 2**50 + 1
    try:
        with sessions.begin() as db:
            user = User(username="pg_reply_" + uuid.uuid4().hex, password_hash="synthetic")
            db.add(user)
            db.flush()
            job = capture_text(db, user.id, "Synthetic thought", "source", settings,
                               channel="telegram", commit=False)
            job.status = "succeeded"
            db.add(Note(capture_id=job.capture_id, user_id=user.id, title="Synthetic",
                        markdown="Synthetic reply", conclusions=[], provider="mock"))
            db.add(TelegramIdentity(user_id=user.id, bot_id=bot_id, telegram_user_id=chat_id, chat_id=chat_id))
            db.add(Outbox(job_id=job.id, user_id=user.id, bot_id=bot_id, chat_id=chat_id, generation=1))
        def claim():
            with sessions.begin() as db:
                return claim_deliveries(db, ClaimRequest(bot_id=bot_id, limit=10), settings)["items"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            claimed = [item for group in pool.map(lambda _: claim(), range(4)) for item in group]
        assert len(claimed) == 1
        item = claimed[0]
        def authorize():
            with sessions.begin() as db:
                return authorize_delivery(db, item["delivery_id"], AuthorizeRequest(
                    lease_token=item["lease_token"], generation=item["generation"],
                ), settings)["send"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            assert sum(pool.map(lambda _: authorize(), range(4))) == 1
    finally:
        engine.dispose()
