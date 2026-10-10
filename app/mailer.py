"""Outgoing mail. Sending is off by default: DisabledMailer refuses every message."""

import html
import smtplib
import ssl
from email.message import EmailMessage
from typing import Protocol

import httpx

from app.config import Settings, read_secret_file

MAIL_DISABLED_TEXT = "Почта пока не подключена"


class MailError(Exception):
    """A message could not be sent. The text never includes the address or the body."""


class MailDisabled(MailError):
    pass


class Mailer(Protocol):
    enabled: bool

    def send(self, to: str, subject: str, body: str) -> None: ...


class DisabledMailer:
    enabled = False

    def send(self, to: str, subject: str, body: str) -> None:
        raise MailDisabled(MAIL_DISABLED_TEXT)


class SmtpMailer:
    """Plain SMTP with STARTTLS from the standard library. One connection per message."""

    enabled = True

    def __init__(self, host, port, user, password, sender, *, timeout=15):
        self.host, self.port, self.user = host, port, user
        self.password, self.sender, self.timeout = password, sender, timeout

    def send(self, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = self.sender, to, subject
        message.set_content(body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                if self.user:
                    smtp.login(self.user, self.password)
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError):
            # ssl.SSLError is an OSError. Never keep the server's reply: it can echo the address.
            raise MailError("Не удалось отправить письмо") from None


SMTP_BZ_SEND_URL = "https://api.smtp.bz/v1/smtp/send"


class SmtpBzMailer:
    """SMTP.BZ HTTPS API. The VPS provider blocks SMTP ports, so mail goes over port 443.

    One POST per message, multipart/form-data, API key in the Authorization header.
    """

    enabled = True

    def __init__(self, api_key, sender, *, sender_name="beresta", timeout=15, transport=None):
        self.api_key, self.sender, self.sender_name = api_key, sender, sender_name
        self.timeout, self.transport = timeout, transport

    def send(self, to: str, subject: str, body: str) -> None:
        fields = {
            "from": self.sender, "name": self.sender_name, "to": to, "subject": subject,
            "html": "<p>" + html.escape(body).replace("\n", "<br>") + "</p>", "text": body,
        }
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport, follow_redirects=False) as client:
                response = client.post(
                    SMTP_BZ_SEND_URL,
                    headers={"Authorization": self.api_key},
                    files=[(name, (None, value)) for name, value in fields.items()],
                )
        except httpx.HTTPError:
            raise MailError("Не удалось отправить письмо") from None
        if not 200 <= response.status_code < 300:
            # The reply body is not kept: it can echo the address.
            raise MailError("Не удалось отправить письмо")


def build_mailer(settings: Settings) -> Mailer:
    if not settings.mail_enabled:
        return DisabledMailer()
    if settings.mail_transport == "smtp_bz":
        key = read_secret_file(settings.smtp_bz_api_key_file, maximum=4096)
        return SmtpBzMailer(key, settings.mail_from)
    password = read_secret_file(settings.smtp_password_file, maximum=4096) if settings.smtp_password_file else ""
    return SmtpMailer(settings.smtp_host, settings.smtp_port, settings.smtp_user, password, settings.mail_from)
