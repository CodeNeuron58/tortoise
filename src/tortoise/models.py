"""Core domain types shared by every part of Tortoise.

These dataclasses are the vocabulary of the project: crawlers produce
:class:`Application` rows, the zone filter decides which are worth reading,
Gemma labels them, and the Temporal workflow threads them through activities.
Everything here is plain data with no I/O so it can be imported anywhere,
including inside Temporal workflow code (which must be deterministic).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

# Date formats UK council sites actually emit, most specific first.
_DATE_FORMATS = (
    "%d %B %Y",  # 14 October 2026
    "%d %b %Y",  # 14 Oct 2026
    "%Y-%m-%d",  # 2026-10-14
    "%d/%m/%Y",  # 14/10/2026
    "%d-%m-%Y",  # 14-10-2026
)

_WEEKDAY_PREFIX = re.compile(
    r"^(mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s+", re.IGNORECASE
)


def parse_uk_date(raw: str | None) -> date | None:
    """Parse a date as UK council sites write it, or ``None`` if unparseable.

    Councils are inconsistent, so this deliberately fails soft: an
    unparseable date is stored as absent rather than crashing a run.
    """
    if not raw:
        return None
    text = raw.strip().rstrip(".")
    # Idox prefixes a weekday: "Sat 14 Nov 2026".
    text = _WEEKDAY_PREFIX.sub("", text)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


class CrawlMode(str, Enum):
    """How Tortoise reaches a council's weekly list."""

    # Idox Public Access "Weekly/Monthly Lists" search, driven step by step.
    IDOX_WEEKLY_LIST = "idox_weekly_list"
    # A plain page the council publishes with the week's applications on it.
    WEEKLY_LIST_PAGE = "weekly_list_page"


@dataclass(slots=True, frozen=True)
class WatchZone:
    """The streets and postcode sectors Tortoise cares about, per council.

    ``label`` is the human name used in the brief and the email subject,
    e.g. ``"Mill Road, Cambridge"``.
    """

    label: str
    streets: tuple[str, ...] = ()
    postcode_sectors: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class Council:
    """One council's site and watch zone.

    ``search_url`` (Idox mode) is the direct URL of the Weekly/Monthly Lists
    search. ``weekly_list_url`` (page mode) is the URL of the published
    weekly list page. Exactly one is required, by mode.
    """

    slug: str
    name: str
    mode: CrawlMode
    zone: WatchZone
    search_url: str = ""
    weekly_list_url: str = ""
    enabled: bool = True
    notes: str = ""

    def __post_init__(self) -> None:
        if self.mode is CrawlMode.IDOX_WEEKLY_LIST and not self.search_url:
            raise ValueError(f"council '{self.slug}': idox_weekly_list mode needs search_url")
        if self.mode is CrawlMode.WEEKLY_LIST_PAGE and not self.weekly_list_url:
            raise ValueError(f"council '{self.slug}': weekly_list_page mode needs weekly_list_url")


@dataclass(slots=True)
class Application:
    """A planning application as it appears in a weekly list."""

    reference: str
    council: str  # council slug
    address: str
    proposal: str
    url: str
    validated_on: date | None = None
    comment_deadline: date | None = None
    postcode: str = ""  # filled when the site exposes one separately


@dataclass(slots=True, frozen=True)
class DocumentLink:
    """A link to a document attached to an application."""

    name: str
    url: str


@dataclass(slots=True)
class ExtractedDocument:
    """A downloaded application document and the text pulled out of it."""

    council: str
    reference: str
    filename: str
    url: str
    path: str
    pages: int
    text: str

    @property
    def is_scanned(self) -> bool:
        """True when the PDF has (almost) no extractable text — a scan or drawing."""
        return len(self.text.strip()) < _MIN_TEXT_CHARS


# Below this many characters a PDF is treated as a scan or a drawing set and
# skipped, per the build plan ("skips drawings and scans").
_MIN_TEXT_CHARS = 200


@dataclass(slots=True, frozen=True)
class FilterResult:
    """Gemma's label for one application."""

    reference: str
    type: str
    street_impact: tuple[str, ...]
    relevance: int  # 0 elsewhere .. 3 on your street
    one_line: str
    model: str
    raw: str  # the exact model reply, for evidence

    @property
    def is_unfiltered(self) -> bool:
        return self.type == UNFILTERED_TYPE


# Sentinel type recorded when Ollama is unreachable and everything is kept
# "unfiltered" rather than silently dropped.
UNFILTERED_TYPE = "unfiltered"


@dataclass(slots=True, frozen=True)
class TokenUsage:
    """Token counts as reported by an OpenAI-compatible API response."""

    input_tokens: int = 0
    cached_tokens: int = 0  # subset of input_tokens, when reported
    output_tokens: int = 0


@dataclass(slots=True)
class RunNumbers:
    """The Lazy Payoff numbers for one run of one council."""

    council: str = ""
    week: str = ""
    status: str = "running"
    listed: int = 0
    kept: int = 0
    pdfs_read: int = 0
    pages_read: int = 0
    browser_seconds: float = 0.0
    gemma_seconds: float = 0.0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    brief_skipped_reason: str = ""
    unfiltered: bool = False

    @property
    def browser_minutes(self) -> float:
        return round(self.browser_seconds / 60.0, 2)

    def footer(self) -> str:
        """One-line footer for the email, per the build plan."""
        return (
            f"Listed {self.listed}, kept {self.kept}, PDFs read {self.pdfs_read}, "
            f"pages read {self.pages_read}, browser {self.browser_minutes} min, "
            f"Gemma {self.gemma_seconds:.0f}s, "
            f"Ultramarine {self.input_tokens + self.output_tokens} tokens, "
            f"cost ${self.cost_usd:.4f}."
        )
