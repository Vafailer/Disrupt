"""The bot half of Telegram sign-in: strict link format, one call to the core, honest replies."""

import asyncio
import json

import httpx
import pytest

from telegram_adapter.config import Settings
from tests.telegram_adapter.test_adapter import TOKEN, message, session

LOGIN_TOKEN = "A" * 43


@pytest.fixture
def settings(tmp_path):
    return Settings(TOKEN, "s" * 32, "http://core.test", "https://example.test", tmp_path / "offset.json")


def start_update(argument, update_id=10, username=None):
    update = message(update_id, "/start " + argument)
    if username is not None:
        update["message"]["from"]["username"] = username
    return update


def confirmed(purpose="login", new_account=False):
    return httpx.Response(200, json={"status": "confirmed", "purpose": purpose, "new_account": new_account})


@pytest.mark.parametrize(
    ("purpose", "new_account", "expected"),
    [
        ("login", False, "Вход подтверждён. Вернитесь в браузер."),
        ("login", True, "Вернитесь в браузер, там мы создадим ваш аккаунт."),
        ("delete", False, "Запрос на удаление аккаунта подтверждён."),
    ],
)
def test_login_link_calls_core_once_and_replies(settings, purpose, new_account, expected):
    async def scenario():
        bot, c, t, calls, replies, _ = await session(
            settings, [start_update("login_" + LOGIN_TOKEN, username="Ivan_Petrov")],
            lambda request: confirmed(purpose, new_account),
        )
        async with c, t:
            await bot.poll_once()
            assert len(calls) == 1 and calls[0].url.path == "/internal/v1/telegram/login-confirm"
            body = json.loads(calls[0].content)
            assert body["token"] == LOGIN_TOKEN and body["telegram_username"] == "Ivan_Petrov"
            assert body["telegram_user_id"] == body["chat_id"] == 101 and body["bot_id"] == settings.bot_id
            assert "first_name" not in body
            assert calls[0].headers["authorization"] == "Bearer " + "s" * 32
            assert len(replies) == 1 and expected in replies[0]["text"]
            assert TOKEN not in json.dumps(replies) and LOGIN_TOKEN not in json.dumps(replies)
            await bot.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("username", ["a" * 33, "bad name", "имя", ""])
def test_unusable_telegram_username_is_not_sent(settings, username):
    async def scenario():
        bot, c, t, calls, _, _ = await session(
            settings, [start_update("login_" + LOGIN_TOKEN, username=username)], lambda request: confirmed(),
        )
        async with c, t:
            await bot.poll_once()
            assert "telegram_username" not in json.loads(calls[0].content)
            await bot.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "argument",
    ["login_", "login_short", "login_" + "A" * 42, "login_" + "A" * 44, "login_" + "A" * 42 + "!", "login_" + "A" * 42 + " x"],
)
def test_malformed_login_link_never_reaches_the_login_call(settings, argument):
    async def scenario():
        def core(request):
            # Some of these strings are still valid link codes, so they go to the ordinary linking call.
            if request.url.path.endswith("/link-request"):
                return httpx.Response(200, json={"status": "pending", "link_request_id": "test-request"})
            return confirmed()

        bot, c, t, calls, replies, _ = await session(settings, [start_update(argument)], core)
        async with c, t:
            await bot.poll_once()
            assert all(not call.url.path.endswith("/login-confirm") for call in calls)
            assert len(replies) == 1
            await bot.close()

    asyncio.run(scenario())


def test_link_code_that_looks_like_a_login_prefix_still_links(settings):
    code = "login_" + "b" * 37  # 43 characters, a valid link code
    async def scenario():
        def core(request):
            return httpx.Response(200, json={"status": "pending", "link_request_id": "test-request"})

        bot, c, t, calls, replies, _ = await session(settings, [start_update(code)], core)
        async with c, t:
            await bot.poll_once()
            assert calls[0].url.path.endswith("/link-request")
            assert json.loads(calls[0].content)["code"] == code
            await bot.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (409, "login_expired", "истекла"),
        (409, "login_used", "уже использована"),
        (409, "invalid_login", "недействительна"),
        (403, "login_forbidden", "не связан"),
    ],
)
def test_core_refusals_are_explained_and_complete_the_update(settings, status, code, expected):
    async def scenario():
        refusal = httpx.Response(status, json={"error": {"code": code, "message": "x"}, "operation_id": "o"})
        bot, c, t, calls, replies, _ = await session(
            settings, [start_update("login_" + LOGIN_TOKEN, 10), start_update("login_" + LOGIN_TOKEN, 11)],
            lambda request: refusal,
        )
        async with c, t:
            await bot.poll_once()
            assert len(calls) == 2 and len(replies) == 2
            assert expected in replies[0]["text"]
            assert bot.state.offset == 12
            await bot.close()

    asyncio.run(scenario())


def test_invalid_core_answer_halts_without_advancing_the_offset(settings):
    from telegram_adapter.clients import RemoteFailure

    async def scenario():
        bot, c, t, calls, replies, _ = await session(
            settings, [start_update("login_" + LOGIN_TOKEN)],
            lambda request: httpx.Response(200, json={"status": "confirmed", "purpose": "other", "new_account": False}),
        )
        async with c, t:
            with pytest.raises(RemoteFailure):
                await bot.poll_once()
            assert replies == [] and bot.state.offset is None
            await bot.close()

    asyncio.run(scenario())
