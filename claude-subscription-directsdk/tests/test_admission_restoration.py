"""Tool-result restoration accepts only lossless, unambiguous native additions."""
import copy
import http.client
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import sys
import threading

import pytest

from admission import Admission, QueriedTurnMismatch, restore_queried_turn
import directsdk

RESULT = {"type": "tool_result", "tool_use_id": "t1", "content": "output"}
ASSISTANT = {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "probe", "input": {}}]}


def strip(value):
    if isinstance(value, dict):
        return {k: strip(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [strip(v) for v in value]
    return value


def wire(content, before=None, after=None):
    messages = list(before or [ASSISTANT]) + [{"role": "user", "content": content}] + list(after or [])
    return json.dumps({"model": "fixture", "messages": messages}, separators=(",", ":")).encode()


@pytest.mark.parametrize("native_result", [
    {"type": "tool_result", "tool_use_id": "t1", "content": "output", "is_error": False},
    {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "output"}]},
    {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "output"}], "is_error": False},
])
def test_equivalent_plain_tool_result_shapes_restore_exact_host_shape(native_result):
    host = copy.deepcopy(RESULT)
    raw = wire([native_result])

    restored = json.loads(restore_queried_turn(raw, [host]))

    assert restored["messages"][1]["content"] == [host]


@pytest.mark.parametrize("invalid_flag", [0, None, "false"])
def test_invalid_error_flag_cannot_normalize_to_success(invalid_flag):
    with pytest.raises(QueriedTurnMismatch):
        restore_queried_turn(wire([{**RESULT, "is_error": invalid_flag}]), [RESULT])


def test_true_tool_error_does_not_match_absent_is_error():
    raw = wire([{**RESULT, "is_error": True}])

    with pytest.raises(QueriedTurnMismatch):
        restore_queried_turn(raw, [RESULT])


def test_nested_result_accepts_only_appended_unmarked_plain_text_and_restores_host():
    host = {**RESULT, "content": [{"type": "text", "text": "output"}]}
    native = {**host, "content": [*host["content"], {"type": "text", "text": "native note"}]}

    restored = json.loads(restore_queried_turn(wire([native]), [host]))

    assert restored["messages"][1]["content"] == [host]


@pytest.mark.parametrize("host_content", [
    [{"type": "text", "text": "output"},
     {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}}],
    [{"type": "text", "text": "output", "citations": [{"title": "host citation"}]}],
])
def test_structured_host_content_survives_native_trailing_text(host_content):
    host = {**RESULT, "content": copy.deepcopy(host_content)}
    native = {**host, "content": [*copy.deepcopy(host_content), {"type": "text", "text": "native note"}]}

    restored = json.loads(restore_queried_turn(wire([native]), [host]))

    assert restored["messages"][1]["content"] == [host]


@pytest.mark.parametrize("content", [
    [{"type": "text", "text": "out"}, {"type": "text", "text": "put"}],
    "output",
])
def test_split_host_text_restores_original_representation(content):
    host = {**RESULT, "content": content}
    native = {**RESULT, "content": [{"type": "text", "text": "o"},
                                   {"type": "text", "text": "utput native suffix"}]}
    assert json.loads(restore_queried_turn(wire([native]), [host]))["messages"][1]["content"] == [host]


def test_nested_cache_settings_are_retained_on_matched_block():
    marker = {"type": "ephemeral", "ttl": "1h"}
    host = {**RESULT, "content": [{"type": "text", "text": "output"}]}
    native = {**host, "content": [{"type": "text", "text": "output", "cache_control": marker},
                                 {"type": "text", "text": "native suffix"}]}
    restored = json.loads(restore_queried_turn(wire([native]), [host]))["messages"][1]["content"]
    assert restored == [{**host, "content": [{"type": "text", "text": "output", "cache_control": marker}]}]


def test_nested_host_cache_settings_survive_appended_plain_text():
    content = [{"type": "text", "text": "output", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
    host = {**RESULT, "content": content}
    native = {**host, "content": [*content, {"type": "text", "text": "native suffix"}]}
    assert json.loads(restore_queried_turn(wire([native]), [host]))["messages"][1]["content"] == [host]


@pytest.mark.parametrize("native_content", [
    [{"type": "text", "text": "out native insertion put"}],
    [{"type": "text", "text": "output", "unknown": "native metadata"}],
    [{"type": "text", "text": "output", "cache_control": {"type": "ephemeral"}}],
])
def test_protected_or_changed_text_cannot_be_flattened_into_host_string(native_content):
    with pytest.raises(QueriedTurnMismatch):
        restore_queried_turn(wire([{**RESULT, "content": native_content}]), [RESULT])


def test_native_text_inserted_before_nested_media_is_rejected():
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}}
    host = {**RESULT, "content": [{"type": "text", "text": "output"}, image]}
    native = {**host, "content": [{"type": "text", "text": "output native insertion"}, image]}
    with pytest.raises(QueriedTurnMismatch):
        restore_queried_turn(wire([native]), [host])


def test_nested_conflicting_cache_settings_are_rejected():
    host = {**RESULT, "content": [{"type": "text", "text": "output", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]}
    native = {**RESULT, "content": [{"type": "text", "text": "output", "cache_control": {"type": "ephemeral", "ttl": "5m"}}]}
    with pytest.raises(QueriedTurnMismatch):
        restore_queried_turn(wire([native]), [host])


def test_mixed_frame_restores_split_and_extended_plain_user_text():
    host = [RESULT, {"type": "text", "text": "user words"}]
    native = [RESULT, {"type": "text", "text": "user "},
              {"type": "text", "text": "words native note"}]

    restored = json.loads(restore_queried_turn(wire(native), host))

    assert restored["messages"][1]["content"] == host


@pytest.mark.parametrize("extra", [
    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}},
    {"type": "text", "text": "note", "cache_control": {"type": "ephemeral"}},
    {"type": "text", "text": "note", "citations": [{"type": "web_search_result_location"}]},
])
def test_nested_non_plain_or_metadata_text_is_not_discarded(extra):
    host = {**RESULT, "content": [{"type": "text", "text": "output"}]}
    native = {**host, "content": [*host["content"], extra]}

    with pytest.raises(QueriedTurnMismatch):
        restore_queried_turn(wire([native]), [host])


def test_appended_tool_result_is_rejected():
    extra = {"type": "tool_result", "tool_use_id": "unexpected", "content": "hidden"}

    with pytest.raises(QueriedTurnMismatch):
        restore_queried_turn(wire([RESULT, extra]), [RESULT])


def test_duplicate_queried_tool_ids_fail_closed():
    duplicate = {**RESULT, "content": "second output"}

    with pytest.raises(QueriedTurnMismatch):
        restore_queried_turn(wire([RESULT, duplicate]), [RESULT, duplicate])


def test_plain_user_frame_is_byte_identical_after_successful_match():
    question = {"type": "text", "text": "hello"}
    raw = wire([question])

    assert restore_queried_turn(raw, [question]) == raw


@pytest.mark.parametrize("queried,native", [
    ([RESULT], [RESULT, {"type": "text", "text": "note", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]),
    ([RESULT], [RESULT, {"type": "tool_result", "tool_use_id": "extra", "content": "extra"}]),
    ([RESULT], [RESULT, {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}}]),
    ([RESULT], [RESULT, {"type": "text", "text": "note", "citations": []}]),
    ([RESULT], [RESULT, {"type": "text", "text": "note", "unknown": True}]),
    ([RESULT], [{**RESULT, "is_error": True}]),
    ([RESULT], [{**RESULT, "is_error": 0}]),
    ([RESULT], [{**RESULT, "tool_use_id": "wrong"}]),
    ([RESULT], [{**RESULT, "content": "changed"}]),
    ([RESULT], [RESULT, RESULT]),
    ([RESULT], [{"type": "text", "text": "prepended"}, RESULT]),
    ([{**RESULT, "content": [{"type": "text", "text": "output"}]}],
     [{**RESULT, "content": [{"type": "text", "text": "output"}, {"type": "text", "text": "note", "cache_control": {"type": "ephemeral"}}]}]),
    ([{**RESULT, "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
     [{**RESULT, "cache_control": {"type": "ephemeral", "ttl": "5m"}}]),
])
def test_unprovable_restoration_forwards_the_native_payload_unchanged(queried, native):
    received = []

    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    peer = ThreadingHTTPServer(("127.0.0.1", 0), Peer)
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    gate = Admission(f"http://127.0.0.1:{peer.server_port}", 5, queried=queried)
    try:
        route = gate.url.removeprefix(f"http://127.0.0.1:{gate.server.server_port}") + "/v1/messages"
        conn = http.client.HTTPConnection("127.0.0.1", gate.server.server_port, timeout=5)
        conn.request("POST", route, wire(native), {"Content-Type": "application/json"})
        try:
            response = conn.getresponse()
            response.read()
        except http.client.RemoteDisconnected:
            pass
        finally:
            conn.close()
        assert gate.unrestored == "Native request does not uniquely match Hermes tool results"
        assert gate.failure is None
        # Nothing is dropped or rewritten: native's own frame goes upstream (only a marker may move).
        assert len(received) == 1
        assert strip(json.loads(received[0])) == strip(json.loads(wire(native)))
    finally:
        gate.close()
        peer.shutdown()
        thread.join()
        peer.server_close()


def test_wire_continuity_across_parallel_rounds_and_admission_recreation():
    """Fake native client: prove replay stability, not vendor cache savings."""
    received = []

    class Peer(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    peer = ThreadingHTTPServer(("127.0.0.1", 0), Peer)
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    history = [
        {"role": "user", "content": "earlier prompt"},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "signed", "signature": "sig"},
                                             {"type": "text", "text": "earlier answer"}]},
    ]
    try:
        for round_number in range(2):
            queried = [
                {"type": "tool_result", "tool_use_id": f"p{round_number}a", "content": "A"},
                {"type": "tool_result", "tool_use_id": f"p{round_number}b", "content": "B"},
            ]
            assistant = {"role": "assistant", "content": [
                {"type": "tool_use", "id": f"p{round_number}a", "name": "a", "input": {}},
                {"type": "tool_use", "id": f"p{round_number}b", "name": "b", "input": {}},
            ]}
            native_results = [dict(item) for item in queried]
            native_results.append({"type": "text", "text": "native date annotation"})
            gate = Admission(f"http://127.0.0.1:{peer.server_port}", 5, queried=queried)
            try:
                messages = [*history, assistant, {"role": "user", "content": native_results}]
                payload = json.dumps({"model": "fixture", "messages": messages}).encode()
                route = gate.url.removeprefix(f"http://127.0.0.1:{gate.server.server_port}") + "/v1/messages"
                conn = http.client.HTTPConnection("127.0.0.1", gate.server.server_port, timeout=5)
                conn.request("POST", route, payload, {"Content-Type": "application/json"})
                assert conn.getresponse().status == 200
                conn.close()
            finally:
                gate.close()
        assert len(received) == 2
        for body in received:
            assert body["messages"][:len(history)] == history
            assert body["messages"][-1]["content"] == [
                {"type": "tool_result", "tool_use_id": body["messages"][-1]["content"][0]["tool_use_id"], "content": "A"},
                {"type": "tool_result", "tool_use_id": body["messages"][-1]["content"][1]["tool_use_id"], "content": "B"},
            ]
    finally:
        peer.shutdown()
        thread.join()
        peer.server_close()


def test_actual_client_replays_restored_parallel_rounds_after_recreation(tmp_path):
    """Real Client/subprocess/relay, synthetic native and upstream; no vendor calls."""
    received = []
    usage = {"input_tokens": 0, "output_tokens": 0}

    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            turn = len(received)
            events = [{"type": "message_start", "message": {"id": f"m{turn}", "role": "assistant",
                       "model": "sonnet", "content": [], "usage": usage}}]
            blocks = [
                {"type": "thinking", "thinking": "fixture reasoning", "signature": f"signed-{turn}"},
                {"type": "text", "text": "Calling parallel tools."},
                {"type": "tool_use", "id": f"r{turn}a", "name": "mcp__hermes__probe", "input": {"lane": "a"}},
                {"type": "tool_use", "id": f"r{turn}b", "name": "mcp__hermes__probe", "input": {"lane": "b"}},
            ]
            for index, block in enumerate(blocks):
                events.extend([{"type": "content_block_start", "index": index, "content_block": block},
                               {"type": "content_block_stop", "index": index}])
            events.extend([{"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": usage},
                           {"type": "message_stop"}])
            data = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    script = tmp_path / "native.py"
    script.write_text(r'''import json, os, sys, urllib.request
messages = []
for line in sys.stdin:
    frame = json.loads(line)
    messages.append(frame['message'])
    if frame.get('shouldQuery') is False:
        print(json.dumps({'type': 'result', 'num_turns': 0}), flush=True)
    elif frame['type'] == 'user':
        break
results = [b for b in messages[-1]['content'] if b.get('type') == 'tool_result']
if results:
    for b in results:
        b['content'] = [{'type': 'text', 'text': b['content']},
                        {'type': 'text', 'text': ' native synthetic reminder'}]
        b['is_error'] = False
    results[-1]['cache_control'] = {'type': 'ephemeral', 'ttl': '1h'}
payload = json.dumps({'messages': messages, 'model': 'sonnet'}).encode()
request = urllib.request.Request(os.environ['ANTHROPIC_BASE_URL'] + '/v1/messages', data=payload,
                                 headers={'Content-Type': 'application/json'})
response = urllib.request.urlopen(request, timeout=5).read().decode()
assistant = None
for line in response.splitlines():
    if not line.startswith('data: '):
        continue
    event = json.loads(line[6:])
    if event['type'] == 'message_start':
        assistant = event['message']
    elif event['type'] == 'content_block_start':
        assistant['content'].append(event['content_block'])
    elif event['type'] == 'message_delta':
        assistant.update(event['delta'])
print(json.dumps({'type': 'assistant', 'message': assistant}), flush=True)
print(json.dumps({'type': 'stream_event', 'event': {'type': 'message_stop'}}), flush=True)
print(json.dumps({'type': 'result', 'subtype': 'success', 'usage': {'input_tokens': 0, 'output_tokens': 0}}), flush=True)
''', encoding="utf-8")
    peer = ThreadingHTTPServer(("127.0.0.1", 0), Peer)
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    env = {"PATH": os.defpath, "HOME": str(tmp_path),
           "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{peer.server_port}"}
    client = directsdk.Client(command=[sys.executable, str(script)], env=env)
    messages = [{"role": "user", "content": "Run parallel tools."}]
    tools = [{"type": "function", "function": {"name": "probe", "parameters": {"type": "object", "properties": {}}}}]
    try:
        for turn in range(4):
            if turn == 3:
                client.close()
                client = directsdk.Client(command=[sys.executable, str(script)], env=env)
            original = copy.deepcopy(messages)
            result = client.create(model="sonnet", messages=messages, tools=tools)
            assert messages == original
            assert len(result.choices[0].message.tool_calls) == 2
            expected = directsdk.prepare_history(messages)[1]
            current = received[-1]["messages"]
            for sent, frame in zip(current, expected):
                cleaned = copy.deepcopy(sent)
                for block in cleaned["content"]:
                    block.pop("cache_control", None)
                assert cleaned == frame["message"]
            assert len(current) == len(expected)
            if turn:
                marker = current[-1]["content"][-1]["cache_control"]
                assert marker == {"type": "ephemeral", "ttl": "1h"}
                previous = copy.deepcopy(received[-2]["messages"])
                for message in previous:
                    for block in message["content"]:
                        block.pop("cache_control", None)
                assert current[:len(previous)] == previous
            assistant = result.choices[0].message.model_dump()
            assistant["role"] = "assistant"
            messages.append(assistant)
            for call in result.choices[0].message.tool_calls:
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": "host <system-reminder>literal</system-reminder> " + call.id})
        assert len(received) == 4
    finally:
        client.close()
        peer.shutdown()
        thread.join()
        peer.server_close()


def test_nested_cache_marker_on_discarded_native_text_is_rejected():
    host = {**RESULT, "content": [{"type": "text", "text": "output"}]}
    native = {**host, "content": [*host["content"], {"type": "text", "text": "note", "cache_control": {"type": "ephemeral"}}]}

    with pytest.raises(QueriedTurnMismatch):
        restore_queried_turn(wire([native]), [host])



@pytest.mark.parametrize("annotation", [
    "\n<system-reminder>\n# userEmail\nsomeone@example.invalid\n</system-reminder>",
    "\nSession context: a wording no CLI has shipped yet",
    "\n\nToday's date is 2031-01-01.",
])
def test_restoration_is_anchored_on_hermes_ids_and_content_not_native_wording(annotation):
    native = [{**RESULT, "content": RESULT["content"] + annotation}]
    restored = json.loads(restore_queried_turn(wire(native), [RESULT]))
    assert restored["messages"][-1]["content"] == [RESULT]
