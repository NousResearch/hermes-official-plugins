"""Authenticated dashboard routes for one-call push pairing (/pair)."""
from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel


def _load_push_module(suffix: str, filename: str):
    """Load a module from ../push/ by path (core imports plugin_api.py standalone)."""
    mod_name = f"{__name__}_{suffix}"
    path = Path(__file__).resolve().parents[1] / "push" / filename
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


_pairing = _load_push_module("pairing", "pairing.py")
_privacy = _load_push_module("privacy", "privacy.py")
_PROFILE_ID_FALLBACK_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

router = APIRouter()


def home(requested=None):
    """The Hermes home of the profile a request is for (same handling as device_api.py)."""
    try:
        from hermes_constants import (
            PROFILE_ID_RE,
            get_default_hermes_root,
            get_hermes_home,
            get_hermes_home_override,
            named_profile_is_live,
            profile_name_for_home,
        )
    except ImportError:
        base = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes")))
        if requested is None:
            return base
        root = base.parent.parent if base.parent.name == "profiles" else base
        if requested == "default":
            return root
        if not _PROFILE_ID_FALLBACK_RE.match(requested):
            raise HTTPException(400, f"Invalid profile name '{requested}'.")
        target = root / "profiles" / requested
        if not target.is_dir():
            raise HTTPException(404, f"Profile '{requested}' does not exist.")
        return target

    bound = get_hermes_home_override()
    if bound and profile_name_for_home(bound):
        return Path(bound)
    if requested is None:
        return get_hermes_home()
    root = get_default_hermes_root()
    if requested == "default":
        return root
    if not PROFILE_ID_RE.match(requested):
        raise HTTPException(400, f"Invalid profile name '{requested}'.")
    if not named_profile_is_live(root / "profiles" / requested):
        raise HTTPException(404, f"Profile '{requested}' does not exist.")
    return root / "profiles" / requested


class PairRequest(BaseModel):
    relay_url: str
    install_key: str | None = None


@router.get("/pair")
def get_pair(profile: str | None = None):
    return _pairing.pairing_status(home=home(profile))


@router.post("/pair")
def post_pair(body: PairRequest, profile: str | None = None):
    target_home = home(profile)
    try:
        return _pairing.pair_install(
            body.relay_url,
            body.install_key,
            home=target_home,
            load_preview_key=_privacy.load_key,
        )
    except _pairing.PairingInvalidError as error:
        raise HTTPException(400, str(error)) from None
    except _pairing.PairingConflictError as error:
        raise HTTPException(409, str(error)) from None
    except _pairing.PairingStorageError as error:
        raise HTTPException(503, str(error)) from None


@router.delete("/pair")
def delete_pair(profile: str | None = None):
    target_home = home(profile)
    try:
        return _pairing.unpair_install(home=target_home)
    except _pairing.PairingStorageError as error:
        raise HTTPException(503, str(error)) from None
