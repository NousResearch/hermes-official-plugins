"""One upstream admission and first-response authority over native recovery."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'evals'))
import directsdk
from directsdk_admission import upstream_closed

NATIVE = r'''
import json, os, sys, urllib.request, urllib.error
for line in sys.stdin:
    frame=json.loads(line)
    if frame.get('shouldQuery') is False:
        print(json.dumps({'type':'result','num_turns':0}),flush=True)
        continue
    break
url=os.environ['ANTHROPIC_BASE_URL']+'/v1/messages'
for _ in range(2):
    try:
        urllib.request.urlopen(urllib.request.Request(url,data=b'{}',headers={'Content-Type':'application/json'}),timeout=5).read()
    except urllib.error.HTTPError:
        break
print(json.dumps({'type':'assistant','message':{'id':'first','role':'assistant','content':[{'type':'text','text':'FIRST'}]}}))
print(json.dumps({'type':'stream_event','event':{'type':'message_stop'}}))
print(json.dumps({'type':'result','subtype':'success','usage':{'input_tokens':0,'output_tokens':0}}))
'''


@pytest.mark.parametrize('stop', ['end_turn', 'max_tokens', 'model_context_window_exceeded'])
def test_first_response_owns_usage_and_stops_recovery(tmp_path, stop):
    calls = []
    usage = {'input_tokens':0, 'output_tokens':0, 'cache_read_input_tokens':0, 'cache_creation_input_tokens':0}
    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            calls.append(self.path)
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.end_headers()
            events = [
                {'type':'message_start','message':{'id':'first','role':'assistant','model':'sonnet','content':[], 'usage':usage}},
                {'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}},
                {'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'FIRST'}},
                {'type':'content_block_stop','index':0},
                {'type':'message_delta','delta':{'stop_reason':stop},'usage':usage},
                {'type':'message_stop'},
            ]
            self.wfile.write(''.join('data: '+json.dumps(e)+'\n\n' for e in events).encode())
    peer=ThreadingHTTPServer(('127.0.0.1',0),Peer)
    thread=threading.Thread(target=peer.serve_forever,daemon=True); thread.start()
    native=tmp_path/'native.py'; native.write_text(NATIVE)
    client=directsdk.Client(command=[sys.executable,str(native)],env={'PATH':os.defpath,'HOME':str(tmp_path),'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{peer.server_port}'})
    try:
        result=client.create(model='sonnet',messages=[{'role':'user','content':'fixture'}])
        assert len(calls)==1
        assert result.choices[0].message.content=='FIRST'
        assert result.choices[0].finish_reason==('stop' if stop=='end_turn' else 'length')
        assert result.usage.prompt_tokens==0
        assert result.choices[0].message.reasoning_details[0]['messages'][0]['stop_reason']==stop
    finally:
        client.close(); peer.shutdown(); thread.join(); peer.server_close()



@pytest.mark.parametrize('details, refusal', [
    ({'category': 'cyber', 'explanation': 'The request was declined.'}, 'The request was declined.'),
    ({'category': 'cyber', 'explanation': None}, 'provider refusal category: cyber'),
    (None, None),
])
def test_contentless_refusal_is_a_terminal_content_filter(tmp_path, details, refusal):
    """A refusal carries no content block; as `stop` with no content Hermes would retry it as an empty reply (re-billing
    the prompt each time). It is content_filter with stop_details' reason, through Hermes' own transport normalizer."""
    from agent.transports import get_transport
    usage = {'input_tokens':0, 'output_tokens':0, 'cache_read_input_tokens':0, 'cache_creation_input_tokens':0}
    class Peer(BaseHTTPRequestHandler):
        extra = []
        def log_message(self, *args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.end_headers()
            delta = {'stop_reason':'refusal', **({'stop_details':details} if details else {})}
            events = [
                {'type':'message_start','message':{'id':'first','role':'assistant','model':'sonnet','content':[], 'usage':usage}},
                *Peer.extra,
                {'type':'message_delta','delta':delta,'usage':usage},
                {'type':'message_stop'},
            ]
            self.wfile.write(''.join('data: '+json.dumps(e)+'\n\n' for e in events).encode())
    peer=ThreadingHTTPServer(('127.0.0.1',0),Peer)
    thread=threading.Thread(target=peer.serve_forever,daemon=True); thread.start()
    native=tmp_path/'native.py'; native.write_text(NATIVE)
    client=directsdk.Client(command=[sys.executable,str(native)],env={'PATH':os.defpath,'HOME':str(tmp_path),'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{peer.server_port}'})
    try:
        if details:
            # A tool call the classifier cut off must not reach Hermes as tool_calls: Hermes would run it.
            tools=[{'type':'function','function':{'name':'list_things','description':'list','parameters':{'type':'object','properties':{}}}}]
            tool_use=[{'type':'content_block_start','index':0,'content_block':{'type':'tool_use','id':'toolu_1','name':'mcp__hermes__list_things','input':{}}},
                      {'type':'content_block_delta','index':0,'delta':{'type':'input_json_delta','partial_json':''}},
                      {'type':'content_block_stop','index':0}]
            Peer.extra = tool_use
            cut=client.create(model='sonnet',messages=[{'role':'user','content':'fixture'}],tools=tools)
            Peer.extra = []
            assert cut.choices[0].finish_reason=='content_filter' and cut.choices[0].message.refusal==refusal
        result=client.create(model='sonnet',messages=[{'role':'user','content':'fixture'}])
        message=result.choices[0].message
        assert result.choices[0].finish_reason=='content_filter'
        assert message.content is None and message.refusal==refusal
        assert message.reasoning_details[0]['messages'][0]['stop_reason']=='refusal'  # the signed native turn still replays
        normalized=get_transport('chat_completions').normalize_response(result)
        assert normalized.finish_reason=='content_filter' and normalized.content==refusal
        last=list(client.create(model='sonnet',messages=[{'role':'user','content':'fixture'}],stream=True))[-1].choices[0]
        assert last.finish_reason=='content_filter' and last.delta.refusal==refusal
    finally:
        client.close(); peer.shutdown(); thread.join(); peer.server_close()


@pytest.mark.parametrize('text', ['', 'done'])
def test_a_thinking_only_turn_reaches_hermes_without_reasoning(tmp_path, text):
    """With summarized display, a turn of thinking alone would reach Hermes as a reasoning-only `stop`, which Hermes
    promotes to the final answer, ending the tool loop mid-task. It arrives with no reasoning, so Hermes' empty-response
    recovery nudges the model on; the signed thinking still replays. A turn with text keeps its reasoning."""
    usage = {'input_tokens':0, 'output_tokens':0, 'cache_read_input_tokens':0, 'cache_creation_input_tokens':0}
    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.end_headers()
            events = [
                {'type':'message_start','message':{'id':'first','role':'assistant','model':'sonnet','content':[], 'usage':usage}},
                {'type':'content_block_start','index':0,'content_block':{'type':'thinking','thinking':'','signature':''}},
                {'type':'content_block_delta','index':0,'delta':{'type':'thinking_delta','thinking':'I will run the next command.'}},
                {'type':'content_block_delta','index':0,'delta':{'type':'signature_delta','signature':'signed-test'}},
                {'type':'content_block_stop','index':0},
                *([{'type':'content_block_start','index':1,'content_block':{'type':'text','text':''}},
                   {'type':'content_block_delta','index':1,'delta':{'type':'text_delta','text':text}},
                   {'type':'content_block_stop','index':1}] if text else []),
                {'type':'message_delta','delta':{'stop_reason':'end_turn'},'usage':usage},
                {'type':'message_stop'},
            ]
            self.wfile.write(''.join('data: '+json.dumps(e)+'\n\n' for e in events).encode())
    peer=ThreadingHTTPServer(('127.0.0.1',0),Peer)
    thread=threading.Thread(target=peer.serve_forever,daemon=True); thread.start()
    native=tmp_path/'native.py'; native.write_text(NATIVE)
    client=directsdk.Client(command=[sys.executable,str(native)],env={'PATH':os.defpath,'HOME':str(tmp_path),'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{peer.server_port}'})
    try:
        result=client.create(model='sonnet',messages=[{'role':'user','content':'fixture'}])
        message=result.choices[0].message
        assert result.choices[0].finish_reason=='stop' and message.tool_calls is None
        assert message.reasoning_content==('I will run the next command.' if text else None)
        assert message.content==(text or None)
        signed=message.reasoning_details[0]['messages'][0]['content'][0]
        assert signed['type']=='thinking' and signed['signature']=='signed-test'  # the native turn still replays
    finally:
        client.close(); peer.shutdown(); thread.join(); peer.server_close()


def _hermes_reply(response):
    """What Hermes shows the user for a content_filter response: core's own refusal handler, no fallback configured."""
    from types import SimpleNamespace
    from agent.transports import get_transport
    from agent.turn_retry_state import TurnRetryState
    from agent.turn_truncation import handle_content_policy_refusal
    noop = lambda *a, **k: None
    agent = SimpleNamespace(
        api_mode='chat_completions', provider='claude-subscription-directsdk', model='sonnet', log_prefix='',
        thinking_callback=None, _get_transport=lambda: get_transport('chat_completions'),
        _extract_reasoning=lambda message: getattr(message, 'reasoning', None),
        _invoke_api_request_error_hook=noop, _has_pending_fallback=lambda: False, _try_activate_fallback=lambda: False,
        _buffer_diagnostic_status=noop, _flush_status_buffer=noop, _emit_diagnostic_status=noop,
        _cleanup_task_resources=noop, _persist_session=noop)
    verdict = handle_content_policy_refusal(
        agent, response, TurnRetryState(), thinking_spinner=None, messages=[], api_messages=[], api_kwargs={},
        active_system_prompt=None, conversation_history=None, api_call_count=1, effective_task_id=None, turn_id=None,
        api_request_id=None, api_start_time=0.0, retry_count=0, max_retries=3)
    assert verdict.action == 'return'
    return verdict.result['final_response']


def _hermes_stream_response(chunks):
    """Assemble streamed chunks the way Hermes' chat-completions stream loop does (its tool-call accumulator,
    delta.refusal collection and _finish_chat_stream), so the test reads what core would act on."""
    from types import SimpleNamespace
    from agent.chat_completion_helpers import _StreamingCall
    try:  # core moved the accumulator into its own module (hermes-agent 208fd6e)
        from agent.chat_completion_helpers_tool_calls import _ToolCallAccumulator
    except ImportError:
        from agent.chat_completion_helpers import _ToolCallAccumulator
    content, refusal, finish, acc = [], [], None, _ToolCallAccumulator()
    for chunk in chunks:
        choice = chunk.choices[0]
        finish = choice.finish_reason or finish
        delta = choice.delta
        if delta.content:
            content.append(delta.content)
        if isinstance(getattr(delta, 'refusal', None), str) and delta.refusal:
            refusal.append(delta.refusal)
        for tc in delta.tool_calls or ():
            acc.feed(tc)
    owner = SimpleNamespace(agent=SimpleNamespace(), _assemble_tool_calls=_StreamingCall._assemble_tool_calls)
    return _StreamingCall._finish_chat_stream(
        owner, SimpleNamespace(response=None), 'assistant', content, [], acc.materialize(), finish, 'sonnet', None,
        flush_pending=lambda: None, refusal_parts=refusal)


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('details, refusal', [
    ({'category': 'cyber', 'explanation': 'The request was declined.'}, 'The request was declined.'),
    ({'category': 'cyber', 'explanation': None}, 'provider refusal category: cyber'),
])
def test_refusal_after_a_tool_call_reaches_the_user(tmp_path, stream, details, refusal):
    """Claude refuses after it started a tool call. Hermes must not run the cut-off call AND must show the refusal's
    reason. With the call left in tool_calls Hermes' normalizer keeps content empty (a refusal is promoted only when
    it is the sole payload) and the user reads "the model returned no explanation"."""
    from agent.transports import get_transport
    usage = {'input_tokens':0, 'output_tokens':0, 'cache_read_input_tokens':0, 'cache_creation_input_tokens':0}
    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.end_headers()
            events = [
                {'type':'message_start','message':{'id':'first','role':'assistant','model':'sonnet','content':[], 'usage':usage}},
                {'type':'content_block_start','index':0,'content_block':{'type':'tool_use','id':'toolu_1','name':'mcp__hermes__list_things','input':{}}},
                {'type':'content_block_delta','index':0,'delta':{'type':'input_json_delta','partial_json':''}},
                {'type':'content_block_stop','index':0},
                {'type':'message_delta','delta':{'stop_reason':'refusal','stop_details':details},'usage':usage},
                {'type':'message_stop'},
            ]
            self.wfile.write(''.join('data: '+json.dumps(e)+'\n\n' for e in events).encode())
    peer=ThreadingHTTPServer(('127.0.0.1',0),Peer)
    thread=threading.Thread(target=peer.serve_forever,daemon=True); thread.start()
    native=tmp_path/'native.py'; native.write_text(NATIVE)
    client=directsdk.Client(command=[sys.executable,str(native)],env={'PATH':os.defpath,'HOME':str(tmp_path),'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{peer.server_port}'})
    tools=[{'type':'function','function':{'name':'list_things','description':'list','parameters':{'type':'object','properties':{}}}}]
    try:
        request = dict(model='sonnet', messages=[{'role':'user','content':'fixture'}], tools=tools)
        if stream:
            chunks = list(client.create(**request, stream=True))
            last = chunks[-1].choices[0]
            assert last.finish_reason == 'content_filter' and last.delta.refusal == refusal
            assert not any(c.choices[0].delta.tool_calls for c in chunks)  # nothing for Hermes to run
            carrier = last.delta.reasoning_details[0]
            response = _hermes_stream_response(chunks)
        else:
            response = client.create(**request)
            message = response.choices[0].message
            assert response.choices[0].finish_reason == 'content_filter' and message.refusal == refusal
            assert not message.tool_calls  # nothing for Hermes to run
            carrier = message.reasoning_details[0]
        # The signed native turn is kept whole, refused tool_use included.
        native_turn = carrier['messages'][0]
        assert native_turn['stop_reason'] == 'refusal'
        assert [b['type'] for b in native_turn['content']] == ['tool_use'] and native_turn['content'][0]['id'] == 'toolu_1'
        normalized = get_transport('chat_completions').normalize_response(response)
        assert normalized.finish_reason == 'content_filter' and not normalized.tool_calls and normalized.content == refusal
        shown = _hermes_reply(response)
        assert shown.endswith('Provider said: ' + refusal)
        assert 'no explanation' not in shown
    finally:
        client.close(); peer.shutdown(); thread.join(); peer.server_close()


def test_empty_tool_input_completes_the_capture(tmp_path):
    """A no-argument tool call streams an empty input_json_delta; the capture must still complete."""
    calls = []
    usage = {'input_tokens':0, 'output_tokens':0, 'cache_read_input_tokens':0, 'cache_creation_input_tokens':0}
    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            calls.append(self.path)
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.end_headers()
            events = [
                {'type':'message_start','message':{'id':'first','role':'assistant','model':'sonnet','content':[], 'usage':usage}},
                {'type':'content_block_start','index':0,'content_block':{'type':'tool_use','id':'toolu_1','name':'mcp__hermes__list_things','input':{}}},
                {'type':'content_block_delta','index':0,'delta':{'type':'input_json_delta','partial_json':''}},
                {'type':'content_block_stop','index':0},
                {'type':'message_delta','delta':{'stop_reason':'tool_use'},'usage':usage},
                {'type':'message_stop'},
            ]
            self.wfile.write(''.join('data: '+json.dumps(e)+'\n\n' for e in events).encode())
    peer=ThreadingHTTPServer(('127.0.0.1',0),Peer)
    thread=threading.Thread(target=peer.serve_forever,daemon=True); thread.start()
    native=tmp_path/'native.py'; native.write_text(NATIVE)
    tools=[{'type':'function','function':{'name':'list_things','description':'list','parameters':{'type':'object','properties':{}}}}]
    client=directsdk.Client(command=[sys.executable,str(native)],env={'PATH':os.defpath,'HOME':str(tmp_path),'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{peer.server_port}'})
    try:
        result=client.create(model='sonnet',messages=[{'role':'user','content':'fixture'}],tools=tools)
        assert len(calls)==1
        call=result.choices[0].message.tool_calls[0]
        assert call.function.name=='list_things'
        assert json.loads(call.function.arguments)=={}
        assert result.choices[0].finish_reason=='tool_calls'
    finally:
        client.close(); peer.shutdown(); thread.join(); peer.server_close()


def test_cancel_closes_the_active_upstream_socket(tmp_path, monkeypatch):
    entered, disconnected = threading.Event(), threading.Event()
    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            entered.set()
            self.close_connection = True
            if upstream_closed(self.rfile):
                disconnected.set()
    spawned = []
    spawn = directsdk.Request.spawn
    monkeypatch.setattr(directsdk.Request, 'spawn', lambda self, *a, **k: spawned.append(spawn(self, *a, **k)) or spawned[-1])
    peer = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    native = tmp_path / 'native.py'
    native.write_text(NATIVE)
    client = directsdk.Client(command=[sys.executable, str(native)], env={'PATH':os.defpath, 'HOME':str(tmp_path), 'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{peer.server_port}'})
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(client.create, model='sonnet', messages=[{'role':'user', 'content':'fixture'}])
            try:
                assert entered.wait(5)
            finally:
                client.cancel()
            with pytest.raises(RuntimeError, match='cancelled'):
                result.result(timeout=3)
            assert disconnected.wait(2)
            assert spawned and all(process.wait(timeout=5) is not None for process in spawned)
    finally:
        client.close(); peer.shutdown(); thread.join(); peer.server_close()


class _Read:
    def __init__(self, outcome): self.outcome = outcome
    def read(self, size):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


@pytest.mark.parametrize('outcome', [
    b'',
    ConnectionResetError(10054, 'An existing connection was forcibly closed by the remote host'),
    ConnectionAbortedError(10053, 'An established connection was aborted by the software in your host machine'),
], ids=['eof', 'winerror-10054', 'winerror-10053'])
def test_upstream_closed_counts_eof_reset_and_abort_as_closed(outcome):
    # Windows often reports the relay's teardown as 10054/10053 rather than EOF; both are a closed socket.
    assert upstream_closed(_Read(outcome)) is True


def test_upstream_closed_does_not_swallow_unrelated_errors():
    with pytest.raises(TimeoutError):
        upstream_closed(_Read(TimeoutError('timed out')))
    with pytest.raises(OSError):
        upstream_closed(_Read(OSError(22, 'invalid argument')))


def test_upstream_closed_sees_a_real_reset():
    import socket, struct
    left, right = socket.socketpair()
    with left, right:
        left.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))  # close() sends RST
        left.close()
        with right.makefile('rb') as rfile:
            assert upstream_closed(rfile) is True


def test_upstream_closed_never_passes_a_socket_that_is_still_open():
    import socket
    left, right = socket.socketpair()
    with left, right, right.makefile('rb') as rfile:
        verdict, done = [], threading.Event()
        watcher = threading.Thread(target=lambda: (verdict.append(upstream_closed(rfile)), done.set()), daemon=True)
        watcher.start()
        assert not done.wait(0.5)  # Open and silent: never reported closed.
        left.sendall(b'x')
        assert done.wait(5) and verdict == [False]  # Open and talking: not closed either.
        watcher.join(5)


def test_incomplete_upstream_error_names_the_first_attempt(tmp_path):
    """Native's retries are denied with ADMISSION_CONSUMED; the raised error must carry the first attempt's status."""
    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            body=b'{"type":"error","error":{"type":"invalid_request_error","message":"prompt is too long: 213000 tokens > 200000 maximum"}}'
            self.send_response(529); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    peer=ThreadingHTTPServer(('127.0.0.1',0),Peer)
    thread=threading.Thread(target=peer.serve_forever,daemon=True); thread.start()
    native=tmp_path/'native.py'; native.write_text(NATIVE)
    client=directsdk.Client(command=[sys.executable,str(native)],env={'PATH':os.defpath,'HOME':str(tmp_path),'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{peer.server_port}'})
    try:
        with pytest.raises(RuntimeError, match=r'status 529, capture incomplete.*upstream said: prompt is too long: 213000 tokens'):
            client.create(model='sonnet',messages=[{'role':'user','content':'fixture'}])
    finally:
        client.close(); peer.shutdown(); thread.join(); peer.server_close()


@pytest.mark.parametrize('status', [429, 401, 400, 500, 529, 200])
def test_incomplete_upstream_error_carries_the_first_status(tmp_path, status):
    """Hermes routes on status_code: the first attempt's real HTTP status, none for a 200 cut short."""
    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            if status == 200:
                # Headers and message_start, then the connection drops: no complete capture.
                self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.end_headers()
                self.wfile.write(b'data: {"type":"message_start","message":{"id":"cut","role":"assistant","content":[]}}\n\n')
                return
            body=b'{"type":"error","error":{"type":"api_error","message":"fixture"}}'
            self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    peer=ThreadingHTTPServer(('127.0.0.1',0),Peer)
    thread=threading.Thread(target=peer.serve_forever,daemon=True); thread.start()
    native=tmp_path/'native.py'; native.write_text(NATIVE)
    client=directsdk.Client(command=[sys.executable,str(native)],env={'PATH':os.defpath,'HOME':str(tmp_path),'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{peer.server_port}'})
    try:
        with pytest.raises(RuntimeError, match=rf'^Incomplete upstream response \(first upstream attempt: status {status}, capture incomplete') as raised:
            client.create(model='sonnet',messages=[{'role':'user','content':'fixture'}])
        assert raised.value.status_code == (None if status == 200 else status)
    finally:
        client.close(); peer.shutdown(); thread.join(); peer.server_close()


def test_invalid_stream_json_error_names_the_offending_line(tmp_path):
    """A native that prints a non-JSON stdout line (a shim banner) fails with that line in the error, not a bare label."""
    native=tmp_path/'native.py'; native.write_text("import sys\nprint('mise WARN tool not activated')\nsys.exit(0)\n")
    client=directsdk.Client(command=[sys.executable,str(native)],env={'PATH':os.defpath,'HOME':str(tmp_path),'ANTHROPIC_BASE_URL':'http://127.0.0.1:9'})
    try:
        with pytest.raises(RuntimeError, match=r"Invalid native stream-json output: 'mise WARN tool not activated"):
            client.create(model='sonnet',messages=[{'role':'user','content':'fixture'}])
    finally:
        client.close()


def test_silent_upstream_fails_fast_instead_of_hanging(tmp_path, monkeypatch):
    """Headers, one event, then silence (a dead path that keeps TCP ESTAB): the relay must error out
    within the idle bound, not wait out the request timeout while Hermes sees nothing."""
    import time
    import admission
    monkeypatch.setattr(admission, 'UPSTREAM_IDLE_SECONDS', 1)
    release = threading.Event()
    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.end_headers()
            start = {'type':'message_start','message':{'id':'m','role':'assistant','model':'sonnet','content':[],'usage':{'input_tokens':0,'output_tokens':0}}}
            self.wfile.write(('data: '+json.dumps(start)+'\n\n').encode()); self.wfile.flush()
            release.wait(60)
    peer=ThreadingHTTPServer(('127.0.0.1',0),Peer)
    thread=threading.Thread(target=peer.serve_forever,daemon=True); thread.start()
    native=tmp_path/'native.py'; native.write_text(NATIVE.replace('timeout=5', 'timeout=60'))
    client=directsdk.Client(command=[sys.executable,str(native)],env={'PATH':os.defpath,'HOME':str(tmp_path),'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{peer.server_port}'})
    began = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match=r'status 200, capture incomplete, relay failure UpstreamIdle'):
            client.create(model='sonnet',messages=[{'role':'user','content':'fixture'}],timeout=20)
        assert time.monotonic() - began < 10
    finally:
        release.set(); client.close(); peer.shutdown(); thread.join(); peer.server_close()


def test_pings_keep_a_slow_upstream_alive(tmp_path, monkeypatch):
    """Long thinking streams only pings for a while; bytes inside the idle bound must never fail it."""
    import time
    import admission
    monkeypatch.setattr(admission, 'UPSTREAM_IDLE_SECONDS', 1)
    usage = {'input_tokens':0, 'output_tokens':0}
    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.end_headers()
            send = lambda e: (self.wfile.write(('data: '+json.dumps(e)+'\n\n').encode()), self.wfile.flush())
            send({'type':'message_start','message':{'id':'first','role':'assistant','model':'sonnet','content':[],'usage':usage}})
            for _ in range(6):  # 3 s of pings, three times the patched idle bound
                time.sleep(.5); send({'type':'ping'})
            send({'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}})
            send({'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'FIRST'}})
            send({'type':'content_block_stop','index':0})
            send({'type':'message_delta','delta':{'stop_reason':'end_turn'},'usage':usage}); send({'type':'message_stop'})
    peer=ThreadingHTTPServer(('127.0.0.1',0),Peer)
    thread=threading.Thread(target=peer.serve_forever,daemon=True); thread.start()
    native=tmp_path/'native.py'; native.write_text(NATIVE.replace('timeout=5', 'timeout=60'))
    client=directsdk.Client(command=[sys.executable,str(native)],env={'PATH':os.defpath,'HOME':str(tmp_path),'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{peer.server_port}'})
    try:
        assert client.create(model='sonnet',messages=[{'role':'user','content':'fixture'}],timeout=20).choices[0].message.content == 'FIRST'
    finally:
        client.close(); peer.shutdown(); thread.join(); peer.server_close()


def test_abort_closes_sockets_where_shutdown_cannot_wake_a_blocked_recv(monkeypatch):
    """Windows: shutdown() does not wake a recv blocked in another thread (the relay waiting on a hung
    upstream), so close() would wait for the upstream; closesocket() cancels it. POSIX keeps shutdown only."""
    import admission
    calls = []
    class Sock:
        def shutdown(self, how):
            calls.append('shutdown')
        def detach(self):
            calls.append('detach')
            return -1  # no real handle behind the fake
    for windows, expected in ((False, ['shutdown']), (True, ['shutdown', 'detach'])):
        monkeypatch.setattr(admission, '_CANCEL_BY_CLOSE', windows, raising=False)
        gate = admission.Admission('https://api.anthropic.com', 5)
        try:
            calls.clear()
            gate.sockets.add(Sock())
            gate.abort()
            assert calls == expected
        finally:
            gate.sockets.clear()
            gate.close()



# The close path is production only on Windows; Linux exercises it too. Not macOS: its poll() can lose the
# shutdown() wakeup when the fd is closed under a waiting reader, which then sleeps out the 30 s socket timeout.
_CLOSE_FLAGS = sorted({__import__('admission')._CANCEL_BY_CLOSE} | (set() if sys.platform == 'darwin' else {True}))


@pytest.mark.parametrize('close_flag', _CLOSE_FLAGS)
def test_abort_wakes_a_real_getresponse_blocked_on_a_silent_upstream(monkeypatch, close_flag):
    """A real http.client read (which holds makefile() refs, so socket.close() alone never reaches the OS)
    blocked on an upstream that accepts and never answers must end promptly after abort()."""
    import admission, http.client, socket, time
    monkeypatch.setattr(admission, '_CANCEL_BY_CLOSE', close_flag)
    entered, target = threading.Event(), []
    real_readinto = socket.SocketIO.readinto
    def readinto(self, buffer):
        if target and self._sock is target[0]:
            entered.set()  # the reader is about to block in recv on the upstream socket
        return real_readinto(self, buffer)
    monkeypatch.setattr(socket.SocketIO, 'readinto', readinto)
    listener = socket.create_server(('127.0.0.1', 0))
    listener.settimeout(5)
    gate = admission.Admission('https://api.anthropic.com', 30)
    conn = http.client.HTTPConnection('127.0.0.1', listener.getsockname()[1], timeout=30)
    peer = reader = None
    try:
        conn.request('POST', '/v1/messages', b'{}')
        peer, _ = listener.accept()  # held open and silent: the upstream never answers
        target.append(conn.sock)
        gate.sockets.add(conn.sock)
        outcome = []
        def read():
            try:
                outcome.append(conn.getresponse())
            except Exception as error:
                outcome.append(error)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        assert entered.wait(5), 'the reader never reached the blocking read'
        time.sleep(.2)  # let it pass from readinto() into the recv syscall
        assert reader.is_alive() and not outcome, 'the upstream never answers; the read must still be blocked'
        started = time.monotonic()
        gate.abort()
        reader.join(3)
        assert not reader.is_alive() and time.monotonic() - started < 3
        assert outcome and isinstance(outcome[0], Exception)
    finally:
        gate.sockets.clear()
        gate.close()
        if peer is not None:
            peer.close()  # EOF wakes a reader the abort failed to wake
        conn.close()
        listener.close()
        if reader is not None:
            reader.join(5)
