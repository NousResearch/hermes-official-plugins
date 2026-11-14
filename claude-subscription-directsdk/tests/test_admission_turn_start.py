"""After a long tool loop the next turn must still read the conversation before it (#122).

Once a user message carries more than tool results (the next turn, or a /steer row after
them), the cache entries written after the finished turn's first thinking block stop matching:
issue #122's captures show the next turn's first call reading only up to the newest entry
written before that block. Anthropic's lookup walks back at most 20 positions from a breakpoint
(a run of tool_use or tool_result blocks counts once), so after a longer loop the recurring
breakpoint cannot reach that entry and the read falls back to tools and system. The relay
keeps a second mark on it. Native layouts: 2.1.285 (two system marks, the message mark on its
trailing per-request ``role: system`` message) and 2.1.287+ (also the last assistant block).
"""
import copy
import hashlib
import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from admission import Admission, pin_message_breakpoint

MARKER = {'type': 'ephemeral', 'ttl': '1h'}
OPENING = {'type': 'text', 'text': 'Run the echo steps one at a time.' + ' source pack' * 200}
FOLLOW_UP = {'type': 'text', 'text': 'One more echo.'}
STEER = {'type': 'text', 'text': 'User sent a /steer: also print the date.'}
REMINDER = '\n<system-reminder>userEmail: user@example.invalid</system-reminder>'
LAYOUTS = {
    # tools, system, whether native also marks the last assistant block
    '2.1.285': ([{'name': 'mcp__hermes__terminal', 'description': 'run', 'input_schema': {'type': 'object'}}],
                [{'type': 'text', 'text': 'x-anthropic-billing-header: cc'},
                 {'type': 'text', 'text': 'You are Claude Code.', 'cache_control': MARKER},
                 {'type': 'text', 'text': 'Hermes system prompt.', 'cache_control': MARKER}], False),
    '2.1.287': ([{'name': 'mcp__hermes__terminal', 'description': 'run', 'input_schema': {'type': 'object'},
                  'cache_control': MARKER}],
                [{'type': 'text', 'text': 'x-anthropic-billing-header: cc'},
                 {'type': 'text', 'text': 'You are Claude Code. Hermes system prompt.', 'cache_control': MARKER}], True),
}


def assistant(n, think, calls):
    return {'role': 'assistant', 'content': [
        *([{'type': 'thinking', 'thinking': f'plan {n}', 'signature': f'sig{n}'}] if think else []),
        *({'type': 'tool_use', 'id': f't{n}c{k}', 'name': 'mcp__hermes__terminal', 'input': {'command': f'echo {n}.{k}'}}
          for k in range(calls))]}


def turn(opening, rounds, think, calls=1, steer=None, start=0):
    """Hermes' messages for one turn: its opening, ``rounds`` tool rounds and the answer. Round n's
    assistant message thinks when ``think(n)``; a /steer row rides in the results of round ``steer``."""
    messages = [{'role': 'user', 'content': [opening]}]
    for n in range(start, start + rounds):
        messages += [assistant(n, think(n), calls), {'role': 'user', 'content': [
            *({'type': 'tool_result', 'tool_use_id': f't{n}c{k}', 'content': f'{n}.{k}'} for k in range(calls)),
            *([STEER] if n == steer else [])]}]
    n = start + rounds
    return messages + [{'role': 'assistant', 'content': [
        *([{'type': 'thinking', 'thinking': f'plan {n}', 'signature': f'sig{n}'}] if think(n) else []),
        {'type': 'text', 'text': f'done {n}'}]}]


def native(messages, layout):
    """Native's request for a history ending in a user frame: its env message after the first turn
    and its per-request date message last, which is where it puts the message mark."""
    tools, system, assistant_mark = LAYOUTS[layout]
    sent = copy.deepcopy(messages)
    sent.insert(1, {'role': 'system', 'content': 'Primary working directory: /tmp/claude-directsdk-cwd'})
    if assistant_mark and len(sent) > 3:
        sent[-2]['content'][-1]['cache_control'] = MARKER
    sent.append({'role': 'system', 'content': [{'type': 'text', 'text': "<system-reminder>\nToday's date is 2026-10-10.\n</system-reminder>",
                                                'cache_control': MARKER}]})
    return {'model': 'claude-opus-5-5', 'tools': copy.deepcopy(tools), 'system': copy.deepcopy(system),
            'messages': sent}


def pinned(messages, layout):
    """The body the relay forwards for the request that queries the last message of ``messages``."""
    raw = json.dumps(native(messages, layout)).encode()
    return json.loads(pin_message_breakpoint(raw, messages[-1]['content']))


class Cache:
    """Anthropic's documented lookup: each breakpoint checks up to 20 positions back, itself included,
    a run of tool_use or tool_result blocks counting once; entries are written only at breakpoints.
    A thinking block renders closed once a later user message carries more than tool results (#122)."""

    def __init__(self):
        self.entries = set()

    def send(self, body):
        """Where this request's read ends: a message block ``(i, j)``, ``'system'`` or ``None``."""
        messages = body['messages']
        opens = [m['role'] == 'user' and any(b.get('type') != 'tool_result' for b in m['content']) for m in messages]
        flat = [('tools', t) for t in body['tools']] + [('system', b) for b in body['system']]
        for i, m in enumerate(messages):
            for j, b in enumerate(m['content'] if isinstance(m['content'], list) else [{'type': 'text', 'text': m['content']}]):
                flat.append(((i, j), dict(b, closed=any(opens[i + 1:])) if b.get('type') == 'thinking' else b))
        keys, digest = [], hashlib.sha256()
        for where, b in flat:
            digest.update(json.dumps([where[0] if isinstance(where, tuple) else where,
                                      {k: v for k, v in b.items() if k != 'cache_control'}], sort_keys=True).encode())
            keys.append(digest.hexdigest())
        marks = [k for k, (_, b) in enumerate(flat) if 'cache_control' in b]
        read = -1
        for k in marks:
            seen = 1
            while k >= 0 and seen <= 20:
                if keys[k] in self.entries:
                    read = max(read, k)
                    break
                kind, k = flat[k][1].get('type'), k - 1
                if not (kind in ('tool_use', 'tool_result') and k >= 0 and flat[k][1].get('type') == kind):
                    seen += 1
        self.entries.update(keys[k] for k in marks)
        return None if read < 0 else flat[read][0]


def message_marks(body):
    return [(i, j) for i, m in enumerate(body['messages']) if isinstance(m['content'], list)
            for j, b in enumerate(m['content']) if 'cache_control' in b]


def mark_count(value):
    """Every breakpoint the API counts toward its four: nested markers and a top-level one included."""
    if isinstance(value, dict):
        return ('cache_control' in value) + sum(mark_count(v) for k, v in value.items() if k != 'cache_control')
    return sum(map(mark_count, value)) if isinstance(value, list) else 0


def plain(value):
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items() if k != 'cache_control'}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


def replay(messages, layout):
    """Send every request of the conversation in order; return (forwarded body, read) per request.
    Only markers ever change, and never past four or more than one over native's own count."""
    cache, out = Cache(), []
    for end in [i + 1 for i, m in enumerate(messages) if m['role'] == 'user']:
        raw, body = native(messages[:end], layout), pinned(messages[:end], layout)
        assert plain(body) == plain(raw) and body['tools'] == raw['tools'] and body['system'] == raw['system']
        assert mark_count(body) <= min(4, mark_count(raw) + 1)
        out.append((body, cache.send(body)))
    return out


def native_index(i):
    return i + 1 if i else 0  # native's env message sits at messages[1]


@pytest.mark.parametrize('layout', LAYOUTS)
@pytest.mark.parametrize('first', [0, 6], ids=['thinks-at-once', 'thinks-from-round-6'])
def test_next_turn_after_a_long_loop_reads_up_to_the_finished_turns_first_thinking(first, layout):
    think = lambda n: n in (first, 9, 14)
    messages = turn(OPENING, 14, think) + [{'role': 'user', 'content': [FOLLOW_UP]}]
    sent = replay(messages, layout)
    # Hermes index of the frame before the first thinking: the opening, or round first-1's results.
    anchor = (native_index(2 * first), 0)
    body, read = sent[-1]
    assert anchor in message_marks(body)
    assert read == anchor  # not 'system': only the finished loop from its first thinking is written again
    # Within the loop each request still reads the previous one's recurring breakpoint.
    for (before, _), (_, after) in zip(sent[:-2], sent[1:-1]):
        assert after == message_marks(before)[-1]


def test_a_later_turn_keeps_everything_before_the_finished_turn():
    """Turn 2 runs the long loop: turn 3's first call reads all of turn 1 and turn 2's opening."""
    first = turn(OPENING, 1, lambda n: True)
    second = turn(FOLLOW_UP, 14, lambda n: True, start=1)
    messages = first + second + [{'role': 'user', 'content': [{'type': 'text', 'text': 'And again.'}]}]
    body, read = replay(messages, '2.1.285')[-1]
    assert read == (native_index(len(first)), 0) == (5, 0)
    assert mark_count(body) == 4


@pytest.mark.parametrize('layout', LAYOUTS)
def test_short_loop_is_forwarded_with_the_recurring_mark_only(layout):
    messages = turn(OPENING, 4, lambda n: True) + [{'role': 'user', 'content': [FOLLOW_UP]}]
    sent = replay(messages, layout)
    tools, system, assistant_mark = LAYOUTS[layout]
    for body, _ in sent:
        # 2.1.287+'s own mark on the last assistant block stays; nothing is added.
        assert len(message_marks(body)) == 1 + (assistant_mark and len(body['messages']) > 4)
    assert sent[-1][1] == (0, 0)  # the recurring breakpoint's own lookback still reaches the opening


def test_loop_without_thinking_is_left_to_the_recurring_mark():
    messages = turn(OPENING, 14, lambda n: False) + [{'role': 'user', 'content': [FOLLOW_UP]}]
    sent = replay(messages, '2.1.285')
    assert all(len(message_marks(body)) == 1 for body, _ in sent)
    assert sent[-1][1] == message_marks(sent[-2][0])[-1]  # nothing closes: the whole loop is read


@pytest.mark.parametrize('layout', LAYOUTS)
def test_steer_row_after_a_long_loop_reads_up_to_the_turns_first_thinking(layout):
    messages = turn(OPENING, 14, lambda n: True, steer=11)
    sent = replay(messages, layout)
    steer = next(k for k, (body, _) in enumerate(sent) if plain(body['messages'][-2]['content'][-1]) == STEER)
    body, read = sent[steer]
    assert read == (0, 0) and (0, 0) in message_marks(body)
    # After the steer the open turn starts at the steer row; the next request reads the steer request's entry.
    assert sent[steer + 1][1] == message_marks(body)[-1]


@pytest.mark.parametrize('layout', LAYOUTS)
def test_parallel_rounds_then_a_new_turn_read_up_to_the_first_thinking(layout):
    messages = turn(OPENING, 6, lambda n: True, calls=4) + [{'role': 'user', 'content': [FOLLOW_UP]}]
    assert replay(messages, layout)[-1][1] == (0, 0)


def test_at_the_limit_a_native_mark_moves_instead_of_a_fifth():
    """2.1.287+ with a tools mark is at four: native's mark on the last assistant block is the one
    the next request does not need (the recurring breakpoint follows it), so it moves."""
    messages = turn(OPENING, 14, lambda n: True) + [{'role': 'user', 'content': [FOLLOW_UP]}]
    raw = native(messages, '2.1.287')
    body = pinned(messages, '2.1.287')
    assert mark_count(raw) == mark_count(body) == 4
    assert message_marks(raw) == [(30, 1), (32, 0)] and message_marks(body) == [(0, 0), (31, 0)]
    assert plain(body) == plain(raw) and body['tools'] == raw['tools'] and body['system'] == raw['system']


def test_no_spare_slot_and_no_native_mark_to_move_forwards_the_fold_only():
    messages = turn(OPENING, 14, lambda n: True) + [{'role': 'user', 'content': [FOLLOW_UP]}]
    raw = native(messages, '2.1.285')
    raw['tools'][0]['cache_control'] = MARKER
    raw['cache_control'] = {'type': 'ephemeral'}  # top-level automatic caching takes a slot too
    body = json.loads(pin_message_breakpoint(json.dumps(raw).encode(), messages[-1]['content']))
    assert message_marks(body) == [(31, 0)]


@pytest.mark.parametrize('layout', LAYOUTS)
def test_a_nested_marker_takes_a_slot_so_the_request_stays_at_four(layout):
    """A marker inside a historical tool_result's content (a host marker restoration carried back)
    counts toward the four: 2.1.285 has no slot left and forwards the fold only, 2.1.287+ moves its
    mark on the last assistant block. Neither forwards a fifth, which the API rejects."""
    messages = turn(OPENING, 14, lambda n: True) + [{'role': 'user', 'content': [FOLLOW_UP]}]
    raw = native(messages, layout)
    raw['tools'][0].pop('cache_control', None)
    raw['messages'][5]['content'][0]['content'] = [{'type': 'text', 'text': '1.0', 'cache_control': MARKER}]
    body = json.loads(pin_message_breakpoint(json.dumps(raw).encode(), messages[-1]['content']))
    assert mark_count(raw) == mark_count(body) == 4
    assert body['messages'][5] == raw['messages'][5]
    assert message_marks(body) == ([(31, 0)] if layout == '2.1.285' else [(0, 0), (31, 0)])
    assert plain(body) == plain(raw) and body['tools'] == raw['tools'] and body['system'] == raw['system']


def test_turn_mark_takes_the_ttl_of_the_mark_after_it():
    messages = turn(OPENING, 14, lambda n: True) + [{'role': 'user', 'content': [FOLLOW_UP]}]
    raw = native(messages, '2.1.285')
    raw['messages'][-1]['content'][0]['cache_control'] = {'type': 'ephemeral'}
    body = json.loads(pin_message_breakpoint(json.dumps(raw).encode(), messages[-1]['content']))
    assert body['messages'][0]['content'][0]['cache_control'] == {'type': 'ephemeral'}
    assert body['messages'][31]['content'][0]['cache_control'] == {'type': 'ephemeral'}


def test_relay_forwards_the_turn_mark_with_the_steer_frame_restored():
    """Through the real relay: native's reminder on the last tool result is restored away, the
    steer frame recurs, and the turn mark is on the opening frame."""
    messages = turn(OPENING, 14, lambda n: True, steer=11)
    end = next(i for i, m in enumerate(messages) if STEER in m['content']) + 1
    raw = native(messages[:end], '2.1.285')
    raw['messages'][-2]['content'][0]['content'] += REMINDER
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
    gate = Admission(f'http://127.0.0.1:{peer.server_port}', 5, queried=messages[end - 1]['content'])
    try:
        route = gate.url.removeprefix(f'http://127.0.0.1:{gate.server.server_port}') + '/v1/messages'
        conn = http.client.HTTPConnection('127.0.0.1', gate.server.server_port, timeout=5)
        conn.request('POST', route, json.dumps(raw).encode(), {'Content-Type': 'application/json'})
        assert conn.getresponse().status == 200
        conn.close()
        assert gate.unrestored is None and gate.failure is None
    finally:
        gate.close()
        peer.shutdown()
        thread.join()
        peer.server_close()
    sent = received[0]
    # The relay returns native's trailing date message to its env message, so the steer frame is last.
    assert sent['messages'][-1]['role'] == 'user' and sent['messages'][1]['content'].endswith("Today's date is 2026-10-10.\n</system-reminder>")
    assert plain(sent['messages'][-1]['content']) == messages[end - 1]['content']
    assert message_marks(sent) == [(0, 0), (len(sent['messages']) - 1, 1)]
    assert mark_count(sent) == 4
