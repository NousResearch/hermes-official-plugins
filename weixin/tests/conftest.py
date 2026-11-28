"""Test bootstrap: import the plugin directory as the package ``weixin_plugin``.

The Hermes plugin loader imports a directory plugin as a package (``hermes_plugins.<slug>``) so the
relative imports in ``__init__.py`` resolve; tests mirror that under a stable alias. Hermes core
(``gateway``, ``agent``, ...) must be importable — run with a hermes-agent checkout on ``PYTHONPATH``.

The adapter resolves ``Platform("weixin")``. Once core no longer ships Weixin, that only resolves for a
platform the registry knows, so register the plugin's real ``PlatformEntry`` up front exactly as the
loader would. On a core that still bundles Weixin the plugin is inert and core's own entry serves.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from pathlib import Path

import pytest

PACKAGE = "weixin_plugin"
_ROOT = Path(__file__).resolve().parents[1]

sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _ROOT]


def _load_plugin_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


def _ensure_platform_registered() -> None:
    from gateway.platform_registry import PlatformEntry, platform_registry

    adapter = importlib.import_module(f"{PACKAGE}.adapter")
    if adapter.core_ships_weixin() or platform_registry.is_registered(adapter.PLATFORM_NAME):
        return
    platform_registry.register(PlatformEntry(source="plugin", **adapter.platform_kwargs()))


_load_plugin_package()
_ensure_platform_registered()


class _RepoRootIsNotATestPackage:
    """The plugin root *is* the package (it has an ``__init__.py`` with relative imports), so pytest would
    collect it as a test ``Package`` and import that ``__init__.py`` standalone during setup, where the
    relative imports cannot resolve. Collect the root as a plain directory instead.

    Registered as a global plugin (not a conftest hook) because the root directory's collector is created
    through its *parent's* hook proxy, which never sees a conftest inside the root."""

    @pytest.hookimpl(tryfirst=True)
    def pytest_collect_directory(self, path, parent):
        if Path(path).resolve() == _ROOT:
            return pytest.Dir.from_parent(parent, path=path)
        return None


def pytest_collection_modifyitems(config, items):
    """On a core that still bundles Weixin the plugin is inert and core's own adapter class serves the
    registry, so tests that pin the plugin's registry entry do not apply there."""
    if not importlib.import_module(f"{PACKAGE}.adapter").core_ships_weixin():
        return
    skip = pytest.mark.skip(reason="this Hermes core still bundles Weixin; the plugin is inert")
    for item in items:
        if item.get_closest_marker("plugin_registry"):
            item.add_marker(skip)


def pytest_configure(config):
    config.addinivalue_line("markers", "plugin_registry: needs the plugin's own registry entry (core without Weixin)")
    if not config.pluginmanager.has_plugin("weixin-repo-root"):
        config.pluginmanager.register(_RepoRootIsNotATestPackage(), "weixin-repo-root")


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path, monkeypatch):
    """Every test gets its own HERMES_HOME and no inherited WEIXIN_* env (``setenv`` only: patching
    ``get_hermes_home`` leaks into modules first imported during the test)."""
    home = tmp_path / ".hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name in [n for n in os.environ if n.startswith("WEIXIN_")]:
        monkeypatch.delenv(name, raising=False)
    yield home
