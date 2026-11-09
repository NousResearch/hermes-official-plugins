"""Real discovery: the plugin, copied into a temp HERMES_HOME and enabled in its config.yaml, is loaded
by core's PluginManager. Its pre_tool_call hook blocks a bad PR body, and its prompt section renders
with the session's model and the configured reasoning effort. Needs a hermes-agent checkout on
PYTHONPATH.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = "details-summary"
pytest.importorskip("hermes_cli.plugins")


@pytest.fixture
def manager(tmp_path, monkeypatch):
    from hermes_cli import plugins as plugins_mod

    home = tmp_path / "os-home" / ".hermes"
    shutil.copytree(ROOT, home / "plugins" / PLUGIN,
                    ignore=shutil.ignore_patterns("tests", "assets", "__pycache__", ".pytest_cache"))
    (home / "config.yaml").write_text(
        f"plugins:\n  enabled: [{PLUGIN}]\nagent:\n  reasoning_effort: high\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "os-home")
    empty_bundled = tmp_path / "bundled"
    empty_bundled.mkdir()
    monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: empty_bundled)
    m = plugins_mod.PluginManager()
    m.discover_and_load()
    return m


def test_hook_blocks_a_bad_body(manager, tmp_path):
    (tmp_path / "body.md").write_text("Adds a thing.", encoding="utf-8")
    results = manager.invoke_hook(
        "pre_tool_call", tool_name="terminal",
        args={"command": "gh pr create --body-file body.md", "workdir": str(tmp_path)})
    blocks = [r for r in results if isinstance(r, dict) and r.get("action") == "block"]
    assert blocks and "No <details>" in blocks[0]["message"]


def test_prompt_section_names_model_and_effort(manager):
    sections = manager.render_system_prompt_sections({"model": "anthropic/claude-opus-5.5"})
    text = next(s.content for s in sections if s.id == "details-summary")
    assert "Claude Opus 5.5 High" in text
