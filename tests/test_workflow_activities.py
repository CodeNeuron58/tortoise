"""Offline tests for activity wiring, using Temporal's ActivityEnvironment.

These prove the failure-test flags do what the plan's failure tests need,
without a Temporal server: TORTOISE_TEST_BRIEF_FAIL forces a retryable brief
failure, TORTOISE_TEST_OLLAMA_DOWN points the filter at a dead port, and the
run dedup guard flips only when a run is recorded as completed.
"""

import asyncio

import pytest
from temporalio.testing import ActivityEnvironment

import tortoise.workflow.activities as acts
from tortoise.models import RunNumbers

CONFIG_TOML = """
[[council]]
slug = "west-suffolk"
name = "West Suffolk Council"
mode = "weekly_list_page"
weekly_list_url = "https://example.gov.uk/list"

[council.zone]
label = "test zone"
streets = ["Mill Road"]
postcode_sectors = ["IP33 1"]
"""


@pytest.fixture()
def runtime(tmp_path, monkeypatch):
    config_path = tmp_path / "councils.toml"
    config_path.write_text(CONFIG_TOML, encoding="utf-8")
    monkeypatch.setenv("TORTOISE_CONFIG", str(config_path))
    monkeypatch.setenv("TORTOISE_DATA_DIR", str(tmp_path / "data"))
    fresh = acts._Runtime()
    monkeypatch.setattr(acts, "_RUNTIME", fresh)
    return fresh


def test_run_dedup_guard_roundtrip(runtime):
    env = ActivityEnvironment()
    assert asyncio.run(env.run(acts.check_not_already_run, ("west-suffolk", "2026-W43"))) is False
    asyncio.run(
        env.run(acts.record_run, RunNumbers(council="west-suffolk", week="2026-W43", status="running"))
    )
    assert asyncio.run(env.run(acts.check_not_already_run, ("west-suffolk", "2026-W43"))) is False
    asyncio.run(
        env.run(
            acts.record_run,
            RunNumbers(council="west-suffolk", week="2026-W43", status="completed"),
        )
    )
    assert asyncio.run(env.run(acts.check_not_already_run, ("west-suffolk", "2026-W43"))) is True


def test_get_council_settings_returns_zone(runtime):
    env = ActivityEnvironment()
    settings = asyncio.run(env.run(acts.get_council_settings, "west-suffolk"))
    assert settings.zone_label == "test zone"
    assert settings.streets == ["Mill Road"]
    assert settings.postcode_sectors == ["IP33 1"]


def test_unknown_council_fails_the_activity(runtime):
    env = ActivityEnvironment()
    with pytest.raises(Exception, match="unknown council"):
        asyncio.run(env.run(acts.get_council_settings, "nowhere"))


def test_brief_fail_flag_forces_retryable_failure(runtime, monkeypatch):
    monkeypatch.setenv("TORTOISE_TEST_BRIEF_FAIL", "1")
    env = ActivityEnvironment()
    params = acts.BriefParams(council="west-suffolk", week="2026-W43", kept_references=[])
    with pytest.raises(acts.BriefTestFailure):
        asyncio.run(env.run(acts.write_brief, params))


def test_ollama_down_flag_points_at_a_dead_port(runtime, monkeypatch):
    from tortoise.gemma import FilterUnavailableError
    from tortoise.models import Application

    # The filter loop only runs over stored applications, so seed one.
    config, db = runtime.get()
    db.upsert_application(
        Application(
            reference="26/0001/FUL",
            council="west-suffolk",
            address="14a Mill Rd",
            proposal="Two-storey rear extension",
            url="https://example.gov.uk/app/1",
        ),
        week="2026-W43",
    )

    monkeypatch.setenv("TORTOISE_TEST_OLLAMA_DOWN", "1")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://should-not-be-used:11434/v1")
    monkeypatch.setenv("GEMMA_MODEL", "gemma4:e4b")
    env = ActivityEnvironment()
    params = acts.FilterParams(council="west-suffolk", week="2026-W43")
    with pytest.raises(FilterUnavailableError):
        asyncio.run(env.run(acts.gemma_filter, params))


def test_iso_week_label_format():
    from tortoise.workflow.starter import iso_week_for_today

    label = iso_week_for_today()
    assert label.startswith("20")
    assert "-W" in label
    assert len(label) == 8  # 2026-W43
