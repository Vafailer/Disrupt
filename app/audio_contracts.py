"""Типы ядра. Само хранилище реализуется в app.audio_storage."""

from contextlib import AbstractContextManager
from typing import BinaryIO, Protocol

MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_AUDIO_SECONDS = 180
AUDIO_MEDIA_TYPES = frozenset({"audio/ogg", "audio/wav", "audio/webm"})


class AudioInfoLike(Protocol):
    @property
    def sha256(self) -> str: ...

    @property
    def byte_count(self) -> int: ...

    @property
    def duration_seconds(self) -> float: ...

    @property
    def media_type(self) -> str: ...


class StoredAudioLike(Protocol):
    @property
    def key(self) -> str: ...

    @property
    def info(self) -> AudioInfoLike: ...


class AudioReader(Protocol):
    def open_original(self, key: str) -> AbstractContextManager[BinaryIO]: ...


class SpeechProvider(Protocol):
    def transcribe(self, source: BinaryIO, *, media_type: str) -> str: ...
