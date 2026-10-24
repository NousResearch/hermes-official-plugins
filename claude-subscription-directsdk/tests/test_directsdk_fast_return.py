"""Result-event completion through fake natives and the real admission relay."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import directsdk


NATIVE = r'''
import json, os, sys, time, urllib.request, urllib.error
from pathlib import Path
cfg = json.loads(os.environ['FIXTURE'])
root = Path(cfg['root'])
for line in sys.stdin:
    if not json.loads(line).get('shouldQuery', True):
        print(json.dumps({'type': 'result', 'num_turns': 0}), flush=True)
        continue
    break
url = os.environ['ANTHROPIC_BASE_URL'] + '/v1/messages'
def query():
    req = urllib.request.Request(url, data=json.dumps({'model': 'sonnet', 'stream': True,
        'messages': [{'role': 'user', 'content': 'fixture'}]}).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer fake-key'})
    try:
        return urllib.request.urlopen(req, timeout=2).read().decode()
    except urllib.error.HTTPError as e:
        return e.read().decode()
if not cfg.get('unused'):
    query()
if cfg.get('denied'):
    query()
usage = {'input_tokens': 3, 'output_tokens': 2}
message = {'role': 'assistant', 'content': [{'type': 'text', 'text': 'FIRST'}], 'usage': usage}
print(json.dumps({'type': 'assistant', 'message': message,
    **({'error': 'server_error'} if cfg.get('native_error') else {})}), flush=True)
print(json.dumps({'type': 'stream_event', 'event': {'type': 'content_block_delta',
    'delta': {'type': 'text_delta', 'text': 'FIRST'}}}), flush=True)
if not cfg.get('no_stop'):
    print(json.dumps({'type': 'stream_event', 'event': {'type': 'message_stop'}}), flush=True)
if cfg.get('before_result'):
    time.sleep(30)
print(json.dumps({'type': 'result', 'subtype': cfg.get('subtype', 'success'),
    'is_error': cfg.get('is_error', False), 'usage': usage,
    'total_cost_usd': 0.25, 'modelUsage': {'sonnet': {'costUSD': 0.25}}}), flush=True)
(root / 'result').touch()
if cfg.get('flood'):
    for _ in range(256):
        print(json.dumps({'type': 'system', 'tail': 'x' * 1024}), flush=True)
        print('x' * 1024, file=sys.stderr, flush=True)
    (root / 'drained').touch()
if cfg.get('sleep'):
    time.sleep(30)
else:
    deadline = time.monotonic() + 30
    while not (root / 'release').exists() and time.monotonic() < deadline:
        if (root / 'late').exists() and not (root / 'late-response').exists():
            (root / 'late-response.tmp').write_text(query())
            os.replace(root / 'late-response.tmp', root / 'late-response')  # the test reads it once it exists
        time.sleep(0.01)
sys.exit(cfg.get('exitcode', 0))
'''

REQUEST = {'model': 'sonnet', 'messages': [{'role': 'user', 'content': 'fixture'}]}


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), 'fixture did not reach the expected state'


@pytest.fixture
def native_case(tmp_path, monkeypatch):
    """Observe process ownership without replacing admission/capture with mocks."""
    spawn = directsdk.Request.spawn
    kill = directsdk.kill_process_tree
    active = None

    class Process:
        def __init__(self, process, state):
            self.process, self.state = process, state

        def __getattr__(self, name):
            if name == 'returncode':
                self.state.returncode_reads.append(threading.current_thread())
                assert not self.state.forbid_returncode, 'fast path read the native exit code'
            return getattr(self.process, name)

        def wait(self, *args, **kwargs):
            if active_gate := self.state.reaper_gate:
                if threading.current_thread() is not self.state.owner:
                    assert active_gate.wait(4), 'foreign reaper never collected native'
            return self.process.wait(*args, **kwargs)

    def observed_spawn(request, *args, **kwargs):
        real = spawn(request, *args, **kwargs)
        state = active
        state.owner = threading.current_thread()
        state.real, state.request, state.admission = real, request, request.admission
        state.process = Process(real, state)
        request.process = state.process
        return state.process

    monkeypatch.setattr(directsdk.Request, 'spawn', observed_spawn)

    @contextmanager
    def case(*, forbid_returncode=False, reaper_gate=None, **config):
        nonlocal active
        root = tmp_path / str(len(list(tmp_path.iterdir())))
        root.mkdir()
        state = SimpleNamespace(root=root, real=None, process=None, owner=None,
                                calls=[], returncode_reads=[], forbid_returncode=forbid_returncode,
                                reaper_gate=reaper_gate)
        active = state
        usage = {'input_tokens': 3, 'output_tokens': 2, 'cache_read_input_tokens': 7,
                 'cache_creation_input_tokens': 5}

        class Peer(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                state.calls.append(self.path)
                self.send_response(config.get('status', 200))
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                if config.get('cut'):
                    # A tool call whose only argument delta cannot be parsed: desktop keeps the block with an
                    # unparsed-input marker and answers it locally; the relay drops it at message_stop (#164).
                    events = [
                        {'type': 'message_start', 'message': {'id': 'captured', 'role': 'assistant',
                            'content': [], 'usage': usage}},
                        {'type': 'content_block_start', 'index': 0,
                         'content_block': {'type': 'text', 'text': 'FIRST'}},
                        {'type': 'content_block_start', 'index': 1,
                         'content_block': {'type': 'tool_use', 'id': 'toolu_1', 'name': 'mcp__hermes__lookup', 'input': {}}},
                        {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': '{"path":'}},
                        {'type': 'content_block_stop', 'index': 1},
                        {'type': 'content_block_stop', 'index': 0},
                        {'type': 'message_delta', 'delta': {'stop_reason': 'max_tokens'}, 'usage': usage},
                    ]
                else:
                    block = ({'type': 'tool_use', 'id': 'toolu_1', 'name': 'mcp__hermes__lookup',
                              'input': {'q': 'fixture'}} if config.get('tool') else
                             {'type': 'text', 'text': 'FIRST'})
                    events = [
                        {'type': 'message_start', 'message': {'id': 'captured', 'role': 'assistant',
                            'content': [], 'usage': usage}},
                        {'type': 'content_block_start', 'index': 0, 'content_block': block},
                        {'type': 'content_block_stop', 'index': 0},
                        {'type': 'message_delta', 'delta': {'stop_reason': config.get('stop_reason',
                            'tool_use' if config.get('tool') else 'end_turn')}, 'usage': usage},
                    ]
                if not config.get('incomplete'):
                    events.append({'type': 'message_stop'})
                self.wfile.write(''.join('data: ' + json.dumps(e) + '\n\n' for e in events).encode())

        peer = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
        thread = threading.Thread(target=peer.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        native = root / 'native.py'
        # Tool captures carry no text, so do not emit a mismatched incremental text delta.
        native.write_text(NATIVE.replace("'text': 'FIRST'}}}), flush=True)",
                                        "'text': ''}}}), flush=True)") if config.get('tool') else NATIVE)
        state.client = directsdk.Client(command=[sys.executable, str(native)], timeout=2,
            env={'PATH': os.defpath, 'HOME': str(root), 'FIXTURE': json.dumps({'root': str(root), **config}),
                 'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{peer.server_port}'})
        try:
            yield state
        finally:
            state.client.close()
            if state.real is not None:
                kill(state.real)
                if reaper_gate:
                    reaper_gate.set()
                state.real.wait(timeout=3)
                wait_for(lambda: state.process not in getattr(directsdk, '_draining', ()))
            peer.shutdown()
            thread.join(timeout=3)
            peer.server_close()

    return case


def test_result_returns_before_eof_and_grace_kills_native(native_case, monkeypatch):
    monkeypatch.setattr(directsdk, 'DRAIN_GRACE_S', .5, raising=False)
    with native_case(sleep=True, flood=True) as state:
        started = time.monotonic()
        chunks = list(state.client.create(**REQUEST, stream=True))
        assert time.monotonic() - started < 1
        assert chunks[-1].choices[0].finish_reason == 'stop'
        assert chunks[-1].usage.prompt_tokens == 15
        assert chunks[-1].usage.native_cost.total_cost_usd == .25
        assert chunks[-1].usage.native_cost.modelUsage.sonnet.costUSD == .25
        assert state.real.poll() is None
        assert state.request.process is None and state.request.admission is None
        wait_for(lambda: (state.root / 'drained').exists())  # stdout AND stderr stay drained
        wait_for(lambda: state.real.poll() is not None)
        assert state.real.returncode != 0
        wait_for(lambda: state.process not in directsdk._draining)
        assert state.real.stdout.closed and state.real.stderr.closed
        assert not state.admission.thread.is_alive()


def test_max_turns_tools_return_without_reading_exit_code(native_case):
    with native_case(tool=True, subtype='error_max_turns', is_error=True, exitcode=1,
                     forbid_returncode=True) as state:
        result = state.client.create(**REQUEST)
        assert result.choices[0].finish_reason == 'tool_calls'
        call = result.choices[0].message.tool_calls[0]
        assert call.function.name == 'lookup' and json.loads(call.function.arguments) == {'q': 'fixture'}
        assert not state.returncode_reads
        (state.root / 'release').touch()
        wait_for(lambda: state.process not in directsdk._draining)
        assert state.real.wait(timeout=1) == 1


def test_a_tool_call_cut_at_max_tokens_returns_finish_length(native_case):
    """#164: native keeps the unparsable call and answers it locally (error_max_turns, exit 1), but the
    capture holds only the text: the answer reaches Hermes as finish length, not a failed turn."""
    with native_case(cut=True, subtype='error_max_turns', is_error=True, exitcode=1,
                     forbid_returncode=True) as state:
        result = state.client.create(**REQUEST)
        assert result.choices[0].finish_reason == 'length'
        assert result.choices[0].message.content == 'FIRST'
        assert result.choices[0].message.tool_calls is None
        assert not state.returncode_reads
        (state.root / 'release').touch()
        wait_for(lambda: state.process not in directsdk._draining)
        assert state.real.wait(timeout=1) == 1


def test_a_cut_call_at_max_turns_returns_finish_length_after_native_exits(native_case):
    """Same turn without fast eligibility (a native retry is denied): the exit code is read, the
    boundary still behaves like a turn with calls, and the retry stays gated."""
    with native_case(cut=True, subtype='error_max_turns', is_error=True, exitcode=1,
                     denied=True) as state, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(state.client.create, **REQUEST)
        try:
            wait_for(lambda: (state.root / 'result').exists())
            time.sleep(.05)
            assert not future.done()
        finally:
            (state.root / 'release').touch()
        result = future.result(timeout=3)
        assert result.choices[0].finish_reason == 'length'
        assert result.choices[0].message.content == 'FIRST'
        assert result.choices[0].message.tool_calls is None
        assert state.real.poll() == 1
        assert len(state.calls) == 1 and state.admission.denied == 1


@pytest.mark.skipif(not hasattr(os, 'killpg'), reason='POSIX foreign-child reaper (waitpid(-1, WNOHANG), process groups)')
def test_foreign_reaper_is_harmless_during_drain(native_case, monkeypatch):
    reaped, stop = threading.Event(), threading.Event()
    with native_case(reaper_gate=reaped, forbid_returncode=True) as state:
        errors, signals = [], []
        killpg = os.killpg

        def record_signal(*args):
            signals.append((args, reaped.is_set()))
            return killpg(*args)

        monkeypatch.setattr(os, 'killpg', record_signal)

        def reap():
            try:
                while not stop.is_set():
                    try:
                        pid, _ = os.waitpid(-1, os.WNOHANG)
                    except ChildProcessError:
                        pid = 0
                    if state.real is not None and pid == state.real.pid:
                        reaped.set()
                    stop.wait(.005)
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=reap, daemon=True)
        thread.start()
        try:
            result = state.client.create(**REQUEST)
            assert result.choices[0].message.content == 'FIRST'
            (state.root / 'release').touch()
            assert reaped.wait(3)
            wait_for(lambda: state.process not in directsdk._draining)
            directsdk.kill_process_tree(state.process)
            # Signalling our own exited-but-unreaped group is safe (its PID still pins the group id); a signal after
            # the foreign reap could hit a recycled group.
            assert not errors and not [s for s, after_reap in signals if after_reap] and not state.returncode_reads
        finally:
            stop.set()
            reaped.set()
            thread.join(timeout=3)


def test_close_and_cancel_detach_only_after_result(native_case):
    for action in ('close', 'cancel'):
        with native_case() as state:
            stream = state.client.create(**REQUEST, stream=True)
            assert next(stream).choices[0].delta.content == 'FIRST'
            final = next(stream)
            assert final.choices[0].finish_reason == 'stop'
            stream.close() if action == 'close' else state.client.cancel()
            assert state.real.poll() is None
            assert not state.admission.cancelled
            (state.root / 'late').touch()
            wait_for(lambda: (state.root / 'late-response').exists())
            assert 'HERMES_MODEL_ADMISSION_CONSUMED' in (state.root / 'late-response').read_text()
            assert state.calls == ['/v1/messages']
            (state.root / 'release').touch()
            stream.close()
        with native_case(before_result=True) as state:
            stream = state.client.create(**REQUEST, stream=True)
            assert next(stream).choices[0].delta.content == 'FIRST'
            stream.close() if action == 'close' else state.client.cancel()
            wait_for(lambda: state.real.poll() is not None)
            assert state.real.returncode != 0 and state.admission.cancelled
            stream.close()


def test_ineligible_results_keep_eof_and_exit_checks(native_case):
    # Positive control makes this regression test red when fast return is removed.
    with native_case() as state:
        assert state.client.create(**REQUEST).choices[0].message.content == 'FIRST'
        assert state.request.process is None
        (state.root / 'release').touch()

    scenarios = [
        ({'subtype': 'error_other'}, 'Native request failed'),
        ({'native_error': True}, 'Native API error'),
        ({'unused': True}, None),
        ({'denied': True}, None),  # a success after a denied native retry still waits for exit
        ({'stop_reason': 'refusal'}, None),
        ({'is_error': True}, 'Native request failed'),
        ({'subtype': 'error_max_turns', 'exitcode': 1}, 'Native request failed'),
        ({'no_stop': True}, None),  # complete capture still owns message_stop on the slow path
        ({'incomplete': True}, 'Incomplete upstream response'),
        ({'status': 503}, 'Incomplete upstream response'),
        ({'exitcode': 1, 'unused': True}, 'Native request failed'),
    ]
    for config, error in scenarios:
        with native_case(**config) as state, ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(state.client.create, **REQUEST)
            try:
                wait_for(lambda: (state.root / 'result').exists())
                time.sleep(.05)
                assert not future.done(), config
                assert state.process not in directsdk._draining
            finally:
                (state.root / 'release').touch()
            if error:
                with pytest.raises(RuntimeError, match=error):
                    future.result(timeout=3)
            else:
                result = future.result(timeout=3)
                assert result.choices[0].finish_reason == ('content_filter' if config.get('stop_reason') == 'refusal' else 'stop')
            assert state.real.poll() is not None


def test_atexit_hook_kills_draining_native(native_case):
    with native_case(sleep=True) as state:
        assert state.client.create(**REQUEST).choices[0].message.content == 'FIRST'
        assert state.process in directsdk._draining and state.real.poll() is None
        directsdk._kill_draining()
        wait_for(lambda: state.real.poll() is not None)
        assert state.real.returncode != 0
        wait_for(lambda: state.process not in directsdk._draining)
