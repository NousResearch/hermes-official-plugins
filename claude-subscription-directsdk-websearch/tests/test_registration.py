"""The companion plugin is discovered by Hermes' own plugin loader and registers the web provider.

Also pins the finding behind the companion layout: a ``kind: model-provider`` plugin never has
``register(ctx)`` called, so the web provider cannot live in the model provider's own ``__init__.py``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODEL_PROVIDER = ROOT.parent / "claude-subscription-directsdk"
PROVIDER_ID = "claude-subscription-websearch"
SETTINGS_ID = "claude-subscription-directsdk-experimental"


def _discover(home: Path, monkeypatch, enabled: list[str], settings: str = "") -> None:
    ignore = shutil.ignore_patterns("tests", "evals", "__pycache__", ".git")
    shutil.copytree(MODEL_PROVIDER, home / "plugins" / SETTINGS_ID, ignore=ignore)
    shutil.copytree(ROOT, home / "plugins" / "claude-subscription-websearch", ignore=ignore)
    (home / "config.yaml").write_text(f"plugins:\n  enabled: [{', '.join(enabled)}]\n{settings}", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    from agent import web_search_registry
    from hermes_cli import plugins
    monkeypatch.setattr(plugins, "_plugin_manager", None, raising=False)
    web_search_registry._registry.reset_for_tests()
    plugins.discover_plugins(force=True) if "force" in plugins.discover_plugins.__code__.co_varnames else plugins.discover_plugins()


@pytest.fixture
def registry():
    from agent import web_search_registry
    yield web_search_registry
    web_search_registry._registry.reset_for_tests()


def test_manifest_is_a_backend_that_declares_its_provider():
    import hermes_yaml as yaml
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["kind"] == "backend"
    assert manifest["provides_web_providers"] == [PROVIDER_ID]
    assert yaml.safe_load((MODEL_PROVIDER / "plugin.yaml").read_text(encoding="utf-8"))["kind"] == "model-provider"


def test_model_provider_manifest_documents_the_settings_and_the_egress_bypass():
    import hermes_yaml as yaml
    schema = yaml.safe_load((MODEL_PROVIDER / "plugin.yaml").read_text(encoding="utf-8"))["config_schema"]
    assert schema["native_web_search"]["default"] is False and schema["native_web_search"]["type"] == "bool"
    assert schema["native_web_search_model"]["default"] == "haiku"
    text = " ".join(schema["native_web_search"]["description"].split())
    assert "Claude subscription" in text and "bypass Hermes-side web config and egress settings" in text


def test_enabled_companion_registers_the_web_provider(tmp_path, monkeypatch, registry):
    _discover(tmp_path / "home", monkeypatch, ["claude-subscription-websearch"])
    provider = registry.get_provider(PROVIDER_ID)
    assert provider is not None and provider.supports_search() and not provider.supports_extract()


def test_not_enabled_registers_nothing(tmp_path, monkeypatch, registry):
    _discover(tmp_path / "home", monkeypatch, [])
    assert registry.get_provider(PROVIDER_ID) is None


def test_model_provider_init_does_not_register_web_providers():
    """Its register(ctx) would never run (hermes_cli.plugins_discovery skips model-provider kinds)."""
    source = (MODEL_PROVIDER / "__init__.py").read_text(encoding="utf-8")
    assert "register_web_search_provider" not in source and "def register(" not in source
