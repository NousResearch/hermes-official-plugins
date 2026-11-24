"""Test bootstrap: make the plugin directory importable as the package ``teams_pipeline_plugin``.

The Hermes plugin loader imports a directory plugin as a package (``hermes_plugins.<slug>``) so the
relative imports resolve; tests use a stable alias instead (the directory name has a hyphen). The
alias is served by a meta-path finder, so a test that drops it from ``sys.modules`` re-imports cleanly. Hermes core (``agent``, ``gateway``, ``hermes_cli``) must be importable — run with a
hermes-agent checkout on ``PYTHONPATH``.
"""

from __future__ import annotations

import importlib.abc
import importlib.util
import sys
from pathlib import Path

import pytest

PACKAGE = "teams_pipeline_plugin"
_ROOT = Path(__file__).resolve().parents[1]

# ``python -m pytest`` from the repo root puts the root on sys.path, where this plugin's modules could
# shadow core packages when core is imported.
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _ROOT]


class _PluginFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != PACKAGE:
            return None
        return importlib.util.spec_from_file_location(
            PACKAGE, _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)])


if not any(isinstance(f, _PluginFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _PluginFinder())


@pytest.fixture(autouse=True)
def _hermes_home(tmp_path, monkeypatch):
    """Every test gets its own profile home, so nothing reads the developer's real config or .env."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    (tmp_path / "hermes").mkdir()


class _RepoRootIsNotATestPackage:
    """The plugin root has an ``__init__.py``, so pytest would collect it as a test ``Package`` and
    import it standalone during setup. Collect the root as a plain directory instead.

    Registered as a global plugin (not a conftest hook) because the root directory's collector is
    created through its *parent's* hook proxy, which never sees a conftest inside the root."""

    @pytest.hookimpl(tryfirst=True)
    def pytest_collect_directory(self, path, parent):
        if Path(path).resolve() == _ROOT:
            return pytest.Dir.from_parent(parent, path=path)
        return None


def pytest_configure(config):
    if not config.pluginmanager.has_plugin("teams-pipeline-repo-root"):
        config.pluginmanager.register(_RepoRootIsNotATestPackage(), "teams-pipeline-repo-root")
