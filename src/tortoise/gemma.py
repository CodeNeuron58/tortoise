"""The local, free filter: Gemma (via Ollama) labels every application.

Gemma reads every application that survived the zone filter and labels it
with a type, likely street impacts, a relevance score, and a one-line summary.
This happens locally and costs nothing, which is what keeps the paid model
(Ultramarine) fed with only a handful of documents per week.

Error model (mirrors the plan's failure tests):
* ``FilterUnavailableError`` — Ollama is unreachable. The Temporal activity
  retries (3 attempts); if it stays down the workflow keeps every application
  and marks the run "unfiltered" instead of silently dropping everything.
* ``FilterParseError`` — the model replied but not with usable JSON. The
  caller fails open: the application is kept and marked unfiltered.
"""

from __future__ import annotations

import json
from openai import AsyncOpenAI

from .models import Application, FilterResult, WatchZone

# The application types the plan's filter JSON allows.
APPLICATION_TYPES: tuple[str, ...] = (
    "extension",
    "new homes",
    "change of use",
    "demolition",
    "trees",
    "mast or antenna",
    "shopfront or advert",
    "other",
)

# relevance runs 0 (elsewhere) to 3 (on your street); Tortoise keeps 2 and 3.
RELEVANCE_KEEP_AT_OR_ABOVE = 2

_SYSTEM_PROMPT = (
    "You label UK planning applications for a residents' watch service. "
    "Reply with JSON only: no markdown fences, no explanation."
)

_USER_PROMPT_TEMPLATE = """\
Application reference: {reference}
Address: {address}
Proposal: {proposal}

The resident's watch zone covers these streets: {streets}
and these postcode sectors: {sectors}

Label this application as JSON with exactly these keys:
{{"type": one of {types}, "street_impact": [short impacts such as "parking", "noise", "daylight", "overshadowing", "trees"], "relevance": 0, 1, 2 or 3, "one_line": "one plain-English sentence describing what is proposed"}}

relevance: 3 = on a watched street or in a watched postcode sector, 2 = immediately
adjacent to the zone, 1 = same town but not near the zone, 0 = elsewhere.
"""


class FilterUnavailableError(Exception):
    """Ollama (or the configured filter endpoint) could not be reached."""


class FilterParseError(Exception):
    """The model replied, but the reply was not the JSON we asked for."""


def build_prompt(application: Application, zone: WatchZone) -> str:
    """The user prompt for one application; kept deterministic for tests."""
    return _USER_PROMPT_TEMPLATE.format(
        reference=application.reference,
        address=application.address,
        proposal=application.proposal or "(no proposal text)",
        streets=", ".join(zone.streets) if zone.streets else "(none)",
        sectors=", ".join(zone.postcode_sectors) if zone.postcode_sectors else "(none)",
        types=", ".join(APPLICATION_TYPES),
    )


def _extract_json_object(raw: str) -> dict:
    """Pull the first JSON object out of a model reply, fences and all."""
    text = (raw or "").strip()
    if not text:
        raise FilterParseError("empty reply")
    if text.startswith("```"):
        # Drop a leading ```json fence and a trailing fence line.
        lines = text.splitlines()
        lines = [line for line in lines if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise FilterParseError(f"no JSON object in reply: {text[:120]!r}") from None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise FilterParseError(f"unparseable JSON in reply: {exc}") from None
    if not isinstance(parsed, dict):
        raise FilterParseError("reply JSON is not an object")
    return parsed


def parse_filter_reply(raw: str, reference: str, model: str) -> FilterResult:
    """Turn a Gemma reply into a :class:`FilterResult`, strictly.

    A reply without usable JSON, or with a non-integer relevance, raises
    :class:`FilterParseError` so the caller can fail open. An unrecognised
    type is coerced to ``"other"`` rather than throwing — small models
    invent types often, and the type is descriptive, not a decision.
    """
    parsed = _extract_json_object(raw)

    relevance_raw = parsed.get("relevance")
    if not isinstance(relevance_raw, int) or isinstance(relevance_raw, bool):
        raise FilterParseError(f"relevance is not an integer: {relevance_raw!r}")
    if not 0 <= relevance_raw <= 3:
        raise FilterParseError(f"relevance out of range: {relevance_raw}")

    type_value = str(parsed.get("type", "other")).strip().lower()
    if type_value not in APPLICATION_TYPES:
        type_value = "other"

    street_impact_raw = parsed.get("street_impact", [])
    if isinstance(street_impact_raw, str):
        street_impact_raw = [street_impact_raw]
    if not isinstance(street_impact_raw, list):
        street_impact_raw = []
    street_impact = tuple(
        str(item).strip().lower() for item in street_impact_raw if str(item).strip()
    )

    one_line = str(parsed.get("one_line", "")).strip()

    return FilterResult(
        reference=reference,
        type=type_value,
        street_impact=street_impact,
        relevance=relevance_raw,
        one_line=one_line,
        model=model,
        raw=raw,
    )


class GemmaFilter:
    """One call per application against Ollama's OpenAI-compatible endpoint."""

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: float = 120.0,
    ) -> None:
        # max_retries=0: the Temporal activity owns retries so they show up
        # in the workflow history (and in the failure-test evidence).
        self.model = model
        self._client = AsyncOpenAI(base_url=base_url, api_key="ollama", timeout=timeout, max_retries=0)

    async def filter_application(self, application: Application, zone: WatchZone) -> FilterResult:
        try:
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": build_prompt(application, zone)},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            raise FilterUnavailableError(
                f"filter endpoint unreachable for {application.reference}: {exc}"
            ) from exc
        try:
            raw = response.choices[0].message.content or ""
        except (IndexError, AttributeError) as exc:
            raise FilterUnavailableError(f"malformed response for {application.reference}") from exc
        return parse_filter_reply(raw, application.reference, self.model)
