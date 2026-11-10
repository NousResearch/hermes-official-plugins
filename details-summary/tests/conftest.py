"""Load the plugin directory as the package ``details_summary_plugin``, the way Hermes's loader
imports a directory plugin, so the relative imports in ``__init__.py`` resolve."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PACKAGE = "details_summary_plugin"
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


_load_plugin_package()


class _RepoRootIsNotATestPackage:
    """The repo root is the plugin package; collect it as a plain directory so pytest does not
    import its ``__init__.py`` standalone, where the relative imports cannot resolve."""

    @pytest.hookimpl(tryfirst=True)
    def pytest_collect_directory(self, path, parent):
        if Path(path).resolve() == _ROOT:
            return pytest.Dir.from_parent(parent, path=path)
        return None


def pytest_configure(config):
    if not config.pluginmanager.has_plugin("details-summary-repo-root"):
        config.pluginmanager.register(_RepoRootIsNotATestPackage(), "details-summary-repo-root")
