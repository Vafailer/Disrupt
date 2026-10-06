"""STT без сети. Текст mock задаётся явно для синтетических проверок."""

from app.audio_contracts import MAX_AUDIO_BYTES
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
