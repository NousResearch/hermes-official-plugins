"""Shared pytest bootstrap for the hermes-mobile plugin suite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = TESTS_DIR.parent

# Ensure sibling test helpers (push_plugin, etc.) are importable under --import-mode=importlib
# without putting the plugin root itself on sys.path (which could shadow packages).
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != PLUGIN_ROOT]
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))


class _PluginRootIsNotATestPackage:
    """Prevent pytest from importing hermes-mobile/__init__.py as a test package."""

    @pytest.hookimpl(tryfirst=True)
    def pytest_collect_directory(self, path, parent):
        if Path(path).resolve() == PLUGIN_ROOT:
            return pytest.Dir.from_parent(parent, path=path)
        return None


def pytest_configure(config):
    if not config.pluginmanager.has_plugin("hermes-mobile-root"):
        config.pluginmanager.register(_PluginRootIsNotATestPackage(), "hermes-mobile-root")
