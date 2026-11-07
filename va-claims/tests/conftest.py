"""Test bootstrap: import the plugin directory as the package ``va_claims_plugin``.

The Hermes plugin loader imports a directory plugin as a package
(``hermes_plugins.<slug>``) so relative imports in ``__init__.py`` resolve;
tests mirror that under a stable alias. Hermes core (``hermes_cli``) must be
importable — run with a hermes-agent checkout on ``PYTHONPATH``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PACKAGE = "va_claims_plugin"
_ROOT = Path(__file__).resolve().parents[1]

# ``python -m pytest`` from the repo root puts the root on sys.path, where this
# plugin's modules could shadow core packages when core is imported.
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


class _PluginRootIsNotATestPackage:
    """The plugin root *is* the plugin package (it has an ``__init__.py`` with
    relative imports), so pytest would collect it as a test ``Package`` and
    import that ``__init__.py`` standalone during setup, where the relative
    imports cannot resolve. Collect the root as a plain directory instead.

    Registered as a global plugin (not a conftest hook) because the root
    directory's collector is created through its *parent's* hook proxy, which
    never sees a conftest inside the root."""

    @pytest.hookimpl(tryfirst=True)
    def pytest_collect_directory(self, path, parent):
        if Path(path).resolve() == _ROOT:
            return pytest.Dir.from_parent(parent, path=path)
        return None


def pytest_configure(config):
    if not config.pluginmanager.has_plugin("va-claims-root"):
        config.pluginmanager.register(_PluginRootIsNotATestPackage(), "va-claims-root")
