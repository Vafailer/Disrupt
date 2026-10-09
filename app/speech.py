"""Opt-in OpenAI-compatible STT candidate; deployment keeps it disabled."""

import json
import os

import httpx

from app.audio_contracts import MAX_AUDIO_BYTES
from app.config import ALLOWED_MODEL_BASE_URLS, Settings, read_secret_file
from app.providers import ProviderError


class MockSpeechProvider:
    def __init__(self, text: str):
        self.text = text
        self.calls = 0

    def transcribe(self, source, *, media_type):
        self.calls += 1
        total = 0
        while chunk := source.read(65536):
            total += len(chunk)
            if total > MAX_AUDIO_BYTES:
                raise ProviderError("audio_invalid")
        if not total:
            raise ProviderError("audio_invalid")
        return self.text


class CloudRuSpeechProvider:
    """No discovery, redirects or retries. Cloud.ru gateway protocol needs owner confirmation."""

    def __init__(self, api_key, model, *, base_url, transport=None):
        if base_url.rstrip("/") not in ALLOWED_MODEL_BASE_URLS or not model:
            raise ValueError("STT requires an approved HTTPS endpoint and model")
        self._api_key, self.model_name = api_key, model
        self.endpoint = base_url.rstrip("/") + "/audio/transcriptions"
        self._transport = transport

    def transcribe(self, source, *, media_type):
        extension = {"audio/ogg": "ogg", "audio/wav": "wav", "audio/webm": "webm"}.get(media_type)
        if extension is None:
            raise ProviderError("audio_invalid")
        # Bounded read works with streams that cannot seek; never writes/converts the original.
        audio = bytearray()
        while chunk := source.read(65536):
            audio.extend(chunk)
            if len(audio) > MAX_AUDIO_BYTES:
                raise ProviderError("audio_invalid")
        if not audio:
            raise ProviderError("audio_invalid")
        try:
            with httpx.Client(timeout=httpx.Timeout(45, connect=10), follow_redirects=False,
                              trust_env=False, transport=self._transport) as client:
                with client.stream(
                    "POST", self.endpoint, headers={"Authorization": f"Bearer {self._api_key}"},
                    data={"model": self.model_name, "response_format": "json", "language": "ru"},
                    files={"file": ("recording." + extension, bytes(audio), media_type)},
                ) as response:
                    if response.status_code != 200:
                        code = {401: "stt_auth", 403: "stt_auth", 404: "stt_model_not_found",
                                408: "stt_timeout_unknown", 429: "stt_rate_limit",
                                500: "stt_unavailable", 502: "stt_unavailable",
                                503: "stt_unavailable", 504: "stt_timeout_unknown"}.get(
                                    response.status_code, "stt_http_" + str(response.status_code))
                        raise ProviderError(code, http_status=response.status_code)
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > 256000:
                            raise ProviderError("stt_response_too_large")
            text = json.loads(content)["text"]
            if not isinstance(text, str) or not text.strip() or "\x00" in text or len(text) > 12000:
                raise ProviderError("stt_invalid_response")
            return text
        except httpx.TimeoutException:
            raise ProviderError("stt_timeout_unknown") from None
        except httpx.HTTPError:
            raise ProviderError("stt_connection_unknown") from None
        except (ValueError, KeyError, TypeError):
            raise ProviderError("stt_invalid_response") from None


def make_speech_provider(settings: Settings):
    # Do not read credentials in mock mode or while STT is disabled.
    if settings.provider == "mock" or os.environ.get("NOTES_STT_ENABLED", "false") == "false":
        return None
    if os.environ.get("NOTES_STT_ENABLED") != "true" or not settings.allow_live_requests:
        raise ValueError("STT requires explicit live permission")
    if os.environ.get("NOTES_STT_PROTOCOL_CONFIRMED") != "true":
        raise ValueError("Confirm the gateway STT protocol before enabling it")
    model = os.environ.get("NOTES_STT_MODEL", "")
    if not model:
        raise ValueError("STT requires an explicitly confirmed model")
    key = os.environ.get("NOTES_CLOUDRU_API_KEY", "")
    key_file = os.environ.get("NOTES_CLOUDRU_API_KEY_FILE", "")
    if key and key_file:
        raise ValueError("Use either API key or API key file")
    if key_file:
        key = read_secret_file(key_file)
    if not key:
        raise ValueError("STT requires a worker credential")
    return CloudRuSpeechProvider(key, model, base_url=settings.cloudru_base_url)
