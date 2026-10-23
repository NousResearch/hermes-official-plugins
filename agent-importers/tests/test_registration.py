"""register(ctx): ``hermes import-agent`` and ``hermes claw`` keep core's spelling and flags, and each
stays inert on a core that still ships its own copy (two subparsers of one name would abort
plugin-command wiring)."""

from __future__ import annotations

import argparse

import pytest

import agent_importers_plugin


class RecordingCtx:
    def __init__(self):
        self.cli = []
        self.skills = []

    def register_cli_command(self, **kwargs):
        self.cli.append(kwargs)

    def register_skill(self, **kwargs):
        self.skills.append(kwargs)


def _register(monkeypatch, core_ships: bool, core_ships_claw: bool = False) -> RecordingCtx:
    monkeypatch.setattr(agent_importers_plugin, "_core_ships_import_agent", lambda: core_ships)
    monkeypatch.setattr(agent_importers_plugin, "_core_ships_claw", lambda: core_ships_claw)
    ctx = RecordingCtx()
    agent_importers_plugin.register(ctx)
    return ctx


def _parser(ctx: RecordingCtx) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    for cmd in ctx.cli:
        sub = subparsers.add_parser(cmd["name"])
        cmd["setup_fn"](sub)
        sub.set_defaults(func=cmd["handler_fn"])
    return parser


def test_registers_import_agent_with_cores_flags(monkeypatch):
    ctx = _register(monkeypatch, core_ships=False, core_ships_claw=True)
    (cmd,) = ctx.cli
    assert cmd["name"] == "import-agent"
    parser = _parser(ctx)
    args = parser.parse_args(["import-agent", "codex", "--source", "/x", "--dry-run", "--overwrite", "-y"])
    assert (args.agent, args.source, args.dry_run, args.overwrite, args.yes, args.sync) == (
        "codex", "/x", True, True, True, False)
    assert parser.parse_args(["import-agent", "--sync"]).sync is True
    with pytest.raises(SystemExit):
        parser.parse_args(["import-agent", "windsurf"])


def test_inert_on_a_core_that_still_ships_both(monkeypatch):
    ctx = _register(monkeypatch, core_ships=True, core_ships_claw=True)
    assert (ctx.cli, ctx.skills) == ([], [])


def test_claw_and_skill_register_on_a_core_without_claw(monkeypatch):
    ctx = _register(monkeypatch, core_ships=False, core_ships_claw=False)
    assert [c["name"] for c in ctx.cli] == ["import-agent", "claw"]
    (skill,) = ctx.skills
    assert skill["name"] == "openclaw-migration"
    assert skill["path"].read_text(encoding="utf-8").startswith("---\nname: openclaw-migration\n")


def test_claw_guard_is_independent_of_the_import_agent_guard(monkeypatch):
    ctx = _register(monkeypatch, core_ships=True, core_ships_claw=False)
    assert [c["name"] for c in ctx.cli] == ["claw"]
    assert len(ctx.skills) == 1


def test_import_agent_openclaw_takes_the_claw_migrate_flags(monkeypatch):
    parser = _parser(_register(monkeypatch, core_ships=False))
    args = parser.parse_args([
        "import-agent", "openclaw", "--preset", "user-data", "--migrate-secrets", "--no-backup",
        "--source", "/oc", "--workspace-target", "/ws", "--skill-conflict", "rename",
        "--overwrite", "--dry-run", "-y"])
    assert (args.agent, args.preset, args.migrate_secrets, args.no_backup, args.source,
            args.workspace_target, args.skill_conflict, args.overwrite, args.dry_run, args.yes) == (
        "openclaw", "user-data", True, True, "/oc", "/ws", "rename", True, True, True)
    with pytest.raises(SystemExit):
        parser.parse_args(["import-agent", "openclaw", "--preset", "everything"])


def test_claw_migrate_and_cleanup_parse(monkeypatch):
    parser = _parser(_register(monkeypatch, core_ships=False))
    migrate = parser.parse_args(["claw", "migrate", "--dry-run"])
    assert (migrate.claw_action, migrate.preset, migrate.skill_conflict, migrate.migrate_secrets,
            migrate.no_backup) == ("migrate", "full", "skip", False, False)
    full = parser.parse_args(["claw", "migrate", "--preset", "full", "--migrate-secrets", "--yes"])
    assert (full.preset, full.migrate_secrets, full.yes) == ("full", True, True)
    for spelling in ("cleanup", "clean"):
        cleanup = parser.parse_args(["claw", spelling, "--source", "/oc", "--dry-run", "-y"])
        assert (cleanup.claw_action, cleanup.source, cleanup.dry_run, cleanup.yes) == (
            spelling, "/oc", True, True)


def test_real_discovery_matches_the_running_core(tmp_path, monkeypatch):
    """Loaded the way users get it: copied into the profile's plugins/ and enabled in config.yaml."""
    import shutil

    from hermes_cli.plugins import PluginManager

    home = tmp_path / ".hermes"
    shutil.copytree(agent_importers_plugin.__path__[0], home / "plugins" / "agent-importers",
                    ignore=shutil.ignore_patterns("tests", "__pycache__", ".pytest_cache"))
    (home / "config.yaml").write_text("plugins:\n  enabled: [agent-importers]\n", encoding="utf-8")
    manager = PluginManager()
    manager.discover_and_load()
    assert ("import-agent" in manager._cli_commands) is not agent_importers_plugin._core_ships_import_agent()
    claw = not agent_importers_plugin._core_ships_claw()
    assert ("claw" in manager._cli_commands) is claw
    assert ("agent-importers:openclaw-migration" in manager._plugin_skills) is claw
