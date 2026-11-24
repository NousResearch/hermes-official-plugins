"""Teams meeting pipeline plugin.

Registers the operator CLI (invoked by the agent via the terminal tool; no model tools) and a
``msgraph_webhook`` platform handler that binds the pipeline runtime to the Graph webhook adapter.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import runtime
from .cli import register_cli, teams_pipeline_command

logger = logging.getLogger(__name__)


def _core_ships_teams_pipeline() -> bool:
    """True on a Hermes core that still bundles this plugin as ``plugins/teams_pipeline``.

    That copy registers the same ``hermes teams-pipeline`` command and core wires its runtime into the
    webhook adapter itself; registering here too would collide with it. Looks only inside the imported
    ``plugins`` package's own ``__path__``."""
    try:
        import plugins as core_plugins
    except ImportError:
        return False
    return any((Path(p) / "teams_pipeline" / "plugin.yaml").is_file()
               for p in getattr(core_plugins, "__path__", None) or ())


def _bind_msgraph(native, adapter) -> None:
    runtime.bind_adapter(adapter)


def register(ctx) -> None:
    if _core_ships_teams_pipeline():
        logger.info("teams-pipeline: this Hermes core still ships the Teams pipeline built in; "
                    "the plugin stays inactive until core drops it")
        return
    ctx.register_cli_command(
        name="teams-pipeline",
        help="Inspect and operate the Microsoft Teams meeting pipeline",
        setup_fn=register_cli,
        handler_fn=teams_pipeline_command,
        description=(
            "Operator CLI for the Microsoft Teams meeting pipeline. "
            "Lists jobs, inspects stored runs, replays jobs, validates Graph "
            "setup, and maintains Graph subscriptions."
        ),
    )
    ctx.register_platform_handler("msgraph_webhook", _bind_msgraph)
