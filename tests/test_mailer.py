"""Tests for email assembly (offline — no Brevo calls)."""

from datetime import date

from tortoise.mailer import EmailContent, build_email, build_payload
from tortoise.models import RunNumbers

NUMBERS = RunNumbers(
    council="west-suffolk",
    week="2026-W43",
    status="completed",
    listed=40,
    kept=3,
    pdfs_read=7,
    pages_read=12,
    browser_seconds=240.0,
    gemma_seconds=90.0,
    input_tokens=60_000,
    output_tokens=800,
    cost_usd=0.0095,
)


def test_subject_has_count_and_earliest_deadline():
    content = build_email("Mill Road", NUMBERS, "the brief", date(2026, 11, 6))
    assert content.subject == (
        "3 planning applications near Mill Road — earliest comment deadline 06 November 2026"
    )
    assert content.body.startswith("the brief")
    assert "Listed 40" in content.body
    assert "$0.0095" in content.body


def test_subject_without_deadline_says_so():
    content = build_email("Mill Road", NUMBERS, "the brief", None)
    assert "no comment deadline stated" in content.subject


def test_zero_applications_email():
    numbers = NUMBERS
    numbers.kept = 0
    content = build_email("Mill Road", numbers, None, None)
    assert content.subject == "No planning applications near Mill Road this week"
    assert "Nothing near your watch zone" in content.body


def test_skipped_brief_email_says_why():
    numbers = NUMBERS
    numbers.brief_skipped_reason = "Ultramarine has no credit left"
    content = build_email("Mill Road", numbers, None, None)
    assert "was skipped" in content.subject
    assert "Ultramarine has no credit left" in content.body
    assert "nothing was invented" in content.body


def test_payload_shape():
    content = EmailContent(subject="s", body="b")
    payload = build_payload("from@example.com", "Tortoise", "to@example.com", content)
    assert payload["sender"] == {"name": "Tortoise", "email": "from@example.com"}
    assert payload["to"] == [{"email": "to@example.com"}]
    assert payload["subject"] == "s"
    assert payload["textContent"] == "b"
