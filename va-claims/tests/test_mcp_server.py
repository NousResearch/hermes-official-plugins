"""The MCP shim (mcp_server.py) exposes the same seven tools over MCP.

Skips when the ``mcp`` package is absent; the plugin itself never needs it."""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest

mcp = pytest.importorskip("mcp")

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def mcp_server_module():
    sys.path.insert(0, str(_ROOT))
    try:
        mod = importlib.import_module("mcp_server")
    finally:
        sys.path.remove(str(_ROOT))
    return mod


def test_mcp_server_exposes_the_same_seven_tools(mcp_server_module):
    tools = asyncio.run(mcp_server_module.server.list_tools())
    assert sorted(t.name for t in tools) == sorted([
        "va_claim_status", "va_claim_track", "va_disability_lookup",
        "va_intent_to_file", "va_benefit_track", "va_benefit_summary",
        "va_claim_guide",
    ])


def test_mcp_call_returns_guide_text(mcp_server_module, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    res = asyncio.run(mcp_server_module.server.call_tool(
        "va_claim_guide", {"topic": "timeline"}))
    assert "38 CFR" in res.content[0].text
