"""System-1 Decider auto-router for ``model_map`` subagent delegation.

When ``delegation.backend`` is ``model_map`` and the Decider is enabled (the plugin's own settings,
``plugins.entries.advanced-delegation.settings.decider``), tasks handed to ``delegate_task`` without an
explicit ``option`` are evaluated in a single batched ``/v1/systemone`` call (local Ollama
``pplx-decider`` or TypeSafe Cloud ``jev-latest``).

For each unassigned task, the Decider answers two structured questions in one forward pass:
1. ``t{i}_needs_specialist`` (``noul``): does this task genuinely require a specialist/frontier
   route (multimodal/visual analysis, complex architecture/refactoring, adversarial verification,
   offensive security, or deep multi-source research), or is the default fast worker sufficient?
2. ``t{i}_route`` (``choice``): which eligible route best matches the task?

To minimize TTFT on non-causal decision models (such as ``pplx-decider:27b-nvfp4`` on Apple Silicon MLX,
where ``NewCaches()`` is nil and prefill cost scales linearly with prompt tokens), full route
descriptions are placed once in ``state.available_routes`` and multi-task batches use compact route
keys in per-question ``criteria``.
"""

from __future__ import annotations

from collections import OrderedDict
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Mapping
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

PLUGIN_ID = "advanced-delegation"
DEFAULT_DECIDER_URL = "http://localhost:11434/v1/systemone"
DEFAULT_DECIDER_MODEL = "pplx-decider:27b-nvfp4"
DEFAULT_FALLBACK_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_FALLBACK_MODEL = "jev-latest"
DEFAULT_DECIDER_TIMEOUT = 18.0
DEFAULT_SPECIALIST_THRESHOLD = 0.70
DEFAULT_CONFIDENCE_THRESHOLD = 0.60

# Routes excluded from automatic assignment unless explicitly requested by the caller:
# paid-per-token routes and advisor-only routes with strict quota/latency budgets.
DEFAULT_EXCLUDED_OPTIONS: tuple[str, ...] = (
    "UltraFastPaid",
    "AbsoluteSOTAGoogle",
    "AbsoluteSOTAOpenAI",
    "AbsoluteSOTAAnthropic",
    "AbsoluteSOTAXAI",
)

_MAX_LOG_ENTRIES = 100
_LAN_COOLDOWN_UNTIL: float = 0.0
_LAN_COOLDOWN_SECONDS: float = 45.0


def _head_tail(text: str, head: int = 320, tail: int = 160) -> str:
    """Keep the beginning and end of ``text`` so batch prefill stays compact."""
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= head + tail + 24:
        return cleaned
    omitted = len(cleaned) - head - tail
    return f"{cleaned[:head]} ...[+{omitted} chars]... {cleaned[-tail:]}"


def _is_ollama_endpoint(url: str) -> bool:
    lower = (url or "").lower()
    return (
        ":11434" in lower
        or "127.0.0.1" in lower
        or "localhost" in lower
        or "192.168." in lower
        or "10." in lower
    )


def _normalize_systemone_url(raw_url: str) -> str:
    url = str(raw_url or "").strip().rstrip("/")
    if not url:
        return DEFAULT_DECIDER_URL
    if url.endswith("/v1/systemone") or url.endswith("/v1/score"):
        return url
    if url.endswith("/v1"):
        return f"{url}/systemone"
    return f"{url}/v1/systemone"


def _adapt_questions_for_endpoint(questions: Mapping[str, Any], url: str) -> dict[str, Any]:
    """Ollama expects ``criteria`` on all question types; TypeSafe Cloud expects ``criterion`` on ``noul``."""
    use_ollama = _is_ollama_endpoint(url)
    adapted: dict[str, Any] = {}
    for qname, qspec in questions.items():
        if not isinstance(qspec, Mapping):
            continue
        q = dict(qspec)
        qtype = q.get("type")
        if use_ollama:
            if "criterion" in q and "criteria" not in q:
                q["criteria"] = q.pop("criterion")
        elif qtype == "noul":
            if "criteria" in q and "criterion" not in q:
                q["criterion"] = q.pop("criteria")
        adapted[qname] = q
    return adapted


def _typesafe_api_key() -> str:
    for env_key in ("TYPESAFE_API_KEY", "JEV_API_KEY"):
        val = os.environ.get(env_key, "").strip()
        if val:
            return val
    try:
        from hermes_constants import get_hermes_home
        env_path = get_hermes_home() / ".env"
        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                stripped = line.strip()
                if stripped.startswith(("TYPESAFE_API_KEY=", "JEV_API_KEY=")):
                    return stripped.split("=", 1)[1].strip().strip("'\"")
    except Exception:
        pass
    return ""


def saved_decider() -> Mapping[str, Any]:
    """The Decider settings this plugin saved: ``plugins.entries.advanced-delegation.settings.decider`` in the
    active profile's config. They are the plugin's own settings, not part of ``delegation.backends.model_map``,
    because core's route editor rewrites that block from its routes and default alone."""
    from hermes_cli.config import load_config_readonly
    node: Any = load_config_readonly() or {}
    for key in ("plugins", "entries", PLUGIN_ID, "settings", "decider"):
        node = node.get(key) if isinstance(node, Mapping) else None
    return node if isinstance(node, Mapping) else {}


def decider_config(settings: Mapping[str, Any] | None) -> dict[str, Any]:
    """Effective System-1 Decider settings: the saved ones (:func:`saved_decider`) over a ``decider`` block in
    the ``model_map`` backend block ``settings``, over the defaults."""
    block = settings if isinstance(settings, Mapping) else {}
    saved = saved_decider()
    if saved:
        nested = block.get("decider")
        block = {**block, "decider": {**(nested if isinstance(nested, Mapping) else {}), **saved}}
    return normalize_decider(block)


def normalize_decider(settings: Mapping[str, Any] | None) -> dict[str, Any]:
    """Complete, range-checked Decider settings from a mapping that holds them under ``decider``."""
    block = settings if isinstance(settings, Mapping) else {}
    nested = block.get("decider")
    sub = nested if isinstance(nested, Mapping) else {}

    env_enabled = os.environ.get("JEV_DECIDER_ENABLED", "").strip().lower()
    if "enabled" in sub:
        enabled = bool(sub.get("enabled"))
    elif "decider_routing_enabled" in block:
        enabled = bool(block.get("decider_routing_enabled"))
    elif env_enabled in ("1", "true", "yes", "on"):
        enabled = True
    elif env_enabled in ("0", "false", "no", "off"):
        enabled = False
    else:
        enabled = False

    raw_url = (
        sub.get("url")
        or block.get("decider_url")
        or os.environ.get("JEV_DECIDER_URL")
        or os.environ.get("JEV_BASE_URL")
        or DEFAULT_DECIDER_URL
    )
    raw_model = (
        sub.get("model")
        or block.get("decider_model")
        or os.environ.get("JEV_DECIDER_MODEL")
        or os.environ.get("JEV_MODEL")
        or DEFAULT_DECIDER_MODEL
    )
    raw_fallback_url = (
        sub.get("fallback_url")
        or block.get("decider_fallback_url")
        or os.environ.get("JEV_FALLBACK_URL")
        or DEFAULT_FALLBACK_URL
    )
    raw_fallback_model = (
        sub.get("fallback_model")
        or block.get("decider_fallback_model")
        or os.environ.get("JEV_FALLBACK_MODEL")
        or DEFAULT_FALLBACK_MODEL
    )
    allow_fallback = bool(
        sub.get("allow_fallback", block.get("decider_allow_fallback", True))
    )

    try:
        timeout = float(sub.get("timeout", block.get("decider_timeout", DEFAULT_DECIDER_TIMEOUT)))
    except (TypeError, ValueError):
        timeout = DEFAULT_DECIDER_TIMEOUT

    try:
        specialist_threshold = float(
            sub.get(
                "specialist_threshold",
                block.get("decider_specialist_threshold", DEFAULT_SPECIALIST_THRESHOLD),
            )
        )
    except (TypeError, ValueError):
        specialist_threshold = DEFAULT_SPECIALIST_THRESHOLD

    try:
        confidence_threshold = float(
            sub.get(
                "confidence_threshold",
                block.get("decider_confidence_threshold", DEFAULT_CONFIDENCE_THRESHOLD),
            )
        )
    except (TypeError, ValueError):
        confidence_threshold = DEFAULT_CONFIDENCE_THRESHOLD

    raw_excluded = sub.get("excluded_options", block.get("decider_excluded_options"))
    if isinstance(raw_excluded, (list, tuple, set)):
        excluded = [str(x).strip() for x in raw_excluded if str(x).strip()]
    else:
        excluded = list(DEFAULT_EXCLUDED_OPTIONS)

    return {
        "enabled": enabled,
        "url": _normalize_systemone_url(str(raw_url)),
        "model": str(raw_model).strip() or DEFAULT_DECIDER_MODEL,
        "fallback_url": _normalize_systemone_url(str(raw_fallback_url)) if raw_fallback_url else "",
        "fallback_model": str(raw_fallback_model).strip() or DEFAULT_FALLBACK_MODEL,
        "allow_fallback": allow_fallback,
        "timeout": max(1.0, min(timeout, 120.0)),
        "specialist_threshold": max(0.0, min(specialist_threshold, 1.0)),
        "confidence_threshold": max(0.0, min(confidence_threshold, 1.0)),
        "excluded_options": excluded,
    }


def decider_enabled(settings: Mapping[str, Any] | None) -> bool:
    """Whether automatic System-1 Decider routing is enabled for ``model_map``."""
    return bool(decider_config(settings)["enabled"])


def _short_route_desc(desc: str, max_chars: int = 85) -> str:
    """Keep the high-signal opening of a route description so 7+ routes stay ~120 tokens total."""
    cleaned = " ".join(str(desc or "").split())
    if len(cleaned) <= max_chars:
        return cleaned
    cut = cleaned[:max_chars].rsplit(" ", 1)[0]
    return f"{cut}..."


def eligible_routes(
    options: OrderedDict[str, dict[str, Any]],
    settings: Mapping[str, Any] | None,
) -> OrderedDict[str, str]:
    """Return ``{route_name: concise_description}`` for routes the Decider may auto-select."""
    cfg = decider_config(settings)
    excluded_set = set(cfg["excluded_options"])
    out: OrderedDict[str, str] = OrderedDict()
    for name, entry in options.items():
        if name in excluded_set:
            continue
        desc = _short_route_desc(str(entry.get("description") or ""), max_chars=85)
        if desc:
            out[name] = desc
    return out


def _extract_answers_map(resp: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Support both ``/v1/systemone`` (``answers``) and ``/v1/score`` (``decisions``) envelopes."""
    if not isinstance(resp, Mapping):
        return {}
    ans = resp.get("answers")
    if isinstance(ans, Mapping):
        return ans
    dec = resp.get("decisions")
    if isinstance(dec, Mapping):
        return dec
    return {}


def _extract_noul_prob(entry: Mapping[str, Any] | None) -> float:
    if not isinstance(entry, Mapping):
        return 0.0
    for key in ("noul", "yes_probability", "probability"):
        val = entry.get(key)
        if isinstance(val, (int, float)):
            return float(val)
    return 0.0


def _post_systemone_once(
    *,
    url: str,
    model: str,
    state: dict[str, Any],
    questions: Mapping[str, Any],
    timeout: float,
    api_key: str = "",
) -> dict[str, Any]:
    use_ollama = _is_ollama_endpoint(url)
    serialized_state: Any = (
        state if use_ollama else json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    )
    body: dict[str, Any] = {
        "model": model,
        "state": serialized_state,
        "questions": _adapt_questions_for_endpoint(questions, url),
    }
    if use_ollama:
        body["keep_alive"] = -1

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "hermes-advanced-delegation/1.2",
    }
    if api_key and not _is_ollama_endpoint(url):
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


def _call_systemone(
    state: dict[str, Any],
    questions: Mapping[str, Any],
    settings: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Call primary ``/v1/systemone`` endpoint with automatic TypeSafe Cloud failover & cooldown."""
    global _LAN_COOLDOWN_UNTIL
    cfg = decider_config(settings)
    primary_url = cfg["url"]
    fb_url = cfg["fallback_url"]
    api_key = _typesafe_api_key()
    can_fallback = (
        cfg["allow_fallback"]
        and bool(fb_url)
        and fb_url != primary_url
        and bool(api_key)
    )

    t0 = time.perf_counter()
    if can_fallback and _is_ollama_endpoint(primary_url) and time.monotonic() < _LAN_COOLDOWN_UNTIL:
        data = _post_systemone_once(
            url=fb_url,
            model=cfg["fallback_model"],
            state=state,
            questions=questions,
            timeout=cfg["timeout"],
            api_key=api_key,
        )
        data["fallback_used"] = True
        data["latency_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
        return data

    try:
        data = _post_systemone_once(
            url=primary_url,
            model=cfg["model"],
            state=state,
            questions=questions,
            timeout=cfg["timeout"],
            api_key=api_key,
        )
        data["fallback_used"] = False
        data["latency_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
        return data
    except Exception as primary_err:
        if _is_ollama_endpoint(primary_url):
            _LAN_COOLDOWN_UNTIL = time.monotonic() + _LAN_COOLDOWN_SECONDS
        if not can_fallback:
            raise
        logger.warning(
            "advanced-delegation: primary Decider %s failed (%s); falling back to %s",
            primary_url,
            primary_err,
            fb_url,
        )
        t_fb = time.perf_counter()
        data = _post_systemone_once(
            url=fb_url,
            model=cfg["fallback_model"],
            state=state,
            questions=questions,
            timeout=cfg["timeout"],
            api_key=api_key,
        )
        data["fallback_used"] = True
        data["latency_ms"] = round((time.perf_counter() - t_fb) * 1000.0, 1)
        return data


def _decision_log_path() -> Path | None:
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home() / "cache" / "advanced-delegation" / "decisions.jsonl"
    except Exception:
        return None


def _record_decision(entry: dict[str, Any]) -> None:
    path = _decision_log_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: list[str] = []
        if path.is_file():
            existing = [
                ln for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()
            ]
        existing.append(json.dumps(entry, ensure_ascii=False))
        if len(existing) > _MAX_LOG_ENTRIES:
            existing = existing[-_MAX_LOG_ENTRIES:]
        path.write_text("\n".join(existing) + "\n", encoding="utf-8")
    except Exception:
        pass


def read_decision_log(limit: int = 30) -> list[dict[str, Any]]:
    """Return the most recent Decider routing decisions (newest first)."""
    path = _decision_log_path()
    if path is None or not path.is_file():
        return []
    try:
        lines = [
            ln for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()
        ]
        items: list[dict[str, Any]] = []
        for ln in reversed(lines[-max(1, min(int(limit), _MAX_LOG_ENTRIES)):]):
            try:
                row = json.loads(ln)
                if isinstance(row, dict):
                    items.append(row)
            except Exception:
                continue
        return items
    except Exception:
        return []


def clear_decision_log() -> int:
    """Clear the cached Decider routing log and return the number of removed entries."""
    path = _decision_log_path()
    if path is None or not path.is_file():
        return 0
    try:
        lines = [
            ln for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()
        ]
        path.unlink(missing_ok=True)
        return len(lines)
    except Exception:
        return 0


def route_unassigned_tasks(
    tasks: list[dict[str, Any]],
    settings: Mapping[str, Any] | None,
) -> dict[int, str]:
    """Mutate ``tasks`` in-place to populate ``option`` for tasks that omitted it.

    Returns ``{task_index: chosen_option}`` for every task whose ``option`` was populated.
    """
    from .model_map import configured_options

    if not tasks or not decider_enabled(settings):
        return {}

    options = configured_options(settings)
    if len(options) <= 1:
        return {}

    eligible = eligible_routes(options, settings)
    if len(eligible) <= 1:
        return {}

    raw_default = settings.get("default") if isinstance(settings, Mapping) else None
    default_opt = (
        " ".join(raw_default.split())
        if isinstance(raw_default, str) and " ".join(raw_default.split()) in options
        else ""
    )

    unassigned: list[tuple[int, dict[str, Any]]] = []
    for idx, task in enumerate(tasks):
        if not isinstance(task, dict):
            continue
        opt = task.get("option")
        if isinstance(opt, str) and opt.strip():
            continue
        goal = str(task.get("goal") or "").strip()
        if not goal:
            continue
        unassigned.append((idx, task))

    if not unassigned:
        return {}

    cfg = decider_config(settings)
    # TTFT optimization: put full route descriptions once in `state.available_routes`.
    # When batching >1 tasks, use short labels in per-question criteria so descriptions
    # aren't repeated N times in the rendered prompt.
    compact_criteria = (
        {name: f"Route {name}" for name in eligible}
        if len(unassigned) > 1
        else dict(eligible)
    )

    state_tasks: dict[str, Any] = {}
    questions: dict[str, Any] = {}
    for idx, task in unassigned:
        key = f"t{idx}"
        images = task.get("images")
        has_images = bool(isinstance(images, list) and len(images) > 0)
        state_tasks[key] = {
            "goal": _head_tail(str(task.get("goal") or ""), head=320, tail=160),
            "context": _head_tail(str(task.get("context") or ""), head=200, tail=100),
            "has_images": has_images,
        }
        questions[f"{key}_needs_specialist"] = {
            "type": "noul",
            "instructions": (
                f"Does task `{key}` require a specialist route (visual/image analysis, complex "
                f"multi-file architecture, adversarial verification, offensive security, or deep "
                f"multi-source synthesis) instead of `{default_opt or 'default'}`?"
            ),
            "criteria": {
                "true": (
                    "Task has images (`has_images: true`), offensive security, complex architecture, "
                    "adversarial review, or deep synthesis needing a specialist route."
                ),
                "false": (
                    f"Standard coding, debugging, test writing, research, or tool-heavy work handled "
                    f"well by `{default_opt or 'default'}`."
                ),
            },
        }
        questions[f"{key}_route"] = {
            "type": "choice",
            "instructions": f"Select the best route from `available_routes` for task `{key}`.",
            "criteria": compact_criteria,
        }

    state: dict[str, Any] = {
        "default_route": default_opt or "inherit",
        "tasks": state_tasks,
    }
    if len(unassigned) > 1:
        state["available_routes"] = dict(eligible)

    try:
        resp = _call_systemone(state, questions, settings)
    except Exception as exc:
        logger.warning(
            "advanced-delegation: Decider unreachable (%s); keeping default route %r",
            exc,
            default_opt,
        )
        return {}

    decisions = _extract_answers_map(resp)
    if not decisions:
        return {}

    spec_thresh = cfg["specialist_threshold"]
    conf_thresh = cfg["confidence_threshold"]
    latency_ms = resp.get("latency_ms", 0.0)
    used_model = str(resp.get("model") or cfg["model"])
    fallback_used = bool(resp.get("fallback_used", False))

    assigned: dict[int, str] = {}
    for idx, task in unassigned:
        key = f"t{idx}"
        needs_d = decisions.get(f"{key}_needs_specialist") or {}
        route_d = decisions.get(f"{key}_route") or {}

        needs_p = _extract_noul_prob(needs_d if isinstance(needs_d, Mapping) else None)
        picked = str((route_d if isinstance(route_d, Mapping) else {}).get("choice") or "").strip()
        conf = float((route_d if isinstance(route_d, Mapping) else {}).get("confidence", 0.0) or 0.0)
        has_images = bool(state_tasks[key]["has_images"])

        if (
            picked in eligible
            and picked != default_opt
            and (
                (needs_p >= spec_thresh and conf >= conf_thresh)
                or (has_images and "visual" in picked.lower() and conf >= 0.40)
            )
        ):
            task["option"] = picked
            assigned[idx] = picked
            action = "upgraded"
            effective_route = picked
            logger.info(
                "advanced-delegation: Decider upgraded task[%d] -> %s (needs_specialist=%.2f, confidence=%.2f, %.0fms)",
                idx,
                picked,
                needs_p,
                conf,
                latency_ms,
            )
        else:
            task["_decider_evaluated"] = True
            if default_opt and default_opt in options:
                task["option"] = default_opt
                assigned[idx] = default_opt
            action = "kept_default"
            effective_route = default_opt or "inherit"
            logger.info(
                "advanced-delegation: Decider kept task[%d] on %s (picked=%s, needs_specialist=%.2f, confidence=%.2f, %.0fms)",
                idx,
                effective_route,
                picked or "-",
                needs_p,
                conf,
                latency_ms,
            )

        _record_decision(
            {
                "timestamp": round(time.time(), 3),
                "goal": state_tasks[key]["goal"][:140],
                "has_images": has_images,
                "action": action,
                "route": effective_route,
                "candidate": picked or default_opt or "inherit",
                "needs_specialist": round(needs_p, 4),
                "confidence": round(conf, 4),
                "latency_ms": latency_ms,
                "model": used_model,
                "fallback_used": fallback_used,
            }
        )

    return assigned


def route_single_task(
    task: dict[str, Any],
    settings: Mapping[str, Any] | None,
) -> str | None:
    """Route a single unassigned task payload and return the chosen option (or None)."""
    if task.get("_decider_evaluated"):
        opt = task.get("option")
        return opt if isinstance(opt, str) and opt.strip() else None
    probe = dict(task)
    route_unassigned_tasks([probe], settings)
    opt = probe.get("option")
    return opt.strip() if isinstance(opt, str) and opt.strip() else None


def probe_decider(
    goal: str,
    *,
    context: str = "",
    has_images: bool = False,
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a live diagnostic probe against the configured System-1 Decider and return full details."""
    from .model_map import configured_options

    if settings is None:
        from . import store
        section = store.delegation_section()
        backends = section.get("backends") if isinstance(section, Mapping) else None
        settings = backends.get("model_map") if isinstance(backends, Mapping) else {}

    cfg = decider_config(settings)
    options = configured_options(settings if isinstance(settings, Mapping) else {})
    eligible = eligible_routes(options, settings)
    raw_default = settings.get("default") if isinstance(settings, Mapping) else None
    default_opt = (
        " ".join(raw_default.split())
        if isinstance(raw_default, str) and " ".join(raw_default.split()) in options
        else ""
    )

    if not eligible:
        return {
            "ok": False,
            "error": "No eligible model routes configured in delegation.backends.model_map.options.",
            "config": cfg,
        }

    questions = {
        "t0_needs_specialist": {
            "type": "noul",
            "instructions": (
                f"Does task `t0` require a specialist route (visual/image analysis, complex "
                f"multi-file architecture, adversarial verification, offensive security, or deep "
                f"multi-source synthesis) instead of `{default_opt or 'default'}`?"
            ),
            "criteria": {
                "true": (
                    "Task has images (`has_images: true`), offensive security, complex architecture, "
                    "adversarial review, or deep synthesis needing a specialist route."
                ),
                "false": (
                    f"Standard coding, debugging, test writing, research, or tool-heavy work handled "
                    f"well by `{default_opt or 'default'}`."
                ),
            },
        },
        "t0_route": {
            "type": "choice",
            "instructions": "Select the best route for task `t0`.",
            "criteria": dict(eligible),
        },
    }
    state = {
        "default_route": default_opt or "inherit",
        "tasks": {
            "t0": {
                "goal": _head_tail(goal, head=320, tail=160),
                "context": _head_tail(context, head=200, tail=100),
                "has_images": bool(has_images),
            }
        },
    }

    try:
        resp = _call_systemone(state, questions, settings)
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "config": cfg,
        }

    decisions = _extract_answers_map(resp)
    needs_d = decisions.get("t0_needs_specialist") if isinstance(decisions, Mapping) else {}
    route_d = decisions.get("t0_route") if isinstance(decisions, Mapping) else {}
    needs_p = _extract_noul_prob(needs_d if isinstance(needs_d, Mapping) else None)
    picked = str((route_d or {}).get("choice") or "").strip()
    conf = float((route_d or {}).get("confidence", 0.0) or 0.0)
    scores = (
        (route_d or {}).get("probabilities") or (route_d or {}).get("scores")
        if isinstance(route_d, Mapping)
        else {}
    )

    upgraded = bool(
        picked in eligible
        and picked != default_opt
        and (
            (needs_p >= cfg["specialist_threshold"] and conf >= cfg["confidence_threshold"])
            or (has_images and "visual" in picked.lower() and conf >= 0.40)
        )
    )
    effective = picked if upgraded else (default_opt or picked or "inherit")

    return {
        "ok": True,
        "goal": goal,
        "has_images": bool(has_images),
        "upgraded": upgraded,
        "effective_route": effective,
        "candidate_route": picked,
        "default_route": default_opt or "inherit",
        "needs_specialist": round(needs_p, 4),
        "confidence": round(conf, 4),
        "scores": scores if isinstance(scores, Mapping) else {},
        "latency_ms": resp.get("latency_ms", 0.0),
        "model": resp.get("model", cfg["model"]),
        "fallback_used": bool(resp.get("fallback_used", False)),
        "config": cfg,
    }
