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


def test_a_messy_split_reply_pasted_on_stdin_imports_each_thing_once(tmp_path, monkeypatch, capsys):
    """What real chat output looks like: the export split over two code blocks (the second sent
    twice), an example block that is not an export, curly quotes and a trailing comma from copying,
    two skills whose names slug alike, two different routines sharing a name and an unknown time
    zone. Piped on stdin, each routine and skill imports once, both skills stay loadable by their own
    names, the role still lands in SOUL.md, and the job says it runs in the profile's zone."""
    import io
    home = tmp_path / ".hermes"
    bot = EXPORT["bots"][0]
    first = {"format": "grok-bot-export", "version": 1, "bots": [{
        "name": bot["name"], "instructions": bot["instructions"], "skills": bot["skills"],
        "routines": [bot["routines"][0]]}]}
    second = {"format": "grok-bot-export", "version": 1, "bots": [{
        "name": bot["name"],
        "skills": [{"name": "weekly-account-health", "instructions": "A different skill."}],
        "routines": [{"name": "Friday recap", "instruction": "Recap the week.", "trigger": "schedule",
                      "schedule_cron": "0 16 * * 5", "timezone": "Mars/Olympus"},
                     {"name": "Morning watch list", "instruction": "Check the pager.", "trigger": "schedule",
                      "schedule_cron": "0 9 * * *"}]}]}
    messy = json.dumps(second, indent=1).replace('"Recap the week."', "\u201cRecap the week.\u201d")
    messy = messy.replace('"Mars/Olympus"', '"Mars/Olympus",')
    reply = ("Part 1:\n```json\n" + json.dumps(first) + "\n```\nPart 2:\n```json\n" + messy
             + "\n```\nAgain, part 2:\n```json\n" + messy + "\n```\nA routine looks like:\n```json\n"
             + '[{"name": "Example", "schedule": "7am"}]\n```\n')
    monkeypatch.setattr("sys.stdin", io.StringIO(reply))

    _run(["grok-bot", "--source", "-", "-y"])

    jobs = {j["name"]: j for j in _jobs(home)}
    assert sorted(jobs) == ["Account Health: Friday recap", "Account Health: Morning watch list",
                            "Account Health: Morning watch list (2)"]
    skills = home / "skills" / "grok-bot-imports"
    assert sorted(p.name for p in skills.iterdir()) == ["weekly-account-health", "weekly-account-health-2"]
    second_skill = (skills / "weekly-account-health-2" / "SKILL.md").read_text(encoding="utf-8")
    assert "A different skill" in second_skill and "name: weekly-account-health-2\n" in second_skill
    assert "Never contact a customer" in (home / "SOUL.md").read_text(encoding="utf-8")
    assert "unknown time zone 'Mars/Olympus'" in capsys.readouterr().out


def _cache_blob(folder: Path, slice_id: str, value) -> None:
    import base64
    name = base64.b32encode(slice_id.encode()).decode().rstrip("=").lower()
    (folder / f"{name}.blob").write_text(json.dumps({"schemaVersion": 1, "value": value}), encoding="utf-8")


def test_the_grok_bot_apps_local_cache_flags_bots_and_routines_the_export_missed(tmp_path, capsys):
    """With the Grok Bot app installed, its cache (the app's own slice files: the Bot roster and a chat
    that created a routine) lists the Bots to export, and after the import names a routine the
    Bot announced but left out of its export and a Bot nobody exported. Nothing is read from login
    files next to it."""
    cache = tmp_path / "AppData" / "Roaming" / "Grok Bot" / "sand-client-persistence"
    cache.mkdir(parents=True)
    (cache.parent / "sand-secrets.json").write_text("{not json, never read")
    account = "sand.client.slice.account.google-oauth2%7Cuser_01ABC"
    _cache_blob(cache, f"{account}.roster.last-roster", {"rows": [
        {"id": "b1", "name": "Account Health", "isGroup": False},
        {"id": "b2", "name": "Drew", "isGroup": False}]})
    _cache_blob(cache, f"{account}.transcript.replicas.b1", {"entries": [
        {"kind": "event", "event": {"type": "automation-changed", "action": "created",
                                    "automationId": "morning-watch-list", "automationName": "Morning watch list"}},
        {"kind": "event", "event": {"type": "automation-changed", "action": "created",
                                    "automationId": "routine-healthcheck", "automationName": "routine-healthcheck"}}]})

    _run(["grok-bot", "--dry-run"])
    assert "on this computer: Account Health, Drew" in capsys.readouterr().out

    reply = tmp_path / "reply.json"
    reply.write_text(json.dumps(EXPORT), encoding="utf-8")
    _run(["grok-bot", "--source", str(reply), "-y"])
    out = capsys.readouterr().out
    assert "export these Bots too to bring them over: Drew" in out
    assert "did not include: routine-healthcheck" in out and "Morning watch list." not in out
