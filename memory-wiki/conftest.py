"""Test bootstrap: put the hermes-agent checkout on ``sys.path`` (for ``hermes_state`` /
``tools.memory_tool`` / FastAPI) and expose the plugin's modules by path.

Set ``HERMES_AGENT_REPO`` to point at a checkout; defaults to ``~/.hermes/hermes-agent``.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent
DASHBOARD_DIR = REPO_ROOT / "dashboard"
HERMES_AGENT_REPO = Path(os.environ.get("HERMES_AGENT_REPO", Path.home() / ".hermes" / "hermes-agent"))

if str(HERMES_AGENT_REPO) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT_REPO))

collect_ignore = ["__init__.py", "dashboard"]


def _load_by_path(module_name: str, path: Path):
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def memory_wiki_core():
    return _load_by_path("hermes_dashboard_plugin_memory_wiki_core", DASHBOARD_DIR / "memory_wiki_core.py")


@pytest.fixture(scope="session")
def memory_wiki_cache():
    return _load_by_path("hermes_dashboard_plugin_memory_wiki_cache", DASHBOARD_DIR / "memory_wiki_cache.py")


@pytest.fixture(scope="session")
def plugin_api():
    return _load_by_path("hermes_dashboard_plugin_memory_wiki_api", DASHBOARD_DIR / "plugin_api.py")


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so the plugin never reads the developer's real state.db / memories."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home
