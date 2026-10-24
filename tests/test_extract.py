"""Tests for PDF text extraction, using the generated fixtures.

Regenerate with: python tests/fixtures/make_fixtures.py
"""

from pathlib import Path

import pytest

from tortoise.extract import ExtractedText, ExtractionError, extract_text

FIXTURES = Path(__file__).parent / "fixtures"


def test_extracts_text_and_counts_pages():
    result = extract_text(FIXTURES / "application_form.pdf")
    assert result.pages == 2
    assert "two-storey rear extension" in result.text
    assert "14a Mill Road" in result.text
    assert not result.is_scanned


def test_single_page_document():
    result = extract_text(FIXTURES / "design_statement.pdf")
    assert result.pages == 1
    assert "DESIGN AND ACCESS STATEMENT" in result.text


def test_scanned_pdf_comes_back_empty_and_is_flagged():
    result = extract_text(FIXTURES / "scanned.pdf")
    assert result.text == ""
    assert result.pages == 1
    assert result.is_scanned


def test_extracted_text_scan_threshold():
    assert ExtractedText(pages=1, text="x" * 199).is_scanned
    assert not ExtractedText(pages=1, text="x" * 200).is_scanned


def test_missing_file_raises_extraction_error(tmp_path):
    with pytest.raises(ExtractionError):
        extract_text(tmp_path / "nope.pdf")
