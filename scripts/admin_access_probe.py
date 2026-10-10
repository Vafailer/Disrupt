"""Owner-authorized private admin acceptance. Credentials stay in memory, never in output."""

import argparse
import base64
import hashlib
import hmac
import io
import json
import re
import struct
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from http.cookiejar import CookieJar
from pathlib import Path
from zoneinfo import ZoneInfo


def code_at(secret, timestamp):
    key = base64.b32decode(secret)
    digest = hmac.new(key, struct.pack(">Q", int(timestamp // 30)), hashlib.sha1).digest()
    offset = digest[-1] & 15
    return f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7fffffff) % 1000000:06d}"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9_.-]{3,64}", args.username):
        parser.error("Invalid administrator name")
    # Never print this text, its parsed fields, HTTP bodies or Set-Cookie values.
    bundle = Path(args.bundle).read_text()
    password = re.search(r"^Password:\n([^\n]+)", bundle, re.M).group(1)
    secret = re.search(r"^Manual setup key:\n([A-Z2-7]+)", bundle, re.M).group(1)
    if args.confirm:
        result = subprocess.run([
            "beresta-core", "run", "--rm", "-T", "--no-deps", "-e", "PYTHONPATH=/app",
            "-v", "/opt/beresta/repository/.local/admin-enroll-receiver.py:/enroll-receiver.py:ro",
            "-v", "/opt/beresta/repository/.local/admin-enroll:/enroll", "api", "python",
            "/enroll-receiver.py", "confirm", args.username,
        ], input=json.dumps({"code": code_at(secret, time.time())}).encode(),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=45, check=False)
        if result.returncode:
            raise RuntimeError("Admin confirmation failed")
        print("Administrator confirmed after offline credential copy was verified.", flush=True)
        # Confirmation consumes a TOTP step. Use a genuinely new step for the login acceptance.
        delay = 31 - (time.time() % 30)
        print("Waiting for the next authenticator step for private login acceptance.", flush=True)
        time.sleep(delay)

    origin = "http://10.77.0.1:18000"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                        urllib.request.HTTPCookieProcessor(CookieJar()))

    def request(path, data=None, csrf=None):
        headers = {"Origin": origin}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if csrf:
            headers["X-CSRF-Token"] = csrf
        req = urllib.request.Request(origin + path, headers=headers,
                                     data=json.dumps(data).encode() if data is not None else None)
        try:
            with opener.open(req, timeout=15) as response:
                return response.status, response.read(4 * 1024 * 1024), response.headers
        except urllib.error.HTTPError as error:
            return error.code, b"", error.headers

    status, body, _ = request("/admin-api/v1/login", {
        "username": args.username, "password": password, "totp": code_at(secret, time.time()),
    })
    if status != 200:
        raise RuntimeError("Private admin login failed")
    session = json.loads(body)
    csrf = session["csrf_token"]
    assert session["username"] == args.username
    assert request("/admin-api/v1/me")[0] == 200
    date = datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()
    query = f"?from={date}&to={date}"
    assert request("/admin-api/v1/summary" + query)[0] == 200
    status, csv, headers = request("/admin-api/v1/export" + query)
    assert status == 200 and "text/csv" in headers.get("Content-Type", "") and csv
    status, zipped, _ = request("/admin-api/v1/export.zip" + query)
    assert status == 200
    with zipfile.ZipFile(io.BytesIO(zipped)) as archive:
        assert "README.txt" in archive.namelist() and "daily.csv" in archive.namelist()
    assert request("/admin-api/v1/audit")[0] == 200
    assert request("/admin-api/v1/logout", {}, csrf)[0] == 204
    assert request("/admin-api/v1/me")[0] == 401
    print(json.dumps({"admin": args.username, "login": True, "summary": True, "csv": True,
                      "metrics_zip": True, "audit": True, "logout": True, "credentials_logged": False}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # Suppress tracebacks and raw replies, even if a provider/library echoes a credential.
        print(json.dumps({"ok": False, "error": "private_admin_acceptance_failed", "credentials_logged": False}))
        raise SystemExit(1) from None
