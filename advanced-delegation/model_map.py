"""The ``model_map`` delegation provider.

Settings live in ``delegation.backends.model_map`` of config.yaml::

    delegation:
      backend: model_map
      backends:
        model_map:
          options:
            - name: fast                    # what the model picks
              description: Cheap and quick; lookups, summaries, simple edits.
              provider: openrouter          # provider and/or base_url, plus model
              model: google/gemini-3-flash-preview
            - name: deep
              description: Slow and careful; hard debugging and design work.
              provider: anthropic
              model: claude-opus-5
              reasoning_effort: high
              service_tier: fast            # optional; core refuses the spawn if the route has no fast tier
            - name: local
              description: Private; runs on this machine.
              base_url: http://localhost:8080/v1
              model: qwen3-coder
            - name: daybreak
              description: Sol with reduced guardrails; security work.
              provider: openai-codex        # Daybreak needs the ChatGPT/Codex subscription route
              model: gpt-6-sol              # the real base model, never a *-daybreak-* alias
              daybreak: true                # sends access_programs.cyber: daybreak_blue
          default: fast                     # optional: option for tasks that set none

``delegate_task`` gains a per-task ``option`` enum of those names. A task that sets one runs on that
entry's route; a task without one runs on ``default`` when set, else on the parent's own route
(in this mode ``delegation.model`` / ``delegation.provider`` are inactive). The raw
provider/model/endpoint/key never become model-facing parameters.

``daybreak: true`` runs that route's model with OpenAI's Daybreak trusted-access program
(``access_programs.cyber: daybreak_blue``), the same request field core sends when the Daybreak switch
is on. It is the route's own opt-in, independent of ``agent.daybreak``, so one base model can back a
plain route and a Daybreak route. Core's other Daybreak rules still hold: the route must be on
``openai-codex``, the account's Codex catalog must list Daybreak for the model, and the delegating
session must be one of the owner's own surfaces (CLI, TUI, Desktop). Otherwise the task is refused,
never run without the program.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from agent.delegation_provider import DelegationProvider

logger = logging.getLogger(__name__)

BACKEND = "model_map"
SETTINGS_PATH = f"delegation.backends.{BACKEND}"
PARAMETER = "option"
#: Entry keys copied into the route (same meaning as the matching core route fields).
ROUTE_FIELDS = ("provider", "model", "base_url", "api_key", "api_mode", "reasoning_effort", "service_tier")
DAYBREAK_KEY = "daybreak"
_warned: set[str] = set()


def _warn_once(message: str, *args: Any) -> None:
    # task_parameters/route_task run on every session build and every task; say each problem once.
    text = message % args
    if text not in _warned:
        _warned.add(text)
        logger.warning(text)


def _text(entry: Mapping[str, Any], key: str) -> str:
    value = entry.get(key)
    return " ".join(value.split()) if isinstance(value, str) else ""


def _entry_problem(entry: Any) -> str:
    if not isinstance(entry, Mapping):
        return "is not a mapping"
    if not _text(entry, "name") or not _text(entry, "description"):
        return "needs a name and a description"
    if not _text(entry, "model"):
        return "needs a model"
    if not (_text(entry, "provider") or _text(entry, "base_url")):
        return "needs a provider or a base_url"
    daybreak = entry.get(DAYBREAK_KEY)
    if daybreak not in (None, True, False):
        return f"has {DAYBREAK_KEY}: {daybreak!r}; use true or false"
    if daybreak is True and not _is_codex(_text(entry, "provider")):
        return f"sets {DAYBREAK_KEY}: true, which needs provider: openai-codex"
    return ""


def _is_codex(provider: str) -> bool:
    if not provider:
        return False
    from hermes_cli.models import normalize_provider
    return normalize_provider(provider) == "openai-codex"


def configured_options(settings: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """``{name: entry}`` from the backend settings' ``options`` in config order, skipping malformed
    entries and repeated names (each skipped entry is warned once). Each entry keeps its description
    plus the non-empty route fields."""
    raw = settings.get("options")
    if raw is None:
        return {}
    if not isinstance(raw, list):
        _warn_once("%s.options must be a list of {name, description, provider/base_url, model} entries; "
                   "ignoring it.", SETTINGS_PATH)
        return {}
    options: dict[str, dict[str, Any]] = {}
    for i, entry in enumerate(raw):
        problem = _entry_problem(entry)
        if problem:
            _warn_once("%s.options[%d] %s; skipped.", SETTINGS_PATH, i, problem)
            continue
        name = _text(entry, "name")
        if name in options:
            _warn_once("%s.options[%d] repeats option %r; skipped.", SETTINGS_PATH, i, name)
            continue
        route = {key: entry[key].strip() for key in ROUTE_FIELDS
                 if isinstance(entry.get(key), str) and entry[key].strip()}
        if isinstance(entry.get("fallback_providers"), list):
            route["fallback_providers"] = list(entry["fallback_providers"])
        options[name] = {"description": _text(entry, "description"), "route": route,
                         "daybreak": entry.get(DAYBREAK_KEY) is True}
    return options


def _gateway_owner_chat() -> Optional[bool]:
    """The gateway's verdict on the turn's chat: ``True`` for the owner's own, ``False`` for any other chat it judged
    (a hosted room, an A2A peer, a ``/bg`` task from one), ``None`` when nothing judged it (classic CLI, the TUI
    backend) or the core predates the verdict. A gateway-hosted chat runs on the ``local`` transport and a ``/bg``
    task binds no session at all, so the session source alone cannot tell them apart."""
    try:
        from agent.session_source import turn_daybreak_surface
    except ImportError:
        return None
    verdict = turn_daybreak_surface()
    return verdict if isinstance(verdict, bool) else None


def _daybreak_route(name: str, route: Mapping[str, Any]) -> dict[str, Any]:
    """``route`` plus the Daybreak request field, or :class:`DelegationRouteError` when core's Daybreak rules
    do not allow it here. Refusing (not dropping the field) keeps a task off a route it did not ask for."""
    from agent.delegation_provider import DelegationRouteError
    try:
        from agent.codex_service_tiers import DAYBREAK_PROGRAM, daybreak_offered
        from agent.session_source import DAYBREAK_SOURCES, session_source_for
        from gateway.session_context import get_session_env
    except ImportError as exc:
        raise DelegationRouteError(
            f"Option {name!r} needs Daybreak, which this Hermes does not support ({exc.name}). Update Hermes or "
            f"pick another option.") from exc
    source = session_source_for(get_session_env("HERMES_SESSION_PLATFORM", "") or None)
    if str(get_session_env("HERMES_CRON_SESSION", "") or "").strip().lower() in {"1", "true", "yes", "on"}:
        source = "cron"  # a cron job binds no platform, so the fallback would read it as ``cli``
    verdict = _gateway_owner_chat()
    if verdict is False or (verdict is None and source not in DAYBREAK_SOURCES) or source == "cron":
        where = "the gateway does not count this chat as the owner's" if verdict is False and source != "cron" \
            else f"this session is {source!r}"
        raise DelegationRouteError(
            f"Option {name!r} uses Daybreak, which only runs from the owner's own CLI, TUI or Desktop chats "
            f"({where}). Pick another option.")
    if not daybreak_offered(route.get("model"), provider=route.get("provider"), base_url=route.get("base_url")):
        raise DelegationRouteError(
            f"Option {name!r} uses Daybreak, but this account's Codex catalog does not list Daybreak for "
            f"{route.get('model')!r} (or the catalog could not be read). Pick another option.")
    overrides = dict(route.get("request_overrides") or {})
    extra_body = dict(overrides.get("extra_body") or {})
    extra_body["access_programs"] = {**dict(extra_body.get("access_programs") or {}), "cyber": DAYBREAK_PROGRAM}
    return {**route, "request_overrides": {**overrides, "extra_body": extra_body}}


class ModelMapDelegationProvider(DelegationProvider):
    """``delegation.backend: model_map``: the model picks a named model option per subagent, from the
    options in ``delegation.backends.model_map``."""

    @property
    def name(self) -> str:
        return BACKEND

    @property
    def display_name(self) -> str:
        return "Model routes"

    @property
    def summary(self) -> str:
        return "The agent picks a named model route for each subagent."

    @property
    def route_keys(self) -> tuple[str, ...]:
        # Route keys beyond core's that settings editors may write and must keep; the Desktop route editor offers
        # its Daybreak control only when this backend declares it. Older cores never read this.
        return (DAYBREAK_KEY,)

    def task_parameters(self, config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        options = configured_options(self.settings(config))
        if not options:
            return {}
        lines = [
            "Which configured model runs this subagent. Omit for the default subagent route.",
            *(f"{name}: {option['description']}" for name, option in options.items()),
        ]
        return {PARAMETER: {"type": "string", "enum": list(options), "description": "\n".join(lines)}}

    def description(self, config: Mapping[str, Any]) -> str:
        if not configured_options(self.settings(config)):
            return ""
        return "Each task may set `option` to run that subagent on one of the listed models."

    def route_task(self, task: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
        settings = self.settings(config)
        options = configured_options(settings)
        chosen = task.get(PARAMETER)
        if not chosen:
            default = settings.get("default")
            chosen = default.strip() if isinstance(default, str) else ""
            if chosen and chosen not in options:
                _warn_once("%s.default %r is not an option in %s.options; tasks without an option use the "
                           "default route.", SETTINGS_PATH, chosen, SETTINGS_PATH)
                chosen = ""
        if not chosen:
            return {}  # the parent's route: delegation.model / delegation.provider are inactive in this mode
        # Core already refused a chosen value outside the enum before calling us.
        route = options[chosen]["route"]
        if options[chosen]["daybreak"]:
            route = _daybreak_route(chosen, route)
        return {**route, "label": chosen}
