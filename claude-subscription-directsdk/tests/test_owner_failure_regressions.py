"""Synthetic ownership/cleanup failures, plus real macOS group membership controls."""
import errno
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

import directsdk as ds
from test_lifecycle_regressions import async_eventually, cli, eventually, living, query


class Raw:
    pid = 12345
    args = ['synthetic']
    returncode = None
    stdin = stdout = stderr = None

    def __init__(self):
        self.calls = []

    def poll(self):
        self.calls.append('raw-reap')
        self.returncode = 0
        return 0

    def communicate(self):
        return self.poll()

    def send_signal(self, *_):
        return self.poll()

    kill = terminate = send_signal


@pytest.mark.parametrize('name', ['communicate', 'send_signal', 'kill', 'terminate'])
def test_raw_reaping_methods_are_not_forwarded(name):
    raw = Raw()
    owner = ds._OwnedProcess(raw)
    with pytest.raises(AttributeError):
        getattr(owner, name)()
    assert not raw.calls and raw.returncode is None
    assert owner.pid == raw.pid and owner.args == raw.args


@pytest.mark.skipif(os.name == 'nt', reason='POSIX group ownership guard')
def test_external_reap_is_reported_without_signaling_stale_group(monkeypatch):
    raw = Raw()
    owner = ds._OwnedProcess(raw)
    raw.returncode = 0  # An external reaper violated the private ownership boundary.
    sent = []
    monkeypatch.setattr(os, 'killpg', lambda *a: sent.append(a))
    with pytest.raises(RuntimeError, match='reaped outside'):
        owner.poll()
    with pytest.raises(RuntimeError, match='reaped outside'):
        owner.kill_group()
    assert not sent


@pytest.mark.skipif(os.name == 'nt', reason='POSIX observer preflight')
def test_missing_observer_is_rejected_before_popen(monkeypatch):
    monkeypatch.setattr(ds, '_nonreaping_supported', lambda: False, raising=False)
    calls = []
    monkeypatch.setattr(ds.subprocess, 'Popen', lambda *a, **kw: calls.append(a) or Raw())
    request = ds.Request(SimpleNamespace())
    with pytest.raises(RuntimeError, match='non-reaping.*unsupported'):
        request.spawn(['synthetic'])
    assert not calls and request.process is None


@pytest.mark.skipif(os.name == 'nt', reason='POSIX legacy group guard')
def test_legacy_tag_cannot_enter_wait_lock_fallback(monkeypatch):
    lock = threading.Lock()
    lock.acquire()
    raw = Raw()
    raw._hermes_owned_group = True
    raw._waitpid_lock = lock
    errors, signals = [], []
    monkeypatch.setattr(os, 'killpg', lambda *a: signals.append(a))
    def cancel():
        try:
            ds.kill_process_tree(raw)
        except BaseException as e:
            errors.append(e)
    thread = threading.Thread(target=cancel)
    thread.start()
    try:
        thread.join(.15)
        blocked = thread.is_alive()
    finally:
        lock.release()
        thread.join(1)
    assert not blocked, 'cancel waited for the lock required by a blocking Popen.wait'
    assert errors and 'unsupported' in str(errors[0]) and not signals


@pytest.mark.skipif(os.name == 'nt', reason='POSIX non-reaping group ownership')
@pytest.mark.parametrize('inspection', ['nonempty', 'error'])
def test_denied_signal_is_visible_and_can_be_retried(monkeypatch, inspection):
    raw = Raw()
    owner = ds._OwnedProcess(raw)
    monkeypatch.setattr(os, 'waitid', lambda *a: SimpleNamespace(si_pid=raw.pid))
    def inspect(_):
        if inspection == 'error':
            raise OSError('synthetic inspection unavailable')
        return False
    monkeypatch.setattr(ds, '_darwin_group_empty', inspect, raising=False)
    calls = []
    def signal(*a):
        calls.append(a)
        if len(calls) == 1:
            raise PermissionError(errno.EPERM, 'synthetic denied signal')
    monkeypatch.setattr(os, 'killpg', signal)
    with pytest.raises(PermissionError):
        owner.kill_group()
    assert not raw.calls  # Failed signaling must never reap the leader.
    owner.kill_group()
    assert len(calls) == 2


@pytest.mark.skipif(sys.platform != 'darwin', reason='actual Darwin exception semantics')
def test_confirmed_empty_zombie_group_may_retire(monkeypatch):
    raw = Raw()
    owner = ds._OwnedProcess(raw)
    monkeypatch.setattr(os, 'waitid', lambda *a: SimpleNamespace(si_pid=raw.pid))
    monkeypatch.setattr(ds, '_darwin_group_empty', lambda _: True, raising=False)
    calls = []
    def denied(*a):
        calls.append(a)
        raise PermissionError(errno.EPERM, 'synthetic empty zombie group')
    monkeypatch.setattr(os, 'killpg', denied)
    owner.kill_group()
    owner.kill_group()
    assert len(calls) == 1


@pytest.mark.skipif(os.name == 'nt', reason='POSIX non-reaping group ownership')
def test_failed_poll_does_not_reap_until_group_signal_succeeds(monkeypatch):
    raw = Raw()
    owner = ds._OwnedProcess(raw)
    monkeypatch.setattr(os, 'waitid', lambda *a: SimpleNamespace(si_pid=raw.pid))
    monkeypatch.setattr(ds, '_darwin_group_empty', lambda _: False, raising=False)
    def denied(*a):
        raise PermissionError(errno.EPERM, 'synthetic denial')
    monkeypatch.setattr(os, 'killpg', denied)
    with pytest.raises(PermissionError):
        owner.poll()
    assert not raw.calls and raw.returncode is None
    monkeypatch.setattr(os, 'killpg', lambda *a: None)
    assert owner.poll() == 0 and raw.calls == ['raw-reap']


def test_cancel_attempts_group_kill_even_if_relay_abort_fails(monkeypatch):
    calls = []
    def abort():
        calls.append('abort')
        raise OSError('synthetic abort failure')
    request = ds.Request(SimpleNamespace())
    request.admission = SimpleNamespace(abort=abort)
    request.process = Raw()
    monkeypatch.setattr(ds, 'kill_process_tree', lambda _: calls.append('kill'))
    with pytest.raises(RuntimeError, match='cleanup'):
        request.cancel()
    assert request.cancelled.is_set() and calls == ['abort', 'kill']


@pytest.mark.parametrize('failure', ['cancel', 'relay_close', 'wait'])
def test_cleanup_exception_cannot_skip_writer_join(cli, monkeypatch, failure):
    client = cli.make()
    stream = client._create(**query(), stream=True)
    next(stream)
    request = stream.request
    p = request.process
    # Ensure real process and writer are already complete before synthetic failure injection.
    p.wait(timeout=2)
    eventually(lambda: not stream.iterator.gi_frame.f_locals['writer'].is_alive())
    writer = stream.iterator.gi_frame.f_locals['writer']
    joined = []
    original_join = writer.join
    monkeypatch.setattr(writer, 'join', lambda timeout=None: joined.append(timeout) or original_join(timeout))
    def fail(*a, **kw):
        raise OSError('synthetic ' + failure + ' failure')
    target, name = {'cancel': (request, 'cancel'), 'relay_close': (request.admission, 'close'),
                    'wait': (p, 'wait')}[failure]
    original = getattr(target, name)
    monkeypatch.setattr(target, name, fail)
    try:
        with pytest.raises(RuntimeError, match='cleanup'):
            list(stream)
        assert joined and all(isinstance(t, (int, float)) and t > 0 for t in joined)
        assert not client._requests
    finally:
        monkeypatch.setattr(target, name, original)
        stream.close()
        request.admission.close()


def test_cleanup_reports_stuck_relay_but_attempts_other_steps(cli, monkeypatch):
    client = cli.make()
    stream = client._create(**query(), stream=True)
    next(stream)
    p = stream.request.process
    p.wait(timeout=2)
    writer = stream.iterator.gi_frame.f_locals['writer']
    eventually(lambda: not writer.is_alive())
    entered, release = threading.Event(), threading.Event()
    gate = stream.request.admission
    original_close = gate.close
    def delayed_close():
        entered.set()
        assert release.wait(2)
        original_close()
    monkeypatch.setattr(gate, 'close', delayed_close)
    monkeypatch.setattr(ds, '_CLEANUP_STEP_SECONDS', .1, raising=False)
    joined = []
    original_join = writer.join
    monkeypatch.setattr(writer, 'join', lambda timeout=None: joined.append(timeout) or original_join(timeout))
    until = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match='cleanup') as failure:
            list(stream)
        assert time.monotonic() - until < .8
        assert entered.is_set() and joined
        assert failure.value.pending_threads
    finally:
        release.set()
        monkeypatch.setattr(gate, 'close', original_close)
        stream.close()
        gate.close()
        for t in threading.enumerate():
            if t.name.startswith('claude-directsdk-cleanup-'):
                t.join(2)
    assert not any(t.name.startswith('claude-directsdk-cleanup-') and t.is_alive()
                   for t in threading.enumerate())


def test_close_attempts_every_registered_request(monkeypatch):
    client = ds.Client(command=['synthetic'], env={'PATH': '', 'HOME': '/tmp'})
    calls = []
    class Registered:
        stream = None
        def __init__(self):
            self.cancelled = threading.Event()
        def cancel(self):
            calls.append(self)
            raise OSError('synthetic per-request cancel failure')
    requests = [Registered(), Registered()]
    client._requests.update(requests)
    with pytest.raises(RuntimeError, match='cleanup'):
        client.close()
    assert set(calls) == set(requests)


@pytest.mark.parametrize('operation', ['cancel', 'close'])
def test_all_registered_tokens_are_set_before_first_cleanup(tmp_path, operation):
    client = ds.Client(command=['synthetic'], env={'PATH': '', 'HOME': str(tmp_path)})
    observed = []
    class Registered:
        stream = None
        def __init__(self):
            self.cancelled = threading.Event()
        def cancel(self):
            # A blocked cleanup must not leave any other preparation free to spawn.
            observed.append(all(request.cancelled.is_set() for request in requests))
    requests = [Registered(), Registered()]
    client._requests.update(requests)
    getattr(client, operation)()
    assert observed == [True, True]


def test_rejected_executor_submission_releases_registration(tmp_path):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    client = ds.Client(command=['synthetic'], env={'PATH': '', 'HOME': str(tmp_path)})
    async def exercise():
        executor = ThreadPoolExecutor(max_workers=1)
        asyncio.get_running_loop().set_default_executor(executor)
        executor.shutdown(wait=True)
        with pytest.raises(RuntimeError, match='cannot schedule new futures'):
            await client._acreate(**query())
        assert not client._requests
    try:
        asyncio.run(exercise())
    finally:
        client.close()


@pytest.mark.skipif(sys.platform != 'darwin', reason='real macOS libproc, synthetic CLI')
def test_real_group_query_distinguishes_live_descendant(cli, monkeypatch):
    request = ds.Request(cli.make())
    p = request.spawn(cli.command, env=dict(cli.env, REVIEW_MODE='launcher_exit'),
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import json
    def records():
        path = cli.root / 'pids.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    try:
        eventually(lambda: len(records()) == 2)
        eventually(lambda: os.waitid(os.P_PID, p.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is not None)
        assert any(row['pid'] != p.pid and living(row['pid']) for row in records())
        with monkeypatch.context() as context:
            context.setattr(os, 'killpg', lambda *a: (_ for _ in ()).throw(
                PermissionError(errno.EPERM, 'synthetic denied actual owned group')))
            with pytest.raises(PermissionError):
                ds.kill_process_tree(p)
        ds.kill_process_tree(p)
        p.wait(timeout=2)
        eventually(lambda: not any(living(row['pid']) for row in records()))
    finally:
        # Keep the old implementation's failing test bounded; its leader is still unreaped.
        if p.returncode is None and any(row['pid'] != p.pid and living(row['pid']) for row in records()):
            os.killpg(p.pid, 9)
        ds.kill_process_tree(p)
        p.wait(timeout=2)


@pytest.mark.parametrize('cancel_again', [False, True])
def test_async_cleanup_wait_does_not_block_event_loop(tmp_path, monkeypatch, cancel_again):
    import asyncio
    client = ds.Client(command=['synthetic'], env={'PATH': '', 'HOME': str(tmp_path)})
    entered, release = threading.Event(), threading.Event()
    cancel_entered, cancel_release = threading.Event(), threading.Event()
    def preparation(request, kwargs):
        entered.set()
        release.wait(2)
        raise RuntimeError('Claude request cancelled')
    def cancel(request):
        request.cancelled.set()
        cancel_entered.set()
        cancel_release.wait(2)
    monkeypatch.setattr(client, '_prepare_request', preparation)
    monkeypatch.setattr(ds.Request, 'cancel', cancel)
    monkeypatch.setattr(ds, '_CLEANUP_STEP_SECONDS', .12)
    async def exercise():
        task = asyncio.create_task(client._acreate(**query()))
        await async_eventually(entered.is_set)
        start = time.monotonic()
        task.cancel()
        await async_eventually(cancel_entered.is_set)
        await asyncio.sleep(.015)  # An unrelated coroutine must remain schedulable.
        assert time.monotonic() - start < .08
        if cancel_again:
            task.cancel()
        with pytest.raises(RuntimeError, match='cleanup') as failure:
            await task
        assert failure.value.pending_threads and not client._requests
    try:
        asyncio.run(exercise())
    finally:
        release.set()
        cancel_release.set()
        for thread in threading.enumerate():
            if thread.name.startswith('claude-directsdk-cleanup-'):
                thread.join(2)


@pytest.mark.parametrize('operation', ['anext_cancel', 'aclose'])
def test_async_stream_cleanup_keeps_other_coroutines_schedulable(monkeypatch, operation):
    import asyncio
    advance_entered, advance_release = threading.Event(), threading.Event()
    close_entered, close_release = threading.Event(), threading.Event()
    class SyncStream:
        request = SimpleNamespace(cancelled=threading.Event(), process=None)
        def __next__(self):
            advance_entered.set()
            advance_release.wait(2)
            raise StopIteration
        def close(self):
            close_entered.set()
            close_release.wait(2)
    sync = SyncStream()
    stream = ds.AsyncStream(sync)
    monkeypatch.setattr(ds, '_CLEANUP_STEP_SECONDS', .12)
    async def exercise():
        if operation == 'anext_cancel':
            task = asyncio.create_task(stream.__anext__())
            await async_eventually(advance_entered.is_set)
            began = time.monotonic()
            task.cancel()
        else:
            began = time.monotonic()
            task = asyncio.create_task(stream.aclose())
        await async_eventually(close_entered.is_set)
        await asyncio.sleep(.015)
        assert time.monotonic() - began < .08
        with pytest.raises(RuntimeError, match='cleanup') as failure:
            await task
        assert failure.value.pending_threads and sync.request.cancelled.is_set()
    try:
        asyncio.run(exercise())
    finally:
        advance_release.set()
        close_release.set()
        for thread in threading.enumerate():
            if thread.name.startswith('claude-directsdk-cleanup-'):
                thread.join(2)


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin libproc protocol, synthetic replies')
@pytest.mark.parametrize('members,count,empty', [
    ([12345], 1, True), ([0], 1, False), ([12345, 12345], 2, False),
    ([12345, 12346], 2, False), (list(range(12345, 12361)), 16, False),
])
def test_group_confirmation_rejects_partial_or_invalid_results(monkeypatch, members, count, empty):
    import ctypes
    calls = []
    def query(leader, buffer, size):
        calls.append(leader)
        for i, value in enumerate(members):
            buffer[i] = value
        return count
    monkeypatch.setattr(ctypes, 'CDLL', lambda *a, **kw: SimpleNamespace(proc_listpgrppids=query))
    assert ds._darwin_group_empty(12345) is empty
    assert calls == [12345]


@pytest.mark.skipif(os.name == 'nt', reason='POSIX observer preflight')
@pytest.mark.parametrize('code', [errno.ENOSYS, errno.EINVAL, errno.EPERM])
def test_runtime_observer_rejection_is_detected_before_spawn(monkeypatch, code):
    def unavailable(*a):
        raise OSError(code, 'synthetic unavailable observer')
    monkeypatch.setattr(os, 'waitid', unavailable)
    calls = []
    monkeypatch.setattr(ds.subprocess, 'Popen', lambda *a, **kw: calls.append(a) or Raw())
    with pytest.raises(RuntimeError, match='unsupported'):
        ds.Request(SimpleNamespace()).spawn(['synthetic'])
    assert not calls


@pytest.mark.skipif(os.name == 'nt', reason='POSIX SIGCHLD ownership contract')
@pytest.mark.parametrize('handler', ['ignore', 'custom'])
def test_external_reaper_disposition_is_not_supported(monkeypatch, handler):
    import signal
    value = signal.SIG_IGN if handler == 'ignore' else lambda *_: None
    monkeypatch.setattr(signal, 'getsignal', lambda _: value)
    calls = []
    monkeypatch.setattr(ds.subprocess, 'Popen', lambda *a, **kw: calls.append(a) or Raw())
    with pytest.raises(RuntimeError, match='unsupported'):
        ds.Request(SimpleNamespace()).spawn(['synthetic'])
    assert not calls
