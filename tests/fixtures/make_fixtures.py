"""Generate the small PDF fixtures used by the extraction tests.

Run from the repo root:  python tests/fixtures/make_fixtures.py

These are hand-built minimal PDFs (no external dependencies) so the offline
test suite can exercise text extraction, multi-page counting, and scanned-PDF
detection without shipping real planning documents.
"""

from pathlib import Path

HERE = Path(__file__).parent


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build_pdf(page_texts: list[str]) -> bytes:
    """Build a minimal valid PDF with one page per entry in page_texts.

    An empty string produces a page with no text operators — the fixture
    stand-in for a scanned or drawing-only PDF.
    """
    count = len(page_texts)
    font_id = 3 + 2 * count
    objects: dict[int, bytes] = {}

    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(count))
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode()

    for i, text in enumerate(page_texts):
        page_id = 3 + 2 * i
        content_id = 4 + 2 * i
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {content_id} 0 R /Resources << /Font << /F1 {font_id} 0 R >> >> >>"
        ).encode()
        if text:
            stream = f"BT /F1 12 Tf 72 720 Td ({_escape(text)}) Tj ET".encode()
        else:
            stream = b""
        objects[content_id] = (
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )

    objects[font_id] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for object_id in sorted(objects):
        offsets[object_id] = len(out)
        out += f"{object_id} 0 obj\n".encode()
        out += objects[object_id]
        out += b"\nendobj\n"

    xref_offset = len(out)
    total = len(objects) + 1
    out += f"xref\n0 {total}\n".encode()
    out += b"0000000000 65535 f \n"
    for object_id in sorted(objects):
        out += f"{offsets[object_id]:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {total} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode()
    )
    return bytes(out)


APPLICATION_FORM_TEXT = (
    "APPLICATION FORM FOR HOUSEHOLDER PLANNING PERMISSION. "
    "Proposal: erection of a two-storey rear extension at 14a Mill Road. "
    "The extension would extend 4.5 metres beyond the rear wall of the original "
    "dwelling, with a maximum height of 3.2 metres at the eaves. "
    "Materials: facing brickwork to match the existing dwelling and slate tiles "
    "to the roof. Parking arrangements would remain unchanged."
)

DESIGN_STATEMENT_TEXT = (
    "DESIGN AND ACCESS STATEMENT. The proposed rear extension responds to the "
    "pattern of neighbouring properties on Mill Road, several of which have "
    "similar extensions. The scale and massing respect the terrace line, and "
    "the facing materials match the host dwelling. Access and parking remain "
    "as existing; no changes to the highway are proposed."
)


def main() -> None:
    (HERE / "application_form.pdf").write_bytes(build_pdf([APPLICATION_FORM_TEXT, APPLICATION_FORM_TEXT]))
    (HERE / "design_statement.pdf").write_bytes(build_pdf([DESIGN_STATEMENT_TEXT]))
    (HERE / "scanned.pdf").write_bytes(build_pdf([""]))
    print("fixtures written to", HERE)


if __name__ == "__main__":
    main()
