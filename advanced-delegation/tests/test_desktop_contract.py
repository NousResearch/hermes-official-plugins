"""Run the actual Desktop disk ESM; static lint alone cannot detect missing SDK exports."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js is needed for Desktop ESM contract tests")
@pytest.mark.parametrize("scenario", ["link", "profile", "body", "error", "mutation-error", "no-settings"])
def test_desktop_public_host_contract(scenario):
    result = subprocess.run(
        [NODE, "--experimental-vm-modules", str(ROOT / "tests" / "desktop-contract.mjs"),
         str(ROOT / "desktop" / "plugin.js"), scenario],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"PASS {scenario}" in result.stdout
