"""#380: a stale claude on the service PATH must not beat a supported install."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import directsdk_setup as d
from unittest.mock import patch


def test_supported_path_cli_is_kept(tmp_path):
    env = {"PATH": str(tmp_path), "HOME": str(tmp_path)}
    exe = tmp_path / "claude"
    exe.write_text("")
    with patch.object(d.shutil, "which", return_value=str(exe)), \
         patch.object(d, "_cli_version", return_value=(2, 1, 322)):
        assert d._resolve(["claude"], env) == [str(exe)]


def test_outdated_path_cli_is_replaced(tmp_path):
    env = {"PATH": str(tmp_path / "bin"), "HOME": str(tmp_path)}
    old = tmp_path / "bin" / ("claude.cmd" if os.name == "nt" else "claude")
    old.parent.mkdir(parents=True)
    old.write_text("")
    os.chmod(old, 0o755)
    newer = tmp_path / ".local" / "bin" / ("claude.cmd" if os.name == "nt" else "claude")
    newer.parent.mkdir(parents=True)
    newer.write_text("")
    os.chmod(newer, 0o755)

    def version(exe, _env):
        return (2, 1, 148) if os.path.normcase(str(exe)) == os.path.normcase(str(old)) else (2, 1, 322)

    # The prefix fallback is a direct path scan in _resolve; mock the prefix list so the
    # test is about version selection, not this platform's HOME shape.
    with patch.object(d, "_install_prefixes", return_value=[str(newer.parent)]), \
         patch.object(d, "_cli_version", side_effect=version):
        assert os.path.normcase(d._resolve(["claude"], env)[0]) == os.path.normcase(str(newer))


def test_explicit_command_is_never_second_guessed(tmp_path):
    env = {"PATH": str(tmp_path), "HOME": str(tmp_path)}
    with patch.object(d.shutil, "which", return_value="/opt/my/claude"), \
         patch.object(d, "_cli_version", return_value=(2, 1, 100)) as ver:
        assert d._resolve(["/opt/my/claude"], env) == ["/opt/my/claude"]
        ver.assert_not_called()


def test_no_version_output_keeps_path_cli(tmp_path):
    env = {"PATH": str(tmp_path), "HOME": str(tmp_path)}
    exe = tmp_path / "claude"
    exe.write_text("")
    with patch.object(d.shutil, "which", return_value=str(exe)), \
         patch.object(d, "_cli_version", return_value=None):
        assert d._resolve(["claude"], env) == [str(exe)]


def test_version_is_asked_once_per_installed_binary(tmp_path):
    """Every turn resolves the CLI; `--version` must not run on each one, and a replaced binary is asked again."""
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\n")
    calls = []

    def run(argv, **_):
        calls.append(argv)
        return type("Done", (), {"stdout": "2.1.322 (Claude Code)"})()

    d._cli_versions.clear()
    with patch.object(d.subprocess, "run", side_effect=run):
        assert d._cli_version(str(exe), {}) == (2, 1, 322)
        assert d._cli_version(str(exe), {}) == (2, 1, 322)
        assert len(calls) == 1
        exe.write_text("#!/bin/sh\n# updated\n")
        d._cli_version(str(exe), {})
        assert len(calls) == 2
