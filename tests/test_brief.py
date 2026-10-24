"""Tests for brief prompt building and document budgeting (offline)."""

from datetime import date

from tortoise.brief import BriefItem, budget_document_texts, build_prompt
from tortoise.models import Application, ExtractedDocument

APP = Application(
    reference="26/01234/FUL",
    council="west-suffolk",
    address="14a Mill Rd",
    proposal="Two-storey rear extension",
    url="https://example.gov.uk/app/1",
    comment_deadline=date(2026, 11, 6),
)


def make_document(name="plans.pdf", text="x" * 500) -> ExtractedDocument:
    return ExtractedDocument(
        council="west-suffolk",
        reference=APP.reference,
        filename=name,
        url=f"https://example.gov.uk/{name}",
        path=f"/tmp/{name}",
        pages=2,
        text=text,
    )


def make_item(documents=None, one_line="Two-storey rear extension at No. 14") -> BriefItem:
    return BriefItem(
        application=APP,
        documents=documents if documents is not None else [make_document()],
        filter_one_line=one_line,
        filter_type="extension",
    )


def test_prompt_contains_the_plan_instructions():
    prompt = build_prompt("Mill Road, Cambridge", [make_item()])
    assert 'Write "not stated" when a fact is missing; never guess' in prompt
    assert "Nothing else this week needs your attention." in prompt
    assert "under 400 words" in prompt


def test_prompt_contains_zone_application_and_documents():
    item = make_item()
    prompt = build_prompt("Mill Road, Cambridge", [item])
    assert "residents of Mill Road, Cambridge" in prompt
    assert "26/01234/FUL" in prompt
    assert "14a Mill Rd" in prompt
    assert "06 November 2026" in prompt
    assert "--- plans.pdf ---" in prompt
    assert "Two-storey rear extension at No. 14" in prompt  # the filter summary


def test_missing_deadline_is_explicit():
    app = Application(
        reference=APP.reference,
        council=APP.council,
        address=APP.address,
        proposal=APP.proposal,
        url=APP.url,
        comment_deadline=None,
    )
    prompt = build_prompt("zone", [BriefItem(application=app)])
    assert "Comment deadline: not stated" in prompt


def test_scanned_documents_are_excluded_from_prompt():
    scanned = make_document(name="drawings.pdf", text="")  # is_scanned
    readable = make_document(name="form.pdf", text="real text " * 100)
    item = make_item(documents=[scanned, readable])
    prompt = build_prompt("zone", [item])
    assert "drawings.pdf" not in prompt
    assert "form.pdf" in prompt
    assert "real text" in prompt


def test_no_documents_says_so():
    item = make_item(documents=[])
    prompt = build_prompt("zone", [item])
    assert "none with readable text (drawings only)" in prompt


def test_budget_leaves_short_texts_alone():
    texts = ["short", "also short"]
    assert budget_document_texts(texts) == texts


def test_budget_truncates_long_documents_and_keeps_all():
    long_text = "a" * 100_000
    texts = [long_text, "tiny"]
    budgeted = budget_document_texts(texts, max_input_tokens=10_000, overhead_tokens=0)
    assert len(budgeted) == 2
    assert budgeted[1] == "tiny"
    assert budgeted[0].startswith("aaaa")
    assert budgeted[0].endswith("...[truncated to fit the budget]")
    # Each long document keeps an equal share of (10_000 tokens * 4 chars).
    assert len(budgeted[0]) == 20_000 + len("...[truncated to fit the budget]")

def test_budget_never_drops_a_document():
    texts = ["a" * 500_000, "b" * 500_000, "c" * 500_000]
    budgeted = budget_document_texts(texts, max_input_tokens=1_000)
    assert len(budgeted) == 3
    assert all(t.endswith("...[truncated to fit the budget]") for t in budgeted)
