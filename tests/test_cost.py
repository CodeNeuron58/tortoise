"""Tests for cost arithmetic, including the plan's hand-worked example."""

from decimal import Decimal

from tortoise.cost import (
    ULTRAMARINE_PRICE,
    compute_cost,
    estimate_tokens,
    usage_from_response,
)
from tortoise.models import TokenUsage


class _Details:
    def __init__(self, cached_tokens):
        self.cached_tokens = cached_tokens


class _Usage:
    def __init__(self, prompt, cached, completion):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.prompt_tokens_details = _Details(cached)


class _Response:
    def __init__(self, usage):
        self.usage = usage


def test_hand_worked_example_from_the_plan():
    # The plan's formula, with n_in the uncached input:
    # (0.15*800,000 + 0.03*200,000 + 0.50*100,000) / 1,000,000 = 0.176
    # The API reports 1,000,000 prompt tokens, 200,000 of them cached.
    usage = TokenUsage(input_tokens=1_000_000, cached_tokens=200_000, output_tokens=100_000)
    assert compute_cost(usage) == Decimal("0.176")


def test_cached_tokens_are_billed_once():
    # A real brief call: 4187 prompt tokens, 4096 served from cache, 4194 out.
    # (0.15*91 + 0.03*4096 + 0.50*4194) / 1e6 = 0.00223353
    usage = TokenUsage(input_tokens=4187, cached_tokens=4096, output_tokens=4194)
    assert compute_cost(usage) == Decimal("0.00223353")


def test_no_cache_is_the_plain_formula():
    # (0.15*4187 + 0.50*2852) / 1e6 = 0.00205405 (the 2026-W44 brief)
    usage = TokenUsage(input_tokens=4187, cached_tokens=0, output_tokens=2852)
    assert compute_cost(usage) == Decimal("0.00205405")


def test_empty_usage_costs_nothing():
    assert compute_cost(TokenUsage()) == Decimal("0")


def test_usage_from_openai_style_response():
    response = _Response(_Usage(prompt=1234, cached=100, completion=567))
    usage = usage_from_response(response)
    assert usage == TokenUsage(input_tokens=1234, cached_tokens=100, output_tokens=567)


def test_usage_from_response_without_details():
    class _UsageNoDetails:
        prompt_tokens = 10
        completion_tokens = 5

    usage = usage_from_response(_Response(_UsageNoDetails()))
    assert usage == TokenUsage(input_tokens=10, cached_tokens=0, output_tokens=5)


def test_pricing_matches_the_plan():
    assert ULTRAMARINE_PRICE.input == Decimal("0.15")
    assert ULTRAMARINE_PRICE.cached_input == Decimal("0.03")
    assert ULTRAMARINE_PRICE.output == Decimal("0.50")


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 400) == 100
    assert estimate_tokens("abc") == 1  # rounds up, never to zero
