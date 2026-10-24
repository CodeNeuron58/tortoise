"""PDF text extraction, with scanned and drawing-only documents detected.

Tortoise only wants *text* documents (application forms, design and access
statements, planning statements). Scans and drawing sets yield almost no
text; they are flagged and skipped rather than fed to a model, per the plan
("skips drawings and scans" — and the brief later says "drawings only").
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

# A PDF with fewer extractable characters than this is treated as a scan or
# a drawing set. Real text documents run to thousands of characters.
SCANNED_TEXT_THRESHOLD = 200


class ExtractionError(Exception):
    """The PDF could not be read at all (corrupt, password-protected, ...)."""


@dataclass(slots=True, frozen=True)
class ExtractedText:
    """Raw extraction output for one PDF file."""

    pages: int
    text: str

    @property
    def is_scanned(self) -> bool:
        return len(self.text.strip()) < SCANNED_TEXT_THRESHOLD


def extract_text(path: str | Path) -> ExtractedText:
    """Pull text out of a PDF, joining all pages.

    Raises :class:`ExtractionError` when the file cannot be read — callers
    record the failure and move on; one bad PDF must never sink a run.
    """
    pdf_path = Path(path)
    try:
        reader = PdfReader(pdf_path)
        if reader.is_encrypted:
            # Planning PDFs are sometimes trivially "encrypted" with an empty
            # owner password; try that before giving up.
            reader.decrypt("")
        parts = [page.extract_text() or "" for page in reader.pages]
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(f"could not read {pdf_path.name}: {exc}") from exc
    return ExtractedText(pages=len(reader.pages), text="\n".join(parts).strip())
