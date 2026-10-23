"""``hermes import-agent openclaw`` end to end through the plugin's own parser and handler, against a
real fixture OpenClaw home and the real bundled migration script (no mocks)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent_importers_plugin.cli import handle_cli, setup_cli


def _run(argv: list[str]) -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command").add_parser("import-agent")
    setup_cli(sub)
    sub.set_defaults(func=handle_cli)
    args = parser.parse_args(["import-agent", *argv])
    args.func(args)


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _fixture_openclaw(home: Path) -> Path:
    source = home / ".openclaw"
    (source / "workspace" / "skills" / "deploy").mkdir(parents=True)
    (source / "workspace" / "SOUL.md").write_text("You are a careful assistant.\n", encoding="utf-8")
    (source / "workspace" / "MEMORY.md").write_text("# Memory\n\n- Prefers short answers\n", encoding="utf-8")
    (source / "workspace" / "skills" / "deploy" / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: Deploy the app.\n---\n\nRun the deploy.\n", encoding="utf-8")
    (source / "openclaw.json").write_text(json.dumps(
        {"channels": {"telegram": {"botToken": "123:fixture-token"}}}), encoding="utf-8")
    return source


def test_dry_run_previews_and_writes_nothing(tmp_path, capsys):
    hermes_home = tmp_path / ".hermes"
    (hermes_home / "config.yaml").write_text("model: test/model\n", encoding="utf-8")
    _fixture_openclaw(tmp_path)
    before = _snapshot(hermes_home)

    _run(["openclaw", "--dry-run"])  # auto-detected source: ~/.openclaw

    out = capsys.readouterr().out
    assert "OpenClaw Migration" in out
    assert "Migration Preview" in out and "Dry Run Results" in out
    assert "soul" in out and "skill" in out
    assert "hermes import-agent openclaw --preset full" in out
    assert _snapshot(hermes_home) == before


def test_openclaw_only_flags_with_another_agent_import_nothing(tmp_path, capsys):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "AGENTS.md").write_text("- Use uv\n", encoding="utf-8")
    before = _snapshot(tmp_path / ".hermes")

    _run(["codex", "--migrate-secrets", "--preset", "user-data", "-y"])

    out = capsys.readouterr().out
    assert "--preset, --migrate-secrets only apply to OpenClaw imports" in out
    assert _snapshot(tmp_path / ".hermes") == before


def test_fresh_home_takes_the_openclaw_persona_but_a_written_soul_is_kept(tmp_path, capsys):
    """Hermes seeds a default SOUL.md into every new home; replacing it is no conflict. A SOUL.md the
    user wrote still is one, and the apply is refused."""
    from hermes_cli.default_soul import DEFAULT_SOUL_MD

    hermes_home = tmp_path / ".hermes"
    (hermes_home / "SOUL.md").write_text(DEFAULT_SOUL_MD, encoding="utf-8")  # what a new home holds
    _fixture_openclaw(tmp_path)

    _run(["openclaw", "--yes", "--no-backup"])

    assert (hermes_home / "SOUL.md").read_text(encoding="utf-8") == "You are a careful assistant.\n"
    assert "Refusing to apply" not in capsys.readouterr().out

    (hermes_home / "SOUL.md").write_text("My own persona.\n", encoding="utf-8")
    _run(["openclaw", "--yes", "--no-backup"])

    assert "Refusing to apply" in capsys.readouterr().out
    assert (hermes_home / "SOUL.md").read_text(encoding="utf-8") == "My own persona.\n"


def test_run_from_setup_speaks_to_setup(tmp_path, capsys):
    """Inside `hermes setup` (which passes --from-setup) the preview leads straight into the question:
    no "run without --dry-run" line, and the many skipped items collapse into one line (the report
    file keeps the detail)."""
    _fixture_openclaw(tmp_path)

    _run(["openclaw", "--from-setup", "--yes", "--no-backup"])

    out = capsys.readouterr().out
    assert "without --dry-run" not in out
    skipped = [line for line in out.splitlines() if "Skipped" in line]
    assert len(skipped) == 2 and all("nothing to bring over" in line for line in skipped)  # preview + result
    assert "No Discord settings found" not in out
    report_dirs = list((tmp_path / ".hermes" / "migration" / "openclaw").iterdir())
    assert "No Discord settings found" in (report_dirs[0] / "report.json").read_text(encoding="utf-8")


def test_a_plain_run_previews_without_the_dry_run_hint(tmp_path, capsys):
    """Only an explicit --dry-run is told how to run for real; a normal run asks right after."""
    _fixture_openclaw(tmp_path)

    _run(["openclaw", "--yes", "--no-backup"])

    out = capsys.readouterr().out
    assert "Migration Results" in out and "without --dry-run" not in out
    assert "No Discord settings found" in out  # outside setup every skipped item is still listed
