import logging
import re

from .clients import MAX_AUDIO_BYTES, REJECTIONS, CoreClient, Rejected, RemoteFailure, TelegramClient
from .config import Settings
from .state import OffsetStore

logger = logging.getLogger(__name__)


class Bot:
    def __init__(self, settings: Settings, core: CoreClient, telegram: TelegramClient, state: OffsetStore):
        self.settings, self.core, self.telegram, self.state = settings, core, telegram, state

    async def notify(self, chat_id, text, url=None):
        # Capture acceptance is durable even when its acknowledgement cannot be delivered.
        # Never replay an ambiguous sendMessage just to obtain a success response.
        try:
            await self.telegram.reply(chat_id, text, url)
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
            if command in {"/start", "/help"}:
                if command == "/start" and argument:
                    if not re.fullmatch(r"[A-Za-z0-9_-]{22,64}", argument):
                        raise Rejected("invalid_link")
                    result = await self.core.post("/telegram/link-request", {**identity, "code": argument})
                    if result.get("status") != "pending" or not result.get("link_request_id"):
                        raise RemoteFailure("core_invalid_response")
                    await self.notify(
                        chat_id,
                        "Запрос на подключение отправлен. Вернись в Beresta, проверь свой Telegram "
                        "и подтверди привязку. До подтверждения записи из бота не сохраняются.",
                        self.settings.web_url,
                    )
                else:
                    await self.notify(
                        chat_id,
                        "Войди в Beresta, подключи Telegram и подтверди привязку в вебе. "
                        "Обычный текст отправляется на обработку ИИ. "
                        "Команда /save текст записи сохраняет текст без ИИ и автоматической структуры. "
                        "Голос сохраняется как оригинал; это ещё не готовая расшифровка. "
                        "Если распознавание выключено, расшифровка не появится. "
                        "Статус, оригинал и правки доступны в вебе. Можно отправить мысль текстом через /save.",
                        self.settings.web_url,
                    )
                return
            if isinstance(text, str) and text:
                mode = "manual" if command == "/save" else "ai"
                content = argument if mode == "manual" else text
                if command.startswith("/") and mode != "manual":
                    await self.notify(chat_id, "Команда неизвестна. Доступны /help и /save текст записи.")
                    return
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
            if isinstance(message.get("voice"), dict):
                notice = (
                    "Оригинал голосовой записи сохранён. Статус и аудио доступны по ссылке. "
                    "Это ещё не готовая расшифровка. Если распознавание выключено, "
                    "можно сохранить мысль текстом через /save текст записи."
                )
            elif mode == "manual":
                notice = "Текст сохранён без обработки ИИ. Открыть и отредактировать запись можно по ссылке."
            else:
                notice = "Запись сохранена. Результат и статус обработки доступны в Beresta."
            await self.notify(chat_id, notice, url)
        except Rejected as error:
            await self.notify(chat_id, REJECTIONS[str(error)], self.settings.web_url)

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
