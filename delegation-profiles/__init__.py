"""Delegation Profiles: run each ``delegate_task`` subagent as one of your Hermes profiles.

A delegation provider (``delegation.backend: profile_routes``, settings in
``delegation.backends.profile_routes``). The provider itself lives in
``provider.py``; this module only registers it, and stays inert on a Hermes core that predates
pluggable delegation providers, so installing it there changes nothing.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    try:
        from .provider import ProfilesDelegationProvider
    except ModuleNotFoundError as exc:
        if exc.name != "agent.delegation_provider":
            raise
        logger.warning(
            "delegation-profiles needs a Hermes core with pluggable delegation providers "
            "(agent.delegation_provider); update Hermes. The plugin is inactive until then.")
        return
    ctx.register_delegation_provider(ProfilesDelegationProvider())
