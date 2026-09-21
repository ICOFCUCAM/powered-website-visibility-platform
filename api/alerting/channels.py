"""Where an alert goes.

A protocol and four implementations, the same shape as the mailer and the
model provider. The one that matters most is `NullNotifier`: an installation
with no destination configured must SAY so rather than dropping alerts into a
log nobody reads and calling it monitoring.

Deliberately dumb. A notifier renders and posts; it does not decide whether to
alert, does not deduplicate, does not rate limit. All of that is in
`operator.py`, where it can be tested without a network.

**Nothing tenant-identifying goes to a webhook.** An operator alert says "12
websites failed sync_search_console", never which twelve. A Slack channel is
not a place for customer domains, and an operator who needs the list can query
`scheduled_runs`, where it is already scoped and audited.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

logger = logging.getLogger("visibility_hub.alerting")

TIMEOUT = httpx.Timeout(10.0)


@dataclass(frozen=True, slots=True)
class Alert:
    severity: str
    title: str
    #: Short, human, and already aggregated. "12 of 40 websites failed" —
    #: never a list of domains.
    body: str
    kind: str = "unspecified"
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def emoji(self) -> str:
        return {"critical": "🔴", "warning": "🟠", "recovered": "🟢"}.get(
            self.severity, "•"
        )


class Notifier(Protocol):
    async def send(self, alert: Alert) -> None: ...

    @property
    def configured(self) -> bool: ...


class NullNotifier:
    """No destination configured.

    Logs at WARNING, because an alert nobody receives is still something an
    operator reading the log should trip over. The caller asks `configured`
    before it decides that alerting is working.
    """

    configured = False

    async def send(self, alert: Alert) -> None:
        logger.warning(
            "ALERT NOT DELIVERED (no destination configured): [%s] %s — %s",
            alert.severity, alert.title, alert.body,
        )


class RecordingNotifier:
    """Keeps alerts in memory. For tests and local development."""

    configured = True

    def __init__(self) -> None:
        self.sent: list[Alert] = []

    async def send(self, alert: Alert) -> None:
        self.sent.append(alert)


class WebhookNotifier:
    """Posts a Slack-compatible payload.

    Slack-compatible rather than Slack-specific: `{"text": ...}` is what Slack,
    Mattermost, Discord's Slack endpoint and most generic webhook receivers
    accept, so the destination is a configuration choice rather than a code
    change.
    """

    configured = True

    def __init__(self, url: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._url = url
        self._client = client

    async def send(self, alert: Alert) -> None:
        payload = {
            "text": f"{alert.emoji} *{alert.title}*\n{alert.body}",
            # Structured alongside the text so a receiver that parses can,
            # and one that does not still shows something readable.
            "attachments": [
                {
                    "color": {"critical": "#c0392b", "warning": "#d98324"}.get(
                        alert.severity, "#1a7f37"
                    ),
                    "fields": [
                        {"title": key, "value": str(value), "short": True}
                        for key, value in sorted(alert.detail.items())
                    ],
                }
            ],
        }
        client = self._client or httpx.AsyncClient(timeout=TIMEOUT)
        try:
            response = await client.post(self._url, json=payload)
            if response.status_code >= 400:
                # Raised, not logged: the caller records that the alert was
                # not delivered, and an undelivered alert must not be marked
                # as sent.
                raise DeliveryFailed(
                    f"the webhook returned {response.status_code}"
                )
        finally:
            if self._client is None:
                await client.aclose()


class EmailNotifier:
    """The same mailer the weekly report uses."""

    configured = True

    def __init__(self, mailer: Any, recipients: list[str]) -> None:
        self._mailer = mailer
        self._recipients = recipients

    async def send(self, alert: Alert) -> None:
        from api.reports.mail import Message

        detail = "\n".join(
            f"  {key}: {value}" for key, value in sorted(alert.detail.items())
        )
        text = f"{alert.body}\n\n{detail}" if detail else alert.body
        await self._mailer.send(
            Message(
                to=self._recipients,
                subject=f"[{alert.severity}] {alert.title}",
                text=text,
                html=f"<pre>{text}</pre>",
            )
        )


class DeliveryFailed(RuntimeError):
    """The destination refused it. Never swallowed: an alert that did not
    arrive must not be recorded as one that did."""


class Fanout:
    """Every configured destination, and a failure in one does not silence
    the others — the point of having two is that one can be down."""

    def __init__(self, notifiers: list[Notifier]) -> None:
        self._notifiers = notifiers

    @property
    def configured(self) -> bool:
        return any(n.configured for n in self._notifiers)

    async def send(self, alert: Alert) -> None:
        failures: list[str] = []
        for notifier in self._notifiers:
            try:
                await notifier.send(alert)
            except Exception as exc:
                logger.warning("alert delivery failed: %s", exc)
                failures.append(f"{type(notifier).__name__}: {exc}")
        if failures and len(failures) == len(self._notifiers):
            raise DeliveryFailed("; ".join(failures))


def from_env() -> Notifier:
    """Whatever is configured, or an honest nothing.

    `ALERT_WEBHOOK_URL` for a chat channel, `ALERT_EMAIL` (comma separated)
    for email, both for both.
    """
    notifiers: list[Notifier] = []

    webhook = os.environ.get("ALERT_WEBHOOK_URL")
    if webhook:
        notifiers.append(WebhookNotifier(webhook))

    recipients = [
        address.strip()
        for address in (os.environ.get("ALERT_EMAIL") or "").split(",")
        if address.strip()
    ]
    if recipients:
        from api.reports.mail import SmtpMailer, SmtpSettings

        smtp = SmtpSettings.from_env()
        if smtp is None:
            logger.warning(
                "ALERT_EMAIL is set but no mail server is configured; "
                "alerts will not be emailed"
            )
        else:
            notifiers.append(EmailNotifier(SmtpMailer(smtp), recipients))

    if not notifiers:
        return NullNotifier()
    return Fanout(notifiers) if len(notifiers) > 1 else notifiers[0]


def render(alert: Alert) -> str:
    """One line, for a log or a test."""
    return json.dumps(
        {
            "severity": alert.severity,
            "kind": alert.kind,
            "title": alert.title,
            "body": alert.body,
            "detail": alert.detail,
        },
        sort_keys=True,
        default=str,
    )
