"""Tests that _profile_cli passes a bounded timeout and handles TimeoutExpired cleanly."""
import subprocess

import agent_importers_plugin.cli as cli_mod


def test_profile_cli_passes_timeout_and_returns_124_on_timeout(monkeypatch, tmp_path):
    seen_kwargs = {}

    def fake_run(argv, **kwargs):
        seen_kwargs.update(kwargs)
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)

    proc = cli_mod._profile_cli("show", "demo")
    assert seen_kwargs["timeout"] == cli_mod._PROFILE_CLI_TIMEOUT
    assert proc.returncode == 124
    assert "timed out" in proc.stderr
    assert cli_mod._profile_home("demo") is None
