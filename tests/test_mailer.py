"""Tests for email assembly (offline — no Brevo calls)."""

from datetime import date

from tortoise.mailer import EmailContent, build_email, build_payload
from tortoise.models import Application, RunNumbers

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
    assert "htmlContent" not in payload  # no HTML part means none is sent
    with_html = build_payload("f@x.com", "T", "t@x.com", EmailContent(subject="s", body="b", html="<p>h</p>"))
    assert with_html["htmlContent"] == "<p>h</p>"


def _numbers(**changes) -> RunNumbers:
    base = dict(
        council="west-suffolk", week="2026-W44", status="completed", listed=23, kept=2,
        pdfs_read=2, pages_read=7, browser_seconds=102.6, gemma_seconds=82.0,
        input_tokens=4187, output_tokens=2852, cost_usd=0.00205405,
    )
    base.update(changes)
    return RunNumbers(**base)


APPS = [
    Application(
        reference="DC/26/1601/TCA", council="west-suffolk",
        address="3 Springfield Road Bury St Edmunds IP33 3AN",
        proposal="Crown reduction of one Indian Bean Tree", url="https://example.gov.uk/a?k=1&t=2",
        comment_deadline=date(2026, 11, 10),
    ),
    Application(
        reference="DC/26/1525/HH", council="west-suffolk",
        address="30 York Road Bury St Edmunds IP33 3EG",
        proposal="Single storey rear extension", url="https://example.gov.uk/b",
        comment_deadline=date(2026, 11, 14),
    ),
]
BRIEF = (
    '{"applications": ['
    '{"reference": "DC/26/1525/HH", "proposed": "A rear extension", "why_care": "Brick walls <b>matching</b>."},'
    '{"reference": "DC/26/1601/TCA", "proposed": "A tree cut back by 5m", "why_care": "not stated"}'
    '], "closing": "Nothing else this week needs your attention."}'
)


def test_designed_email_has_a_card_per_application():
    content = build_email(
        "York Road area", _numbers(), BRIEF, date(2026, 11, 10), applications=APPS,
        labels={"DC/26/1525/HH": "extension"}, council_name="West Suffolk Council",
        today=date(2026, 10, 26), logo_url="https://example.com/logo.png",
    )
    assert content.subject.startswith("2 planning applications near York Road area")
    page = content.html
    assert page.count("Open on the council's site") == 2
    assert "Comment by Tue 10 Nov 2026" in page and "15 days left" in page
    assert "Comment by Sat 14 Nov 2026" in page and "19 days left" in page
    assert page.index("Springfield") < page.index("York Road Bury")  # earliest deadline first
    assert "https://example.gov.uk/a?k=1&amp;t=2" in page  # links escaped
    assert "&lt;b&gt;matching&lt;/b&gt;" in page  # model text never injects HTML
    assert ">extension<" in page
    assert "https://example.com/logo.png" in page
    assert "$0.0021" in page and "PDFs read for you" in page
    # The plain-text part carries the same facts.
    assert "Comment by: Tue 10 Nov 2026 (15 days left)" in content.body
    assert "Why a neighbour might care: Brick walls" in content.body
    assert content.body.rstrip().endswith("cost $0.0021.")


def test_single_application_subject_is_singular():
    content = build_email("Mill Road", _numbers(kept=1), BRIEF, None, applications=APPS[:1])
    assert content.subject == "1 planning application near Mill Road — no comment deadline stated"


def test_skipped_brief_shows_only_the_council_listing():
    content = build_email(
        "York Road area", _numbers(brief_skipped_reason="Ultramarine has no credit left"),
        None, None, applications=APPS, today=date(2026, 10, 26),
    )
    assert "was skipped" in content.subject
    assert "Ultramarine has no credit left" in content.html
    assert "Crown reduction of one Indian Bean Tree" in content.html  # listing, not model text
    assert "WHY A NEIGHBOUR MIGHT CARE" not in content.html
    assert "What the council's own listing says" in content.body


def test_unfiltered_run_says_so_in_the_email():
    content = build_email("York Road area", _numbers(unfiltered=True), BRIEF, None, applications=APPS)
    assert "Gemma was down this week" in content.html


def test_plain_text_brief_from_older_runs_still_renders():
    content = build_email("Zone", _numbers(), "# Old brief\nIn **markdown**.", None, applications=APPS)
    assert "In <strong>markdown</strong>." in content.html
    assert content.body.startswith("# Old brief")


def test_inline_styles_never_break_out_of_their_attribute():
    from html.parser import HTMLParser

    allowed = {"style", "role", "width", "height", "cellpadding", "cellspacing", "align",
               "href", "src", "alt", "charset", "name", "content"}
    seen: set[str] = set()

    class Collect(HTMLParser):
        def handle_starttag(self, tag, attrs):
            seen.update(name for name, _ in attrs)

    page = build_email("Zone", _numbers(), BRIEF, None, applications=APPS,
                       logo_url="https://example.com/logo.png").html
    Collect().feed(page)
    # A quote inside a style value would end the attribute early and leave
    # fragments like "ui',roboto,..." behind as junk attribute names.
    assert seen <= allowed, seen - allowed
