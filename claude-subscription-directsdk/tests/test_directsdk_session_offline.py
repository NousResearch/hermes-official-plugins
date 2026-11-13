"""Session-mode paths that need no Claude Code process: failures before a launch and errors the reader thread hands over."""
from collections import deque
from pathlib import Path
import queue
import socket
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import directsdk

TOOLS = [{'type': 'function', 'function': {'name': 'read_file', 'description': 'Read a file',
          'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']}}}]


@pytest.fixture
def first_step(tmp_path, monkeypatch):
    """A session-mode first step up to its launch; the command resolves but no test here lets it spawn."""
    monkeypatch.setattr(directsdk, 'apply_traffic_policy', lambda env: env)
    monkeypatch.setattr(directsdk, 'session_settings', lambda: (True, 1))
    client = directsdk.Client(command=[sys.executable, '-I', str(Path(__file__).parent / 'fixtures/lifecycle_cli.py')], timeout=2,
                              env={'HOME': str(tmp_path), 'PATH': '', 'ANTHROPIC_BASE_URL': 'http://127.0.0.1:9'})
    yield lambda: client.create(model='sonnet', messages=[{'role': 'user', 'content': 'go'}], tools=TOOLS)
    client.close()


def held():
    with directsdk._live_lock:
        return len(directsdk._live)


def test_a_control_write_the_reader_thread_gave_up_on_reaches_the_step_as_a_timeout():
    """The reader thread answers native's later tool calls itself. When that bounded write gives up, the step must
    see the timeout error, not the bad-output error every other reader failure maps to."""
    native = object.__new__(directsdk.Native)
    native.events, native.stderr = queue.Queue(), deque()
    native.events.put(TimeoutError('Claude request timed out'))
    with pytest.raises(TimeoutError, match='Claude request timed out'):
        native.receive(SimpleNamespace(cancelled=threading.Event()), 1)


def test_a_launch_that_fails_before_the_spawn_leaves_no_relay(first_step, monkeypatch):
    """The launch directory cannot be made (a full or read-only temp dir) after the relay is up: the relay's thread
    and listener must go with the failed launch, and so must its place against ``max_sessions``."""
    relays, make = [], directsdk.relay

    def recorded(*args, **kwargs):
        relays.append(make(*args, **kwargs))
        return relays[-1]

    def no_dir():
        raise OSError(28, 'No space left on device')
    monkeypatch.setattr(directsdk, 'relay', recorded)
    monkeypatch.setattr(directsdk, '_launch_dir', no_dir)
    monkeypatch.setattr(directsdk.Request, 'spawn', lambda *_, **__: pytest.fail('nothing may spawn'))
    with pytest.raises(OSError, match='No space left'):
        first_step()
    relay, = relays
    try:
        assert not relay.thread.is_alive(), 'the relay thread outlived the failed launch'
        with pytest.raises(OSError):
            socket.create_connection(('127.0.0.1', int(relay.url.split(':')[2].split('/')[0])), timeout=1).close()
        assert held() == 0, 'the failed launch kept its place'
    finally:
        if relay.thread.is_alive():
            relay.close()
        with directsdk._live_lock:
            directsdk._live.clear()

