"""#389 Shape A: an opt-in WebSearch call, separate from the main turn, registered as a web_search provider."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import pytest

import native_search as ns

# The fake `claude` is a POSIX sh wrapper; Windows CI exercises the pure tests only.
needs_posix = pytest.mark.skipif(os.name == "nt", reason="fake CLI is a POSIX sh wrapper")

FAKE = Path(__file__).parent / "fixtures" / "fake_cli.py"
HITS = json.loads((Path(__file__).parent / "fixtures" / "websearch_hits.json").read_text(encoding="utf-8"))["hits"]


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """A fake `claude` that records how it was started; returns the record's path."""
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_RECORD", str(record))
    monkeypatch.setenv("CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND", str(tmp_path / "claude"))
    wrapper = tmp_path / "claude"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} -I {FAKE} \"$@\"\n")
    wrapper.chmod(0o755)
    for key in ns.BACKEND_OVERRIDES + ns.API_CREDENTIALS:
        monkeypatch.delenv(key, raising=False)
    return record


@pytest.fixture
def on(monkeypatch):
    settings = {ns.ENABLE_SETTING: True}
    monkeypatch.setattr(ns, "_setting", lambda key, default=None: settings.get(key, default))
    return settings


def provider():
    return ns.ClaudeSubscriptionWebSearchProvider()


@needs_posix
def test_off_by_default(monkeypatch, cli):
    monkeypatch.setattr(ns, "_setting", lambda key, default=None: default)
    assert provider().is_available() is False
    assert provider().search("q")["success"] is False
    assert not cli.exists(), "no process may start while the setting is off"


@pytest.mark.parametrize("raw,expected", [(True, True), ("true", True), ("on", True), ("1", True),
                                          (False, False), ("false", False), ("", False), (None, False), ([], False)])
def test_setting_parsing(monkeypatch, raw, expected):
    monkeypatch.setattr(ns, "_setting", lambda key, default=None: raw)
    assert ns.enabled() is expected


@needs_posix
def test_available_needs_the_cli(monkeypatch, on, tmp_path):
    monkeypatch.setenv("CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND", str(tmp_path / "missing"))
    assert provider().is_available() is False
    assert provider().search("q")["success"] is False


@needs_posix
def test_rows_are_title_url_position_from_the_structured_result(on, cli):
    out = provider().search("hermes plugin", limit=5)
    assert out["success"] is True
    web = out["data"]["web"]
    assert [r["url"] for r in web] == [h["url"] for h in HITS[:5]]
    assert [r["title"] for r in web] == [h["title"] for h in HITS[:5]]
    assert [r["position"] for r in web] == [1, 2, 3, 4, 5]
    assert all(set(r) == {"title", "url", "description", "position"} for r in web)


@needs_posix
def test_limit_caps_rows_and_commentary_is_never_returned(on, cli):
    out = provider().search("q", limit=100)
    assert len(out["data"]["web"]) == len(HITS)
    blob = json.dumps(out)
    assert "SUMMARY MUST NOT BE RETURNED" not in blob and "IGNORE PREVIOUS" not in blob


@needs_posix
def test_links_line_fallback_when_no_structured_result(on, cli, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "no_structured")
    web = provider().search("q", limit=3)["data"]["web"]
    assert [r["url"] for r in web] == [h["url"] for h in HITS[:3]]


@needs_posix
def test_argv_has_exactly_one_native_tool_no_mcp_and_a_separate_cwd(on, cli, tmp_path):
    provider().search("a query")
    rec = json.loads(cli.read_text())
    argv = rec["argv"]
    assert argv[argv.index("--tools") + 1] == "WebSearch"
    assert argv[argv.index("--allowedTools") + 1] == "WebSearch"
    assert argv[argv.index("--model") + 1] == "haiku"
    assert "--strict-mcp-config" in argv and argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--disable-slash-commands" in argv and "--no-session-persistence" in argv
    assert "--max-turns" not in argv, "this is not a directsdk main turn"
    assert "claude-directsdk-websearch-" in rec["cwd"]
    assert rec["prompt"].endswith("Query: a query")
    assert not Path(rec["cwd"]).exists(), "the scratch dir goes with the call"


@needs_posix
def test_model_is_configurable_and_validated(on, cli):
    on[ns.MODEL_SETTING] = "claude-sonnet-4-6"
    provider().search("q")
    argv = json.loads(cli.read_text())["argv"]
    assert argv[argv.index("--model") + 1] == "claude-sonnet-4-6"
    on[ns.MODEL_SETTING] = "haiku; rm -rf /"
    provider().search("q")
    argv = json.loads(cli.read_text())["argv"]
    assert argv[argv.index("--model") + 1] == "haiku"


@needs_posix
def test_api_credentials_never_reach_the_child(on, cli, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    provider().search("q")
    assert json.loads(cli.read_text())["env"]["ANTHROPIC_API_KEY"] is None


@needs_posix
def test_inherited_gateway_override_is_refused(on, cli, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:1")
    out = provider().search("q")
    assert out["success"] is False and "ANTHROPIC_BASE_URL" in out["error"]
    assert not cli.exists()


@needs_posix
def test_profile_config_dir_setting_selects_the_login(on, cli, tmp_path):
    on["claude_config_dir"] = str(tmp_path / "login")
    provider().search("q")
    assert json.loads(cli.read_text())["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path / "login")


@needs_posix
def test_cli_error_is_reported_not_swallowed(on, cli, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "error")
    out = provider().search("q")
    assert out["success"] is False and "Not logged in" in out["error"]


@needs_posix
def test_no_output_is_an_error(on, cli, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "silent")
    out = provider().search("q")
    assert out["success"] is False and "without a search result" in out["error"]


@needs_posix
def test_timeout_kills_the_child(on, cli, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "hang")
    monkeypatch.setattr(ns, "_timeout", lambda: 1.0)
    started = time.monotonic()
    out = provider().search("q")
    assert out["success"] is False and "timed out" in out["error"]
    assert time.monotonic() - started < 10


@needs_posix
def test_child_is_stopped_at_the_tool_result_not_after_the_summary_turn(on, cli, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "slow_summary")  # the fake sleeps 30 s after the tool result
    started = time.monotonic()
    out = provider().search("q", limit=100)
    assert out["success"] is True and len(out["data"]["web"]) == len(HITS)
    assert time.monotonic() - started < 10


@needs_posix
def test_query_and_result_count_are_logged_at_info(on, cli, caplog):
    with caplog.at_level(logging.INFO, logger=ns.logger.name):
        provider().search("hermes plugin", limit=3)
    line = next(r.getMessage() for r in caplog.records if "Claude WebSearch" in r.getMessage())
    assert caplog.records[0].levelno == logging.INFO
    assert "query='hermes plugin'" in line and "results=3" in line


@needs_posix
def test_failure_is_logged_at_info_with_the_query(on, cli, caplog, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "error")
    with caplog.at_level(logging.INFO, logger=ns.logger.name):
        provider().search("hermes plugin")
    assert any("query='hermes plugin'" in r.getMessage() and "failed" in r.getMessage() for r in caplog.records)


def test_only_search_is_supported():
    p = provider()
    assert p.supports_search() is True and p.supports_extract() is False
    assert p.name == "claude-subscription-websearch"
