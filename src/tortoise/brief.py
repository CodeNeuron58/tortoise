"""The one paid step: Ultramarine reads the kept applications' documents and
writes the weekly brief.

Only the applications Gemma kept reach this module, and every token is
counted and logged. Error model (mirrors the plan's failure test 3):

* ``BriefAuthError`` — bad key, missing credit, wrong model name. NOT
  retried; the email says the brief was skipped.
* ``BriefError`` — rate limits, server errors, network trouble. Retried by
  the Temporal activity with backoff; after the limit the email says the
  brief was skipped.

The whole request is kept under about 60,000 tokens (the model takes
131,072) by trimming document texts to a per-document budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)

from .cost import compute_cost, estimate_tokens, usage_from_response
from .models import Application, ExtractedDocument, TokenUsage

# Keep the whole request under ~60k tokens; the model takes 131,072.
MAX_INPUT_TOKENS = 60_000

# Characters per token used to convert the token budget into a character
# budget. Conservative on purpose.
_CHARS_PER_TOKEN = 4

_TRUNCATION_MARKER = "...[truncated to fit the budget]"

_PROMPT_TEMPLATE = """\
You write a weekly planning brief for residents of {zone}. For each \
application give: what is proposed, in one line; why a neighbour might care, \
using only facts from the council's listing and the documents below; the \
comment deadline; and the link. Write "not stated" when a fact is missing; \
never guess. Never name applicants, agents or any other private individual. \
Plain English, under 400 words. End with: "Nothing else this week needs your \
attention."

Here are the applications, each with the council's own listing and the text \
of its documents:

{applications}
"""


class BriefAuthError(Exception):
    """Bad API key, missing credit, or wrong model name — not worth retrying."""


class BriefError(Exception):
    """Transient failure (rate limit, server error, network) — worth retrying."""


@dataclass(slots=True)
class BriefItem:
    """One application plus its readable documents, ready for the prompt."""

    application: Application
    documents: list[ExtractedDocument] = field(default_factory=list)
    filter_one_line: str = ""
    filter_type: str = ""

    @property
    def readable_documents(self) -> list[ExtractedDocument]:
        """Documents with real text — scans and drawings are excluded."""
        return [d for d in self.documents if not d.is_scanned]


def _deadline_text(application: Application) -> str:
    if application.comment_deadline:
        return application.comment_deadline.strftime("%d %B %Y")
    return "not stated"


def budget_document_texts(
    texts: list[str],
    max_input_tokens: int = MAX_INPUT_TOKENS,
    overhead_tokens: int = 2_000,
) -> list[str]:
    """Trim document texts so the whole prompt stays inside the token budget.

    Every document keeps an equal share; a document shorter than its share is
    untouched, a longer one is truncated (never dropped — the model needs all
    applications represented). Pure and deterministic, so tests can pin it.
    """
    if not texts:
        return []
    char_budget = max(0, (max_input_tokens - overhead_tokens) * _CHARS_PER_TOKEN)
    share = char_budget // len(texts)
    result: list[str] = []
    for text in texts:
        if len(text) <= share:
            result.append(text)
        else:
            result.append(text[:share] + _TRUNCATION_MARKER)
    return result


def build_prompt(zone_label: str, items: list[BriefItem]) -> str:
    """Assemble the full user prompt from the plan's template plus the material."""
    blocks: list[str] = []
    for item in items:
        app = item.application
        lines = [
            f"## Application {app.reference}",
            f"Address: {app.address}",
            f"Proposal (council listing): {app.proposal or 'not stated'}",
            f"Validated: {app.validated_on.strftime('%d %B %Y') if app.validated_on else 'not stated'}",
            f"Comment deadline: {_deadline_text(app)}",
            f"Link: {app.url}",
        ]
        if item.filter_type:
            lines.append(f"Labelled by the local filter as: {item.filter_type}")
        if item.filter_one_line:
            lines.append(f"Local filter summary: {item.filter_one_line}")
        readable = item.readable_documents
        if readable:
            lines.append("Documents:")
            for document in readable:
                lines.append(f"--- {document.filename} ---")
                lines.append(document.text.strip() or "(no extractable text)")
        elif item.documents:
            lines.append("Documents: drawings or scans only, no readable text")
        else:
            lines.append("Documents: none downloaded; rely on the council listing above")
        blocks.append("\n".join(lines))

    applications_text = "\n\n".join(blocks) if blocks else "(no applications this week)"
    return _PROMPT_TEMPLATE.format(zone=zone_label, applications=applications_text)


@dataclass(slots=True, frozen=True)
class BriefResult:
    """The brief and exactly what it cost."""

    text: str
    usage: TokenUsage
    cost_usd: Decimal

    @property
    def cost_usd_float(self) -> float:
        return float(self.cost_usd)


class BriefWriter:
    """One call per run against Ultramarine 1 Flash."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 300.0,
    ) -> None:
        self.model = model
        # max_retries=0: the Temporal activity owns retries so they appear in
        # the workflow history.
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=0)

    async def write_brief(self, zone_label: str, items: list[BriefItem]) -> BriefResult:
        prompt = build_prompt(zone_label, items)
        if estimate_tokens(prompt) > MAX_INPUT_TOKENS:
            raise BriefError("brief input exceeds the token budget even after trimming")

        try:
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                # Ultramarine-1-flash thinks before answering and thinking
                # tokens come out of this budget; with many applications the
                # reasoning alone can exceed a small cap (verified live: the
                # brief came back empty at 3000 tokens with 17 apps).
                max_tokens=8000,
            )
        except (AuthenticationError, PermissionDeniedError, NotFoundError) as exc:
            raise BriefAuthError(f"Ultramarine rejected the credentials or model: {exc}") from exc
        except (RateLimitError, APIConnectionError, APITimeoutError) as exc:
            raise BriefError(f"Ultramarine is temporarily unavailable: {exc}") from exc
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status in (401, 402, 403, 404):
                raise BriefAuthError(f"Ultramarine rejected the request: {exc}") from exc
            raise BriefError(f"Ultramarine call failed: {exc}") from exc

        try:
            text = (response.choices[0].message.content or "").strip()
        except (IndexError, AttributeError) as exc:
            raise BriefError("Ultramarine returned a malformed response") from exc
        if not text:
            raise BriefError("Ultramarine returned an empty brief")

        usage = usage_from_response(response)
        return BriefResult(text=text, usage=usage, cost_usd=compute_cost(usage))


__all__ = [
    "MAX_INPUT_TOKENS",
    "BriefAuthError",
    "BriefError",
    "BriefItem",
    "BriefResult",
    "BriefWriter",
    "budget_document_texts",
    "build_prompt",
]
