"""Recovery verifies the original bytes after authenticated decryption."""

import hashlib
import uuid

import pytest

from app import crypto
from app.audio_storage import audio_aad
from app.recovery_verify import verify


def test_plain_and_encrypted_originals_share_digest(tmp_path):
    rows = []
    for encrypted in (False, True):
        key = str(uuid.uuid4()) + ".audio"
        original = b"synthetic recovery original"
        stored = crypto.encrypt_bytes(original, audio_aad(key)) if encrypted else original
        (tmp_path / key).write_bytes(stored)
        rows.append([key, hashlib.sha256(original).hexdigest()])
    assert verify(rows, root=tmp_path) == 2


def test_modified_or_missing_original_is_rejected(tmp_path):
    key = str(uuid.uuid4()) + ".audio"
    checksum = hashlib.sha256(b"original").hexdigest()
    (tmp_path / key).write_bytes(b"changed")
    with pytest.raises(ValueError):
        verify([[key, checksum]], root=tmp_path)
    (tmp_path / key).unlink()
    with pytest.raises(Exception):
        verify([[key, checksum]], root=tmp_path)


def test_wrong_key_and_tampering_are_rejected(tmp_path):
    key = str(uuid.uuid4()) + ".audio"
    original = b"synthetic sealed original"
    sealed = crypto.encrypt_bytes(original, audio_aad(key))
    (tmp_path / key).write_bytes(sealed[:-1] + bytes([sealed[-1] ^ 1]))
    with pytest.raises(Exception):
        verify([[key, hashlib.sha256(original).hexdigest()]], root=tmp_path)
    (tmp_path / key).write_bytes(sealed)
    other_key = tmp_path / "synthetic-other.key"
    other_key.write_text(crypto.generate_key())
    crypto.configure("required", str(other_key))
    with pytest.raises(Exception):
        verify([[key, hashlib.sha256(original).hexdigest()]], root=tmp_path)
