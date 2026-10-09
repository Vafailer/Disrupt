#!/usr/bin/env python3
"""Owner-only hidden input, no shell argument/history and no network calls."""
import argparse
import fcntl
import getpass
import os
import re
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("name", choices=["telegram_token", "cloudru_api_key"])
args = parser.parse_args()
if os.geteuid() != 0:
    raise SystemExit("Use the server operator account (root)")
if not os.isatty(0):
    raise SystemExit("Use an interactive SSH terminal; input must not be echoed")
value = getpass.getpass("Secret (hidden): ")
if not value or any(c.isspace() for c in value) or "\x00" in value:
    raise SystemExit("Invalid secret")
if args.name == "telegram_token" and not re.fullmatch(r"[1-9][0-9]*:[A-Za-z0-9_-]{20,}", value):
    raise SystemExit("Invalid Telegram token")
directory = Path("secrets")
directory.mkdir(mode=0o700, exist_ok=True)
if directory.is_symlink():
    raise SystemExit("Invalid secret directory")
directory.chmod(0o700)
path = directory / (args.name + ".txt")
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o400)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
if os.fstat(fd).st_size:
    os.close(fd)
    raise SystemExit("An existing secret will not be overwritten")
os.fchmod(fd, 0o400)
os.fchown(fd, 1000, 1000)
with os.fdopen(fd, "w") as target:
    target.write(value)
    target.flush()
    os.fsync(target.fileno())
print("Secret saved. No service started.")
