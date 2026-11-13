"""Regression coverage for binds targeting the hidden Hermes cron directory."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from bubblewrap_plugin.sandbox import BubblewrapEnvironment, load_bubblewrap_config


def _bwrap_usable() -> bool:
    if shutil.which("bwrap") is None:
        return False
    try:
        probe = subprocess.run(
            ["bwrap", "--unshare-user", "--ro-bind", "/", "/", "true"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(
    not _bwrap_usable(), reason="bwrap missing or its namespace probe failed"
)


def test_bind_to_hermes_cron_is_hidden_by_later_overlay(monkeypatch):
    """An external operator bind must not reveal content under HERMES_HOME/cron."""
    with tempfile.TemporaryDirectory(prefix="hermes-bwrap-cron-overlay-", dir="/var/tmp") as temp:
        root = Path(temp)
        home = root / "home"
        project = home / "project"
        hermes_home = home / ".hermes"
        source = root / "external-cron"
        project.mkdir(parents=True)
        hermes_home.mkdir()
        source.mkdir()
        (source / "marker").write_text("MUST_STAY_HIDDEN\n")

        monkeypatch.chdir(root)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("HERMES_REAL_HOME", raising=False)
        monkeypatch.delenv("TERMINAL_SANDBOX_DIR", raising=False)

        config = load_bubblewrap_config({
            "binds": [{
                "src": str(source),
                "dest": str(hermes_home / "cron"),
                "readonly": True,
            }],
        })
        env = BubblewrapEnvironment(cwd=str(project), timeout=30, config=config)
        try:
            marker = hermes_home / "cron" / "marker"
            result = env.execute(
                f"test -r {marker} && printf VISIBLE || printf HIDDEN"
            )
        finally:
            env.cleanup()

        assert result.get("returncode") == 0, result
        assert result["output"].strip() == "HIDDEN", result


def test_file_bind_beneath_hidden_hermes_path_keeps_file_target(monkeypatch):
    """Materializing a hidden bind's parents must not create its file target as a directory."""
    with tempfile.TemporaryDirectory(prefix="hermes-bwrap-cron-file-", dir="/var/tmp") as temp:
        root = Path(temp)
        home = root / "home"
        project = home / "project"
        hermes_home = home / ".hermes"
        source = root / "external-cron-marker"
        project.mkdir(parents=True)
        hermes_home.mkdir()
        source.write_text("MUST_STAY_HIDDEN\n")

        monkeypatch.chdir(root)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("HERMES_REAL_HOME", raising=False)
        monkeypatch.delenv("TERMINAL_SANDBOX_DIR", raising=False)

        dest = hermes_home / "cron" / "marker"
        config = load_bubblewrap_config({
            "binds": [{"src": str(source), "dest": str(dest), "readonly": True}],
        })
        env = BubblewrapEnvironment(cwd=str(project), timeout=30, config=config)
        try:
            result = env.execute(f"test -r {dest} && printf VISIBLE || printf HIDDEN")
        finally:
            env.cleanup()

        assert result.get("returncode") == 0, result
        assert result["output"].strip() == "HIDDEN", result


def test_cron_dir_exposes_only_the_configured_external_directory(monkeypatch):
    """cron_dir is a sandbox-only mount and remains writable at HERMES_HOME/cron."""
    with tempfile.TemporaryDirectory(prefix="hermes-bwrap-cron-dir-", dir="/var/tmp") as temp:
        root = Path(temp)
        home = root / "home"
        project = home / "project"
        hermes_home = home / ".hermes"
        host_cron = hermes_home / "cron"
        source = root / "external-cron"
        project.mkdir(parents=True)
        host_cron.mkdir(parents=True)
        source.mkdir()
        (host_cron / "host-only").write_text("HOST_SCHEDULER_DATA\n")
        (source / "marker").write_text("EXTERNAL_CRON_DATA\n")

        monkeypatch.chdir(root)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("HERMES_REAL_HOME", raising=False)
        monkeypatch.delenv("TERMINAL_SANDBOX_DIR", raising=False)

        config = load_bubblewrap_config({"cron_dir": str(source)})
        env = BubblewrapEnvironment(cwd=str(project), timeout=30, config=config)
        try:
            marker = host_cron / "marker"
            write_probe = host_cron / "write-probe"
            result = env.execute(
                f"test -r {marker} && printf VISIBLE || printf HIDDEN; "
                f"printf WRITABLE > {write_probe}; "
                f"test -r {host_cron / 'host-only'} && printf HOST_VISIBLE || printf HOST_HIDDEN"
            )
        finally:
            env.cleanup()

        assert result.get("returncode") == 0, result
        assert result["output"].strip() == "VISIBLEHOST_HIDDEN", result
        assert (source / "write-probe").read_text() == "WRITABLE"
        assert (host_cron / "host-only").read_text() == "HOST_SCHEDULER_DATA\n"


def test_cron_dir_rejects_a_source_inside_hermes_home(monkeypatch):
    """The real Hermes home must never be reused as the configured source."""
    with tempfile.TemporaryDirectory(prefix="hermes-bwrap-cron-reject-", dir="/var/tmp") as temp:
        root = Path(temp)
        home = root / "home"
        project = home / "project"
        hermes_home = home / ".hermes"
        source = hermes_home / "external"
        project.mkdir(parents=True)
        source.mkdir(parents=True)

        monkeypatch.chdir(root)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("HERMES_REAL_HOME", raising=False)
        monkeypatch.delenv("TERMINAL_SANDBOX_DIR", raising=False)

        config = load_bubblewrap_config({"cron_dir": str(source)})
        with pytest.raises(ValueError, match="cron_dir cannot be bound safely"):
            BubblewrapEnvironment(cwd=str(project), timeout=30, config=config)
