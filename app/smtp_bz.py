"""SMTP.BZ HTTPS transport. Construction sends nothing; errors never echo mail or keys."""

import html
import json
import re

import httpx

ENDPOINT = "https://api.smtp.bz/v1/smtp/send"
ADDRESS = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\Z")


class SmtpBzError(Exception):
    def __init__(self, code, *, http_status=None):
        self.code, self.http_status = code, http_status
        super().__init__(code)


def validate_address(value):
    if not isinstance(value, str) or len(value) > 254 or not ADDRESS.fullmatch(value):
        raise SmtpBzError("mail_invalid_address")
    return value


class SmtpBzMailer:
    enabled = True

    def __init__(self, api_key, sender, *, sender_name="Beresta", transport=None):
        if not api_key or len(api_key) > 8192 or any(c.isspace() for c in api_key):
            raise SmtpBzError("mail_invalid_configuration")
        self._api_key = api_key
        self.sender = validate_address(sender)
        self.sender_name = sender_name
        self._transport = transport

    def send(self, to, subject, body):
        self.send_with_receipt(to, subject, body)

    def send_with_receipt(self, to, subject, body):
        validate_address(to)
        if not isinstance(subject, str) or not subject.strip() or len(subject) > 200 or any(c in subject for c in "\r\n\0"):
            raise SmtpBzError("mail_invalid_subject")
        if not isinstance(body, str) or not body.strip() or len(body) > 24000 or "\0" in body:
            raise SmtpBzError("mail_invalid_body")
        fields = {
            "from": self.sender, "name": self.sender_name, "to": to, "subject": subject,
            "text": body, "html": "<pre>" + html.escape(body) + "</pre>",
        }
        try:
            with httpx.Client(
                timeout=httpx.Timeout(20, connect=10), trust_env=False, follow_redirects=False,
                transport=self._transport,
            ) as client:
                with client.stream(
                    "POST", ENDPOINT, headers={"Authorization": self._api_key, "Accept": "application/json"},
                    files={name: (None, value) for name, value in fields.items()},
                ) as response:
                    if response.status_code != 200:
                        code = "mail_auth" if response.status_code in {401, 403} else "mail_http_error"
                        raise SmtpBzError(code, http_status=response.status_code)
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 65536:
                            raise SmtpBzError("mail_response_too_large")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise SmtpBzError("mail_invalid_response")
            if result.get("error") not in (None, False, 0, "") or result.get("success") is False:
                raise SmtpBzError("mail_rejected")
            if type(result.get("code")) is int and result["code"] >= 400:
                raise SmtpBzError("mail_rejected")
            values = result.get("data") if isinstance(result.get("data"), dict) else result
            receipt = values.get("id", values.get("message_id", values.get("messageid")))
            # Return only an opaque identifier, never arbitrary server strings.
            receipt = str(receipt) if type(receipt) in {str, int} else ""
            return receipt if re.fullmatch(r"[A-Za-z0-9_-]{1,100}", receipt) else None
        except httpx.TimeoutException:
            raise SmtpBzError("mail_timeout_unknown") from None
        except httpx.HTTPError:
            raise SmtpBzError("mail_connection_unknown") from None
        except (ValueError, TypeError):
            raise SmtpBzError("mail_invalid_response") from None
