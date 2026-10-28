"""Preset transport. Mounted/authenticated/profile-scoped by Hermes' plugin API loader.

The dashboard loader imports API entries as standalone modules (not plugin packages).
Load only our own adjacent store file, without mutating sys.path or loading another
profile's plugin instance. All validation and preset semantics remain in that store.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict


def _load_store():
    path = Path(__file__).resolve().parent.parent / "store.py"
    spec = importlib.util.spec_from_file_location(f"{__name__}_store", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load preset store: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


store = _load_store()
router = APIRouter()


class PresetAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["save", "use", "rename", "delete"]
    name: str
    new_name: str = ""
    overwrite: bool = False


def _state():
    section = store.delegation_section()
    # Names only: snapshots may contain credentials, including nested fallbacks
    # and request headers. They never cross this transport, even as previews.
    return {"names": store.list_presets(section), "active": store.active_preset(section)}


@router.get("/presets")
def presets():
    try:
        return _state()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/presets")
def change_preset(body: PresetAction):
    # Match core settings' write serialization; the host middleware has already
    # bound the authenticated request's profile. No session ID is accepted here.
    from hermes_cli.web_routers._common import config_write_scope
    with config_write_scope(None):
        try:
            actions = {
                "save": lambda: store.save_preset(body.name, overwrite=body.overwrite),
                "use": lambda: store.use_preset(body.name),
                "rename": lambda: store.rename_preset(body.name, body.new_name),
                "delete": lambda: store.delete_preset(body.name),
            }
            actions[body.action]()
            return _state()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, " ".join(str(exc).split())) from exc
