"""Safety invariants of the Meshy dashboard router: what it will read, fetch, write and remember.

Real router code against a temp Hermes home + temp user home. The only stand-in is a network tripwire:
every refusal below must happen BEFORE the router reaches Meshy, so any urlopen call fails the test.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastapi import HTTPException

_SRC = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
_TASK = "01a157b8-7317-7551-abc5-f99fe8c1236c"


@pytest.fixture
def api(tmp_path, monkeypatch):
    home, hermes = tmp_path / "home", tmp_path / "hermes"
    data = hermes / "plugin-data" / "agent-plugin-meshy"
    for d in (home, data, tmp_path / "outside"):
        d.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("MESHY_API_KEY", "msy-test-not-a-real-key")  # key present: only validation can stop a call

    def tripwire(*_a, **_k):
        raise AssertionError("network call reached before input validation")

    monkeypatch.setattr(urllib.request, "urlopen", tripwire)
    monkeypatch.setattr(urllib.request, "build_opener", tripwire)  # the router's own opener (redirect guard)
    spec = importlib.util.spec_from_file_location("meshy_plugin_api_under_test", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.tmp = tmp_path
    mod.data = data
    return mod


class _Resp(io.BytesIO):
    """An asset response as the router reads it: a body and its headers."""
    headers: dict = {}


def _status(fn, *args, **kwargs) -> int:
    with pytest.raises(HTTPException) as err:
        fn(*args, **kwargs)
    return err.value.status_code


def test_file_reads_only_models_and_images_inside_allowed_roots(api):
    model = api.data / "meshy_output" / "abc" / "model.glb"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"glTF-binary")
    assert api.file(str(model))["name"] == "model.glb"

    secret = api.data / ".env"
    secret.write_text("MESHY_API_KEY=x")
    exe = api.data / "tool.exe"
    exe.write_bytes(b"MZ")
    outside = api.tmp / "outside" / "stolen.glb"
    outside.write_bytes(b"glTF")
    assert _status(api._resolve_readable, "model.glb") == 400  # relative
    assert _status(api._resolve_readable, str(secret)) == 415
    assert _status(api._resolve_readable, str(exe)) == 415
    assert _status(api.file, str(outside)) == 403

    link = api.data / "innocent.glb"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges on this Windows runner")
    assert _status(api.file, str(link)) == 403  # resolved target decides, not the link's location


def test_save_and_formats_validate_input_before_calling_meshy(api):
    outside = api.tmp / "outside" / "m.glb"
    outside.write_bytes(b"glTF")
    note = api.data / "notes.txt"
    note.write_text("x")
    assert _status(api.save, {"task": _TASK, "format": "exe"}) == 400
    for bad_task in ("../../etc", "..\\..\\Windows", "", _TASK + "/../x"):
        assert _status(api.save, {"task": bad_task, "format": "stl"}) == 400
        assert _status(api.formats, bad_task) == 400
    assert _status(api.save, {"task": _TASK, "format": "stl", "beside": str(outside)}) == 403
    assert _status(api.formats, _TASK, beside=str(outside)) == 403
    assert _status(api.save, {"task": _TASK, "format": "stl", "beside": str(note)}) == 415
    assert not (api.data / "meshy_output").exists()  # nothing written for a refused request


def test_remote_fetches_only_https_assets_meshy_ai(api):
    for url in ("https://assets.meshy.ai.evil.com/m.glb", "http://assets.meshy.ai/m.glb",
                "https://assets.meshy.ai@evil.com/m.glb", "https://evil.com/assets.meshy.ai/m.glb",
                "https://evilassets.meshy.ai/m.glb", "file:///etc/passwd"):
        assert _status(api.remote, url) == 400, url
    assert _status(api.remote, "https://assets.meshy.ai/x/secrets.env") == 415


@pytest.mark.parametrize("status", [401, 403, 404, 410, 429, 503])
@pytest.mark.parametrize("operation", ["preview", "download"])
def test_asset_failures_never_claim_the_api_key_was_rejected(api, monkeypatch, status, operation):
    url = "https://assets.meshy.ai/model.glb"

    def fail(*_args, **_kwargs):
        raise urllib.error.HTTPError(url, status, "asset error", {}, None)

    monkeypatch.setattr(api, "_open", fail)
    with pytest.raises(HTTPException) as err:
        if operation == "preview":
            api.remote(url)
        else:
            api._download(url, api.data / "model.glb")
    assert "key" not in err.value.detail.lower()
    assert err.value.status_code == (410 if status in (401, 403, 404, 410) else 502)
    if status == 403:
        # API authentication remains a different failure from a denied CDN signature.
        assert "API key" in api._meshy_error(urllib.error.HTTPError(url, 401, "", {}, None)).detail


def test_gallery_includes_image_sets_expiry_and_local_fallback(api, monkeypatch):
    image_id = "01a15965-4a40-718b-8912-4046ede4837f"
    image2_id = "01a15965-4a40-718b-8912-4046ede48380"
    local = api.data / "ship.glb"
    local.write_bytes(b"local copy")
    api.file(str(local), task=_TASK)
    monkeypatch.setattr(api, "_api_key", lambda: "offline-test-key")
    monkeypatch.setattr(api.time, "time", lambda: 1000)
    records = {
        "/openapi/v1/image-to-3d": [{"id": _TASK, "status": "SUCCEEDED", "created_at": 100,
                                    "expires_at": 999000, "consumed_credits": 15,
                                    "model_urls": {"glb": "https://assets.meshy.ai/ship.glb"}}],
        "/openapi/v1/text-to-image": [{"id": image_id, "status": "SUCCEEDED", "created_at": 200,
                                      "expires_at": 1100000, "consumed_credits": 3,
                                      "image_urls": ["https://assets.meshy.ai/front.png", "https://assets.meshy.ai/back.png"]}],
        "/openapi/v1/image-to-image": [{"id": image2_id, "status": "SUCCEEDED", "created_at": 300,
                                       "expires_at": 0, "image_urls": ["https://assets.meshy.ai/edit.png"]}],
    }
    monkeypatch.setattr(api, "_meshy_get", lambda path, *_a, **_k: records.get(path.split("?")[0], []))
    result = api.models(limit=10)["models"]
    assert [r["task_id"] for r in result] == [image2_id, image_id, _TASK]
    model, image, edited = result[2], result[1], result[0]
    assert model["kind"] == "model" and model["expired"]
    assert Path(model["local_path"]) == local.resolve()
    assert model["credits"] == 15  # available without the team-only Usage API
    assert image["kind"] == "image" and not image["expired"]
    assert len(image["image_urls"]) == 2 and image["glb_url"] is None
    assert not edited["expired"]  # zero/absent expiry is not evidence of expiration


def test_asset_index_records_only_allowed_model_paths_and_never_urls(api):
    project = Path.home() / "projects" / "demo" / "ship.glb"
    project.parent.mkdir(parents=True)
    project.write_bytes(b"glTF-ship")
    outside = api.tmp / "outside" / "x.glb"
    outside.write_bytes(b"glTF")

    api.file(str(project), task=_TASK)
    api._record_asset(outside, _TASK)
    api._record_asset(project.with_suffix(".png"), None)
    api.file(str(project), task="https://assets.meshy.ai/signed?Expires=1")

    rows = json.loads(api._index_path().read_text(encoding="utf-8"))
    assert [Path(r["path"]) for r in rows] == [project.resolve()]
    assert rows[0]["task_id"] == _TASK and len(rows[0]["sha256"]) == 64
    assert "http" not in api._index_path().read_text(encoding="utf-8")
    listed = api.local(limit=50)["files"]
    assert [(Path(f["path"]), f["task"]) for f in listed] == [(project.resolve(), _TASK)]


def test_actions_state_a_real_cost_and_only_what_the_task_supports(api, monkeypatch):
    """Card buttons send a chat message naming the cost; a task type the tool can't take gets no button."""
    records = {"preview": {"id": _TASK, "type": "text-to-3d-preview", "consumed_credits": 20, "prompt": "owl"},
               "image": {"id": _TASK, "type": "image-to-3d", "consumed_credits": 15},
               "rig": {"id": _TASK, "type": "rig", "consumed_credits": 5}}
    for kind, row in records.items():
        monkeypatch.setattr(api, "_task", lambda _t, _k, row=row: row)
        monkeypatch.setattr(api, "_meshy_get", lambda *_a, **_k: {"balance": 12})
        acts = {a["id"]: a for a in api.actions(_TASK)["actions"]}
        assert all(isinstance(a["cost"], int) and (f"{a['cost']} credit" in a["message"] or a["cost"] == 0)
                   for a in acts.values()), kind
        if kind == "preview":
            assert acts["regenerate"]["cost"] == 20 and not acts["regenerate"]["affordable"]  # 20 > balance 12
            assert "texture" in acts and "rig" not in acts
        if kind == "image":
            assert "regenerate" not in acts and "texture" not in acts and "rig" in acts
        if kind == "rig":
            assert list(acts) == ["animate"]
    assert _status(api.actions, "not-a-task-id") == 400


def _sessions(api, *ids):
    """This profile's state.db with these chats (the real table name and key column)."""
    import sqlite3
    with sqlite3.connect(api.tmp / "hermes" / "state.db") as db:
        db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY)")
        db.executemany("INSERT OR IGNORE INTO sessions VALUES (?)", [(i,) for i in ids])


def test_provenance_keeps_what_made_a_model_and_never_a_signed_url(api, monkeypatch):
    """The gallery's "Made from": a child step shows its origin's prompt; tokens, junk and another
    profile's chats are never stored."""
    parent, child = _TASK, "01a15965-4a40-718b-8912-4046ede4837f"
    _sessions(api, "sess-1")
    api.provenance({"task_id": parent, "tool": "meshy_image_to_3d", "session_id": "sess-1",
                    "args": {"image_url": "https://cdn.example.com/frog.png?Signature=SECRET&Expires=9",
                             "texture_prompt": "mossy green frog", "model_type": "smart-topology", "target_polycount": 15000}})
    api.provenance({"task_id": child, "tool": "meshy_remesh", "session_id": "sess-1",
                    "args": {"input_task_id": parent, "target_polycount": 8000, "topology": "triangle"}})
    stored = api._prov_path().read_text(encoding="utf-8")
    assert "SECRET" not in stored and "Signature" not in stored
    for bad in ({"task_id": "x", "tool": "meshy_rig"}, {"task_id": child, "tool": "rm -rf"}):
        assert _status(api.provenance, bad) == 400
    other = {"task_id": "01a15965-4a40-718b-8912-000000000001", "tool": "meshy_rig", "args": {}}
    assert _status(api.provenance, {**other, "session_id": "a-chat-on-another-profile"}) == 409
    assert _status(api.provenance, other) == 409

    monkeypatch.setattr(api, "_api_key", lambda: None)  # no key: details still answers from the record
    d = api.details(child)
    assert d["texture_prompt"] == "mossy green frog" and d["prompt_from"] == parent
    assert d["image"] == "https://cdn.example.com/frog.png" and d["settings"]["target_polycount"] == 8000
    assert [p["task_id"] for p in d["parents"]] == [parent]
    assert [c["task_id"] for c in api.details(parent)["children"]] == [child]
    assert api.details(parent)["session_id"] == "sess-1"


def test_image_details_recovers_provider_prompt_without_local_provenance(api, monkeypatch):
    monkeypatch.setattr(api, "_api_key", lambda: "offline-test-key")
    def get(path, *_a, **_k):
        if path == f"/openapi/v1/text-to-image/{_TASK}":
            return {"id": _TASK, "type": "text-to-image", "prompt": "low poly mech concept"}
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)
    monkeypatch.setattr(api, "_meshy_get", get)
    assert api.details(_TASK)["prompt"] == "low poly mech concept"


def test_gallery_hides_expired_unsaved_assets_and_deduplicates_tasks(api, monkeypatch):
    monkeypatch.setattr(api, "_api_key", lambda: "offline-test-key")
    monkeypatch.setattr(api.time, "time", lambda: 1000)
    current = {"id": _TASK, "type": "image-to-image", "status": "SUCCEEDED", "created_at": 100,
               "expires_at": 2000000, "image_urls": ["https://assets.meshy.ai/current.png"]}
    old = {**current, "id": "01a15965-4a40-718b-8912-4046ede4837f", "created_at": 200,
           "expires_at": 999000}
    def get(path, *_a, **_k):
        if path.split("?")[0] in {"/openapi/v1/text-to-image", "/openapi/v1/image-to-image"}:
            return [old, current]
        return []
    monkeypatch.setattr(api, "_meshy_get", get)
    result = api.models(limit=1)["models"]
    assert len(result) == 1  # expired rows must not consume the visible limit
    assert result[0]["task_id"] == _TASK
    assert result[0]["task_type"] == "image-to-image"


def test_gallery_dedup_prefers_owning_endpoint_animation_outputs(api, monkeypatch):
    monkeypatch.setattr(api, "_api_key", lambda: "offline-test-key")
    generic = {"id": _TASK, "type": "rig", "status": "SUCCEEDED", "created_at": 100,
               "model_urls": {"glb": "https://assets.meshy.ai/bind-pose.glb"}}
    rig = {**generic, "model_urls": None,
           "result": {"rigged_character_glb_url": "https://assets.meshy.ai/bind-pose.glb",
                      "basic_animations": {"walking_glb_url": "https://assets.meshy.ai/walking.glb"}}}

    def get(path, *_a, **_k):
        endpoint = path.split("?")[0]
        if endpoint == "/openapi/v2/text-to-3d":
            return [generic]
        if endpoint == "/openapi/v1/rigging":
            return [rig]
        return []

    monkeypatch.setattr(api, "_meshy_get", get)
    rows = api.models(limit=10)["models"]
    assert len(rows) == 1
    assert rows[0]["task_type"] == "rigging"
    assert rows[0]["glb_url"] == "https://assets.meshy.ai/walking.glb"


def test_dangling_model_link_does_not_break_the_gallery(api, monkeypatch):
    folder = api.data / "meshy_output" / "project"
    folder.mkdir(parents=True)
    (folder / "gone.glb").symlink_to(folder / "missing.glb")
    (folder / "model.glb").write_bytes(b"model")
    monkeypatch.setattr(api, "_api_key", lambda: "offline-test-key")
    monkeypatch.setattr(api, "_meshy_get", lambda *_a, **_k: [])
    assert [f["name"] for f in api.local(limit=50)["files"]] == ["model.glb"]
    assert api.models(limit=10)["models"] == []


def test_storage_counts_companions_and_indexed_files_once_without_network(api):
    folder = api.data / "meshy_output" / "project"
    folder.mkdir(parents=True)
    model = folder / "model.glb"
    model.write_bytes(b"model")
    (folder / "texture.png").write_bytes(b"texture")
    (folder / "interrupted.part").write_bytes(b"partial")
    api._record_asset(model, _TASK)
    summary = api.storage()
    assert summary["files"] == 2 and summary["bytes"] == 12
    assert Path(summary["directory"]) == api.data


@pytest.mark.parametrize("task", ["", "../outside", "https://assets.meshy.ai/x.png"])
def test_image_download_validates_task_before_network(api, task):
    assert _status(api.save_images, {"task": task}) == 400


def test_image_download_saves_all_views_and_reuses_files_offline(api, monkeypatch):
    monkeypatch.setattr(api, "_api_key", lambda: "offline-test-key")
    urls = ["https://assets.meshy.ai/front.png?Signature=SECRET", "https://assets.meshy.ai/back.webp"]
    monkeypatch.setattr(api, "_task", lambda *_a, **_k: {"prompt": "Mech", "image_urls": urls})
    calls = []
    def download(url, dest):
        calls.append(url)
        dest.write_bytes(b"image")
    monkeypatch.setattr(api, "_download", download)
    result = api.save_images({"task": _TASK})
    assert result["bytes"] == 10 and len(result["paths"]) == 2
    api.save_images({"task": _TASK})
    assert len(calls) == 2
    record = Path(result["directory"]) / "images.json"
    assert "SECRET" not in record.read_text() and "https://" not in record.read_text()
    monkeypatch.setattr(api, "_api_key", lambda: None)
    local = api.local(limit=200)["files"]
    assert len(local) == 1 and local[0]["task"] == _TASK
    assert local[0]["images"] == result["paths"]
    assert api.file(local[0]["path"])["bytes"] == 5


def test_image_download_partial_success_is_preserved_and_retry_resumes(api, monkeypatch):
    monkeypatch.setattr(api, "_api_key", lambda: "offline-test-key")
    monkeypatch.setattr(api, "_task", lambda *_a, **_k: {"image_urls": ["https://assets.meshy.ai/a.png", "https://assets.meshy.ai/b.png"]})
    def download(url, dest):
        if url.endswith("b.png"):
            raise HTTPException(status_code=502, detail="Temporary failure")
        dest.write_bytes(b"first")
    monkeypatch.setattr(api, "_download", download)
    assert _status(api.save_images, {"task": _TASK}) == 502
    assert len(api.local(limit=200)["files"][0]["images"]) == 1
    assert api.local(limit=200)["files"][0]["download_incomplete"]
    monkeypatch.setattr(api, "_download", lambda _url, dest: dest.write_bytes(b"second"))
    saved = api.save_images({"task": _TASK})
    assert len(api.local(limit=200)["files"][0]["images"]) == 2
    assert not api.local(limit=200)["files"][0]["download_incomplete"]
    assert Path(saved["paths"][0]).read_bytes() == b"first"


@pytest.mark.parametrize("urls,expected", [([], 404), (["https://example.com/image.png"], 502)])
def test_image_download_rejects_missing_outputs_and_foreign_hosts_before_writing(api, monkeypatch, urls, expected):
    monkeypatch.setattr(api, "_api_key", lambda: "offline-test-key")
    monkeypatch.setattr(api, "_task", lambda *_a, **_k: {"image_urls": urls})
    assert _status(api.save_images, {"task": _TASK}) == expected
    assert not api._saved_assets_root().exists()


def test_image_lookup_uses_image_endpoint_response(api, monkeypatch):
    """A generic route answers reshaped (no images); the record's own route has them."""
    monkeypatch.setattr(api, "_api_key", lambda: "offline-test-key")
    def get(path, *_a, **_k):
        if path.startswith("/openapi/v1/text-to-image/"):
            return {"id": _TASK, "type": "text-to-image", "image_urls": ["https://assets.meshy.ai/a.png"]}
        return {"id": _TASK, "type": "text-to-image"}
    monkeypatch.setattr(api, "_meshy_get", get)
    monkeypatch.setattr(api, "_download", lambda _url, dest: dest.write_bytes(b"image"))
    assert len(api.save_images({"task": _TASK})["paths"]) == 1



def test_image_download_ignores_preexisting_temporary_symlinks(api, monkeypatch):
    monkeypatch.setattr(api, "_api_key", lambda: "offline-test-key")
    monkeypatch.setattr(api, "_task", lambda *_a, **_k: {"image_urls": ["https://assets.meshy.ai/a.png"]})
    folder = api._saved_assets_root() / _TASK
    folder.mkdir(parents=True)
    victim = api.tmp / "outside" / "keep.txt"
    victim.write_text("unchanged")
    try:
        (folder / "image-1.png.part").symlink_to(victim)
        (folder / "images.json.part").symlink_to(victim)
    except OSError:
        pytest.skip("symlinks unavailable")
    monkeypatch.setattr(api, "_open", lambda *_a, **_k: _Resp(b"image"))
    saved = api.save_images({"task": _TASK})
    assert victim.read_text() == "unchanged"
    assert Path(saved["paths"][0]).read_bytes() == b"image"
    assert json.loads((folder / "images.json").read_text())["complete"]



@pytest.mark.parametrize("kind,formats,preview", [
    ("image-to-3d", {"glb": "https://assets.meshy.ai/a.glb"}, False),
    ("text-to-3d-preview", {"glb": "https://assets.meshy.ai/a.glb"}, True),
    ("text-to-3d-refine", {"glb": "https://assets.meshy.ai/a.glb", "fbx": "https://assets.meshy.ai/a.fbx"}, False),
])
def test_formats_separates_existing_downloads_from_priced_conversions(api, monkeypatch, kind, formats, preview):
    monkeypatch.setattr(api, "_api_key", lambda: "offline-test-key")
    monkeypatch.setattr(api, "_task", lambda *_a, **_k: {"id": _TASK, "type": kind, "model_urls": formats})
    result = api.formats(_TASK)
    assert {r["format"] for r in result["formats"]} == set(formats)
    assert result["is_preview"] == preview
    assert {r["format"] for r in result["conversions"]} == {"glb", "fbx", "obj", "stl", "usdz", "blend", "3mf"} - set(formats)
    assert all(r["cost"] == 1 and "meshy_convert" in r["message"] and _TASK in r["message"] for r in result["conversions"])


def test_parallel_writes_keep_every_record_and_tasks_get_their_own_folder(api):
    """Completions of a parallel tool_call arrive together, as do /file reads; two tasks a millisecond
    apart share their first 8 characters."""
    from concurrent.futures import ThreadPoolExecutor
    _sessions(api, "s")
    ids = [f"01a15976-d838-7532-99d8-{i:012x}" for i in range(24)]
    models = []
    for i in range(12):
        m = api.data / "meshy_output" / f"m{i}" / "model.glb"
        m.parent.mkdir(parents=True)
        m.write_bytes(b"glTF" + bytes([i]) * 64)
        models.append(m)
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda i: api.provenance({"task_id": i, "tool": "meshy_remesh", "args": {}, "session_id": "s"}), ids))
        list(pool.map(api._record_asset, models))
    assert set(ids) <= set(api._load_prov())
    assert {str(m.resolve()) for m in models} <= {r["path"] for r in api._load_index()}
    assert api._save_target(ids[0], None)[0] != api._save_target(ids[1], None)[0]


def _fake_assets(monkeypatch, api, files: dict[str, bytes | int], delay: float = 0.0) -> dict[str, int]:
    """Serve ``https://assets.meshy.ai/<name>`` from ``files`` (an int = that HTTP error); count each fetch."""
    import threading
    import time
    hits: dict[str, int] = {}
    lock = threading.Lock()

    def urlopen(url, timeout=None, allowed=None):
        assert allowed(url)
        name = str(url).rsplit("/", 1)[-1]
        with lock:
            hits[name] = hits.get(name, 0) + 1
        time.sleep(delay)  # long enough for a second request to overlap the first
        if isinstance(files[name], int):
            raise urllib.error.HTTPError(str(url), files[name], "fixture", {}, None)
        return _Resp(files[name])

    monkeypatch.setattr(api, "_open", urlopen)
    return hits


def test_save_downloads_a_file_once_and_a_retry_finishes_failed_companions(api, monkeypatch):
    """Two clicks on one export download it once (both succeed), and an OBJ whose .mtl failed is completed
    by the retry instead of being reported saved without its material."""
    from concurrent.futures import ThreadPoolExecutor
    a = "https://assets.meshy.ai/"
    row = {"id": _TASK, "model_urls": {"fbx": a + "m.fbx", "obj": a + "m.obj", "mtl": a + "m.mtl"}}
    monkeypatch.setattr(api, "_task", lambda _t, _k: row)
    files = {"m.fbx": b"FBX" * 1000, "m.obj": b"mtllib material.mtl\nv 0 0 0\n", "m.mtl": 503}
    hits = _fake_assets(monkeypatch, api, files, delay=0.2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        saved = list(pool.map(lambda _: api.save({"task": _TASK, "format": "fbx"}), range(2)))
    assert [s["bytes"] for s in saved] == [3000, 3000] and hits["m.fbx"] == 1

    folder = api.data / "meshy_output" / _TASK
    assert _status(api.save, {"task": _TASK, "format": "obj"}) == 502  # the .mtl fetch failed: not reported as saved
    files["m.mtl"] = b"newmtl material\n"
    api.save({"task": _TASK, "format": "obj"})
    assert (folder / "model.mtl").read_text(encoding="utf-8") == "newmtl material\n" and hits["m.mtl"] == 2
    assert (folder / "model.obj").read_bytes().startswith(b"mtllib model.mtl\n")
    assert not list(folder.glob("*.part"))  # no staging file left behind


def test_a_beside_export_is_reused_only_for_the_task_that_saved_it(api, monkeypatch):
    """A project file regenerated as a new task keeps its name; the old task's robot.stl beside it is never
    served or listed as the new task's export, and a file the user made or edited there is never overwritten."""
    old, new = "01a15976-d838-7532-99d8-000000000050", _TASK
    a = "https://assets.meshy.ai/"
    rows = {old: {"id": old, "model_urls": {"stl": a + "old.stl"}},
            new: {"id": new, "model_urls": {"stl": a + "new.stl", "obj": a + "m.obj", "mtl": a + "m.mtl"}}}
    monkeypatch.setattr(api, "_task", lambda t, _k: rows[t])
    hits = _fake_assets(monkeypatch, api, {"old.stl": b"OLD TASK STL", "new.stl": b"NEW TASK STL",
                                           "m.obj": b"mtllib material.mtl\nv 0 0 0\n", "m.mtl": b"newmtl material\n"})
    anchor = Path.home() / "projects" / "demo" / "robot.glb"
    anchor.parent.mkdir(parents=True)
    anchor.write_bytes(b"glTF-old")
    api.save({"task": old, "format": "stl", "beside": str(anchor)})

    anchor.write_bytes(b"glTF-regenerated")
    assert {"format": "stl", "saved": None} in api.formats(new, beside=str(anchor))["formats"]
    saved = Path(api.save({"task": new, "format": "stl", "beside": str(anchor)})["path"])
    assert saved.read_bytes() == b"NEW TASK STL" and hits.get("new.stl") == 1
    assert {"format": "stl", "saved": str(saved)} in api.formats(new, beside=str(anchor))["formats"]
    api.save({"task": new, "format": "stl", "beside": str(anchor)})
    assert hits["new.stl"] == 1  # this task's own export is reused
    # Every saved format is a gallery model, listed under the task that saved it last (the regenerated one).
    assert [(f["name"], f["task"]) for f in api.local(limit=50)["files"]] == [("robot.stl", new)]

    # A sibling Hermes never saved is the user's: never overwritten. The card's own model is this task's file.
    tank = anchor.with_name("tank.glb")
    tank.write_bytes(b"glTF-tank")
    mine = tank.with_suffix(".stl")
    mine.write_bytes(b"USER MADE THIS")
    other = Path(api.save({"task": new, "format": "stl", "beside": str(tank)})["path"])
    assert mine.read_bytes() == b"USER MADE THIS" and other.name == f"tank-{new[-8:]}.stl"
    assert other.read_bytes() == b"NEW TASK STL"
    # ...nor is one Hermes saved that the user edited since, nor a user's .mtl an OBJ save would bring along.
    saved.write_bytes(b"USER EDITED THE EXPORT")
    api.save({"task": new, "format": "stl", "beside": str(anchor)})
    assert saved.read_bytes() == b"USER EDITED THE EXPORT"
    other.write_bytes(b"USER EDITED THE SUFFIXED ONE")  # the fallback name is checked the same way
    third = Path(api.save({"task": new, "format": "stl", "beside": str(tank)})["path"])
    assert other.read_bytes() == b"USER EDITED THE SUFFIXED ONE" and third.name == f"tank-{new[-8:]}-2.stl"
    tank.with_suffix(".mtl").write_text("USER MTL", encoding="utf-8")
    obj = Path(api.save({"task": new, "format": "obj", "beside": str(tank)})["path"])
    assert tank.with_suffix(".mtl").read_text(encoding="utf-8") == "USER MTL" and obj.name == f"tank-{new[-8:]}.obj"
    assert obj.with_suffix(".mtl").read_text(encoding="utf-8") == "newmtl material\n"


def test_viewing_a_file_never_makes_it_replaceable(api, monkeypatch):
    """Only /save's own write lets a later save replace a file beside the project. Opening a handmade STL, or
    reopening an export the user edited, in the viewer (/file, with or without a task) must never vouch for it:
    the next save beside the same model takes a task-suffixed name instead. An untouched save viewed since is
    still this task's file (reused, not duplicated), and an index row without a save-time record is not."""
    other, task = "01a15976-d838-7532-99d8-000000000051", _TASK
    a = "https://assets.meshy.ai/"
    rows = {t: {"id": t, "model_urls": {"stl": a + f"{t[-2:]}.stl"}} for t in (other, task)}
    monkeypatch.setattr(api, "_task", lambda t, _k: rows[t])
    hits = _fake_assets(monkeypatch, api, {"51.stl": b"OTHER TASK STL", "6c.stl": b"THIS TASK STL"})
    anchor = Path.home() / "projects" / "bot" / "robot.glb"
    anchor.parent.mkdir(parents=True)
    anchor.write_bytes(b"glTF")

    handmade = anchor.with_suffix(".stl")
    handmade.write_bytes(b"HANDMADE STL")
    api.file(str(handmade))
    api.file(str(handmade), task=task)
    assert {"format": "stl", "saved": None} in api.formats(task, beside=str(anchor))["formats"]
    first = Path(api.save({"task": task, "format": "stl", "beside": str(anchor)})["path"])
    assert handmade.read_bytes() == b"HANDMADE STL" and first.name == f"robot-{task[-8:]}.stl"

    # This task's untouched save, viewed since, is still its own: reused, never downloaded twice.
    api.file(str(first), task=task)
    assert Path(api.save({"task": task, "format": "stl", "beside": str(anchor)})["path"]) == first
    assert hits["6c.stl"] == 1
    # Edited, then reopened in the viewer: another task's save beside the project keeps the edit.
    first.write_bytes(b"USER EDITED THE EXPORT")
    api.file(str(first), task=task)
    api.file(str(first))
    second = Path(api.save({"task": other, "format": "stl", "beside": str(anchor)})["path"])
    assert first.read_bytes() == b"USER EDITED THE EXPORT" and second.name == f"robot-{other[-8:]}.stl"
    assert hits["6c.stl"] == 1

    # An index row with only read-time size/mtime (an older index, or a gallery read) vouches for nothing.
    legacy = anchor.with_name("tank.stl")
    legacy.write_bytes(b"USER TANK")
    st = legacy.stat()
    api._write_json(api._index_path(), [*api._load_index(), {"path": str(legacy.resolve()), "bytes": st.st_size,
                                                             "mtime": int(st.st_mtime * 1000), "task_id": other}])
    tank = anchor.with_name("tank.glb")
    tank.write_bytes(b"glTF-tank")
    saved = Path(api.save({"task": other, "format": "stl", "beside": str(tank)})["path"])
    assert legacy.read_bytes() == b"USER TANK" and saved.name == f"tank-{other[-8:]}.stl"


def test_an_obj_anchor_hermes_did_not_save_is_never_rewritten(api, monkeypatch):
    """The card's model can be the user's own OBJ (no .mtl beside it). Download ▸ OBJ of that card must not
    complete "missing companions" over it: the user's mtllib line, texture and mesh stay, nothing is marked as
    Hermes' save, and another task's OBJ saved beside the project later takes a new name."""
    other, task = "01a15976-d838-7532-99d8-000000000052", _TASK
    a = "https://assets.meshy.ai/"
    rows = {t: {"id": t, "model_urls": {"obj": a + f"{t[-2:]}.obj", "mtl": a + "m.mtl"},
                "texture_urls": [{"base_color": a + "tex.png"}]} for t in (other, task)}
    monkeypatch.setattr(api, "_task", lambda t, _k: rows[t])
    _fake_assets(monkeypatch, api, {"52.obj": b"mtllib material.mtl\nv 1 1 1\n", "6c.obj": b"mtllib material.mtl\nv 0 0 0\n",
                                    "m.mtl": b"newmtl material\nmap_Kd texture_0.png\n", "tex.png": b"PNG-MESHY"})
    proj = Path.home() / "projects" / "obj"
    proj.mkdir(parents=True)
    user_obj, user_tex = proj / "robot.obj", proj / "robot_texture.png"
    user_obj.write_bytes(b"mtllib my_materials.mtl\nv 9 9 9\n")
    user_tex.write_bytes(b"PNG-USER")
    api.file(str(user_obj), task=task)

    got = Path(api.save({"task": task, "format": "obj", "beside": str(user_obj)})["path"])
    assert got == user_obj.resolve()
    assert user_obj.read_bytes() == b"mtllib my_materials.mtl\nv 9 9 9\n" and user_tex.read_bytes() == b"PNG-USER"
    assert not (proj / "robot.mtl").exists()
    assert not any(r.get("saved") for r in api._load_index())

    anchor = proj / "robot.glb"
    anchor.write_bytes(b"glTF")
    second = Path(api.save({"task": other, "format": "obj", "beside": str(anchor)})["path"])
    assert second.name == f"robot-{other[-8:]}.obj" and second.with_suffix(".mtl").is_file()
    assert user_obj.read_bytes() == b"mtllib my_materials.mtl\nv 9 9 9\n" and user_tex.read_bytes() == b"PNG-USER"


@pytest.mark.skipif(os.name == "nt", reason="Windows has no metadata-change time; POSIX ctime catches this edit")
def test_an_edit_that_restores_size_and_mtime_is_still_the_users(api, monkeypatch):
    """``cp -p``/``touch -r`` and coarse-mtime filesystems can leave an edited export with its saved size and
    mtime; it is still never replaced."""
    other, task = "01a15976-d838-7532-99d8-000000000053", _TASK
    a = "https://assets.meshy.ai/"
    rows = {t: {"id": t, "model_urls": {"stl": a + f"{t[-2:]}.stl"}} for t in (other, task)}
    monkeypatch.setattr(api, "_task", lambda t, _k: rows[t])
    _fake_assets(monkeypatch, api, {"53.stl": b"OTHER TASK STL", "6c.stl": b"THIS TASK STL"})
    anchor = Path.home() / "projects" / "edit" / "robot.glb"
    anchor.parent.mkdir(parents=True)
    anchor.write_bytes(b"glTF")
    saved = Path(api.save({"task": task, "format": "stl", "beside": str(anchor)})["path"])
    before = saved.stat()
    with open(saved, "r+b") as fh:  # same size, in place
        fh.write(b"USER")
    os.utime(saved, ns=(before.st_atime_ns, before.st_mtime_ns))
    again = Path(api.save({"task": other, "format": "stl", "beside": str(anchor)})["path"])
    assert saved.read_bytes() == b"USER TASK STL" and again != saved


def test_a_dangling_user_symlink_is_never_replaced(api, monkeypatch):
    a = "https://assets.meshy.ai/"
    monkeypatch.setattr(api, "_task", lambda t, _k: {"id": t, "model_urls": {"stl": a + "m.stl"}})
    _fake_assets(monkeypatch, api, {"m.stl": b"MESHY STL"})
    anchor = Path.home() / "projects" / "link" / "robot.glb"
    anchor.parent.mkdir(parents=True)
    anchor.write_bytes(b"glTF")
    link = anchor.with_suffix(".stl")
    try:
        os.symlink(anchor.parent / "elsewhere.stl", link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges on this Windows runner")
    saved = Path(api.save({"task": _TASK, "format": "stl", "beside": str(anchor)})["path"])
    assert link.is_symlink() and saved.name == f"robot-{_TASK[-8:]}.stl"
