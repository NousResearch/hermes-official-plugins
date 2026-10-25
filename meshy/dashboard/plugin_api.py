"""Meshy plugin backend: what the Desktop half needs from the machine Hermes runs on.

Mounted at ``/api/plugins/meshy/`` once ``meshy`` is in ``plugins.enabled``. The Desktop renderer
cannot read files or call Meshy itself, so this router does both, scoped to the requesting profile
(the host sets ``HERMES_HOME`` per request; nothing here binds a path or a key at import).

Routes:
    GET /status             key configured? + balance (no key → configured: false, never an error)
                            + the user's Settings ▸ Plugins ▸ Meshy preferences
    GET /models?limit=      recent finished Meshy generations: task id, type, prompt, thumbnail, credits
    GET /local?limit=       saved models (GLB, glTF, FBX, OBJ, STL, USDZ, 3MF, BLEND) under the plugin's meshy_output/
                            (the server's default download dir) plus models in the asset index (project folders),
                            each with its Meshy task id when known and whether the viewer can draw it
    GET /file?path=&task=   bytes of one model (.glb/.gltf/.fbx/.obj/.stl/.usdz/.3mf), an OBJ's .mtl, or an image
                            (data URL), refused outside allowed roots; a served model is recorded in the
                            profile's asset index (path, size, sha256, task)
    POST /upload            a model file sent from the Desktop's own computer (multipart ``file``, or JSON
                            ``{filename, data: base64}`` from older Desktops): stored under the plugin's
                            uploads/ so a REMOTE backend can show a file it cannot open by path
    GET /formats?task=      the file types a finished task offers (glb, fbx, obj, stl, usdz ...) + any already saved
    POST /save              {task, format, beside?}: download one format next to ``beside`` (no credits)
    GET /task?task=         one task's status + progress % + thumbnail, for a card given only a task id: it shows
                            progress while Meshy works and the model once it is done (the agent need not wait)
    GET /active?tasks=      progress % of the named in-flight tasks (``kind:id,...``; progress chip): one Meshy
                            read per task, none when the chip watches nothing
    GET /actions?task=      the follow-up actions a finished task supports (texture, remesh, rig ...), each with
                            its credit cost and the chat message a card button sends; plus the live balance
    POST /provenance        {task_id, tool, args, session_id?}: remember what made a task (Desktop records it
                            from the tool call that created it): prompt, style, source image, settings, parent
    GET /details?task=      "Made from" for the gallery: prompt(s), source image, settings, credits, the chain of
                            tasks it came from and what was made from it, and the chat that made it
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool

_log = logging.getLogger(__name__)
router = APIRouter()

_API = "https://api.meshy.ai"
_KEY_ENV = "MESHY_API_KEY"
# A Meshy GLB with 2K textures is ~30 MB; the cap leaves room for 4K/8K while bounding memory.
_MAX_FILE_BYTES = 128 * 1024 * 1024
# Every format Meshy exports that three.js can draw (the viewer picks the loader by extension).
_MODEL_MIME = {".glb": "model/gltf-binary", ".gltf": "model/gltf+json", ".fbx": "application/octet-stream",
               ".obj": "model/obj", ".stl": "model/stl", ".usdz": "model/vnd.usdz+zip", ".3mf": "model/3mf"}
# What /file serves: models, an OBJ's material file, and images (thumbnails, saved image sets, OBJ textures).
_MIME = {**_MODEL_MIME, ".mtl": "text/plain", ".png": "image/png",
         ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
# Where a signed asset link (and every redirect hop of it) may point.
_ASSET_HOSTS = frozenset({"assets.meshy.ai"})
_ASSET_SCHEMES = frozenset({"https"})
# Every route the plugin's tools create tasks on (the Meshy MCP server's TASK_ENDPOINTS): GET <route>/<id>
# reads one task, GET <route> lists them. Creative Lab routes are per product AND stage.
_CREATIVE_LAB = ("figure", "lamp", "keychain", "fridge-magnet", "vinyl-figure", "brick-figure", "keycap", "fidget-pixel")
_TASK_ROUTES = {
    "text-to-3d": "/openapi/v2/text-to-3d", "image-to-3d": "/openapi/v1/image-to-3d",
    "multi-image-to-3d": "/openapi/v1/multi-image-to-3d", "remesh": "/openapi/v1/remesh",
    "retexture": "/openapi/v1/retexture", "rigging": "/openapi/v1/rigging", "animation": "/openapi/v1/animations",
    "text-to-image": "/openapi/v1/text-to-image", "image-to-image": "/openapi/v1/image-to-image",
    "convert": "/openapi/v1/convert", "resize": "/openapi/v1/resize", "uv-unwrap": "/openapi/v1/uv-unwrap",
    "print-repair": "/openapi/v1/print/repair", "print-split": "/openapi/v1/print/split",
    "print-multi-color": "/openapi/v1/print/multi-color", "print-analyze": "/openapi/v1/print/analyze",
    "text-to-motion": "/openapi/v1/text-to-motion",
    **{f"creative-lab-{p}-{stage}": f"/openapi/creative-lab/{p}/v1/{stage}"
       for p in _CREATIVE_LAB for stage in ("prototype", "build")},
    "creative-lab-fidget-collapsible": "/openapi/creative-lab/fidget-collapsible/v1",
}
# A record's ``type`` uses Meshy's API names; these differ from the route names above.
_TYPE_ENDPOINT = {"rig": "rigging", "animate": "animation", "multi-color-print": "print-multi-color",
                  "text-to-3d-preview": "text-to-3d", "text-to-3d-refine": "text-to-3d"}
# Types whose output is a 3D model, for the gallery (newest-first per type; merged and re-sorted below).
_MODEL_TYPES = ("text-to-3d", "image-to-3d", "multi-image-to-3d", "remesh", "retexture", "rigging", "animation",
                "convert", "resize", "uv-unwrap", "print-repair", "print-split", "print-multi-color",
                *(t for t in _TASK_ROUTES if t.startswith("creative-lab-") and not t.endswith("-prototype")))
_MODEL_ENDPOINTS = tuple((t, _TASK_ROUTES[t]) for t in _MODEL_TYPES)
_IMAGE_ENDPOINTS = tuple((t, _TASK_ROUTES[t]) for t in ("text-to-image", "image-to-image"))
# Meshy allows 20 requests/s per key: list and probe fan-outs share this many in-flight calls (_FANOUT_SLOTS),
# and the progress chip's per-task reads get as many slots of their own (_ACTIVE_SLOTS).
_FANOUT = 4


# Defaults mirror plugin.json ``extensions.com.nousresearch.hermes.config_schema``; Settings ▸ Plugins
# writes the user's choices to ``plugins.entries.meshy.settings``.
_SETTING_DEFAULTS = {"show_credits": True, "inline_viewer": True, "auto_rotate": True, "gallery_limit": 24}


def _settings() -> dict:
    from hermes_cli.config import load_config_readonly
    entry = ((load_config_readonly() or {}).get("plugins") or {}).get("entries", {}).get("meshy") or {}
    saved = entry.get("settings") if isinstance(entry.get("settings"), dict) else {}
    out = dict(_SETTING_DEFAULTS)
    for key, default in _SETTING_DEFAULTS.items():
        value = saved.get(key)
        # bool is an int subclass: match the default's exact type so True never passes as a count.
        if type(value) is type(default):
            out[key] = value
    out["gallery_limit"] = max(1, min(60, out["gallery_limit"]))
    return out


def _api_key() -> str | None:
    from agent.secret_scope import get_secret
    return get_secret(_KEY_ENV) or None


class _RedirectRefused(urllib.error.URLError):
    """A redirect to a host the request may not go to (another host, or plain http)."""


class _GuardedRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only where ``allowed`` says: urllib otherwise follows any hop to any host, http
    included, and re-sends the request's headers there (the API key for a Meshy API call)."""

    def __init__(self, allowed):
        super().__init__()
        self._allowed = allowed

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self._allowed(newurl):
            fp.close()
            raise _RedirectRefused("redirect off the allowed host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(req, timeout: float, allowed):
    """urlopen, but every redirect hop is re-validated with ``allowed`` (the one network seam)."""
    return urllib.request.build_opener(_GuardedRedirects(allowed)).open(req, timeout=timeout)


def _same_api_origin(url: str) -> bool:
    api, hop = urllib.parse.urlsplit(_API), urllib.parse.urlsplit(url)
    return (hop.scheme, hop.hostname, hop.port) == (api.scheme, api.hostname, api.port)


def _asset_allowed(url: object) -> bool:
    parts = urllib.parse.urlsplit(str(url or ""))
    return parts.scheme in _ASSET_SCHEMES and parts.hostname in _ASSET_HOSTS


def _meshy_get(path: str, key: str, timeout: float = 15.0):
    req = urllib.request.Request(_API + path, headers={
        "Authorization": f"Bearer {key}", "User-Agent": "hermes-meshy-plugin/0.3"})
    try:
        with _open(req, timeout, _same_api_origin) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except http.client.HTTPException as exc:  # a short or malformed response: report it as unreachable
        raise urllib.error.URLError(exc) from exc


class _KeyRefused(HTTPException):
    """Meshy refused this key or account outright (401, or a 403 on the generic routes every plan reads): no task
    read can succeed until the user fixes it, unlike a refusal on one product route."""


_KEY_REJECTED = "Meshy rejected the API key"


def _meshy_error(exc: Exception) -> HTTPException:
    if isinstance(exc, urllib.error.HTTPError):
        # 401 is the key; 403 is an IP allowlist, org IP filtering or a plan-gated route, and Meshy's body says which.
        detail = {401: _KEY_REJECTED}.get(exc.code) or (
            f"Meshy refused the request: {_meshy_message(exc)}" if exc.code == 403 else f"Meshy API error {exc.code}")
        return HTTPException(status_code=502, detail=detail)
    return HTTPException(status_code=502, detail="Meshy API unreachable")


def _meshy_message(exc: urllib.error.HTTPError) -> str:
    """Meshy's own one-line reason from an error body (``{"message": ...}``), else the status."""
    try:
        body = json.loads(exc.read(4096).decode("utf-8", "replace"))
    except (OSError, ValueError, AttributeError):
        body = None
    text = body.get("message") if isinstance(body, dict) else None
    return " ".join(str(text).split())[:200] if text else f"HTTP {exc.code}"


def _disk_error(exc: OSError) -> HTTPException:
    return HTTPException(status_code=500, detail=f"could not write the file ({exc.strerror or type(exc).__name__})")


_FANOUT_SLOTS = threading.BoundedSemaphore(_FANOUT)
# Separate from _FANOUT_SLOTS on purpose: an untyped chip read runs _task, whose probe takes _FANOUT_SLOTS, so a
# chip read holding one of those while waiting for another would deadlock every probe in the process.
_ACTIVE_SLOTS = threading.BoundedSemaphore(_FANOUT)


def _fan_out(fn, items) -> list:
    """Futures of ``fn(item)`` per item, in order; at most _FANOUT of them run at once across all requests."""
    def run(item):
        with _FANOUT_SLOTS:
            return fn(item)
    with ThreadPoolExecutor(max_workers=_FANOUT) as pool:
        return [pool.submit(run, item) for item in items]


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


# The asset index and provenance are read-modify-written by concurrent requests in the one dashboard process.
_STATE_LOCK = threading.Lock()


def _write_json(target: Path, value: object) -> None:
    """Atomic replace through a per-writer temp file (callers hold _STATE_LOCK)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.part")
    try:
        tmp.write_text(json.dumps(value, indent=1), encoding="utf-8")
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)


def _asset_error(exc: Exception) -> HTTPException:
    """CDN requests do not carry the API key: a denied signed URL is not an auth failure."""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403, 404, 410):
            return HTTPException(status_code=410, detail="This asset is unavailable or its download link has expired. "
                                 "Open a locally saved copy if you have one.")
        return HTTPException(status_code=502, detail=f"Meshy asset download failed (HTTP {exc.code})")
    if isinstance(exc, _RedirectRefused):
        return HTTPException(status_code=502, detail="Meshy asset link redirected off assets.meshy.ai")
    return HTTPException(status_code=502, detail="Meshy asset download unreachable")


def _data_root() -> Path:
    """The plugin's data dir (the MCP server's cwd, so its default ``meshy_output/`` lives here)."""
    from hermes_constants import get_hermes_home
    base = get_hermes_home() / "plugin-data"
    found = sorted(base.glob("agent-plugin-meshy-*")) if base.is_dir() else []
    return found[0] if found else base / "agent-plugin-meshy"


def _allowed_roots() -> list[Path]:
    """Where /file may read: the plugin's output dir, the Hermes home's working dirs, the user's home.

    The viewer opens files the agent saved with ``save_to`` (usually inside a project), so the home
    directory is the practical root; secrets are excluded by extension, not by location.
    """
    roots = [_data_root(), Path.home()]
    return [r.resolve() for r in roots if r.exists()]


def _resolve_readable(raw: str, *, exts=_MIME, max_bytes: int | None = _MAX_FILE_BYTES) -> Path:
    if not raw or "\x00" in raw:
        raise HTTPException(status_code=400, detail="path required")
    target = Path(os.path.expanduser(raw))
    if not target.is_absolute():
        raise HTTPException(status_code=400, detail="path must be absolute")
    try:
        resolved = target.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(status_code=404, detail="file not found") from None
    if resolved.suffix.lower() not in exts:
        raise HTTPException(status_code=415, detail="only 3D model and image files")
    if not any(resolved.is_relative_to(root) for root in _allowed_roots()):
        raise HTTPException(status_code=403, detail="outside the allowed folders")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    if max_bytes is not None and resolved.stat().st_size > max_bytes:
        raise HTTPException(status_code=413, detail="file too large for the viewer")
    return resolved


@router.get("/status")
def status():
    key = _api_key()
    if not key:
        return {"configured": False, "key_env": _KEY_ENV, "settings": _settings()}
    try:
        bal = _meshy_get("/openapi/v1/balance", key)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # The status chip shows WHY there is no number (bad key vs refused vs offline) instead of a stale one. It
        # reads error_code: the detail quotes Meshy's own 403 text, which can say anything ("Request rejected: ...").
        code = getattr(exc, "code", None) if isinstance(exc, urllib.error.HTTPError) else None
        return {"configured": True, "settings": _settings(), "error": _meshy_error(exc).detail,
                "error_code": {401: "key_rejected", 403: "refused"}.get(code, "offline")}
    bal = bal if isinstance(bal, dict) else {}
    return {"configured": True, "settings": _settings(), "balance": bal.get("balance"),
            "monthly_credits": bal.get("monthly_credits"), "permanent_credits": bal.get("permanent_credits"),
            "monthly_resets_at": bal.get("monthly_resets_at")}


def _credits_by_task(key: str) -> dict[str, int]:
    try:
        rows = _meshy_get("/openapi/v1/usage/tasks?page_size=50", key)
    except (urllib.error.URLError, OSError, ValueError):
        return {}  # usage is a team/API-key view; a personal key may not have it
    return {r["task_id"]: int(r["consumed_credits"]) for r in (rows if isinstance(rows, list) else [])
            if isinstance(r, dict) and r.get("task_id") and isinstance(r.get("consumed_credits"), (int, float))}


def _gallery_lists(endpoints, limit: int, key: str) -> tuple[list[tuple[str, object]], list[dict]]:
    """Each product's newest tasks, plus ``failed`` (``{"task_type", "error"}``) for the products whose list read
    failed: a plan-gated product (403) or one route's outage never hides the others. Only a refusal of the key
    itself (401, or 403 on both generic routes every plan reads) fails the gallery, and so does every list failing."""
    lists = _fan_out(lambda e: _meshy_get(f"{e[1]}?page_size={limit}&sort_by=-created_at", key), endpoints)
    generic = {_TASK_ROUTES[t] for t in _PROBE_FIRST}
    listed: list[tuple[str, object]] = []
    failed: list[tuple[str, Exception]] = []
    generic_refusals = []
    for (task_type, path), future in zip(endpoints, lists):
        try:
            listed.append((task_type, future.result()))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise _meshy_error(exc) from exc
            if exc.code == 403 and path in generic:
                generic_refusals.append(exc)
            if exc.code not in (404, 405):  # a route this account does not have, or that does not list
                failed.append((task_type, exc))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            failed.append((task_type, exc))
    if len(generic_refusals) == len(generic):  # both refusing is the key or account (an IP allowlist), as in _task
        raise _meshy_error(generic_refusals[0]) from generic_refusals[0]
    if failed and not listed:
        raise _meshy_error(failed[0][1]) from failed[0][1]
    return listed, [{"task_type": t, "error": _meshy_error(exc).detail} for t, exc in failed]


@router.get("/models")
def models(limit: int | None = Query(None, ge=1, le=60)):
    limit = limit or _settings()["gallery_limit"]
    key = _api_key()
    if not key:
        return {"configured": False, "models": [], "failed": []}
    merged: list[dict] = []
    records: dict[str, tuple[str, dict]] = {}
    endpoints = (*_MODEL_ENDPOINTS, *_IMAGE_ENDPOINTS)
    listed, failed = _gallery_lists(endpoints, limit, key)
    for task_type, rows in listed:
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or row.get("status") != "SUCCEEDED":
                continue
            task_id = row.get("id")
            if not isinstance(task_id, str) or not task_id:
                continue
            # Some list endpoints overlap. Prefer the task's owning endpoint so a generic
            # representation cannot hide its images or animation clips.
            if task_id not in records or _TYPE_ENDPOINT.get(row.get("type"), row.get("type")) == task_type:
                records[task_id] = (task_type, row)
    for task_type, row in records.values():
        owner = _TYPE_ENDPOINT.get(row.get("type"), row.get("type"))
        label_type = owner if owner in {t for t, _ in (*_MODEL_ENDPOINTS, *_IMAGE_ENDPOINTS)} else task_type
        urls = _result_urls(row)
        # A rig's bind-pose character sits still; its walking clip shows what the rig is for.
        clips = [u for k, u in sorted(urls.items()) if k.startswith("glb:")]
        glb = (clips[-1] if clips and task_type == "rigging" else None) or urls.get("glb") or next(iter(clips), None)
        glb = glb or urls.get(_viewable_format(urls) or "")  # a Convert/print task may carry no GLB
        images = [u for u in (row.get("image_urls") or []) if isinstance(u, str) and u]
        expires = row.get("expires_at")
        merged.append({"task_id": row.get("id"), "task_type": label_type, "prompt": row.get("prompt") or _clip_label(urls),
                       "created_at": row.get("created_at"), "finished_at": row.get("finished_at"), "thumbnail_url": row.get("thumbnail_url"),
                       "glb_url": glb, "kind": "image" if task_type in {t for t, _ in _IMAGE_ENDPOINTS} else "model",
                       "image_urls": images, "expires_at": expires,
                       "expired": isinstance(expires, (int, float)) and 0 < expires <= time.time() * 1000,
                       "credits": row.get("consumed_credits")})
    merged.sort(key=lambda m: m.get("created_at") or 0, reverse=True)
    credits = _credits_by_task(key)
    prov = _load_prov()
    best: dict[str, tuple[tuple[bool, bool], str]] = {}
    for f in local(limit=200)["files"]:  # newest first
        if f.get("task") and f.get("kind", "model") == "model":
            # Several saved formats of one task: open the GLB (else one the viewer draws), not the last one saved.
            rank = (not f["path"].lower().endswith(".glb"), not f.get("viewable", True))
            if f["task"] not in best or rank < best[f["task"]][0]:
                best[f["task"]] = (rank, f["path"])
    saved = {task: path for task, (_rank, path) in best.items()}
    for m in merged:
        if m["credits"] is None:
            m["credits"] = credits.get(m["task_id"])
        m["local_path"] = saved.get(m["task_id"]) if m["kind"] == "model" else None
        m["prompt"] = _label_for(m["task_id"], prov) or m["prompt"]
    visible = [m for m in merged if not m["expired"] or m["local_path"]]
    return {"configured": True, "models": visible[:limit], "failed": failed}


# Profile-scoped index of models this plugin served or saved, so files the agent wrote into a project
# (``save_to``) reach the gallery without scanning the home folder. Paths, sizes and hashes only: never
# a provider URL or a key. Capped; vanished files drop out on read.
_INDEX_NAME = "asset-index.json"
_INDEX_CAP = 500
# Gallery models: every viewable format, plus BLEND (listed with a plain tile; three.js cannot draw it).
_INDEXED = (*_MODEL_MIME, ".blend")


def _index_path() -> Path:
    return _data_root() / _INDEX_NAME


def _load_index() -> list[dict]:
    try:
        rows = json.loads(_index_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("path"), str)] if isinstance(rows, list) else []


def _sha256(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _record_asset(path: Path, task_id: str | None = None, export: bool = False) -> None:
    """Upsert one file into the index. Only a resolved gallery model inside the allowed roots is recorded
    (the same gate /file applies), and a task id only when it is a well-formed Meshy id. ``export``: a file
    /save itself just wrote, in any format (the gallery lists models only). Only that call records the
    ``saved`` fingerprint /save trusts to replace a file; a read (/file, the gallery) never grants or refreshes
    it, so viewing a handmade file, or an edited export, never makes it replaceable."""
    try:
        resolved = path.resolve(strict=True)
        model = resolved.suffix.lower() in _INDEXED
        if not (model or export) or not resolved.is_file():
            return
        if not any(resolved.is_relative_to(root) for root in _allowed_roots()):
            return
        st = resolved.stat()
        task = task_id if isinstance(task_id, str) and _TASK_ID.fullmatch(task_id) else None
        now = int(time.time() * 1000)
        with _STATE_LOCK:  # concurrent /file reads each upsert: one read-modify-write at a time
            rows = _load_index()
            key = str(resolved)
            row = next((r for r in rows if r.get("path") == key), None)
            if row is None:
                row = {"path": key, "first_seen": now}
                rows.append(row)
            if row.get("bytes") != st.st_size or row.get("mtime") != int(st.st_mtime * 1000) or (model and not row.get("sha256")):
                row.update(bytes=st.st_size, mtime=int(st.st_mtime * 1000))
                if model:
                    row["sha256"] = _sha256(resolved)
            row.update(name=resolved.name, last_opened=now, task_id=task or row.get("task_id"))
            if export:
                row["saved"] = {**_fingerprint(st), "task_id": task}
            rows.sort(key=lambda r: r.get("last_opened") or 0, reverse=True)
            # Models and other exports are capped apart: a run of STL saves must not push gallery models out.
            is_model = [Path(r.get("path", "")).suffix.lower() in _INDEXED for r in rows]
            _write_json(_index_path(), [r for r, m in zip(rows, is_model) if m][:_INDEX_CAP]
                        + [r for r, m in zip(rows, is_model) if not m][:_INDEX_CAP])
    except OSError as exc:  # the index is a convenience; serving the file never fails on it
        _log.debug("meshy asset index: %s", exc)


@router.get("/local")
def local(limit: int = Query(50, ge=1, le=200)):
    """meshy_output/ downloads plus indexed models that still exist, deduped by resolved path."""
    out_dir = _data_root() / "meshy_output"
    seen: dict[str, dict] = {}
    for p in out_dir.rglob("*") if out_dir.is_dir() else ():
        if p.suffix.lower() not in _INDEXED:
            continue
        try:
            st = p.stat()
        except OSError:  # a dangling link, or deleted since the scan
            continue
        if not p.is_file():
            continue
        seen[str(p.resolve())] = {"path": str(p), "name": p.name, "project": p.parent.name, "bytes": st.st_size,
                                  "modified": int(st.st_mtime * 1000), "task": _local_task(p),
                                  "viewable": _viewable(p, st.st_size)}
    for item in _saved_images():
        seen[item["path"]] = item
    for row in _load_index():
        p = Path(row["path"])
        if p.suffix.lower() not in _INDEXED:
            continue  # an OBJ's .mtl/texture /save wrote: indexed for its task, not a gallery model
        if str(p) in seen or not p.is_file():
            if str(p) in seen and not seen[str(p)]["task"] and row.get("task_id"):
                seen[str(p)]["task"] = row["task_id"]
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        seen[str(p)] = {"path": str(p), "name": p.name, "project": p.parent.name, "bytes": st.st_size,
                        "modified": max(int(st.st_mtime * 1000), _as_int(row.get("last_opened"))),
                        "task": row.get("task_id") if isinstance(row.get("task_id"), str) else None,
                        "viewable": _viewable(p, st.st_size)}
    files = sorted(seen.values(), key=lambda f: f["modified"], reverse=True)[:limit]
    prov = _load_prov()
    for f in files:
        f["prompt"] = f.get("prompt") or (_label_for(f["task"], prov) if f["task"] else None) or _project_prompt(Path(f["path"]))
    return {"dir": str(out_dir), "files": files}


def _viewable(path: Path, size: int) -> bool:
    """The viewer can draw it: a format three.js loads, small enough for /file."""
    return path.suffix.lower() in _MODEL_MIME and size <= _MAX_FILE_BYTES


def _project_prompt(model: Path) -> str | None:
    try:
        name = json.loads((model.parent / "metadata.json").read_text(encoding="utf-8")).get("project_name")
    except (OSError, ValueError, AttributeError):
        return None
    return name if isinstance(name, str) and name not in ("text-to-3d", "image-to-3d", "") else None


def _label_for(task_id: str, prov: dict[str, dict]) -> str | None:
    """A one-line "what made this": its own prompt, else the nearest recorded ancestor's, else its image."""
    cur, hops = task_id, 0
    while cur and hops < 12:
        rec = prov.get(cur) or {}
        text = rec.get("prompt") or rec.get("text_style_prompt") or rec.get("texture_prompt")
        if text:
            return text
        if rec.get("image"):
            return f"from image {Path(urllib.parse.urlsplit(rec['image']).path).name}"
        cur, hops = rec.get("parent"), hops + 1
    return None


# /save writes the full task id here (older saves used an 8-character folder name).
_TASK_SIDECAR = ".meshy-task"


def _local_task(model: Path) -> str | None:
    """The Meshy task a downloaded file came from: the MCP server's project ``metadata.json`` (the task
    whose ``files`` list it, else the project's root task), or the sidecar /save leaves; else None."""
    found = None
    try:
        meta = json.loads((model.parent / "metadata.json").read_text(encoding="utf-8"))
        tasks = [t for t in meta.get("tasks") or [] if isinstance(t, dict)]
        found = next((t.get("task_id") for t in tasks if model.name in (t.get("files") or [])), None)
        found = found or meta.get("root_task_id")
    except (OSError, ValueError, AttributeError):
        try:
            found = (model.parent / _TASK_SIDECAR).read_text(encoding="utf-8").strip()
        except OSError:
            return None
    return found if isinstance(found, str) and _TASK_ID.fullmatch(found) else None


@router.get("/file")
def file(path: str, task: str | None = None):
    target = _resolve_readable(path)
    try:
        data = target.read_bytes()
    except OSError:  # removed or replaced since it was resolved
        raise HTTPException(status_code=404, detail="file not found") from None
    if target.suffix.lower() in _INDEXED:
        _record_asset(target, task)
    return {"name": target.name, "bytes": len(data), "mtime": int(target.stat().st_mtime * 1000),
            "dataUrl": f"data:{_MIME[target.suffix.lower()]};base64,{base64.b64encode(data).decode('ascii')}"}


# Display order and labels for the download menu; ``mtl`` rides along with ``obj``, never on its own.
_FORMAT_ORDER = ("glb", "fbx", "obj", "stl", "usdz", "3mf", "blend")
# Always fullmatch: ``$`` also matches before a trailing newline.
_TASK_ID = re.compile(r"[0-9a-f][0-9a-f-]{15,63}")
# A refined Meshy OBJ is ~180 MB; the cap bounds a runaway response, not a real model.
_MAX_SAVE_BYTES = 1024 * 1024 * 1024


# Rigging and animation tasks carry no ``model_urls``: their files sit in ``result`` as
# ``<what>_<fmt>_url`` (``rigged_character_glb_url``, ``animation_fbx_url``) and, for a rig's free
# clips, ``result.basic_animations.<clip>_<fmt>_url``. The main file maps to the plain format key;
# extra clips map to ``<fmt>:<clip>`` (``glb:walking``) so the Download menu lists one row each.
_MAIN_OUTPUTS = ("rigged_character", "animation", "processed")
_VARIANT = re.compile(r"[a-z0-9_]{1,40}")


def _result_urls(row: dict) -> dict[str, str]:
    """``{format or format:clip: url}`` for any task: ``model_urls`` as-is, else its ``result`` files."""
    urls = row.get("model_urls")
    if isinstance(urls, dict) and urls:
        return {k: v for k, v in urls.items() if isinstance(v, str) and v}
    result = row.get("result")
    out: dict[str, str] = {}
    if not isinstance(result, dict):
        return out

    def take(prefix: str, field: str, url: object) -> None:
        if not (isinstance(url, str) and url and field.endswith("_url")):
            return
        stem = field[:-4]
        fmt = next((f for f in _FORMAT_ORDER if stem == f or stem.endswith("_" + f)), None)
        if not fmt:
            return
        what = stem[: -len(fmt)].rstrip("_")
        if "armature" in what:
            return  # skeleton-only exports: not a model a user means to open
        name = what if not prefix else f"{prefix}_{what}" if what else prefix
        if not prefix and (not what or what in _MAIN_OUTPUTS):
            out.setdefault(fmt, url)
            return
        clip = re.sub(r"^(basic_animations_|processed_)", "", name)
        if _VARIANT.fullmatch(clip):
            out.setdefault(f"{fmt}:{clip}", url)

    for field, value in result.items():
        if isinstance(value, dict):
            for sub, url in value.items():
                take("", sub, url) if field == "basic_animations" else take(field, sub, url)
        else:
            take("", field, value)
    return out


# The model formats Desktop's viewer opens, best first (src/formats.ts ranks them the same way).
_VIEW_ORDER = ("glb", "fbx", "usdz", "obj", "3mf", "stl")  # formats /save writes


def _viewable_format(urls: dict[str, str]) -> str | None:
    """The best main-model format a task carries; a Convert task lists only the formats it made."""
    return next((f for f in _VIEW_ORDER if f in urls), None)


def _clip_label(urls: dict[str, str]) -> str:
    """A rig/animation task has no prompt; name it by the clips it carries (``walking, running``)."""
    clips = sorted({_split_format(k)[1] for k in urls if ":" in k} - {None})
    return ", ".join(c.replace("_", " ") for c in clips if c)


def _split_format(key: str) -> tuple[str, str | None]:
    base, _, clip = key.partition(":")
    return base, (clip or None)


def _format_sort_key(key: str) -> tuple[int, str]:
    base, clip = _split_format(key)
    return (_FORMAT_ORDER.index(base) if base in _FORMAT_ORDER else 99, clip or "")


# Resolved task routes, so polling a task costs one Meshy call instead of a probe of every route, and recent
# "no such task" verdicts, so a card for a deleted task does not re-probe on every retry. Keyed by (key
# fingerprint, task id): one dashboard process serves several profiles, each with its own key. Touched from
# request threads, so every read and write holds _ROUTE_LOCK (no I/O under it).
_ROUTE_LOCK = threading.Lock()
_ROUTE_CACHE: dict[tuple[str, str], str] = {}
_MISS_CACHE: dict[tuple[str, str], float] = {}
_ROUTE_CACHE_CAP = 1024
_MISS_TTL_S = 30.0
# Meshy's generic GET routes answer for a task of (almost) any type, reshaped for that route; the record's own
# ``type`` names the route with its full result (the Meshy MCP server's PROBE_TASK_TYPES note). Creative Lab
# and Text to Motion answer only on their own routes, and the rest are probed too in case a route stops
# answering generically.
_PROBE_FIRST = ("image-to-3d", "text-to-3d")


def _route_for(task_type: object) -> str | None:
    name = str(task_type or "").strip().lower()
    return _TASK_ROUTES.get(_TYPE_ENDPOINT.get(name, name))


def _task_at(route: str, task_id: str, key: str) -> dict | None:
    """The task as ``route`` returns it; None when that route says it is not there (400/404 or another id).
    Any other failure is Meshy's (outage, rate limit, rejected key): a 502, never a "no such task"."""
    try:
        row = _meshy_get(f"{route}/{task_id}", key)
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 404):
            return None
        if exc.code == 401:  # a revoked or mistyped key: permanent until the user fixes it, so a 4xx
            raise _KeyRefused(status_code=403, detail=_KEY_REJECTED) from exc
        if exc.code == 403:  # IP allowlist, org file filtering or a plan-gated route: Meshy says which
            raise HTTPException(status_code=403, detail=f"Meshy refused the request: {_meshy_message(exc)}") from exc
        raise _meshy_error(exc) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise _meshy_error(exc) from exc
    return row if isinstance(row, dict) and row.get("id") == task_id else None


def _forget(slot: tuple[str, str]) -> None:
    """Drop a cached route that stopped answering, WITHOUT a "no such task" verdict: only a lookup that went on to
    read every route and found none (``_remember(slot, None)``) may say that, never one an outage cut short."""
    with _ROUTE_LOCK:
        _ROUTE_CACHE.pop(slot, None)


def _remember(slot: tuple[str, str], route: str | None) -> None:
    with _ROUTE_LOCK:
        if route is None:
            _ROUTE_CACHE.pop(slot, None)
            _MISS_CACHE[slot] = time.monotonic() + _MISS_TTL_S
            cache: dict = _MISS_CACHE
        else:
            _MISS_CACHE.pop(slot, None)
            _ROUTE_CACHE[slot] = route
            cache = _ROUTE_CACHE
        while len(cache) > _ROUTE_CACHE_CAP:
            cache.pop(next(iter(cache)))


def _owned(row: dict, route: str, task_id: str, key: str) -> tuple[dict, str | None]:
    """Re-read a task found on a generic route from its own route, which carries its files. When the own route does
    not answer, the generic record lacks the files: a running or failed task still gets it (status and progress
    are all a card reads) with route None, so it is never cached and the next read asks the own route again; a
    finished one is a 502, because its record without files would show a finished task with no model."""
    own = _route_for(row.get("type"))
    if own and own != route:
        owned = _task_at(own, task_id, key)
        if owned is None:
            if str(row.get("status") or "").upper() == "SUCCEEDED":
                # Finished, but its files are only on the route that missed: "no model" is unknown, not known, and
                # a card stops polling on a final status, so answer an outage it retries instead.
                raise HTTPException(status_code=502, detail="Meshy did not return this task's files; try again")
            return row, None
        return owned, own
    return row, route


def _whose_refusal(exc: HTTPException, route: str, task_id: str, key: str) -> HTTPException:
    """A 403 on a task's known route: the key's or account's (_KeyRefused) when a generic route, which every plan
    reads, refuses too; otherwise only this task's (its own route is plan-gated) and ``exc`` stands."""
    generic = next(r for r in (_TASK_ROUTES[t] for t in _PROBE_FIRST) if r != route)
    try:
        _task_at(generic, task_id, key)
    except HTTPException as again:
        if again.status_code == 403:
            return again if isinstance(again, _KeyRefused) else _KeyRefused(status_code=403, detail=again.detail)
    return exc


_PROBE_LOCKS: dict[tuple[str, str], threading.Lock] = {}


def _task(task_id: str, key: str) -> dict:
    """The task record (signed ``model_urls`` / ``result`` / ``image_urls``) from the route that owns it."""
    if not _TASK_ID.fullmatch(task_id or ""):
        raise HTTPException(status_code=400, detail="not a Meshy task id")
    slot = (hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:16], task_id)
    with _ROUTE_LOCK:
        lock = _PROBE_LOCKS.setdefault(slot, threading.Lock())
        while len(_PROBE_LOCKS) > _ROUTE_CACHE_CAP:
            _PROBE_LOCKS.pop(next(iter(_PROBE_LOCKS)))
    # One lookup per task at a time: a card mounting twice, the chip and a retry asking together would each fan
    # out over every route (~34 reads) for an unknown id, past Meshy's 20 req/s, and get 429s. Waiters reuse the
    # first one's cached route or miss instead.
    with lock:
        return _task_locked(slot, task_id, key)


def _task_locked(slot: tuple[str, str], task_id: str, key: str) -> dict:
    with _ROUTE_LOCK:
        cached = _ROUTE_CACHE.get(slot)
        missed = _MISS_CACHE.get(slot, 0.0) > time.monotonic()
    if cached:
        try:
            row = _task_at(cached, task_id, key)
        except _KeyRefused:
            raise
        except HTTPException as exc:
            refusal = exc if exc.status_code != 403 else _whose_refusal(exc, cached, task_id, key)
            if refusal is exc:
                raise
            raise refusal from exc
        if row is not None:
            return row
        _forget(slot)  # deleted, no longer this key's, or a brief miss: probe again below
    elif missed:
        raise HTTPException(status_code=404, detail="Meshy has no finished task with that id")
    probe = [_TASK_ROUTES[t] for t in _PROBE_FIRST]
    refused = []  # every plan reads the generic routes, so their refusal is about the key or the account
    for route in probe:
        try:
            row = _task_at(route, task_id, key)
        except HTTPException as exc:
            if exc.status_code != 403 or isinstance(exc, _KeyRefused):
                raise
            refused.append(exc)  # the task may still be on its own route
            continue
        if row is not None:
            row, route = _owned(row, route, task_id, key)
            if route:
                _remember(slot, route)
            return row
    rest = [r for r in dict.fromkeys(_TASK_ROUTES.values()) if r not in probe]
    errors = []
    for route, found in zip(rest, _fan_out(lambda r: _task_at(r, task_id, key), rest)):
        try:
            row = found.result()
        except HTTPException as exc:
            errors.append(exc)
            continue
        if row is not None:
            row, route = _owned(row, route, task_id, key)
            if route:
                _remember(slot, route)
            return row
    # An outage (the card retries) outranks a refusal (the card stops); neither is remembered as a miss. A
    # refusal only on other routes is a plan-gated product, not this task: "no such task" as before.
    outage = next((e for e in errors if e.status_code >= 500), None)
    if outage:
        raise outage
    if refused:
        raise _KeyRefused(status_code=403, detail=refused[0].detail)
    _remember(slot, None)
    raise HTTPException(status_code=404, detail="Meshy has no finished task with that id")


def _asset_url(url: object) -> str:
    if not _asset_allowed(url):
        raise HTTPException(status_code=502, detail="unexpected asset host")
    return str(url)


def _asset_chunks(url: str, timeout: float, cap: int, too_big: str):
    """The bytes of an asset link, streamed; refused over ``cap`` (declared or counted) and on any redirect
    off the asset host. Network failures become HTTP errors here, so an OSError the caller sees is its own disk."""
    try:
        with _open(_asset_url(url), timeout, _asset_allowed) as resp:
            declared = str(resp.headers.get("Content-Length") or "").strip()
            if declared.isdigit() and int(declared) > cap:
                raise HTTPException(status_code=413, detail=too_big)
            copied = 0
            while chunk := resp.read(1 << 20):
                copied += len(chunk)
                if copied > cap:
                    raise HTTPException(status_code=413, detail=too_big)
                yield chunk
    except (OSError, http.client.HTTPException) as exc:  # URLError, HTTPError, timeouts, resets, short reads
        raise _asset_error(exc) from exc


def _save_target(task_id: str, beside: str | None) -> tuple[Path, str, Path | None]:
    """Folder + file stem for a saved format (+ the anchor): next to the model the card shows, else
    meshy_output/<task>. Validates both inputs, so callers run it BEFORE spending a Meshy API call."""
    if not _TASK_ID.fullmatch(task_id or ""):
        raise HTTPException(status_code=400, detail="not a Meshy task id")
    if beside is not None and not isinstance(beside, str):
        raise HTTPException(status_code=400, detail="beside must be a path")
    if beside:
        anchor = _resolve_readable(beside)
        return anchor.parent, anchor.stem, anchor
    return _data_root() / "meshy_output" / task_id, "model", None  # full id: UUIDv7 prefixes repeat within ms


def _saved_rows() -> dict[str, dict]:
    return {r["path"]: r for r in _load_index() if isinstance(r, dict) and r.get("path")}


def _fingerprint(st: os.stat_result) -> dict:
    """What /save's write looked like: any in-place edit changes ctime (no user tool can set it, unlike mtime, which
    ``cp -p``/``touch -r`` restore), and an editor's save-as-rename changes the inode. Windows' st_ctime is the
    creation time (and deprecated there), so it counts only on POSIX."""
    return {"bytes": st.st_size, "mtime_ns": st.st_mtime_ns, "ino": st.st_ino,
            "ctime_ns": st.st_ctime_ns if os.name != "nt" else None}


def _ours(path: Path, saved: dict[str, dict]) -> dict | None:
    """The save-time record (``_fingerprint`` + ``task_id``) of a file /save wrote and nobody has touched
    since, else None. Only /save's own write records it (``_record_asset(export=True)``): the row's read-time
    size/mtime are gallery metadata and never vouch for a file, and a row from before this record (or one the
    index cap dropped) is not ours either, so in doubt the save takes a new name instead of replacing a file."""
    row = saved.get(str(path))
    mark = row.get("saved") if isinstance(row, dict) else None
    if not isinstance(mark, dict):
        return None
    try:
        st = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or any(mark.get(k) != v for k, v in _fingerprint(st).items()):
        return None
    return mark


def _bundle(stem: str, fmt: str) -> list[str]:
    """Every file one save writes: an OBJ brings its .mtl and texture."""
    main = _saved_name(stem, fmt)
    return [main, f"{stem}.mtl", f"{stem}_texture.png"] if fmt == "obj" else [main]


def _beside_dest(folder: Path, stem: str, fmt: str, task_id: str, anchor: Path | None,
                 saved: dict[str, dict]) -> tuple[Path, bool]:
    """Where a format of ``task_id`` goes, and whether a file already there is this task's.
    Beside a project file only Hermes' own, untouched saves are replaceable: a sibling an earlier task
    saved is refreshed, but a file Hermes never saved, or one the user edited since (robot.stl, robot.mtl),
    is never overwritten, so the export takes a task-suffixed name instead. The anchor itself is the
    card's model, i.e. this task's."""
    dest = folder / _saved_name(stem, fmt)
    if anchor is None or dest == anchor:
        return dest, dest.is_file()
    # The suffixed name gets the same check (the user may have edited it too): first free-or-ours wins.
    for name in [stem, f"{stem}-{task_id[-8:]}", *(f"{stem}-{task_id[-8:]}-{n}" for n in range(2, 100))]:
        if not any(os.path.lexists(folder / n) and not _ours(folder / n, saved) for n in _bundle(name, fmt)):
            dest = folder / _saved_name(name, fmt)
            break
    else:
        raise HTTPException(status_code=409, detail=f"no free file name for this {fmt.upper()} beside the model")
    row = _ours(dest, saved)
    return dest, bool(row) and row.get("task_id") == task_id


def _staging(dest: Path) -> tuple[int, Path]:
    """A new, exclusively created temp file beside ``dest``: never an existing inode (symlink, hardlink or
    another request's partial file). Publish it with ``os.replace``, which swaps the name, not the target."""
    part = dest.with_name(f".{dest.name}.{os.urandom(8).hex()}.part")
    # O_EXCL refuses any existing entry, a dangling symlink included; 0o666 keeps the user's umask.
    return os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o666), part


def _write_new(dest: Path, chunks) -> None:
    """Publish ``chunks`` at ``dest`` through a fresh staging file; nothing is left behind on failure."""
    try:
        fd, part = _staging(dest)
    except OSError as exc:
        raise _disk_error(exc) from exc
    try:
        with open(fd, "wb") as out:
            for chunk in chunks:
                out.write(chunk)
        os.replace(part, dest)
    except OSError as exc:  # network errors arrive as HTTPException: this is the disk
        raise _disk_error(exc) from exc
    finally:
        part.unlink(missing_ok=True)


def _download(url: str, dest: Path) -> None:
    chunks = _asset_chunks(url, 120, _MAX_SAVE_BYTES, "asset larger than 1 GB")
    try:
        _write_new(dest, chunks)
    finally:
        chunks.close()


@router.get("/formats")
def formats(task: str, beside: str | None = None):
    folder, stem, anchor = _save_target(task, beside)
    key = _api_key()
    if not key:
        raise HTTPException(status_code=400, detail="no Meshy API key")
    row = _task(task, key)
    urls = _result_urls(row)
    saved_rows = _saved_rows() if beside else {}
    out = []
    for fmt in sorted((k for k in urls if _split_format(k)[0] in _FORMAT_ORDER), key=_format_sort_key):
        saved, mine = _beside_dest(folder, stem, fmt, task, anchor, saved_rows)
        out.append({"format": fmt, "saved": str(saved) if mine else None})
    kind = str(row.get("type") or "")
    conversions = []
    if urls and kind in ("text-to-3d-preview", "text-to-3d-refine", "image-to-3d", "multi-image-to-3d", "remesh", "retexture", "convert", "resize"):
        for fmt in ("glb", "fbx", "obj", "stl", "usdz", "blend", "3mf"):
            if fmt not in urls:
                conversions.append({"format": fmt, "cost": 1,
                                    "message": f'Meshy task {task}: convert it to {fmt.upper()} using meshy_convert '
                                    f'with input_task_id "{task}" and target_formats ["{fmt}"] (1 credit), '
                                    'then wait and download the converted task.'})
    return {"task": task, "formats": out, "conversions": conversions, "is_preview": kind == "text-to-3d-preview"}


@router.post("/save")
def save(body: dict):
    """Download one format of a finished task. Meshy produced every listed format at generation time,
    so this spends no credits. OBJ brings its .mtl and base-colour texture, renamed per model."""
    task_id, fmt = str(body.get("task") or ""), str(body.get("format") or "").lower()
    base, clip = _split_format(fmt)
    if base not in _FORMAT_ORDER or (clip is not None and not _VARIANT.fullmatch(clip)):
        raise HTTPException(status_code=400, detail="unknown format")
    beside = body.get("beside") or None
    folder, stem, anchor = _save_target(task_id, beside)
    key = _api_key()
    if not key:
        raise HTTPException(status_code=400, detail="no Meshy API key")
    row = _task(task_id, key)
    urls = _result_urls(row)
    if not urls.get(fmt):
        raise HTTPException(status_code=404, detail=f"this model has no {fmt.upper()} file")
    try:
        return _save_format(row, urls, task_id, fmt, folder, stem, anchor, bool(beside))
    except OSError as exc:  # downloads report the network as HTTP errors: this is the local disk
        raise _disk_error(exc) from exc


def _save_format(row: dict, urls: dict[str, str], task_id: str, fmt: str, folder: Path, stem: str,
                 anchor: Path | None, beside: bool) -> dict:
    if not beside:
        # meshy_output/<task> is the plugin's own folder: a link there would send the save somewhere else.
        if folder.is_symlink():
            raise HTTPException(status_code=403, detail="the download folder must not be a symlink")
        folder.mkdir(parents=True, exist_ok=True)
        if folder.resolve() != (_data_root() / "meshy_output").resolve() / task_id:
            raise HTTPException(status_code=403, detail="the download folder must not be a symlink")
        # Replaced, never written through: a planted link at the sidecar's name must not redirect the write.
        _write_new(folder / _TASK_SIDECAR, [task_id.encode("ascii")])
    else:
        folder.mkdir(parents=True, exist_ok=True)
    # Two clicks (or two windows) saving one file: the second waits, then finds it saved.
    with _STATE_LOCK:
        dest_lock = _SAVE_LOCKS.setdefault(str(folder / _saved_name(stem, fmt)), threading.Lock())
    with dest_lock:
        dest, mine = _beside_dest(folder, stem, fmt, task_id, anchor, _saved_rows() if beside else {})
        stem = dest.name[: -len(_saved_name("", fmt))] if beside else stem  # OBJ companions follow a suffixed name
        fresh = not mine
        if fresh:
            _download(urls[fmt], dest)
            _record_asset(dest, task_id, export=True)
        # The .mtl is published last: an OBJ without one is a save whose companions failed, so finish them now, but
        # only on this task's own save. An OBJ that is the card's model (the anchor) without a .mtl may be the user's.
        if fmt == "obj" and urls.get("mtl") and (fresh or (not (folder / f"{stem}.mtl").is_file()
                                                           and _finishable(folder, stem, fmt, task_id, beside))):
            _save_obj_material(row, urls["mtl"], folder, stem)
            for name in _bundle(stem, fmt):  # the rewritten OBJ and its companions are Hermes' saves too
                _record_asset(folder / name, task_id, export=True)
    if fmt in ("glb", "gltf"):
        _record_asset(dest, task_id)
    return {"path": str(dest), "bytes": dest.stat().st_size}


def _finishable(folder: Path, stem: str, fmt: str, task_id: str, beside: bool) -> bool:
    """Whether an earlier save of ``task_id`` may be completed in place: always in the plugin's own folder; beside a
    project only when the main file is that task's untouched save and every companion name is free or Hermes'."""
    if not beside:
        return True
    saved = _saved_rows()
    main, *companions = _bundle(stem, fmt)
    mark = _ours(folder / main, saved)
    return bool(mark) and mark.get("task_id") == task_id and all(
        not os.path.lexists(folder / n) or _ours(folder / n, saved) for n in companions)


# One lock per destination path, so concurrent saves of the same file download it once.
_SAVE_LOCKS: dict[str, threading.Lock] = {}


def _saved_name(stem: str, fmt: str) -> str:
    """``model.glb`` for the main file, ``model_walking.glb`` for a rig's extra clip."""
    base, clip = _split_format(fmt)
    return f"{stem}_{clip}.{base}" if clip else f"{stem}.{base}"


def _save_obj_material(row: dict, mtl_url: str, folder: Path, stem: str) -> None:
    """The .mtl names ``texture_0.png``; save it as ``<stem>_texture.png`` so two models in one folder
    never share (and overwrite) a texture, and point the .mtl at the new name."""
    mtl = b"".join(_asset_chunks(mtl_url, 60, 1 << 20, "material file larger than 1 MB")).decode("utf-8", "replace")
    textures = row.get("texture_urls") if isinstance(row.get("texture_urls"), list) else []
    base = textures[0].get("base_color") if textures and isinstance(textures[0], dict) else None
    if base:
        _download(base, folder / f"{stem}_texture.png")
        mtl = re.sub(r"(?m)^(\s*map_Kd\s+).*$", rf"\g<1>{stem}_texture.png", mtl)
    # Repoint ``mtllib`` (a header line) by streaming: the OBJ is ~180 MB, never read it whole.
    obj = folder / f"{stem}.obj"
    fd, tmp = _staging(obj)
    try:
        with open(fd, "wb") as dst, open(obj, "rb") as src:
            for _ in range(64):
                line = src.readline()
                if not line:
                    break
                dst.write(f"mtllib {stem}.mtl\n".encode() if line.startswith(b"mtllib ") else line)
            shutil.copyfileobj(src, dst, 1 << 20)
        os.replace(tmp, obj)
    finally:
        tmp.unlink(missing_ok=True)
    # Last, so /save treats an OBJ without its .mtl as unfinished and retries the companions.
    mtl_path = folder / f"{stem}.mtl"
    fd, tmp = _staging(mtl_path)
    try:
        with open(fd, "w", encoding="utf-8") as out:
            out.write(mtl)
        os.replace(tmp, mtl_path)
    finally:
        tmp.unlink(missing_ok=True)


@router.get("/remote")
def remote(url: str):
    """Fetch a Meshy asset URL (signed ``assets.meshy.ai`` link) server-side; the renderer cannot
    (no CORS on that host). Only that host, only model/image types."""
    if not _asset_allowed(url):
        raise HTTPException(status_code=400, detail="only assets.meshy.ai URLs")
    parts = urllib.parse.urlsplit(url)
    suffix = Path(parts.path).suffix.lower()
    if suffix not in _MIME:
        raise HTTPException(status_code=415, detail="only .glb and image assets")
    started = time.monotonic()
    data = b"".join(_asset_chunks(url, 60, _MAX_FILE_BYTES, "asset too large for the viewer"))
    _log.debug("meshy remote asset %s: %d bytes in %.1fs", parts.path, len(data), time.monotonic() - started)
    return {"name": Path(parts.path).name, "bytes": len(data),
            "dataUrl": f"data:{_MIME[suffix]};base64,{base64.b64encode(data).decode('ascii')}"}


_TERMINAL_FAILED = ("FAILED", "CANCELED", "EXPIRED")


@router.get("/task")
def task_state(task: str):
    """Status for a ``::meshy-model{task=...}`` card. Polled every few seconds while the task runs."""
    key = _api_key()
    if not key:
        raise HTTPException(status_code=400, detail="no Meshy API key")
    row = _task(task, key)
    status = str(row.get("status") or "")
    error = row.get("task_error") if isinstance(row.get("task_error"), dict) else {}
    urls = _result_urls(row) if status == "SUCCEEDED" else {}
    view = _viewable_format(urls)
    return {
        "task": task, "status": status, "progress": _as_int(row.get("progress")),
        "type": str(row.get("type") or ""), "stage": str(row.get("mode") or row.get("type") or ""),
        "prompt": str(row.get("prompt") or "")[:200] or _clip_label(urls) or None,
        "thumbnail": row.get("thumbnail_url") if isinstance(row.get("thumbnail_url"), str) else None,
        "credits": row.get("consumed_credits"),
        "has_model": view is not None, "view_format": view,
        "has_files": any(k in _FORMAT_ORDER for k in urls),  # e.g. a Convert-to-BLEND task: downloadable, not viewable
        "error": (str(error.get("message") or "") or status.lower()) if status in _TERMINAL_FAILED else None,
    }


# In-flight tasks for the Desktop progress chip. The renderer names the tasks it saw this profile's tools start
# (``kind:id``, kind = a ``_TASK_ROUTES`` name or a record ``type``), so a poll costs one Meshy read per running task
# and nothing when none runs. Listing every route instead cost 17 calls per 4 s poll (Meshy allows 20/s per key).
_ACTIVE_STATUSES = ("PENDING", "IN_PROGRESS")
_ACTIVE_MAX = 8
_ACTIVE_ITEM = re.compile(r"([a-z0-9-]{1,48}):([0-9a-f][0-9a-f-]{15,63})")


def _active_wanted(tasks: str) -> list[tuple[str, str]]:
    wanted: dict[str, str] = {}
    for item in filter(None, (part.strip() for part in tasks.split(","))):
        m = _ACTIVE_ITEM.fullmatch(item)
        if not m:
            raise HTTPException(status_code=400, detail="tasks must be kind:task_id pairs")
        # A kind with no route (a status call's wrong task_type; Meshy's server tolerates those) is found by lookup.
        wanted.setdefault(m.group(2), m.group(1) if _route_for(m.group(1)) else "any")
    if len(wanted) > _ACTIVE_MAX:
        raise HTTPException(status_code=400, detail=f"at most {_ACTIVE_MAX} tasks")
    return [(kind, task_id) for task_id, kind in wanted.items()]


def _active_read(kind: str, task_id: str, key: str) -> dict | None:
    """The task from its own route, else by lookup (cached after the first read): "any" (a status call that named
    no type) goes straight to the lookup, and so does a named route that does not know the task or refuses it. A
    status call's ``task_type`` can be a real route name and still be wrong, and Meshy's server accepts that."""
    with _ACTIVE_SLOTS:
        if kind != "any":
            try:
                row = _task_at(_route_for(kind), task_id, key)
            except _KeyRefused:
                raise
            except HTTPException as exc:
                if exc.status_code != 403:
                    raise
                row = None  # a plan-gated product route, or the wrong one: the lookup decides
            if row is not None:
                return row
        try:
            return _task(task_id, key)
        except HTTPException as exc:
            if exc.status_code == 404:
                return None
            raise


def _active_row(kind: str, task_id: str, row: dict) -> dict:
    return {"task_id": task_id, "task_type": _TYPE_ENDPOINT.get(kind, kind), "status": str(row.get("status") or ""),
            "stage": str(row.get("mode") or row.get("type") or kind), "progress": _as_int(row.get("progress")),
            "prompt": str(row.get("prompt") or "")[:120], "created_at": row.get("created_at") or 0}


@router.get("/active")
def active(tasks: str = ""):
    """Status and Meshy's progress % of the named tasks (``kind:id,...``): ``tasks`` holds the ones still PENDING /
    IN_PROGRESS (newest first), ``done`` the ids that reached any other status, ``missing`` the ids Meshy does not
    know (yet), ``failed`` the ids whose read failed this time (an outage, or a route that refuses that one task;
    the chip keeps them and retries). Only a refusal of the key itself fails the whole request: it fails every read."""
    key = _api_key()
    if not key:
        return {"configured": False, "tasks": [], "done": [], "missing": [], "failed": []}
    wanted = _active_wanted(tasks)
    running, done, missing, failed = [], [], [], []
    with ThreadPoolExecutor(max_workers=_FANOUT) as pool:
        reads = [pool.submit(_active_read, kind, task_id, key) for kind, task_id in wanted]
    for (kind, task_id), found in zip(wanted, reads):
        try:
            row = found.result()
        except _KeyRefused:
            raise  # 403: the chip stops until the next Meshy call
        except HTTPException:
            failed.append(task_id)
            continue
        if row is None:
            missing.append(task_id)
            continue
        if str(row.get("type") or ""):  # the record's own type, whatever kind the caller guessed
            kind = _TYPE_ENDPOINT.get(str(row["type"]), str(row["type"]))
        if row.get("status") in _ACTIVE_STATUSES:
            running.append(_active_row(kind, task_id, row))
        else:
            done.append(task_id)
    running.sort(key=lambda t: t["created_at"] if isinstance(t["created_at"], (int, float)) else 0, reverse=True)
    return {"configured": True, "tasks": running, "done": done, "missing": missing, "failed": failed}


# Card buttons. Each one SENDS A CHAT MESSAGE (the agent runs the tool behind Hermes' approval card); the
# router never spends credits. Which actions a task offers follows each Meshy tool's documented input
# ("input_task_id: a SUCCEEDED text-to-3d, image-to-3d or remesh task" ...); costs are Meshy's API prices.
_GENERATED = ("text-to-3d-refine", "image-to-3d", "multi-image-to-3d")
_ACTIONS = (
    # id, label, cost (None = the task's own consumed_credits), task types it applies to, message
    ("texture", "Texture it", 10, ("text-to-3d-preview",),
     "texture it with meshy_text_to_3d_refine (10 credits). Ask me for a texture style only if I haven't given one."),
    # Text only: an image task's record does not keep its input image, so it cannot be re-run from the id.
    # Priced and worded per task by _regenerate (the same ai_model, so the same price as the original).
    ("regenerate", "Regenerate", None, ("text-to-3d-preview",),
     ("regenerate it: a NEW meshy_text_to_3d preview with the same prompt (read it with meshy_get_task_status) "
      "and {settings} ({cost} credits). Meshy's API has no free retry.")),
    ("remesh", "Remesh", 5, ("text-to-3d-preview", *_GENERATED, "retexture"),
     ("remesh it with meshy_remesh (5 credits). Suggest a triangle target for what it is (player character "
      "~15k, NPC 5-15k, weapon ~3k, small prop ~1.5k; mobile about half; close-up or print: keep detail) and ask "
      "me the target and triangle or quad before calling.")),
    ("retexture", "Retexture", 10, ("text-to-3d-preview", *_GENERATED, "remesh"),
     "retexture it with meshy_retexture (10 credits). Ask me for the style in words first."),
    ("rig", "Rig", 5, (*_GENERATED, "retexture", "remesh"),
     ("rig it with meshy_rig (5 credits, includes walking + running clips). Only if it is a textured humanoid; "
      "say so and stop if it is not.")),
    ("animate", "Animate", 3, ("rig",),
     ("animate this rig with meshy_animate (3 credits per action). Look up action ids with "
      "meshy_list_animations (free) and ask me which ones, and whether my game moves the character "
      "(then use the in-place variants: search \"inplace\").")),
    ("resize", "Resize", 1, (*_GENERATED, "remesh", "retexture"),
     "resize it to real-world size with meshy_resize (1 credit). Ask me for the height."),
    ("printability", "Check printability", 0, ("text-to-3d-preview", *_GENERATED, "remesh", "retexture"),
     "check it for 3D printing with meshy_analyze_printability (free) and summarise the result."),
)


# meshy_text_to_3d preview prices (the tool's own description). Ultra geometry (2k/4k) adds 5.
_PREVIEW_PRICE = {"latest": 20, "meshy-7.1": 20, "meshy-7": 20, "meshy-6": 20,
                  "meshy-6-lite": 5, "meshy-t2": 5, "meshy-t2.5": 5}


def _regenerate(row: dict, rec: dict) -> tuple[str, int]:
    """The settings a re-run must pass to land on the original's model, and what that model costs.

    Meshy's task record has no ``ai_model``, and meshy_text_to_3d defaults to ``latest`` (20 credits), so a
    re-run that omits it can cost 4x what a meshy-6-lite preview did. The model comes from the recorded tool
    call; unrecorded, a 5-credit standard preview can only have been meshy-6-lite, and otherwise the tool's
    default for that model_type is named explicitly (and priced)."""
    settings = rec.get("settings") if rec.get("tool") == "meshy_text_to_3d" and isinstance(rec.get("settings"), dict) else {}
    model_type = next((v for v in (row.get("model_type"), settings.get("model_type"))
                       if isinstance(v, str) and re.fullmatch(r"[a-z0-9-]{1,32}", v)), "standard")
    model = next((v for v in (settings.get("ai_model"), row.get("ai_model")) if isinstance(v, str) and v in _PREVIEW_PRICE), None)
    if model is None:
        model = ("meshy-t2" if model_type == "smart-topology"
                 else "meshy-6-lite" if row.get("consumed_credits") == 5 else "latest")
    picked = [f'ai_model "{model}"', f'model_type "{model_type}"']
    cost = _PREVIEW_PRICE[model]
    if settings.get("geometry_resolution") in ("2k", "4k"):
        picked.append(f'geometry_resolution "{settings["geometry_resolution"]}"')
        cost += 5
    return ", ".join(picked), cost


@router.get("/actions")
def actions(task: str):
    if not _TASK_ID.fullmatch(task or ""):
        raise HTTPException(status_code=400, detail="not a Meshy task id")
    key = _api_key()
    if not key:
        raise HTTPException(status_code=400, detail="no Meshy API key")
    row = _task(task, key)
    kind = str(row.get("type") or "").lower()
    spent = row.get("consumed_credits") if isinstance(row.get("consumed_credits"), (int, float)) else None
    try:
        bal = _meshy_get("/openapi/v1/balance", key)
    except (urllib.error.URLError, OSError, ValueError):
        bal = None
    balance = bal.get("balance") if isinstance(bal, dict) and isinstance(bal.get("balance"), (int, float)) else None
    what = str(row.get("prompt") or "").strip()[:80]
    subject = f'Meshy task {task}' + (f' ("{what}")' if what else "")
    out = []
    for aid, label, cost, kinds, message in _ACTIONS:
        if kind not in kinds:
            continue
        if aid == "regenerate":
            picked, cost = _regenerate(row, _load_prov().get(task, {}))
            message = message.replace("{settings}", picked)
        cost = int(spent) if cost is None and spent is not None else cost
        if cost is None:
            continue  # unknown price: never show a button that can't state what it spends
        out.append({"id": aid, "label": label, "cost": cost,
                    "affordable": balance is None or cost <= balance,
                    "covers": (int(balance // cost) if cost and isinstance(balance, (int, float)) else None),
                    "message": f"{subject}: {message.format(cost=cost)} This button message is my confirmation "
                               f"of the {cost}-credit cost." if cost else f"{subject}: {message}"})
    return {"task": task, "type": kind, "model_type": row.get("model_type"), "spent": spent,
            "balance": balance, "actions": out}


# Provenance: what made each model. Meshy's task record keeps a text prompt but not an image task's
# input, nor which task a remesh/rig/animation came from, so Desktop records the creating tool call here.
_PROV_NAME = "provenance.json"
_PROV_CAP = 2000
_PROV_TEXT = ("prompt", "texture_prompt", "text_style_prompt", "negative_prompt", "object_prompt")
_PROV_PARENT = ("preview_task_id", "input_task_id", "rig_task_id")
_PROV_SETTINGS = ("ai_model", "model_type", "topology", "target_polycount", "texture_resolution", "enable_pbr",
                  "geometry_resolution", "pose_mode", "symmetry_mode", "should_texture", "resize_height",
                  "height_meters", "action_id", "action_ids")
_SESSION_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def _prov_path() -> Path:
    return _data_root() / _PROV_NAME


def _load_prov() -> dict[str, dict]:
    try:
        rows = json.loads(_prov_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in rows.items() if isinstance(v, dict) and _TASK_ID.fullmatch(k)} if isinstance(rows, dict) else {}


def _image_ref(args: dict) -> str | None:
    """A source image worth showing again: a local path, or a URL without its query (signed tokens)."""
    path = args.get("file_path")
    if isinstance(path, str) and path.strip() and not path.startswith("data:"):
        return path.strip()[:1024]
    url = args.get("image_url")
    if isinstance(url, str) and url.startswith(("https://", "http://")):
        parts = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))[:1024]
    return None


@router.post("/provenance")
def provenance(body: dict):
    task_id, tool = str(body.get("task_id") or ""), str(body.get("tool") or "")
    if not _TASK_ID.fullmatch(task_id) or not re.fullmatch(r"meshy_[a-z0-9_]{1,40}", tool):
        raise HTTPException(status_code=400, detail="task_id and a meshy_* tool required")
    args = body.get("args") if isinstance(body.get("args"), dict) else {}
    row: dict = {"tool": tool, "recorded_at": int(time.time() * 1000)}
    for key in _PROV_TEXT:
        if isinstance(args.get(key), str) and args[key].strip():
            row[key] = args[key].strip()[:800]
    parent = next((args[k] for k in _PROV_PARENT if isinstance(args.get(k), str) and _TASK_ID.fullmatch(args[k])), None)
    if parent:
        row["parent"] = parent
    if image := _image_ref(args):
        row["image"] = image
    settings = {k: args[k] for k in _PROV_SETTINGS
                if isinstance(args.get(k), (str, int, float, bool)) or (k == "action_ids" and isinstance(args.get(k), list))}
    if settings:
        row["settings"] = {k: (v[:10] if isinstance(v, list) else v) for k, v in settings.items()}
    sid = body.get("session_id")
    # Ownership comes from this backend's own sessions, never from the client's guess: Desktop tags a
    # local event with whatever profile/connection is in front when it arrives, so a record is accepted
    # only by the profile whose state.db holds the chat that made it. 409 = keep it and try elsewhere.
    if not (isinstance(sid, str) and _SESSION_ID.fullmatch(sid)) or not _own_session(sid):
        raise HTTPException(status_code=409, detail="that chat is not on this profile")
    row["session_id"] = sid
    with _STATE_LOCK:  # completions of a parallel tool_call arrive together
        rows = _load_prov()
        rows[task_id] = row
        if len(rows) > _PROV_CAP:
            rows = dict(sorted(rows.items(), key=lambda kv: _as_int(kv[1].get("recorded_at")))[-_PROV_CAP:])
        try:
            _write_json(_prov_path(), rows)
        except OSError as exc:
            raise _disk_error(exc) from exc
    return {"ok": True}


def _own_session(session_id: str) -> bool:
    """True when this profile's state.db has the session (read-only; no schema work, no writes)."""
    import sqlite3

    from hermes_constants import get_hermes_home
    db = get_hermes_home() / "state.db"
    if not db.is_file():
        return False
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5) as conn:
            return conn.execute("SELECT 1 FROM sessions WHERE id = ? LIMIT 1", (session_id,)).fetchone() is not None
    except sqlite3.Error as exc:
        _log.debug("meshy provenance: session lookup failed: %s", exc)
        return False


def _download_lineage(task_id: str) -> tuple[str | None, list[dict]]:
    """The MCP server's own record for downloaded models (any surface, CLI included): the project prompt and
    its task chain in order, from ``meshy_output/<project>/metadata.json``."""
    out_dir = _data_root() / "meshy_output"
    for meta_path in out_dir.glob("*/metadata.json") if out_dir.is_dir() else ():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue  # another tool's metadata.json (valid JSON, not a project record)
        tasks = [t for t in meta.get("tasks") or [] if isinstance(t, dict) and _TASK_ID.fullmatch(str(t.get("task_id") or ""))]
        if any(t["task_id"] == task_id for t in tasks):
            seen, chain = set(), []
            for t in tasks:
                if t["task_id"] not in seen:
                    seen.add(t["task_id"])
                    chain.append({"task_id": t["task_id"], "type": str(t.get("task_type") or ""), "stage": str(t.get("stage") or "")})
            return (meta.get("project_name") if isinstance(meta.get("project_name"), str) else None), chain
    return None, []


def _earlier_in(download_chain: list[dict], task: str) -> list[dict]:
    """Tasks before ``task`` in its download project (the MCP server records them in order)."""
    ids = [c["task_id"] for c in download_chain]
    # The MCP server types every text-to-3d stage alike; its stage says preview vs textured.
    def step(c: dict) -> str:
        return "meshy_text_to_3d_refine" if c["stage"] == "refined" else c["type"]
    return [{"task_id": c["task_id"], "tool": step(c)} for c in download_chain[:ids.index(task)]] if task in ids else []


@router.get("/details")
def details(task: str):
    if not _TASK_ID.fullmatch(task or ""):
        raise HTTPException(status_code=400, detail="not a Meshy task id")
    prov = _load_prov()
    rec = prov.get(task, {})
    key = _api_key()
    row: dict = {}
    remote_error = None
    if key:
        # The recorded lineage is local: only a refused key fails the panel. A task Meshy lost answers without a
        # notice; an outage or a plan-gated route keeps the recorded fields and says why Meshy's are missing.
        try:
            row = _task(task, key)
        except _KeyRefused:
            raise
        except HTTPException as exc:
            remote_error = None if exc.status_code == 404 else exc.detail
    # Walk recorded parents for the chain, and find what was made from this task.
    chain, cur, hops = [], rec.get("parent"), 0
    while cur and hops < 12 and cur not in {c["task_id"] for c in chain}:
        chain.insert(0, {"task_id": cur, "tool": prov.get(cur, {}).get("tool")})
        cur, hops = prov.get(cur, {}).get("parent"), hops + 1
    children = [{"task_id": k, "tool": v.get("tool")} for k, v in prov.items() if v.get("parent") == task]
    project_prompt, download_chain = _download_lineage(task)
    # A remesh/rig/animation has no prompt of its own: each origin field comes from the nearest step that has
    # one (a text model's prompt; an image model's source image and texture prompt), labelled as inherited.
    own = {"prompt": rec.get("prompt") or (row.get("prompt") if isinstance(row.get("prompt"), str) else ""),
           "texture_prompt": rec.get("texture_prompt") or (row.get("texture_prompt") if isinstance(row.get("texture_prompt"), str) else ""),
           "image": rec.get("image")}
    inherited = None
    for field in ("prompt", "texture_prompt", "image"):
        if own[field]:
            continue
        for link in reversed(chain):
            if anc := prov.get(link["task_id"], {}).get(field):
                own[field], inherited = anc, inherited or link["task_id"]
                break
    # The MCP server names a project after its first task's type when there is no prompt ("convert", "remesh",
    # ...): that is not a prompt. The first task's own project prompt is its own, not inherited from itself.
    origin = download_chain[0]["task_id"] if download_chain else None
    if (not own["prompt"] and not own["image"] and project_prompt
            and project_prompt not in {c["type"] for c in download_chain} | {"text-to-3d", "image-to-3d"}):
        own["prompt"] = project_prompt
        if origin != task:
            inherited = inherited or origin
    prompt, texture, image = own["prompt"], own["texture_prompt"], own["image"]
    image_local = bool(image) and not str(image).startswith(("http://", "https://"))
    if image_local:
        try:
            _resolve_readable(str(image))
        except HTTPException:
            image_local = False  # moved, or outside the folders the viewer may read
    style = rec.get("text_style_prompt") or (row.get("text_style_prompt") if isinstance(row.get("text_style_prompt"), str) else "")
    settings = dict(rec.get("settings") or {})
    if isinstance(row.get("model_type"), str) and row["model_type"]:
        settings.setdefault("model_type", row["model_type"])
    return {
        "task": task, "type": str(row.get("type") or rec.get("tool") or ""),
        "prompt": prompt or None, "prompt_from": inherited,
        "texture_prompt": texture or None, "style_prompt": style or None,
        "negative_prompt": rec.get("negative_prompt"),
        "image": image, "image_viewable": image_local,
        "settings": settings, "seed": row.get("seed") or None,  # 0 = a step with no seed (resize, rig, ...)
        "credits": row.get("consumed_credits"), "created_at": row.get("created_at") or rec.get("recorded_at"),
        "parents": chain or _earlier_in(download_chain, task),
        "children": children,
        "session_id": rec.get("session_id"),
        "recorded": bool(rec),
        "remote_error": remote_error,
    }


def _saved_assets_root() -> Path:
    return _data_root() / "saved-assets"


@router.get("/storage")
def storage():
    # Count known files once, including companion textures and copies saved by the MCP server.
    paths = set()
    for folder in (_saved_assets_root(), _data_root() / "meshy_output", _data_root() / _UPLOAD_DIR):
        for path in folder.rglob("*") if folder.is_dir() else ():
            if path.is_file() and not path.is_symlink() and path.suffix != ".part":
                paths.add(path.resolve())
    for row in _load_index():
        try:
            # Every saved format counts (BLEND too), at any size: the total is disk use, not what the viewer opens.
            paths.add(_resolve_readable(row["path"], exts=_COUNTED, max_bytes=None))
        except HTTPException:
            continue
    sizes = []
    for path in paths:
        try:
            sizes.append(path.stat().st_size)
        except OSError:
            continue
    return {"bytes": sum(sizes), "files": len(sizes), "directory": str(_data_root())}


_COUNTED = frozenset((*_MIME, *_INDEXED))


_IMAGE_SAVE_LOCKS: dict[str, threading.Lock] = {}  # one per task: unrelated sets download in parallel


@router.post("/save-images")
def save_images(body: dict):
    """Explicitly download every image in a finished image task; never generate or convert."""
    task = str(body.get("task") or "")
    if not _TASK_ID.fullmatch(task):
        raise HTTPException(status_code=400, detail="not a Meshy task id")
    key = _api_key()
    if not key:
        raise HTTPException(status_code=400, detail="no Meshy API key")
    with _STATE_LOCK:
        lock = _IMAGE_SAVE_LOCKS.setdefault(task, threading.Lock())
    with lock:
        row = _task(task, key)
        try:
            return _save_image_set(task, row)
        except OSError as exc:  # downloads report the network as HTTP errors: this is the local disk
            raise _disk_error(exc) from exc


def _save_image_set(task: str, row: dict) -> dict:
    urls = row.get("image_urls")
    if not isinstance(urls, list) or not urls:
        raise HTTPException(status_code=404, detail="this task has no generated images")
    urls = [_asset_url(url) for url in urls]
    root = _saved_assets_root()
    folder = root / task
    if root.is_symlink() or folder.is_symlink():
        raise HTTPException(status_code=403, detail="image folder must not be a symlink")
    folder.mkdir(parents=True, exist_ok=True)
    if folder.resolve() != root.resolve() / task:
        raise HTTPException(status_code=403, detail="image folder must not be a symlink")
    outputs = []
    for index, url in enumerate(urls):
        ext = Path(urllib.parse.urlsplit(url).path).suffix.lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            ext = ".png"
        outputs.append((folder / f"image-{index + 1}{ext}", url))
    for path, url in outputs:
        if path.is_symlink():
            raise HTTPException(status_code=403, detail="image file must not be a symlink")
        if not path.is_file():
            _download(url, path)
        # Record partial successes too: a retry resumes, and already saved views stay usable.
        names = [p.name for p, _ in outputs if p.is_file() and not p.is_symlink()]
        record = {"task": task, "prompt": row.get("prompt"), "files": names, "complete": len(names) == len(outputs)}
        target = folder / "images.json"
        part = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder,
                                             prefix=".images.", suffix=".part", delete=False) as out:
                part = Path(out.name)
                json.dump(record, out)
            part.replace(target)
        finally:
            if part is not None:
                part.unlink(missing_ok=True)
    paths = [str(path) for path, _ in outputs]
    return {"paths": paths, "directory": str(folder), "bytes": sum(path.stat().st_size for path, _ in outputs)}


def _saved_images() -> list[dict]:
    out = []
    for manifest in _saved_assets_root().glob("*/images.json"):
        try:
            row = json.loads(manifest.read_text(encoding="utf-8"))
            task = row.get("task")
            if not isinstance(task, str) or not _TASK_ID.fullmatch(task) or task != manifest.parent.name:
                continue
            prompt = row.get("prompt") if isinstance(row.get("prompt"), str) else None
            paths = []
            for name in row.get("files", []):
                if not isinstance(name, str) or Path(name).name != name:
                    continue
                try:
                    path = _resolve_readable(str(manifest.parent / name))
                except HTTPException:
                    continue
                if path.is_relative_to(_saved_assets_root().resolve()) and path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                    paths.append(path)
            if paths:
                out.append({"path": str(paths[0]), "images": [str(p) for p in paths], "name": paths[0].name,
                            "task": task, "kind": "image", "prompt": prompt, "project": "Saved images",
                            "download_incomplete": not row.get("complete", True) or len(paths) != len(row.get("files", [])),
                            "bytes": sum(p.stat().st_size for p in paths), "modified": max(int(p.stat().st_mtime * 1000) for p in paths)})
        except (OSError, ValueError, AttributeError, TypeError):
            continue
    return out


# --- /upload: "View a model file" against a remote backend --------------------------------------------------
# The picked file is on the Desktop's computer; a remote backend cannot open that path, so the renderer
# sends the bytes. Stored content-addressed under the plugin's data dir (a re-send reuses the file) and
# recorded in the asset index, so it shows in the gallery like any other saved model.
_UPLOAD_DIR = "uploads"
_UPLOAD_NAME = re.compile(r"[^A-Za-z0-9._-]+")
# Older Desktops send base64 inside JSON: a third larger than the file, plus the envelope.
_MAX_UPLOAD_BODY = _MAX_FILE_BYTES * 4 // 3 + 64 * 1024


def _multipart_file(body: bytes, content_type: str) -> tuple[str, bytes] | None:
    """The first file part of a multipart/form-data body (Desktop sends exactly one, field ``file``)."""
    match = re.search(r'boundary="?([^";]+)"?', content_type)
    if not match:
        return None
    delim = b"--" + match.group(1).encode("latin-1")
    start = body.find(delim)
    while start != -1:
        head_start = start + len(delim)
        if body[head_start:head_start + 2] == b"--":
            return None  # closing delimiter: no file part
        head_end = body.find(b"\r\n\r\n", head_start)
        if head_end == -1:
            return None
        headers = body[head_start:head_end].decode("utf-8", "replace")
        data_start = head_end + 4
        nxt = body.find(b"\r\n" + delim, data_start)
        if nxt == -1:
            return None
        name = re.search(r'filename="([^"]*)"', headers)
        if name:
            return name.group(1), body[data_start:nxt]
        start = nxt + 2
    return None


def _json_file(body: bytes) -> tuple[str, bytes] | None:
    try:
        doc = json.loads(body)
    except ValueError:
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("filename"), str) or not isinstance(doc.get("data"), str):
        return None
    try:
        return doc["filename"], base64.b64decode(doc["data"], validate=True)
    except ValueError:
        raise HTTPException(status_code=400, detail="file data is not valid base64") from None


def _store_upload(filename: str, data: bytes) -> dict:
    name = _UPLOAD_NAME.sub("_", filename.replace("\\", "/").rsplit("/", 1)[-1]).strip("._ ")[-80:] or "model"
    if Path(name).suffix.lower() not in _MODEL_MIME:
        raise HTTPException(status_code=415, detail="only .glb, .gltf, .fbx, .obj, .stl, .usdz and .3mf models")
    if not data:
        raise HTTPException(status_code=400, detail="the file is empty")
    if len(data) > _MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="file too large for the viewer")
    base = _data_root()
    root = base / _UPLOAD_DIR
    if root.is_symlink():
        raise HTTPException(status_code=403, detail="upload folder must not be a symlink")
    try:
        root.mkdir(parents=True, exist_ok=True)
        if root.resolve() != base.resolve() / _UPLOAD_DIR:
            raise HTTPException(status_code=403, detail="upload folder must not be a symlink")
        dest = root / f"{hashlib.sha256(data).hexdigest()[:16]}-{name}"
        if dest.is_symlink():
            raise HTTPException(status_code=403, detail="upload target must not be a symlink")
        if not (dest.is_file() and dest.stat().st_size == len(data)):
            fd, part = _staging(dest)
            try:
                with open(fd, "wb") as out:
                    out.write(data)
                os.replace(part, dest)
            finally:
                part.unlink(missing_ok=True)
        _record_asset(dest)
    except OSError as exc:  # a full disk or an unwritable data folder: a JSON error, not a raw 500
        raise _disk_error(exc) from exc
    return {"path": str(dest), "name": name, "bytes": len(data)}


@router.post("/upload")
async def upload(request: Request):
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > _MAX_UPLOAD_BODY:
        raise HTTPException(status_code=413, detail="file too large for the viewer")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_UPLOAD_BODY:
            raise HTTPException(status_code=413, detail="file too large for the viewer")
        chunks.append(chunk)
    body = b"".join(chunks)
    content_type = request.headers.get("content-type", "")
    got = _multipart_file(body, content_type) if content_type.startswith("multipart/form-data") else _json_file(body) if body.strip() else None
    if got is None:
        # An older Desktop drops the multipart option and sends nothing: the renderer reads this exact
        # phrase as "retry as JSON" (src/upload.ts::uploadUnsupported).
        raise HTTPException(status_code=400, detail="no file in the request")
    return await run_in_threadpool(_store_upload, *got)
