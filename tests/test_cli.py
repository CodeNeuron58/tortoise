"""Tests for the command-line interface (argparse-level, offline)."""

import pytest

from tortoise.cli import _build_parser, main


def test_every_command_is_present():
    parser = _build_parser()
    choices = parser._subparsers._group_actions[0].choices
    assert set(choices) == {
        "init-db", "config-check", "worker", "run-once", "schedule", "runs", "email-preview",
    }


def test_run_once_defaults_to_local_chrome():
    args = _build_parser().parse_args(["run-once", "--council", "west-suffolk"])
    assert args.council == "west-suffolk"
    assert args.local_browser is True


def test_run_once_browserbase_flag_flips_the_default():
    args = _build_parser().parse_args(["run-once", "--council", "x", "--browserbase"])
    assert args.local_browser is False


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    assert "slow, steady" in capsys.readouterr().out


def test_unknown_council_fails_with_a_clear_error(capsys):
    assert main(["run-once", "--council", "nowhere"]) == 1
    assert "unknown council 'nowhere'" in capsys.readouterr().err


def test_schedule_requires_a_known_council(capsys):
    assert main(["schedule", "add", "--council", "nowhere"]) == 1
    assert "unknown council 'nowhere'" in capsys.readouterr().err


def test_disabled_council_cannot_run_or_be_scheduled(tmp_path, monkeypatch, capsys):
    config = tmp_path / "councils.toml"
    config.write_text(
        """
[[council]]
slug = "off"
name = "Off Council"
mode = "weekly_list_page"
weekly_list_url = "https://example.gov.uk/list"
enabled = false

[council.zone]
label = "zone"
streets = ["Mill Road"]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TORTOISE_CONFIG", str(config))
    assert main(["run-once", "--council", "off"]) == 1
    assert "council 'off' is disabled" in capsys.readouterr().err
    assert main(["schedule", "add", "--council", "off"]) == 1
    assert "council 'off' is disabled" in capsys.readouterr().err
