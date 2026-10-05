import re
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from .config import Settings

MAX_AUDIO_BYTES = 10 * 1024 * 1024


class RemoteFailure(Exception):
    """Sanitized error: never retain response bodies, URLs containing tokens, or note text."""

    def __init__(self, code="remote_unavailable", *, fatal=False, retry_after=3):
        super().__init__(code)
        self.code = code
        self.fatal = fatal
        self.retry_after = max(1, retry_after)


class Rejected(Exception):
    pass


REJECTIONS = {
    "telegram_not_linked": "Подключи Telegram в своем аккаунте в веб-приложении, затем отправь запись еще раз.",
    "link_expired": "Ссылка истекла. Создай новую в веб-приложении.",
    "link_conflict": "Этот Telegram уже связан с аккаунтом. Проверь связь в веб-приложении.",
    "invalid_link": "Ссылка недействительна. Создай новую в веб-приложении.",
    "input_too_large": "Запись слишком большая. Максимум: 12 000 символов или 3 минуты и 10 МБ аудио.",
    "unsupported_audio": "Формат аудио не поддерживается. Отправь голосовое сообщение Telegram.",
    "empty_input": "В записи нет текста.",
    "quota_exceeded": "Лимит обработки исчерпан. Сохрани текст без ИИ командой /save текст записи.",
    "action_forbidden": "Это действие недоступно для твоего аккаунта.",
    "action_expired": "Кнопка устарела. Открой задачу в веб-приложении.",
}


class CoreClient:
    def __init__(self, client: httpx.AsyncClient, settings: Settings):
        self.client, self.settings = client, settings

    async def post(self, path, payload, *, audio=None):
        kwargs = (
            {"json": payload}
            if audio is None
            else {
                "data": {k: str(v) for k, v in payload.items()},
                "files": {"audio": ("voice.ogg", audio, "audio/ogg")},
            }
        )
        try:
            response = await self.client.post(
                self.settings.core_url + "/internal/v1" + path,
                headers={"Authorization": "Bearer " + self.settings.service_token},
                **kwargs,
            )
        except httpx.HTTPError:
            raise RemoteFailure() from None
        if 300 <= response.status_code < 400:
            raise RemoteFailure("core_redirect_refused", fatal=True)
        if response.status_code >= 400:
            try:
                code = response.json()["error"]["code"]
            except (ValueError, KeyError, TypeError):
                code = None
            if (
                isinstance(code, str)
                and code in REJECTIONS
                and response.status_code in {400, 403, 409, 413, 422, 429}
            ):
                raise Rejected(code)
            if response.status_code == 409 and code == "conflict":
                raise RemoteFailure("core_update_conflict", fatal=True)
            # A missing route / service credential error is NOT a rejected user message.
            # Stop without acknowledging its update so deployment can be repaired safely.
            retry = response.headers.get("Retry-After", "3")
            retry = int(retry) if retry.isdigit() and len(retry) < 8 else 3
            raise RemoteFailure(
                "core_contract_or_auth" if response.status_code < 500 else "core_unavailable",
                fatal=response.status_code in {401, 403, 404, 405, 422},
                retry_after=retry,
            )
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except ValueError:
            raise RemoteFailure("core_invalid_response") from None

    def validate_saved(self, data):
        try:
            UUID(data["capture_id"])
            if data.get("job_id") is not None:
                UUID(data["job_id"])
            if data["status"] != "saved":
                raise ValueError()
            url = data["note_url"]
            parsed, expected = urlsplit(url), urlsplit(self.settings.web_url)
            if (parsed.scheme, parsed.netloc) != (expected.scheme, expected.netloc) or parsed.username:
                raise ValueError()
            if any(ord(char) < 32 for char in url):
                raise ValueError()
            return url
        except (ValueError, KeyError, TypeError, AttributeError):
            raise RemoteFailure("core_invalid_response") from None


class TelegramClient:
    def __init__(self, client: httpx.AsyncClient, token: str):
        self.client = client
        self._base = "https://api.telegram.org/bot" + token + "/"
        self._files = "https://api.telegram.org/file/bot" + token + "/"

    async def call(self, method, payload):
        try:
            response = await self.client.post(self._base + method, json=payload)
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise RemoteFailure("telegram_unknown") from None
        if not isinstance(data, dict):
            raise RemoteFailure("telegram_invalid_response")
        if data.get("ok") is not True or not response.is_success:
            code = data.get("error_code", response.status_code)
            params = data.get("parameters") or {}
            retry = params.get("retry_after", 3) if isinstance(params, dict) else 3
            raise RemoteFailure(
                f"telegram_{code}" if code in {400, 401, 403, 409, 429} else "telegram_unknown",
                fatal=code in {401, 409},
                retry_after=retry if type(retry) is int else 3,
            )
        return data.get("result")

    async def reply(self, chat_id, text, url=None):
        payload = {"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}}
        if url:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": "Открыть Beresta", "url": url}]]}
        # Plain text: never interpret user/model supplied Markdown or HTML.
        return await self.call("sendMessage", payload)

    async def voice(self, file_id):
        result = await self.call("getFile", {"file_id": file_id})
        path = result.get("file_path") if isinstance(result, dict) else None
        if (
            not isinstance(path, str)
            or not re.fullmatch(r"[A-Za-z0-9_./-]+", path)
            or path.startswith("/")
            or ".." in path.split("/")
        ):
            raise RemoteFailure("telegram_invalid_file")
        chunks, size = [], 0
        try:
            async with self.client.stream("GET", self._files + path) as response:
                if response.status_code != 200:
                    raise RemoteFailure("telegram_file_unavailable")
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_AUDIO_BYTES:
                        raise Rejected("input_too_large")
                    chunks.append(chunk)
        except httpx.HTTPError:
            raise RemoteFailure("telegram_file_unavailable") from None
        return b"".join(chunks)
