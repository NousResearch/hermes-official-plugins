"""Advanced Delegation: two extra ``delegation.backend`` modes for ``delegate_task``.

* ``model_map`` (``model_map.py``): the agent picks a named model route for each subagent.
* ``profile_routes`` (``profile_routes.py``): the agent runs each subagent as one of your Hermes profiles.

Plus ``hermes delegation config`` (``cli.py``) to pick the mode and edit its routes, and the
``/delegation`` slash command (``slash.py``) to switch modes in a chat.

The bundled ``default`` backend stays in Hermes core. This module only registers; on a Hermes core
that predates pluggable delegation providers it logs one warning and registers nothing, so
installing it there changes nothing.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    try:
        from .model_map import ModelMapDelegationProvider
        from .profile_routes import ProfilesDelegationProvider
    except ModuleNotFoundError as exc:
        if exc.name != "agent.delegation_provider":
            raise
        logger.warning(
            "advanced-delegation needs a Hermes core with pluggable delegation providers "
            "(agent.delegation_provider); update Hermes. The plugin is inactive until then.")
        return
    ctx.register_delegation_provider(ModelMapDelegationProvider())
    ctx.register_delegation_provider(ProfilesDelegationProvider())
    from . import cli, slash
    ctx.register_command("delegation", slash.make_handler(ctx), description="Show or switch the delegation mode",
                         args_hint="[default|model_map|profile_routes] [--global]")
    ctx.register_cli_command(
        name="delegation", help="Delegation modes: default, model routes, profile routes",
        setup_fn=cli.setup_parser, handler_fn=cli.handle,
        description="Pick how delegate_task routes subagents and edit the routes: "
                    "`hermes delegation config` (or `--show` to print the current setup).")
