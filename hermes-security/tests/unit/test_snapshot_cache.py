"""Snapshot checks re-read only files whose size, mtime or file ID changed since they were hashed."""
import os

import pytest

from hermes_security.errors import Conflict
from hermes_security.service import SecurityService
from hermes_security.target import inventory, snapshot


def start(tmp_path, files=3):
    root = tmp_path / 'repo'; root.mkdir()
    for i in range(files):
        (root / f'm{i}.py').write_text(f'x = {i}\n')
    service = SecurityService(tmp_path / 'data')
    return root, service, service.start_scan(path=str(root))['scanId']


def count_reads(monkeypatch):
    calls = []
    real = inventory.safe_read

    def counting(root, path, *args, **kwargs):
        calls.append(path)
        return real(root, path, *args, **kwargs)
    monkeypatch.setattr(snapshot, 'safe_read', counting)
    return calls


def check(service, sid):
    service._current(service._scan(sid))


def test_an_unchanged_tree_is_checked_without_reading_files(tmp_path, monkeypatch):
    _, service, sid = start(tmp_path)
    calls = count_reads(monkeypatch)
    check(service, sid)
    assert calls == []


def test_an_edit_that_keeps_the_size_is_caught(tmp_path):
    root, service, sid = start(tmp_path)
    path = root / 'm0.py'
    before = os.stat(path)
    path.write_text('x = 9\n')
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 5_000_000_000))
    with pytest.raises(Conflict):
        check(service, sid)


def test_a_deleted_file_is_caught(tmp_path):
    root, service, sid = start(tmp_path)
    (root / 'm1.py').unlink()
    with pytest.raises(Conflict):
        check(service, sid)


def test_a_touched_but_unchanged_file_is_rehashed_once_then_trusted(tmp_path, monkeypatch):
    root, service, sid = start(tmp_path)
    path = root / 'm2.py'
    before = os.stat(path)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 5_000_000_000))
    calls = count_reads(monkeypatch)
    check(service, sid)
    assert calls == ['m2.py']
    check(service, sid)
    assert calls == ['m2.py']


def test_a_scan_without_a_cache_hashes_once_then_trusts_stat(tmp_path, monkeypatch):
    _, service, sid = start(tmp_path)
    (service._work(sid) / snapshot.STAT_CACHE).unlink()
    calls = count_reads(monkeypatch)
    check(service, sid)
    assert sorted(calls) == ['m0.py', 'm1.py', 'm2.py']
    check(service, sid)
    assert len(calls) == 3


def test_new_files_do_not_void_the_scan(tmp_path):
    root, service, sid = start(tmp_path)
    (root / 'notes.txt').write_text('stray\n')
    check(service, sid)


def test_a_background_prepared_scan_gets_a_cache_too(tmp_path, monkeypatch):
    root = tmp_path / 'repo'; root.mkdir()
    (root / 'app.py').write_text('x = 1\n')
    service = SecurityService(tmp_path / 'data')
    sid = service.start_scan(path=str(root), background=True, dependency_check=False)['scanId']
    thread = service._dependency_threads.get(sid)
    if thread:
        thread.join(10)
    calls = count_reads(monkeypatch)
    check(service, sid)
    assert calls == []
