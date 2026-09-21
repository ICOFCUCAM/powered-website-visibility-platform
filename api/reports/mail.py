"""Sending the report.

A protocol and two implementations, because the interesting part of email in
this codebase is not SMTP — it is refusing to record a send that did not
happen. `reports.status = 'sent'` is a claim to the customer, and the only
thing that may set it is a delivery that actually returned.

`smtplib` is synchronous and is run on a worker thread rather than wrapped in
an async SMTP dependency. One message every seven days per website does not
justify another library on the critical path.
"""

from __future__ import annotations

import asyncio
import logging
import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

logger = logging.getLogger("visibility_hub.reports")


@dataclass(frozen=True, slots=True)
class Message:
    to: list[str]
    subject: str
    text: str
    html: str


class Mailer(Protocol):
    async def send(self, message: Message) -> None: ...


@dataclass(frozen=True, slots=True)
class SmtpSettings:
    host: str
    port: int
    username: str | None
    password: str | None
    sender: str
    use_tls: bool = True

    @classmethod
    def from_env(cls) -> SmtpSettings | None:
        """None when email is not configured, which is a supported state.

        The rest of the product works without a mail server; only sending
        does not, and the send endpoint says so rather than failing obscurely.
        """
        host = os.environ.get("SMTP_HOST")
        sender = os.environ.get("REPORT_FROM_EMAIL")
        if not host or not sender:
            return None
        return cls(
            host=host,
            port=int(os.environ.get("SMTP_PORT", "587")),
            username=os.environ.get("SMTP_USERNAME") or None,
            password=os.environ.get("SMTP_PASSWORD") or None,
            sender=sender,
            use_tls=os.environ.get("SMTP_USE_TLS", "1") != "0",
        )


def build(message: Message, sender: str) -> EmailMessage:
    email = EmailMessage()
    email["From"] = sender
    email["To"] = ", ".join(message.to)
    email["Subject"] = message.subject
    # Text first, HTML as the alternative: a client that shows the first part
    # it understands should show the readable one.
    email.set_content(message.text)
    email.add_alternative(message.html, subtype="html")
    return email


class SmtpMailer:
    def __init__(self, settings: SmtpSettings) -> None:
        self._settings = settings

    async def send(self, message: Message) -> None:
        email = build(message, self._settings.sender)
        await asyncio.to_thread(self._deliver, email)

    def _deliver(self, email: EmailMessage) -> None:
        settings = self._settings
        with smtplib.SMTP(settings.host, settings.port, timeout=30) as server:
            if settings.use_tls:
                server.starttls()
            if settings.username and settings.password:
                server.login(settings.username, settings.password)
            server.send_message(email)


class RecordingMailer:
    """Keeps messages in memory. For tests and local development."""

    def __init__(self) -> None:
        self.sent: list[Message] = []

    async def send(self, message: Message) -> None:
        self.sent.append(message)
        logger.info(
            "report email recorded, not delivered: recipients=%d", len(message.to)
        )
