"""Delegation Model Map: the agent picks a named model option for each ``delegate_task`` subagent.

A delegation provider (``delegation.backend: model_map``, settings in
``delegation.backends.model_map``). The provider itself lives in
``provider.py``; this module only registers it, and stays inert on a Hermes core that predates
pluggable delegation providers, so installing it there changes nothing.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    try:
        from .provider import ModelMapDelegationProvider
    except ModuleNotFoundError as exc:
        if exc.name != "agent.delegation_provider":
            raise
        logger.warning(
            "delegation-model-map needs a Hermes core with pluggable delegation providers "
            "(agent.delegation_provider); update Hermes. The plugin is inactive until then.")
        return
    ctx.register_delegation_provider(ModelMapDelegationProvider())
