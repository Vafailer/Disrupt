"""Шифрование пользовательского содержимого на диске.

AES-256-GCM, ключ лежит в файле на сервере. Тексты и аудио не читаются из дампа базы или папки с аудио.
Ключи, открытые тексты и расшифрованные данные никогда не попадают в логи и сообщения об ошибках.

Строка в базе выглядит так: enc:v1:<key_id>:<base64url(nonce12 + ciphertext)>.
Значения без префикса enc: считаются старым открытым текстом и возвращаются как есть.
"""

import base64
import binascii
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "enc:v1:"
MODES = ("off", "required")
NONCE_BYTES = 12
KEY_BYTES = 32
AUDIO_MAGIC = b"BERESTA-ENC1\n"
KEY_ID_BYTES = 8


class EncryptionError(Exception):
    """Базовая ошибка шифрования. Текст ошибки не содержит данных."""


class EncryptionConfigError(EncryptionError, ValueError):
    """Неверная настройка шифрования. Приложение не должно стартовать."""


class KeyUnavailable(EncryptionError):
    """Данные зашифрованы, а нужного ключа нет."""


class DecryptionFailed(EncryptionError):
    """Данные повреждены, подменены или относятся к другому полю."""


def key_id_of(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:KEY_ID_BYTES]


def parse_key(text: str) -> bytes:
    """Ключ хранится как base64 от 32 случайных байт. Подходит обычный и urlsafe вариант."""
    value = text.strip()
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)) if ("-" in value or "_" in value) else (
            base64.b64decode(value, validate=True)
        )
    except (binascii.Error, ValueError):
        raise EncryptionConfigError("Ключ данных должен быть в формате base64") from None
    if len(raw) != KEY_BYTES:
        raise EncryptionConfigError("Ключ данных должен состоять из 32 байт")
    return raw


def read_key_file(path: str) -> bytes:
    try:
        text = Path(path).read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        raise EncryptionConfigError("Файл ключа данных не читается") from None
    if len(text) > 1024:
        raise EncryptionConfigError("Файл ключа данных слишком большой")
    return parse_key(text)


def generate_key() -> str:
    return base64.b64encode(os.urandom(KEY_BYTES)).decode("ascii")


@dataclass(frozen=True)
class KeyRing:
    current_id: str
    ciphers: dict  # key_id -> AESGCM

    @classmethod
    def build(cls, current: bytes, old=()):
        ciphers = {key_id_of(key): AESGCM(key) for key in (*old, current)}
        return cls(key_id_of(current), ciphers)


@dataclass(frozen=True)
class State:
    mode: str = "off"
    ring: KeyRing | None = None


_state = State()


def state() -> State:
    return _state


def configure(mode: str = "off", key_file: str = "", old_key_files: str = "") -> State:
    """Готовит связку ключей. `required` без читаемого верного ключа даёт ошибку при старте."""
    global _state
    if mode not in MODES:
        raise EncryptionConfigError("NOTES_DATA_ENCRYPTION должен быть off или required")
    ring = None
    if key_file:
        current = read_key_file(key_file)
        old = [read_key_file(part.strip()) for part in old_key_files.split(",") if part.strip()]
        ring = KeyRing.build(current, old)
    elif old_key_files.strip():
        raise EncryptionConfigError("Старые ключи заданы без текущего ключа")
    if mode == "required" and ring is None:
        raise EncryptionConfigError("NOTES_DATA_ENCRYPTION=required требует файл ключа NOTES_DATA_KEY_FILE")
    _state = State(mode, ring)
    return _state


def configure_from_settings(settings) -> State:
    return configure(settings.data_encryption, settings.data_key_file, settings.data_old_key_files)


def reset():
    """Возвращает режим по умолчанию (без шифрования). Нужен тестам."""
    global _state
    _state = State()


def writes_encrypted() -> bool:
    return _state.mode == "required" and _state.ring is not None


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def _cipher_for(key_id: str) -> AESGCM:
    ring = _state.ring
    if ring is None:
        raise KeyUnavailable("Данные зашифрованы, а ключ NOTES_DATA_KEY_FILE не настроен")
    cipher = ring.ciphers.get(key_id)
    if cipher is None:
        raise KeyUnavailable("Для этих данных нет ключа. Добавьте его в NOTES_DATA_OLD_KEY_FILES")
    return cipher


def _aad(label: str) -> bytes:
    return label.encode("utf-8")


def encrypt_text(value: str, label: str) -> str:
    """Шифрует всегда, если шифрование включено. В режиме off значение остаётся открытым."""
    if not writes_encrypted():
        return value
    ring = _state.ring
    nonce = os.urandom(NONCE_BYTES)
    sealed = ring.ciphers[ring.current_id].encrypt(nonce, value.encode("utf-8"), _aad(label))
    return PREFIX + ring.current_id + ":" + base64.urlsafe_b64encode(nonce + sealed).decode("ascii")


def key_id_in(value: str) -> str:
    parts = value.split(":", 3)
    if len(parts) != 4 or parts[0] != "enc" or parts[1] != "v1" or len(parts[2]) != KEY_ID_BYTES:
        raise DecryptionFailed("Зашифрованное значение имеет неверный формат")
    return parts[2]


def decrypt_text(value: str, label: str) -> str:
    """Значения без префикса enc: считаются старым открытым текстом."""
    if not is_encrypted(value):
        return value
    key_id = key_id_in(value)
    cipher = _cipher_for(key_id)
    try:
        blob = base64.urlsafe_b64decode(value.split(":", 3)[3])
        if len(blob) < NONCE_BYTES + 16:
            raise DecryptionFailed("Зашифрованное значение повреждено")
        return cipher.decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], _aad(label)).decode("utf-8")
    except (InvalidTag, binascii.Error, ValueError, UnicodeDecodeError):
        raise DecryptionFailed("Не удалось расшифровать значение: оно повреждено или относится к другому полю") from None


def encrypt_json(value, label: str):
    """JSON превращается в строку enc:v1:..., которую база хранит как JSON-строку."""
    if value is None or not writes_encrypted():
        return value
    return encrypt_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), label)


def decrypt_json(value, label: str):
    if not is_encrypted(value):
        return value
    try:
        return json.loads(decrypt_text(value, label))
    except ValueError:
        raise DecryptionFailed("Расшифрованное значение не является JSON") from None


def key_state(value) -> str:
    """plain, current или old (другой ключ) без расшифровки. Для статистики и CLI."""
    if not is_encrypted(value):
        return "plain"
    ring = _state.ring
    try:
        return "current" if ring is not None and key_id_in(value) == ring.current_id else "old"
    except DecryptionFailed:
        return "old"


def encrypt_bytes(data: bytes, aad: str) -> bytes:
    ring = _state.ring
    if ring is None:
        raise KeyUnavailable("Ключ NOTES_DATA_KEY_FILE не настроен")
    nonce = os.urandom(NONCE_BYTES)
    sealed = ring.ciphers[ring.current_id].encrypt(nonce, data, aad.encode("utf-8"))
    return AUDIO_MAGIC + ring.current_id.encode("ascii") + nonce + sealed


def is_encrypted_bytes(head: bytes) -> bool:
    return head.startswith(AUDIO_MAGIC)


def audio_key_id(blob: bytes) -> str:
    start = len(AUDIO_MAGIC)
    return blob[start:start + KEY_ID_BYTES].decode("ascii", "replace")


def decrypt_bytes(blob: bytes, aad: str) -> bytes:
    """Файл без заголовка считается старым открытым аудио и возвращается как есть."""
    if not is_encrypted_bytes(blob):
        return blob
    start = len(AUDIO_MAGIC)
    key_id = audio_key_id(blob)
    cipher = _cipher_for(key_id)
    nonce = blob[start + KEY_ID_BYTES:start + KEY_ID_BYTES + NONCE_BYTES]
    sealed = blob[start + KEY_ID_BYTES + NONCE_BYTES:]
    if len(nonce) != NONCE_BYTES or len(sealed) < 16:
        raise DecryptionFailed("Зашифрованный файл повреждён")
    try:
        return cipher.decrypt(nonce, sealed, aad.encode("utf-8"))
    except InvalidTag:
        raise DecryptionFailed("Не удалось расшифровать файл: он повреждён или относится к другой записи") from None
