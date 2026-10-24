"""Configuration loading: councils from TOML, secrets and paths from the environment.

``config/councils.toml`` is the single file a user edits: which councils to
watch and what the watch zone is. Everything secret lives in ``.env`` and is
read from the environment at runtime; this module only raises clear errors —
it has no side effects, so importing it is safe everywhere.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .models import Council, CrawlMode, WatchZone

DEFAULT_CONFIG_PATH = Path("config/councils.toml")


class ConfigError(Exception):
    """The configuration is missing or wrong; the message says what to fix."""


@dataclass(slots=True, frozen=True)
class AppConfig:
    """The whole configuration: councils plus where runtime data lives."""

    data_dir: Path
    councils: tuple[Council, ...]

    def council(self, slug: str) -> Council:
        for council in self.councils:
            if council.slug == slug:
                return council
        known = ", ".join(c.slug for c in self.councils)
        raise ConfigError(f"unknown council '{slug}' (configured: {known})")

    def enabled_councils(self) -> list[Council]:
        return [c for c in self.councils if c.enabled]


def env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_required(name: str, what_for: str) -> str:
    value = env_str(name)
    if not value:
        raise ConfigError(
            f"missing environment variable {name} ({what_for}). "
            "Copy .env.example to .env and fill it in."
        )
    return value


def _parse_zone(table: dict, council_slug: str) -> WatchZone:
    zone_table = table.get("zone", {})
    label = str(zone_table.get("label", "")).strip()
    streets = tuple(str(s).strip() for s in zone_table.get("streets", []) if str(s).strip())
    sectors = tuple(
        str(s).strip() for s in zone_table.get("postcode_sectors", []) if str(s).strip()
    )
    if not label:
        raise ConfigError(f"council '{council_slug}': zone.label is required")
    if not streets and not sectors:
        raise ConfigError(
            f"council '{council_slug}': the watch zone needs at least one street "
            "or postcode sector in [council.zone]"
        )
    return WatchZone(label=label, streets=streets, postcode_sectors=sectors)


def _parse_council(table: dict) -> Council:
    slug = str(table.get("slug", "")).strip()
    if not slug:
        raise ConfigError("every [[council]] needs a slug")
    try:
        mode = CrawlMode(str(table.get("mode", "")).strip())
    except ValueError:
        valid = ", ".join(m.value for m in CrawlMode)
        raise ConfigError(
            f"council '{slug}': unknown mode '{table.get('mode')}' (valid: {valid})"
        ) from None
    try:
        council = Council(
            slug=slug,
            name=str(table.get("name", slug)).strip(),
            mode=mode,
            zone=_parse_zone(table, slug),
            search_url=str(table.get("search_url", "")).strip(),
            weekly_list_url=str(table.get("weekly_list_url", "")).strip(),
            enabled=bool(table.get("enabled", True)),
            notes=str(table.get("notes", "")).strip(),
        )
    except ValueError as exc:  # Council validates its mode/URL combination
        raise ConfigError(str(exc)) from None
    return council


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load the council configuration from TOML.

    The path is ``config/councils.toml`` by default, overridable with the
    ``TORTOISE_CONFIG`` environment variable.
    """
    config_path = Path(path or env_str("TORTOISE_CONFIG", str(DEFAULT_CONFIG_PATH)))
    if not config_path.is_file():
        raise ConfigError(
            f"config file not found: {config_path} (set TORTOISE_CONFIG or run from the repo root)"
        )
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_path} is not valid TOML: {exc}") from None

    storage = raw.get("storage", {})
    data_dir = Path(env_str("TORTOISE_DATA_DIR") or str(storage.get("data_dir", "data")))

    councils = [_parse_council(table) for table in raw.get("council", [])]
    slugs = [c.slug for c in councils]
    if not councils:
        raise ConfigError(f"{config_path} defines no [[council]] entries")
    duplicates = sorted({s for s in slugs if slugs.count(s) > 1})
    if duplicates:
        raise ConfigError(f"{config_path} defines duplicate council slugs: {duplicates}")

    return AppConfig(data_dir=data_dir, councils=tuple(councils))
