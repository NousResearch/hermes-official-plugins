"""Wire-boundary regression: foreign call IDs must be valid and stay paired.

Synthetic history only: no provider requests, session database, or private data.
"""
import copy
import importlib.util
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location("directsdk_foreign_ids", ROOT / "directsdk.py")
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)


def tool_history(ids):
    calls = [
        {"id": tid, "type": "function", "function": {
            "name": "read_file", "arguments": json.dumps({"marker": tid}),
        }}
        for tid in ids
    ]
    return [
        {"role": "user", "content": "Synthetic protocol test."},
        {"role": "assistant", "content": "Reading.", "tool_calls": calls},
        *[{"role": "tool", "tool_call_id": tid, "content": "Synthetic result."} for tid in ids],
    ]


def wire_calls_and_results(history):
    _, frames = native.prepare_history(history, {"read_file"})
    calls, results = [], []
    for frame in frames:
        for block in frame["message"]["content"]:
            if block["type"] == "tool_use":
                calls.append(block)
            elif block["type"] == "tool_result":
                results.append(block)
    return calls, results


def test_colon_id_is_valid_on_wire_and_result_stays_paired():
    history = tool_history(["terminal:42"])
    original = copy.deepcopy(history)
    calls, results = wire_calls_and_results(history)
    assert len(calls) == len(results) == 1
    assert re.fullmatch(r"[a-zA-Z0-9_-]+", calls[0]["id"])
    assert calls[0]["id"] == results[0]["tool_use_id"]
    assert calls[0]["input"] == {"marker": "terminal:42"}
    assert history == original


def test_valid_native_and_openai_ids_remain_unchanged():
    ids = ["toolu_01Abc-DEF_123", "call_sample_01"]
    calls, results = wire_calls_and_results(tool_history(ids))
    assert [call["id"] for call in calls] == ids
    assert [result["tool_use_id"] for result in results] == ids


def test_foreign_ids_never_merge_with_each_other_or_valid_ids():
    ids = ["terminal:42", "terminal/42", "terminal 42", "terminal_42", "ferramenta:ação"]
    calls, results = wire_calls_and_results(tool_history(ids))
    emitted = [call["id"] for call in calls]
    assert len(set(emitted)) == len(ids)
    assert all(re.fullmatch(r"[a-zA-Z0-9_-]+", tid) for tid in emitted)
    assert emitted == [result["tool_use_id"] for result in results]
    assert emitted[3] == "terminal_42"
    assert [call["input"]["marker"] for call in calls] == ids


def test_reserved_valid_ids_take_precedence_over_foreign_mapping():
    # Discover a candidate through the public interface, then supply it as a
    # valid native ID in the same history: validity must take precedence.
    first, _ = wire_calls_and_results(tool_history(["terminal:42"]))
    reserved = first[0]["id"]
    ids = ["terminal:42", reserved, reserved + "_1"]
    history = tool_history(ids)
    original = copy.deepcopy(history)
    calls, results = wire_calls_and_results(history)
    emitted = [call["id"] for call in calls]
    assert len(set(emitted)) == len(ids)
    assert emitted[1:] == ids[1:]
    assert emitted == [result["tool_use_id"] for result in results]
    assert history == original


def test_mapping_is_repeatable_and_independent_of_history_order():
    ids = ["read_file:7", "terminal:42", "read_file_7"]
    def mapping(order):
        calls, results = wire_calls_and_results(tool_history(order))
        assert [call["id"] for call in calls] == [result["tool_use_id"] for result in results]
        return {call["input"]["marker"]: call["id"] for call in calls}
    assert mapping(ids) == mapping(ids) == mapping(list(reversed(ids)))


def test_signed_native_replay_is_preserved_next_to_foreign_history():
    first, _ = wire_calls_and_results(tool_history(["terminal:42"]))
    reserved = first[0]["id"]
    blocks = [
        {"type": "thinking", "thinking": "Synthetic reasoning.", "signature": "synthetic-signature"},
        {"type": "text", "text": "Native reply."},
        {"type": "tool_use", "id": reserved, "name": "mcp__hermes__read_file", "input": {"marker": "native:keep"}},
    ]
    native_message = {
        "role": "assistant", "content": "Native reply.",
        "tool_calls": [{"id": reserved, "type": "function", "function": {
            "name": "read_file", "arguments": '{"marker":"native:keep"}',
        }}],
        "reasoning_details": [{
            "type": "claude-subscription-directsdk-experimental.native_assistant", "version": 1,
            "projection": {"content": "Native reply.", "tool_calls": [{
                "id": reserved, "name": "read_file", "input": {"marker": "native:keep"},
            }]},
            "messages": [{"role": "assistant", "content": blocks}],
        }],
    }
    history = tool_history(["terminal:42"]) + [
        native_message,
        {"role": "tool", "tool_call_id": reserved, "content": "Native result."},
    ]
    original = copy.deepcopy(history)
    _, frames = native.prepare_history(history, {"read_file"})
    assert frames[3]["message"]["content"] == blocks
    calls, results = wire_calls_and_results(history)
    assert len({call["id"] for call in calls}) == 2
    assert [call["id"] for call in calls] == [result["tool_use_id"] for result in results]
    assert history == original


def test_nested_tool_result_and_system_payloads_are_not_rewritten():
    history = [{"role": "system", "content": "Keep terminal:42 literal."}] + tool_history(["terminal:42"])
    history[-1]["content"] = [{"type": "text", "text": "ID terminal:42 is payload, not protocol."}]
    original = copy.deepcopy(history)
    system, frames = native.prepare_history(history, {"read_file"})
    assert system == "Keep terminal:42 literal."
    assert frames[-1]["message"]["content"][0]["content"] == history[-1]["content"]
    assert history == original
