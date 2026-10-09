"""Opt-in OpenAI-compatible STT candidate; deployment keeps it disabled."""

import io
import json
import os
import subprocess
import sys
import wave

import httpx

from app.audio_contracts import MAX_AUDIO_BYTES, MAX_AUDIO_SECONDS
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

    def __init__(self, api_key, model, *, base_url, transport=None, normalize_audio=False, ffmpeg_path="ffmpeg"):
        if base_url.rstrip("/") not in ALLOWED_MODEL_BASE_URLS or not model:
            raise ValueError("STT requires an approved HTTPS endpoint and model")
        self._api_key, self.model_name = api_key, model
        self.endpoint = base_url.rstrip("/") + "/audio/transcriptions"
        self._transport = transport
        self.normalize_audio, self.ffmpeg_path = normalize_audio, ffmpeg_path

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
        if self.normalize_audio:
            audio = normalized_wav(bytes(audio), self.ffmpeg_path)
            extension, media_type = "wav", "audio/wav"
        try:
            with httpx.Client(timeout=httpx.Timeout(45, connect=10), follow_redirects=False,
                              trust_env=False, transport=self._transport) as client:
                with client.stream(
                    "POST", self.endpoint, headers={"Authorization": f"Bearer {self._api_key}"},
                    data={"model": self.model_name, "language": "ru"},
                    files={"file": ("recording." + extension, bytes(audio), media_type)},
                ) as response:
                    if response.status_code != 200:
                        code = {401: "stt_auth", 403: "stt_auth", 404: "stt_model_not_found",
                                408: "stt_timeout_unknown", 429: "stt_rate_limit",
                                500: "stt_unavailable", 502: "stt_unavailable",
                                503: "stt_unavailable", 504: "stt_timeout_unknown"}.get(
                                    response.status_code, "stt_http_" + str(response.status_code))
                        error = ProviderError(code, http_status=response.status_code)
                        error.diagnostic_stage = safe_error_stage(response)
                        raise error
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
    return CloudRuSpeechProvider(key, model, base_url=settings.cloudru_base_url,
                                 normalize_audio=True, ffmpeg_path=settings.audio_ffmpeg_path)


def normalized_wav(audio, ffmpeg_path):
    """Convert a bounded copy to 16 kHz mono WAV; never overwrite the stored original."""
    if not audio or len(audio) > MAX_AUDIO_BYTES:
        raise ProviderError("audio_invalid")
    maximum_pcm = MAX_AUDIO_SECONDS * 16000 * 2
    command = [sys.executable, "-m", "app.audio_process", ffmpeg_path, "-nostdin", "-v", "error",
               "-threads", "1", "-i", "pipe:0", "-map", "0:a:0", "-vn", "-threads", "1",
               "-filter_threads", "1", "-ac", "1", "-ar", "16000",
               "-t", str(MAX_AUDIO_SECONDS + 1), "-fs", str(maximum_pcm + 32000),
               "-f", "s16le", "pipe:1"]
    try:
        result = subprocess.run(command, input=audio, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=25, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ProviderError("stt_audio_conversion_failed") from None
    pcm = result.stdout
    if result.returncode or not pcm or len(pcm) > maximum_pcm or len(pcm) % 2:
        raise ProviderError("stt_audio_conversion_failed")
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(pcm)
    return output.getvalue()


def safe_error_stage(response):
    """Return a fixed diagnostic category, never provider text or request contents."""
    body = bytearray()
    try:
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > 16000:
                return "unclassified"
        data = json.loads(body)
        error = data.get("error", {}) if isinstance(data, dict) else {}
        message = error.get("message", "") if isinstance(error, dict) else ""
        if not isinstance(message, str):
            return "unclassified"
        message = message.lower()
        for needle, category in (("response_format", "response_format"), ("language", "language"),
                                 ("unsupported audio", "audio_format"), ("file format", "audio_format"),
                                 ("unsupported file", "audio_format"), ("no deployments", "model_routing"),
                                 ("model not found", "model_routing"), ("llm provider", "provider_routing"),
                                 ("api key", "upstream_auth")):
            if needle in message:
                return category
    except (ValueError, TypeError):
        pass
    return "unclassified"
