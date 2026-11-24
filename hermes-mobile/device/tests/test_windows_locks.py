"""Device follow-ups load and serialize without fcntl, as on Windows."""
import importlib.util
import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


class FakeMsvcrt(types.ModuleType):
    """msvcrt.locking over one byte, enforced between handles like Windows' mandatory locks."""
    LK_UNLCK, LK_LOCK, LK_NBLCK = 0, 1, 2

    def __init__(self):
        super().__init__("msvcrt")
        self.owners, self.guard = {}, threading.Lock()

    def locking(self, fd, mode, length):
        assert length == 1 and os.lseek(fd, 0, os.SEEK_CUR) == 0
        key = os.fstat(fd).st_ino
        with self.guard:
            if mode == self.LK_UNLCK:
                assert self.owners.pop(key) == fd
                return
            if self.owners.get(key, fd) != fd:
                time.sleep(0.01)
                raise OSError(36, "Resource deadlock avoided")
            self.owners[key] = fd


def load_continuation(name="device_continuation_windows"):
    spec = importlib.util.spec_from_file_location(name, ROOT / "continuation.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Store:
    def __init__(self, path):
        self.path, self.reads, self.marked = path, 0, []

    def ready_replies(self):
        self.reads += 1
        return [{"request_id": "r1"}]

    def mark_reply(self, request_id, job):
        self.marked.append((request_id, job))


def test_reconcile_runs_once_at_a_time_without_fcntl(monkeypatch, tmp_path):
    fake = FakeMsvcrt()
    monkeypatch.setitem(sys.modules, "fcntl", None)  # `import fcntl` raises ImportError
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    continuation = load_continuation()
    assert continuation.fcntl is None
    store = Store(tmp_path / "bridge.db")
    continuation.reconcile(store, schedule=lambda item: "job-1")
    assert store.marked == [("r1", "job-1")] and fake.owners == {}
    # Another process mid-reconcile: this one skips instead of scheduling twice.
    with store.path.with_name("bridge.replies.lock").open("a") as other:
        assert continuation._try_lock(other)
        continuation.reconcile(store, schedule=lambda item: "job-2")
        continuation._unlock(other)
    assert store.reads == 1 and fake.owners == {}


def test_reconcile_skips_while_locked_with_flock(tmp_path):
    pytest.importorskip("fcntl")
    continuation = load_continuation("device_continuation_posix")
    assert continuation.fcntl is not None
    store = Store(tmp_path / "bridge.db")
    with store.path.with_name("bridge.replies.lock").open("a") as other:
        assert continuation._try_lock(other)
        continuation.reconcile(store, schedule=lambda item: "job")
        continuation._unlock(other)
    assert store.reads == 0
    continuation.reconcile(store, schedule=lambda item: "job")
    assert store.marked == [("r1", "job")]
