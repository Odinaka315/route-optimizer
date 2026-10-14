"""Outgoing email, behind a tiny interface so any provider works.

``console`` (default) just logs the message, which is enough for development: the
verification/reset link appears in the server log. ``smtp`` works with any
provider that offers SMTP (Resend, SendGrid, Mailgun, Brevo, Gmail, ...).
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage as MimeMessage
from typing import Protocol

from .config import Settings

logger = logging.getLogger(__name__)


@dataclass
class EmailMessage:
    to: str
    subject: str
    body: str


class EmailSender(Protocol):
    def send(self, message: EmailMessage) -> None: ...


class ConsoleEmailSender:
    """Development: print the email in the server's terminal so you can click the link.

    Printed (not logged) because uvicorn's default logging hides INFO messages from
    application loggers, which would make the verification link invisible."""

    def send(self, message: EmailMessage) -> None:
        print(
            f"\n--- EMAIL (console backend) ---\nTo: {message.to}\nSubject: {message.subject}\n\n"
            f"{message.body}\n-------------------------------\n",
            flush=True,
        )


@dataclass
class MemoryEmailSender:
    """Collects messages instead of sending them. Used by the tests."""

    outbox: list[EmailMessage] = field(default_factory=list)

    def send(self, message: EmailMessage) -> None:
        self.outbox.append(message)


class SmtpEmailSender:
    def __init__(
        self,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        starttls: bool,
        sender: str,
    ):
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.starttls, self.sender = starttls, sender

    def send(self, message: EmailMessage) -> None:
        mime = MimeMessage()
        mime["From"] = self.sender
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime.set_content(message.body)
        with smtplib.SMTP(self.host, self.port, timeout=20) as smtp:
            if self.starttls:
                smtp.starttls(context=ssl.create_default_context())
            if self.username:
                smtp.login(self.username, self.password or "")
            smtp.send_message(mime)


def build_email_sender(settings: Settings) -> EmailSender:
    if settings.email_backend == "smtp":
        return SmtpEmailSender(
            settings.smtp_host or "",
            settings.smtp_port,
            settings.smtp_username,
            settings.smtp_password,
            settings.smtp_starttls,
            settings.email_from,
        )
    return ConsoleEmailSender()


def verification_email(to: str, link: str, valid_hours: int) -> EmailMessage:
    return EmailMessage(
        to=to,
        subject="Confirm your email address",
        body=(
            "Welcome! Please confirm your email address to finish setting up your account:\n\n"
            f"{link}\n\n"
            f"This link is valid for {valid_hours} hours. "
            "If you didn't create an account, you can ignore this message."
        ),
    )


def password_reset_email(to: str, link: str, valid_minutes: int) -> EmailMessage:
    return EmailMessage(
        to=to,
        subject="Reset your password",
        body=(
            "We received a request to reset your password. Choose a new one here:\n\n"
            f"{link}\n\n"
            f"This link is valid for {valid_minutes} minutes and can be used once. "
            "If you didn't ask for this, you can ignore this message; your password "
            "has not changed."
        ),
    )
