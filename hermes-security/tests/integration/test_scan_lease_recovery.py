"""A submit process that dies while holding the scan lease must not stall the scan."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import hermes_security.service as service_module
from hermes_security.errors import Conflict
from test_end_to_end_static import worker
from test_service_modes import setup as start_scan

ROOT = Path(__file__).resolve().parents[2]

HOLD = r'''
import os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from hermes_security.service import SecurityService
with SecurityService(Path(sys.argv[2]))._mutation(sys.argv[3]):
    print('held', os.getpid(), flush=True)
    time.sleep(600)
'''


def hard_kill(pid):
    """End *pid* the way a crash or Task Manager does: no finally blocks, no cleanup."""
    if os.name == 'nt':
        import _winapi
        handle = _winapi.OpenProcess(0x00100001, False, pid)  # SYNCHRONIZE | PROCESS_TERMINATE
        try:
            _winapi.TerminateProcess(handle, 1)
            _winapi.WaitForSingleObject(handle, 10_000)
        finally:
            _winapi.CloseHandle(handle)
    else:
        import signal
        os.kill(pid, signal.SIGKILL)


def test_a_killed_lease_holder_does_not_block_worker_submits(tmp_path):
    root, service, plan = start_scan(tmp_path)
    # A venv python on Windows is a launcher, so the holder reports its own process ID.
    child = subprocess.Popen([sys.executable, '-c', HOLD, str(ROOT), str(tmp_path / 'data'), plan['scanId']],
                             stdout=subprocess.PIPE, text=True)
    try:
        word, pid = child.stdout.readline().split()
        assert word == 'held'
        with pytest.raises(Conflict, match=f'process {pid}'):
            service.submit_worker_result(worker(plan, []))
        hard_kill(int(pid))
        child.wait(timeout=30)
        assert service.submit_worker_result(worker(plan, []))['attemptId'] == 'att_baseline'
    finally:
        child.kill()
        child.wait(timeout=30)


def test_the_lease_heartbeat_survives_a_busy_database(tmp_path, monkeypatch):
    root, service, plan = start_scan(tmp_path)
    monkeypatch.setattr(service_module, 'LEASE_RENEW_S', 0.02, raising=False)
    real, calls = service.store.lease, []

    def flaky(scan_id, owner, ttl_s):
        calls.append(owner)
        if len(calls) == 2:
            raise Conflict('database is busy; retry the operation')
        return real(scan_id, owner, ttl_s)

    monkeypatch.setattr(service.store, 'lease', flaky)
    with service._mutation(plan['scanId']):
        deadline = time.monotonic() + 10
        while len(calls) < 4 and time.monotonic() < deadline:
            time.sleep(0.01)
    assert len(calls) >= 4
