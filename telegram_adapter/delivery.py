"""One authorized send, followed by durable retries of its result only."""

import asyncio
import json
import os
import re
import tempfile
import time
from uuid import UUID

from .clients import RemoteFailure


def validate_result(delivery_id, body):
    try:
        UUID(delivery_id)
        if not isinstance(body, dict) or set(body) != {
            "lease_token", "generation", "status", "telegram_message_id", "error_code", "retry_after_seconds",
        }:
            raise ValueError()
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", body["lease_token"]):
            raise ValueError()
        if type(body["generation"]) is not int or body["generation"] < 1:
            raise ValueError()
        status, message = body["status"], body["telegram_message_id"]
        error, retry = body["error_code"], body["retry_after_seconds"]
        if status == "sent":
            if type(message) is not int or message <= 0 or error is not None or retry is not None:
                raise ValueError()
        elif status in {"blocked", "retryable", "unknown"}:
            if message is not None or not isinstance(error, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error):
                raise ValueError()
            if status == "retryable":
                if type(retry) is not int or not 0 <= retry <= 86400:
                    raise ValueError()
            elif retry is not None:
                raise ValueError()
        else:
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError("Invalid delivery journal; do not discard it") from None


class DeliveryJournal:
    """Private pending receipt, with no note text or chat ID. Lease tokens are secrets."""

    def __init__(self, path, bot_id):
        self.path, self.bot_id = path, bot_id
        self.pending = None
        if path.exists():
            if path.is_symlink() or path.stat().st_mode & 0o077:
                raise ValueError("Delivery journal must be a private file")
            data = json.loads(path.read_text())
            if data.get("bot_id") != bot_id:
                raise ValueError("Delivery journal belongs to another bot")
            validate_result(data.get("delivery_id"), data.get("body"))
            self.pending = data

    def sync_directory(self):
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def save(self, delivery_id, body):
        validate_result(delivery_id, body)
        if self.pending is not None:
            raise ValueError("Record the previous delivery result first")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"bot_id": self.bot_id, "delivery_id": delivery_id, "body": dict(body)}
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".delivery-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            self.sync_directory()
            self.pending = data
        finally:
            from pathlib import Path

            Path(name).unlink(missing_ok=True)

    def clear(self):
        self.path.unlink()
        self.sync_directory()
        self.pending = None


class DeliverySender:
    def __init__(self, settings, core, telegram, journal):
        self.settings, self.core, self.telegram, self.journal = settings, core, telegram, journal
        self.cooldown_until = 0

    def validate_claim(self, item):
        try:
            UUID(item["delivery_id"])
            if type(item["generation"]) is not int or item["generation"] < 1:
                raise ValueError()
            if not re.fullmatch(r"[A-Za-z0-9_-]{43}", item["lease_token"]):
                raise ValueError()
            if type(item["chat_id"]) is not int or not 0 < item["chat_id"] < 2**63:
                raise ValueError()
            if not isinstance(item["text"], str) or not 1 <= len(item["text"]) <= 1000 or "\x00" in item["text"]:
                raise ValueError()
            callback = item["callback_token"]
            if callback is not None and not re.fullmatch(r"[A-Za-z0-9_-]{43}", callback):
                raise ValueError()
            self.core.validate_saved({"capture_id": item["delivery_id"], "status": "saved", "note_url": item["note_url"]})
        except (ValueError, KeyError, TypeError, AttributeError):
            raise RemoteFailure("core_invalid_delivery", fatal=True) from None

    async def flush_result(self):
        pending = self.journal.pending
        if pending is None:
            return
        response = await self.core.post("/deliveries/" + pending["delivery_id"] + "/result", pending["body"])
        if response.get("status") != "recorded":
            raise RemoteFailure("core_invalid_response")
        self.journal.clear()
        if pending["body"]["error_code"] == "telegram_401":
            raise RemoteFailure("telegram_401", fatal=True)

    async def run_once(self):
        # A restart must report the same receipt, never resend its Telegram message.
        await self.flush_result()
        if time.monotonic() < self.cooldown_until:
            return False
        response = await self.core.post("/deliveries/claim", {"bot_id": self.settings.bot_id, "limit": 1})
        items = response.get("items")
        if not isinstance(items, list) or len(items) > 1:
            raise RemoteFailure("core_invalid_delivery", fatal=True)
        if not items:
            return False
        item = items[0]
        self.validate_claim(item)
        lease = {k: item[k] for k in ("lease_token", "generation")}
        try:
            async with asyncio.timeout(10):
                permission = await self.core.post("/deliveries/" + item["delivery_id"] + "/authorize", lease)
        except TimeoutError:
            raise RemoteFailure("core_authorization_unknown") from None
        if type(permission.get("send")) is not bool:
            raise RemoteFailure("core_invalid_response")
        if not permission["send"]:
            return True
        result = await self.telegram.send_reminder(item)
        self.journal.save(item["delivery_id"], {**lease, **result})
        if result["status"] == "retryable":
            self.cooldown_until = time.monotonic() + result["retry_after_seconds"]
        await self.flush_result()
        return True
