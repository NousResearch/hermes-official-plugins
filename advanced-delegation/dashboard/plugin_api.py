"""Host-authenticated backend API for the Advanced Delegation & System-1 Decider Desktop UI.

Mounted by the Hermes web server at ``/api/plugins/advanced-delegation/`` when ``advanced-delegation``
is enabled. Loads its sibling package by path without mutating ``sys.path`` or relying on ``cwd``.

Routes:
    GET    /state       Current delegation mode, routes, profiles, Decider config, and recent decisions
    POST   /mode        Switch delegation.backend (default | model_map | profile_routes)
    PUT    /decider     Validate and save the Decider settings (plugins.entries.advanced-delegation.settings.decider)
    POST   /probe       Run a live /v1/systemone routing probe for a sample subagent goal
    DELETE /decisions   Clear cached System-1 Decider routing history
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException

_log = logging.getLogger(__name__)
router = APIRouter()

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = (
    "_advanced_delegation_dashboard_"
    + hashlib.sha256(str(_PLUGIN_ROOT).encode("utf-8")).hexdigest()[:16]
)


def _pkg():
    """Load the sibling ``advanced-delegation`` package by path without mutating ``sys.path``."""
    pkg = sys.modules.get(_PACKAGE_NAME)
    if pkg is not None:
        return pkg
    spec = importlib.util.spec_from_file_location(
        _PACKAGE_NAME,
        _PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(_PLUGIN_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("advanced-delegation package unavailable")
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[_PACKAGE_NAME] = pkg
    try:
        spec.loader.exec_module(pkg)
    except Exception:
        sys.modules.pop(_PACKAGE_NAME, None)
        raise
    return pkg


def _store():
    _pkg()
    return importlib.import_module(".store", _PACKAGE_NAME)


def _decider_router():
    _pkg()
    return importlib.import_module(".decider_router", _PACKAGE_NAME)


def _broadcast(event: str, payload: dict[str, Any]) -> None:
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event("advanced-delegation", event, payload)
    except Exception:
        pass


def _build_state(decision_limit: int = 25) -> dict[str, Any]:
    store = _store()
    dr = _decider_router()
    section = store.delegation_section()
    mode = store.current_mode(section)
    options = store.routes(section)
    decider = store.decider_settings(section)
    excluded = set(decider.get("excluded_options") or [])
    route_items = []
    for opt in options:
        name = str(opt.get("name") or "").strip()
        route_items.append(
            {
                **opt,
                "summary": store.route_summary(opt),
                "decider_eligible": bool(name and name not in excluded),
            }
        )
    return {
        "mode": mode,
        "mode_label": store.mode_label(mode),
        "modes": [
            {"id": m, "label": label, "blurb": store.MODE_BLURBS.get(m, "")}
            for m, label in store.MODES.items()
        ],
        "routes": route_items,
        "route_default": store.route_default(section),
        "profiles": store.listed_profiles(section),
        "profile_default": store.profile_default(section),
        "decider": decider,
        "decisions": dr.read_decision_log(limit=decision_limit),
    }


@router.get("/state")
async def get_state(limit: int = 25):
    safe_limit = max(1, min(int(limit), 100))
    return await asyncio.to_thread(_build_state, safe_limit)


@router.post("/mode")
async def update_mode(payload: dict[str, Any] = Body(...)):
    mode = str((payload or {}).get("mode") or "").strip().lower()

    def _run():
        store = _store()
        if mode not in store.MODES:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown delegation mode {mode!r}; use one of: {', '.join(store.MODES)}.",
            )
        try:
            store.set_mode(mode)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        state = _build_state()
        _broadcast("updated", {"mode": mode})
        return state

    return await asyncio.to_thread(_run)


@router.put("/decider")
async def update_decider(payload: dict[str, Any] = Body(...)):
    def _run():
        store = _store()
        section = store.delegation_section()
        current = dict(store.decider_settings(section))
        for key in (
            "enabled",
            "url",
            "model",
            "fallback_url",
            "fallback_model",
            "allow_fallback",
            "timeout",
            "specialist_threshold",
            "confidence_threshold",
            "excluded_options",
        ):
            if key in payload:
                current[key] = payload[key]
        problems = store.validate_decider(current)
        if problems:
            raise HTTPException(status_code=400, detail=" ".join(problems))
        try:
            store.save_decider(current)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        state = _build_state()
        _broadcast("updated", {"decider": state["decider"]})
        return state

    return await asyncio.to_thread(_run)


@router.post("/probe")
async def run_probe(payload: dict[str, Any] = Body(...)):
    goal = str((payload or {}).get("goal") or "").strip()
    context = str((payload or {}).get("context") or "").strip()
    has_images = bool((payload or {}).get("has_images", False))
    if not goal:
        raise HTTPException(status_code=400, detail="A non-empty subagent goal is required to run a probe.")

    def _run():
        dr = _decider_router()
        return dr.probe_decider(goal, context=context, has_images=has_images)

    return await asyncio.to_thread(_run)


@router.delete("/decisions")
async def delete_decisions():
    def _run():
        dr = _decider_router()
        cleared = dr.clear_decision_log()
        _broadcast("updated", {"decisions_cleared": cleared})
        return {"ok": True, "cleared": cleared}

    return await asyncio.to_thread(_run)
