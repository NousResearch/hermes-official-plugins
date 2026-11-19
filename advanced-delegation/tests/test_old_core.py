"""On a Hermes core without pluggable delegation providers the plugin loads and does nothing.

Runs on every core: against one that predates ``agent.delegation_provider`` this is the real
situation; against a newer one the module is hidden for the duration of the test.
"""

from __future__ import annotations

import logging
import sys

import advanced_delegation_plugin


class OldCoreContext:
    """A PluginContext from before delegation providers: no ``register_delegation_provider``."""


def test_register_is_a_logged_no_op_without_the_core_seam(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "agent.delegation_provider", None)
    monkeypatch.delitem(sys.modules, "advanced_delegation_plugin.model_map", raising=False)
    monkeypatch.delitem(sys.modules, "advanced_delegation_plugin.profile_routes", raising=False)
    with caplog.at_level(logging.WARNING):
        advanced_delegation_plugin.register(OldCoreContext())
    assert "needs a Hermes core with pluggable delegation providers" in caplog.text
