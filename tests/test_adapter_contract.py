"""Run the colleague's unchanged, pinned client against the real in-process API."""

import asyncio
import importlib
import os
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import Capture, Inbox, LinkRequest
from tests.conftest import register
from tests.test_internal import BASE, SERVICE_TOKEN, linked
from tests.test_telegram_actions import TOKEN
from tests.test_telegram_actions import task as prepared_task  # noqa: F401


@pytest.fixture
def adapter(monkeypatch):
    path = os.environ.get("TEST_TELEGRAM_ADAPTER_PATH")
    if not path:
        pytest.skip("Set TEST_TELEGRAM_ADAPTER_PATH to the pinned colleague adapter snapshot")
    assert (Path(path) / "telegram_adapter" / "clients.py").is_file()
    monkeypatch.syspath_prepend(path)
    names = ("clients", "config", "bot", "state")
    modules = {name: importlib.import_module("telegram_adapter." + name) for name in names}
    yield modules
    for name in list(sys.modules):
        if name == "telegram_adapter" or name.startswith("telegram_adapter."):
            del sys.modules[name]


def settings(adapter, tmp_path):
    return adapter["config"].Settings(
        bot_token=str(BASE["bot_id"]) + ":" + "synthetic-test-only-token",
        service_token=SERVICE_TOKEN, core_url="http://127.0.0.1:8000", web_url="http://127.0.0.1:8000",
        state_file=tmp_path / "offset.json",
    )


def test_real_adapter_preserves_text_and_replays_commit(app_factory, adapter, tmp_path):
    app = app_factory(internal_api_token=SERVICE_TOKEN)
    with TestClient(app) as browser:
        linked(browser)
        config = settings(adapter, tmp_path)
        replies = []

        class Telegram:
            async def reply(self, chat_id, text, url=None):
                replies.append((chat_id, text, url))

        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
                core = adapter["clients"].CoreClient(client, config)
                bot = adapter["bot"].Bot(config, core, Telegram(), adapter["state"].OffsetStore(config.state_file, config.bot_id))
                for update_id, text in [(2, "  исходный текст\n"), (3, "/save  ручной текст\n")]:
                    update = {
                        "update_id": update_id,
                        "message": {
                            "from": {"id": BASE["telegram_user_id"]},
                            "chat": {"type": "private", "id": BASE["chat_id"]}, "text": text,
                        },
                    }
                    await bot.handle(update)
                    await bot.handle(update)

        asyncio.run(run())
        assert len(replies) == 4
        assert replies[0][2] == replies[1][2] and replies[2][2] == replies[3][2]
        with app.state.sessions() as db:
            captures = db.scalars(select(Capture).order_by(Capture.created_at, Capture.id)).all()
            assert {(c.original_text, c.processing_mode) for c in captures} == {
                ("  исходный текст\n", "ai"), (" ручной текст\n", "manual"),
            }
            assert len(db.scalars(select(Inbox)).all()) == 3


def test_real_adapter_classifies_business_and_service_errors(app_factory, adapter, tmp_path):
    app = app_factory(internal_api_token=SERVICE_TOKEN)
    with TestClient(app) as browser:
        register(browser)
        expired = browser.post("/api/v1/telegram/link-code").json()
        with app.state.sessions() as db:
            db.get(LinkRequest, expired["link_request_id"]).expires_at = time.time() - 1
            db.commit()
        config = settings(adapter, tmp_path)

        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
                core = adapter["clients"].CoreClient(client, config)
                cases = [
                    ("/telegram/updates", {**BASE, "update_id": 1, "text": "hello"}, "telegram_not_linked"),
                    ("/telegram/link-request", {**BASE, "update_id": 2, "code": expired["code"]}, "link_expired"),
                    ("/telegram/link-request", {**BASE, "update_id": 3, "code": "x" * 22}, "invalid_link"),
                    ("/telegram/link-request", {**BASE, "update_id": 4, "code": "x"}, "invalid_link"),
                    ("/telegram/updates", {**BASE, "update_id": 5, "text": "x" * 12001}, "input_too_large"),
                    ("/telegram/updates", {**BASE, "update_id": 6, "text": " "}, "empty_input"),
                ]
                for path, body, code in cases:
                    with pytest.raises(adapter["clients"].Rejected, match="^" + code + "$"):
                        await core.post(path, body)
                for path, body in [
                    ("/telegram/voice", {}),
                    ("/telegram/updates", {**BASE, "update_id": 7, "text": "hello", "user_id": "forged"}),
                ]:
                    with pytest.raises(adapter["clients"].RemoteFailure) as error:
                        await core.post(path, body)
                    assert error.value.fatal
                bad = adapter["clients"].CoreClient(
                    client, adapter["config"].Settings(
                        bot_token=config.bot_token, service_token="wrong-service-token-" + "x" * 32,
                        core_url=config.core_url, web_url=config.web_url, state_file=config.state_file,
                    ),
                )
                with pytest.raises(adapter["clients"].RemoteFailure) as error:
                    await bad.post("/telegram/updates", {**BASE, "update_id": 8, "text": "hello"})
                assert error.value.fatal

        asyncio.run(run())


def test_real_adapter_accepts_action_errors_and_pending_link(app_factory, adapter, tmp_path):
    app = app_factory(internal_api_token=SERVICE_TOKEN)
    with TestClient(app) as browser:
        linked(browser)
        code = browser.post("/api/v1/telegram/link-code").json()
        config = settings(adapter, tmp_path)

        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
                core = adapter["clients"].CoreClient(client, config)
                pending = await core.post("/telegram/link-request", {**BASE, "update_id": 2, "code": code["code"]})
                assert pending == {"link_request_id": code["link_request_id"], "status": "pending"}
                for token in ["x", "я" * 33]:
                    with pytest.raises(adapter["clients"].Rejected, match="^action_expired$"):
                        await core.post("/telegram/actions", {
                            "bot_id": BASE["bot_id"], "telegram_user_id": BASE["telegram_user_id"],
                            "update_id": 3, "callback_token": token,
                        })

        asyncio.run(run())


def test_real_adapter_runs_task_callback(request, adapter, tmp_path):
    app, _, ids = request.getfixturevalue("prepared_task")
    config = settings(adapter, tmp_path)
    replies, acknowledgements = [], []

    class Telegram:
        async def reply(self, chat_id, text, url=None):
            replies.append(text)

        async def call(self, method, payload):
            acknowledgements.append((method, payload))

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            core = adapter["clients"].CoreClient(client, config)
            bot = adapter["bot"].Bot(config, core, Telegram(), adapter["state"].OffsetStore(config.state_file, config.bot_id))
            update = {
                "update_id": 3,
                "callback_query": {
                    "id": "synthetic-callback-id", "data": TOKEN,
                    "from": {"id": BASE["telegram_user_id"]},
                    "message": {"chat": {"type": "private", "id": BASE["chat_id"]}},
                },
            }
            await bot.handle(update)
            await bot.handle(update)

    asyncio.run(run())
    assert replies == ["Задача выполнена."] * 2
    assert len(acknowledgements) == 2 and acknowledgements[0][0] == "answerCallbackQuery"
    from app.models import Note

    with app.state.sessions() as db:
        assert db.get(Note, ids["note"]).version == 2
