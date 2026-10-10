import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from app.models import Item, Outbox, ProductEvent, Reminder, TelegramIdentity
from app.scheduler import Scheduler
from telegram_adapter.bot import Bot
from telegram_adapter.clients import CoreClient, RemoteFailure, TelegramClient
from telegram_adapter.config import Settings
from telegram_adapter.delivery import DeliveryJournal, DeliverySender
from telegram_adapter.state import OffsetStore
from tests.test_internal import BASE, SERVICE_TOKEN
from tests.test_reminders import reminders as reminders
from tests.test_scheduler import make_due


def settings_for(tmp_path):
    return Settings(
        bot_token=str(BASE["bot_id"]) + ":" + "test_only_token_" * 3, service_token=SERVICE_TOKEN,
        core_url="http://core.invalid", web_url="http://127.0.0.1:8000",
        state_file=tmp_path / "offset.json",
    )


def journal_for(settings):
    return DeliveryJournal(settings.state_file.with_suffix(".delivery.json"), settings.bot_id)


async def clients(app, settings, handler, core_transport=None):
    core_http = httpx.AsyncClient(transport=core_transport or httpx.ASGITransport(app=app), trust_env=False)
    telegram_http = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    core, telegram = CoreClient(core_http, settings), TelegramClient(telegram_http, settings.bot_token)
    return core_http, telegram_http, core, telegram


def success(request):
    body = json.loads(request.content)
    if request.url.path.endswith("/answerCallbackQuery"):
        return httpx.Response(200, json={"ok": True, "result": True})
    return httpx.Response(200, json={"ok": True, "result": {"message_id": 123, "chat": {"id": body["chat_id"]}}})


def test_full_scheduler_sender_and_completed_button_flow(reminders, tmp_path):
    app, client, ids = reminders
    reminder_id = make_due(app, client, ids)
    assert Scheduler(app.state.sessions).run_once() == 1
    settings, sends = settings_for(tmp_path), []

    def handler(request):
        sends.append(json.loads(request.content))
        return success(request)

    async def scenario():
        c, t, core, telegram = await clients(app, settings, handler)
        async with c, t:
            sender = DeliverySender(settings, core, telegram, journal_for(settings))
            assert await sender.run_once()
            assert not await sender.run_once()
            assert not sender.journal.path.exists()
            button = sends[0]["reply_markup"]["inline_keyboard"][1][0]
            assert button["text"] == "Выполнено" and len(button["callback_data"].encode()) <= 64
            assert sends[0]["text"] == "Позвонить" and "parse_mode" not in sends[0]
            bot = Bot(settings, core, telegram, OffsetStore(settings.state_file, settings.bot_id))
            await bot.handle({"update_id": 987, "callback_query": {
                "id": "synthetic-callback", "data": button["callback_data"], "from": {"id": BASE["telegram_user_id"]},
                "message": {"chat": {"id": BASE["chat_id"], "type": "private"}},
            }})

    asyncio.run(scenario())
    with app.state.sessions() as db:
        assert db.get(Item, ids["item"]).status == "completed"
        assert db.get(Reminder, reminder_id).status == "cancelled"
        assert db.scalar(select(Outbox)).telegram_message_id == 123
        assert db.scalar(select(func.count()).select_from(ProductEvent).where(ProductEvent.name == "reminder_sent")) == 1


@pytest.mark.parametrize("failure", ["timeout", "invalid_json", "invalid_success", "500", "400", "fake_403", "bad_429"])
def test_ambiguous_send_is_unknown_without_retry(reminders, tmp_path, failure, caplog):
    app, client, ids = reminders
    make_due(app, client, ids)
    Scheduler(app.state.sessions).run_once()
    settings, attempts = settings_for(tmp_path), []

    def handler(request):
        attempts.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("secret token and private note", request=request)
        if failure == "invalid_json":
            return httpx.Response(200, text="private note")
        if failure == "invalid_success":
            return httpx.Response(200, json={"ok": True, "result": {"message_id": True}})
        if failure == "fake_403":
            return httpx.Response(503, json={"ok": False, "error_code": 403})
        if failure == "bad_429":
            return httpx.Response(429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": True}})
        return httpx.Response(int(failure), json={"ok": False, "error_code": int(failure), "description": "private"})

    async def scenario():
        c, t, core, telegram = await clients(app, settings, handler)
        async with c, t:
            sender = DeliverySender(settings, core, telegram, journal_for(settings))
            assert await sender.run_once()
            assert not await sender.run_once()
    asyncio.run(scenario())
    assert len(attempts) == 1 and "private" not in caplog.text and "test_only_token" not in caplog.text
    with app.state.sessions() as db:
        assert db.scalar(select(Outbox)).status == "unknown"
        assert db.scalar(select(Reminder)).status == "unknown"


@pytest.mark.parametrize("code", [403, 429, 401])
def test_confirmed_rejections_block_delay_or_stop(reminders, tmp_path, code):
    app, client, ids = reminders
    make_due(app, client, ids)
    Scheduler(app.state.sessions).run_once()
    settings = settings_for(tmp_path)

    def handler(request):
        return httpx.Response(code, json={"ok": False, "error_code": code, "parameters": {"retry_after": 30}})

    async def scenario():
        c, t, core, telegram = await clients(app, settings, handler)
        async with c, t:
            sender = DeliverySender(settings, core, telegram, journal_for(settings))
            if code == 401:
                with pytest.raises(RemoteFailure) as caught:
                    await sender.run_once()
                assert caught.value.fatal
            else:
                assert await sender.run_once()
            if code != 401:
                assert not await sender.run_once()
    asyncio.run(scenario())
    with app.state.sessions() as db:
        row = db.scalar(select(Outbox))
        assert row.status == {403: "blocked", 429: "retryable", 401: "unknown"}[code]
        if code == 403:
            assert db.scalar(select(TelegramIdentity)).delivery_status == "blocked"
        if code == 429:
            assert row.retry_at > time.time() + 25


def test_result_commit_then_lost_response_survives_restart_without_resend(reminders, tmp_path):
    app, client, ids = reminders
    make_due(app, client, ids, item=False)
    Scheduler(app.state.sessions).run_once()
    settings, sends, receipts = settings_for(tmp_path), [], []

    class LostResult(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx.ASGITransport(app=app)

        async def handle_async_request(self, request):
            response = await self.inner.handle_async_request(request)
            if request.url.path.endswith("/result"):
                receipts.append(json.loads(request.content))
                if len(receipts) == 1:
                    raise httpx.ReadTimeout("private response", request=request)
            return response

    def handler(request):
        sends.append(json.loads(request.content))
        return success(request)

    async def scenario():
        c, t, core, telegram = await clients(app, settings, handler, LostResult())
        async with c, t:
            sender = DeliverySender(settings, core, telegram, journal_for(settings))
            with pytest.raises(RemoteFailure):
                await sender.run_once()
            path = sender.journal.path
            assert path.stat().st_mode & 0o077 == 0
            assert "Позвонить" not in path.read_text() and '"chat_id"' not in path.read_text()
            restarted = DeliverySender(settings, core, telegram, journal_for(settings))
            assert not await restarted.run_once()
            assert not path.exists()
    asyncio.run(scenario())
    assert len(sends) == 1 and len(receipts) == 2 and receipts[0] == receipts[1]
    assert len(sends[0]["reply_markup"]["inline_keyboard"]) == 1


@pytest.mark.parametrize("stage", ["before_authorize", "authorize_response_lost"])
def test_no_send_without_known_authorization(reminders, tmp_path, stage):
    app, client, ids = reminders
    reminder_id = make_due(app, client, ids)
    Scheduler(app.state.sessions).run_once()
    settings, sends = settings_for(tmp_path), []

    class Intercept(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path.endswith("/authorize") and stage == "before_authorize":
                assert client.post("/api/v1/reminders/" + reminder_id + "/cancel", json={"generation": 1}).status_code == 200
            response = await httpx.ASGITransport(app=app).handle_async_request(request)
            if request.url.path.endswith("/authorize") and stage == "authorize_response_lost":
                raise httpx.ReadTimeout("unknown authorization", request=request)
            return response

    def handler(request):
        sends.append(request)
        return success(request)

    async def scenario():
        c, t, core, telegram = await clients(app, settings, handler, Intercept())
        async with c, t:
            sender = DeliverySender(settings, core, telegram, journal_for(settings))
            if stage == "authorize_response_lost":
                with pytest.raises(RemoteFailure):
                    await sender.run_once()
                with app.state.sessions.begin() as db:
                    db.scalar(select(Outbox)).lease_until = time.time() - 1
                assert not await sender.run_once()
            else:
                assert await sender.run_once()
            assert not settings.state_file.exists()
    asyncio.run(scenario())
    assert not sends
    with app.state.sessions() as db:
        assert db.scalar(select(Outbox)).status == ("cancelled" if stage == "before_authorize" else "unknown")


def test_thirty_mock_reminders_are_sent_once(reminders, tmp_path):
    app, client, ids = reminders
    for index in range(30):
        make_due(app, client, ids, key="batch-" + str(index), item=False)
    assert Scheduler(app.state.sessions).run_once() == 30
    settings, sends = settings_for(tmp_path), []

    def handler(request):
        sends.append(request)
        return success(request)

    async def scenario():
        c, t, core, telegram = await clients(app, settings, handler)
        async with c, t:
            sender = DeliverySender(settings, core, telegram, journal_for(settings))
            for _ in range(30):
                assert await sender.run_once()
            assert not await sender.run_once()
    asyncio.run(scenario())
    assert len(sends) == 30
    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Outbox).where(Outbox.status == "sent")) == 30


def test_invalid_or_foreign_journal_is_not_silently_discarded(tmp_path):
    settings = settings_for(tmp_path)
    journal = journal_for(settings)
    body = {"lease_token": "a" * 43, "generation": 1, "status": "sent", "telegram_message_id": 123,
            "error_code": None, "retry_after_seconds": None}
    journal.save("00000000-0000-4000-8000-000000000001", body)
    with pytest.raises(ValueError):
        DeliveryJournal(journal.path, settings.bot_id + 1)
    journal.path.chmod(0o644)
    with pytest.raises(ValueError):
        journal_for(settings)
    assert Path(journal.path).exists()


def test_429_retry_uses_new_lease_and_sends_only_after_delay(reminders, tmp_path):
    app, client, ids = reminders
    make_due(app, client, ids)
    Scheduler(app.state.sessions).run_once()
    settings, sends = settings_for(tmp_path), []

    def handler(request):
        sends.append(json.loads(request.content))
        if len(sends) == 1:
            return httpx.Response(429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 30}})
        return success(request)

    async def scenario():
        c, t, core, telegram = await clients(app, settings, handler)
        async with c, t:
            sender = DeliverySender(settings, core, telegram, journal_for(settings))
            assert await sender.run_once()
            with app.state.sessions() as db:
                old_hash = db.scalar(select(Outbox)).lease_token_hash
            sender.cooldown_until = 0
            assert not await sender.run_once()  # Server retry_at still prevents a send.
            with app.state.sessions.begin() as db:
                db.scalar(select(Outbox)).retry_at = time.time() - 1
            assert await sender.run_once()
            with app.state.sessions() as db:
                row = db.scalar(select(Outbox))
                assert row.status == "sent" and row.lease_token_hash != old_hash
    asyncio.run(scenario())
    assert len(sends) == 2
    assert sends[0]["reply_markup"] != sends[1]["reply_markup"]  # Fresh callback token too.


def test_delivery_continues_while_long_poll_waits_and_fatal_error_stops_both():
    from telegram_adapter.__main__ import serve

    async def scenario():
        polling, cancelled, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class SlowBot:
            async def poll_once(self):
                polling.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        class Sender:
            async def run_once(self):
                await polling.wait()
                delivered.set()
                raise RemoteFailure("core_contract_or_auth", fatal=True)

        assert await asyncio.wait_for(serve(SlowBot(), Sender()), 1) == 1
        assert delivered.is_set() and cancelled.is_set()
    asyncio.run(scenario())


def test_long_processing_reply_is_accepted_up_to_telegram_limit(tmp_path):
    # Processing replies may reach Telegram's 4096 UTF-16 units; a longer claim must not stop the bot loop.
    settings = settings_for(tmp_path)
    core = CoreClient(httpx.AsyncClient(), settings)
    sender = DeliverySender(settings, core, None, journal_for(settings))
    item = {
        "delivery_id": "00000000-0000-4000-8000-000000000001", "generation": 1, "lease_token": "a" * 43,
        "chat_id": 42, "callback_token": None, "note_url": settings.web_url + "/?capture=x",
    }
    sender.validate_claim({**item, "text": "Длинный ответ. " * 250})
    with pytest.raises(RemoteFailure):
        sender.validate_claim({**item, "text": "я" * 4097})
