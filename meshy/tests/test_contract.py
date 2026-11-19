"""Contracts against real Hermes core (CI installs hermes-agent main and runs this with it on PYTHONPATH).

The package goes through core's own Agent Plugins loader, and the server it declares goes through core's own
MCP approval gate: a core change that would make Meshy spend credits without asking fails here, not on a user.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def package(tmp_path, monkeypatch):
    """This checkout installed the way `hermes plugins install` lays it out, loaded by core."""
    home = tmp_path / "hermes"
    root = home / "plugins" / "meshy"
    shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns("node_modules", "tests", "__pycache__", ".pytest_cache"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name in ("HERMES_INTERACTIVE", "HERMES_GATEWAY_SESSION", "HERMES_YOLO_MODE", "HERMES_SESSION_KEY"):
        monkeypatch.delenv(name, raising=False)
    from hermes_cli.agent_plugins import load_agent_plugin

    return load_agent_plugin(root, home / "plugin-data" / "meshy")


def test_core_loads_the_package_as_an_untrusted_pinned_server_with_its_skill(package):
    server = package.mcp_servers["meshy"]
    assert server["trust"] == "untrusted"  # every write-capable Meshy call asks first
    assert any("@meshy-ai/meshy-mcp-server@" in str(a) for a in server.get("args", []))  # pinned, never @latest
    assert [s.name for s in package.skills] == ["meshy-3d"]
    # A declaration key this core does not know yet may only warn; anything else is a broken package.
    assert all("needs a newer Hermes" in d.message for d in package.diagnostics), package.diagnostics


def test_core_refuses_a_paid_meshy_call_when_nobody_can_approve(package):
    from tools.mcp_tool_handlers import _trust_gate_check
    from tools.mcp_tool_registration import _record_tool_trust_metadata

    tools = [SimpleNamespace(name="meshy_text_to_3d", annotations=None),
             SimpleNamespace(name="meshy_check_balance", annotations=SimpleNamespace(readOnlyHint=True))]
    _record_tool_trust_metadata("meshy", package.mcp_servers["meshy"], tools)
    refused = (_trust_gate_check("meshy", "meshy_text_to_3d") or "").lower()
    assert "not approve" in refused or "blocked" in refused, refused
    assert _trust_gate_check("meshy", "meshy_check_balance") is None  # read-only calls never prompt


def test_core_gives_a_meshy_call_longer_than_the_servers_longest_wait(package):
    """get_task_status waits up to 300 s and creative_lab up to 600 s per stage for 2 stages: on core's
    300 s MCP default the call is cut off mid-wait and 3 such timeouts trip the server's breaker."""
    from tools.mcp_tool_common import _resolve_tool_timeout

    assert _resolve_tool_timeout(package.mcp_servers["meshy"]) > 2 * 600


def _messaging_reply_example() -> str:
    """The reply the skill tells the agent to send with a thumbnail on a chat platform."""
    doc = (ROOT / "skills" / "meshy-3d" / "references" / "messaging.md").read_text(encoding="utf-8")
    blocks = [b for b in re.findall(r"```[^\n]*\n(.*?)```", doc, re.DOTALL) if "MEDIA:" in b]
    assert len(blocks) == 1, blocks
    return blocks[0]


def test_core_delivers_the_skills_thumbnail_reply_as_a_photo_even_in_strict_media_mode(tmp_path):
    """The streaming gateway path (Telegram's default) delivers only MEDIA: lines, so the skill's thumbnail reply
    must be a MEDIA: line to a file core accepts. Strict mode with recency trust off leaves only core's delivery
    folders, and a fresh interpreter makes core compute those folders from this HERMES_HOME."""
    home = tmp_path / "hermes"
    reply = _messaging_reply_example().replace("<HERMES_HOME>", str(home)).replace("<task_id>", "0abc")
    probe = textwrap.dedent("""
        import json, os, sys
        from pathlib import Path
        home, reply = Path(sys.argv[1]), sys.argv[2]
        old = 1_000_000_000  # long before any recency window
        for sub in ("cache/images", "cache/meshy-preview"):  # the second is a control outside the delivery folders
            f = home / sub / "meshy-0abc.png"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"png")
            os.utime(f, (old, old))
        from gateway.platforms.base import BasePlatformAdapter, validate_media_delivery_path
        from gateway.run_notifications import _IMAGE_EXTS
        media, cleaned = BasePlatformAdapter.extract_media(reply)
        kept = BasePlatformAdapter.filter_media_delivery_paths(media)
        print(json.dumps({"media": media, "kept": kept, "cleaned": cleaned,
                          "photo": [Path(p).suffix.lower() in _IMAGE_EXTS for p, _ in kept],
                          "control": validate_media_delivery_path(str(home / "cache/meshy-preview/meshy-0abc.png"))}))
    """)
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_")}
    env.update(HOME=str(tmp_path), HERMES_HOME=str(home), HERMES_MEDIA_DELIVERY_STRICT="1", HERMES_MEDIA_TRUST_RECENT_FILES="0")
    out = subprocess.run([sys.executable, "-c", probe, str(home), reply], env=env, capture_output=True,
                         text=True, encoding="utf-8", check=True).stdout
    result = json.loads(out.strip().splitlines()[-1])
    assert result["control"] is None, result  # strict mode is really on: a non-delivery folder is refused
    expected = (home / "cache" / "images" / "meshy-0abc.png").resolve()
    assert [Path(p) for p, _ in result["kept"]] == [expected], result
    assert result["photo"] == [True], result
    assert "MEDIA:" not in result["cleaned"] and "](" not in result["cleaned"], result
