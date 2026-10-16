"""Outgoing email, behind a tiny interface so any provider works.

``brevo`` (default) sends transactional emails using Brevo's v3 REST API.
``console`` just logs the message, which is enough for local inspection: the
verification/reset link appears in the server log.
``smtp`` works with any provider that offers SMTP.
"""

from __future__ import annotations

import html
import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage as MimeMessage
from typing import Protocol

import requests

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


class BrevoError(RuntimeError):
    """Raised when sending an email via the Brevo API fails."""


BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"


class BrevoEmailSender:
    """Sends transactional emails via Brevo's v3 REST API."""

    def __init__(
        self,
        api_key: str,
        sender_email: str = "nwolisaodinaka5@gmail.com",
        sender_name: str = "routeWise",
        api_url: str = BREVO_API_URL,
        timeout: float = 15.0,
    ):
        self.api_key = api_key
        self.sender_email = sender_email
        self.sender_name = sender_name
        self.api_url = api_url
        self.timeout = timeout

    def _render_html(self, message: EmailMessage) -> str:
        escaped_body = html.escape(message.body)
        paragraphs = [p.replace("\n", "<br>") for p in escaped_body.split("\n\n") if p.strip()]
        content_html = "".join(
            f'<p style="margin: 0 0 16px 0; line-height: 1.6; color: #374151;">{p}</p>'
            for p in paragraphs
        )
        return (
            "<!DOCTYPE html>"
            f'<html><head><meta charset="utf-8"><title>{html.escape(message.subject)}</title></head>'
            '<body style="font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, Helvetica, Arial, sans-serif; '
            'background-color: #f9fafb; margin: 0; padding: 32px 16px;">'
            '<div style="max-width: 560px; margin: 0 auto; background: #ffffff; padding: 32px; '
            'border-radius: 8px; border: 1px solid #e5e7eb;">'
            f'<h2 style="margin-top: 0; color: #111827; font-size: 20px;">{html.escape(message.subject)}</h2>'
            f'{content_html}'
            '<hr style="border: none; border-top: 1px solid #e5e7eb; margin: 24px 0;">'
            f'<p style="font-size: 12px; color: #9ca3af; margin: 0;">Sent by {html.escape(self.sender_name)}</p>'
            '</div></body></html>'
        )

    def send(self, message: EmailMessage) -> None:
        if not self.api_key:
            raise BrevoError(
                "Brevo API key is not configured. Set BREVO_API_KEY (or BREVO_SECRET_KEY) in your .env file."
            )

        payload = {
            "sender": {
                "name": self.sender_name,
                "email": self.sender_email,
            },
            "to": [{"email": message.to}],
            "subject": message.subject,
            "textContent": message.body,
            "htmlContent": self._render_html(message),
        }
        headers = {
            "accept": "application/json",
            "api-key": self.api_key,
            "content-type": "application/json",
        }
        try:
            resp = requests.post(
                self.api_url,
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            logger.exception("Network failure while connecting to Brevo API")
            raise BrevoError(f"Network error while connecting to Brevo API: {exc}") from exc

        if not resp.ok:
            try:
                err_data = resp.json()
                error_msg = err_data.get("message") or err_data.get("error") or resp.text
            except Exception:
                error_msg = resp.text
            logger.error("Brevo API error: %s (status code %s)", error_msg, resp.status_code)
            raise BrevoError(f"Brevo API error ({resp.status_code}): {error_msg}")


def build_email_sender(settings: Settings) -> EmailSender:
    if settings.email_backend == "brevo":
        if not settings.brevo_api_key:
            logger.warning(
                "EMAIL_BACKEND=brevo but no BREVO_API_KEY (or BREVO_SECRET_KEY) is configured. "
                "Outgoing emails will fail until an API key is provided in .env."
            )
        return BrevoEmailSender(
            api_key=settings.brevo_api_key or "",
            sender_email=settings.sender_email,
            sender_name=settings.sender_name,
        )
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
