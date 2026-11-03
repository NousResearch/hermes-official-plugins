"""A scan lease must not outlive the process that holds it."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest
from hermes_security.errors import NotFound
from hermes_security.store import SecurityStore, leases
from test_store_db import scan


def exited_owner():
    """A lease owner recorded by a process that has since exited."""
    code = 'from hermes_security.store import leases; print(leases.owner("service"))'
    done = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True,
                          timeout=120, check=True)
    return done.stdout.strip()


def test_a_lease_from_an_exited_process_is_taken_over_at_once(tmp_path):
    s = SecurityStore(tmp_path)
    sid = scan(s)
    assert s.lease(sid, exited_owner(), 300)
    assert s.lease(sid, leases.owner('service'), 300)


def test_a_live_holder_keeps_its_lease(tmp_path):
    s = SecurityStore(tmp_path)
    sid = scan(s)
    assert s.lease(sid, leases.owner('service'), 300)
    assert not s.lease(sid, leases.owner('service'), 300)


def test_a_reused_process_id_does_not_keep_a_lease(tmp_path):
    if not leases.process_start(os.getpid()):
        pytest.skip('this platform does not report process start times')
    s = SecurityStore(tmp_path)
    sid = scan(s)
    kind, host, pid, start, nonce = leases.owner('service').split(':')
    # Same process ID, different start time: the original holder is gone and the ID was reused.
    assert s.lease(sid, ':'.join([kind, host, pid, str(int(start) + 1), nonce]), 300)
    assert s.lease(sid, leases.owner('service'), 300)


def test_deleting_a_stopped_scan_ignores_a_dead_holders_lease(tmp_path):
    s = SecurityStore(tmp_path)
    sid = scan(s)
    s.set_scan_status(sid, 'canceled')
    assert s.lease(sid, exited_owner(), 300)
    s.delete_scan(sid)
    with pytest.raises(NotFound):
        s.get_scan(sid)
