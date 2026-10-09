#!/usr/bin/env python3
"""Root-only registration switch. Does not start workers or load model credentials."""

import argparse
import os
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("open", "close"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("Run as root")
    path = Path("/opt/beresta/current/.env.production")
    if path.stat().st_mode & 0o077:
        raise SystemExit("Environment file must be private")
    lines = path.read_text().splitlines()
    flag = "true" if args.action == "open" else "false"
    updated = []
    found = False
    for line in lines:
        if line.split("=", 1)[0] == "NOTES_ALLOW_REGISTRATION":
            updated.append("NOTES_ALLOW_REGISTRATION=" + flag)
            found = True
        else:
            updated.append(line)
    if not found:
        updated.append("NOTES_ALLOW_REGISTRATION=" + flag)
    temporary = path.with_suffix(".registration-prepared")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as target:
            target.write("\n".join(updated) + "\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    subprocess.run(["/usr/local/sbin/beresta-core", "up", "-d", "--no-deps", "--no-build", "api"],
                   check=True, timeout=120)
    print("Registration " + ("opened" if args.action == "open" else "closed"))


if __name__ == "__main__":
    main()
