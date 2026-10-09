"""Ручная проверка STT шлюза. Запускает владелец внутри контейнера worker.

    python -m app.stt_probe --list
    python -m app.stt_probe --file /probe/voice.ogg --model ID

Скрипт не пишет в базу и не трогает счётчики живых вызовов приложения.
Запросы идут мимо лимитов приложения, но стоят денег. Запускать пару раз, не чаще.
Ключ читается только из NOTES_CLOUDRU_API_KEY_FILE и нигде не выводится.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

from app.config import ALLOWED_MODEL_BASE_URLS, Settings, read_secret_file
from app.providers import ProviderError
from app.speech import CloudRuSpeechProvider

MEDIA_TYPES = {".ogg": "audio/ogg", ".oga": "audio/ogg", ".wav": "audio/wav", ".webm": "audio/webm"}
AUDIO_HINTS = ("whisper", "stt", "speech", "audio", "asr")
MAX_LIST_BYTES = 256000


def _parser():
    parser = argparse.ArgumentParser(
        prog="python -m app.stt_probe",
        description="Ручная проверка STT шлюза. Нужен NOTES_ALLOW_LIVE_REQUESTS=true.",
        epilog="Запросы мимо лимитов приложения, но платные. Запускать не больше пары раз.",
    )
    parser.add_argument("--list", action="store_true", help="показать модели шлюза (GET /models)")
    parser.add_argument("--file", metavar="PATH", help="аудиофайл .ogg, .oga, .wav или .webm")
    parser.add_argument("--model", metavar="ID", help="идентификатор STT модели")
    return parser


def _load():
    """Возвращает (base_url, key) или печатает причину отказа и возвращает None."""
    try:
        settings = Settings.from_env()
    except (ValueError, OSError):
        print("Не удалось прочитать настройки окружения.", file=sys.stderr)
        return None
    if not settings.allow_live_requests:
        print("Живые запросы не разрешены. Владелец должен задать NOTES_ALLOW_LIVE_REQUESTS=true "
              "в .env.production. Без этого проверка не запускается.", file=sys.stderr)
        return 2
    base_url = settings.cloudru_base_url.rstrip("/")
    if base_url not in ALLOWED_MODEL_BASE_URLS:
        print("Адрес шлюза не входит в список разрешённых.", file=sys.stderr)
        return None
    key_file = os.environ.get("NOTES_CLOUDRU_API_KEY_FILE", "")
    if not key_file:
        print("Задайте NOTES_CLOUDRU_API_KEY_FILE. Ключ из переменной окружения не используется.",
              file=sys.stderr)
        return None
    try:
        return base_url, read_secret_file(key_file)
    except (ValueError, OSError):
        print("Файл ключа не читается или пуст.", file=sys.stderr)
        return None


def _list_models(base_url, key, transport):
    started = time.monotonic()
    try:
        with httpx.Client(timeout=httpx.Timeout(45, connect=10), follow_redirects=False,
                          trust_env=False, transport=transport) as client:
            with client.stream("GET", base_url + "/models",
                               headers={"Authorization": f"Bearer {key}"}) as response:
                status = response.status_code
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_LIST_BYTES:
                        print("Ответ шлюза слишком большой.", file=sys.stderr)
                        return 1
    except httpx.TimeoutException:
        print("Таймаут запроса к шлюзу.", file=sys.stderr)
        return 1
    except httpx.HTTPError:
        print("Ошибка соединения со шлюзом.", file=sys.stderr)
        return 1
    print(f"HTTP {status}, {time.monotonic() - started:.1f} с")
    if status != 200:
        shown = bytes(body).decode("utf-8", "replace").replace(key, "***")
        print("Тело ответа (обрезано):", shown[:300], file=sys.stderr)
        return 1
    try:
        ids = sorted(str(item["id"]) for item in json.loads(body)["data"])
    except (ValueError, KeyError, TypeError):
        print("Ответ шлюза не похож на список моделей.", file=sys.stderr)
        return 1
    for model_id in ids:
        mark = "  <- возможно аудио" if any(h in model_id.lower() for h in AUDIO_HINTS) else ""
        print(model_id + mark)
    print(f"Всего моделей {len(ids)}")
    return 0


def _transcribe(base_url, key, path, model, transport):
    media_type = MEDIA_TYPES.get(Path(path).suffix.lower())
    if media_type is None:
        print("Поддерживаются только .ogg, .oga, .wav и .webm.", file=sys.stderr)
        return 2
    try:
        provider = CloudRuSpeechProvider(key, model, base_url=base_url, transport=transport)
        source = open(path, "rb")
    except OSError:
        print("Файл не открывается.", file=sys.stderr)
        return 1
    started = time.monotonic()
    try:
        with source:
            text = provider.transcribe(source, media_type=media_type)
    except ProviderError as exc:
        print(f"Ошибка {exc.code}, {time.monotonic() - started:.1f} с", file=sys.stderr)
        return 1
    print(f"HTTP 200, {time.monotonic() - started:.1f} с")
    print(f"Длина расшифровки {len(text)} символов")
    print("Начало:", text[:200])
    return 0


def main(argv=None, transport=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if args.list == bool(args.file) or (args.file and not args.model) or (args.list and args.model):
        parser.print_usage(sys.stderr)
        print("Нужен либо --list, либо --file PATH вместе с --model ID.", file=sys.stderr)
        return 2
    loaded = _load()
    if loaded == 2:
        return 2
    if loaded is None:
        return 1
    base_url, key = loaded
    if args.list:
        return _list_models(base_url, key, transport)
    return _transcribe(base_url, key, args.file, args.model, transport)


if __name__ == "__main__":
    sys.exit(main())
