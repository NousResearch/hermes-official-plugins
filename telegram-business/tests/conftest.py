"""Test bootstrap: import the plugin as a package + stub python-telegram-bot.

The plugin directory has a hyphen in its name, so tests load it under the
alias ``telegram_business_plugin`` via importlib. python-telegram-bot is
stubbed when absent so the manager's lazy keyboard import works.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parents[1]


def _ensure_telegram_stub() -> None:
    try:
        __import__("telegram")
        __import__("telegram.ext")
    except ImportError:
        pass
    else:
        return
    telegram = types.ModuleType("telegram")
    telegram.InlineKeyboardButton = MagicMock(name="InlineKeyboardButton")
    telegram.InlineKeyboardMarkup = MagicMock(name="InlineKeyboardMarkup")
    ext = types.ModuleType("telegram.ext")

    class ApplicationHandlerStop(Exception):
        pass

    ext.ApplicationHandlerStop = ApplicationHandlerStop
    ext.BusinessConnectionHandler = MagicMock(name="BusinessConnectionHandler")
    ext.CallbackQueryHandler = MagicMock(name="CallbackQueryHandler")
    ext.CommandHandler = MagicMock(name="CommandHandler")
    ext.MessageHandler = MagicMock(name="MessageHandler")
    ext.filters = MagicMock(name="filters")
    telegram.ext = ext
    sys.modules["telegram"] = telegram
    sys.modules["telegram.ext"] = ext


def _load_plugin_package() -> None:
    if "telegram_business_plugin" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "telegram_business_plugin",
        _ROOT / "__init__.py",
        submodule_search_locations=[str(_ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["telegram_business_plugin"] = module
    spec.loader.exec_module(module)


_ensure_telegram_stub()
_load_plugin_package()
