"""``/delegation``: show the delegation mode and switch it (CLI, TUI, Desktop, messaging).

``/delegation``                 the current mode and a picker to switch this session (a clarify card
                                with buttons where the surface has them, a numbered list elsewhere);
                                plain text when nothing can ask (``ctx.ask_choice`` absent or None)
``/delegation <mode>``          switch this session (``default``, ``model_map``/``models``,
                                ``profile_routes``/``profiles``, or the number shown)
``/delegation <mode> --global`` save it as this profile's ``delegation.backend``

A session switch changes routing at once, but not the agent's per-task choices: the ``delegate_task``
schema is part of the prompt cache and is never rebuilt mid-session, so ``option`` / ``profile`` follow
the mode the chat started with until ``/new``. Core refuses a task that sends a choice the active mode
does not declare (it never drops one silently). ``--global`` saves the mode, and ``/new`` then starts a
session whose choices match; ``/new`` also clears a session-only switch.

Session scoping follows ``/model``. Core keeps the per-session choice and the global write behind
``ctx.delegation_backends`` (``list_backends`` / ``get_session_backend`` / ``set_session_backend`` /
``set_global_backend``). It is a host remote facade, so under ``plugins.isolation: host`` the switch
lands in the Hermes process ``delegate_task`` runs in, not in the plugin host's copy. Feature-detected:
on a core without it a plain switch says to use ``--global``, which then writes ``delegation.backend``
through ``store``. When the configured backend fails, core's error names this command, so the listing
flags a backend that cannot run and switching away from it is the fix.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Optional

from . import store

_ALIASES = {
    "default": "default", "1": "default", "off": "default", "builtin": "default",
    "model_map": "model_map", "2": "model_map", "models": "model_map", "model_routes": "model_map",
    "routes": "model_map", "model": "model_map",
    "profile_routes": "profile_routes", "3": "profile_routes", "profiles": "profile_routes",
    "profile": "profile_routes",
}
USAGE = "/delegation [default | model_map | profile_routes] [--global]"
AskChoice = Callable[[str, list], Optional[str]]


def make_handler(ctx) -> Callable[..., str]:
    """The registered handler: ``handle`` with the context's ``ask_choice`` and ``delegation_backends``
    (each None on a core without it)."""
    def delegation_command(raw_args: str = "", session_key: str = "", **_: Any) -> str:
        return handle(raw_args, session_key=session_key, ask_choice=getattr(ctx, "ask_choice", None),
                      api=getattr(ctx, "delegation_backends", None))
    return delegation_command


def _problem(mode: str, api) -> str:
    """Why backend ``mode`` cannot run here ("" when it can, or when this core cannot tell)."""
    if api is None:
        return ""
    info = next((b for b in api.list_backends() if b.get("name") == mode), None)
    if info is None:
        return f"Delegation backend '{mode}' is not available: no enabled plugin provides it."
    return "" if info.get("available") else (info.get("reason") or f"Delegation backend '{mode}' is not available.")


def _settings(section: Mapping[str, Any], mode: str) -> Mapping[str, Any]:
    backends = section.get("backends")
    block = backends.get(mode) if isinstance(backends, Mapping) else None
    return block if isinstance(block, Mapping) else {}


def _ready(mode: str, section: Mapping[str, Any]) -> list[str]:
    """Names the mode would route to; empty for a mode with nothing configured (``default``: [""])."""
    if mode == "model_map":
        from .model_map import configured_options
        return list(configured_options(_settings(section, mode)))
    if mode == "profile_routes":
        from .profile_routes import configured_profiles
        return [name for name, _ in configured_profiles(_settings(section, mode))]
    return [""]


def _mode_default(mode: str, section: Mapping[str, Any]) -> str:
    default = store.route_default(section) if mode == "model_map" else store.profile_default(section)
    return default if default in _ready(mode, section) else ""


def _detail(mode: str, section: Mapping[str, Any]) -> str:
    if mode == "default":
        model = " ".join(str(section.get("model") or "").split()) or "the parent's model"
        return f"subagents use delegation.model: {model}"
    names = _ready(mode, section)
    if not names:
        return "nothing set up yet (`hermes delegation config`)"
    default = _mode_default(mode, section)
    shown = ", ".join(f"{n} (default)" if n == default else n for n in names)
    noun = "route" if mode == "model_map" else "profile"
    return f"{len(names)} {noun}{'s' * (len(names) != 1)}: {shown}"


def _parse(raw: str) -> tuple[Optional[str], bool, str]:
    """``(mode or None, global?, unknown word)`` from the raw arguments."""
    words = raw.split()
    is_global = any(w in ("--global", "-g") for w in words)
    rest = " ".join(w for w in words if w not in ("--global", "-g")).strip()
    if not rest:
        return None, is_global, ""
    key = re.sub(r"[\s-]+", "_", rest.lower())
    return _ALIASES.get(key), is_global, ("" if key in _ALIASES else rest)


def _headline(effective: str, saved: str) -> str:
    line = f"Delegation mode: {store.mode_label(effective)}"
    return line + (f" (this session) · saved: {store.mode_label(saved)}" if effective != saved else "")


def _listing(section: Mapping[str, Any], effective: str, saved: str, api) -> str:
    lines = [_headline(effective, saved)]
    problem = _problem(effective, api)
    if problem:
        lines.append(f"⚠ {problem} Switch to a mode below to fix delegation.")
    for i, mode in enumerate(store.MODES, 1):
        mark = "  ← current" if mode == effective else ""
        lines.append(f"  {i}. {store.MODES[mode]:<15} {_detail(mode, section)}{mark}")
    lines.append(f"Switch: {USAGE}  (no flag = this session's routing only; the agent's choices change "
                 "with --global + /new)")
    return "\n".join(lines)


def _ask(ask_choice: AskChoice, section: Mapping[str, Any], effective: str, saved: str) -> Optional[str]:
    """The mode the user picked from the surface's own picker, or None (nothing can ask, cancelled)."""
    choices = [{"value": mode, "label": store.MODES[mode] + (" (current)" if mode == effective else ""),
                "description": _detail(mode, section)}
               for mode in store.MODES if _ready(mode, section)]
    question = (f"{_headline(effective, saved)}. Switch this session's routing to (the agent's choices "
                "change only with --global + /new):")
    return ask_choice(question, choices)


def _unrouted_target(mode: str, section: Mapping[str, Any]) -> str:
    """Where a task that picks nothing runs in ``mode``."""
    if mode == "default":
        model = " ".join(str(section.get("model") or "").split())
        return f"delegation.model ({model})" if model else "the parent's model"
    default = _mode_default(mode, section)
    noun = "route" if mode == "model_map" else "profile"
    return f"the default {noun} '{default}'" if default else "the parent's model"


def _session_note(mode: str, saved: str, section: Mapping[str, Any]) -> str:
    """A session switch moves routing now, but the chat's ``delegate_task`` schema (prompt cache) keeps the
    choices of the mode it started with (the saved one, as ``/new`` starts there) until ``/new``."""
    if mode == saved:
        return ""
    return (" Routing changes now; the agent's per-task choices (option/profile) follow the mode this chat "
            "started with until /new. A task that sends a choice this mode doesn't have is refused; one that "
            f"sends none runs on {_unrouted_target(mode, section)}. --global saves it; /new then gives a session "
            "whose choices match.")


def handle(raw_args: str = "", session_key: str = "", ask_choice: Optional[AskChoice] = None, api=None) -> str:
    """``api``: ``ctx.delegation_backends`` (None on a core without it); ``ask_choice``: ``ctx.ask_choice``,
    which itself returns None where the surface cannot ask (a hook, cron, an event-loop handler)."""
    section = store.delegation_section()
    saved = store.current_mode(section)
    scoped = api is not None and bool(session_key)
    effective = (api.get_session_backend(session_key) if scoped else None) or saved
    mode, is_global, unknown = _parse(raw_args or "")
    if unknown:
        return f"Unknown delegation mode '{unknown}'. Usage: {USAGE}"
    if mode is None:
        mode = _ask(ask_choice, section, effective, saved) if ask_choice and scoped else None
        if mode is None:
            return _listing(section, effective, saved, api)
    return _switch(mode, is_global, section, saved, api, session_key if scoped else "")


def _switch(mode: str, is_global: bool, section: Mapping[str, Any], saved: str, api, session_key: str) -> str:
    if not _ready(mode, section):
        return (f"{store.mode_label(mode)} has nothing set up yet, so the agent would have nothing to pick. "
                "Set it up with `hermes delegation config`, then switch.")
    if not is_global and not session_key:
        return (f"This Hermes can't switch delegation for one session yet. Use `/delegation {mode} --global` "
                "to save it for this profile.")
    try:
        if is_global and api is not None:
            # Core writes delegation.backend and drops the session's override (the session follows the saved
            # mode again); it refuses a package-manager-managed install with PermissionError.
            api.set_global_backend(mode, session_key=session_key)
        elif is_global:
            store.set_mode(mode)  # a core without ctx.delegation_backends: there are no session overrides
        else:
            api.set_session_backend(session_key, mode)
    except (ValueError, PermissionError, RuntimeError) as exc:  # cannot run here, managed install, bad config
        return f"Not switched: {exc}"
    scope = "saved for this profile" if is_global else "for this session (add --global to save it)"
    note = " delegation.model / delegation.provider apply again." if mode == "default" else (
        f" {_detail(mode, section).capitalize()}.")
    if is_global:
        tail = " /new starts a session whose choices match." if mode != saved else ""
    else:
        tail = _session_note(mode, saved, section)
    return f"Delegation mode: {store.mode_label(mode)}, {scope}.{note}{tail}"
