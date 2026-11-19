"""Claude Code 2.1.287+ marks two message blocks from the second request on (#33).

The marks sit on the last tool_use of the assistant turn and on a trailing ``role: system``
message carrying per-request context. That message never recurs: in the next request its
index holds the next assistant turn, so the entry written there is never read and every
parallel round is cache-written twice. The relay must keep one breakpoint on the last block
that recurs and drop the marks after it, so the next request replays the cached prefix.
"""
import copy
import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from admission import Admission, pin_message_breakpoint

MARKER = {'type': 'ephemeral'}


def tools_as_native(body):
    return [{k: v for k, v in t.items() if k != 'eager_input_streaming'} for t in body['tools']]

# Native's own system/tools marks: with two message marks the request is at Anthropic's limit of four.
SYSTEM = [{'type': 'text', 'text': 'x-anthropic-billing-header: cc'},
          {'type': 'text', 'text': 'You are Claude Code. Hermes system prompt.', 'cache_control': MARKER}]
TOOLS = [{'name': 'mcp__hermes__terminal', 'description': 'run', 'input_schema': {'type': 'object'},
          'cache_control': MARKER}]
REMINDER = '\n<system-reminder>userEmail: user@example.com</system-reminder>'


def results(n):
    return [{'type': 'tool_result', 'tool_use_id': f'r{n}c{k}', 'content': f'output {n}.{k}'} for k in range(4)]


def assistant(n):
    return {'role': 'assistant', 'content': [
        {'type': 'thinking', 'thinking': f'plan {n}', 'signature': f'sig{n}'},
        *({'type': 'tool_use', 'id': f'r{n}c{k}', 'name': 'mcp__hermes__terminal', 'input': {'command': f'echo {n}.{k}'}}
          for k in range(4))]}


def native_request(rounds, reminder):
    """What native sends after ``rounds`` parallel rounds: older rounds replay as Hermes sent them,
    the newest carries native's reminder (optionally) and is followed by its per-request context."""
    messages = [{'role': 'user', 'content': [{'type': 'text', 'text': 'run four probes, twice'}]}]
    for n in range(rounds):
        messages += [assistant(n), {'role': 'user', 'content': results(n)}]
    messages[-2]['content'][-1]['cache_control'] = MARKER  # native mark 1: last tool_use of the turn
    if reminder:
        newest = messages[-1]['content'][-1]
        newest['content'] += REMINDER
    messages.append({'role': 'system', 'content': [  # native mark 2: per-request context, never replayed
        {'type': 'text', 'text': f"Today's date is 2026-10-04. Request {rounds}.", 'cache_control': MARKER}]})
    return {'model': 'claude-opus-5-5', 'system': copy.deepcopy(SYSTEM), 'tools': copy.deepcopy(TOOLS),
            'messages': messages}


def message_marks(body):
    return [(i, j) for i, m in enumerate(body['messages']) for j, b in enumerate(m['content']) if 'cache_control' in b]


def mark_count(body):
    return sum('cache_control' in b for part in (body['system'], body['tools']) for b in part) + len(message_marks(body))


def plain(value):
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items() if k != 'cache_control'}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


def prefix(body, at):
    """Bytes of the request through block ``at``, in the order the cache reads them (tools, system,
    messages). Markers are left out: they move between requests and are not prompt content."""
    i, j = at
    head = body['messages'][:i] + [{**body['messages'][i], 'content': body['messages'][i]['content'][:j + 1]}]
    return json.dumps(plain({'tools': body['tools'], 'system': body['system'], 'messages': head}),
                      ensure_ascii=False, separators=(',', ':')).encode()


def forward(native_body, queried):
    """Send one native request through a real admission relay; return the body that went upstream."""
    received = []

    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200)
            self.send_header('Content-Length', '0')
            self.end_headers()

    peer = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    gate = Admission(f'http://127.0.0.1:{peer.server_port}', 5, queried=queried)
    try:
        route = gate.url.removeprefix(f'http://127.0.0.1:{gate.server.server_port}') + '/v1/messages'
        conn = http.client.HTTPConnection('127.0.0.1', gate.server.server_port, timeout=5)
        conn.request('POST', route, json.dumps(native_body).encode(), {'Content-Type': 'application/json'})
        assert conn.getresponse().status == 200
        conn.close()
        assert gate.unrestored is None and gate.failure is None
    finally:
        gate.close()
        peer.shutdown()
        thread.join()
        peer.server_close()
    assert len(received) == 1
    return received[0]


@pytest.mark.parametrize('reminder', [False, True], ids=['context-only', 'reminder-in-last-result'])
def test_two_native_marks_keep_one_breakpoint_the_next_request_reads(reminder):
    native_n = native_request(1, reminder)
    sent_n = forward(native_n, results(0))
    sent_n1 = forward(native_request(2, reminder), results(1))

    # Native's mark on the last tool_use recurs and stays; the trailing system message's mark
    # moves onto the last tool result (which, restored to Hermes' frame, recurs).
    assert message_marks(native_n) == [(1, 4), (3, 0)]
    assert message_marks(sent_n) == [(1, 4), (2, 3)]
    assert message_marks(sent_n1) == [(3, 4), (4, 3)]
    # Only a marker moved: same content, same system/tools marks, never more than native's four.
    # (The relay also asks for streamed tool input on every tool; see test_admission_tool_streaming.)
    assert sent_n['system'] == native_n['system'] and tools_as_native(sent_n) == native_n['tools']
    assert plain(sent_n['messages'][3]) == plain(native_n['messages'][3])
    assert mark_count(sent_n) == mark_count(native_n) == 4

    # Request N+1 replays request N's prefix through its last breakpoint byte for byte, so the
    # entry N wrote is read instead of the round being written again.
    last = message_marks(sent_n)[-1]
    assert prefix(sent_n1, last) == prefix(sent_n, last)


def test_unrestorable_result_falls_back_to_native_tool_use_mark():
    """Restoration fails open (native's reminder left in the last result): the newest turn does not
    recur, so the only breakpoint kept is native's own on the last tool_use, and it is read next time."""
    native = native_request(1, reminder=True)
    sent = json.loads(pin_message_breakpoint(json.dumps(native).encode(), results(0)))
    assert message_marks(sent) == [(1, 4)] and mark_count(sent) == mark_count(native) - 1
    assert plain(sent) == plain(native) and sent['system'] == native['system'] and sent['tools'] == native['tools']
    following = native_request(2, reminder=True)
    assert prefix(following, (1, 4)) == prefix(sent, (1, 4))


def test_marks_inside_the_recurring_span_are_left_alone():
    """Two marks that both recur: nothing to drop, payload forwarded as native built it."""
    native = native_request(1, reminder=False)
    native['messages'].pop()  # no trailing context message
    native['messages'][2]['content'][-1]['cache_control'] = MARKER
    raw = json.dumps(native).encode()
    assert pin_message_breakpoint(raw, results(0)) == raw
