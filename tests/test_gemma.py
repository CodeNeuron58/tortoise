"""Tests for Gemma reply parsing (offline — no Ollama needed)."""

import pytest

from tortoise.gemma import FilterParseError, build_prompt, parse_filter_reply
from tortoise.models import Application, WatchZone

APP = Application(
    reference="26/01234/FUL",
    council="west-suffolk",
    address="14a Mill Rd",
    proposal="Two-storey rear extension",
    url="https://example.gov.uk/app/1",
)

ZONE = WatchZone(label="zone", streets=("Mill Road",), postcode_sectors=("IP33 1",))

GOOD = '{"type": "extension", "street_impact": ["parking", "noise"], "relevance": 2, "one_line": "Two-storey rear extension at No. 14"}'


def test_parse_good_reply():
    result = parse_filter_reply(GOOD, APP.reference, "gemma4:e4b")
    assert result.type == "extension"
    assert result.street_impact == ("parking", "noise")
    assert result.relevance == 2
    assert result.one_line.startswith("Two-storey rear extension")
    assert result.model == "gemma4:e4b"
    assert result.raw == GOOD


def test_parse_reply_in_markdown_fences():
    fenced = f"```json\n{GOOD}\n```"
    result = parse_filter_reply(fenced, APP.reference, "gemma4:e4b")
    assert result.relevance == 2


def test_parse_reply_with_chatter_around_json():
    chatty = f"Here is the label you asked for:\n{GOOD}\nHope that helps!"
    result = parse_filter_reply(chatty, APP.reference, "gemma4:e4b")
    assert result.type == "extension"


def test_unknown_type_becomes_other():
    weird = GOOD.replace('"extension"', '"moonbase"')
    result = parse_filter_reply(weird, APP.reference, "gemma4:e4b")
    assert result.type == "other"


def test_broken_reply_raises():
    with pytest.raises(FilterParseError):
        parse_filter_reply("not json at all", APP.reference, "gemma4:e4b")


def test_empty_reply_raises():
    with pytest.raises(FilterParseError):
        parse_filter_reply("", APP.reference, "gemma4:e4b")


def test_truncated_json_raises():
    truncated = '{"type": "extension", "street_impact": ["par'
    with pytest.raises(FilterParseError):
        parse_filter_reply(truncated, APP.reference, "gemma4:e4b")


def test_non_integer_relevance_raises():
    bad = GOOD.replace('"relevance": 2', '"relevance": "high"')
    with pytest.raises(FilterParseError):
        parse_filter_reply(bad, APP.reference, "gemma4:e4b")


def test_out_of_range_relevance_raises():
    bad = GOOD.replace('"relevance": 2', '"relevance": 9')
    with pytest.raises(FilterParseError):
        parse_filter_reply(bad, APP.reference, "gemma4:e4b")


def test_string_street_impact_is_accepted():
    single = GOOD.replace('["parking", "noise"]', '"parking"')
    result = parse_filter_reply(single, APP.reference, "gemma4:e4b")
    assert result.street_impact == ("parking",)


def test_prompt_names_the_zone_and_types():
    prompt = build_prompt(APP, ZONE)
    assert "26/01234/FUL" in prompt
    assert "14a Mill Rd" in prompt
    assert "Mill Road" in prompt
    assert "extension" in prompt  # the type list is spelled out
