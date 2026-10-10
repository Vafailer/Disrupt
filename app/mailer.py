"""Outgoing mail. Sending is off by default: DisabledMailer refuses every message."""

import smtplib
import ssl
from email.message import EmailMessage
from typing import Protocol

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


def build_mailer(settings: Settings) -> Mailer:
    if not settings.mail_enabled:
        return DisabledMailer()
    password = read_secret_file(settings.smtp_password_file, maximum=4096) if settings.smtp_password_file else ""
    return SmtpMailer(settings.smtp_host, settings.smtp_port, settings.smtp_user, password, settings.mail_from)
