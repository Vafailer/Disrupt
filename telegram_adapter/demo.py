"""Offline transport demo. Does not load environment secrets or contact any network."""

import tempfile
from pathlib import Path

import httpx

from .bot import Bot
from .clients import CoreClient, TelegramClient
from .config import Settings
from .state import OffsetStore


async def demo():
    saved, replies = [], []
    update = {
        "update_id": 1,
        "message": {
            "from": {"id": 101, "is_bot": False},
            "chat": {"id": 101, "type": "private"},
            "text": "Тестовая мысль для Beresta",
        },
    }

    def core(request):
        saved.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "capture_id": "00000000-0000-0000-0000-000000000001",
                "job_id": None,
                "status": "saved",
                "note_url": "https://example.test/notes/demo",
            },
        )

    def telegram(request):
        if request.url.path.endswith("/getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": [update]})
        replies.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    with tempfile.TemporaryDirectory() as temp:
        settings = Settings(
            "123456:" + "x" * 30,
            "s" * 32,
            "http://core.test",
            "https://example.test",
            Path(temp) / "offset.json",
        )
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(core)) as c,
            httpx.AsyncClient(transport=httpx.MockTransport(telegram)) as t,
        ):
            bot = Bot(
                settings,
                CoreClient(c, settings),
                TelegramClient(t, settings.bot_token),
                OffsetStore(settings.state_file, settings.bot_id),
            )
            await bot.poll_once()
            await bot.poll_once()
            assert len(saved) == len(replies) == 1
            print("OFFLINE OK: capture accepted once, acknowledgement sent, duplicate skipped, offset=2")
            print("Mock core only: actual FastAPI integration and Telegram have NOT been tested live.")
    return 0
