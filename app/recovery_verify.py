"""Verify restored audio in an offline container. No database, worker or sender."""

import hashlib
import json
import re
import sys

from app import crypto
from app.audio_storage import AudioStorage


def verify(rows, *, root="/audio"):
    storage = AudioStorage(root, read_only=True)
    for row in rows:
        if not isinstance(row, list) or len(row) != 2:
            raise ValueError("Invalid audio metadata")
        key, checksum = row
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f-]{36}\.audio", key):
            raise ValueError("Invalid audio metadata")
        if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ValueError("Invalid audio metadata")
        with storage.open_original(key) as original:
            if hashlib.file_digest(original, "sha256").hexdigest() != checksum:
                raise ValueError("Original checksum mismatch")
    return len(rows)


def main():
    try:
        crypto.configure("required", "/run/secrets/data_key")
        rows = json.load(sys.stdin)
        if not isinstance(rows, list):
            raise ValueError("Invalid audio metadata")
        count = verify(rows)
    except Exception:
        # Do not print decryption errors, metadata, keys or audio content.
        print("Restored audio verification failed", file=sys.stderr)
        return 1
    print(json.dumps({"audio_verified": count}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
