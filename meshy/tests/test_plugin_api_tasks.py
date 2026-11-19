"""Task lookup, redirects and size caps of the Meshy router, against a loopback stand-in for api.meshy.ai.

Real router code over real HTTP: the router's API base and asset-host allowlist point at loopback servers
(test-only monkeypatches), so redirects, Content-Length and streamed bodies are the real urllib paths. The
stand-in follows Meshy's documented shapes (docs/openapi + the Meshy MCP server's endpoint notes): every
task answers in full on its own route; the generic routes also answer for other tasks, reshaped without
their files, except Creative Lab and Text to Motion, which answer only on their own routes.
"""

from __future__ import annotations

import importlib.util
import json
import os
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi import HTTPException

_SRC = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
_KEY = "msy-test-not-a-real-key"
_CL = ("figure", "lamp", "keychain", "fridge-magnet", "vinyl-figure", "brick-figure", "keycap", "fidget-pixel")
# Meshy's task routes (Meshy MCP server utils/endpoints.js TASK_ENDPOINTS), by the record's ``type``.
_ROUTES = {
    "text-to-3d-preview": "/openapi/v2/text-to-3d", "image-to-3d": "/openapi/v1/image-to-3d",
    "multi-image-to-3d": "/openapi/v1/multi-image-to-3d", "remesh": "/openapi/v1/remesh",
    "retexture": "/openapi/v1/retexture", "rig": "/openapi/v1/rigging", "animate": "/openapi/v1/animations",
    "text-to-image": "/openapi/v1/text-to-image", "image-to-image": "/openapi/v1/image-to-image",
    "convert": "/openapi/v1/convert", "resize": "/openapi/v1/resize", "uv-unwrap": "/openapi/v1/uv-unwrap",
    "print-repair": "/openapi/v1/print/repair", "print-split": "/openapi/v1/print/split",
    "print-multi-color": "/openapi/v1/print/multi-color", "print-analyze": "/openapi/v1/print/analyze",
    "text-to-motion": "/openapi/v1/text-to-motion",
    **{f"creative-lab-{p}-{s}": f"/openapi/creative-lab/{p}/v1/{s}" for p in _CL for s in ("prototype", "build")},
    "creative-lab-fidget-collapsible": "/openapi/creative-lab/fidget-collapsible/v1",
}
_GENERIC = {"/openapi/v2/text-to-3d", "/openapi/v1/image-to-3d", "/openapi/v1/multi-image-to-3d",
            "/openapi/v1/retexture", "/openapi/v1/print/repair"}
_FILES = ("model_urls", "result", "image_urls", "texture_urls", "thumbnail_url")


def _tid(n: int) -> str:
    return f"01a157b8-7317-7551-abc5-{n:012x}"


class _Server:
    """One loopback HTTP server; ``handle(path, headers) -> (status, headers, body)``; records every hit."""

    def __init__(self, handle):
        hits, lock = [], threading.Lock()

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                with lock:
                    hits.append((self.path, self.headers.get("Authorization")))
                status, headers, body = handle(self.path, self.headers)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                if "Content-Length" not in headers and status not in (204, 304):
                    if not headers.get("X-No-Length"):
                        self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_a):
                pass

        self.hits = hits
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.httpd.block_on_close = False
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def url(self, host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self.port}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class _Meshy:
    """api.meshy.ai + assets.meshy.ai stand-in. ``generic=False`` models routes that answer only their own type."""

    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.overrides: dict[str, tuple[int, dict, bytes]] = {}
        self.assets: dict[str, object] = {}
        self.generic = True
        self.owner_key = _KEY
        self.server = _Server(self._handle)

    def add(self, task_type: str, n: int, **fields) -> str:
        task_id = _tid(n)
        self.tasks[task_id] = {"id": task_id, "type": task_type, "status": "SUCCEEDED", "progress": 100,
                               "created_at": 1_000 + n, **fields}
        return task_id

    def meshy_hits(self) -> list[str]:
        return [p for p, _auth in self.server.hits if p.startswith("/openapi/")]

    def _handle(self, raw: str, headers):
        path = raw.split("?", 1)[0]
        if path in self.overrides:
            return self.overrides[path]
        if path.startswith("/assets/"):
            got = self.assets.get(path[len("/assets/"):], 404)
            if isinstance(got, int):
                return got, {}, b""
            return got if isinstance(got, tuple) else (200, {}, got)
        if headers.get("Authorization") != f"Bearer {self.owner_key}":
            return 404, {}, b'{"message": "Not Found"}'
        route, _, last = path.rpartition("/")
        if last in self.tasks:
            row = self.tasks[last]
            own = _ROUTES[row["type"]]
            if route == own:
                return 200, {"Content-Type": "application/json"}, json.dumps(row).encode()
            if self.generic and route in _GENERIC and not row["type"].startswith(("creative-lab-", "text-to-motion")):
                shaped = {k: v for k, v in row.items() if k not in _FILES}
                return 200, {"Content-Type": "application/json"}, json.dumps(shaped).encode()
            return 404, {}, b'{"message": "Not Found"}'
        if path in _ROUTES.values():
            rows = [r for r in self.tasks.values() if _ROUTES[r["type"]] == path]
            return 200, {"Content-Type": "application/json"}, json.dumps(rows).encode()
        return 404, {}, b'{"message": "Not Found"}'


@pytest.fixture
def meshy():
    stand_in = _Meshy()
    foreign = _Server(lambda *_a: (200, {}, b"FOREIGN"))
    stand_in.foreign = foreign
    yield stand_in
    stand_in.server.close()
    foreign.close()


@pytest.fixture
def api(tmp_path, monkeypatch, meshy):
    home, hermes = tmp_path / "home", tmp_path / "hermes"
    data = hermes / "plugin-data" / "agent-plugin-meshy"
    for d in (home, data, tmp_path / "outside"):
        d.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("MESHY_API_KEY", _KEY)

    def tripwire(*_a, **_k):
        raise AssertionError("bare urlopen: every request must go through the router's guarded opener")

    monkeypatch.setattr(urllib.request, "urlopen", tripwire)
    spec = importlib.util.spec_from_file_location("meshy_plugin_api_tasks_under_test", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Test-only: the API base and the asset host are the loopback stand-in (no real Meshy host is reachable).
    monkeypatch.setattr(mod, "_API", meshy.server.url())
    monkeypatch.setattr(mod, "_ASSET_HOSTS", frozenset({"127.0.0.1"}))
    monkeypatch.setattr(mod, "_ASSET_SCHEMES", frozenset({"http"}))
    monkeypatch.setattr(mod, "_api_key", lambda: os.environ.get("MESHY_API_KEY"))
    mod.tmp, mod.data, mod.home = tmp_path, data, home
    return mod


def _status(fn, *args, **kwargs) -> HTTPException:
    with pytest.raises(HTTPException) as err:
        fn(*args, **kwargs)
    return err.value


def _asset(meshy, name: str) -> str:
    return f"{meshy.server.url()}/assets/{name}"


# --- item 2: a card resolves every task type the plugin creates -------------------------------------------

_CASES = [  # type, fields, has_model
    ("convert", {"model_urls": {"glb": "G", "fbx": "F"}}, True),
    ("resize", {"model_urls": {"glb": "G"}}, True),
    ("uv-unwrap", {"model_urls": {"glb": "G"}}, True),
    ("print-repair", {"model_urls": {"glb": "G", "stl": "S"}}, True),
    ("print-split", {"model_urls": {"glb": "G"}, "part_count": 3}, True),
    ("print-multi-color", {"model_urls": {"3mf": "M"}}, True),  # 3MF is viewable (item 13)
    ("text-to-motion", {"result": {"motion_url": "F", "motion_format": "fbx"}}, False),
    ("creative-lab-figure-build", {"model_urls": {"glb": "G", "obj": "O"}}, True),
    ("creative-lab-lamp-prototype", {"image_urls": ["I"]}, False),
    ("creative-lab-fidget-collapsible", {"model_urls": {"glb": "G"}}, True),
]


@pytest.mark.parametrize("generic", [True, False], ids=["generic-routes", "own-routes-only"])
def test_task_card_resolves_every_task_type_and_polls_with_one_call(api, meshy, generic):
    meshy.generic = generic
    for n, (kind, fields, has_model) in enumerate(_CASES):
        fields = {k: ({f: _asset(meshy, f"{n}.{f}") for f in v} if k == "model_urls" else v) for k, v in fields.items()}
        task = meshy.add(kind, n, **fields)
        got = api.task_state(task)
        assert (got["status"], got["type"], got["has_model"]) == ("SUCCEEDED", kind, has_model), kind
        meshy.server.hits.clear()
        api.task_state(task)  # the card's next poll: the owning route, once
        assert meshy.meshy_hits() == [f"{_ROUTES[kind]}/{task}"], kind
    convert = _tid(0)
    assert [f["format"] for f in api.formats(convert)["formats"]] == ["glb", "fbx"]


def test_unknown_task_is_404_and_a_retry_does_not_probe_again(api, meshy, monkeypatch):
    unknown = _tid(999)
    err = _status(api.task_state, unknown)
    assert (err.status_code, err.detail) == (404, "Meshy has no finished task with that id")
    assert len(meshy.meshy_hits()) == len(set(_ROUTES.values()))  # every route once, never twice
    meshy.server.hits.clear()
    assert _status(api.task_state, unknown).status_code == 404
    assert meshy.meshy_hits() == []  # the card's retry within the window costs no Meshy call
    # The verdict expires: a task that shows up later is found.
    monkeypatch.setattr(api, "_MISS_TTL_S", 0.0)
    assert _status(api.task_state, unknown).status_code == 404  # still cached from before the patch
    with api._ROUTE_LOCK:
        api._MISS_CACHE.clear()
    meshy.add("convert", 999, model_urls={"glb": _asset(meshy, "m.glb")})
    assert api.task_state(unknown)["status"] == "SUCCEEDED"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_meshy_outage_is_a_502_never_no_such_task(api, meshy, status):
    task = meshy.add("convert", 1, model_urls={"glb": _asset(meshy, "m.glb")})
    meshy.overrides[f"/openapi/v1/image-to-3d/{task}"] = (status, {}, b"{}")
    assert _status(api.task_state, task).status_code == 502
    # Outage in the probe of the remaining routes (own-route-only Meshy) is a 502 too, and not remembered.
    meshy.generic = False
    meshy.overrides = {f"/openapi/v1/uv-unwrap/{_tid(2)}": (status, {}, b"{}")}
    assert _status(api.task_state, _tid(2)).status_code == 502
    meshy.overrides = {}
    assert api.task_state(task)["has_model"]


def test_lookups_are_per_api_key(api, meshy, monkeypatch):
    """One process serves several profiles: another key's miss never hides this key's task."""
    task = meshy.add("print-repair", 3, model_urls={"glb": _asset(meshy, "m.glb")})
    monkeypatch.setenv("MESHY_API_KEY", "msy-another-profile")
    assert _status(api.task_state, task).status_code == 404
    monkeypatch.setenv("MESHY_API_KEY", _KEY)
    assert api.task_state(task)["has_model"]


def test_route_cache_is_bounded_and_safe_under_concurrent_polls(api, meshy, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(api, "_ROUTE_CACHE_CAP", 3)
    tasks = [meshy.add("resize", 10 + i, model_urls={"glb": _asset(meshy, "m.glb")}) for i in range(6)]
    with ThreadPoolExecutor(max_workers=12) as pool:
        states = list(pool.map(api.task_state, tasks * 4))
    assert all(s["status"] == "SUCCEEDED" for s in states)
    assert len(api._ROUTE_CACHE) <= 3


def test_progress_chip_and_gallery_cover_post_processing_and_creative_lab(api, meshy):
    running = meshy.add("print-repair", 20, status="IN_PROGRESS", progress=40)
    queued = meshy.add("convert", 21, status="PENDING", progress=0)
    import time
    for t in (running, queued):
        meshy.tasks[t]["created_at"] = int(time.time() * 1000)
    active = api.active(tasks=f"print-repair:{running},convert:{queued}")["tasks"]
    assert sorted((t["task_id"], t["task_type"]) for t in active) == [(running, "print-repair"), (queued, "convert")]

    converted = meshy.add("convert", 22, model_urls={"glb": _asset(meshy, "c.glb")})
    figure = meshy.add("creative-lab-figure-build", 23, model_urls={"glb": _asset(meshy, "f.glb")})
    listed = {m["task_id"]: m for m in api.models(limit=10)["models"]}
    assert listed[converted]["task_type"] == "convert" and listed[converted]["glb_url"].endswith("c.glb")
    assert listed[figure]["task_type"] == "creative-lab-figure-build"


def test_gallery_opens_the_glb_when_a_task_has_several_saved_formats(api, meshy):
    task = meshy.add("image-to-3d", 30, model_urls={"glb": _asset(meshy, "m.glb")})
    folder = api.data / "meshy_output" / task
    folder.mkdir(parents=True)
    (folder / ".meshy-task").write_text(task, encoding="utf-8")
    glb, stl = folder / "model.glb", folder / "model.stl"
    glb.write_bytes(b"glTF")
    stl.write_bytes(b"solid")
    os.utime(glb, (1_000_000, 1_000_000))  # the STL was saved last
    row = next(m for m in api.models(limit=10)["models"] if m["task_id"] == task)
    assert row["local_path"] == str(glb)


# --- hardening (b): redirects stay on the allowed host -------------------------------------------------------

def _redirect_to(location: str) -> tuple[int, dict, bytes]:
    return 302, {"Location": location}, b""


@pytest.mark.parametrize("route", ["download", "remote"])
def test_asset_redirect_off_the_asset_host_is_refused(api, meshy, route):
    meshy.assets["m.glb"] = _redirect_to(f"{meshy.foreign.url('localhost')}/m.glb")
    dest = api.data / "model.glb"
    err = _status(api._download, _asset(meshy, "m.glb"), dest) if route == "download" else _status(api.remote, _asset(meshy, "m.glb"))
    assert err.status_code == 502 and "redirect" in err.detail
    assert meshy.foreign.hits == []
    assert not dest.exists() and not list(api.data.glob("*.part"))
    # A hop that stays on the asset host is followed (signed links may bounce within the CDN).
    meshy.assets["m.glb"] = _redirect_to(_asset(meshy, "real.glb"))
    meshy.assets["real.glb"] = b"glTF-real"
    if route == "download":
        api._download(_asset(meshy, "m.glb"), dest)
        assert dest.read_bytes() == b"glTF-real"
    else:
        assert api.remote(_asset(meshy, "m.glb"))["bytes"] == 9


def test_obj_material_redirect_off_the_asset_host_is_refused(api, meshy):
    task = meshy.add("image-to-3d", 40, model_urls={"obj": _asset(meshy, "m.obj"), "mtl": _asset(meshy, "m.mtl")})
    meshy.assets["m.obj"] = b"mtllib material.mtl\nv 0 0 0\n"
    meshy.assets["m.mtl"] = _redirect_to(f"{meshy.foreign.url('localhost')}/m.mtl")
    assert _status(api.save, {"task": task, "format": "obj"}).status_code == 502
    assert meshy.foreign.hits == []
    assert not (api.data / "meshy_output" / task / "model.mtl").exists()


def test_api_redirect_never_carries_the_key_to_another_host(api, meshy):
    task = _tid(41)
    meshy.overrides[f"/openapi/v1/image-to-3d/{task}"] = _redirect_to(f"{meshy.foreign.url('localhost')}/steal")
    assert _status(api.task_state, task).status_code == 502
    assert meshy.foreign.hits == []


# --- hardening (d): size caps leave nothing behind ----------------------------------------------------------

def test_declared_and_counted_size_caps_leave_nothing_on_disk(api, meshy, monkeypatch):
    monkeypatch.setattr(api, "_MAX_SAVE_BYTES", 1000)
    monkeypatch.setattr(api, "_MAX_FILE_BYTES", 1000)
    # Declares a body over the cap: refused before reading it (the stand-in sends 10 bytes and hangs up).
    meshy.assets["declared.glb"] = (200, {"Content-Length": str(10**9)}, b"0123456789")
    # Declares nothing and streams past the cap: refused by the bytes counted.
    meshy.assets["streamed.glb"] = (200, {"X-No-Length": "1"}, b"x" * 5000)
    for name in ("declared.glb", "streamed.glb"):
        dest = api.data / name
        err = _status(api._download, _asset(meshy, name), dest)
        assert err.status_code == 413, name
        assert not dest.exists() and not [p for p in api.data.iterdir() if p.name.endswith(".part")], name
        assert _status(api.remote, _asset(meshy, name)).status_code == 413, name


# --- hardening (c): clean errors, not raw 500s ----------------------------------------------------------------

def test_meshy_answers_of_the_wrong_shape_never_raise(api, meshy):
    meshy.overrides["/openapi/v1/balance"] = (200, {}, b'["not", "an", "object"]')
    assert api.status()["balance"] is None
    task = meshy.add("text-to-3d-preview", 50, progress="n/a", status="IN_PROGRESS", model_type="standard")
    assert api.task_state(task)["progress"] == 0
    assert api.actions(task)["balance"] is None
    meshy.overrides["/openapi/v1/convert"] = (200, {}, b'["junk", 7, null]')
    assert isinstance(api.models(limit=5)["models"], list)
    meshy.overrides[f"/openapi/v1/image-to-3d/{_tid(51)}"] = (200, {}, b"<html>not json</html>")
    assert _status(api.task_state, _tid(51)).status_code == 502


def test_a_task_without_a_glb_is_viewable_in_its_best_format(api, meshy):
    # Meshy's documented Convert record lists only the formats it made; the card's own Convert row makes these.
    task = meshy.add("convert", 7, model_urls={"fbx": _asset(meshy, "c.fbx"), "stl": _asset(meshy, "c.stl")})
    got = api.task_state(task)
    assert (got["has_model"], got["view_format"]) == (True, "fbx")
    assert [r["glb_url"] for r in api.models(limit=60)["models"] if r["task_id"] == task] == [_asset(meshy, "c.fbx")]


def test_a_rejected_key_is_a_permanent_4xx_and_a_plan_gated_route_is_not(api, meshy):
    task = meshy.add("convert", 8, model_urls={"glb": _asset(meshy, "m.glb")})
    # A route this key's plan cannot read answers 403; the task is still found on its own route.
    meshy.overrides[f"/openapi/v1/image-to-3d/{task}"] = (403, {}, b"{}")
    assert api.task_state(task)["has_model"]
    # A revoked key (401 everywhere): a 4xx the card stops on, never a 502 it retries forever.
    meshy.owner_key = "msy_revoked"
    meshy.overrides = {f"/openapi/v1/image-to-3d/{_tid(9)}": (401, {}, b"{}")}
    err = _status(api.task_state, _tid(9))
    assert (err.status_code, err.detail) == (403, "Meshy rejected the API key")


def test_a_refusal_on_every_route_is_meshys_403_never_no_such_task(api, meshy):
    # Meshy's 403 (IP allowlist, org file filtering) on every route: its reason reaches the card as a 403,
    # and it is never remembered as "no such task".
    refused = (403, {}, b'{"message": "ip_not_authorized: 203.0.113.9"}')
    task = _tid(11)
    meshy.overrides = {f"{r}/{task}": refused for r in set(_ROUTES.values())}
    err = _status(api.task_state, task)
    assert err.status_code == 403 and "ip_not_authorized" in err.detail
    with api._ROUTE_LOCK:
        assert not api._MISS_CACHE
    # An outage on one route outranks a refusal elsewhere: the card retries instead of stopping.
    meshy.overrides[f"/openapi/v1/uv-unwrap/{task}"] = (503, {}, b"{}")
    assert _status(api.task_state, task).status_code == 502
    # The task's own route refusing after it was found is a 403 too, never a file-less generic record.
    meshy.overrides = {}
    found = meshy.add("convert", 12, model_urls={"glb": _asset(meshy, "m.glb")})
    assert api.task_state(found)["has_model"]
    meshy.overrides = {f"{_ROUTES['convert']}/{found}": refused}
    assert _status(api.task_state, found).status_code == 403


def test_a_task_with_only_a_blend_still_offers_its_download(api, meshy):
    task = meshy.add("convert", 13, model_urls={"blend": _asset(meshy, "m.blend")})
    got = api.task_state(task)
    assert (got["has_model"], got["has_files"]) == (False, True)


def test_a_plan_gated_route_never_turns_an_unknown_task_into_a_refusal(api, meshy):
    # One product route 403s for every id (the plan lacks it); a deleted task is still "no such task", cached.
    unknown = _tid(14)
    meshy.overrides = {f"/openapi/v1/print/repair/{unknown}": (403, {}, b'{"message": "plan lacks endpoint"}')}
    assert "/openapi/v1/print/repair" in _ROUTES.values()
    assert _status(api.task_state, unknown).status_code == 404
    meshy.server.hits.clear()
    assert _status(api.task_state, unknown).status_code == 404
    assert meshy.meshy_hits() == []


def test_progress_chip_reads_only_the_tasks_it_names(api, meshy):
    """One Meshy read per named task on its own route, none when the chip watches nothing (was 17 list calls)."""
    running = meshy.add("text-to-3d-preview", 30, status="IN_PROGRESS", progress=40, prompt="duck")
    done = meshy.add("rig", 31)
    rigged = meshy.add("rig", 32, status="PENDING", progress=0)
    assert api.active() == {"configured": True, "tasks": [], "done": [], "missing": [], "failed": []}
    assert meshy.meshy_hits() == []

    got = api.active(tasks=f"text-to-3d:{running},rigging:{done},any:{rigged},remesh:{_tid(33)}")
    assert [(t["task_id"], t["task_type"], t["progress"]) for t in got["tasks"]] == [
        (rigged, "rigging", 0), (running, "text-to-3d", 40)]
    assert (got["done"], got["missing"]) == ([done], [_tid(33)])
    hits = meshy.meshy_hits()
    assert f"/openapi/v2/text-to-3d/{running}" in hits and f"/openapi/v1/rigging/{done}" in hits
    assert not any("page_size" in h or h.rstrip("/").endswith(("text-to-3d", "rigging")) for h in hits)
    meshy.server.hits.clear()
    api.active(tasks=f"text-to-3d:{running},any:{rigged}")
    assert len(meshy.meshy_hits()) == 2  # the "any" task's route is cached after its first read


@pytest.mark.parametrize("tasks", ["text-to-3d", f"Text:{_tid(1)}", f"text-to-3d:{_tid(1)}/../balance", "any:../../v1/balance",
                                   ",".join(f"text-to-3d:{_tid(i)}" for i in range(9))])
def test_progress_chip_input_is_checked_before_any_meshy_call(api, meshy, tasks):
    assert _status(api.active, tasks=tasks).status_code == 400
    assert meshy.meshy_hits() == []


def test_progress_chip_stops_on_a_refused_key_and_retries_an_outage(api, meshy):
    task = meshy.add("text-to-3d-preview", 34, status="IN_PROGRESS", progress=10)
    meshy.overrides = {f"/openapi/v2/text-to-3d/{task}": (401, {}, b"{}")}
    assert _status(api.active, tasks=f"text-to-3d:{task}").status_code == 403  # permanent: the chip stops
    meshy.overrides = {f"/openapi/v2/text-to-3d/{task}": (503, {}, b"{}")}
    assert api.active(tasks=f"text-to-3d:{task}")["failed"] == [task]  # transient: the chip retries that task


def test_one_tasks_failed_read_never_fails_the_others(api, meshy):
    """Only a refusal of the key fails the batch. A plan-gated 403 or a 5xx on one task's route, or a status call's
    real-but-wrong task_type, leaves the other rows alone and never turns a running task into done or missing."""
    good = meshy.add("text-to-3d-preview", 50, status="IN_PROGRESS", progress=40)
    gated = meshy.add("print-split", 51, status="IN_PROGRESS", progress=10)
    down = meshy.add("remesh", 52, status="IN_PROGRESS", progress=20)
    named_wrong = meshy.add("text-to-3d-preview", 53, status="IN_PROGRESS", progress=60)
    plan_gated = (403, {}, b'{"message": "Your plan does not include this endpoint"}')
    meshy.overrides = {f"/openapi/v1/print/split/{i}": plan_gated for i in (gated, named_wrong)}
    meshy.overrides[f"/openapi/v1/remesh/{down}"] = (500, {}, b"{}")
    got = api.active(tasks=f"text-to-3d:{good},print-split:{gated},remesh:{down},print-split:{named_wrong}")
    assert [(t["task_id"], t["task_type"], t["progress"]) for t in got["tasks"]] == [
        (named_wrong, "text-to-3d", 60), (good, "text-to-3d", 40)]
    assert sorted(got["failed"]) == sorted([gated, down])
    assert (got["done"], got["missing"]) == ([], [])
    # A wrong route that just does not know the task: found by lookup, never "missing" while it runs.
    meshy.overrides = {}
    got = api.active(tasks=f"rigging:{named_wrong}")
    assert [(t["task_id"], t["task_type"]) for t in got["tasks"]] == [(named_wrong, "text-to-3d")]
    # A refusal on the generic routes every plan reads is about the key or account: the whole poll stops.
    refused = (403, {}, b'{"message": "ip_not_authorized"}')
    meshy.overrides = {f"{r}/{t}": refused for r in set(_ROUTES.values()) for t in (good, down)}
    err = _status(api.active, tasks=f"text-to-3d:{good},any:{down}")
    assert err.status_code == 403 and "ip_not_authorized" in err.detail


def test_a_refused_key_stops_the_chip_even_when_the_tasks_route_is_cached(api, meshy):
    """A card poll caches the task's route, so a later read skips the generic probe; an IP allowlist refusing every
    route must still fail the whole chip request, while a refusal on the task's own route alone stays that task's."""
    task = meshy.add("convert", 54, status="IN_PROGRESS", progress=30)
    other = meshy.add("text-to-3d-preview", 55, status="IN_PROGRESS", progress=50)
    assert api.task_state(task)["status"] == "IN_PROGRESS"  # route cached
    meshy.overrides = {f"{_ROUTES['convert']}/{task}": (403, {}, b'{"message": "plan lacks convert"}')}
    got = api.active(tasks=f"convert:{task},text-to-3d:{other}")
    assert (got["failed"], [t["task_id"] for t in got["tasks"]]) == ([task], [other])
    # The same for a task whose own route is a generic one: one route refusing it is not the key.
    assert api.task_state(other)["status"] == "IN_PROGRESS"
    meshy.overrides = {f"{_ROUTES['text-to-3d-preview']}/{other}": (403, {}, b'{"message": "file filtered"}')}
    assert api.active(tasks=f"any:{other}")["failed"] == [other]
    refused = (403, {}, b'{"message": "ip_not_authorized"}')
    meshy.overrides = {f"{r}/{t}": refused for r in set(_ROUTES.values()) for t in (task, other)}
    for tasks in (f"convert:{task},text-to-3d:{other}", f"any:{task}", f"any:{other}"):
        err = _status(api.active, tasks=tasks)
        assert err.status_code == 403 and "ip_not_authorized" in err.detail, tasks


def test_progress_chip_reads_share_a_cap_across_windows(api, meshy, monkeypatch):
    """Each window's chip polls on its own; together their Meshy reads stay within _FANOUT in flight."""
    ids = [meshy.add("text-to-3d-preview", 60 + i, status="IN_PROGRESS") for i in range(api._ACTIVE_MAX)]
    real, lock, live = api._meshy_get, threading.Lock(), [0, 0]

    def counted(*args, **kwargs):
        with lock:
            live[0] += 1
            live[1] = max(live[1], live[0])
        try:
            threading.Event().wait(0.05)
            return real(*args, **kwargs)
        finally:
            with lock:
                live[0] -= 1

    monkeypatch.setattr(api, "_meshy_get", counted)
    batch = ",".join(f"text-to-3d:{i}" for i in ids)
    windows = [threading.Thread(target=api.active, kwargs={"tasks": batch}, daemon=True) for _ in range(3)]
    for w in windows:
        w.start()
    for w in windows:
        w.join(30)
    assert not any(w.is_alive() for w in windows)
    assert 1 < live[1] <= api._FANOUT


def test_status_names_a_403_refusal_instead_of_blaming_the_key(api, meshy):
    meshy.overrides = {"/openapi/v1/balance": (403, {}, b'{"message": "Request rejected: IP not allowed"}')}
    got = api.status()
    assert (got["error"], got["error_code"]) == ("Meshy refused the request: Request rejected: IP not allowed", "refused")
    meshy.overrides = {"/openapi/v1/balance": (401, {}, b"{}")}
    assert (api.status()["error"], api.status()["error_code"]) == ("Meshy rejected the API key", "key_rejected")
    meshy.overrides = {"/openapi/v1/balance": (503, {}, b"{}")}
    assert api.status()["error_code"] == "offline"


def test_progress_chip_finds_a_task_named_with_a_wrong_kind(api, meshy):
    # Meshy's server accepts a wrong task_type on a status call; one such entry must not fail the whole batch.
    task = meshy.add("text-to-motion", 35, status="IN_PROGRESS", progress=70)
    got = api.active(tasks=f"preview:{task}")
    assert [(t["task_id"], t["task_type"]) for t in got["tasks"]] == [(task, "text-to-motion")]


def test_progress_chip_lookups_never_starve_the_shared_probe_slots(api, meshy, monkeypatch):
    """Four lookups of ids no generic route knows run _task's own fan-out; they must finish, and a card's
    lookup alongside them too (holding a shared slot while waiting for one deadlocked every probe).

    Fails instead of hanging if that deadlock returns: the slots are plain semaphores this test can flood open
    once it has waited long enough, so every stuck worker finishes and the pool can shut down."""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
    slots = [threading.Semaphore(api._FANOUT), threading.Semaphore(api._FANOUT)]
    monkeypatch.setattr(api, "_FANOUT_SLOTS", slots[0])
    monkeypatch.setattr(api, "_ACTIVE_SLOTS", slots[1])
    ids = [meshy.add("text-to-motion", 40 + i, status="IN_PROGRESS") for i in range(api._ACTIVE_MAX)]
    card = meshy.add("creative-lab-figure-build", 49, model_urls={"glb": _asset(meshy, "f.glb")})
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        chip = pool.submit(api.active, tasks=",".join(f"any:{i}" for i in ids))
        state = pool.submit(api.task_state, card)
        try:
            assert len(chip.result(timeout=60)["tasks"]) == api._ACTIVE_MAX
            assert state.result(timeout=60)["has_model"]
        except FutureTimeout:
            pytest.fail("progress chip lookups deadlocked on the shared probe slots")
    finally:
        for sem in slots:
            for _ in range(256):
                sem.release()
        pool.shutdown(wait=True, cancel_futures=True)


def test_one_products_failed_list_never_fails_the_gallery(api, meshy):
    """Only a refusal of the key fails /models. A plan-gated product (403) or one route's outage is named per
    product in ``failed`` and every other product still lists."""
    shown = meshy.add("text-to-3d-preview", 70, model_urls={"glb": _asset(meshy, "a.glb")})
    image = meshy.add("text-to-image", 71, image_urls=[_asset(meshy, "a.png")])
    meshy.add("creative-lab-lamp-build", 72, model_urls={"glb": _asset(meshy, "l.glb")})
    meshy.add("remesh", 73, model_urls={"glb": _asset(meshy, "r.glb")})
    meshy.overrides = {_ROUTES["creative-lab-lamp-build"]: (403, {}, b'{"message": "Your plan does not include Creative Lab"}'),
                       _ROUTES["remesh"]: (503, {}, b"{}")}
    got = api.models(limit=10)
    assert {m["task_id"] for m in got["models"]} == {shown, image}
    assert sorted((f["task_type"], f["error"]) for f in got["failed"]) == [
        ("creative-lab-lamp-build", "Meshy refused the request: Your plan does not include Creative Lab"),
        ("remesh", "Meshy API error 503")]
    meshy.overrides = {}
    assert api.models(limit=10)["failed"] == []


def test_a_refused_key_or_a_total_outage_still_fails_the_gallery(api, meshy):
    meshy.add("text-to-3d-preview", 74, model_urls={"glb": _asset(meshy, "a.glb")})
    meshy.overrides = {_ROUTES["remesh"]: (401, {}, b"{}")}
    err = _status(api.models, limit=10)
    assert (err.status_code, err.detail) == (502, "Meshy rejected the API key")
    # Both generic routes every plan reads refusing is the key's or the account's (an IP allowlist), not a product's.
    refused = (403, {}, b'{"message": "ip_not_authorized"}')
    meshy.overrides = {_ROUTES["image-to-3d"]: refused, _ROUTES["text-to-3d-preview"]: refused}
    err = _status(api.models, limit=10)
    assert err.status_code == 502 and "ip_not_authorized" in err.detail
    # One of them alone (file filtering on that route) is that product's, as the task lookup treats it.
    meshy.overrides = {_ROUTES["image-to-3d"]: refused}
    got = api.models(limit=10)
    assert len(got["models"]) == 1 and [f["task_type"] for f in got["failed"]] == ["image-to-3d"]
    # Nothing listed at all: the gallery says Meshy is down instead of showing an empty list with 29 notices.
    meshy.overrides = {r: (503, {}, b"{}") for r in set(_ROUTES.values())}
    assert _status(api.models, limit=10).detail == "Meshy API error 503"


def test_details_keeps_the_recorded_lineage_when_only_meshys_record_fails(api, meshy):
    """The Made-from panel (and the gallery's chat buttons, which read its session) is local provenance plus
    Meshy's record: a plan-gated route or an outage on that one read leaves the recorded fields and names why;
    a refused key still fails it."""
    task = meshy.add("creative-lab-lamp-build", 75, model_urls={"glb": _asset(meshy, "l.glb")}, prompt="lamp")
    (api.data / "provenance.json").write_text(json.dumps(
        {task: {"tool": "meshy_creative_lab", "prompt": "a moon lamp", "session_id": "sess-1"}}), encoding="utf-8")
    assert api.details(task)["remote_error"] is None  # route cached
    meshy.overrides = {f"{_ROUTES['creative-lab-lamp-build']}/{task}": (403, {}, b'{"message": "plan lacks Creative Lab"}')}
    got = api.details(task)
    assert (got["prompt"], got["session_id"]) == ("a moon lamp", "sess-1")
    assert got["remote_error"] == "Meshy refused the request: plan lacks Creative Lab"
    meshy.overrides = {f"{_ROUTES['creative-lab-lamp-build']}/{task}": (502, {}, b"{}")}
    assert (api.details(task)["session_id"], api.details(task)["remote_error"]) == ("sess-1", "Meshy API error 502")
    meshy.overrides = {f"{_ROUTES['creative-lab-lamp-build']}/{task}": (401, {}, b"{}")}
    assert _status(api.details, task).detail == "Meshy rejected the API key"


def test_concurrent_lookups_of_an_unknown_task_probe_every_route_once_in_total(api, meshy, monkeypatch):
    """A card mounting twice, the chip and a retry ask for the same unknown id together: one probe of every route,
    not one per caller (live: 4 callers x 34 routes in 5 s drew Meshy 429s past its 20 req/s per key)."""
    real, gate = api._meshy_get, threading.Barrier(4, timeout=10)

    def slow(*args, **kwargs):  # Meshy's latency: every caller arrives before the first verdict
        threading.Event().wait(0.02)
        return real(*args, **kwargs)

    monkeypatch.setattr(api, "_meshy_get", slow)
    unknown = _tid(4242)

    def ask():
        gate.wait()
        return _status(api.task_state, unknown).status_code

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = list(pool.map(lambda _i: ask(), range(4)))
    assert codes == [404] * 4
    assert len(meshy.meshy_hits()) == len(set(_ROUTES.values()))


def test_made_from_never_shows_a_type_named_project_or_credits_the_task_itself(api, meshy):
    """The Meshy MCP server names a prompt-less project after its task type (``convert``) and starts its chain at
    that task: Made-from shows no prompt rather than \"convert (from the original <itself>)\"."""
    task = meshy.add("convert", 80, model_urls={"3mf": _asset(meshy, "m.3mf")})
    folder = api.data / "meshy_output" / f"20261024_044847_convert_{task[:8]}"
    folder.mkdir(parents=True)
    (folder / "metadata.json").write_text(json.dumps(
        {"project_name": "convert", "tasks": [{"task_id": task, "task_type": "convert", "stage": ""}]}), encoding="utf-8")
    got = api.details(task)
    assert (got["prompt"], got["prompt_from"]) == (None, None)
    # Control: a project named after its prompt still lends it to a later step, credited to the first task.
    preview = meshy.add("text-to-3d-preview", 81, model_urls={"glb": _asset(meshy, "p.glb")})
    remesh = meshy.add("remesh", 82, model_urls={"glb": _asset(meshy, "r.glb")})
    other = api.data / "meshy_output" / "20261024_050000_barrel"
    other.mkdir(parents=True)
    (other / "metadata.json").write_text(json.dumps({"project_name": "a small wooden barrel", "tasks": [
        {"task_id": preview, "task_type": "text-to-3d", "stage": "preview"},
        {"task_id": remesh, "task_type": "remesh", "stage": ""}]}), encoding="utf-8")
    got = api.details(remesh)
    assert (got["prompt"], got["prompt_from"]) == ("a small wooden barrel", preview)
    # The first task keeps its own project prompt when Meshy's record has none, credited to no other task.
    got = api.details(preview)
    assert (got["prompt"], got["prompt_from"]) == ("a small wooden barrel", None)


def test_a_brief_miss_on_the_owning_route_never_hides_the_finished_model(api, meshy):
    """A generic route knows a rig task but its own (rigging) route 404s once: the generic record lacks the files,
    so it is never cached, and the next poll asks the owning route again and finds them."""
    task = meshy.add("rig", 40, result={"rigged_character_glb_url": _asset(meshy, "rig.glb")})
    meshy.overrides = {f"/openapi/v1/rigging/{task}": (404, {}, b"{}")}
    # Finished, files unknown: a 502 the card retries, never a final "SUCCEEDED, no model" that ends its polling.
    assert _status(api.task_state, task).status_code == 502
    assert _status(api.formats, task).status_code == 502
    running = meshy.add("rig", 42, status="IN_PROGRESS", progress=50)
    meshy.overrides[f"/openapi/v1/rigging/{running}"] = (404, {}, b"{}")
    assert api.task_state(running)["progress"] == 50  # a running task's progress needs no files
    meshy.overrides = {}
    assert api.task_state(task)["has_model"]
    assert [f["format"] for f in api.formats(task)["formats"]] == ["glb"]
    meshy.server.hits.clear()
    api.task_state(task)  # now resolved: the owning route is cached, one call per poll
    assert meshy.meshy_hits() == [f"/openapi/v1/rigging/{task}"]


def test_an_outage_during_the_lookup_never_caches_no_such_task(api, meshy):
    """The cached route loses the task while another route is down: that read is a 502 (the card retries), and
    no "no such task" verdict survives it, so the read after recovery asks Meshy and finds the task. A lookup
    that reads every route and finds nothing still caches its 404."""
    task = meshy.add("image-to-3d", 41, status="IN_PROGRESS", progress=30)
    assert api.task_state(task)["status"] == "IN_PROGRESS"
    meshy.overrides = {f"/openapi/v1/image-to-3d/{task}": (404, {}, b"{}"),
                       f"/openapi/v2/text-to-3d/{task}": (503, {}, b"{}")}
    assert _status(api.task_state, task).status_code == 502
    meshy.overrides = {}
    meshy.server.hits.clear()
    assert api.task_state(task)["status"] == "IN_PROGRESS"
    assert meshy.meshy_hits()  # read from Meshy, not a cached miss

    del meshy.tasks[task]  # really gone: the full lookup's 404 is remembered for the retry window
    assert _status(api.task_state, task).status_code == 404
    meshy.server.hits.clear()
    assert _status(api.task_state, task).status_code == 404
    assert meshy.meshy_hits() == []
