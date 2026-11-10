"""Authenticated backend endpoint used by the desktop composer plugin."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()
PLUGIN_ROOT = Path(__file__).resolve().parent.parent


class ProofreadRequest(BaseModel):
    text: str


def _plugin_core() -> Any:
    expected_file = (PLUGIN_ROOT / "__init__.py").resolve()
    for module in tuple(sys.modules.values()):
        module_file = getattr(module, "__file__", None)
        if not module_file:
            continue
        try:
            is_plugin_module = Path(module_file).resolve() == expected_file
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        if is_plugin_module and callable(getattr(module, "proofread_text", None)):
            return module
    raise HTTPException(
        status_code=503,
        detail="The Grammarly plugin backend is not initialized.",
    )


@router.post("/proofread")
def proofread(payload: ProofreadRequest) -> dict[str, str]:
    try:
        corrected = _plugin_core().proofread_text(payload.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Proofreading failed: {type(exc).__name__}: {exc}",
        ) from exc
    return {"text": corrected}
