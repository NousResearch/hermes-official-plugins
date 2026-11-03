import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import pytest
from hermes_security.store import SecurityStore
from hermes_security.errors import NotFound


def test_profile_a_b_a(tmp_path):
    a = SecurityStore(tmp_path / "A")
    sid = a.create_scan(
        mode="standard",
        safety_level="static",
        target={"root": "/repo", "repoKey": "r"},
        inventory=[],
        options={},
    )["scan_id"]
    a.record_attempt(sid, "att_a", "baseline")
    a.close()
    b = SecurityStore(tmp_path / "B")
    assert b.list_scans() == {"items": [], "total": 0}
    assert b.list_findings() == {"items": [], "total": 0}
    assert b.list_repositories() == {"items": [], "total": 0}
    for action in (
        lambda: b.get_scan(sid),
        lambda: b.set_scan_status(sid, "failed"),
        lambda: b.add_event(sid, "x", "x"),
        lambda: b.record_attempt(sid, "att_a", "baseline"),
        lambda: b.lease(sid, "owner", 30),
    ):
        with pytest.raises(NotFound):
            action()
    b.close()
    a = SecurityStore(tmp_path / "A")
    assert a.get_scan(sid)["status"] == "created"
    assert a.record_attempt(sid, "att_a", "baseline")["status"] == "running"
    assert a.list_scans()["total"] == 1
    a.close()
