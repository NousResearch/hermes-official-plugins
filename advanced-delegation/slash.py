"""``/delegation``: show the delegation mode and save a new one (CLI, TUI, Desktop, messaging).

``/delegation``               the saved mode and a picker to change it (a clarify card with buttons where
                              the surface has them, a numbered list elsewhere), then a second question:
                              next session (recommended) or now; plain text when nothing can ask
                              (``ctx.ask_choice`` absent or None)
``/delegation <mode>``        save it as this profile's ``delegation.backend``; it takes effect from the
                              next session (``/new``). Modes: ``default``, ``model_map``/``models``,
                              ``profile_routes``/``profiles``, or the number shown
``/delegation <mode> --now``  save it AND apply it to this chat now
``/delegation preset [NAME] [--now]`` list/pick or select a saved model-route preset

Saving for the next session is the default because the mode is part of the chat's cached prompt: the
``delegate_task`` schema carries the mode's per-task choices (``option`` / ``profile``), and swapping it
mid-chat breaks the prompt cache. ``--now`` is the opt-in: core rebuilds this session's tool schema at
its next turn (one cache break) so the agent's choices follow the new mode in this chat too.

Core does both behind ``ctx.delegation_backends`` (``list_backends`` /
``set_global_backend(name, session_key=, apply_now=)``). It is a host remote facade, so under
``plugins.isolation: host`` the save and the rebuild land in the Hermes process the chat runs in, not
in the plugin host. Feature-detected: on a core without the API the plugin writes
``delegation.backend`` itself through ``store``; on a core whose ``set_global_backend`` has no
``apply_now``, ``--now`` saves for the next session and says it needs a newer Hermes. When the
configured backend fails, core's error names this command, so the listing flags a backend that cannot
run and switching away from it is the fix.
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
USAGE = "/delegation [default | model_map | profile_routes] [--now]"
AskChoice = Callable[[str, list], Optional[str]]
_WHEN = [{"value": "next", "label": "Next session (recommended)",
          "description": "Saved now; /new starts a chat that uses it. This chat keeps its prompt cache."},
         {"value": "now", "label": "Now (rebuilds this chat's tools)",
          "description": "Saved and applied to this chat: its tools are rebuilt at your next message "
                         "(one prompt-cache break)."}]


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
    """``(mode or None, now?, what was not understood)`` from the raw arguments."""
    words = raw.split()
    flags = [w for w in words if w.startswith("-")]
    unknown_flag = next((f for f in flags if f != "--now"), "")
    if unknown_flag:
        return None, False, unknown_flag
    now = "--now" in flags
    rest = " ".join(w for w in words if w not in flags).strip()
    if not rest:
        return None, now, ""
    key = re.sub(r"[\s-]+", "_", rest.lower())
    return _ALIASES.get(key), now, ("" if key in _ALIASES else rest)


def _listing(section: Mapping[str, Any], saved: str, api) -> str:
    lines = [f"Delegation mode: {store.mode_label(saved)}"]
    problem = _problem(saved, api)
    if problem:
        lines.append(f"⚠ {problem} Switch to a mode below to fix delegation.")
    for i, mode in enumerate(store.MODES, 1):
        mark = "  ← current" if mode == saved else ""
        lines.append(f"  {i}. {store.MODES[mode]:<15} {_detail(mode, section)}{mark}")
    lines.append("Switch: /delegation <mode> saves it for the next session (/new); "
                 "/delegation <mode> --now also applies it to this chat (rebuilds its tools once).")
    return "\n".join(lines)


def _ask_mode(ask_choice: AskChoice, section: Mapping[str, Any], saved: str) -> Optional[str]:
    """The mode the user picked from the surface's own picker, or None (nothing can ask, cancelled)."""
    choices = [{"value": mode, "label": store.MODES[mode] + (" (current)" if mode == saved else ""),
                "description": _detail(mode, section)}
               for mode in store.MODES if _ready(mode, section)]
    return ask_choice(f"Delegation mode: {store.mode_label(saved)}. Switch to:", choices)


def handle(raw_args: str = "", session_key: str = "", ask_choice: Optional[AskChoice] = None, api=None) -> str:
    """``api``: ``ctx.delegation_backends`` (None on a core without it); ``ask_choice``: ``ctx.ask_choice``,
    which itself returns None where the surface cannot ask (a hook, cron, an event-loop handler)."""
    section = store.delegation_section()
    saved = store.current_mode(section)
    words = (raw_args or "").split()
    if words and words[0].lower() == "preset":
        return _preset(words[1:], section, session_key, ask_choice, api)
    mode, now, unknown = _parse(raw_args or "")
    if unknown.startswith("-"):
        return f"Unknown option '{unknown}'. Usage: {USAGE}"
    if unknown:
        return f"Unknown delegation mode '{unknown}'. Usage: {USAGE}"
    if mode is None:
        mode = _ask_mode(ask_choice, section, saved) if ask_choice else None
        if mode is None:
            return _listing(section, saved, api)
        if api is not None and not now:  # only core can apply a mode to the running chat
            when = ask_choice(f"Use {store.mode_label(mode)} from:", _WHEN)
            if when is None:
                return f"Delegation mode unchanged: {store.mode_label(saved)}."
            now = when == "now"
    return _switch(mode, now, section, saved, api, session_key)


def _preset(words: list[str], section: Mapping[str, Any], session_key: str,
            ask_choice: Optional[AskChoice], api) -> str:
    usage = "Usage: /delegation preset [NAME] [--now]"
    flags = [word for word in words if word.startswith("-")]
    names = [word for word in words if not word.startswith("-")]
    if any(flag != "--now" for flag in flags) or len(flags) > 1 or len(names) > 1:
        return f"Invalid preset arguments. {usage}"
    now = bool(flags)
    try:
        presets = store.list_presets(section)
        active = store.active_preset(section)
        if not names:
            if not presets:
                return "No model-route presets saved. Create one with `hermes delegation preset save NAME`."
            choices = [{"value": name, "label": name + (" (current)" if name == active else "")}
                       for name in presets]
            name = ask_choice("Choose a model-route preset:", choices) if ask_choice else None
            if name is None:
                return "Model-route presets:\n" + "\n".join(
                    f"  {i}. {item['label']}" for i, item in enumerate(choices, 1)) + f"\n{usage}"
            if name not in presets:
                return f"Invalid preset selection. {usage}"
            if api is not None and not now:
                when = ask_choice(f"Use preset {name} from:", _WHEN)
                if when is None:
                    return "Preset unchanged."
                if when not in ("now", "next"):
                    return "Invalid timing selection. Preset unchanged."
                now = when == "now"
        else:
            name = names[0]
        snapshot = store.get_preset(section, name)
        candidate = {"backends": {"model_map": snapshot}}
        if not _ready("model_map", candidate):
            return f"Preset {name!r} has no configured model routes; not switched."
        if now and not session_key:
            return "Not switched: --now requires a session key."
        problem = _problem("model_map", api)
        if problem:
            return f"Not switched: {problem}"
        store.use_preset(name)
    except (ValueError, PermissionError, RuntimeError, OSError) as exc:
        return f"Not switched: {exc}"

    head = f"Preset {name!r} saved"
    if not now:
        return (f"{head} for the next session (/new). Add --now to rebuild this chat's tools. "
                "Older Hermes versions that do not pin route settings may use the saved models "
                "in existing chats before then; update Hermes for next-session isolation.")
    if api is None:
        return f"{head} for the next session (/new). Applying it now needs a newer Hermes."
    try:
        api.set_global_backend("model_map", session_key=session_key, apply_now=True)
    except TypeError as exc:
        if "apply_now" in str(exc) and ("unexpected keyword" in str(exc) or "keyword argument" in str(exc)):
            return f"{head} for the next session (/new). Applying it now needs a newer Hermes."
        return f"{head}, but applying it now failed: {exc}"
    except (ValueError, PermissionError, RuntimeError, OSError) as exc:
        return f"{head}, but applying it now failed: {exc}"
    return f"{head} and applied to this chat: its tools rebuild at the next message (one prompt-cache break)."


def _save(mode: str, now: bool, api, session_key: str) -> bool:
    """Save ``mode`` (and apply it to this chat when ``now``); True when it was applied now."""
    if api is None:
        store.set_mode(mode)  # a core without ctx.delegation_backends
        return False
    if not now:
        api.set_global_backend(mode, session_key=session_key)
        return False
    try:
        api.set_global_backend(mode, session_key=session_key, apply_now=True)
    except TypeError:  # a core from before apply_now refuses the keyword before writing anything
        api.set_global_backend(mode, session_key=session_key)
        return False
    return True


def _switch(mode: str, now: bool, section: Mapping[str, Any], saved: str, api, session_key: str) -> str:
    if not _ready(mode, section):
        return (f"{store.mode_label(mode)} has nothing set up yet, so the agent would have nothing to pick. "
                "Set it up with `hermes delegation config`, then switch.")
    head = f"Delegation mode: {store.mode_label(mode)}"
    if mode == saved and not now:
        return f"{head} is already saved. Add --now to apply it to this chat (rebuilds its tools once)."
    try:
        applied = _save(mode, now, api, session_key)
    except (ValueError, PermissionError, RuntimeError) as exc:  # cannot run here, managed install, bad config
        return f"Not switched: {exc}"
    note = " delegation.model / delegation.provider apply again." if mode == "default" else (
        f" {_detail(mode, section).capitalize()}.")
    if applied:
        return (f"{head}, saved and applied to this chat: its tools are rebuilt for it at your next message "
                f"(one prompt-cache break).{note}")
    if now:
        return f"{head}, saved for the next session (/new). Applying it to this chat needs a newer Hermes.{note}"
    return (f"{head}, saved. It takes effect from the next session (/new); this chat's tools keep the previous "
            f"mode's choices until then (add --now to apply it here, which rebuilds this chat's tools once).{note}")
