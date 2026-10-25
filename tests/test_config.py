"""Tests for configuration loading."""

from pathlib import Path

import pytest

from tortoise.config import ConfigError, load_config
from tortoise.models import CrawlMode

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_load_shipped_council_config():
    config = load_config(REPO_ROOT / "config" / "councils.toml")
    slugs = [c.slug for c in config.councils]
    assert slugs == ["west-suffolk", "sevenoaks"]

    west_suffolk = config.council("west-suffolk")
    assert west_suffolk.mode is CrawlMode.IDOX_WEEKLY_LIST
    assert "search.do?action=weeklyList" in west_suffolk.search_url
    assert "York Road" in west_suffolk.zone.streets
    assert west_suffolk.zone.postcode_sectors == ("IP33 3",)

    sevenoaks = config.council("sevenoaks")
    assert sevenoaks.mode is CrawlMode.WEEKLY_LIST_PAGE
    assert sevenoaks.zone.postcode_sectors == ("TN13 1", "TN13 2")

    # Both portals' robots.txt say Disallow: /, so both ship switched off.
    assert config.enabled_councils() == []
    with pytest.raises(ConfigError, match="disabled"):
        config.enabled_council("west-suffolk")


def test_unknown_council_gives_clear_error():
    config = load_config(REPO_ROOT / "config" / "councils.toml")
    with pytest.raises(ConfigError, match="unknown council 'nowhere'"):
        config.council("nowhere")


def test_missing_zone_is_rejected(tmp_path):
    path = tmp_path / "councils.toml"
    path.write_text(
        """
[[council]]
slug = "broken"
name = "Broken Council"
mode = "weekly_list_page"
weekly_list_url = "https://example.gov.uk/list"

[council.zone]
label = "nowhere"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="at least one street"):
        load_config(path)


def test_duplicate_slugs_are_rejected(tmp_path):
    path = tmp_path / "councils.toml"
    council = """
[[council]]
slug = "same"
name = "Same Council"
mode = "weekly_list_page"
weekly_list_url = "https://example.gov.uk/list"

[council.zone]
label = "somewhere"
streets = ["High Street"]
"""
    path.write_text(council + council, encoding="utf-8")
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(path)


def test_idox_mode_requires_search_url(tmp_path):
    path = tmp_path / "councils.toml"
    path.write_text(
        """
[[council]]
slug = "idoxless"
name = "No URL Council"
mode = "idox_weekly_list"

[council.zone]
label = "somewhere"
streets = ["High Street"]
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="search_url"):
        load_config(path)
