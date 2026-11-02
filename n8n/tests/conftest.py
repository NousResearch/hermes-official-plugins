"""Test bootstrap: import the plugin directory as the package ``n8n_plugin``.

The Hermes plugin loader imports a directory plugin as a package (``hermes_plugins.<slug>``) so the
relative imports in ``__init__.py`` resolve; tests mirror that under a stable alias. Hermes core
(``hermes_cli``, ``tools``) must be importable: run with a hermes-agent checkout on ``PYTHONPATH``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PACKAGE = "n8n_plugin"
_ROOT = Path(__file__).resolve().parents[1]

# ``python -m pytest`` from the plugin dir puts it on sys.path, where its modules could shadow core's.
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


_load_plugin_package()


@pytest.fixture
def plugin_root() -> Path:
    return _ROOT


@pytest.fixture(autouse=True)
def _hermes_home(tmp_path, monkeypatch):
    """Every test gets its own profile home: config.yaml lands there, never in a real one."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


class _RootIsNotATestPackage:
    """The plugin dir *is* a package (``__init__.py`` with relative imports), so pytest would collect it
    as a test ``Package`` and import that ``__init__.py`` standalone, where the relative imports cannot
    resolve. Collect it as a plain directory instead (registered globally: the root's collector is
    created through its parent's hook proxy, which never sees a conftest inside the root)."""

    @pytest.hookimpl(tryfirst=True)
    def pytest_collect_directory(self, path, parent):
        if Path(path).resolve() == _ROOT:
            return pytest.Dir.from_parent(parent, path=path)
        return None


def pytest_configure(config):
    if not config.pluginmanager.has_plugin("n8n-root"):
        config.pluginmanager.register(_RootIsNotATestPackage(), "n8n-root")
