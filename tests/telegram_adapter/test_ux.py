import asyncio
import json

import httpx
import pytest

from telegram_adapter.bot import BTN_HELP, BTN_LINK, BTN_MANUAL, BTN_MENU, BTN_WEB, BTN_WRITE, BUTTONS, MAIN_MENU
from tests.telegram_adapter.test_adapter import message, session
from tests.telegram_adapter.test_adapter import settings as settings

LINK_ID = "00000000-0000-4000-8000-000000000001"


def saved_requests(calls):
    return [json.loads(call.content) for call in calls if call.url.path.endswith("/updates")]


@pytest.mark.parametrize("label", sorted(BUTTONS))
def test_button_labels_are_never_saved_as_notes(settings, label):
    async def scenario():
        bot, c, t, calls, replies, _ = await session(settings, [message(text=label)])
        async with c, t:
            await bot.poll_once()
            assert not calls
            assert len(replies) == 1

    asyncio.run(scenario())


def test_menu_keyboard_is_persistent_and_resent(settings):
    async def scenario():
        updates = [message(10, "/menu"), message(11, "/start"), message(12, BTN_MENU), message(13, BTN_HELP)]
        bot, c, t, calls, replies, _ = await session(settings, updates)
        async with c, t:
            await bot.poll_once()
            assert not calls
            assert len(replies) == 4
            for reply in replies:
                assert reply["reply_markup"] == MAIN_MENU
            assert MAIN_MENU["is_persistent"] is True and MAIN_MENU["resize_keyboard"] is True
            labels = [button["text"] for row in MAIN_MENU["keyboard"] for button in row]
            assert set(labels) == BUTTONS and len(labels) == 6

    asyncio.run(scenario())


def test_hint_reply_does_not_carry_the_keyboard(settings):
    async def scenario():
        bot, c, t, _, replies, _ = await session(settings, [message(10, BTN_WRITE)])
        async with c, t:
            await bot.poll_once()
            assert "reply_markup" not in replies[0]

    asyncio.run(scenario())


def test_link_and_web_buttons_use_inline_urls(settings):
    async def scenario():
        bot, c, t, _, replies, _ = await session(settings, [message(10, BTN_LINK), message(11, BTN_WEB)])
        async with c, t:
            await bot.poll_once()
            link = replies[0]["reply_markup"]["inline_keyboard"][0][0]
            assert link == {"text": "Открыть beresta", "url": "https://example.test/#telegram"}
            assert "Подключить Telegram" in replies[0]["text"]
            web = replies[1]["reply_markup"]["inline_keyboard"][0][0]
            assert web["url"] == "https://example.test"

    asyncio.run(scenario())


def test_manual_button_applies_to_next_text_only(settings):
    async def scenario():
        updates = [message(10, BTN_MANUAL), message(11, "Первая"), message(12, "Вторая")]
        bot, c, t, calls, replies, _ = await session(settings, updates)
        async with c, t:
            await bot.poll_once()
            sent = saved_requests(calls)
            assert [item["text"] for item in sent] == ["Первая", "Вторая"]
            assert [item["processing_mode"] for item in sent] == ["manual", "ai"]
            assert replies[0]["text"] == "Следующее сообщение сохраню без ИИ."

    asyncio.run(scenario())


def test_manual_mark_survives_buttons_and_expires(settings):
    async def scenario():
        now = [1000.0]
        bot, c, t, calls, _, _ = await session(settings, [])
        bot.clock = lambda: now[0]
        async with c, t:
            await bot.handle(message(10, BTN_MANUAL))
            await bot.handle(message(11, BTN_HELP))
            await bot.handle(message(12, "Раз"))
            await bot.handle(message(13, BTN_MANUAL))
            now[0] += 601
            await bot.handle(message(14, "Позже"))
            modes = [item["processing_mode"] for item in saved_requests(calls)]
            assert modes == ["manual", "ai"]

    asyncio.run(scenario())


def test_start_with_code_replies_with_confirm_url_and_watcher_sends_welcome(settings):
    async def scenario():
        statuses = iter([
            {"status": "pending", "username": None},
            {"status": "confirmed", "username": "tester"},
        ])
        gets = []

        def core(request):
            if request.method == "GET":
                gets.append(request)
                return httpx.Response(200, json=next(statuses))
            return httpx.Response(200, json={"status": "pending", "link_request_id": LINK_ID})

        bot, c, t, calls, replies, _ = await session(settings, [message(10, "/start " + "a" * 22)], core)
        bot.watch_interval = 0
        async with c, t:
            await bot.poll_once()
            button = replies[0]["reply_markup"]["inline_keyboard"][0][0]
            assert button == {"text": "Открыть beresta", "url": "https://example.test/#telegram-confirm"}
            await asyncio.wait_for(bot.watchers[101], 2)
            assert [g.url.path for g in gets] == ["/internal/v1/telegram/link-requests/" + LINK_ID] * 2
            assert gets[0].url.params["telegram_user_id"] == "101"
            assert gets[0].headers["authorization"] == "Bearer " + "s" * 32
            assert replies[-1]["text"] == "Вы присоединили учётную запись. Добро пожаловать, tester!"
            assert replies[-1]["reply_markup"] == MAIN_MENU
            await asyncio.sleep(0)
            assert 101 not in bot.watchers

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [200, 404])
def test_watcher_stops_on_expired_or_missing(settings, status):
    async def scenario():
        def core(request):
            if request.method == "GET":
                return httpx.Response(status, json={"status": "expired", "username": None})
            return httpx.Response(200, json={"status": "pending", "link_request_id": LINK_ID})

        bot, c, t, _, replies, _ = await session(settings, [message(10, "/start " + "a" * 22)], core)
        bot.watch_interval = 0
        async with c, t:
            await bot.poll_once()
            await asyncio.wait_for(bot.watchers[101], 2)
            assert len(replies) == 1

    asyncio.run(scenario())


def test_new_watcher_replaces_older_one(settings):
    async def scenario():
        def core(request):
            return httpx.Response(200, json={"status": "pending", "link_request_id": LINK_ID})

        bot, c, t, _, _, _ = await session(
            settings, [message(10, "/start " + "a" * 22), message(11, "/start " + "b" * 22)], core
        )
        async with c, t:
            await bot.poll_once()
            assert len(bot.watchers) == 1
            await bot.close()
            assert not bot.watchers

    asyncio.run(scenario())


def test_set_my_commands_failure_does_not_crash(settings, caplog):
    async def scenario():
        def refuse(request):
            if request.url.path.endswith("/setMyCommands"):
                return httpx.Response(400, json={"ok": False, "error_code": 400, "description": "private"})

        bot, c, t, _, _, _ = await session(settings, [], tg_handler=refuse)
        async with c, t:
            await bot.setup()

    asyncio.run(scenario())
    assert "bot_commands_not_set" in caplog.text
    assert "private" not in caplog.text


def test_set_my_commands_sends_russian_command_list(settings):
    async def scenario():
        bot, c, t, _, replies, _ = await session(settings, [])
        async with c, t:
            await bot.setup()
            assert [item["command"] for item in replies[0]["commands"]] == ["start", "menu", "help", "save"]

    asyncio.run(scenario())


def test_save_command_still_works(settings):
    async def scenario():
        bot, c, t, calls, _, _ = await session(settings, [message(10, "/save текст")])
        async with c, t:
            await bot.poll_once()
            assert saved_requests(calls)[0]["processing_mode"] == "manual"

    asyncio.run(scenario())
