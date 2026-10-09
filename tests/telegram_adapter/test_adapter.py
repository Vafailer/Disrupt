import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from telegram_adapter.bot import Bot
from telegram_adapter.clients import MAX_AUDIO_BYTES, CoreClient, Rejected, RemoteFailure, TelegramClient
from telegram_adapter.config import Settings
from telegram_adapter.state import OffsetStore

TOKEN = "123456:" + "x" * 30
SAVED = {
    "capture_id": "00000000-0000-0000-0000-000000000001",
    "job_id": None,
    "status": "saved",
    "note_url": "https://example.test/notes/1",
}


def message(update_id=10, text="  Моя исходная мысль\nбез изменений  "):
    return {
        "update_id": update_id,
        "message": {
            "from": {"id": 101, "is_bot": False},
            "chat": {"id": 101, "type": "private"},
            "text": text,
        },
    }


@pytest.fixture
def settings(tmp_path):
    return Settings(TOKEN, "s" * 32, "http://core.test", "https://example.test", tmp_path / "offset.json")


async def session(settings, updates, core_handler=None, tg_handler=None):
    core_requests, replies, polls = [], [], []

    def core(request):
        core_requests.append(request)
        return core_handler(request) if core_handler else httpx.Response(200, json=SAVED)

    def telegram(request):
        if tg_handler:
            response = tg_handler(request)
            if response is not None:
                return response
        body = json.loads(request.content)
        if request.url.path.endswith("/getUpdates"):
            polls.append(body)
            return httpx.Response(200, json={"ok": True, "result": updates})
        replies.append(body)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    c = httpx.AsyncClient(transport=httpx.MockTransport(core))
    t = httpx.AsyncClient(transport=httpx.MockTransport(telegram))
    bot = Bot(
        settings,
        CoreClient(c, settings),
        TelegramClient(t, TOKEN),
        OffsetStore(settings.state_file, settings.bot_id),
    )
    return bot, c, t, core_requests, replies, polls


def test_save_preserves_original_and_deduplicates_across_restart(settings):
    async def scenario():
        update = message()
        bot, c, t, calls, replies, polls = await session(settings, [update, update])
        async with c, t:
            await bot.poll_once()
            assert len(calls) == len(replies) == 1
            assert json.loads(calls[0].content)["text"] == update["message"]["text"]
            assert calls[0].headers["authorization"] == "Bearer " + "s" * 32
            bot.state = OffsetStore(settings.state_file, settings.bot_id)
            await bot.poll_once()
            assert polls[-1]["offset"] == 11
            assert len(calls) == 1
            assert "Сохран" not in settings.state_file.read_text()
            assert settings.state_file.stat().st_mode & 0o777 == 0o600

    asyncio.run(scenario())


def test_daily_limit_reply_says_saved_without_ai(settings):
    async def scenario():
        limited = {**SAVED, "ai_limit_exceeded": True}
        bot, c, t, calls, replies, _ = await session(
            settings, [message(10), message(11)],
            lambda request: httpx.Response(200, json=limited if json.loads(request.content)["update_id"] == 10 else SAVED),
        )
        async with c, t:
            await bot.poll_once()
            assert replies[0]["text"] == (
                "Лимит ИИ на сегодня исчерпан. Запись сохранена без ИИ, разобрать её можно завтра."
            )
            assert replies[1]["text"] == "Запись сохранена. Результат и статус обработки доступны в Beresta."

    asyncio.run(scenario())


def test_outage_does_not_acknowledge_or_skip_later_updates(settings):
    async def scenario():
        def outage(request):
            if json.loads(request.content)["update_id"] == 11:
                return httpx.Response(503)
            return httpx.Response(200, json=SAVED)

        bot, c, t, calls, replies, _ = await session(
            settings, [message(10), message(11), message(12)], outage
        )
        async with c, t:
            with pytest.raises(RemoteFailure):
                await bot.poll_once()
            assert len(calls) == 2
            assert len(replies) == 1
            assert OffsetStore(settings.state_file, settings.bot_id).offset == 11

    asyncio.run(scenario())


def test_commit_then_timeout_reuses_same_key(settings):
    async def scenario():
        captures, attempts = {}, []

        def core(request):
            payload = json.loads(request.content)
            key = (payload["bot_id"], payload["update_id"])
            captures.setdefault(key, payload["text"])
            attempts.append(key)
            if len(attempts) == 1:
                raise httpx.ReadTimeout("private exception must not escape", request=request)
            return httpx.Response(200, json=SAVED)

        bot, c, t, _, replies, _ = await session(settings, [message()], core)
        async with c, t:
            with pytest.raises(RemoteFailure, match="remote_unavailable"):
                await bot.poll_once()
            assert not settings.state_file.exists()
            await bot.poll_once()
            assert len(captures) == len(replies) == 1
            assert attempts[0] == attempts[1]

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [401, 403, 404, 405, 422])
def test_missing_core_or_auth_halts_without_losing_message(settings, status):
    async def scenario():
        bot, c, t, _, replies, _ = await session(settings, [message()], lambda _: httpx.Response(status))
        async with c, t:
            with pytest.raises(RemoteFailure) as caught:
                await bot.poll_once()
            assert caught.value.fatal
            assert not replies and not settings.state_file.exists()

    asyncio.run(scenario())


def test_payload_conflict_halts_without_advancing_offset(settings):
    async def scenario():
        def core(request):
            if json.loads(request.content)["update_id"] == 11:
                return httpx.Response(409, json={"error": {"code": "conflict"}})
            return httpx.Response(200, json=SAVED)

        bot, c, t, calls, replies, _ = await session(
            settings, [message(10), message(11), message(12)], core
        )
        async with c, t:
            with pytest.raises(RemoteFailure, match="core_update_conflict") as caught:
                await bot.poll_once()
            assert caught.value.fatal
            assert len(calls) == 2
            assert len(replies) == 1
            assert OffsetStore(settings.state_file, settings.bot_id).offset == 11

    asyncio.run(scenario())


@pytest.mark.parametrize("code", ["link_expired", "action_expired", "link_conflict", "invalid_link"])
def test_known_409_rejection_completes_update(settings, code):
    async def scenario():
        bot, c, t, calls, replies, _ = await session(
            settings, [message()],
            lambda _: httpx.Response(409, json={"error": {"code": code, "message": "private"}}),
        )
        async with c, t:
            await bot.poll_once()
            assert len(calls) == len(replies) == 1
            assert "private" not in str(replies)
            assert OffsetStore(settings.state_file, settings.bot_id).offset == 11

    asyncio.run(scenario())


def test_not_linked_is_explained_without_claiming_saved(settings):
    async def scenario():
        bot, c, t, _, replies, _ = await session(
            settings,
            [message()],
            lambda _: httpx.Response(
                403, json={"error": {"code": "telegram_not_linked", "message": "untrusted secret"}}
            ),
        )
        async with c, t:
            await bot.poll_once()
            assert "Подключи Telegram" in replies[0]["text"]
            assert "сохранена" not in replies[0]["text"]
            assert "untrusted" not in str(replies)

    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["/help", "/start", "/unknown", "/save", "a" * 12001])
def test_local_commands_and_limits_do_not_call_core(settings, text):
    async def scenario():
        bot, c, t, calls, replies, _ = await session(settings, [message(text=text)])
        async with c, t:
            await bot.poll_once()
            assert not calls
            assert len(replies) == 1

    asyncio.run(scenario())


def test_manual_mode_and_link_request(settings):
    async def scenario():
        def core(request):
            if request.url.path.endswith("/link-request"):
                return httpx.Response(200, json={"status": "pending", "link_request_id": "test-request"})
            return httpx.Response(200, json=SAVED)

        bot, c, t, calls, replies, _ = await session(
            settings, [message(10, "/save  исходник"), message(11, "/start " + "a" * 22)], core
        )
        async with c, t:
            await bot.poll_once()
            assert json.loads(calls[0].content)["processing_mode"] == "manual"
            assert json.loads(calls[0].content)["text"] == " исходник"
            assert calls[1].url.path.endswith("/link-request")
            assert "Подтверждаю" in replies[1]["text"]
            assert replies[1]["reply_markup"]["inline_keyboard"][0][0]["url"] == (
                "https://example.test/#telegram-confirm"
            )
            await bot.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("mutate", ["group", "different_actor", "bot"])
def test_ignore_non_private_or_invalid_actor(settings, mutate):
    async def scenario():
        update = message()
        if mutate == "group":
            update["message"]["chat"]["type"] = "group"
        elif mutate == "different_actor":
            update["message"]["from"]["id"] = 202
        else:
            update["message"]["from"]["is_bot"] = True
        bot, c, t, calls, replies, _ = await session(settings, [update])
        async with c, t:
            await bot.poll_once()
            assert not calls and not replies

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "patch",
    [
        {"status": "queued"},
        {"capture_id": "invalid"},
        {"note_url": "https://evil.test/phishing"},
        {"job_id": "bad"},
    ],
)
def test_bad_core_response_not_acknowledged(settings, patch):
    async def scenario():
        bot, c, t, _, replies, _ = await session(
            settings, [message()], lambda _: httpx.Response(200, json={**SAVED, **patch})
        )
        async with c, t:
            with pytest.raises(RemoteFailure):
                await bot.poll_once()
            assert not replies and not settings.state_file.exists()

    asyncio.run(scenario())


def test_reply_timeout_does_not_repeat_saved_operation_or_leak_secrets(settings, caplog):
    async def scenario():
        def failure(request):
            if request.url.path.endswith("/sendMessage"):
                raise httpx.ReadTimeout("secret " + TOKEN, request=request)

        bot, c, t, calls, _, _ = await session(settings, [message()], tg_handler=failure)
        async with c, t:
            await bot.poll_once()
            await bot.poll_once()
            assert len(calls) == 1
            assert TOKEN not in caplog.text
            assert "Моя исходная" not in caplog.text

    asyncio.run(scenario())


def test_telegram_429_backoff_does_not_leak_description(settings):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    429,
                    json={
                        "ok": False,
                        "error_code": 429,
                        "description": TOKEN,
                        "parameters": {"retry_after": 47},
                    },
                )
            )
        ) as client:
            with pytest.raises(RemoteFailure) as caught:
                await TelegramClient(client, TOKEN).call("getUpdates", {})
            assert caught.value.retry_after == 47
            assert TOKEN not in str(caught.value)

    asyncio.run(scenario())


def test_voice_multipart_from_trusted_download(settings):
    async def scenario():
        update = message()
        del update["message"]["text"]
        update["message"]["voice"] = {"file_id": "abc", "duration": 2, "file_size": 7}

        def tg(request):
            if request.url.path.endswith("/getFile"):
                return httpx.Response(200, json={"ok": True, "result": {"file_path": "voice/a.oga"}})
            if "/file/" in request.url.path:
                return httpx.Response(200, content=b"OggS123")

        bot, c, t, calls, replies, _ = await session(settings, [update], tg_handler=tg)
        async with c, t:
            await bot.poll_once()
            assert calls[0].url.path.endswith("/telegram/voice")
            assert b"OggS123" in calls[0].content
            assert b'name="telegram_user_id"' in calls[0].content
            assert len(replies) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["https://evil.test/a", "../secret", "/absolute", "voice/%2e%2e/a"])
def test_voice_rejects_untrusted_file_path(settings, path):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"ok": True, "result": {"file_path": path}})
            )
        ) as client:
            with pytest.raises(RemoteFailure):
                await TelegramClient(client, TOKEN).voice("id")

    asyncio.run(scenario())


def test_voice_enforces_actual_size_not_telegram_metadata(settings):
    async def scenario():
        def tg(request):
            if request.method == "POST":
                return httpx.Response(200, json={"ok": True, "result": {"file_path": "voice/a.oga"}})
            return httpx.Response(200, content=b"x" * (MAX_AUDIO_BYTES + 1))

        async with httpx.AsyncClient(transport=httpx.MockTransport(tg)) as client:
            with pytest.raises(Rejected, match="input_too_large"):
                await TelegramClient(client, TOKEN).voice("id")

    asyncio.run(scenario())


def test_callback_identity_is_derived_from_actor(settings):
    async def scenario():
        update = {
            "update_id": 20,
            "callback_query": {
                "id": "callback",
                "from": {"id": 101},
                "message": {"chat": {"type": "private", "id": 101}},
                "data": "opaque-token",
            },
        }
        bot, c, t, calls, _, _ = await session(
            settings, [update], lambda _: httpx.Response(200, json={"status": "completed"})
        )
        async with c, t:
            await bot.poll_once()
            payload = json.loads(calls[0].content)
            assert payload["telegram_user_id"] == 101
            assert "user_id" not in payload
            assert payload["callback_token"] == "opaque-token"

    asyncio.run(scenario())


def test_disabled_network_does_not_read_secrets(monkeypatch):
    monkeypatch.delenv("BERESTA_BOT_NETWORK_ENABLED", raising=False)
    monkeypatch.setenv("BERESTA_BOT_TOKEN_FILE", "/must-not-read")
    with pytest.raises(ValueError, match="Network is disabled"):
        Settings.from_env()


@pytest.mark.parametrize(
    "url",
    [
        "http://public.test",
        "https://user:pass@example.test",
        "https://example.test/path",
        "https://example.test?token=x",
    ],
)
def test_invalid_public_origin_rejected(settings, url):
    with pytest.raises(ValueError):
        replace(settings, web_url=url)


def test_secrets_hidden_in_settings_repr(settings):
    assert TOKEN not in repr(settings)
    assert settings.service_token not in repr(settings)


def test_corrupt_checkpoint_is_not_silently_reset(settings):
    settings.state_file.write_text('{"bot_id":999,"offset":11}')
    with pytest.raises(ValueError):
        OffsetStore(settings.state_file, settings.bot_id)


def test_two_pollers_cannot_share_checkpoint(settings):
    from telegram_adapter.state import InstanceLock

    with InstanceLock(settings.state_file):
        with pytest.raises(ValueError, match="Another bot process"):
            with InstanceLock(settings.state_file):
                pass
    with InstanceLock(settings.state_file):
        pass


def test_core_retry_after_is_respected(settings):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(429, headers={"Retry-After": "43"}))
        ) as client:
            with pytest.raises(RemoteFailure) as caught:
                await CoreClient(client, settings).post("/telegram/updates", {})
            assert caught.value.retry_after == 43

    asyncio.run(scenario())


def test_secret_setup_hides_token_and_refuses_overwrite(tmp_path, monkeypatch, capsys):
    from telegram_adapter import setup_secrets

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(setup_secrets.getpass, "getpass", lambda _: TOKEN)
    assert setup_secrets.main() == 0
    assert TOKEN not in capsys.readouterr().out
    path = tmp_path / "secrets/telegram_token.txt"
    assert path.read_text().strip() == TOKEN
    assert path.stat().st_mode & 0o777 == 0o600
    assert setup_secrets.main() == 1
    assert path.read_text().strip() == TOKEN


def test_malformed_batch_does_not_commit_partial_offset(settings):
    async def scenario():
        bot, c, t, calls, _, _ = await session(settings, [message(), {"update_id": "invalid"}])
        async with c, t:
            with pytest.raises(RemoteFailure):
                await bot.poll_once()
            assert not calls and not settings.state_file.exists()

    asyncio.run(scenario())


def test_redirect_cannot_be_mistaken_for_saved_capture(settings):
    async def scenario():
        bot, c, t, _, replies, _ = await session(
            settings,
            [message()],
            lambda _: httpx.Response(307, headers={"Location": "https://evil.test"}, json=SAVED),
        )
        async with c, t:
            with pytest.raises(RemoteFailure, match="core_redirect_refused"):
                await bot.poll_once()
            assert not replies and not settings.state_file.exists()

    asyncio.run(scenario())


def test_backoff_does_not_retry_before_long_provider_limit():
    assert RemoteFailure(retry_after=600).retry_after == 600
