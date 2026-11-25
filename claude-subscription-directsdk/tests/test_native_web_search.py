"""#389 Shape A: an opt-in native WebSearch call, separate from the main turn."""

from __future__ import annotations

import json

import pytest

from directsdk_setup import native_web_search_enabled
import native_web_search as nws


def _stream(tool_result):
    rows = [
        {"type": "system", "subtype": "init", "tools": ["WebSearch"]},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "WebSearch", "input": {"query": "q"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": tool_result}]}},
        {"type": "result", "subtype": "success", "num_turns": 2, "is_error": False},
    ]
    return "\n".join(json.dumps(r) for r in rows)


def test_off_by_default(monkeypatch):
    monkeypatch.setattr("directsdk_setup._setting", lambda key, default=None: default)
    assert native_web_search_enabled() is False


@pytest.mark.parametrize("raw,expected", [(True, True), ("true", True), ("on", True),
                                          (False, False), ("false", False), ("", False)])
def test_setting_parsing(monkeypatch, raw, expected):
    monkeypatch.setattr("directsdk_setup._setting", lambda key, default=None: raw)
    assert native_web_search_enabled() is expected


def test_unavailable_when_disabled(monkeypatch):
    monkeypatch.setattr(nws, "native_web_search_enabled", lambda: False)
    assert nws.ClaudeNativeWebSearchProvider().is_available() is False


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(nws, "native_web_search_enabled", lambda: True)


def test_argv_has_exactly_one_native_tool_and_no_mcp(monkeypatch, enabled):
    seen = {}

    def _probe(argv, **kw):
        seen["argv"] = argv
        seen["kw"] = kw
        return _stream("1. Example — https://example.com")

    monkeypatch.setattr(nws, "_probe", _probe)
    monkeypatch.setattr(nws, "_resolve", lambda *a, **k: ["claude"])
    monkeypatch.setattr(nws, "_child_env", lambda env, **k: dict(env))
    out = nws.ClaudeNativeWebSearchProvider().search("q", limit=5)
    argv = seen["argv"]
    assert argv[argv.index("--tools") + 1] == "WebSearch"
    assert argv[argv.index("--allowedTools") + 1] == "WebSearch"
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert "--setting-sources" in argv
    assert out["success"] is True


def test_results_pass_through_unprocessed(monkeypatch, enabled):
    raw = "1. Title A — https://a.example\n2. Title B — https://b.example"
    monkeypatch.setattr(nws, "_probe", lambda argv, **kw: _stream(raw))
    monkeypatch.setattr(nws, "_resolve", lambda *a, **k: ["claude"])
    monkeypatch.setattr(nws, "_child_env", lambda env, **k: dict(env))
    out = nws.ClaudeNativeWebSearchProvider().search("q")
    assert out["data"]["web"][0]["description"] == raw


def test_native_error_is_reported_not_swallowed(monkeypatch, enabled):
    rows = [{"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in"}]
    monkeypatch.setattr(nws, "_probe", lambda argv, **kw: "\n".join(json.dumps(r) for r in rows))
    monkeypatch.setattr(nws, "_resolve", lambda *a, **k: ["claude"])
    monkeypatch.setattr(nws, "_child_env", lambda env, **k: dict(env))
    out = nws.ClaudeNativeWebSearchProvider().search("q")
    assert out["success"] is False and "Not logged in" in out["error"]


def test_missing_cli_fails_cleanly(monkeypatch, enabled):
    monkeypatch.setattr(nws, "_resolve", lambda *a, **k: None)
    out = nws.ClaudeNativeWebSearchProvider().search("q")
    assert out["success"] is False
