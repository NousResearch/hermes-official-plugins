"""Hermes tool names round-trip through native's shorter MCP namespace."""
import copy
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import directsdk
import test_directsdk


LONG_NAME = "mcp__server__" + "x" * 51
ALIAS = LONG_NAME[:41] + "_" + hashlib.sha256(LONG_NAME.encode()).hexdigest()[:8]


def tool(name):
    return {"type": "function", "function": {"name": name}}


@pytest.mark.parametrize("name", ["x" * 51, LONG_NAME])
def test_long_name_has_deterministic_50_character_alias(name):
    alias = name[:41] + "_" + hashlib.sha256(name.encode()).hexdigest()[:8]
    request = {"tools": [tool(name)]}
    first = directsdk.request_body(request)
    second = directsdk.request_body(request)
    assert first == second
    encoded, manifest, names = first
    assert len(alias) == 50
    assert json.loads(encoded)["tools"][0]["name"] == directsdk.PREFIX + alias
    assert len(json.loads(encoded)["tools"][0]["name"]) <= 64
    assert manifest[0]["name"] == alias
    assert names == {name: alias}
    assert name in names


def test_short_names_preserve_literal_wire_manifest_and_history():
    request = {"max_tokens": 17, "tools": [tool("probe"), tool("A_1-z"),
                                              tool("x" * 50)]}
    encoded, manifest, names = directsdk.request_body(request)
    assert encoded == (
        '{"max_tokens":17,"tools":['
        '{"name":"mcp__hermes__probe","description":"","input_schema":{"type":"object","properties":{}}},'
        '{"name":"mcp__hermes__A_1-z","description":"","input_schema":{"type":"object","properties":{}}},'
        '{"name":"mcp__hermes__xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx","description":"","input_schema":{"type":"object","properties":{}}}]}'
    )
    assert json.dumps(manifest) == (
        '[{"name": "probe", "description": "", "inputSchema": {"type": "object", "properties": {}}}, '
        '{"name": "A_1-z", "description": "", "inputSchema": {"type": "object", "properties": {}}}, '
        '{"name": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "description": "", "inputSchema": {"type": "object", "properties": {}}}]'
    )
    history = [{"role": "user", "content": "go"},
               {"role": "assistant", "content": "Calling.", "tool_calls": [
                   {"id": "t1", "function": {"name": "probe", "arguments": "{}"}}]},
               {"role": "tool", "tool_call_id": "t1", "content": "ok"}]
    assert json.dumps(directsdk.prepare_history(history, names), separators=(",", ":")) == (
        '["",[{"type":"user","message":{"role":"user","content":[{"type":"text","text":"go"}]}},'
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Calling."},'
        '{"type":"tool_use","id":"t1","name":"mcp__hermes__probe","input":{}}]}},'
        '{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"t1","content":"ok"}]}}]]'
    )


@pytest.mark.parametrize("name", ["x" * 65, "has.dot", "", None, 123, []])
def test_invalid_names_are_rejected(name):
    with pytest.raises(ValueError, match="at most 64 characters"):
        directsdk.request_body({"tools": [tool(name)]})


def test_duplicate_names_are_rejected():
    with pytest.raises(ValueError, match="at most 64 characters"):
        directsdk.request_body({"tools": [tool("probe"), tool("probe")]})


@pytest.mark.parametrize("names", [[LONG_NAME, ALIAS], [ALIAS, LONG_NAME]])
def test_alias_collision_with_short_name_is_rejected(names):
    with pytest.raises(ValueError, match="collision") as raised:
        directsdk.request_body({"tools": [tool(name) for name in names]})
    assert ALIAS in str(raised.value)
    assert LONG_NAME in str(raised.value)


def test_alias_collision_with_another_alias_is_rejected(monkeypatch):
    class SameDigest:
        def hexdigest(self):
            return "0" * 64

    monkeypatch.setattr(hashlib, "sha256", lambda _: SameDigest())
    with pytest.raises(ValueError, match="collision"):
        directsdk.request_body({"tools": [tool("x" * 64), tool("x" * 63 + "y")]})


def test_bridge_wraps_only_a_tool_it_can_run():
    # A typo core's auto-repair maps (`Read_File` -> read_file) must reach core unwrapped, and its invalid-name limit too.
    names = {"read_file": "read_file", "tool_call": "tool_call"}
    with test_directsdk.deferred_tool("mcp__woz__Edit"):
        assert directsdk.bridge_tool("mcp__woz__Edit", names) == "tool_call"
        assert directsdk.bridge_tool("Read_File", names) is None


def test_history_replays_offered_long_name_and_preserves_unoffered_name():
    _, _, names = directsdk.request_body({"tools": [tool(LONG_NAME)]})
    history = [{"role": "user", "content": "go"},
               {"role": "assistant", "content": None, "tool_calls": [
                   {"id": "t1", "function": {"name": LONG_NAME, "arguments": "{}"}},
                   {"id": "t2", "function": {"name": "unoffered", "arguments": "{}"}}]},
               {"role": "tool", "tool_call_id": "t1", "content": "ok"}]
    blocks = directsdk.prepare_history(history, names)[1][1]["message"]["content"]
    assert [block["name"] for block in blocks] == [directsdk.PREFIX + ALIAS, "unoffered"]


@pytest.mark.parametrize("bare", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
def test_native_alias_maps_back_and_carrier_restores_on_second_request(tmp_path, bare, streaming):
    fixture = test_directsdk.Contract()
    client = fixture.client(str(tmp_path), TOOL_NAME=ALIAS if bare else directsdk.PREFIX + ALIAS,
                            REPLAY_NAME=directsdk.PREFIX + ALIAS)
    request = fixture.request()
    request["tools"][0]["function"]["name"] = LONG_NAME
    try:
        result = client.create(**request, stream=streaming)
        if streaming:
            chunks = list(result)
            assert chunks[-1].choices[0].delta.tool_calls[0].function.name == LONG_NAME
            message = chunks[-1]._response.choices[0].message.model_dump()
        else:
            message = result.choices[0].message.model_dump()
        assert message["tool_calls"][0]["function"]["name"] == LONG_NAME
        carrier = message["reasoning_details"][0]
        assert carrier["projection"] == directsdk.projection(message)
        assert carrier["messages"][0]["content"][-1]["name"] == directsdk.PREFIX + ALIAS
        message["content"] = message["content"].strip()
        request["messages"] += [message, {"role": "tool", "tool_call_id": "toolu_test", "content": "ok"}]
        original = copy.deepcopy(request["messages"])
        _, _, names = directsdk.request_body(request)
        replay = directsdk.prepare_history(request["messages"], names)[1][1]["message"]
        assert replay == carrier["messages"][0]
        assert replay["content"][0]["type"] == "thinking"
        # The existing fake asserts exact signed blocks and REPLAY_NAME on request #2.
        assert client.create(**request).choices[0].message.content == "done"
        assert request["messages"] == original
        # A host edit drops the carrier and still rebuilds the alias correctly.
        message["content"] = "middleware changed"
        rebuilt = directsdk.prepare_history(request["messages"], names)[1][1]["message"]["content"]
        assert [block["type"] for block in rebuilt] == ["text", "tool_use"]
        assert rebuilt[-1]["name"] == directsdk.PREFIX + ALIAS
        assert client.create(**request).choices[0].message.content == "done"
    finally:
        client.close()
