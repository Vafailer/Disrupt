"""Save an owner-supplied SMTP app password from stdin. No mail requests."""

import os
import sys
from pathlib import Path


def main():
    folder = Path("/opt/beresta/repository/secrets")
    if folder.is_symlink() or not folder.is_dir():
        raise SystemExit("Unsafe secrets directory")
    target = folder / "smtp_password.txt"
    if sys.argv[1:] == ["--check"]:
        print("SMTP password already exists; it will not be overwritten." if target.exists()
              else "SMTP password receiver is ready. No password has been saved.")
        return 0
    value = sys.stdin.buffer.read(4097).strip()
    if not 8 <= len(value) <= 4096 or any(char in value for char in (b"\0", b"\r", b"\n", b"\t")):
        raise SystemExit("Invalid SMTP app password; nothing saved")
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    except FileExistsError:
        raise SystemExit("SMTP password exists; it was not changed") from None
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value + b"\n")
            os.fchown(stream.fileno(), 1000, 1000)
            os.fchmod(stream.fileno(), 0o400)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        target.unlink(missing_ok=True)
        raise SystemExit("Could not save SMTP password; incomplete file removed") from None
    print("SMTP app password saved on the core VPS. No mail requests sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
