"""The tier each prompt-cache write was granted at reaches core where core reads it.

On this route Claude Code places the cache markers and picks their tier; Hermes sends none (core's
``anthropic_prompt_cache_policy`` is ``(False, False)`` for the ``process://`` URL). The API reports the tier it
granted in ``usage.cache_creation`` (``ephemeral_5m_input_tokens`` / ``ephemeral_1h_input_tokens``) and core reads
that split from a response's top-level ``usage.cache_creation`` (``usage_pricing.cache_write_tiers``). This route kept
it only inside ``native_usage``, so with no marker and no split core's prompt-cache clock filed the route as OpenAI
spec and assumed a 30-minute window (``'30m'``, ``declared``) whatever native was granted.

Each test drives the fake CLI and reads the usage Hermes receives through core's own normalizer.
"""
import json
import os
import sys
from types import SimpleNamespace

import pytest

from conftest import PLUGIN_NAME
from test_directsdk import FAKE

UNSPLIT = "'output_tokens_details':{'thinking_tokens':4}}"
SPLIT = "'output_tokens_details':{'thinking_tokens':4},**json.loads(os.environ['NATIVE_USAGE'])}"
TOOLS = [{"type": "function", "function": {"name": "probe", "description": "TAIL",
                                           "parameters": {"type": "object", "properties": {}}}}]


def _usage(tmp_path, native_usage, *, stream=False, **extras):
    """The ``usage`` of the final response, for a native that reports ``native_usage`` on top of its counters."""
    import directsdk

    assert UNSPLIT in FAKE
    script = tmp_path / "native.py"
    script.write_text(FAKE.replace(UNSPLIT, SPLIT))
    client = directsdk.Client(command=[sys.executable, str(script)],
                              env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "NATIVE_USAGE": json.dumps(native_usage)})
    try:
        result = client.create(model="sonnet", messages=[{"role": "user", "content": "go"}], tools=TOOLS, stream=stream,
                               **extras)
        if stream:
            return [c.usage for c in result if c.usage is not None][-1]
        return result.usage
    finally:
        client.close()


def _granted(usage):
    from agent.usage_pricing import normalize_usage
    canonical = normalize_usage(usage, provider=PLUGIN_NAME, api_mode="chat_completions")
    assert (canonical.input_tokens, canonical.cache_read_tokens, canonical.cache_write_tokens) == (3, 7, 11)
    return canonical.cache_write_by_ttl


@pytest.mark.parametrize("stream", [False, True])
def test_the_granted_tier_reaches_core(tmp_path, stream):
    usage = _usage(tmp_path, {"cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 11}},
                   stream=stream)
    assert _granted(usage) == (("1h", 11),)
    assert vars(usage.cache_creation) == {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 11}


@pytest.mark.parametrize("split, tiers", [
    ({"ephemeral_5m_input_tokens": 11}, (("5m", 11),)),
    ({"ephemeral_1h_input_tokens": 11}, (("1h", 11),)),
    ({"ephemeral_5m_input_tokens": 4, "ephemeral_1h_input_tokens": 7}, (("5m", 4), ("1h", 7))),
])
def test_each_reported_tier_reaches_core_as_counted(tmp_path, split, tiers):
    # One tier, the other, or a write split across both: core gets the counts as reported.
    assert _granted(_usage(tmp_path, {"cache_creation": split})) == tiers


@pytest.mark.parametrize("split, tier, seconds, confidence", [
    ({"ephemeral_5m_input_tokens": 11, "ephemeral_1h_input_tokens": 0}, "5m", 300, "proven"),
    ({"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 11}, "1h", 3600, "proven"),
    (None, "30m", 1800, "declared"),  # native reports no split: core's OpenAI-spec assumption, as before
])
def test_the_clock_holds_the_tier_native_was_granted(profile, tmp_path, split, tier, seconds, confidence):
    """Hermes sends this route no cache marker (a temporary HERMES_HOME, the real route ceiling and route store), so
    the clock files it as OpenAI spec; before the split reached core every write was the 30m assumption."""
    import agent.prompt_cache_clock as clock
    from agent.agent_runtime_helpers import anthropic_prompt_cache_policy
    from agent.usage_pricing import normalize_usage

    # The private attributes are ones core reads off a real agent here: the clock treats its store as already restored,
    # there is no session DB, and the cache policy finds no custom providers. A core rename would need them updated.
    agent = SimpleNamespace(provider=PLUGIN_NAME, base_url=profile.base_url, model="sonnet",
                            api_mode="chat_completions", session_id="s1", _session_db=None, _custom_providers=None,
                            _prompt_cache_clock_restored=True, context_compressor=SimpleNamespace(compression_count=0))
    assert anthropic_prompt_cache_policy(agent) == (False, False)  # core adds no marker on this route
    clock.record_attempt_sent(agent, {"model": "sonnet", "tools": TOOLS, "messages": [
        {"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]})
    usage = _usage(tmp_path, {} if split is None else {"cache_creation": split})
    event = clock.record_cache_response(agent, normalize_usage(usage, provider=agent.provider, api_mode=agent.api_mode),
                                        model=agent.model, provider=agent.provider, base_url=agent.base_url,
                                        api_mode=agent.api_mode)
    assert (event.route_class, event.requested_ttl, event.tier, event.confidence) == (
        "openai_spec", None, tier, confidence)
    assert event.guaranteed_until - event.sent_at == pytest.approx(seconds)


def test_a_split_native_does_not_report_stays_unknown(tmp_path):
    usage = _usage(tmp_path, {})
    assert not hasattr(usage, "cache_creation")
    assert _granted(usage) == ()  # unreported, never a counted zero


@pytest.mark.parametrize("creation", [
    "1h", [11], None, {},
    {"ephemeral_5m_input_tokens": True, "ephemeral_1h_input_tokens": -1},
    {"ephemeral_1h_input_tokens": "11"}, {"ephemeral_1h_input_tokens": 11.0}, {"ephemeral_1h_input_tokens": None},
])
@pytest.mark.parametrize("stream", [False, True])
def test_a_malformed_split_is_unknown_and_never_fails_the_request(tmp_path, creation, stream):
    usage = _usage(tmp_path, {"cache_creation": creation}, stream=stream)
    assert not hasattr(usage, "cache_creation")
    assert _granted(usage) == ()


def test_only_the_two_counts_are_carried(tmp_path):
    usage = _usage(tmp_path, {"cache_creation": {"ephemeral_5m_input_tokens": 4, "ephemeral_1h_input_tokens": "x",
                                                 "note": "prompt text"}})
    assert vars(usage.cache_creation) == {"ephemeral_5m_input_tokens": 4}
    assert _granted(usage) == (("5m", 4),)
