"""Reasoning tokens are part of Hermes' ``output_tokens``; Langfuse sums every usage key.

``CanonicalUsage.total_tokens`` is ``prompt_tokens + output_tokens``: reasoning is already inside
``output_tokens``. Langfuse treats each ``usage_details`` key as its own bucket (the total is the sum
of all keys, and the cost of each key is priced separately), so exporting reasoning next to the full
output counts it twice. When Langfuse ingests OpenAI usage it reports ``output_reasoning_tokens`` and
subtracts it from ``output``; the exported buckets must follow the same split.
"""
from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace

import pytest


def _fresh_plugin():
    sys.modules.pop("langfuse_plugin", None)
    return importlib.import_module("langfuse_plugin")


def _summary(*, input_tokens=1000, output_tokens=500, cache_read=20000, cache_write=0, reasoning=300):
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write, "reasoning_tokens": reasoning, "request_count": 1}


def _export(mod, usage, *, provider="anthropic", model="claude-sonnet-4-6", base_url="https://api.anthropic.com"):
    return mod._usage_and_cost(None, provider=provider, model=model, base_url=base_url, usage=usage)


def test_exported_buckets_sum_to_the_canonical_total():
    mod = _fresh_plugin()
    usage_details, _ = _export(mod, _summary())

    # 1000 input + 20000 cache read + 500 output (300 of it reasoning) = 21500, not 21800.
    assert sum(usage_details.values()) == 21500
    assert usage_details["output"] == 200
    assert usage_details["output_reasoning_tokens"] == 300
    assert "reasoning_tokens" not in usage_details


def test_no_reasoning_keeps_output_whole():
    mod = _fresh_plugin()
    usage_details, _ = _export(mod, _summary(reasoning=0))

    assert usage_details["output"] == 500
    assert "output_reasoning_tokens" not in usage_details


def test_reasoning_above_output_is_clamped():
    """A provider that reports more reasoning than output must not create tokens."""
    mod = _fresh_plugin()
    usage_details, _ = _export(mod, _summary(output_tokens=100, reasoning=250))

    assert usage_details["output"] == 0
    assert usage_details["output_reasoning_tokens"] == 100
    assert sum(usage_details.values()) == 1000 + 20000 + 100


def test_cost_breakdown_still_adds_up_to_the_total():
    mod = _fresh_plugin()
    _, cost_details = _export(mod, _summary(cache_write=3000))

    assert cost_details, "anthropic pricing should produce a cost"
    components = {k: v for k, v in cost_details.items() if k != "total"}
    assert cost_details["total"] == pytest.approx(sum(components.values()))
    # Reasoning is billed at the output rate, in its own bucket.
    assert cost_details["output_reasoning_tokens"] == pytest.approx(cost_details["output"] * 300 / 200)


def test_response_object_path_splits_reasoning_too():
    """post_llm_call hands a response with ``.usage`` (Codex Responses shape here)."""
    mod = _fresh_plugin()
    usage = SimpleNamespace(input_tokens=21000, output_tokens=500, total_tokens=21500,
                            input_tokens_details=SimpleNamespace(cached_tokens=20000),
                            output_tokens_details=SimpleNamespace(reasoning_tokens=300))
    usage_details, _ = mod._usage_and_cost(SimpleNamespace(usage=usage), provider="openai-codex",
                                           api_mode="codex_responses", model="gpt-5.5",
                                           base_url="https://chatgpt.com/backend-api/codex")

    assert sum(usage_details.values()) == 21500
    assert usage_details["output_reasoning_tokens"] == 300
