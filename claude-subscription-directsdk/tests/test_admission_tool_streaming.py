"""Tool arguments stream as the model writes them, so a long tool call is not mistaken for a stall.

Native sets ``eager_input_streaming`` only when it talks to a first-party base URL; behind the relay it
leaves the flag off and the API buffers a large argument to the end (measured live: one 89.8 s gap
while Opus wrote a 2,000-word ``write_file`` argument).
"""
import json

from admission import Capture, stream_tool_input

TOOLS = [{'name': 'mcp__hermes__write_file', 'input_schema': {'type': 'object'}},
         {'name': 'mcp__hermes__read_file', 'input_schema': {'type': 'object'}, 'eager_input_streaming': False}]


def test_every_tool_asks_for_streamed_arguments_and_native_values_win():
    out = json.loads(stream_tool_input(json.dumps({'model': 'm', 'tools': TOOLS, 'messages': []}).encode()))
    assert out['tools'] == [dict(TOOLS[0], eager_input_streaming=True), TOOLS[1]]


def test_requests_without_tools_or_unparseable_forward_byte_identical():
    for raw in (json.dumps({'model': 'm', 'messages': []}).encode(), b'not json',
                json.dumps({'model': 'm', 'tools': [dict(TOOLS[1])], 'messages': []}).encode()):
        assert stream_tool_input(raw) is raw


def sse(*events):
    return b''.join(b'event: x\ndata: ' + json.dumps(e).encode() + b'\n\n' for e in events)


def test_a_tool_call_cut_off_mid_argument_is_dropped_and_the_stream_still_captures():
    """Streamed input is unvalidated: a max_tokens cut leaves partial JSON. The relay must keep going
    (it forwards each chunk after capturing it) and must never hand Hermes a call with guessed arguments."""
    capture = Capture()
    capture.feed(sse(
        {'type': 'message_start', 'message': {'id': 'm', 'role': 'assistant', 'content': [], 'usage': {'input_tokens': 1}}},
        {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'Writing it.'}},
        {'type': 'content_block_stop', 'index': 0},
        {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'tool_use', 'id': 't', 'name': 'w', 'input': {}}},
        {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': '{"path": "a.md", "content": "trunc'}},
        {'type': 'content_block_stop', 'index': 1},
        {'type': 'message_delta', 'delta': {'stop_reason': 'max_tokens'}, 'usage': {'output_tokens': 9}},
        {'type': 'message_stop'}))
    assert capture.complete
    assert capture.message['content'] == [{'type': 'text', 'text': 'Writing it.'}]
    assert capture.message['stop_reason'] == 'max_tokens'
