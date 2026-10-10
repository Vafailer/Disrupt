"""TOTP (RFC 6238) on the standard library: HMAC-SHA1, 6 digits, 30 second step."""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

PERIOD = 30
DIGITS = 6
WINDOW = 1


def new_secret() -> str:
    """160 random bits as unpadded base32, the format authenticator apps expect."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def decode_secret(secret: str) -> bytes:
    cleaned = secret.replace(" ", "").upper()
    return base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))


def hotp(key: bytes, counter: int, digits: int = DIGITS, algorithm=hashlib.sha1) -> str:
    digest = hmac.new(key, struct.pack(">Q", counter), algorithm).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10**digits).zfill(digits)


def step_at(timestamp: float, period: int = PERIOD) -> int:
    return int(timestamp // period)


def code_at(secret: str, timestamp: float, digits: int = DIGITS, algorithm=hashlib.sha1) -> str:
    return hotp(decode_secret(secret), step_at(timestamp), digits, algorithm)


def verify(secret: str, code: str, *, now: float | None = None, last_step: int | None = None) -> int | None:
    """Return the matched time step, or None.

    The window is one step either way. A step at or before `last_step` was already
    accepted for this account, so a replayed code is rejected.
    """
    if not isinstance(code, str) or len(code) != DIGITS or not code.isascii() or not code.isdigit():
        return None
    current = step_at(time.time() if now is None else now)
    key = decode_secret(secret)
    matched = None
    for step in range(current - WINDOW, current + WINDOW + 1):
        # No early exit: every candidate is compared in constant time.
        if hmac.compare_digest(hotp(key, step), code):
            matched = step
    if matched is None or (last_step is not None and matched <= last_step):
        return None
    return matched


def otpauth_uri(secret: str, account: str, issuer: str = "beresta") -> str:
    label = quote(f"{issuer}:{account}", safe="")
    return (f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer, safe='')}"
            f"&algorithm=SHA1&digits={DIGITS}&period={PERIOD}")
