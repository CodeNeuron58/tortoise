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
    # (0.15*1,000,000 + 0.03*200,000 + 0.50*100,000) / 1,000,000 = 0.206
    usage = TokenUsage(input_tokens=1_000_000, cached_tokens=200_000, output_tokens=100_000)
    assert compute_cost(usage) == Decimal("0.206")


def test_small_realistic_call():
    # 60k input, 2k cached, 900 out:
    # (0.15*60000 + 0.03*2000 + 0.50*900) / 1e6 = 0.009 + 0.00006 + 0.00045 = 0.00951
    usage = TokenUsage(input_tokens=60_000, cached_tokens=2_000, output_tokens=900)
    assert compute_cost(usage) == Decimal("0.00951")


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
