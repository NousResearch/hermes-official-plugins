"""Test bootstrap and shared fixture.

The standalone plugin imports Hermes core (`providers`, `agent`) from a checkout: set
``HERMES_AGENT_REPO`` to your hermes-agent clone (default ``~/.hermes/hermes-agent``).

``profile`` loads this repo exactly as `hermes plugins install` leaves it —
``$HERMES_HOME/plugins/<name>/`` with ``kind: model-provider`` — through core's provider discovery.
"""
import os
import shutil
import sys
from pathlib import Path

import pytest

HERMES_AGENT_REPO = Path(os.environ.get("HERMES_AGENT_REPO") or Path.home() / ".hermes" / "hermes-agent").expanduser()
REPO_ROOT = Path(__file__).resolve().parent
PLUGIN_NAME = "claude-subscription-directsdk-experimental"
for path in (str(HERMES_AGENT_REPO), str(REPO_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

collect_ignore = ["__init__.py", "directsdk.py", "directsdk_setup.py", "admission.py", "model_catalog.py",
                  "inert_mcp.py", "login_health.py", "evals"]


@pytest.fixture
def profile(tmp_path, monkeypatch):
    import providers

    home = tmp_path / "hermes-home"
    installed = home / "plugins" / PLUGIN_NAME
    shutil.copytree(REPO_ROOT, installed, ignore=shutil.ignore_patterns(".git", "tests", "evals", "__pycache__"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text("plugins:\n  enabled: []\n", encoding="utf-8")
    # A machine with Claude Code installed must not leak it into offline tests.
    monkeypatch.setenv("CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND", str(tmp_path / "no-such-claude"))
    for name in tuple(sys.modules):
        if name.startswith("_hermes_user_provider_"):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(providers, "_REGISTRY", {})
    monkeypatch.setattr(providers, "_ALIASES", {})
    monkeypatch.setattr(providers, "_PROVIDER_LIST_CACHE", None)
    monkeypatch.setattr(providers, "_discovered", False)
    providers._discover_providers()
    result = providers.get_provider_profile(PLUGIN_NAME)
    assert result is not None, "installed-plugin discovery (step 2b) did not register the provider"
    return result
