"""Regression: profile methods must survive the plugin package being evicted from sys.modules.

Hermes core's plugin re-discovery removes the plugin package from sys.modules
(`providers/__init__.py::_exec_plugin_module`, `hermes_cli/plugins_loader.py`). The
registered profile object survives and keeps serving calls; a lazy relative import inside
one of its methods then fails, and the bare-name fallback masks the real error as
"No module named 'directsdk'" (upstream issue #381).

These tests watch `builtins.__import__` while calling each profile method after eviction:
a method that imports `directsdk` / `directsdk_setup` at call time is the bug, regardless
of whether that import happens to succeed (conftest puts the plugin dir on sys.path, which
would let the bare-name fallback leak through as a false pass).
"""
import importlib.util
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PKG_NAME = "claude-subscription-directsdk"


def _load_profile():
    spec = importlib.util.spec_from_file_location(
        PKG_NAME, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[PKG_NAME] = module
    spec.loader.exec_module(module)
    return module


def _evict_package():
    for key in [k for k in sys.modules if k == PKG_NAME or k.startswith(PKG_NAME + ".")]:
        del sys.modules[key]


def _builtins_import():
    import builtins

    return builtins.__import__


@pytest.fixture
def evicted_profile(monkeypatch):
    module = _load_profile()
    profile = module.profile
    _evict_package()
    # Loading the module registers the provider with Hermes core; core's account-usage and
    # discovery hooks re-discover installed plugins on each call, which would re-import the
    # INSTALLED plugin (not this checkout) mid-test and pollute the import spy. Stub the
    # re-discovery out — the eviction is what we are testing, not core's registry.
    monkeypatch.setattr("providers._import_plugin_dir", lambda *a, **k: None, raising=False)
    return profile


def _assert_no_plugin_import(callable_, label):
    imported = []
    real_import = _builtins_import()

    def spy(name, *args, **kwargs):
        imported.append(name)
        return real_import(name, *args, **kwargs)

    import builtins

    original = builtins.__import__
    builtins.__import__ = spy
    try:
        callable_()
    except ImportError as exc:
        pytest.fail(f"{label} imported at call time and it failed: {exc}")
    except Exception:
        pass  # environment failures (no CLI, no login) are out of scope
    finally:
        builtins.__import__ = original
    # Match directsdk / directsdk_setup / the plugin package itself, but not unrelated
    # substrings (e.g. a future module merely mentioning the name in a docstring import).
    leaked = [n for n in imported
              if n.split(".")[0] in {"directsdk", "directsdk_setup", PKG_NAME}]
    assert not leaked, f"{label} imported at call time: {leaked}"


def test_create_client_does_not_import_at_call_time(evicted_profile):
    _assert_no_plugin_import(evicted_profile.create_client, "create_client")


def test_setup_status_does_not_import_at_call_time(evicted_profile):
    _assert_no_plugin_import(evicted_profile.setup_status, "setup_status")


def test_discover_models_does_not_import_at_call_time(evicted_profile):
    _assert_no_plugin_import(evicted_profile.discover_models, "discover_models")


def test_fetch_account_usage_does_not_import_at_call_time(evicted_profile):
    _assert_no_plugin_import(evicted_profile.fetch_account_usage, "fetch_account_usage")
