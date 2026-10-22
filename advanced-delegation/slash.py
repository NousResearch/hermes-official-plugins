"""``/delegation``: show the delegation mode and switch it (CLI, TUI, Desktop, messaging).

``/delegation``                 the current mode, what each mode routes to, and how to switch
``/delegation <mode>``          switch this session (``default``, ``model_map``/``models``,
                                ``profile_routes``/``profiles``, or the number shown)
``/delegation <mode> --global`` also save it as this profile's ``delegation.backend``

Session scoping follows ``/model``: core keeps the per-session choice (``agent.delegation_registry``
``get_session_backend`` / ``set_session_backend``, feature-detected); on a core without it a plain
switch says to use ``--global``. When the configured backend fails, core's error names this
command, so the listing flags an unavailable backend and switching away from it is the fix.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from . import store

_ALIASES = {
    "default": "default", "1": "default", "off": "default", "builtin": "default",
    "model_map": "model_map", "2": "model_map", "models": "model_map", "model_routes": "model_map",
    "routes": "model_map", "model": "model_map",
    "profile_routes": "profile_routes", "3": "profile_routes", "profiles": "profile_routes",
    "profile": "profile_routes",
}
USAGE = "/delegation [default | model_map | profile_routes] [--global]"


def _session_api():
    """Core's per-session backend calls, or None on a core without them."""
    from agent import delegation_registry as registry
    get = getattr(registry, "get_session_backend", None)
    set_ = getattr(registry, "set_session_backend", None)
    return (get, set_) if callable(get) and callable(set_) else None


def _registered(name: str) -> bool:
    from agent import delegation_registry as registry
    return name == "default" or registry.get_provider(name) is not None


def _ready(mode: str, section: Mapping[str, Any]) -> list[str]:
    """Names the mode would route to; empty for a mode with nothing configured (``default``: [""])."""
    if mode == "model_map":
        from .model_map import configured_options
        return list(configured_options(_settings(section, mode)))
    if mode == "profile_routes":
        from .profile_routes import configured_profiles
        return [name for name, _ in configured_profiles(_settings(section, mode))]
    return [""]


def _settings(section: Mapping[str, Any], mode: str) -> Mapping[str, Any]:
    backends = section.get("backends")
    block = backends.get(mode) if isinstance(backends, Mapping) else None
    return block if isinstance(block, Mapping) else {}


def _detail(mode: str, section: Mapping[str, Any]) -> str:
    if mode == "default":
        model = " ".join(str(section.get("model") or "").split()) or "the parent's model"
        return f"subagents use delegation.model: {model}"
    names = _ready(mode, section)
    if not names:
        return "nothing set up yet (`hermes delegation config`)"
    default = store.route_default(section) if mode == "model_map" else store.profile_default(section)
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


def _listing(section: Mapping[str, Any], effective: str, saved: str, scoped: bool) -> str:
    where = " (this session)" if scoped and effective != saved else ""
    lines = [f"Delegation mode: {store.mode_label(effective)}{where}"]
    if scoped and effective != saved:
        lines[0] += f" · saved: {store.mode_label(saved)}"
    if not _registered(effective):
        lines.append(f"⚠ delegation.backend '{effective}' is not available (plugin missing or disabled). "
                     "Switch to a mode below to fix delegation.")
    for i, mode in enumerate(store.MODES, 1):
        mark = "  ← current" if mode == effective else ""
        lines.append(f"  {i}. {store.MODES[mode]:<15} {_detail(mode, section)}{mark}")
    lines.append(f"Switch: {USAGE}  (no flag = this session only)")
    return "\n".join(lines)


def handle(raw_args: str = "", session_key: str = "", **_: Any) -> str:
    section = store.delegation_section()
    saved = store.current_mode(section)
    api = _session_api()
    effective = (api[0](session_key) if api and session_key else None) or saved
    mode, is_global, unknown = _parse(raw_args or "")
    if unknown:
        return f"Unknown delegation mode '{unknown}'. Usage: {USAGE}"
    if mode is None:
        return _listing(section, effective, saved, bool(api and session_key))
    if not _ready(mode, section):
        return (f"{store.mode_label(mode)} has nothing set up yet, so the agent would have nothing to pick. "
                "Set it up with `hermes delegation config`, then switch.")
    if not is_global and not (api and session_key):
        return (f"This Hermes can't switch delegation for one session yet. Use `/delegation {mode} --global` "
                "to save it for this profile.")
    if api and session_key:
        api[1](session_key, mode)
    if is_global:
        store.set_mode(mode)
    scope = "saved for this profile" if is_global else "for this session (add --global to save it)"
    note = (" delegation.model / delegation.provider apply again." if mode == "default"
            else f" {_detail(mode, section).capitalize()}.")
    return f"Delegation mode: {store.mode_label(mode)}, {scope}.{note}"
