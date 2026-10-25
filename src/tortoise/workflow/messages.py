"""Flat messages crossing the Temporal activity boundary.

Everything here is primitives only (str/int/float/bool/None), so the default
JSON payload converter round-trips them without ceremony. Domain objects
convert to these at the edges of each activity.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import RunNumbers


@dataclass(slots=True, frozen=True)
class WeeklyWatchInput:
    """Workflow argument: which council, which week, which browser."""

    council: str
    week: str | None = None  # ISO week like "2026-W43"; computed when None
    local_browser: bool = False  # develop on local Chrome, spend no Browserbase quota


@dataclass(slots=True, frozen=True)
class CouncilSettings:
    """Config values the workflow needs, loaded by an activity (fresh each run)."""

    slug: str
    name: str
    zone_label: str
    streets: list[str] = field(default_factory=list)
    postcode_sectors: list[str] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class ApplicationRow:
    """One application as it travels between activities."""

    reference: str
    council: str
    address: str
    proposal: str
    url: str
    validated_on: str = ""
    comment_deadline: str = ""
    postcode: str = ""


@dataclass(slots=True, frozen=True)
class CrawlListResult:
    applications: list[ApplicationRow] = field(default_factory=list)
    pages_read: int = 0
    browser_seconds: float = 0.0


@dataclass(slots=True, frozen=True)
class FetchResult:
    reference: str
    comment_deadline: str = ""
    documents: int = 0
    pages_read: int = 0
    browser_seconds: float = 0.0


@dataclass(slots=True, frozen=True)
class FilterSummary:
    kept_references: list[str] = field(default_factory=list)
    unfiltered: bool = False
    gemma_seconds: float = 0.0


@dataclass(slots=True, frozen=True)
class BriefOutcome:
    text: str | None
    model: str = ""
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass(slots=True, frozen=True)
class EmailResult:
    subject: str
    sent: bool = True


# --- activity parameters -----------------------------------------------------


def rebuild(cls, payload):
    """Rebuild a message dataclass from whatever the payload converter gave.

    Temporal sometimes delivers activity results as plain dicts (the workflow
    sandbox's payload conversion); this walks the known message types and
    rebuilds them. Pass already-typed values through untouched.
    """
    import dataclasses
    import typing

    def build(target_cls, value):
        if value is None or not dataclasses.is_dataclass(target_cls):
            return value
        if isinstance(value, target_cls):
            return value
        if not isinstance(value, dict):
            return value
        # get_type_hints resolves the string annotations that
        # `from __future__ import annotations` leaves on the fields.
        hints = typing.get_type_hints(target_cls)
        kwargs = {}
        for name, field_type in hints.items():
            if name not in value:
                continue
            inner = value[name]
            origin = getattr(field_type, "__origin__", None)
            if origin is list and isinstance(inner, list):
                item_type = getattr(field_type, "__args__", (None,))[0]
                kwargs[name] = [build(item_type, item) for item in inner]
            elif dataclasses.is_dataclass(field_type):
                kwargs[name] = build(field_type, inner)
            else:
                kwargs[name] = inner
        return target_cls(**kwargs)

    return build(cls, payload)


@dataclass(slots=True, frozen=True)
class CrawlListParams:
    council: str
    week: str
    local_browser: bool = False


@dataclass(slots=True, frozen=True)
class FetchParams:
    council: str
    week: str
    reference: str
    local_browser: bool = False


@dataclass(slots=True, frozen=True)
class FilterParams:
    council: str
    week: str


@dataclass(slots=True, frozen=True)
class BriefParams:
    council: str
    week: str
    kept_references: list[str]


@dataclass(slots=True, frozen=True)
class EmailParams:
    council: str
    week: str
    zone_label: str
    numbers: RunNumbers
    brief_text: str | None
    earliest_deadline: str | None = None
