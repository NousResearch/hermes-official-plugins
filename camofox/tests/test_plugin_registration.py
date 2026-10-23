"""register(): the full Camofox tool surface on a core without Camofox, nothing on one that has it."""

from __future__ import annotations

import logging

import pytest

import camofox_plugin

EXPECTED_TOOLS = {
    "browser_navigate", "browser_snapshot", "browser_click", "browser_type", "browser_scroll",
    "browser_back", "browser_press", "browser_get_images", "browser_vision", "browser_console",
    "browser_import_cookies",
}


class _Ctx:
    def __init__(self):
        self.tools, self.hooks, self.aux, self.cli = {}, {}, {}, {}
        self.llm = object()

    def register_tool(self, name, toolset, schema, handler, check_fn=None, emoji="", **_):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler, "check_fn": check_fn}

    def register_hook(self, name, cb):
        self.hooks[name] = cb

    def register_auxiliary_task(self, key, **kw):
        self.aux[key] = kw

    def register_cli_command(self, name, **kw):
        self.cli[name] = kw


def test_registers_the_browser_tools_in_the_camofox_toolset():
    ctx = _Ctx()
    camofox_plugin.register(ctx)
    assert set(ctx.tools) == EXPECTED_TOOLS
    assert {v["toolset"] for v in ctx.tools.values()} == {"camofox"}
    assert all(v["schema"]["name"] == name for name, v in ctx.tools.items())
    assert set(ctx.hooks) == {"on_session_end", "on_session_finalize"}
    assert ctx.aux["camofox_vision"]["inherit_from"] == "vision"
    assert "camofox" in ctx.cli


def test_manifest_lists_exactly_the_registered_tools():
    import pathlib
    import hermes_yaml
    manifest = hermes_yaml.safe_load((pathlib.Path(camofox_plugin.__file__).parent / "plugin.yaml").read_text())
    assert set(manifest["provides_tools"]) == EXPECTED_TOOLS


def test_tools_are_offered_only_once_camofox_url_is_set(monkeypatch):
    ctx = _Ctx()
    camofox_plugin.register(ctx)
    check = ctx.tools["browser_navigate"]["check_fn"]
    assert check() is False
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    assert check() is True


def test_cookie_import_needs_the_api_key_too(monkeypatch):
    ctx = _Ctx()
    camofox_plugin.register(ctx)
    check = ctx.tools["browser_import_cookies"]["check_fn"]
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    assert check() is False
    monkeypatch.setenv("CAMOFOX_API_KEY", "k")
    assert check() is True


def test_handlers_take_args_positionally_and_task_id_by_keyword(camofox):
    ctx = _Ctx()
    camofox_plugin.register(ctx)
    import json
    out = json.loads(ctx.tools["browser_navigate"]["handler"]({"url": "https://example.com"}, task_id="h1"))
    assert out["success"] is True
    assert camofox.calls("POST", "/tabs")[0]["body"]["listItemId"] == "task_h1"


def test_each_turn_end_closes_that_tasks_ephemeral_session(camofox):
    """Core cleaned up after every turn; a cron or subagent task id differs from the session id."""
    ctx = _Ctx()
    camofox_plugin.register(ctx)
    ctx.tools["browser_navigate"]["handler"]({"url": "https://example.com"}, task_id="cron:job:1")
    ctx.hooks["on_session_end"](session_id="sess-1", task_id="cron:job:1", completed=True)
    assert len(camofox.calls("DELETE")) == 1


def test_headed_browser_keeps_its_tab_across_turns(camofox, write_config):
    """Core skipped the per-turn browser cleanup with ``browser.headed: true`` (someone is watching
    the window); the session still closes when the conversation is finalized."""
    write_config("browser:\n  headed: true\n")
    ctx = _Ctx()
    camofox_plugin.register(ctx)
    ctx.tools["browser_navigate"]["handler"]({"url": "https://example.com"}, task_id="sess-1")
    ctx.hooks["on_session_end"](session_id="sess-1", task_id="sess-1", completed=True)
    assert camofox.calls("DELETE") == []
    ctx.hooks["on_session_finalize"](session_id="sess-1", platform="cli")
    assert len(camofox.calls("DELETE")) == 1


def test_session_finalize_closes_the_ephemeral_session(camofox):
    ctx = _Ctx()
    camofox_plugin.register(ctx)
    ctx.tools["browser_navigate"]["handler"]({"url": "https://example.com"}, task_id="sess-1")
    ctx.hooks["on_session_finalize"](session_id="sess-1", platform="cli")
    assert len(camofox.calls("DELETE")) == 1


@pytest.fixture
def core_with_camofox(tmp_path, monkeypatch):
    """Simulate a core that still ships ``tools/browser_camofox.py``."""
    import tools
    fake = tmp_path / "core_tools"
    fake.mkdir()
    (fake / "browser_camofox.py").write_text("# bundled Camofox backend\n")
    monkeypatch.setattr(tools, "__path__", list(tools.__path__) + [str(fake)])


def test_inert_on_a_core_that_still_bundles_camofox(real_core_detection, core_with_camofox, caplog):
    ctx = _Ctx()
    with caplog.at_level(logging.INFO):
        camofox_plugin.register(ctx)
    assert ctx.tools == {} and ctx.hooks == {} and ctx.aux == {} and ctx.cli == {}
    lines = [r for r in caplog.records if "camofox plugin inactive" in r.getMessage()]
    assert len(lines) == 1


def test_detection_reads_only_the_running_cores_tools_package(real_core_detection, monkeypatch, tmp_path):
    """A core tree without ``tools/browser_camofox.py`` reads as post-removal."""
    import tools
    empty = tmp_path / "post_removal_tools"
    empty.mkdir()
    monkeypatch.setattr(tools, "__path__", [str(empty)])
    assert camofox_plugin.core_bundles_camofox() is False
