"""Per-call context recurs through the real relay; all upstream traffic is loopback."""
import copy
import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from admission import Admission, stable_request_context

ENV = 'Fixture environment'
DATE = "Today's date is 2031-01-01."
FRAME = {'type': 'text', 'text': 'Next user question'}
REMINDER = {'type': 'text', 'text': 'Native context reminder'}
MARK = {'type': 'ephemeral', 'ttl': '1h'}
OTHER = {'type': 'ephemeral', 'ttl': '5m'}
ASSISTANT = {'role': 'assistant', 'content': [
    {'type': 'thinking', 'thinking': 'Fixture reasoning', 'signature': 'fixture-signature'},
    {'type': 'text', 'text': 'Previous answer'}]}


def wire(messages):
    return json.dumps({'model': 'fixture', 'messages': messages}, ensure_ascii=False, separators=(',', ':')).encode()


def native(env=ENV, tail=None):
    return copy.deepcopy([{'role': 'system', 'content': env}, ASSISTANT,
                          {'role': 'user', 'content': [REMINDER, FRAME]},
                          {'role': 'system', 'content': tail if tail is not None else DATE}])


@pytest.mark.parametrize('env', [ENV, [{'type': 'text', 'text': 'Earlier'}, {'type': 'text', 'text': ENV}]])
@pytest.mark.parametrize('tail', [DATE, [{'type': 'text', 'text': DATE}]])
def test_date_returns_to_string_or_list_environment(env, tail):
    out = json.loads(stable_request_context(wire(native(env, tail)), [FRAME]))['messages']
    assert len(out) == 3
    if isinstance(env, list):
        assert out[0]['content'] == [env[0], {'type': 'text', 'text': ENV + '\n\n' + DATE}]
    else:
        assert out[0]['content'] == ENV + '\n\n' + DATE
    assert out[1] == ASSISTANT
    assert out[2]['content'] == [FRAME]


def test_date_first_in_multi_block_trailing_system_moves_to_environment():
    # Native 2.1.311 on Fable: the date line shares the trailing system
    # message with a per-step reminder. Only the date moves, so the
    # environment message matches the user-turn shape byte for byte.
    reminder = {'type': 'text', 'text': 'First privately list what you need next'}
    messages = native(tail=[{'type': 'text', 'text': DATE}, reminder])
    out = json.loads(stable_request_context(wire(messages), [FRAME]))['messages']
    assert out[0]['content'] == ENV + '\n\n' + DATE
    assert out[-1] == {'role': 'system', 'content': [reminder]}


def test_date_first_multi_block_matches_user_turn_environment():
    user_turn = json.loads(stable_request_context(wire(native()), [FRAME]))['messages']
    reminder = {'type': 'text', 'text': 'First privately list what you need next'}
    tool_step = json.loads(stable_request_context(
        wire(native(tail=[{'type': 'text', 'text': DATE}, reminder])), [FRAME]))['messages']
    assert tool_step[0] == user_turn[0]


@pytest.mark.parametrize('tail', ["Today's date is 2031-01-01. Now 09:30.", 'Per-request note', "Today's date is soon."])
def test_trailing_text_other_than_the_date_line_stays_in_place(tail):
    out = json.loads(stable_request_context(wire(native(tail=tail)), [FRAME]))['messages']
    assert out[0]['content'] == ENV
    assert out[-1] == {'role': 'system', 'content': tail}
    assert out[-2]['content'] == [FRAME]


@pytest.mark.parametrize('env', [ENV + DATE, ENV + '\n' + DATE])
def test_environment_already_ending_with_the_date_is_not_given_it_twice(env):
    out = json.loads(stable_request_context(wire(native(env)), [FRAME]))['messages']
    assert out[0]['content'] == env
    assert out[-1] == {'role': 'system', 'content': DATE}


def test_reminder_equal_to_the_frame_start_is_still_dropped():
    messages = native()[:-1]
    messages[-1]['content'] = [REMINDER, REMINDER, FRAME]
    out = json.loads(stable_request_context(wire(messages), [REMINDER, FRAME]))['messages']
    assert out[-1]['content'] == [REMINDER, FRAME]


@pytest.mark.parametrize('blocks, queried', [
    ([{'type': 'text', 'text': 'ab'}, {'type': 'text', 'text': 'a'}], [{'type': 'text', 'text': 'a'}, {'type': 'text', 'text': 'ba'}]),
    ([FRAME, FRAME], [FRAME]),
])
def test_frame_not_found_exactly_once_is_left_as_native_built_it(blocks, queried):
    messages = native()[:-1]
    messages[-1]['content'] = blocks
    assert stable_request_context(wire(messages), queried) == wire(messages)


@pytest.mark.parametrize('existing', [False, True])
def test_removed_markers_transfer_with_setdefault_and_never_multiply(existing):
    messages = native(tail=[{'type': 'text', 'text': DATE, 'cache_control': MARK}])
    messages[2]['content'][0]['cache_control'] = MARK
    if existing:
        messages[2]['content'][1]['cache_control'] = OTHER
    out = json.loads(stable_request_context(wire(messages), [FRAME]))['messages']
    assert out[-1]['content'] == [{**FRAME, 'cache_control': OTHER if existing else MARK}]
    assert sum('cache_control' in b for m in out for b in m['content']) == 1


def test_reminder_marker_moves_onto_frame_and_suffix_is_kept():
    messages = native()[:-1]
    messages[-1]['content'] = [{**REMINDER, 'cache_control': MARK}, REMINDER, FRAME, REMINDER]
    out = json.loads(stable_request_context(wire(messages), [FRAME]))['messages']
    assert out[-1]['content'] == [{**FRAME, 'cache_control': MARK}, REMINDER]


@pytest.mark.parametrize('case', ['tool_result', 'opening', 'protected', 'missing', 'already_stable', 'multiple_tail'])
def test_ineligible_context_is_byte_unchanged(case):
    messages = native()[:-1]
    queried = [FRAME]
    if case == 'tool_result':
        queried = [{'type': 'tool_result', 'tool_use_id': 'fixture-tool', 'content': 'output'}]
        messages[-1]['content'] = queried
    elif case == 'opening':
        del messages[1]
    elif case == 'protected':
        messages[-1]['content'][0]['citations'] = []
    elif case == 'missing':
        queried = [{'type': 'text', 'text': 'Absent frame'}]
    elif case == 'already_stable':
        messages[0]['content'] = ENV + '\n\n' + DATE
        messages[-1]['content'] = [FRAME]
        messages.append({'role': 'system', 'content': DATE})
    else:
        messages[-1]['content'] = [FRAME]
        messages.append({'role': 'system', 'content': [REMINDER, {'type': 'text', 'text': DATE}]})
    payload = wire(messages)
    assert stable_request_context(payload, queried) == payload


@pytest.mark.parametrize('payload', [b'not json', b'\xff', b'{}', b'null', b'{"messages":[null]}',
    wire([{'role': 'system', 'content': ENV}, {'role': 'system', 'content': [{'type': 'text', 'text': 42}]}])])
def test_unparseable_or_unexpected_payload_is_byte_unchanged(payload):
    assert stable_request_context(payload, [FRAME]) == payload


def test_transform_is_idempotent():
    once = stable_request_context(wire(native()), [FRAME])
    assert stable_request_context(once, [FRAME]) == once


@pytest.fixture
def relay():
    received = []

    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            received.append(self.rfile.read(int(self.headers['Content-Length'])))
            self.send_response(200)
            self.send_header('Content-Length', '0')
            self.end_headers()

    peer = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
    thread = threading.Thread(target=peer.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
    thread.start()

    def send(payload, queried):
        gate = Admission(f'http://127.0.0.1:{peer.server_port}', 5, queried=queried)
        conn = http.client.HTTPConnection('127.0.0.1', gate.server.server_port, timeout=5)
        try:
            conn.request('POST', gate.prefix + '/v1/messages', payload, {'Content-Type': 'application/json'})
            response = conn.getresponse()
            assert response.status == 200
            response.read()
            assert gate.failure is None
            return received[-1]
        finally:
            conn.close()
            gate.close()

    try:
        yield send
    finally:
        peer.shutdown()
        thread.join()
        peer.server_close()


def test_consecutive_per_call_requests_share_byte_identical_prompt_prefix(relay):
    first = json.loads(relay(wire(native()), [FRAME]))['messages']
    next_frame = {'type': 'text', 'text': 'Follow-up question'}
    messages = native()[:2] + [{'role': 'user', 'content': [FRAME]}, copy.deepcopy(ASSISTANT),
        {'role': 'user', 'content': [REMINDER, next_frame]}, {'role': 'system', 'content': DATE}]
    second = json.loads(relay(wire(messages), [next_frame]))['messages']
    assert len(first) == 3
    assert first[-1]['content'] == [FRAME]
    assert wire(second[:len(first)]) == wire(first)
    assert second[len(first)] == ASSISTANT
    assert second[-1]['content'] == [next_frame]
