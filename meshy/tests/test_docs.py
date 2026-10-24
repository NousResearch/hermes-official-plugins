"""Docs contracts: what the README and the skill tell users and the agent must agree with the manifest and each other."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
SCHEMA = MANIFEST["extensions"]["com.nousresearch.hermes"]["config_schema"]
README = (ROOT / "README.md").read_text(encoding="utf-8")
SKILL_DIR = ROOT / "skills" / "meshy-3d"
SKILL_DOCS = {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8")
              for p in [SKILL_DIR / "SKILL.md", *sorted((SKILL_DIR / "references").glob("*.md"))]}


def test_readme_gives_a_cli_command_for_every_setting():
    """Every option must be reachable off-Desktop as a plain `hermes config set` key."""
    missing = []
    for key, spec in SCHEMA.items():
        command = (f"hermes config set {spec['env']} " if spec.get("type") == "secret"
                   else f"hermes config set plugins.entries.meshy.settings.{key} ")
        if command not in README:
            missing.append(command)
    assert not missing, missing


def test_every_doc_sends_the_user_to_the_same_meshy_key_page():
    links = re.findall(r"meshy\.ai/[\w/-]+", SCHEMA["api_key"]["description"])
    assert links, "the settings form must say where the key is created"
    page = links[0]
    for name, text in {"README.md": README, "SKILL.md": SKILL_DOCS["skills/meshy-3d/SKILL.md"]}.items():
        assert page in text, f"{name} does not point at {page}"


def test_upload_caps_agree_across_the_docs():
    caps: dict[str, dict[str, set[str]]] = {}
    for name, text in {"README.md": README, **SKILL_DOCS}.items():
        for platform, mb in re.findall(r"\b(Telegram|Discord|Slack|WhatsApp|Signal)\b[^.\d]{0,40}?(\d+)\s*[MG]B", text):
            caps.setdefault(platform, {}).setdefault(mb, set()).add(name)
    assert caps.get("Telegram") and caps.get("Discord"), caps
    disagreeing = {platform: found for platform, found in caps.items() if len(found) > 1}
    assert not disagreeing, disagreeing


def test_the_skill_never_has_the_agent_send_a_markdown_image():
    """Streaming gateways (Telegram's default) drop image links and leave the raw markdown: attach with MEDIA:."""
    offenders = []
    for name, text in SKILL_DOCS.items():
        for sentence in re.split(r"(?<=[.:])\s", text):
            if "![" in sentence and "never" not in sentence.lower():
                offenders.append((name, sentence.strip()[:120]))
    assert not offenders, offenders


def test_no_doc_promises_unattended_runs_never_spend():
    """Core reads `approvals.mode: off`, a `--yolo` process and saved Always answers in cron and `chat -q` too, so an
    unattended run spends up to the spend limit, and without one when `spend_limit: 0`. Every doc that offers those
    settings says so; the README's refusal sentence is conditional on the defaults."""
    assert "unattended runs refuse" not in README.lower()
    assert re.search(r"cannot approve, so with the default settings paid calls are refused", README)
    for name, text in {"README.md": README, "SKILL.md": SKILL_DOCS["skills/meshy-3d/SKILL.md"]}.items():
        flat = " ".join(text.split())
        assert re.search(r"unattended runs.{0,400}without asking.{0,120}spend limit", flat), name
        assert re.search(r"spend_limit: 0`.{0,200}(no|without any) cap", flat), name
