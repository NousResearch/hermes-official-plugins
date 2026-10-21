"""On a Hermes core without pluggable delegation providers the plugin loads and does nothing.

Runs on every core: against one that predates ``agent.delegation_provider`` this is the real
situation; against a newer one the module is hidden for the duration of the test.
"""

from __future__ import annotations

import logging
import sys

import delegation_model_map_plugin


class OldCoreContext:
    """A PluginContext from before delegation providers: no ``register_delegation_provider``."""


def test_register_is_a_logged_no_op_without_the_core_seam(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "agent.delegation_provider", None)
    monkeypatch.delitem(sys.modules, "delegation_model_map_plugin.provider", raising=False)
    with caplog.at_level(logging.WARNING):
        delegation_model_map_plugin.register(OldCoreContext())
    assert "needs a Hermes core with pluggable delegation providers" in caplog.text
