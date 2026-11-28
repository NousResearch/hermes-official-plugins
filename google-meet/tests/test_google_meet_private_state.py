"""Google Meet's private state and the secrets it hands its bot, as core's suite pinned them.

* Every credential file the plugin writes (the node token, the node registry, the Google auth state) is
  created 0600 before any byte lands — never "open at umask, then chmod" (the #19673 window). POSIX-only:
  mode bits are not enforced on Windows.
* ``process_manager.start`` resolves the realtime key through the active secret scope AT SPAWN TIME and
  passes it explicitly in the child environment: the detached bot inherits the process env, not the
  contextvar scope, and under multiplexing the process env holds another profile's values.
* A node presents its token through a timing-safe compare that fails closed on hostile input.
"""

from __future__ import annotations

import hmac
import json
import os
import stat
import sys
import types
from pathlib import Path
from typing import Any

import pytest

_PRIVATE = stat.S_IRUSR | stat.S_IWUSR
URL = "https://meet.google.com/abc-defg-hij"


@pytest.fixture
def opens_spy(monkeypatch):
    """Record every ``os.open`` create (path, flags, mode) issued through ``utils``' atomic writers."""
    import utils.files

    observed: list[tuple[str, int, int]] = []
    real_open = os.open

    def spying(path, flags, mode=0o777, *args, **kwargs):
        if flags & os.O_CREAT:
            observed.append((os.fspath(path), flags, mode))
        return real_open(path, flags, mode, *args, **kwargs)

    # mkstemp resolves ``os.open`` at call time from the ``tempfile`` module namespace.
    monkeypatch.setattr(utils.files.tempfile._os, "open", spying)
    return observed


def _fake_playwright(monkeypatch):
    """Install a ``playwright.sync_api`` whose context mimics ``storage_state`` (plain open at umask)."""

    class _Context:
        def new_page(self):
            return types.SimpleNamespace(goto=lambda *a, **k: None)

        def storage_state(self, path=None):
            state = {"cookies": [{"name": "SID", "value": "secret"}], "origins": []}
            if path is not None:
                Path(path).write_text(json.dumps(state), encoding="utf-8")
            return state

    browser = types.SimpleNamespace(new_context=_Context, close=lambda: None)
    pw = types.SimpleNamespace(chromium=types.SimpleNamespace(launch=lambda **k: browser))

    class _SyncPlaywright:
        def __enter__(self):
            return pw

        def __exit__(self, *exc):
            return False

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = _SyncPlaywright
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)


def _writers(home: Path, monkeypatch):
    """``(label, callable, target_path)`` for every credential file the plugin writes."""
    from google_meet_plugin import cli as meet_cli
    from google_meet_plugin.node.registry import NodeRegistry
    from google_meet_plugin.node.server import NodeServer

    node = NodeServer(token_path=home / "meetings" / "node_token.json")
    registry = NodeRegistry(path=home / "meetings" / "nodes.json")
    _fake_playwright(monkeypatch)
    monkeypatch.setattr(meet_cli, "chromium_executable", lambda: None)
    monkeypatch.setattr("builtins.input", lambda *_: "")
    return [
        ("meet node token", node.ensure_token, node.token_path),
        ("meet node registry", lambda: registry.add("mac", "ws://mac.local:18789", "tok"), registry.path),
        ("meet google auth state", lambda: meet_cli._cmd_auth() == 0 or pytest.fail("meet auth failed"),
         meet_cli._auth_state_path()),
    ]


@pytest.mark.platforms("posix")
def test_every_credential_file_is_created_0600_before_any_byte_lands(monkeypatch, opens_spy):
    home = Path(os.environ["HERMES_HOME"])
    old_umask = os.umask(0o022)  # a "write then chmod" regression would surface as 0o644
    try:
        for label, write, target in _writers(home, monkeypatch):
            del opens_spy[:]
            write()
            assert target.exists(), f"{label}: nothing written at {target}"
            assert stat.S_IMODE(target.stat().st_mode) == _PRIVATE, f"{label}: final file not 0600"
            creates = [(p, m) for p, _fl, m in opens_spy if Path(p).parent == target.parent]
            assert creates, f"{label}: no temp file created in {target.parent}; opens={opens_spy!r}"
            for path, mode in creates:
                assert mode == _PRIVATE, f"{label}: temp file {path} created 0o{mode:o}, not 0600"
    finally:
        os.umask(old_umask)


@pytest.fixture
def multiplex_scope():
    """Install a secret scope with multiplexing ON (one process serving many profiles); restore after."""
    from agent.secret_scope import reset_secret_scope, set_multiplex_active, set_secret_scope

    tokens = []

    def install(scope: dict[str, str]):
        set_multiplex_active(True)
        tokens.append(set_secret_scope(scope))

    yield install

    for token in tokens:
        reset_secret_scope(token)
    set_multiplex_active(False)


def _spawn(monkeypatch, tmp_path, **kwargs) -> dict[str, Any]:
    """Run ``process_manager.start`` with Popen captured; returns the child's env."""
    from google_meet_plugin import process_manager as pm

    monkeypatch.setattr(pm, "_root", lambda: tmp_path)
    captured: dict[str, Any] = {}

    class _FakeProc:
        pid = 4242

    def fake_popen(cmd, **popen_kwargs):
        captured["env"] = popen_kwargs.get("env")
        return _FakeProc()

    monkeypatch.setattr(pm.subprocess, "Popen", fake_popen)
    result = pm.start(URL, out_dir=tmp_path / "meeting", **kwargs)
    assert result["ok"] is True
    return captured["env"]


def test_child_env_carries_the_scoped_key_not_the_process_env(multiplex_scope, monkeypatch, tmp_path):
    """The process env holds another profile's key; the bot gets the active profile's, explicitly."""
    monkeypatch.setenv("OPENAI_API_KEY", "env-other-profile")
    monkeypatch.delenv("HERMES_MEET_REALTIME_KEY", raising=False)
    multiplex_scope({"OPENAI_API_KEY": "scoped-openai-key"})

    env = _spawn(monkeypatch, tmp_path, mode="realtime")

    assert env["HERMES_MEET_REALTIME_KEY"] == "scoped-openai-key"


def test_explicit_key_argument_still_wins_over_the_scope(multiplex_scope, monkeypatch, tmp_path):
    multiplex_scope({"OPENAI_API_KEY": "scoped-openai-key"})

    env = _spawn(monkeypatch, tmp_path, realtime_api_key="explicit-key")

    assert env["HERMES_MEET_REALTIME_KEY"] == "explicit-key"


def test_node_token_is_compared_timing_safe_and_fails_closed(monkeypatch):
    from google_meet_plugin.node import protocol

    secret = "s3cret"
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))

    def accepts(presented: str) -> bool:
        return protocol.validate_request({"type": "ping", "id": "1", "token": presented, "payload": {}},
                                         secret)[0]

    assert accepts(secret)
    assert calls, "the token was not compared with hmac.compare_digest"
    assert not accepts(secret + "x")
    # A byte that is not UTF-8 reaches the server as a lone surrogate (JSON decodes "\udcff" escapes).
    assert not accepts("tök\udcff")
