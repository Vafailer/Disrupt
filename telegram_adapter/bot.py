import asyncio
import logging
import re
import time

from .clients import MAX_AUDIO_BYTES, REJECTIONS, CoreClient, Rejected, RemoteFailure, TelegramClient
from .config import Settings
from .state import OffsetStore

logger = logging.getLogger(__name__)

BTN_WRITE, BTN_MANUAL = "📝 Записать", "✍️ Без ИИ"
BTN_LINK, BTN_WEB = "🔗 Подключить аккаунт", "🌐 Открыть beresta"
BTN_HELP, BTN_MENU = "❓ Помощь", "🏠 Меню"
BUTTONS = {BTN_WRITE, BTN_MANUAL, BTN_LINK, BTN_WEB, BTN_HELP, BTN_MENU}
MAIN_MENU = {
    "keyboard": [
        [{"text": BTN_WRITE}, {"text": BTN_MANUAL}],
        [{"text": BTN_LINK}, {"text": BTN_WEB}],
        [{"text": BTN_HELP}, {"text": BTN_MENU}],
    ],
    "is_persistent": True,
    "resize_keyboard": True,
}
BOT_COMMANDS = [
    {"command": "start", "description": "Начать и подключить аккаунт"},
    {"command": "menu", "description": "Показать кнопки"},
    {"command": "help", "description": "Что умеет бот"},
    {"command": "save", "description": "Сохранить текст без ИИ"},
]
HELP_TEXT = (
    "Что умеют кнопки.\n"
    "📝 Записать. Напиши текст или отправь голосовое, я сохраню запись.\n"
    "✍️ Без ИИ. Следующее сообщение сохраню как есть, без разбора.\n"
    "🔗 Подключить аккаунт. Свяжу этот чат с beresta.\n"
    "🌐 Открыть beresta. Ссылка на веб.\n"
    "🏠 Меню. Вернуть кнопки, если они пропали.\n\n"
    "Голосовое тоже сохраняется. Текст из него появится в beresta."
)
MANUAL_TTL = 600
WATCH_TTL = 600
WATCH_INTERVAL = 3.0


class Bot:
    def __init__(self, settings: Settings, core: CoreClient, telegram: TelegramClient, state: OffsetStore):
        self.settings, self.core, self.telegram, self.state = settings, core, telegram, state
        self.clock = time.monotonic
        self.watch_interval = WATCH_INTERVAL
        # In-memory only: a restart forgets pending "next message without AI" marks and link watchers.
        self.manual_until = {}
        self.watchers = {}

    @property
    def web(self):
        return self.settings.web_url.rstrip("/")

    async def setup(self):
        """Best effort: the command list is a convenience, never a reason to stop the bot."""
        try:
            await self.telegram.call("setMyCommands", {"commands": BOT_COMMANDS})
        except RemoteFailure as error:
            logger.warning("bot_commands_not_set code=%s", error.code)

    async def close(self):
        tasks = list(self.watchers.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.watchers.clear()

    def start_watch(self, chat_id, telegram_user_id, link_request_id):
        old = self.watchers.get(chat_id)
        if old is not None:
            old.cancel()
        task = asyncio.create_task(self._watch(chat_id, telegram_user_id, link_request_id))
        self.watchers[chat_id] = task

        def forget(done):
            if self.watchers.get(chat_id) is done:
                del self.watchers[chat_id]

        task.add_done_callback(forget)

    async def _watch(self, chat_id, telegram_user_id, link_request_id):
        deadline = self.clock() + WATCH_TTL
        try:
            while self.clock() < deadline:
                await asyncio.sleep(self.watch_interval)
                try:
                    data = await self.core.get(
                        "/telegram/link-requests/" + link_request_id,
                        {"telegram_user_id": telegram_user_id},
                    )
                except RemoteFailure as error:
                    logger.warning("bot_watch_paused code=%s", error.code)
                    if error.fatal:
                        return
                    continue
                if data is None or data.get("status") == "expired":
                    return
                if data.get("status") == "confirmed":
                    name = data.get("username")
                    name = name if isinstance(name, str) and 0 < len(name) <= 64 else None
                    text = "Вы присоединили учётную запись. Добро пожаловать" + (f", {name}!" if name else "!")
                    await self.notify(chat_id, text, reply_markup=MAIN_MENU)
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never log exception text: it may carry URLs or private payloads.
            logger.warning("bot_watch_failed")

    async def notify(self, chat_id, text, url=None, *, button="Открыть Beresta", reply_markup=None):
        # Capture acceptance is durable even when its acknowledgement cannot be delivered.
        # Never replay an ambiguous sendMessage just to obtain a success response.
        try:
            await self.telegram.reply(chat_id, text, url, button=button, reply_markup=reply_markup)
        except RemoteFailure as error:
            if error.fatal:
                raise
            logger.warning("bot_reply_unconfirmed code=%s", error.code)

    async def handle(self, update):
        callback = update.get("callback_query")
        message = callback.get("message") if isinstance(callback, dict) else update.get("message")
        if not isinstance(message, dict):
            return
        sender = callback.get("from", {}) if callback else message.get("from", {})
        chat = message.get("chat", {})
        if (
            chat.get("type") != "private"
            or type(sender.get("id")) is not int
            or chat.get("id") != sender["id"]
            or sender.get("is_bot")
        ):
            return
        chat_id = chat["id"]
        identity = {
            "bot_id": self.settings.bot_id,
            "update_id": update["update_id"],
            "telegram_user_id": sender["id"],
            "chat_id": chat_id,
        }
        try:
            if callback:
                token = callback.get("data")
                if not isinstance(token, str) or not 1 <= len(token.encode()) <= 64:
                    return
                payload = {k: v for k, v in identity.items() if k != "chat_id"}
                result = await self.core.post("/telegram/actions", {**payload, "callback_token": token})
                if result.get("status") not in {"completed", "already_completed"}:
                    raise RemoteFailure("core_invalid_response")
                await self.notify(chat_id, "Задача выполнена.")
                try:
                    await self.telegram.call("answerCallbackQuery", {"callback_query_id": callback["id"]})
                except RemoteFailure as error:
                    if error.fatal:
                        raise
                return
            text = message.get("text", "")
            command, _, argument = text.partition(" ") if isinstance(text, str) else ("", "", "")
            command = command.split("@", 1)[0]
            # Button labels arrive as plain text: answer them before anything can be saved as a note.
            if isinstance(text, str) and text.strip() in BUTTONS:
                await self.press(chat_id, text.strip())
                return
            if command in {"/menu", "/help"} or (command == "/start" and not argument.strip()):
                if command == "/help":
                    await self.notify(chat_id, HELP_TEXT, reply_markup=MAIN_MENU)
                else:
                    await self.notify(chat_id, "Привет! Кнопки внизу, выбирай.", reply_markup=MAIN_MENU)
                return
            if command == "/start":
                if not re.fullmatch(r"[A-Za-z0-9_-]{22,64}", argument):
                    raise Rejected("invalid_link")
                result = await self.core.post("/telegram/link-request", {**identity, "code": argument})
                link_id = result.get("link_request_id")
                if (
                    result.get("status") != "pending"
                    or not isinstance(link_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", link_id)
                ):
                    raise RemoteFailure("core_invalid_response")
                await self.notify(
                    chat_id,
                    "Почти готово. Подтверждаю подключение в beresta…",
                    self.web + "/#telegram-confirm",
                    button="Открыть beresta",
                )
                self.start_watch(chat_id, sender["id"], link_id)
                return
            if isinstance(text, str) and text:
                mode = "manual" if command == "/save" else "ai"
                content = argument if mode == "manual" else text
                if command.startswith("/") and mode != "manual":
                    await self.notify(chat_id, "Команда неизвестна. Доступны /menu, /help и /save текст записи.")
                    return
                if mode == "ai" and self.manual_until.pop(chat_id, 0) > self.clock():
                    mode = "manual"
                if not content.strip():
                    raise Rejected("empty_input")
                if len(content) > 12000:
                    raise Rejected("input_too_large")
                result = await self.core.post(
                    "/telegram/updates", {**identity, "text": content, "processing_mode": mode}
                )
            elif isinstance(message.get("voice"), dict):
                voice = message["voice"]
                if voice.get("duration", 181) > 180 or voice.get("file_size", 0) > MAX_AUDIO_BYTES:
                    raise Rejected("input_too_large")
                if not isinstance(voice.get("file_id"), str):
                    raise Rejected("unsupported_audio")
                audio = await self.telegram.voice(voice["file_id"])
                result = await self.core.post(
                    "/telegram/voice", {**identity, "processing_mode": "ai"}, audio=audio
                )
            else:
                await self.notify(
                    chat_id, "Отправь текст или голосовое сообщение. Этот тип вложения пока недоступен."
                )
                return
            url = self.core.validate_saved(result)
            saved = (
                "Лимит ИИ на сегодня исчерпан. Запись сохранена без ИИ, разобрать её можно завтра."
                if result.get("ai_limit_exceeded") is True
                else "Запись сохранена. Результат и статус обработки доступны в Beresta."
            )
            await self.notify(chat_id, saved, url)
        except Rejected as error:
            await self.notify(chat_id, REJECTIONS[str(error)], self.settings.web_url)

    async def press(self, chat_id, label):
        if label == BTN_WRITE:
            await self.notify(chat_id, "Напиши текст или отправь голосовое. Я сохраню и разберу его с ИИ.")
        elif label == BTN_MANUAL:
            self.manual_until[chat_id] = self.clock() + MANUAL_TTL
            await self.notify(chat_id, "Следующее сообщение сохраню без ИИ.")
        elif label == BTN_LINK:
            await self.notify(
                chat_id,
                "Открой beresta, нажми «Подключить Telegram», затем «Открыть бота».",
                self.web + "/#telegram",
                button="Открыть beresta",
            )
        elif label == BTN_WEB:
            await self.notify(chat_id, "Вот ссылка на beresta.", self.web, button="Открыть beresta")
        elif label == BTN_HELP:
            await self.notify(chat_id, HELP_TEXT, reply_markup=MAIN_MENU)
        else:
            await self.notify(chat_id, "Кнопки снова на месте.", reply_markup=MAIN_MENU)

    async def poll_once(self):
        payload = {"timeout": 25, "limit": 50, "allowed_updates": ["message", "callback_query"]}
        if self.state.offset is not None:
            payload["offset"] = self.state.offset
        updates = await self.telegram.call("getUpdates", payload)
        if not isinstance(updates, list):
            raise RemoteFailure("telegram_invalid_response")
        # Validate the whole batch before committing any offset.
        if any(
            not isinstance(u, dict) or type(u.get("update_id")) is not int or u["update_id"] < 0
            for u in updates
        ):
            raise RemoteFailure("telegram_invalid_response")
        for update in sorted(updates, key=lambda item: item["update_id"]):
            if self.state.offset is not None and update["update_id"] < self.state.offset:
                continue
            await self.handle(update)
            self.state.advance(update["update_id"])
