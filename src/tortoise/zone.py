"""Street and postcode-sector matching against a watch zone.

This is the cheapest, most important filter in Tortoise: it runs in plain
Python on every application from the weekly list *before* any browser minute
or model token is spent on it. It must never crash and never miss an obvious
match, so everything here is pure string work with tolerant normalisation.

Matching rules
--------------
Postcodes: a configured sector like ``CB9 0`` matches any application
postcode starting with it, case-insensitively and ignoring spacing.
A configured district like ``CB9`` (no space) matches the outward code.

Streets: configured names match the application address case-insensitively,
ignoring punctuation, with common UK suffix abbreviations expanded on both
sides (``Mill Rd`` matches ``Mill Road``). A street must start at a word
boundary in the address.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import WatchZone

# Common UK street-suffix abbreviations, expanded to the canonical long form.
_SUFFIX_ABBREVIATIONS = {
    "RD": "ROAD",
    "ST": "STREET",
    "LN": "LANE",
    "AVE": "AVENUE",
    "DR": "DRIVE",
    "CL": "CLOSE",
    "GDNS": "GARDENS",
    "GRN": "GREEN",
    "PK": "PARK",
    "HWY": "HIGHWAY",
}

_NON_WORD = re.compile(r"[^A-Z0-9]+")
# A full UK postcode appearing inside free text, e.g. "CB9 0AA" or "CB90AA".
_POSTCODE_IN_TEXT = re.compile(
    r"\b([A-Z]{1,2}[0-9][A-Z0-9]?)\s*([0-9][A-Z]{2})\b"
)


@dataclass(slots=True)
class ZoneVerdict:
    """Why an application was kept or dropped, for logs and evidence."""

    matched: bool = False
    streets: list[str] = field(default_factory=list)
    postcode_sectors: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.matched


def _squash(raw: str) -> str:
    """Uppercase and remove everything that is not a letter or digit."""
    return _NON_WORD.sub("", (raw or "").upper())


def normalise_postcode(raw: str) -> str:
    """Uppercase, strip spacing, and re-space a full postcode (``cb90aa`` → ``CB9 0AA``).

    Partial codes (a district ``CB9`` or a sector ``CB9 0``) come back
    uppercased and squashed (``CB90``), which is the form matching uses.
    """
    squashed = _squash(raw)
    match = re.fullmatch(r"([A-Z]{1,2}[0-9][A-Z0-9]?)([0-9][A-Z]{2})", squashed)
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return squashed


def postcode_sector(postcode: str) -> str:
    """The sector of a full postcode: ``CB9 0AA`` → ``CB9 0``."""
    normalised = normalise_postcode(postcode)
    parts = normalised.split(" ")
    if len(parts) == 2:
        return f"{parts[0]} {parts[1][0]}"
    return normalised


def postcode_in_text(text: str) -> str:
    """The first full postcode found in an address line, or ``""``."""
    match = _POSTCODE_IN_TEXT.search((text or "").upper())
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return ""


def _canonical_text(text: str) -> str:
    """Normalise free text for street matching.

    Uppercase, strip punctuation, collapse spacing, and expand common
    suffix abbreviations in every token, so ``"14a Mill Rd."`` and
    ``"14A MILL ROAD"`` normalise identically. Both the configured streets
    and the address go through the same expansion, so a form on one side
    matches the same form on the other.
    """
    tokens = _NON_WORD.sub(" ", (text or "").upper()).split()
    return " ".join(_SUFFIX_ABBREVIATIONS.get(token, token) for token in tokens)


def matched_streets(address: str, streets: tuple[str, ...]) -> list[str]:
    """Configured street names that appear at a word boundary in the address."""
    normalised = f" {_canonical_text(address)} "
    hits: list[str] = []
    for street in streets:
        canonical = _canonical_text(street)
        if canonical and f" {canonical}" in normalised:
            hits.append(street)
    return hits


def matched_sectors(address: str, postcode: str, sectors: tuple[str, ...]) -> list[str]:
    """Configured postcode sectors/districts that the application falls in.

    The application's own postcode field is used when present, otherwise a
    postcode is looked for inside the address line. Comparison is done on
    squashed forms, so ``"CB9 0"`` matches ``"CB90AA"``.
    """
    if not sectors:
        return []
    candidates = [_squash(postcode), _squash(postcode_in_text(address))]
    candidates = [c for c in candidates if c]
    hits: list[str] = []
    for sector in sectors:
        target = _squash(sector)
        if not target:
            continue
        if any(candidate.startswith(target) for candidate in candidates):
            hits.append(sector)
    return hits


def in_zone(
    address: str,
    zone: WatchZone,
    postcode: str = "",
) -> ZoneVerdict:
    """Decide whether an application is inside the watch zone.

    Either a street match or a postcode-sector match keeps the application.
    """
    streets = matched_streets(address, zone.streets)
    sectors = matched_sectors(address, postcode, zone.postcode_sectors)
    return ZoneVerdict(
        matched=bool(streets or sectors),
        streets=streets,
        postcode_sectors=sectors,
    )
