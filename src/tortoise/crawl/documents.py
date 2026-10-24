"""Choosing and downloading application documents.

The plan caps each application at three *text* documents — the application
form, a design and access statement, a planning statement — and explicitly
skips drawings and scans. Selection scores document names against priority
keywords, drops obvious drawing sets, and takes the top three. Downloads go
over plain HTTP (not the browser) so they cost no browser minutes.
"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request
from pathlib import Path

from .schemas import DocumentLink

# Which documents Tortoise wants, in priority order (first match wins the
# ranking). These are the documents that actually say what is proposed.
_PRIORITY_KEYWORDS: tuple[tuple[str, ...], ...] = (
    ("application form", "planning application form", "householder"),
    ("design and access", "design & access", "design statement"),
    ("planning statement", "supporting statement", "planning, design and access"),
    ("heritage", "statement of significance"),
    ("transport", "parking", "highway"),
    ("decision notice", "officer report", "delegated report"),
)

# Names that signal drawings rather than text; never worth downloading.
_EXCLUDE_KEYWORDS = (
    "drawing",
    "site plan",
    "location plan",
    "block plan",
    "floor plan",
    "elevation",
    "elevations",
    "section",
    "roof plan",
    "street view",
    "aerial",
    "photograph",
    "photo",
)

_PDF_MAGIC = b"%PDF"
_MAX_BYTES = 30 * 1024 * 1024  # 30 MB — planning PDFs are rarely larger
_USER_AGENT = "Mozilla/5.0 (Tortoise planning watcher; polite; contact via repo)"


def _score(name: str) -> int | None:
    """Rank a document name, or None when it should never be fetched.

    Extension is deliberately not checked: Idox document links usually carry
    no filename suffix, so the decision is made on the name and the download
    verifies PDF magic bytes.
    """
    lowered = (name or "").lower()
    if any(keyword in lowered for keyword in _EXCLUDE_KEYWORDS):
        return None
    for rank, keywords in enumerate(_PRIORITY_KEYWORDS):
        if any(keyword in lowered for keyword in keywords):
            return rank
    return len(_PRIORITY_KEYWORDS)  # unrecognised text documents rank last


def select_documents(links: list[DocumentLink], limit: int = 3) -> list[DocumentLink]:
    """The documents worth downloading: text-like, prioritised, at most ``limit``."""
    candidates: list[tuple[int, int, DocumentLink]] = []
    for index, link in enumerate(links):
        if not link.url:
            continue
        score = _score(link.name or url_filename(link.url))
        if score is None:
            continue
        candidates.append((score, index, link))
    candidates.sort(key=lambda item: (item[0], item[1]))
    return [link for _, _, link in candidates[:limit]]


def url_filename(url: str) -> str:
    """A readable filename for a document URL."""
    from urllib.parse import parse_qs, urlparse

    path = urlparse(url).path
    name = Path(path).name
    if not name or not Path(name).suffix:
        # Idox document links carry no filename; try a query parameter.
        query = parse_qs(urlparse(url).query)
        for key in ("filename", "docname", "name"):
            if key in query and query[key]:
                supplied = Path(query[key][0]).name
                return supplied if Path(supplied).suffix else f"{supplied}.pdf"
        return "document.pdf"
    return name


def safe_filename(name: str) -> str:
    """Make a document name safe as a file on disk."""
    cleaned = "".join(ch if ch.isalnum() or ch in "-_. " else "_" for ch in name).strip()
    return cleaned or "document.pdf"


def download_pdf(url: str, dest_dir: Path, filename: str, timeout: float = 90.0) -> Path:
    """Download one PDF over plain HTTP; no browser minutes spent.

    Verifies the response actually is a PDF (magic bytes) and enforces a size
    cap. Raises :class:`DownloadError` with a message safe to log.
    """
    request = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/pdf,*/*"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(_MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise DownloadError(f"HTTP {exc.code} fetching {filename}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DownloadError(f"could not fetch {filename}: {exc}") from exc

    if len(data) > _MAX_BYTES:
        raise DownloadError(f"{filename} exceeds the {_MAX_BYTES // (1024 * 1024)} MB cap")
    if not data.startswith(_PDF_MAGIC):
        raise DownloadError(f"{filename} is not a PDF (wrong content type at source)")

    dest_dir.mkdir(parents=True, exist_ok=True)
    destination = dest_dir / filename
    destination.write_bytes(data)
    return destination


class DownloadError(Exception):
    """A document could not be downloaded; the run continues without it."""


async def download_pdf_async(url: str, dest_dir: Path, filename: str, timeout: float = 90.0) -> Path:
    """Async wrapper around :func:`download_pdf` (urllib is blocking)."""
    return await asyncio.to_thread(download_pdf, url, dest_dir, filename, timeout)
