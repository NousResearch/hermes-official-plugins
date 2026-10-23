"""Singularity/Apptainer preflight: a clear error when neither CLI is installed, instead of a
cryptic FileNotFoundError (hermes-agent#1511)."""

from unittest.mock import MagicMock, patch

import pytest

from singularity_plugin.singularity_env import _ensure_singularity_available, _find_singularity_executable


def test_prefers_apptainer():
    def which_both(name):
        return f"/usr/bin/{name}" if name in {"apptainer", "singularity"} else None

    with patch("shutil.which", side_effect=which_both):
        assert _find_singularity_executable() == "apptainer"


def test_falls_back_to_singularity():
    with patch("shutil.which", side_effect=lambda n: "/usr/bin/singularity" if n == "singularity" else None):
        assert _find_singularity_executable() == "singularity"


def test_raises_when_neither_found():
    with patch("shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="Neither.*apptainer.*nor.*singularity"):
            _find_singularity_executable()


def test_returns_executable_when_version_check_passes():
    with patch("shutil.which", side_effect=lambda n: "/usr/bin/apptainer" if n == "apptainer" else None), \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")):
        assert _ensure_singularity_available() == "apptainer"


def test_version_failure_names_the_exit_code():
    with patch("shutil.which", side_effect=lambda n: "/usr/bin/apptainer" if n == "apptainer" else None), \
         patch("subprocess.run", return_value=MagicMock(returncode=3, stderr="boom")):
        with pytest.raises(RuntimeError, match="exit code 3"):
            _ensure_singularity_available()
