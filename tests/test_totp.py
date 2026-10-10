"""RFC 4226 and RFC 6238 vectors, plus the window and replay rules."""

import base64
import hashlib

import pytest

from app import totp

SHA1_KEY = b"12345678901234567890"
SHA1_SECRET = base64.b32encode(SHA1_KEY).decode()
SHA256_SECRET = base64.b32encode(b"12345678901234567890123456789012").decode()
SHA512_SECRET = base64.b32encode(b"1234567890123456789012345678901234567890123456789012345678901234").decode()


def test_rfc4226_hotp_vectors():
    expected = ["755224", "287082", "359152", "969429", "338314", "254676", "287922", "162583", "399871", "520489"]
    assert [totp.hotp(SHA1_KEY, counter) for counter in range(10)] == expected


@pytest.mark.parametrize("moment,code", [
    (59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"),
    (1234567890, "89005924"), (2000000000, "69279037"), (20000000000, "65353130"),
])
def test_rfc6238_sha1_vectors(moment, code):
    assert totp.code_at(SHA1_SECRET, moment, digits=8) == code
    assert totp.code_at(SHA1_SECRET, moment) == code[-6:]


def test_rfc6238_other_digest_vectors_for_the_shared_algorithm():
    assert totp.code_at(SHA256_SECRET, 59, digits=8, algorithm=hashlib.sha256) == "46119246"
    assert totp.code_at(SHA512_SECRET, 59, digits=8, algorithm=hashlib.sha512) == "90693936"


def test_window_is_one_step_either_way():
    now = 1111111111
    current = totp.step_at(now)
    for offset, accepted in [(-2, False), (-1, True), (0, True), (1, True), (2, False)]:
        code = totp.hotp(SHA1_KEY, current + offset)
        expected = current + offset if accepted else None
        assert totp.verify(SHA1_SECRET, code, now=now) == expected


def test_same_step_cannot_be_reused_and_older_steps_stay_rejected():
    now = 1111111111
    step = totp.step_at(now)
    code = totp.hotp(SHA1_KEY, step)
    assert totp.verify(SHA1_SECRET, code, now=now) == step
    assert totp.verify(SHA1_SECRET, code, now=now, last_step=step) is None
    older = totp.hotp(SHA1_KEY, step - 1)
    assert totp.verify(SHA1_SECRET, older, now=now, last_step=step) is None
    newer = totp.hotp(SHA1_KEY, step + 1)
    assert totp.verify(SHA1_SECRET, newer, now=now, last_step=step) == step + 1


@pytest.mark.parametrize("code", ["", "12345", "1234567", "12345a", " 12345", "١٢٣٤٥٦", None, 123456])
def test_malformed_codes_are_rejected(code):
    assert totp.verify(SHA1_SECRET, code, now=59) is None


def test_new_secret_and_uri():
    secret = totp.new_secret()
    assert len(secret) == 32 and secret == secret.upper() and "=" not in secret
    assert len(totp.decode_secret(secret)) == 20
    assert totp.new_secret() != secret
    uri = totp.otpauth_uri(secret, "owner name")
    assert uri.startswith("otpauth://totp/beresta%3Aowner%20name?secret=" + secret)
    assert "issuer=beresta" in uri and "digits=6" in uri and "period=30" in uri
