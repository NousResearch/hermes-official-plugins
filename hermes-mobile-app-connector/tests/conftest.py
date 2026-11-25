"""Shared pytest bootstrap for the hermes-mobile-app-connector plugin suite."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = TESTS_DIR.parent

# Importing a Hermes entry point (gateway.run, cli, ...) points TMPDIR/TMP/TEMP at
# <HERMES_HOME>/cache/scratch unless a temp var is already set. Tests that import one under a
# temporary Hermes home would leave tempfile aimed at a directory deleted with that home. Drop a
# value Hermes itself exported (HERMES_SCRATCH_DIR marks it), then pin the system default so the
# import-time redirect stays a no-op for the whole run.
_HERMES_EXPORTED_TMP = os.environ.pop("HERMES_SCRATCH_DIR", "")
for _key in ("TMPDIR", "TMP", "TEMP"):
    if _HERMES_EXPORTED_TMP and os.environ.get(_key, "").strip() == _HERMES_EXPORTED_TMP:
        del os.environ[_key]
tempfile.tempdir = None
os.environ.setdefault("TMPDIR", tempfile.gettempdir())

# Ensure sibling test helpers (push_plugin, etc.) are importable under --import-mode=importlib
# without putting the plugin root itself on sys.path (which could shadow packages).
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != PLUGIN_ROOT]
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))


class _PluginRootIsNotATestPackage:
    """Prevent pytest from importing hermes-mobile-app-connector/__init__.py as a test package."""

    @pytest.hookimpl(tryfirst=True)
    def pytest_collect_directory(self, path, parent):
        if Path(path).resolve() == PLUGIN_ROOT:
            return pytest.Dir.from_parent(parent, path=path)
        return None


def pytest_configure(config):
    if not config.pluginmanager.has_plugin("hermes-mobile-app-connector-root"):
        config.pluginmanager.register(_PluginRootIsNotATestPackage(), "hermes-mobile-app-connector-root")
