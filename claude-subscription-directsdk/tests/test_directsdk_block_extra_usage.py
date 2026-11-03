"""Opt-in block_extra_usage (#43): a recorded paid-use marker refuses the next
spawn until its window expires; no marker never blocks."""

import json
import time

import pytest

import directsdk_setup
from directsdk import ClaudeAPIError


def _marker(tmp_path, monkeypatch, *, window="five_hour", age=0.0):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "hermes-directsdk-overage.json").write_text(
        json.dumps({"window": window, "seen_at": time.time() - age}), encoding="utf-8")


def test_no_marker_never_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert directsdk_setup.overage_blocked() is False


def test_fresh_marker_blocks_and_expired_one_does_not(tmp_path, monkeypatch):
    _marker(tmp_path, monkeypatch)
    assert directsdk_setup.overage_blocked() is True

    _marker(tmp_path, monkeypatch, age=6 * 3600)  # five_hour window has reset
    assert directsdk_setup.overage_blocked() is False


def test_record_then_block_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    directsdk_setup.record_overage("overage")
    assert directsdk_setup.overage_blocked() is True


def test_create_refuses_before_spawn_when_blocked(monkeypatch):
    import directsdk

    monkeypatch.setattr(directsdk, "block_extra_usage", lambda: True)
    monkeypatch.setattr(directsdk, "overage_blocked", lambda: True)
    client = directsdk.Client.__new__(directsdk.Client)
    client._lock = __import__("threading").Lock()
    client._requests = set()
    client._closed = False

    with pytest.raises(ClaudeAPIError) as exc:
        client._create(model="m", messages=[])
    assert exc.value.status_code == 402
