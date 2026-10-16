"""Every request of one conversation must start native with the same session id.

With feature flags on (the claude_code_telemetry default) native names a per-session scratchpad
directory in the environment message right after the first user turn. Each request is a new native
process, so a fresh session id per spawn changed that message on every call: the cached prefix
collapsed to tools, system and the first turn, and the rest of the conversation was written to the
cache again on every round (measured on Opus 5.5, Claude Code 2.1.285).
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location("directsdk_session_id", ROOT / "directsdk.py")
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)

FAKE = '''import json, os, pathlib, sys
args = sys.argv[1:]
session = args[args.index("--session-id") + 1] if "--session-id" in args else ""
pathlib.Path(os.path.join(os.environ["HOME"], "sessions.log")).open("a").write(session + "\\n")
for line in sys.stdin:
    frame = json.loads(line)
    if frame.get("shouldQuery") is False:
        print(json.dumps({"type": "result", "subtype": "success", "num_turns": 0, "is_error": False}), flush=True)
print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}], "id": "m", "stop_reason": "end_turn"}}), flush=True)
print(json.dumps({"type": "stream_event", "event": {"type": "message_stop"}}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "usage": {"input_tokens": 1, "output_tokens": 1}}), flush=True)
'''

TOOLS = [{"type": "function", "function": {"name": "probe", "description": "d"}}]


def request(messages):
    return dict(model="sonnet", messages=messages, tools=TOOLS, stream=True, timeout=SimpleNamespace(read=5))


def conversation(first):
    """Two rounds of one conversation: the opening request, then the same history plus a tool round."""
    opening = [{"role": "system", "content": "s"}, {"role": "user", "content": first}]
    later = opening + [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "probe", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
    ]
    return opening, later


def client(tmp_path):
    script = tmp_path / "native.py"
    script.write_text(FAKE)
    return native.Client(command=[sys.executable, str(script)], env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)})


def sessions(tmp_path):
    return (tmp_path / "sessions.log").read_text().splitlines()


def test_one_conversation_keeps_one_native_session_across_rounds_and_clients(tmp_path):
    opening, later = conversation("go")
    first, rebuilt = client(tmp_path), client(tmp_path)
    try:
        list(first.create(**request(opening)))
        list(first.create(**request(later)))
        list(rebuilt.create(**request(later)))
    finally:
        first.close()
        rebuilt.close()
    ids = sessions(tmp_path)
    assert len(ids) == 3 and len(set(ids)) == 1, ids
    assert uuid.UUID(ids[0]).version == 4


def test_different_conversations_get_different_native_sessions(tmp_path):
    one, other = client(tmp_path), client(tmp_path)
    try:
        list(one.create(**request(conversation("go")[0])))
        list(other.create(**request(conversation("something else")[0])))
    finally:
        one.close()
        other.close()
    ids = sessions(tmp_path)
    assert len(ids) == 2 and ids[0] != ids[1], ids
