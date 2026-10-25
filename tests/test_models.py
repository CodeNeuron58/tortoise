"""Tests for the pure parts of tortoise.models."""

from datetime import date

from tortoise.models import RunNumbers, parse_uk_date


def test_parse_uk_date_handles_common_council_formats():
    assert parse_uk_date("14 October 2026") == date(2026, 10, 14)
    assert parse_uk_date("14 Oct 2026") == date(2026, 10, 14)
    assert parse_uk_date("2026-10-14") == date(2026, 10, 14)
    assert parse_uk_date("14/10/2026") == date(2026, 10, 14)
    assert parse_uk_date("14-10-2026") == date(2026, 10, 14)
    assert parse_uk_date(" 14 Oct 2026 ") == date(2026, 10, 14)
    # Idox's Important Dates tab prefixes the weekday.
    assert parse_uk_date("Sat 14 Nov 2026") == date(2026, 11, 14)
    assert parse_uk_date("Saturday, 14 November 2026") == date(2026, 11, 14)


def test_parse_uk_date_fails_soft():
    assert parse_uk_date("") is None
    assert parse_uk_date(None) is None
    assert parse_uk_date("not a date") is None
    assert parse_uk_date("Not Available") is None
    # Ambiguous US-style month-first dates must NOT be parsed as day-first.
    assert parse_uk_date("10/14/2026") is None


def test_run_numbers_browser_minutes():
    numbers = RunNumbers(browser_seconds=300.0)
    assert numbers.browser_minutes == 5.0


def test_run_numbers_footer_has_lazy_payoff_numbers():
    numbers = RunNumbers(
        listed=40, kept=3, pdfs_read=7, pages_read=12,
        browser_seconds=240.0, gemma_seconds=90.0,
        input_tokens=10_000, output_tokens=2_000, cost_usd=0.0065,
    )
    footer = numbers.footer()
    assert "Listed 40" in footer
    assert "kept 3" in footer
    assert "PDFs read 7" in footer
    assert "pages read 12" in footer
    assert "4.0 min" in footer
    assert "$0.0065" in footer
