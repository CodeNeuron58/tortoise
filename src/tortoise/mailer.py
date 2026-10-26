"""Email delivery through Brevo's HTTP API, plus message assembly.

The email is the product: subject carries the number of applications near the
zone and the earliest comment deadline; the body is the brief — one card per
application, with what is proposed, why a neighbour might care, the deadline
and a link to the council's page — followed by the run's Lazy Payoff numbers.
Every email goes out as designed HTML and as plain text.

Facts in the cards that Tortoise owns (address, reference, deadline, link)
come from the council's pages, never from the model. When the brief was
skipped (failure test 3), the email says so plainly and shows only what the
council's own listing says.

Error model:
* ``MailAuthError`` — bad Brevo API key. Not retried.
* ``MailError`` — rate limits and server errors. Retried by the activity.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date

from .brief import DEFAULT_CLOSING, parse_brief
from .models import Application, RunNumbers

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"

# The palette, taken from the logo.
_PAGE = "#F3EFE4"
_INK = "#1E2B24"
_MUTED = "#5E6B62"
_GREEN_DARK = "#1D4A36"
_GREEN = "#2E6B4F"
_LEAF_BG = "#EAF0DD"
_CREAM = "#F8F3E6"
_AMBER = "#D9770B"
_AMBER_INK = "#8A4700"
_AMBER_BG = "#FCEFD9"
_BORDER = "#E5DECB"
# Unquoted family names: the styles sit inside single-quoted attributes.
_FONT = "-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif"
_MONO = "SFMono-Regular,Consolas,Liberation Mono,Menlo,monospace"


class MailError(Exception):
    """Transient email failure — worth retrying."""


class MailAuthError(Exception):
    """Brevo rejected the API key — not worth retrying."""


@dataclass(slots=True, frozen=True)
class EmailContent:
    """A fully assembled email, ready to send."""

    subject: str
    body: str  # plain text
    html: str = ""


@dataclass(slots=True, frozen=True)
class _Card:
    """One application as the email shows it."""

    application: Application
    proposed: str
    why_care: str
    label: str  # Gemma's type, e.g. "extension"


def build_email(
    zone_label: str,
    numbers: RunNumbers,
    brief_text: str | None,
    earliest_deadline: date | None,
    applications: list[Application] | None = None,
    labels: dict[str, str] | None = None,
    council_name: str = "",
    today: date | None = None,
    logo_url: str = "",
) -> EmailContent:
    """Assemble the weekly email from the brief, the applications and the numbers.

    ``brief_text`` is the model's reply (JSON, or plain text from older
    runs), or ``None`` when the brief was skipped — the reason is then in
    ``numbers.brief_skipped_reason`` — or nothing was kept this week.
    ``applications`` are the kept applications, as stored from the council's
    pages; ``labels`` maps a reference to Gemma's type label.
    """
    today = today or date.today()
    applications = list(applications or [])
    labels = labels or {}
    brief = parse_brief(brief_text) if brief_text else None
    count = numbers.kept

    if numbers.brief_skipped_reason:
        subject = f"Tortoise: this week's brief for {zone_label} was skipped"
    elif not brief_text:
        subject = f"No planning applications near {zone_label} this week"
    else:
        deadline_part = (
            f"earliest comment deadline {earliest_deadline.strftime('%d %B %Y')}"
            if earliest_deadline
            else "no comment deadline stated"
        )
        noun = "application" if count == 1 else "applications"
        subject = f"{count} planning {noun} near {zone_label} — {deadline_part}"

    cards: list[_Card] = []
    for application in sorted(applications, key=_deadline_key):
        entry = brief.entry_for(application.reference) if brief else None
        cards.append(
            _Card(
                application=application,
                proposed=entry.proposed if entry else application.proposal or "not stated",
                why_care=entry.why_care if entry else "",
                label=labels.get(application.reference, ""),
            )
        )

    body = _text_body(zone_label, numbers, brief, cards, today)
    page = _html_body(
        zone_label, numbers, brief, cards, earliest_deadline, council_name, today, logo_url, subject
    )
    return EmailContent(subject=subject, body=body, html=page)


def _deadline_key(application: Application) -> tuple[int, date]:
    deadline = application.comment_deadline
    return (0, deadline) if deadline else (1, date.max)


def _fmt_date(value: date) -> str:
    return f"{value:%a} {value.day} {value:%b %Y}"


def _days_left(deadline: date, today: date) -> str:
    days = (deadline - today).days
    if days > 1:
        return f"{days} days left"
    if days == 1:
        return "1 day left"
    if days == 0:
        return "closes today"
    return "deadline passed"


# --- plain text ---------------------------------------------------------------


def _text_body(zone_label, numbers, brief, cards, today) -> str:
    if numbers.brief_skipped_reason:
        lines = [
            f"The weekly brief was skipped: {numbers.brief_skipped_reason}.",
            "No planning facts below — nothing was invented to fill the gap.",
        ]
        if cards:
            lines.append("\nWhat the council's own listing says:")
            lines += [_text_card(card, today, listing_only=True) for card in cards]
        return "\n".join(lines) + f"\n\n{numbers.footer()}"
    if brief is None:
        return f"Nothing near your watch zone in last week's list.\n\n{numbers.footer()}"
    if not brief.structured or not cards:
        return f"{brief.raw.strip()}\n\n{numbers.footer()}"
    parts = [_text_card(card, today) for card in cards]
    return "\n\n".join(parts) + f"\n\n{brief.closing or DEFAULT_CLOSING}\n\n{numbers.footer()}"


def _text_card(card: _Card, today: date, listing_only: bool = False) -> str:
    app = card.application
    deadline = (
        f"{_fmt_date(app.comment_deadline)} ({_days_left(app.comment_deadline, today)})"
        if app.comment_deadline
        else "not stated"
    )
    lines = [f"{app.address} — {app.reference}"]
    lines.append(f"Proposed: {app.proposal if listing_only else card.proposed}")
    if card.why_care and not listing_only:
        lines.append(f"Why a neighbour might care: {card.why_care}")
    lines.append(f"Comment by: {deadline}")
    lines.append(f"Council page: {app.url}")
    return "\n".join(lines)


# --- HTML ---------------------------------------------------------------------


def _esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def _rich(text: str) -> str:
    """Escape model text, keeping **bold** and line breaks."""
    escaped = _esc(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    return escaped.replace("\n", "<br>")


def _html_body(
    zone_label, numbers, brief, cards, earliest, council_name, today, logo_url, subject
) -> str:
    if numbers.brief_skipped_reason:
        eyebrow, title = "BRIEF SKIPPED THIS WEEK", "This week's brief was skipped"
        lead = (
            f"The summary step failed: {_esc(numbers.brief_skipped_reason)}. Below is only what "
            "the council's own listing says — nothing was invented to fill the gap."
        )
        sections = [_html_card(card, today, listing_only=True) for card in cards]
    elif brief is None:
        eyebrow, title = "WEEKLY PLANNING BRIEF", f"Nothing near {_esc(zone_label)} this week"
        lead = (
            f"Tortoise read the whole weekly list ({numbers.listed} applications); none of them "
            "is in your watch zone. Enjoy the quiet."
        )
        sections = []
    else:
        noun = "application" if numbers.kept == 1 else "applications"
        eyebrow = "WEEKLY PLANNING BRIEF"
        title = f"{numbers.kept} planning {noun} near {_esc(zone_label)}"
        lead = ""
        if brief.structured and cards:
            sections = [_html_card(card, today) for card in cards]
        else:
            sections = [_html_panel(f"<div style='font-size:15px;line-height:1.6;color:{_INK}'>"
                                    f"{_rich(brief.raw)}</div>")]
        sections.append(
            f"<tr><td style='padding:6px 8px 22px;text-align:center;font:italic 15px/1.5 {_FONT};"
            f"color:{_MUTED}'>{_esc(brief.closing or DEFAULT_CLOSING)}</td></tr>"
        )
    if council_name:
        eyebrow = f"{eyebrow} · {_esc(council_name.upper())}"

    pill = ""
    if earliest and not numbers.brief_skipped_reason and brief is not None:
        pill = (
            f"<table role='presentation' cellpadding='0' cellspacing='0' style='margin-top:18px'><tr>"
            f"<td style='background:{_AMBER};border-radius:999px;padding:8px 16px;"
            f"font:600 14px/1.2 {_FONT};color:#FFFFFF'>Earliest deadline to comment: "
            f"{_fmt_date(earliest)} · {_days_left(earliest, today)}</td></tr></table>"
        )
    lead_html = (
        f"<div style='margin-top:12px;font:15px/1.55 {_FONT};color:#D9E4D2'>{lead}</div>" if lead else ""
    )
    hero = (
        f"<tr><td style='background:{_GREEN_DARK};border-radius:18px;padding:30px 30px 28px'>"
        f"<div style='font:700 12px/1.2 {_FONT};letter-spacing:.12em;color:#A9C08A'>{eyebrow}</div>"
        f"<div style='margin-top:10px;font:700 27px/1.25 {_FONT};color:{_CREAM}'>{title}</div>"
        f"{lead_html}{pill}</td></tr>{_spacer(18)}"
    )
    preheader = _esc(subject)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='color-scheme' content='light'><meta name='supported-color-schemes' content='light'>"
        f"<title>{_esc(subject)}</title></head>"
        f"<body style='margin:0;padding:0;background:{_PAGE}'>"
        f"<div style='display:none;max-height:0;overflow:hidden;opacity:0'>{preheader}</div>"
        f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='background:{_PAGE}'>"
        "<tr><td align='center' style='padding:28px 12px 36px'>"
        "<table role='presentation' width='600' cellpadding='0' cellspacing='0' "
        "style='width:100%;max-width:600px'>"
        f"{_html_header(logo_url)}{hero}{''.join(sections)}"
        f"{_html_payoff(numbers)}{_html_footer()}"
        "</table></td></tr></table></body></html>"
    )


def _spacer(height: int) -> str:
    return f"<tr><td style='height:{height}px;line-height:{height}px;font-size:0'>&nbsp;</td></tr>"


def _html_header(logo_url: str) -> str:
    logo = (
        f"<td style='padding-right:12px;vertical-align:middle'><img src='{_esc(logo_url)}' "
        "width='66' height='44' alt='' style='display:block;border:0;width:66px;height:44px'></td>"
        if logo_url
        else ""
    )
    return (
        "<tr><td style='padding:0 4px 18px'><table role='presentation' cellpadding='0' cellspacing='0'>"
        f"<tr>{logo}<td style='vertical-align:middle'>"
        f"<div style='font:800 22px/1.1 {_FONT};color:{_GREEN_DARK};letter-spacing:-.01em'>Tortoise</div>"
        f"<div style='font:13px/1.4 {_FONT};color:{_MUTED}'>Your planning list, read for you</div>"
        "</td></tr></table></td></tr>"
    )


def _html_panel(inner: str) -> str:
    return (
        f"<tr><td style='background:#FFFFFF;border:1px solid {_BORDER};border-radius:16px;"
        f"padding:24px 26px'>{inner}</td></tr>{_spacer(16)}"
    )


def _label(text: str) -> str:
    return (
        f"<div style='margin:16px 0 4px;font:700 11px/1.2 {_FONT};letter-spacing:.1em;"
        f"color:{_GREEN}'>{text}</div>"
    )


def _html_card(card: _Card, today: date, listing_only: bool = False) -> str:
    app = card.application
    chips = ""
    if card.label and not listing_only:
        chips += (
            f"<span style='display:inline-block;background:{_LEAF_BG};color:{_GREEN_DARK};"
            f"border-radius:999px;padding:4px 10px;font:600 12px/1.2 {_FONT};margin-right:8px'>"
            f"{_esc(card.label)}</span>"
        )
    chips += f"<span style='font:13px/1.2 {_MONO};color:{_MUTED}'>{_esc(app.reference)}</span>"

    if app.comment_deadline:
        deadline = (
            f"<strong style='color:{_AMBER_INK}'>Comment by {_fmt_date(app.comment_deadline)}</strong>"
            f"<span style='color:{_AMBER_INK}'> &nbsp;·&nbsp; {_days_left(app.comment_deadline, today)}</span>"
        )
    else:
        deadline = f"<span style='color:{_AMBER_INK}'>Comment deadline not stated on the council's page</span>"

    proposed = _esc(app.proposal) if listing_only else _rich(card.proposed)
    why = (
        _label("WHY A NEIGHBOUR MIGHT CARE")
        + f"<div style='font:15px/1.6 {_FONT};color:{_INK}'>{_rich(card.why_care)}</div>"
        if card.why_care and not listing_only
        else ""
    )
    inner = (
        f"<div>{chips}</div>"
        f"<div style='margin-top:12px;font:700 19px/1.3 {_FONT};color:{_GREEN_DARK}'>{_esc(app.address)}</div>"
        + _label("WHAT'S PROPOSED")
        + f"<div style='font:15px/1.6 {_FONT};color:{_INK}'>{proposed}</div>"
        + why
        + f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='margin-top:18px'>"
        f"<tr><td style='background:{_AMBER_BG};border-left:4px solid {_AMBER};border-radius:8px;"
        f"padding:12px 14px;font:14px/1.4 {_FONT}'>{deadline}</td></tr></table>"
        f"<table role='presentation' cellpadding='0' cellspacing='0' style='margin-top:16px'><tr>"
        f"<td style='background:{_GREEN_DARK};border-radius:10px'>"
        f"<a href='{_esc(app.url)}' style='display:inline-block;padding:11px 18px;font:600 14px/1.2 {_FONT};"
        f"color:#FFFFFF;text-decoration:none'>Open on the council's site &rarr;</a></td></tr></table>"
    )
    return _html_panel(inner)


def _html_payoff(numbers: RunNumbers) -> str:
    stats = [
        (str(numbers.listed), "applications listed"),
        (str(numbers.kept), "in your zone"),
        (str(numbers.pdfs_read), "PDFs read for you"),
        (str(numbers.pages_read), "pages read"),
        (f"{numbers.browser_minutes:g}", "browser minutes"),
        (f"${numbers.cost_usd:.4f}", "total cost"),
    ]
    cells = [
        f"<td width='33%' style='padding:10px 6px;vertical-align:top'>"
        f"<div style='font:800 22px/1.1 {_FONT};color:{_GREEN_DARK}'>{_esc(value)}</div>"
        f"<div style='margin-top:4px;font:12px/1.3 {_FONT};color:{_MUTED}'>{label}</div></td>"
        for value, label in stats
    ]
    rows = f"<tr>{''.join(cells[:3])}</tr><tr>{''.join(cells[3:])}</tr>"
    detail = (
        f"Gemma filtered locally in {numbers.gemma_seconds:.0f}s · Ultramarine used "
        f"{numbers.input_tokens + numbers.output_tokens:,} tokens"
    )
    unfiltered = (
        f"<div style='margin-top:12px;background:{_AMBER_BG};border-radius:8px;padding:10px 12px;"
        f"font:13px/1.45 {_FONT};color:{_AMBER_INK}'>Gemma was down this week, so every application "
        "in your zone was kept and marked unfiltered — nothing was dropped.</div>"
        if numbers.unfiltered
        else ""
    )
    return (
        f"<tr><td style='background:{_CREAM};border:1px solid {_BORDER};border-radius:16px;padding:20px 20px 16px'>"
        f"<div style='font:700 11px/1.2 {_FONT};letter-spacing:.12em;color:{_GREEN}'>THE LAZY PAYOFF</div>"
        f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='margin-top:6px'>{rows}</table>"
        f"<div style='margin-top:6px;font:12px/1.4 {_FONT};color:{_MUTED}'>{detail}</div>{unfiltered}"
        f"</td></tr>{_spacer(18)}"
    )


def _html_footer() -> str:
    return (
        f"<tr><td style='padding:0 16px;text-align:center;font:12px/1.6 {_FONT};color:{_MUTED}'>"
        "Tortoise reads the council's public planning register once a week, one page at a time. "
        "It never logs in, comments or submits anything. Deadlines and links come from the "
        "council's pages; anything missing says &ldquo;not stated&rdquo;.</td></tr>"
    )


def build_payload(
    api_key_from_email: str,
    from_name: str,
    to_email: str,
    content: EmailContent,
) -> dict:
    """The Brevo /smtp/email JSON payload; pure so tests can pin it."""
    payload = {
        "sender": {"name": from_name, "email": api_key_from_email},
        "to": [{"email": to_email}],
        "subject": content.subject,
        "textContent": content.body,
    }
    if content.html:
        payload["htmlContent"] = content.html
    return payload


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
