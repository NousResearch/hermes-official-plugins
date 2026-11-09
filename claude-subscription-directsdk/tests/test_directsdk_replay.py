"""Host transforms and streaming deadlines must not strand native sessions."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location("directsdk_replay", ROOT / "directsdk.py")
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)


def test_host_history_edits_replay_canonical_visible_blocks():
    msg = {"role": "assistant", "content": "Writing.", "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "x", "content": "a" * 3000})}}]}
    blocks = [{"type": "thinking", "thinking": "private", "signature": "opaque"}, {"type": "text", "text": "Writing."}, {"type": "tool_use", "id": "t1", "name": native.PREFIX + "write_file", "input": {"path": "x", "content": "a" * 3000}}]
    msg["reasoning_details"] = [{"type": native.CARRIER, "version": 1, "projection": native.projection(msg), "messages": [{"role": "assistant", "content": blocks}]}]
    history = [{"role": "user", "content": "write"}, msg, {"role": "tool", "tool_call_id": "t1", "content": "ok"}]
    original = copy.deepcopy(history)
    assert native.prepare_history(history)[1][1]["message"]["content"] == blocks
    # Hermes compaction keeps tool-call arguments byte-exact and only demotes tool-result bodies, so the
    # signed native blocks must survive it verbatim.
    history[2]["content"] = "[tool result demoted]"
    assert native.prepare_history(history)[1][1]["message"]["content"] == blocks
    # Hooks still own visible assistant content: an edit must drop the stale signed thinking and replay
    # canonical text and tool_use built from the host message.
    history[1]["content"] = "Hook replacement"
    frame = native.prepare_history(history)[1][1]["message"]
    assert [b["type"] for b in frame["content"]] == ["text", "tool_use"]
    assert frame["content"][0]["text"] == "Hook replacement"
    assert frame["content"][-1]["input"] == json.loads(history[1]["tool_calls"][0]["function"]["arguments"])
    assert history[1]["reasoning_details"] == original[1]["reasoning_details"]
    history[1]["reasoning_details"][0]["version"] = 999
    with pytest.raises(ValueError, match="version"):
        native.prepare_history(history)


def test_edited_history_replays_a_call_outside_the_inventory_by_its_own_name():
    # Hermes answered a directly-called deferred tool with its unknown-tool error (#39); after a
    # host edit, rebuilt history must not rename it into the mcp__hermes__ namespace.
    calls = [{"id": "t1", "type": "function", "function": {"name": "mcp__fastmail__draft_email", "arguments": "{}"}},
             {"id": "t2", "type": "function", "function": {"name": "write_file", "arguments": "{}"}}]
    history = [{"role": "user", "content": "draft"}, {"role": "assistant", "content": "Drafting.", "tool_calls": calls},
               {"role": "tool", "tool_call_id": "t1", "content": "unknown"}, {"role": "tool", "tool_call_id": "t2", "content": "ok"}]
    blocks = native.prepare_history(history, {"write_file"})[1][1]["message"]["content"]
    assert [b["name"] for b in blocks if b["type"] == "tool_use"] == ["mcp__fastmail__draft_email", native.PREFIX + "write_file"]


def test_active_stream_outlives_idle_budget_and_large_request_uses_files(tmp_path):
    script = tmp_path / "native.py"
    script.write_text('''import json, os, pathlib, sys, time
settings = json.loads(pathlib.Path(sys.argv[sys.argv.index('--settings')+1]).read_text())
body = json.loads(settings['env']['CLAUDE_CODE_EXTRA_BODY'])
assert len(body['tools'][0]['description']) == 150000
assert 'temperature' not in body and 'top_p' not in body
assert body['output_config']['effort'] == 'low'
assert sys.argv[sys.argv.index('--effort')+1] == 'low' and 'CLAUDE_CODE_EFFORT_LEVEL' not in os.environ
assert len(pathlib.Path(sys.argv[sys.argv.index('--system-prompt-file')+1]).read_text()) == 150000
for line in sys.stdin:
 row=json.loads(line)
 if row['type']=='user' and row.get('shouldQuery', True): break
for i in range(8):
 print(json.dumps({'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'text_delta','text':'x'}}}),flush=True)
 time.sleep(.1)
print(json.dumps({'type':'assistant','message':{'role':'assistant','content':[{'type':'text','text':'x'*8}],'id':'m','stop_reason':'end_turn'}}),flush=True)
print(json.dumps({'type':'stream_event','event':{'type':'message_stop'}}),flush=True)
print(json.dumps({'type':'result','subtype':'success','usage':{'input_tokens':1,'output_tokens':8}}),flush=True)
''')
    client = native.Client(command=[sys.executable, str(script)], env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "CLAUDE_CODE_EFFORT_LEVEL": "max"})
    request = dict(model="sonnet", messages=[{"role": "system", "content": "s" * 150000}, {"role": "user", "content": "go"}], tools=[{"type": "function", "function": {"name": "probe", "description": "d" * 150000}}], temperature=.3, top_p=.9, extra_body={"reasoning": {"effort": "minimal"}}, timeout=SimpleNamespace(read=.5), stream=True)
    try:
        chunks = list(client.create(**request))
        assert "".join(c.choices[0].delta.content or "" for c in chunks) == "x" * 8
        script.write_text("import time\ntime.sleep(60)\n")
        with pytest.raises(TimeoutError):
            list(client.create(**request))
        assert not client._requests
    finally:
        client.close()


def test_replayed_carrier_drops_native_usage_so_compression_does_not_trip_the_local_size_gate():
    """#92: native gates each request locally on the newest assistant message's `usage`. A replayed
    carrier kept the usage of the request that produced it, so right after Hermes compressed the history
    native still saw the pre-compression size and refused with "Prompt is too long" without sending.
    `usage` is local metadata (never part of the wire request), so the replay drops it."""
    usage = {"input_tokens": 158258, "output_tokens": 900, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    msg = {"role": "assistant", "content": "Done."}
    natives = [{"id": "msg_1", "role": "assistant", "content": [{"type": "text", "text": "Done."}], "usage": usage, "stop_reason": "end_turn"}]
    msg["reasoning_details"] = [{"type": native.CARRIER, "version": 1, "projection": native.projection(msg), "messages": natives}]
    history = [{"role": "user", "content": "go"}, msg, {"role": "user", "content": "STILL IN PROGRESS"}]
    original = copy.deepcopy(history)
    frame = native.prepare_history(history)[1][1]["message"]
    assert "usage" not in frame
    assert frame == {k: v for k, v in natives[0].items() if k != "usage"}
    assert history == original  # the stored carrier keeps its usage; only the replayed copy drops it
