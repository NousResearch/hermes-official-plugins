"""Combined dashboard API for hermes-mobile (push silent sessions + paired device access)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from fastapi import APIRouter


def _load_sibling(suffix: str, filename: str):
    """Load a sibling dashboard module by path (core imports plugin_api.py standalone)."""
    mod_name = f"{__name__}_{suffix}"
    path = Path(__file__).resolve().with_name(filename)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {filename} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(mod_name, None)
        raise
    return module


push_api = _load_sibling("push", "push_api.py")
device_api = _load_sibling("device", "device_api.py")

router = APIRouter()
router.routes.extend(push_api.router.routes)
router.routes.extend(device_api.router.routes)
