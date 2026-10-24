"""Tests for street and postcode-sector matching (the cheap, offline filter)."""

from tortoise.models import WatchZone
from tortoise.zone import (
    in_zone,
    matched_sectors,
    matched_streets,
    normalise_postcode,
    postcode_in_text,
    postcode_sector,
)

ZONE = WatchZone(
    label="test zone",
    streets=("Mill Road", "High Street"),
    postcode_sectors=("CB9 0", "IP33"),
)


def test_normalise_postcode_full_codes():
    assert normalise_postcode("cb90aa") == "CB9 0AA"
    assert normalise_postcode(" CB9  0AA ") == "CB9 0AA"
    assert normalise_postcode("ip331jb") == "IP33 1JB"


def test_normalise_postcode_partials():
    # Partial codes are squashed (no re-spacing); matching uses squashed forms.
    assert normalise_postcode("CB9 0") == "CB90"
    assert normalise_postcode("cb9") == "CB9"


def test_postcode_sector_of_full_code():
    assert postcode_sector("CB9 0AA") == "CB9 0"
    assert postcode_sector("cb9 0aa") == "CB9 0"


def test_postcode_in_text():
    assert postcode_in_text("14a Mill Rd, Cambridge CB9 0AA") == "CB9 0AA"
    assert postcode_in_text("no postcode here") == ""


def test_street_match_with_abbreviation_and_case():
    # The plan's own example: "14a Mill Rd" must match a configured "Mill Road".
    assert matched_streets("14a Mill Rd, Cambridge", ZONE.streets) == ["Mill Road"]
    assert matched_streets("14A MILL ROAD", ZONE.streets) == ["Mill Road"]
    assert matched_streets("4 High St", ZONE.streets) == ["High Street"]


def test_street_match_requires_word_boundary():
    # "Millstone Road" must not match "Mill Road"-like prefixes... but more
    # importantly "Hamilton Road" must not contain "Mill Road", and a street
    # name embedded inside another word must not match.
    assert matched_streets("9 Millstone Row", ZONE.streets) == []
    assert matched_streets("2 Summit Road", ZONE.streets) == []


def test_sector_match_case_and_spacing_insensitive():
    assert matched_sectors("anywhere", "cb9 0aa", ZONE.postcode_sectors) == ["CB9 0"]
    assert matched_sectors("anywhere", "CB90AA", ZONE.postcode_sectors) == ["CB9 0"]
    # A configured district (no space) matches by outward-code prefix.
    assert matched_sectors("anywhere", "IP33 3RT", ZONE.postcode_sectors) == ["IP33"]


def test_sector_found_inside_address_text():
    assert matched_sectors("14a Mill Rd CB9 0AA", "", ZONE.postcode_sectors) == ["CB9 0"]


def test_no_sector_match_outside_zone():
    assert matched_sectors("anywhere", "LU1 4TT", ZONE.postcode_sectors) == []
    assert matched_sectors("anywhere", "CB9 9ZZ", ZONE.postcode_sectors) == []


def test_in_zone_matches_street_and_reports_why():
    verdict = in_zone("14a Mill Rd, Cambridge CB9 9ZZ", ZONE)
    assert verdict
    assert verdict.streets == ["Mill Road"]
    assert verdict.postcode_sectors == []


def test_in_zone_matches_sector_alone():
    assert in_zone("somewhere else", ZONE, postcode="IP33 1AB")


def test_in_zone_rejects_everything_outside():
    verdict = in_zone("99 Kingsway, Luton", ZONE, postcode="LU1 4TT")
    assert not verdict
    assert verdict.streets == []
    assert verdict.postcode_sectors == []


def test_in_zone_with_empty_zone_never_matches():
    empty = WatchZone(label="empty")
    assert not in_zone("14a Mill Rd", empty, postcode="CB9 0AA")
