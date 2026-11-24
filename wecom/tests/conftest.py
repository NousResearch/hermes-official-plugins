"""Test bootstrap: import the plugin directory as the package ``wecom_plugin``.

The Hermes plugin loader imports a directory plugin as a package (``hermes_plugins.<slug>``) so the
relative imports in ``__init__.py`` resolve; tests mirror that under a stable alias. Hermes core
(``gateway``, ``agent``, ...) must be importable — run with a hermes-agent checkout on ``PYTHONPATH``.

The adapters resolve ``Platform("wecom")`` / ``Platform("wecom_callback")``. Once core no longer ships
WeCom, those only resolve for platforms the registry knows, so register the plugin's two real
``PlatformEntry`` rows up front exactly as the loader would. On a core that still bundles WeCom the plugin
is inert and core's own entries serve.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from pathlib import Path

import pytest

PACKAGE = "wecom_plugin"
_ROOT = Path(__file__).resolve().parents[1]

# ``python -m pytest`` from the plugin dir puts it on sys.path; keep the plugin importable only as a package.
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


def _ensure_platforms_registered() -> None:
    from gateway.platform_registry import PlatformEntry, platform_registry

    register = importlib.import_module(f"{PACKAGE}.register")
    if register.core_ships_wecom():
        return
    for kwargs in (register.platform_kwargs(), register.callback_platform_kwargs()):
        if not platform_registry.is_registered(kwargs["name"]):
            platform_registry.register(PlatformEntry(source="plugin", **kwargs))


_load_plugin_package()
_ensure_platforms_registered()


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
    """On a core that still bundles WeCom the plugin is inert and core's own adapter classes serve the
    registry, so tests that pin the plugin's registry entries do not apply there."""
    if not importlib.import_module(f"{PACKAGE}.register").core_ships_wecom():
        return
    skip = pytest.mark.skip(reason="this Hermes core still bundles WeCom; the plugin is inert")
    for item in items:
        if item.get_closest_marker("plugin_registry"):
            item.add_marker(skip)


def pytest_configure(config):
    config.addinivalue_line("markers", "plugin_registry: needs the plugin's own registry entries (core without WeCom)")
    if not config.pluginmanager.has_plugin("wecom-repo-root"):
        config.pluginmanager.register(_RepoRootIsNotATestPackage(), "wecom-repo-root")


_ENV_PREFIXES = ("WECOM_", "HERMES_WECOM_", "HERMES_LANGUAGE")


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path, monkeypatch):
    """Every test gets its own HERMES_HOME and no inherited WECOM_* env (``setenv`` only: patching
    ``get_hermes_home`` leaks into modules first imported during the test)."""
    home = tmp_path / ".hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name in [n for n in os.environ if n.startswith(_ENV_PREFIXES)]:
        monkeypatch.delenv(name, raising=False)
    yield home
