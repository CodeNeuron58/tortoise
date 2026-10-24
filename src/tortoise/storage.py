"""SQLite persistence: applications, documents, filter results, runs, costs.

Deliberately boring and dumb: every method is a small, idempotent upsert or
select with no business logic. Idempotency is what makes failure test 1 pass
("resumes from the last heartbeated page; no duplicate rows") — re-running any
activity over the same data must not create duplicate rows.

The database lives in the data directory (``data/tortoise.db``), which is
gitignored: downloaded planning documents and applicant material must never
be committed.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .models import Application, ExtractedDocument, FilterResult, RunNumbers, TokenUsage

_SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    council          TEXT NOT NULL,
    reference        TEXT NOT NULL,
    week             TEXT NOT NULL,
    address          TEXT NOT NULL,
    proposal         TEXT NOT NULL,
    url              TEXT NOT NULL,
    validated_on     TEXT,
    comment_deadline TEXT,
    postcode         TEXT NOT NULL DEFAULT '',
    first_seen       TEXT NOT NULL,
    PRIMARY KEY (council, reference)
);

CREATE TABLE IF NOT EXISTS documents (
    council       TEXT NOT NULL,
    reference     TEXT NOT NULL,
    filename      TEXT NOT NULL,
    url           TEXT NOT NULL,
    path          TEXT NOT NULL,
    pages         INTEGER NOT NULL,
    text          TEXT NOT NULL DEFAULT '',
    text_chars    INTEGER NOT NULL,
    is_scanned    INTEGER NOT NULL,
    downloaded_at TEXT NOT NULL,
    PRIMARY KEY (council, reference, filename)
);

CREATE TABLE IF NOT EXISTS filter_results (
    council       TEXT NOT NULL,
    reference     TEXT NOT NULL,
    model         TEXT NOT NULL,
    type          TEXT NOT NULL,
    relevance     INTEGER,
    one_line      TEXT NOT NULL,
    street_impact TEXT NOT NULL,
    kept          INTEGER NOT NULL,
    raw           TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (council, reference)
);

CREATE TABLE IF NOT EXISTS runs (
    council               TEXT NOT NULL,
    week                  TEXT NOT NULL,
    status                TEXT NOT NULL,
    started_at            TEXT NOT NULL,
    finished_at           TEXT,
    listed                INTEGER NOT NULL DEFAULT 0,
    kept                  INTEGER NOT NULL DEFAULT 0,
    pdfs_read             INTEGER NOT NULL DEFAULT 0,
    pages_read            INTEGER NOT NULL DEFAULT 0,
    browser_minutes       REAL    NOT NULL DEFAULT 0,
    gemma_seconds         REAL    NOT NULL DEFAULT 0,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    cached_tokens         INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cost_usd              REAL    NOT NULL DEFAULT 0,
    brief_skipped_reason  TEXT    NOT NULL DEFAULT '',
    unfiltered            INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (council, week)
);

CREATE TABLE IF NOT EXISTS costs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    council       TEXT NOT NULL,
    week          TEXT NOT NULL,
    call          TEXT NOT NULL,
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL,
    cached_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd      REAL NOT NULL,
    created_at    TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pdf_dir(data_dir: Path, council: str, reference: str) -> Path:
    """Where a council application's downloaded PDFs live."""
    safe_ref = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in reference)
    return data_dir / "pdfs" / council / safe_ref


class Database:
    """Thin, idempotent persistence layer over SQLite."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def init_schema(self) -> None:
        with self._connect() as db:
            db.executescript(_SCHEMA)

    # --- applications -----------------------------------------------------

    def upsert_application(self, application: Application, week: str) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO applications (council, reference, week, address, proposal, url,
                                          validated_on, comment_deadline, postcode, first_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(council, reference) DO UPDATE SET
                    week             = excluded.week,
                    address          = excluded.address,
                    proposal         = excluded.proposal,
                    url              = excluded.url,
                    validated_on     = excluded.validated_on,
                    comment_deadline = COALESCE(excluded.comment_deadline, applications.comment_deadline),
                    postcode         = excluded.postcode
                """,
                (
                    application.council,
                    application.reference,
                    week,
                    application.address,
                    application.proposal,
                    application.url,
                    application.validated_on.isoformat() if application.validated_on else None,
                    application.comment_deadline.isoformat() if application.comment_deadline else None,
                    application.postcode,
                    _now(),
                ),
            )

    def set_comment_deadline(self, council: str, reference: str, deadline) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE applications SET comment_deadline = ? WHERE council = ? AND reference = ?",
                (deadline.isoformat() if deadline else None, council, reference),
            )

    def applications_for_week(self, council: str, week: str) -> list[Application]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM applications WHERE council = ? AND week = ? ORDER BY reference",
                (council, week),
            ).fetchall()
        return [
            Application(
                reference=row["reference"],
                council=row["council"],
                address=row["address"],
                proposal=row["proposal"],
                url=row["url"],
                validated_on=_date_or_none(row["validated_on"]),
                comment_deadline=_date_or_none(row["comment_deadline"]),
                postcode=row["postcode"],
            )
            for row in rows
        ]

    def has_completed_run(self, council: str, week: str) -> bool:
        """True when this council+week already ran to completion (dedup guard)."""
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM runs WHERE council = ? AND week = ? AND status = 'completed'",
                (council, week),
            ).fetchone()
        return row is not None

    # --- documents --------------------------------------------------------

    def save_document(self, document: ExtractedDocument) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO documents (council, reference, filename, url, path, pages,
                                       text, text_chars, is_scanned, downloaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(council, reference, filename) DO UPDATE SET
                    url = excluded.url,
                    path = excluded.path,
                    pages = excluded.pages,
                    text = excluded.text,
                    text_chars = excluded.text_chars,
                    is_scanned = excluded.is_scanned,
                    downloaded_at = excluded.downloaded_at
                """,
                (
                    document.council,
                    document.reference,
                    document.filename,
                    document.url,
                    document.path,
                    document.pages,
                    document.text,
                    len(document.text),
                    1 if document.is_scanned else 0,
                    _now(),
                ),
            )

    def documents_for(self, council: str, reference: str) -> list[ExtractedDocument]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM documents WHERE council = ? AND reference = ? ORDER BY filename",
                (council, reference),
            ).fetchall()
        return [
            ExtractedDocument(
                council=row["council"],
                reference=row["reference"],
                filename=row["filename"],
                url=row["url"],
                path=row["path"],
                pages=row["pages"],
                text=row["text"],
            )
            for row in rows
        ]

    # --- filter results ---------------------------------------------------

    def save_filter_result(self, result: FilterResult, council: str, kept: bool) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO filter_results (council, reference, model, type, relevance,
                                            one_line, street_impact, kept, raw, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(council, reference) DO UPDATE SET
                    model = excluded.model,
                    type = excluded.type,
                    relevance = excluded.relevance,
                    one_line = excluded.one_line,
                    street_impact = excluded.street_impact,
                    kept = excluded.kept,
                    raw = excluded.raw,
                    created_at = excluded.created_at
                """,
                (
                    council,
                    result.reference,
                    result.model,
                    result.type,
                    result.relevance,
                    result.one_line,
                    ",".join(result.street_impact),
                    1 if kept else 0,
                    result.raw,
                    _now(),
                ),
            )

    def kept_references(self, council: str) -> set[str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT reference FROM filter_results WHERE council = ? AND kept = 1",
                (council,),
            ).fetchall()
        return {row["reference"] for row in rows}

    # --- runs and costs ---------------------------------------------------

    def upsert_run(self, numbers: RunNumbers) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO runs (council, week, status, started_at, finished_at, listed, kept,
                                  pdfs_read, pages_read, browser_minutes, gemma_seconds,
                                  input_tokens, cached_tokens, output_tokens, cost_usd,
                                  brief_skipped_reason, unfiltered)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(council, week) DO UPDATE SET
                    status = excluded.status,
                    finished_at = excluded.finished_at,
                    listed = excluded.listed,
                    kept = excluded.kept,
                    pdfs_read = excluded.pdfs_read,
                    pages_read = excluded.pages_read,
                    browser_minutes = excluded.browser_minutes,
                    gemma_seconds = excluded.gemma_seconds,
                    input_tokens = excluded.input_tokens,
                    cached_tokens = excluded.cached_tokens,
                    output_tokens = excluded.output_tokens,
                    cost_usd = excluded.cost_usd,
                    brief_skipped_reason = excluded.brief_skipped_reason,
                    unfiltered = excluded.unfiltered
                """,
                (
                    numbers.council,
                    numbers.week,
                    numbers.status,
                    _now(),
                    _now() if numbers.status in ("completed", "failed") else None,
                    numbers.listed,
                    numbers.kept,
                    numbers.pdfs_read,
                    numbers.pages_read,
                    numbers.browser_minutes,
                    numbers.gemma_seconds,
                    numbers.input_tokens,
                    numbers.cached_tokens,
                    numbers.output_tokens,
                    numbers.cost_usd,
                    numbers.brief_skipped_reason,
                    1 if numbers.unfiltered else 0,
                ),
            )

    def recent_runs(self, limit: int = 10) -> list[RunNumbers]:
        """The most recent runs, newest first — the Lazy Payoff log."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            RunNumbers(
                council=row["council"],
                week=row["week"],
                status=row["status"],
                listed=row["listed"],
                kept=row["kept"],
                pdfs_read=row["pdfs_read"],
                pages_read=row["pages_read"],
                browser_seconds=row["browser_minutes"] * 60.0,
                gemma_seconds=row["gemma_seconds"],
                input_tokens=row["input_tokens"],
                cached_tokens=row["cached_tokens"],
                output_tokens=row["output_tokens"],
                cost_usd=row["cost_usd"],
                brief_skipped_reason=row["brief_skipped_reason"],
                unfiltered=bool(row["unfiltered"]),
            )
            for row in rows
        ]

    def log_cost(
        self, council: str, week: str, call: str, model: str, usage: TokenUsage, cost_usd: float
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO costs (council, week, call, model, input_tokens,
                                   cached_tokens, output_tokens, cost_usd, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    council,
                    week,
                    call,
                    model,
                    usage.input_tokens,
                    usage.cached_tokens,
                    usage.output_tokens,
                    cost_usd,
                    _now(),
                ),
            )


def _date_or_none(iso: str | None):
    if not iso:
        return None
    from datetime import date

    return date.fromisoformat(iso)
