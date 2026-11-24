"""Loads the shipped push plugin's modules for the tests, with Hermes' modules stubbed.

Each call imports a fresh copy, so a test's patched attributes and module state
(watches, sessions, the delivery queue) never leak into the next test.
"""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import patch

PUSH = Path(__file__).resolve().parents[1]
PACKAGE = "hermes_mobile_push_under_test"


def load_module(relative, modules=None):
    """Import one of the plugin's files as a module of a fresh copy of the plugin package, as
    Hermes' loader does, so its relative imports load fresh copies of their modules too."""
    with patch.dict(sys.modules, modules or {}):
        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(PUSH)]
        sys.modules[PACKAGE] = package
        spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{Path(relative).stem}", PUSH / relative)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


def load_push_adapter(home):
    """Import the whole shipped adapter with Hermes' gateway modules stubbed.

    Returns the adapter and a ``hermes_constants`` stub naming *home*, for the
    tests to put in ``sys.modules`` while they call into it.
    """
    base = types.ModuleType("gateway.platforms.base")
    base.BasePlatformAdapter = type("BasePlatformAdapter", (), {"__init__": lambda self, *args, **kwargs: None})
    base.SendResult = type("SendResult", (), {"__init__": lambda self, *args, **kwargs: None})
    config = types.ModuleType("gateway.config")
    config.Platform = lambda value: value
    config.PlatformConfig = type("PlatformConfig", (), {})
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: home
    adapter = load_module("adapter.py", {
        "gateway": types.ModuleType("gateway"), "gateway.platforms": types.ModuleType("gateway.platforms"),
        "gateway.platforms.base": base, "gateway.config": config,
    })
    return adapter, constants


def load_privacy(modules=None):
    """Import the shipped privacy.py; *modules* patches ``sys.modules`` while it imports."""
    return load_module("privacy.py", modules)


def load_activity():
    return load_module("activity.py")


def finish_turn(adapter, session_id, body, platform="", **end):
    """A turn's end as Hermes' turn finalizer reports it: ``post_llm_call`` with the final reply
    (none when *body* is ``None``), then ``on_session_end``; *end* overrides its outcome."""
    if body is not None:
        adapter._on_turn_reply(session_id=session_id, turn_id="turn-1", assistant_response=body, platform=platform)
    adapter._on_session_end(**{"session_id": session_id, "turn_id": "turn-1", "completed": True, "failed": False,
                               "interrupted": False, "platform": platform, **end})


def registered_hooks(adapter, constants, valid_hooks=None):
    """The hook names the adapter's ``register(ctx)`` registers, in order."""
    hooks = []
    context = types.SimpleNamespace(
        register_platform=lambda **kwargs: None, on_unload=lambda callback: None,
        register_hook=lambda name, callback: hooks.append(name))
    modules = {"hermes_constants": constants}
    if valid_hooks is not None:
        registry = types.ModuleType("hermes_cli.plugins")
        registry.VALID_HOOKS = set(valid_hooks)
        modules.update({"hermes_cli": types.ModuleType("hermes_cli"), "hermes_cli.plugins": registry})
    with patch.dict(sys.modules, modules):
        adapter.register(context)
    return hooks
