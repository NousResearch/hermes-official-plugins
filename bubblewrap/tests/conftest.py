"""Test bootstrap: import the plugin directory as the package ``bubblewrap_plugin``.

The Hermes plugin loader imports a directory plugin as a package (``hermes_plugins.<slug>``) so the
relative imports in ``__init__.py`` resolve; tests mirror that under a stable alias. Hermes core
(``agent``, ``tools``, ...) must be importable: run with a hermes-agent checkout on ``PYTHONPATH``.

bwrap is Linux-only and the plugin declares ``platforms: [linux]``, so off Linux only the provider
tests (which load no sandbox code) are collected.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

PACKAGE = "bubblewrap_plugin"
_ROOT = Path(__file__).resolve().parents[1]

sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _ROOT]

if not sys.platform.startswith("linux"):
    collect_ignore_glob = ["test_bubblewrap_*.py"]


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


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a per-test directory, as core's test suite does, so no test reads or
    writes the real ~/.hermes (the sandbox makes its scratch and state dirs under it)."""
    home = tmp_path / "hermes_test"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "_PINNED_PROCESS_HERMES_HOME", None, raising=False)


@pytest.fixture
def bwrap_provider():
    """The plugin's provider in the global terminal backend registry, as ``register(ctx)`` leaves it.

    Yields ``(provider, settings)``: *settings* stands in for ``plugins.entries.bubblewrap.settings``
    and is read on every environment construction, like ``ctx.get_config``.
    """
    import agent.terminal_env_registry as registry

    plugin = sys.modules[PACKAGE]
    settings: dict = {}
    provider = plugin.BubblewrapProvider(get_config=lambda key, default=None: settings.get(key, default),
                                         set_config=settings.__setitem__)
    previous = registry.snapshot_registration(plugin.BACKEND_NAME)
    registry.register_provider(provider)
    try:
        yield provider, settings
    finally:
        registry.restore_registration(plugin.BACKEND_NAME, provider, previous)


class _RepoRootIsNotATestPackage:
    """The plugin root has an ``__init__.py`` with relative imports; collect it as a plain directory
    so pytest never imports that file standalone. Registered as a global plugin because the root's
    collector is created through its parent's hook proxy, which never sees a conftest inside it."""

    @pytest.hookimpl(tryfirst=True)
    def pytest_collect_directory(self, path, parent):
        if Path(path).resolve() == _ROOT:
            return pytest.Dir.from_parent(parent, path=path)
        return None


def pytest_configure(config):
    # Every sandbox mounts a fresh /tmp, so a test directory under /tmp does not exist inside it and the
    # integration tests that cd or write there fail. Without an explicit --basetemp, put pytest's
    # directories under /var/tmp instead, removed when the session ends.
    if sys.platform.startswith("linux") and config.option.basetemp is None:
        temp = os.path.realpath(tempfile.gettempdir())
        if (temp == "/tmp" or temp.startswith("/tmp/")) and os.path.isdir("/var/tmp"):
            config.option.basetemp = config._plugin_basetemp = tempfile.mkdtemp(prefix="pytest-plugin-", dir="/var/tmp")
    if not config.pluginmanager.has_plugin("bubblewrap-repo-root"):
        config.pluginmanager.register(_RepoRootIsNotATestPackage(), "bubblewrap-repo-root")


def pytest_unconfigure(config):
    basetemp = getattr(config, "_plugin_basetemp", None)
    if basetemp:
        shutil.rmtree(basetemp, ignore_errors=True)
