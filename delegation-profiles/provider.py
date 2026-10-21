"""The ``profiles`` delegation provider.

Config (the ``delegation`` section of config.yaml)::

    delegation:
      backend: profiles
      profiles:                       # allowlist AND what the model reads about each profile
        - name: coder
          description: Writes and refactors code; runs tests.
        - name: researcher
          description: Web research and long-form summaries.
      profiles_default: coder         # optional: profile for tasks that set none

``delegate_task`` gains a per-task ``profile`` enum of those names. A task that sets one runs as that
Hermes profile (core binds the profile's own config, model, credentials, skills and SOUL.md); a task
without one runs as ``profiles_default`` when set, else on today's ``delegation.*`` route.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from agent.delegation_provider import DelegationProvider

logger = logging.getLogger(__name__)

PARAMETER = "profile"
_warned: set[str] = set()


def _warn_once(message: str, *args: Any) -> None:
    # task_parameters/route_task run on every session build and every task; say each problem once.
    text = message % args
    if text not in _warned:
        _warned.add(text)
        logger.warning(text)


def configured_profiles(config: Mapping[str, Any]) -> list[tuple[str, str]]:
    """``[(name, description), ...]`` from ``delegation.profiles`` in config order, skipping entries
    without a name and repeated names (each skipped entry is warned once)."""
    raw = config.get("profiles") if isinstance(config, Mapping) else None
    if raw is None:
        return []
    if not isinstance(raw, list):
        _warn_once("delegation.profiles must be a list of {name, description} entries; ignoring it.")
        return []
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        name = entry.get("name") if isinstance(entry, Mapping) else None
        if not isinstance(name, str) or not name.strip():
            _warn_once("delegation.profiles[%d] has no name; skipped.", i)
            continue
        name = name.strip()
        if name in seen:
            _warn_once("delegation.profiles[%d] repeats profile %r; skipped.", i, name)
            continue
        seen.add(name)
        description = entry.get("description")
        entries.append((name, " ".join(description.split()) if isinstance(description, str) else ""))
    return entries


class ProfilesDelegationProvider(DelegationProvider):
    """``delegation.backend: profiles``: the model picks a listed Hermes profile per subagent."""

    @property
    def name(self) -> str:
        return "profiles"

    def task_parameters(self, config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        profiles = configured_profiles(config)
        if not profiles:
            return {}
        lines = [
            "Run this subagent as one of these Hermes profiles: it then uses that profile's own model, "
            "skills, persona and credentials. Omit for the default subagent route.",
            *(f"{name}: {description}" if description else name for name, description in profiles),
        ]
        return {PARAMETER: {"type": "string", "enum": [name for name, _ in profiles],
                            "description": "\n".join(lines)}}

    def description(self, config: Mapping[str, Any]) -> str:
        if not configured_profiles(config):
            return ""
        return "Each task may set `profile` to run that subagent as one of the listed Hermes profiles."

    def route_task(self, task: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
        chosen = task.get(PARAMETER)
        if chosen:
            # Core already refused anything outside the enum before calling us.
            return {"profile": chosen, "label": chosen}
        default = config.get("profiles_default")
        if isinstance(default, str) and default.strip():
            default = default.strip()
            if default in {name for name, _ in configured_profiles(config)}:
                return {"profile": default, "label": default}
            _warn_once("delegation.profiles_default %r is not in delegation.profiles; "
                       "tasks without a profile use the default route.", default)
        return dict(config)
