"""Owner-run administrator enrollment. Credentials arrive on stdin, never in argv.

Run inside the API container with /enroll bound to a private temporary directory.
The owner downloads the authenticator bundle and removes this temporary copy.
"""

import argparse
import getpass
import json
import os
import re
import sys
from pathlib import Path

from app import admin_cli, totp

EXPORT_DIRECTORY = Path("/enroll")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("create", "confirm"))
    parser.add_argument("username")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9_.-]{3,64}", args.username):
        parser.error("Invalid administrator username")
    raw = sys.stdin.buffer.read(4097)
    if len(raw) > 4096:
        parser.error("Input too large")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        parser.error("Invalid input")
    if not isinstance(payload, dict):
        parser.error("Invalid input")
    if args.command == "confirm":
        code = payload.get("code", "")
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
            parser.error("Enter the six-digit authenticator code")
        return admin_cli.main(["confirm", args.username, code])

    password = payload.get("password", "")
    if not isinstance(password, str) or not 12 <= len(password) <= 128 or "\0" in password:
        parser.error("Password must contain 12 to 128 characters")
    # Reserve before committing the account; never overwrite a previous enrollment.
    target = EXPORT_DIRECTORY / (args.username + ".txt")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    exported = False
    original_getpass, original_show = getpass.getpass, admin_cli.show_secret

    def export(admin, secret):
        nonlocal exported
        bundle = (
            "Beresta administrator: " + admin.username + "\n\n"
            "Authenticator setup URI:\n" + totp.otpauth_uri(secret, admin.username) + "\n\n"
            "Manual setup key:\n" + secret + "\n\n"
            "Keep this file in your password manager, separately from backups.\n"
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(bundle)
            stream.flush()
            os.fsync(stream.fileno())
        exported = True
        print("Administrator created. Authenticator setup saved privately; confirmation is required.")

    getpass.getpass = lambda prompt="": password
    admin_cli.show_secret = export
    try:
        return admin_cli.main(["create", args.username])
    finally:
        getpass.getpass, admin_cli.show_secret = original_getpass, original_show
        if not exported:
            # A zero-length reserved file contains no secret. Do not remove a written bundle.
            try:
                os.close(descriptor)
            except OSError:
                pass
            if target.is_file() and target.stat().st_size == 0:
                target.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
