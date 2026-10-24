"""Email delivery through Brevo's HTTP API, plus message assembly.

The email is the product: subject carries the number of applications near the
zone and the earliest comment deadline; the body is the brief followed by a
footer with that run's Lazy Payoff numbers and cost. When the brief was
skipped (failure test 3), the email says so plainly instead of going silent.

Error model:
* ``MailAuthError`` — bad Brevo API key. Not retried.
* ``MailError`` — rate limits and server errors. Retried by the activity.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date

from .models import RunNumbers

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"


class MailError(Exception):
    """Transient email failure — worth retrying."""


class MailAuthError(Exception):
    """Brevo rejected the API key — not worth retrying."""


@dataclass(slots=True, frozen=True)
class EmailContent:
    """A fully assembled email, ready to send."""

    subject: str
    body: str


def build_email(
    zone_label: str,
    numbers: RunNumbers,
    brief_text: str | None,
    earliest_deadline: date | None,
) -> EmailContent:
    """Assemble the weekly email from the brief and the run's numbers.

    ``brief_text`` is ``None`` when the brief was skipped (the reason is in
    ``numbers.brief_skipped_reason``) or when nothing was kept this week.
    """
    if numbers.brief_skipped_reason:
        subject = f"Tortoise: this week's brief for {zone_label} was skipped"
        body = (
            f"The weekly brief was skipped: {numbers.brief_skipped_reason}.\n"
            f"No planning facts below — nothing was invented to fill the gap.\n\n"
            f"{numbers.footer()}"
        )
    elif not brief_text:
        subject = f"No planning applications near {zone_label} this week"
        body = (
            "Nothing near your watch zone in last week's list.\n\n"
            f"{numbers.footer()}"
        )
    else:
        deadline_part = (
            f"earliest comment deadline {earliest_deadline.strftime('%d %B %Y')}"
            if earliest_deadline
            else "no comment deadline stated"
        )
        subject = f"{numbers.kept} planning applications near {zone_label} — {deadline_part}"
        body = f"{brief_text}\n\n{numbers.footer()}"
    return EmailContent(subject=subject, body=body)


def build_payload(
    api_key_from_email: str,
    from_name: str,
    to_email: str,
    content: EmailContent,
) -> dict:
    """The Brevo /smtp/email JSON payload; pure so tests can pin it."""
    return {
        "sender": {"name": from_name, "email": api_key_from_email},
        "to": [{"email": to_email}],
        "subject": content.subject,
        "textContent": content.body,
    }


class BrevoSender:
    """Sends the weekly email via Brevo's free-plan HTTP API."""

    def __init__(
        self,
        api_key: str,
        from_email: str,
        to_email: str,
        from_name: str = "Tortoise",
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.from_email = from_email
        self.from_name = from_name
        self.to_email = to_email
        self.timeout = timeout

    async def send(self, content: EmailContent) -> None:
        """Send one email; raises :class:`MailError`/:class:`MailAuthError`."""
        payload = build_payload(self.from_email, self.from_name, self.to_email, content)
        request = urllib.request.Request(
            BREVO_API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "api-key": self.api_key,
                "content-type": "application/json",
                "accept": "application/json",
            },
            method="POST",
        )

        def _blocking_send() -> None:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if response.status not in (200, 201, 202):
                    raise MailError(f"Brevo returned HTTP {response.status}")

        # urllib is blocking; keep the worker's event loop free.
        try:
            await asyncio.to_thread(_blocking_send)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise MailAuthError(f"Brevo rejected the API key (HTTP {exc.code})") from exc
            if exc.code == 429 or exc.code >= 500:
                raise MailError(f"Brevo is temporarily unavailable (HTTP {exc.code})") from exc
            raise MailError(f"Brevo rejected the email (HTTP {exc.code}: {exc.reason})") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise MailError(f"could not reach Brevo: {exc}") from exc
