"""Advanced Delegation: two extra ``delegation.backend`` modes for ``delegate_task``.

* ``model_map`` (``model_map.py``): the agent picks a named model route for each subagent.
* ``profile_routes`` (``profile_routes.py``): the agent runs each subagent as one of your Hermes profiles.

Plus ``hermes delegation config`` (``cli.py``) to pick the mode and edit its routes, the
``/delegation`` slash command (``slash.py``) to switch modes in a chat, and the ``model-routing``
skill (``skills/model-routing``, loaded as ``advanced-delegation:model-routing``), which guides the
agent in picking a model route per task.

The bundled ``default`` backend stays in Hermes core. This module only registers. ``plugin.yaml``
declares ``requires_hermes: ">=0.22.5"``, which Hermes 0.22.0+ enforces before import; older cores
ignore that field, so on a core without pluggable delegation providers ``register`` logs one warning
and registers nothing (the skill included: it only makes sense with model routes available).
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SKILL = "model-routing"
# Shown in skill listings; the 60-character limit applies (same text as the SKILL.md frontmatter).
SKILL_DESCRIPTION = "Route subagent tasks to the cheapest model that passes."
SKILL_MD = Path(__file__).resolve().parent / "skills" / SKILL / "SKILL.md"


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
    ctx.register_command("delegation", slash.make_handler(ctx),
                         description="Show or switch the delegation mode (next session; --now for this chat)",
                         args_hint="[default|model_map|profile_routes] [--now]")
    ctx.register_cli_command(
        name="delegation", help="Delegation modes: default, model routes, profile routes",
        setup_fn=cli.setup_parser, handler_fn=cli.handle,
        description="Pick how delegate_task routes subagents and edit the routes: "
                    "`hermes delegation config` (or `--show` to print the current setup).")
    # Last, and guarded: a skill that fails to register must never take the delegation modes down.
    try:
        ctx.register_skill(name=SKILL, path=SKILL_MD, description=SKILL_DESCRIPTION)
    except Exception as exc:
        logger.warning("advanced-delegation: could not register the %s skill (%s); the delegation modes "
                       "still work.", SKILL, exc)
