"""One explicitly authorized operator email. No database, model, retry or raw secret output."""

import argparse
import json
import os
from pathlib import Path

from app.config import read_secret_file
from app.smtp_bz import SmtpBzError, SmtpBzMailer, validate_address

SUBJECT = "Beresta: проверка доставки"
BODY = "Это тестовое письмо Beresta. Подтверждать регистрацию не требуется."


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", required=True)
    parser.add_argument("--allow-send", action="store_true")
    args = parser.parse_args()
    if not args.allow_send:
        raise SystemExit("Explicit --allow-send is required")
    validate_address(args.to)
    mailer = SmtpBzMailer(
        read_secret_file("/run/secrets/smtp_bz_api_key", maximum=8192), "no-reply@berestaapp.ru",
    )
    directory = Path("/audit")
    target = directory / "first-smtp-bz-delivery.json"
    # This receipt exists before dispatch. Re-running cannot send a second email after a lost reply.
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit("Attempt already recorded. No email sent.") from None
    with os.fdopen(fd, "w") as stream:
        json.dump({"status": "unknown"}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        receipt = mailer.send_with_receipt(args.to, SUBJECT, BODY)
        result = {"status": "accepted_http_200", "message_id": receipt, "delivery_confirmed": False}
    except SmtpBzError as error:
        result = {"status": "failed_or_unknown", "code": error.code, "http_status": error.http_status}
    target.write_text(json.dumps(result), encoding="utf-8")
    print(json.dumps(result))
    return 0 if result["status"] == "accepted_http_200" else 1


if __name__ == "__main__":
    raise SystemExit(main())
