"""Owner-operated secret setup; no network requests, no model keys."""

import getpass
import os
import re
import secrets
from pathlib import Path


def write_secret(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(value + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def main():
    directory = Path("secrets")
    directory.mkdir(mode=0o700, exist_ok=True)
    bot = directory / "telegram_token.txt"
    service = directory / "telegram_service_token.txt"
    if bot.exists() or service.exists():
        print("Secret files already exist; nothing changed. Rotate credentials explicitly if needed.")
        return 1
    token = getpass.getpass("Telegram test bot token (hidden): ").strip()
    if not re.fullmatch(r"[1-9][0-9]*:[A-Za-z0-9_-]{20,}", token):
        print("Invalid token format; no files written.")
        return 1
    write_secret(bot, token)
    write_secret(service, secrets.token_urlsafe(32))
    print("Saved two files under secrets/ with mode 0600. No network calls made.")
    print("The core API must use the same service token. Never send it in chat or commit it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
