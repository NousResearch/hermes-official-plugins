"""Test bootstrap: make the plugin directory importable as the package ``advanced_delegation_plugin``.

The Hermes plugin loader imports a directory plugin as a package (``hermes_plugins.<slug>``) so the
relative import in ``__init__.py`` resolves; tests mirror that under a stable alias. Hermes core
(``agent``, ``hermes_cli``, ``tools``) must be importable: run with a hermes-agent checkout on
``PYTHONPATH``. Every test module skips itself when that core has no ``agent.delegation_provider``
(a core older than pluggable delegation providers).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PACKAGE = "advanced_delegation_plugin"
_ROOT = Path(__file__).resolve().parents[1]

# ``python -m pytest`` from the plugin root puts the root on sys.path, where the plugin's modules could
# shadow core modules when core is imported.
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
    """Every test gets its own HOME and profile home, so nothing reads the developer's real config."""
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "os-home" / ".hermes"))
    (tmp_path / "os-home" / ".hermes").mkdir(parents=True)


class _RepoRootIsNotATestPackage:
    """The plugin root has an ``__init__.py`` with a relative import, so pytest would collect it as a
    test ``Package`` and import it standalone during setup. Collect the root as a plain directory.

    Registered as a global plugin (not a conftest hook) because the root directory's collector is
    created through its *parent's* hook proxy, which never sees a conftest inside the root."""

    @pytest.hookimpl(tryfirst=True)
    def pytest_collect_directory(self, path, parent):
        if Path(path).resolve() == _ROOT:
            return pytest.Dir.from_parent(parent, path=path)
        return None


def pytest_configure(config):
    if not config.pluginmanager.has_plugin("advanced-delegation-repo-root"):
        config.pluginmanager.register(_RepoRootIsNotATestPackage(), "advanced-delegation-repo-root")
