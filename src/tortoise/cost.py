"""Token-cost arithmetic for Ultramarine, exactly as the build plan specifies.

The plan prices Ultramarine 1 Flash at $0.15 per million input tokens,
$0.03 cached, and $0.50 per million output tokens, with the run cost as::

    cost = (0.15*n_in + 0.03*n_cached + 0.50*n_out) / 1_000_000

where ``n_in`` is the *uncached* input. APIs report ``prompt_tokens`` with
the cached tokens included, so they are subtracted before pricing — a
cached token is billed once, at the cached rate.

Everything uses :class:`decimal.Decimal` so a hand-worked example matches to
the last digit. Token counts come from each API response's usage object; the
counts and the computed dollars are logged per call into the ``costs`` table.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from .models import TokenUsage


@dataclass(slots=True, frozen=True)
class ModelPrice:
    """USD per million tokens."""

    input: Decimal
    cached_input: Decimal
    output: Decimal


# Ultramarine 1 Flash pricing, per the build plan (confirm against the docs
# before the first real run — the plan says the same).
ULTRAMARINE_PRICE = ModelPrice(
    input=Decimal("0.15"),
    cached_input=Decimal("0.03"),
    output=Decimal("0.50"),
)


def compute_cost(
    usage: TokenUsage,
    price: ModelPrice = ULTRAMARINE_PRICE,
) -> Decimal:
    """The dollar cost of one API call, as Decimal USD.

    ``usage.input_tokens`` is the API's prompt count, cached tokens included.
    """
    million = Decimal(1_000_000)
    uncached = max(0, usage.input_tokens - usage.cached_tokens)
    return (
        price.input * uncached
        + price.cached_input * usage.cached_tokens
        + price.output * usage.output_tokens
    ) / million


def usage_from_response(response) -> TokenUsage:
    """Pull token counts out of an OpenAI-compatible chat completion response.

    Field names follow the OpenAI SDK: ``usage.prompt_tokens``,
    ``usage.prompt_tokens_details.cached_tokens``, ``usage.completion_tokens``.
    The plan says to confirm the exact field names in the docs; this is the
    one place to change if Ultramarine differs.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return TokenUsage()
    input_tokens = getattr(usage, "prompt_tokens", 0) or 0
    output_tokens = getattr(usage, "completion_tokens", 0) or 0
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", 0) or 0 if details is not None else 0
    return TokenUsage(
        input_tokens=int(input_tokens),
        cached_tokens=int(cached),
        output_tokens=int(output_tokens),
    )


def estimate_tokens(text: str) -> int:
    """A conservative character-based token estimate (~4 characters per token).

    Used only for budgeting the brief's input before sending it; the exact
    counts come back from the API response.
    """
    return math.ceil(len(text or "") / 4)
