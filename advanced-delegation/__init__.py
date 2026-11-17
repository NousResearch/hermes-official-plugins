"""Advanced Delegation: two extra ``delegation.backend`` modes for ``delegate_task``.

* ``model_map`` (``model_map.py``): the agent picks a named model route for each subagent.
* ``profile_routes`` (``profile_routes.py``): the agent runs each subagent as one of your Hermes profiles.

Plus ``hermes delegation config`` (``cli.py``) to pick the mode and edit its routes, and the
``/delegation`` slash command (``slash.py``) to switch modes in a chat.

The bundled ``default`` backend stays in Hermes core. This module only registers. ``plugin.yaml``
declares ``requires_hermes: ">=0.22.5"``, which Hermes 0.22.0+ enforces before import; older cores
ignore that field, so on a core without pluggable delegation providers ``register`` logs one warning
and registers nothing.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _on_pre_tool_call(tool_name: str = "", args: dict | None = None, **kwargs):
    """Batch-route unassigned ``delegate_task`` items through the System-1 Decider."""
    if tool_name != "delegate_task" or not isinstance(args, dict):
        return None
    action = str(args.get("action") or "spawn").strip().lower()
    if action != "spawn":
        return None
    tasks = args.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return None
    try:
        from . import store
        from .decider_router import decider_enabled, route_unassigned_tasks
        section = store.delegation_section()
        if store.current_mode(section) != "model_map":
            return None
        backends = section.get("backends")
        mm_settings = backends.get("model_map") if isinstance(backends, dict) else None
        if not isinstance(mm_settings, dict) or not decider_enabled(mm_settings):
            return None
        assigned = route_unassigned_tasks(tasks, mm_settings)
        if assigned:
            return {"action": "modify", "args": {"tasks": tasks}}
    except Exception as exc:
        logger.debug("advanced-delegation pre_tool_call decider hook skipped: %s", exc)
    return None


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
    try:
        ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    except Exception:
        pass
    from . import cli, slash
    ctx.register_command("delegation", slash.make_handler(ctx),
                         description="Show or switch the delegation mode (next session; --now for this chat)",
                         args_hint="[default|model_map|profile_routes|decider] [--now]")
    ctx.register_cli_command(
        name="delegation", help="Delegation modes: default, model routes, profile routes, and System-1 Decider",
        setup_fn=cli.setup_parser, handler_fn=cli.handle,
        description="Pick how delegate_task routes subagents, edit routes, or configure the System-1 Decider: "
                    "`hermes delegation config` / `hermes delegation decider` (or `--show`).")
