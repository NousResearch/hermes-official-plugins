"""Kept processes never outnumber ``max_sessions`` and never outlive a dropped client, a cancel or a close.

Real Claude Code CLI against the scripted loopback upstream of ``test_directsdk_session``; no credentials, no
inference. Skipped without the CLI; ``CLAUDE_CODE_BIN`` picks a binary.
"""
import gc
import os
from pathlib import Path
import shutil
import sys
import threading
import time

import psutil
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import directsdk
from test_directsdk_session import MODEL, TOOLS, client, home, session_pids, upstream  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not (os.environ.get('CLAUDE_CODE_BIN') or shutil.which('claude')), reason='needs the Claude Code CLI')


def gone(pid, within=10):
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.Error:
            return True
        time.sleep(.05)
    return False


def test_concurrent_launches_never_exceed_max_sessions(home, upstream, monkeypatch):
    """Three agents start their first step at once with ``max_sessions`` 1: one process is kept, the other two steps
    run one-shot. Before the place was taken under the lock, all three passed the check and all three were kept."""
    clients = [client(home, upstream, monkeypatch, session=True, cap=1) for _ in range(3)]
    gate, errors = threading.Barrier(3), []

    def first_step(c):
        try:
            gate.wait(10)
            c.create(model=MODEL, messages=[{'role': 'user', 'content': 'go'}], tools=TOOLS, max_tokens=1000)
        except Exception as error:  # noqa: BLE001
            errors.append(error)
    threads = [threading.Thread(target=first_step, args=(c,)) for c in clients]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert not errors, errors
        kept = [c for c in clients if session_pids(c)]
        assert len(kept) <= 1, f'{len(kept)} processes kept with max_sessions 1'
        assert len(upstream.bodies) == 3  # every step still answered, the others as one-shots
        with directsdk._live_lock:
            assert len(directsdk._live) <= 1
    finally:
        for c in clients:
            c.close()


def test_a_dropped_client_ends_its_kept_process(home, upstream, monkeypatch):
    """Hermes drops a client without close(): once it is collected its kept process is gone, not kept for the idle
    hour by the timer's reference to the client."""
    c = client(home, upstream, monkeypatch, session=True)
    c.create(model=MODEL, messages=[{'role': 'user', 'content': 'go'}], tools=TOOLS, max_tokens=1000)
    pid = next(iter(session_pids(c)))
    del c
    gc.collect()
    assert gone(pid), 'the kept process outlived its dropped client'


@pytest.mark.parametrize('stop', ['close', 'cancel'])
def test_a_stop_while_a_process_becomes_the_kept_session_ends_it(home, upstream, monkeypatch, stop):
    """close() or cancel() lands after the step has its answer and before its process is the kept session. The stop
    must still find an owner: the process ends and the step raises the cancellation error. Before, the request had
    already let go of the process and the client did not hold it yet, so the stop found neither and it stayed kept."""
    c = client(home, upstream, monkeypatch, session=True)
    reached, proceed, natives, errors = threading.Event(), threading.Event(), [], []
    keep = directsdk.Client._keep

    def paused(self, native, *rest):
        natives.append(native)
        reached.set()
        proceed.wait(30)
        return keep(self, native, *rest)
    monkeypatch.setattr(directsdk.Client, '_keep', paused)

    def step():
        try:
            c.create(model=MODEL, messages=[{'role': 'user', 'content': 'go'}], tools=TOOLS, max_tokens=1000)
        except Exception as error:  # noqa: BLE001
            errors.append(f'{type(error).__name__}: {error}')
    worker = threading.Thread(target=step)
    try:
        worker.start()
        assert reached.wait(30), 'the step never reached the hand-over'
        pid = natives[0].process.pid
        getattr(c, stop)()
        proceed.set()
        worker.join(30)
        assert not worker.is_alive()
        assert errors == ['RuntimeError: Claude request cancelled']
        assert not session_pids(c), 'the stopped step left a kept session'
        assert gone(pid), 'the process outlived the stop'
        with directsdk._live_lock:
            assert not directsdk._live
    finally:
        proceed.set()
        worker.join(30)
        c.close()


def test_an_idle_kept_process_that_dies_gives_its_place_back(home, upstream, monkeypatch):
    """A kept process that exits while idle (a crash, an OOM kill) frees its relay and its place at once: with
    ``max_sessions`` 1 the next agent keeps a process. Before, the dead one held the place until its own client's
    next step, close or the idle hour, and the next agent ran per-call."""
    first = client(home, upstream, monkeypatch, session=True, cap=1)
    second = client(home, upstream, monkeypatch, session=True, cap=1)
    try:
        first.create(model=MODEL, messages=[{'role': 'user', 'content': 'go'}], tools=TOOLS, max_tokens=1000)
        native = first._session
        directsdk.kill_process_tree(native.process)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (directsdk._live or native.admission.thread.is_alive()):
            time.sleep(.02)
        assert not directsdk._live, 'the dead process still holds its place'
        assert first._session is None and not native.admission.thread.is_alive(), 'its relay is still up'
        second.create(model=MODEL, messages=[{'role': 'user', 'content': 'go'}], tools=TOOLS, max_tokens=1000)
        assert session_pids(second), 'the next agent ran per-call beside a dead process'
    finally:
        first.close()
        second.close()


def test_a_process_that_dies_as_it_becomes_the_kept_session_gives_its_place_back(home, upstream, monkeypatch):
    """The process exits after the step has its answer and before it is the kept session, so its reader finds no
    session to end. The hand-over itself must then end it: the step still returns, and no place stays held."""
    c = client(home, upstream, monkeypatch, session=True, cap=1)
    keep = directsdk.Client._keep

    def dead_first(self, native, *rest):
        directsdk.kill_process_tree(native.process)
        native.reader.join(10)
        return keep(self, native, *rest)
    monkeypatch.setattr(directsdk.Client, '_keep', dead_first)
    try:
        assert c.create(model=MODEL, messages=[{'role': 'user', 'content': 'go'}], tools=TOOLS, max_tokens=1000).choices
        assert c._session is None
        with directsdk._live_lock:
            assert not directsdk._live, 'the dead process holds a place'
    finally:
        c.close()
