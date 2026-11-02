"""``hermes import-agent grok-bot`` end to end through the plugin's own parser and handler, against a
Bot's pasted export reply, the real cron store and a real Hermes home (no mocks)."""

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


EXPORT = {
    "format": "grok-bot-export", "version": 1,
    "bots": [{
        "name": "Account Health", "title": "Customer success",
        "instructions": "Own the weekly account-health review. Never contact a customer without approval.",
        "memories": ["Churn signal: three weeks without a login", "Token sk-abcdefghijklmnopqrstuvwx is old"],
        "user_facts": ["Works in America/New_York"],
        "skills": [{"name": "Weekly account health", "description": "Score accounts each week",
                    "instructions": "1. Pull usage.\n2. Flag churn.\n3. Post a watch list."}],
        "routines": [
            {"name": "Morning watch list", "instruction": "Run the Weekly account health skill.",
             "trigger": "schedule", "schedule_cron": "0 8 * * 1-5", "schedule_text": "Every weekday at 8:00 AM",
             "timezone": "America/New_York", "paused": False, "skills": ["Weekly account health"]},
            {"name": "Escalations", "instruction": "Reproduce the ticket in staging.", "trigger": "slack",
             "trigger_details": "#customer-escalations, phrase 'needs repro'"},
        ],
        "apps": [{"name": "Linear", "mcp_url": "https://mcp.linear.app/mcp"}, {"name": "Gmail", "mcp_url": ""}],
    }],
}


def _jobs(home: Path) -> list[dict]:
    path = home / "cron" / "jobs.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    return data["jobs"] if isinstance(data, dict) else data


def test_pasted_export_becomes_paused_cron_jobs_skills_memories_and_soul(tmp_path, capsys):
    """The Bot's whole reply pasted into a file imports: a scheduled routine becomes a paused cron job
    with its skill attached, the event routine is kept as notes, and a re-run adds no duplicate."""
    from hermes_cli.default_soul import DEFAULT_SOUL_MD
    home = tmp_path / ".hermes"
    (home / "SOUL.md").write_text(DEFAULT_SOUL_MD, encoding="utf-8")
    reply = tmp_path / "reply.md"
    reply.write_text("Here is my export:\n\n```json\n" + json.dumps(EXPORT, indent=2) + "\n```\n", encoding="utf-8")

    _run(["grok-bot", "--source", str(reply), "-y"])
    _run(["grok-bot", "--source", str(reply), "-y"])

    [job] = _jobs(home)
    assert job["name"] == "Account Health: Morning watch list"
    assert job["schedule"]["expr"] == "0 8 * * 1-5" and job["enabled"] is False
    assert job["skills"] == ["weekly-account-health"]
    assert "Pull usage" in (home / "skills" / "grok-bot-imports" / "weekly-account-health" / "SKILL.md").read_text()
    memory = (home / "memories" / "MEMORY.md").read_text(encoding="utf-8")
    assert "three weeks without a login" in memory and "sk-abc" not in memory and "[redacted]" in memory
    assert "America/New_York" in (home / "memories" / "USER.md").read_text(encoding="utf-8")
    assert "Never contact a customer" in (home / "SOUL.md").read_text(encoding="utf-8")
    notes = (home / "grok-bot-import" / "account-health-event-routines.md").read_text(encoding="utf-8")
    assert "Reproduce the ticket in staging" in notes
    assert "https://mcp.linear.app/mcp" in (home / "config.yaml").read_text(encoding="utf-8")
    assert "hermes cron resume" in capsys.readouterr().out


def test_without_an_export_it_prints_the_prompt_and_failures_exit_non_zero(tmp_path, capsys):
    """Nothing to read yet: the user gets the message to paste into the Bot and nothing is written. A
    missing --source or a credential-carrying export fails the command (exit 1) and imports nothing;
    an export holding only event-triggered routines still saves their notes."""
    import pytest
    home = tmp_path / ".hermes"
    _run(["grok-bot", "--dry-run"])
    out = capsys.readouterr().out
    assert '"format": "grok-bot-export"' in out and "hermes import-agent grok-bot --source" in out
    assert not _jobs(home) and not (home / "grok-bot-import").exists()

    with pytest.raises(SystemExit) as missing:
        _run(["grok-bot", "--source", str(tmp_path / "nope.json"), "-y"])
    assert missing.value.code == 1

    bad = dict(EXPORT, bots=[dict(EXPORT["bots"][0], api_key="xai-123")])
    path = tmp_path / "Downloads" / "grok-bot-export.json"
    path.parent.mkdir()
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(SystemExit) as refused:
        _run(["grok-bot", "-y"])  # found in ~/Downloads without --source
    assert refused.value.code == 1 and "credential-looking keys" in capsys.readouterr().out
    assert not _jobs(home) and not (home / "skills" / "grok-bot-imports").exists()

    bot = EXPORT["bots"][0]
    events_only = {"format": "grok-bot-export", "version": 1,
                   "bots": [{"name": bot["name"], "routines": [bot["routines"][1]]}]}
    path.write_text(json.dumps(events_only), encoding="utf-8")
    _run(["grok-bot", "-y"])
    notes = home / "grok-bot-import" / "account-health-event-routines.md"
    assert "Reproduce the ticket in staging" in notes.read_text(encoding="utf-8")
