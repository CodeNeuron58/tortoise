"""Tests for the SQLite storage layer."""

from datetime import date

from tortoise.models import Application, ExtractedDocument, FilterResult, RunNumbers, TokenUsage
from tortoise.storage import Database


def make_db(tmp_path) -> Database:
    db = Database(tmp_path / "tortoise.db")
    db.init_schema()
    return db


def make_application(reference="26/01234/FUL", council="west-suffolk") -> Application:
    return Application(
        reference=reference,
        council=council,
        address="14a Mill Rd, Bury St Edmunds IP33 1AA",
        proposal="Two-storey rear extension",
        url="https://planning.example.gov.uk/app/1",
        validated_on=date(2026, 10, 19),
    )


def test_application_upsert_is_idempotent(tmp_path):
    db = make_db(tmp_path)
    app = make_application()
    db.upsert_application(app, "2026-W43")
    db.upsert_application(app, "2026-W43")
    rows = db.applications_for_week("west-suffolk", "2026-W43")
    assert len(rows) == 1
    assert rows[0].address.startswith("14a Mill Rd")
    assert rows[0].validated_on == date(2026, 10, 19)


def test_application_upsert_preserves_discovered_deadline(tmp_path):
    db = make_db(tmp_path)
    db.upsert_application(make_application(), "2026-W43")
    db.set_comment_deadline("west-suffolk", "26/01234/FUL", date(2026, 11, 6))
    # A re-crawl that does not see a deadline must not erase the one found earlier.
    db.upsert_application(make_application(), "2026-W43")
    rows = db.applications_for_week("west-suffolk", "2026-W43")
    assert rows[0].comment_deadline == date(2026, 11, 6)


def test_documents_roundtrip_and_scanned_flag(tmp_path):
    db = make_db(tmp_path)
    document = ExtractedDocument(
        council="west-suffolk",
        reference="26/01234/FUL",
        filename="plans.pdf",
        url="https://example.gov.uk/plans.pdf",
        path=str(tmp_path / "plans.pdf"),
        pages=12,
        text="x" * 500,
    )
    db.save_document(document)
    db.save_document(document)  # idempotent
    loaded = db.documents_for("west-suffolk", "26/01234/FUL")
    assert len(loaded) == 1
    assert loaded[0].pages == 12
    assert loaded[0].is_scanned is False


def test_filter_result_saved_with_kept_flag(tmp_path):
    db = make_db(tmp_path)
    result = FilterResult(
        reference="26/01234/FUL",
        type="extension",
        street_impact=("parking", "noise"),
        relevance=3,
        one_line="Two-storey rear extension at No. 14",
        model="gemma4:e4b",
        raw='{"type": "extension"}',
    )
    db.save_filter_result(result, "west-suffolk", kept=True)
    assert db.kept_references("west-suffolk") == {"26/01234/FUL"}


def test_run_dedup_guard(tmp_path):
    db = make_db(tmp_path)
    assert not db.has_completed_run("west-suffolk", "2026-W43")
    numbers = RunNumbers(council="west-suffolk", week="2026-W43", status="running")
    db.upsert_run(numbers)
    assert not db.has_completed_run("west-suffolk", "2026-W43")  # running, not completed
    numbers.status = "completed"
    db.upsert_run(numbers)
    assert db.has_completed_run("west-suffolk", "2026-W43")


def test_cost_log_roundtrip(tmp_path):
    db = make_db(tmp_path)
    db.log_cost(
        "west-suffolk", "2026-W43", "brief", "ultramarine-1-flash",
        TokenUsage(input_tokens=1000, cached_tokens=200, output_tokens=500),
        cost_usd=0.0005,
    )
    import sqlite3

    with sqlite3.connect(db.path) as conn:
        row = conn.execute("SELECT call, input_tokens, cost_usd FROM costs").fetchone()
    assert row[0] == "brief"
    assert row[1] == 1000
    assert abs(row[2] - 0.0005) < 1e-9


def test_brief_and_run_numbers_roundtrip(tmp_path):
    db = make_db(tmp_path)
    assert db.brief_for("west-suffolk", "2026-W44") is None
    db.save_brief("west-suffolk", "2026-W44", "ultramarine-1-flash", '{"applications": []}')
    db.save_brief("west-suffolk", "2026-W44", "ultramarine-1-flash", '{"applications": [1]}')  # replaced
    assert db.brief_for("west-suffolk", "2026-W44") == '{"applications": [1]}'

    db.upsert_run(RunNumbers(council="west-suffolk", week="2026-W44", status="completed", listed=23, kept=2))
    numbers = db.run_numbers("west-suffolk", "2026-W44")
    assert (numbers.listed, numbers.kept, numbers.status) == (23, 2, "completed")
    assert db.run_numbers("west-suffolk", "2026-W45") is None


def test_application_lookup_by_reference(tmp_path):
    db = make_db(tmp_path)
    db.upsert_application(make_application("26/0001/FUL"), "2026-W43")
    assert db.application("west-suffolk", "26/0001/FUL").address.startswith("14a Mill Rd")
    assert db.application("west-suffolk", "nope") is None
