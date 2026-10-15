"""Request ownership, bounded I/O, and group lifetime contracts; synthetic CLI only."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import socket
import sys
import threading
import time
from types import SimpleNamespace

import psutil
import pytest

import directsdk as ds
from admission import Admission


def eventually(predicate, timeout=2):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.01)
    pytest.fail('bounded condition did not become true')


async def async_eventually(predicate, timeout=2):
    until = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= until:
            pytest.fail('bounded async condition did not become true')
        await asyncio.sleep(.005)


def living(pid):
    try:
        return psutil.Process(pid).is_running() and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, 'apply_traffic_policy', lambda env: env)
    env = {'HOME': str(tmp_path), 'PATH': '', 'ANTHROPIC_BASE_URL': 'http://127.0.0.1:9',
           'REVIEW_PIDS': str(tmp_path / 'pids.jsonl'), 'REVIEW_STARTED': str(tmp_path / 'started'),
           'REVIEW_ENV': str(tmp_path / 'env.json')}
    command = [sys.executable, '-I', str(Path(__file__).parent / 'fixtures/lifecycle_cli.py')]
    clients = []
    initial = set(threading.enumerate())
    def make(mode='normal', timeout=2):
        client = ds.Client(command=command, env=dict(env, REVIEW_MODE=mode), timeout=timeout)
        clients.append(client)
        return client
    yield SimpleNamespace(make=make, env=env, command=command, root=tmp_path)
    for client in clients:
        client.close()
        assert not client._requests
    leftovers = [t for t in threading.enumerate() if t not in initial
                 and t.name.startswith('claude-directsdk-') and t.is_alive()]
    for t in leftovers:
        t.join(2)
    assert not any(t.is_alive() for t in leftovers), 'transport thread leaked'


def query(text='go', **kw):
    return dict(model='sonnet', messages=[{'role': 'user', 'content': text}], **kw)


@pytest.mark.parametrize('stage', ['request_body', 'prepare_history'])
@pytest.mark.parametrize('asynchronous', [False, True])
def test_preparation_cancel_never_spawns(cli, monkeypatch, stage, asynchronous):
    entered, release = threading.Event(), threading.Event()
    original = getattr(ds, stage)
    client = cli.make()
    def delayed(*a, **kw):
        entered.set()
        assert release.wait(3)
        return original(*a, **kw)
    monkeypatch.setattr(ds, stage, delayed)
    if asynchronous:
        async def exercise():
            task = asyncio.create_task(client._acreate(**query()))
            await async_eventually(entered.is_set)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.set()
        try:
            asyncio.run(exercise())
        finally:
            release.set()
    else:
        errors = []
        def work():
            try:
                client._create(**query())
            except BaseException as e:
                errors.append(e)
        thread = threading.Thread(target=work)
        thread.start()
        try:
            eventually(entered.is_set)
            client.cancel()
        finally:
            release.set()
            thread.join(3)
        assert not thread.is_alive()
        assert len(errors) == 1 and str(errors[0]) == 'Claude request cancelled'
    assert not (cli.root / 'started').exists()
    assert not client._requests


def test_async_cancel_is_request_scoped(cli, monkeypatch):
    client = cli.make()
    survivor = client._create(**query(), stream=True)
    entered, release = threading.Event(), threading.Event()
    original = ds.prepare_history
    def delayed(*a, **kw):
        entered.set()
        assert release.wait(3)
        return original(*a, **kw)
    monkeypatch.setattr(ds, 'prepare_history', delayed)
    async def exercise():
        task = asyncio.create_task(client._acreate(**query()))
        await async_eventually(entered.is_set)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert list(survivor)[-1]._response.choices[0].message.content == 'offline-ok'
    try:
        asyncio.run(exercise())
    finally:
        release.set()
        survivor.close()


def test_failed_preparation_releases_request(cli):
    client = cli.make()
    with pytest.raises(ValueError):
        client._create(model='sonnet', messages=[])
    assert not client._requests
    assert client._create(**query()).choices[0].message.content == 'offline-ok'


def test_partial_headers_idle_timeout():
    gate = Admission('http://127.0.0.1:9', .1)
    sock = socket.create_connection(('127.0.0.1', gate.server.server_port), timeout=.5)
    try:
        sock.sendall(b'POST / HTTP/1.1\r\nHost: synthetic\r\nUnfinished: ')
        assert sock.recv(1024) == b''
        eventually(lambda: not gate.sockets)
    finally:
        sock.close()
        gate.close()
    assert not gate.thread.is_alive() and gate.server.socket.fileno() == -1


@pytest.mark.parametrize('payload', [b'POST /', b'POST / HTTP/1.1\r\nHost: synthetic\r\nX: '])
def test_shutdown_interrupts_request_line_and_headers(payload):
    gate = Admission('http://127.0.0.1:9', 3)
    sock = socket.create_connection(('127.0.0.1', gate.server.server_port), timeout=.5)
    sock.sendall(payload)
    errors = []
    def close():
        try:
            gate.close()
        except BaseException as e:
            errors.append(e)
    thread = threading.Thread(target=close)
    try:
        eventually(lambda: bool(gate.sockets))
        thread.start()
        thread.join(.4)
        blocked = thread.is_alive()
    finally:
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
        sock.close()
        if thread.ident:
            thread.join(2)
        else:
            gate.close()
    assert not blocked, 'shutdown waited for incomplete headers'
    assert not errors and not thread.is_alive() and not gate.thread.is_alive()


def test_connection_accepted_after_abort_cannot_block_close():
    gate = Admission('http://127.0.0.1:9', 2)
    gate.abort()
    sock = socket.create_connection(('127.0.0.1', gate.server.server_port), timeout=.5)
    try:
        assert sock.recv(1024) == b''
    finally:
        sock.close()
        gate.close()
    assert not gate.thread.is_alive()


@pytest.mark.parametrize('cancel', [False, True])
def test_blocked_stdin_has_deadline_and_cancellation(cli, cancel):
    client = cli.make('no_read', timeout=2 if cancel else .3)
    errors = []
    def work():
        try:
            client._create(**query('x' * (2 * 1024 * 1024)))
        except BaseException as e:
            errors.append(e)
    thread = threading.Thread(target=work)
    started = time.monotonic()
    thread.start()
    try:
        eventually(lambda: (cli.root / 'started').exists())
        if cancel:
            time.sleep(.1)
            began = time.monotonic()
            client.cancel()
            assert time.monotonic() - began < .3
        thread.join(1.5)
        blocked = thread.is_alive()
    finally:
        client.cancel()
        thread.join(3)
    assert not blocked, 'write exceeded its bounded deadline'
    assert time.monotonic() - started < 2
    assert len(errors) == 1
    if cancel:
        assert isinstance(errors[0], RuntimeError) and str(errors[0]) == 'Claude request cancelled'
    else:
        assert isinstance(errors[0], TimeoutError)


def test_large_history_replay_and_eof(cli):
    client = cli.make()
    req = query('x' * (2 * 1024 * 1024))
    req['messages'] += [{'role': 'assistant', 'content': 'a'}, {'role': 'user', 'content': 'b'}]
    assert client._create(**req).choices[0].message.content == 'offline-ok'


@pytest.mark.skipif(os.name == 'nt' or not hasattr(os, 'waitid'), reason='POSIX WNOWAIT ownership contract')
@pytest.mark.parametrize('completion', ['cancel', 'wait', 'poll'])
def test_exited_owned_launcher_cleans_descendants_before_reap(cli, completion):
    request = ds.Request(cli.make())
    p = request.spawn(cli.command, env=dict(cli.env, REVIEW_MODE='launcher_exit'),
                      stdout=ds.subprocess.DEVNULL, stderr=ds.subprocess.DEVNULL)
    child = None
    def records():
        path = cli.root / 'pids.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    try:
        eventually(lambda: len(records()) == 2)
        child = next(row['pid'] for row in records() if row['pid'] != p.pid)
        eventually(lambda: os.waitid(os.P_PID, p.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is not None)
        assert living(child)
        if completion == 'cancel':
            request.cancel()
        elif completion == 'wait':
            assert p.wait(timeout=2) == 0
        else:
            assert p.poll() == 0
        eventually(lambda: not living(child))
    finally:
        # If the old runtime fails, rescue only this explicitly created fixture group.
        if child and living(child):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(p.pid, signal.SIGKILL)
        p.wait(timeout=2)
        request.cancel()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX group ownership guard')
def test_reaped_unowned_process_cannot_signal_reused_pid(monkeypatch):
    sent = []
    monkeypatch.setattr(os, 'killpg', lambda *a: sent.append(a))
    stale = SimpleNamespace(pid=12345, returncode=0, poll=lambda: 0)
    ds.kill_process_tree(stale)
    assert not sent


@pytest.mark.skipif(os.name == 'nt', reason='POSIX process group signaling')
def test_only_the_owned_group_is_signaled_once(cli, monkeypatch):
    request = ds.Request(cli.make())
    p = request.spawn(cli.command, env=dict(cli.env, REVIEW_MODE='no_read'),
                      stdout=ds.subprocess.DEVNULL, stderr=ds.subprocess.DEVNULL)
    sent = []
    original = os.killpg
    def record(group, sig):
        sent.append(group)
        return original(group, sig)
    monkeypatch.setattr(os, 'killpg', record)
    try:
        request.cancel()
        p.wait(timeout=2)
        request.cancel()
        ds.kill_process_tree(p)
        assert sent == [p.pid]
    finally:
        request.cancel()
        p.wait(timeout=2)


def test_cancellation_during_popen_construction_unwinds(cli, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    client = cli.make('no_read')
    original = ds.subprocess.Popen
    errors = []
    def delayed(*a, **kw):
        p = original(*a, **kw)
        entered.set()
        assert release.wait(3)
        return p
    monkeypatch.setattr(ds.subprocess, 'Popen', delayed)
    def work():
        try:
            client._create(**query())
        except BaseException as e:
            errors.append(e)
    owner = threading.Thread(target=work)
    canceller = threading.Thread(target=client.cancel)
    owner.start()
    try:
        eventually(entered.is_set)
        canceller.start()
        eventually(lambda: any(r.cancelled.is_set() for r in client._requests))
    finally:
        release.set()
        if canceller.ident:
            canceller.join(2)
        owner.join(2)
    assert not owner.is_alive() and not canceller.is_alive()
    assert len(errors) == 1 and str(errors[0]) == 'Claude request cancelled'


def test_async_cancel_before_executor_start_releases_registration(cli):
    from concurrent.futures import ThreadPoolExecutor
    client = cli.make()
    entered, release = threading.Event(), threading.Event()
    def hold():
        entered.set()
        release.wait(3)
    async def exercise():
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
        blocker = asyncio.create_task(asyncio.to_thread(hold))
        await async_eventually(entered.is_set)
        task = asyncio.create_task(client._acreate(**query()))
        await asyncio.sleep(.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await blocker
    try:
        asyncio.run(exercise())
    finally:
        release.set()
    assert not client._requests and not (cli.root / 'started').exists()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX group ownership guard')
def test_unowned_live_pid_is_not_a_group_ownership_token(monkeypatch):
    sent = []
    monkeypatch.setattr(os, 'killpg', lambda *a: sent.append(a))
    stranger = SimpleNamespace(pid=os.getpid(), returncode=None, poll=lambda: None)
    ds.kill_process_tree(stranger)
    assert not sent


@pytest.mark.skipif(os.name == 'nt', reason='POSIX inherited pipe and retained leader')
def test_exited_launcher_with_inherited_stdout_is_cleaned_on_timeout(cli):
    source = (Path(__file__).parent / 'fixtures/lifecycle_cli.py').read_text()
    source = source.replace('stdout=subprocess.DEVNULL,\n                     stderr=',
                            'stdout=None,\n                     stderr=')
    script = cli.root / 'inherited_pipe.py'
    script.write_text(source)
    client = cli.make('launcher_exit', timeout=.3)
    client.command = [sys.executable, '-I', str(script)]
    began = time.monotonic()
    with pytest.raises(TimeoutError):
        client._create(**query())
    assert time.monotonic() - began < 2, 'cleanup waited for a descendant holding stdout'
    records = [json.loads(line) for line in (cli.root / 'pids.jsonl').read_text().splitlines()]
    assert len(records) == 2
    eventually(lambda: not any(living(row['pid']) for row in records))


@pytest.mark.skipif(os.name == 'nt' or not hasattr(os, 'waitid'), reason='POSIX ownership serialization')
def test_concurrent_waiters_and_cancel_reap_only_after_group_cleanup(cli):
    request = ds.Request(cli.make())
    p = request.spawn(cli.command, env=dict(cli.env, REVIEW_MODE='no_read'),
                      stdout=ds.subprocess.DEVNULL, stderr=ds.subprocess.DEVNULL)
    results, errors = [], []
    def wait():
        try:
            results.append(p.wait(timeout=2))
        except BaseException as e:
            errors.append(e)
    waiters = [threading.Thread(target=wait) for _ in range(2)]
    for waiter in waiters:
        waiter.start()
    request.cancel()
    for waiter in waiters:
        waiter.join(2)
    assert not errors and len(results) == 2 and all(r == -signal.SIGKILL for r in results)
    assert not any(t.is_alive() for t in waiters)
