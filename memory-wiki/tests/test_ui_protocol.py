"""Keep UI display dictionaries aligned with actual backend protocol producers."""
from __future__ import annotations

import ast
import os
from pathlib import Path
import re

PLUGIN = Path(__file__).resolve().parents[1]


def _dictionary_keys(source: str, field: str) -> list[set[str]]:
    return [set(re.findall(r"\b([a-z][a-z0-9_]*)\s*:", block))
            for block in re.findall(rf"\b{field}\s*:\s*\{{([^}}]+)\}}", source)]


def test_desktop_localizes_kinds_from_actual_backend_output(memory_wiki_core):
    messages = [
        {"role": "user", "content": "fix regression", "timestamp": 1},
        {"role": "user", "content": "research a topic", "timestamp": 2},
        {"role": "user", "content": "plan roadmap", "timestamp": 3},
        {"role": "user", "content": "日常沟通", "timestamp": 4},
        {"role": "tool", "tool_name": "web_search", "content": "", "timestamp": 5},
    ]
    items = memory_wiki_core._work_items_for_session({"id": "synthetic-protocol", "started_at": 1}, messages)
    produced = {item["kind"] for item in items}
    assert produced == {"coding", "research", "planning", "conversation", "tool"}
    source = (PLUGIN / "desktop/plugin.js").read_text(encoding="utf-8")
    dictionaries = _dictionary_keys(source, "kind")
    assert len(dictionaries) == 2
    assert all(produced <= keys for keys in dictionaries)


def test_both_frontends_cover_host_platform_sources():
    core = Path(os.environ["HERMES_AGENT_REPO"])
    tree = ast.parse((core / "gateway/config.py").read_text(encoding="utf-8"))
    platform = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Platform")
    known = {node.value.value for node in platform.body
             if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
             and isinstance(node.value.value, str)}
    assert "weixin" in known and "email" in known
    desktop = (PLUGIN / "desktop/plugin.js").read_text(encoding="utf-8")
    dictionaries = _dictionary_keys(desktop, "source")
    assert len(dictionaries) == 2
    assert all(known <= keys for keys in dictionaries)
    dashboard = (PLUGIN / "dashboard/src/index.jsx").read_text(encoding="utf-8")
    block = re.search(r"const ZH_SOURCES\s*=\s*\{([^}]+)\}", dashboard).group(1)
    keys = set(re.findall(r"\b([a-z][a-z0-9_]*)\s*:", block))
    assert known <= keys
