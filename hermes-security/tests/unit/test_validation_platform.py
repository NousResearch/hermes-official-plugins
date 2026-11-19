import sys

import pytest

from hermes_security.errors import PolicyDenied
from hermes_security.validation import runner


@pytest.mark.skipif(sys.platform == "win32", reason="Windows enforces limits with Job Objects instead")
def test_local_validation_refuses_unsupported_process_limits(tmp_path, monkeypatch):
    """Never fall back to running commands without the promised OS limits."""
    monkeypatch.setattr(runner, "resource", None)
    target = tmp_path / "source"
    target.mkdir()
    marker = tmp_path / "must-not-execute"
    command = [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('ran')"]
    plan = {
        "candidateId": "c",
        "kind": "local-command",
        "level": "local-safe",
        "commands": [command, command],
        "positiveControl": {"commandIndex": 0, "expectedMarker": "VULN-MARKER"},
        "negativeControl": {"commandIndex": 1},
        "cleanup": {"strategy": "remove-copy"},
        "timeoutS": 2,
        "maxOutputBytes": 4096,
    }

    with pytest.raises(PolicyDenied, match="process limits"):
        runner.run_local(plan, target=target, workdir_parent=tmp_path)

    assert not marker.exists()
