"""Subagent model cost-cap: OpenRouter pricing + tool-support lookup, cached 24h.

Feeds the ``model_map`` dispatch guard: a non-specialist route whose model costs more
than ``CAP_INPUT_USD_PER_M`` per million input tokens, or that lacks tool support, is
refused before a subagent is ever spawned on it. Prices come from the OpenRouter
``/models`` endpoint (input $/M = ``pricing.prompt * 1e6``) and are cached on disk with a
24h TTL so dispatch adds no network call after the first warm.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional
import urllib.request

logger = logging.getLogger(__name__)

CAP_INPUT_USD_PER_M = 0.75  # $ per million input tokens (Stephen's standing cap)
CACHE_TTL_S = 24 * 3600
_MODELS_ENDPOINT = "https://openrouter.ai/api/v1/models"

#: In-memory memo so route_task (which runs per dispatch) does not re-read the file.
_mem: dict[str, Any] = {"ts": 0.0, "models": {}}


def _cache_path() -> Path:
    base = os.environ.get("HERMES_CACHE_DIR")
    if base:
        return Path(base) / "delegation-model-pricing.json"
    return Path.home() / ".hermes" / "cache" / "delegation-model-pricing.json"


def _openrouter_key() -> Optional[str]:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    for path in (Path.home() / ".hermes" / ".env", Path.home() / ".env"):
        try:
            for line in open(path, encoding="utf-8"):
                if line.startswith("OPENROUTER_API_KEY"):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            pass
    return None


def _fetch() -> dict[str, dict[str, Any]]:
    key = _openrouter_key()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not found in env or ~/.hermes/.env")
    req = urllib.request.Request(_MODELS_ENDPOINT, headers={"Authorization": "Bearer " + key})
    data = json.load(urllib.request.urlopen(req, timeout=30))
    models: dict[str, dict[str, Any]] = {}
    for m in data.get("data", []):
        models[m["id"]] = {
            "input_per_m": float((m.get("pricing") or {}).get("prompt", 0.0)) * 1e6,
            "tools": "tools" in (m.get("supported_parameters") or []),
        }
    return models


def _load_disk() -> tuple[dict[str, dict[str, Any]], float]:
    try:
        raw = json.load(open(_cache_path(), encoding="utf-8"))
        return raw.get("models", {}), float(raw.get("ts", 0.0))
    except (OSError, ValueError, TypeError):
        return {}, 0.0


def _save_disk(models: dict[str, dict[str, Any]]) -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        json.dump({"models": models, "ts": time.time()}, open(path, "w", encoding="utf-8"))
    except OSError:
        pass


def _models() -> dict[str, dict[str, Any]]:
    if time.time() - _mem["ts"] < CACHE_TTL_S and _mem["models"]:
        return _mem["models"]
    disk_models, disk_ts = _load_disk()
    if time.time() - disk_ts < CACHE_TTL_S and disk_models:
        _mem["models"] = disk_models
        _mem["ts"] = disk_ts
        return disk_models
    try:
        fresh = _fetch()
        _mem["models"] = fresh
        _mem["ts"] = time.time()
        _save_disk(fresh)
        return fresh
    except Exception as exc:  # network / key / parse — fall back to stale
        logger.warning("delegation cost-cap: pricing fetch failed (%s); using stale cache if any", exc)
        _mem["models"] = disk_models
        _mem["ts"] = disk_ts
        return disk_models


def model_pricing(model: str) -> Optional[dict[str, Any]]:
    """``{input_per_m, tools}`` for a model id, or ``None`` when it cannot be resolved."""
    return _models().get(model)


def verdict(model: str) -> tuple[Optional[float], Optional[bool]]:
    """``(input_usd_per_m, has_tools)`` or ``(None, None)`` when unresolvable."""
    info = model_pricing(model)
    if info is None:
        return None, None
    return info["input_per_m"], info["tools"]
