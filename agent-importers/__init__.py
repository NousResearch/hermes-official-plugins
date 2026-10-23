"""agent-importers — ``hermes import-agent`` (and ``hermes claw``) for Hermes Agent.

Imports another agent's setup (instructions, permission rules, MCP servers, skills, memories) into
the active Hermes profile, preview first. Formerly built into hermes-agent core
(``hermes_cli/agent_import.py`` and, for OpenClaw, ``hermes_cli/claw.py`` plus the
``openclaw-migration`` optional skill); the command spellings, their flags and the per-profile sync
manifest (``HERMES_HOME/import-sync.json``) are unchanged.

Wiring goes only through the public plugin API (``ctx.register_cli_command`` /
``ctx.register_skill``).
"""

from __future__ import annotations

from pathlib import Path

from .cli import (CLAW_DESCRIPTION, CLAW_HELP, DESCRIPTION, HELP, handle_claw_cli, handle_cli,
                  setup_claw_cli, setup_cli)

_SKILL_MD = Path(__file__).resolve().parent / "skills" / "openclaw-migration" / "SKILL.md"


def _core_module_exists(name: str) -> bool:
    """Looked up next to the running core's own modules, so an editable install of another checkout
    cannot answer for it."""
    from hermes_cli import config
    return (Path(config.__file__).parent / name).is_file()


def _core_ships_import_agent() -> bool:
    """A core from before the move still registers its own ``hermes import-agent``; adding a second
    subparser of that name would abort the CLI's plugin-command wiring, so this copy stays inert
    there."""
    return _core_module_exists("agent_import.py")


def _core_ships_claw() -> bool:
    """Same guard for ``hermes claw``: a core that still has ``hermes_cli/claw.py`` registers its own
    ``claw`` subparser and ships its own copy of the ``openclaw-migration`` skill."""
    return _core_module_exists("claw.py")


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin loader."""
    if not _core_ships_import_agent():
        ctx.register_cli_command(name="import-agent", help=HELP, description=DESCRIPTION,
                                 setup_fn=setup_cli, handler_fn=handle_cli)
    if not _core_ships_claw():
        ctx.register_cli_command(name="claw", help=CLAW_HELP, description=CLAW_DESCRIPTION,
                                 setup_fn=setup_claw_cli, handler_fn=handle_claw_cli)
        ctx.register_skill(
            name="openclaw-migration", path=_SKILL_MD,
            description="Guided OpenClaw → Hermes migration (memories, skills, settings; "
                        "secrets only on request).")


__all__ = ["register"]
