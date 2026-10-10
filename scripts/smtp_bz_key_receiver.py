"""Read a key from SSH stdin. Never accept it in arguments or print its value."""

import os
import stat
import sys
import tempfile
from pathlib import Path

DIRECTORY = Path("/opt/beresta/repository/secrets")
SYNTHETIC_KEY = b"synthetic-smtp-bz-key-not-a-real-credential"


def save(directory, value):
    value = value.strip()
    if not value or len(value) > 8192 or b"\0" in value or any(c in value for c in b"\r\n\t "):
        raise SystemExit("Invalid API key; nothing saved")
    if directory.is_symlink() or not directory.is_dir():
        raise SystemExit("Unsafe or missing secrets directory")
    target = directory / "smtp_bz_api_key.txt"
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit("SMTP key file already exists; it was not changed") from None
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value + b"\n")
            stream.flush()
            os.fchown(stream.fileno(), 1000, 1000)
            os.fchmod(stream.fileno(), 0o400)
            os.fsync(stream.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise SystemExit("Could not save key; incomplete file removed") from None
    return target


def self_test(value):
    if value.strip() != SYNTHETIC_KEY:
        raise SystemExit("Self-test accepts only the built-in synthetic value")
    with tempfile.TemporaryDirectory(prefix="beresta-smtp-receiver-test-") as location:
        directory = Path(location)
        target = save(directory, value)
        assert target.read_bytes() == SYNTHETIC_KEY + b"\n"
        assert stat.S_IMODE(target.stat().st_mode) == 0o400
        assert (target.stat().st_uid, target.stat().st_gid) == (1000, 1000)
        try:
            save(directory, b"another-synthetic-value")
        except SystemExit:
            pass
        else:
            raise AssertionError("Existing file was overwritten")
        assert target.read_bytes() == SYNTHETIC_KEY + b"\n"
        for invalid in (b"", b"with space", b"with\nnewline", b"with\0null", b"x" * 8193):
            try:
                save(directory, invalid)
            except SystemExit as error:
                assert str(error) == "Invalid API key; nothing saved"
            else:
                raise AssertionError("Invalid value was accepted")
    print("Receiver self-test passed. Production secret untouched. No requests sent.")


def main():
    if sys.argv[1:] == ["--check"]:
        if DIRECTORY.is_symlink() or not DIRECTORY.is_dir():
            raise SystemExit("Unsafe or missing secrets directory")
        print("Receiver and secrets directory are ready.")
        return
    if sys.argv[1:] not in ([], ["--self-test"]):
        raise SystemExit("Unsupported receiver mode")
    value = sys.stdin.buffer.read(8195)
    if len(value) >= 8195:
        raise SystemExit("API key input too large; nothing saved")
    if sys.argv[1:] == ["--self-test"]:
        self_test(value)
    else:
        save(DIRECTORY, value)
        print("SMTP.BZ API key saved on the core VPS. No requests sent.")


if __name__ == "__main__":
    main()
