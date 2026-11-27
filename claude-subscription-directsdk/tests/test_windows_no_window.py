"""Tests that Windows subprocess launches suppress flashing console windows (#533)."""
import subprocess
from types import SimpleNamespace

import directsdk_setup
import process_tree


def test_own_process_group_adds_create_no_window_on_nt(monkeypatch):
    monkeypatch.setattr(process_tree.os, "name", "nt")
    monkeypatch.setattr(process_tree.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
    monkeypatch.setattr(process_tree.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)

    kwargs = process_tree.own_process_group()
    assert kwargs == {"creationflags": 0x00000200 | 0x08000000}


def test_own_process_group_on_posix_uses_start_new_session(monkeypatch):
    monkeypatch.setattr(process_tree.os, "name", "posix")
    assert process_tree.own_process_group() == {"start_new_session": True}
    assert process_tree.no_window_kwargs() == {}


def test_cli_version_and_setup_status_pass_create_no_window_on_nt(tmp_path, monkeypatch):
    monkeypatch.setattr(process_tree.os, "name", "nt")
    monkeypatch.setattr(process_tree.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)

    fake_cli = tmp_path / "claude.exe"
    fake_cli.write_text("", encoding="utf-8")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if "--version" in cmd:
            return SimpleNamespace(returncode=0, stdout="2.1.318\n", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout='{"loggedIn": true, "subscriptionType": "max", "apiProvider": "firstParty"}',
            stderr="",
        )

    monkeypatch.setattr(directsdk_setup.subprocess, "run", fake_run)
    monkeypatch.setattr(directsdk_setup, "_cli_versions", {})

    version = directsdk_setup._cli_version(str(fake_cli), {})
    assert version == (2, 1, 318)

    status = directsdk_setup.setup_status(command=[str(fake_cli)], env={"PATH": str(tmp_path)})
    assert status["logged_in"] is True

    assert len(calls) == 2
    for _cmd, kwargs in calls:
        assert kwargs.get("creationflags") == 0x08000000
