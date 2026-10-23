"""Contracts against real Hermes core (CI installs hermes-agent main and runs this with it on PYTHONPATH).

The package goes through core's own Agent Plugins loader, and the server it declares goes through core's own
MCP approval gate: a core change that would make Meshy spend credits without asking fails here, not on a user.
"""

from __future__ import annotations

import shutil
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
