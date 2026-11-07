"""Dashboard backend routes for va-claims, mounted at /api/plugins/va-claims/.

GET  /summary?icn=            -> full benefits picture for one veteran
GET  /people                  -> everyone with a claim file
GET  /guide?topic=            -> guide text (federal standards)
POST /benefit                 -> {icn, program, fields...} upsert a program record
POST /deadline                -> {icn, label, due, program, note}
GET  /lookup?q=               -> contention search (proxies VA reference API)
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

_ROOT = Path(__file__).resolve().parents[1]


def _mod(name: str):
    key = f"va_claims_{name}"
    mod = sys.modules.get(key)
    if mod is None:
        spec = importlib.util.spec_from_file_location(key, _ROOT / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
    return mod


store = _mod("store")
router = APIRouter()


@router.get("/health")
def health():
    return {"ok": True, "people": len(store.list_people())}


@router.get("/people")
def people():
    return {"people": store.list_people()}


@router.get("/summary")
def summary(icn: str = Query("")):
    return store.summary(icn or None)


@router.get("/guide")
def guide(topic: str = Query("")):
    guide_mod = _mod("guide")
    return guide_mod.guide_dict(topic)


@router.post("/benefit")
def benefit(payload: dict):
    try:
        data = store.set_benefit(payload.get("icn"), payload.get("program", ""), payload.get("fields") or {})
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    store.save(payload.get("icn"), data)
    return {"ok": True, "benefits": data["benefits"]}


@router.post("/deadline")
def deadline(payload: dict):
    if not payload.get("label") or not payload.get("due"):
        raise HTTPException(status_code=422, detail="label and due are required")
    data = store.add_deadline(payload.get("icn"), payload["label"], payload["due"], payload.get("program", "disability"), payload.get("note", ""))
    store.save(payload.get("icn"), data)
    return {"ok": True, "deadlines": data["deadlines"]}
