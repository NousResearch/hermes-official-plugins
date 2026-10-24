"""Desktop-facing routes of the Meshy router: every saved format reaches the gallery and the storage total,
and a remote backend can receive a model file sent from the Desktop's computer.

Same harness as test_plugin_api.py: real router code against a temp Hermes home + temp user home, with a
network tripwire (none of these routes may reach Meshy).
"""

from __future__ import annotations

import base64
import importlib.util
import os
import urllib.request
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

_SRC = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
_TASK = "01a157b8-7317-7551-abc5-f99fe8c1236c"
_MODELS = ("glb", "gltf", "fbx", "obj", "stl", "usdz", "3mf")


@pytest.fixture
def api(tmp_path, monkeypatch):
    home, hermes = tmp_path / "home", tmp_path / "hermes"
    data = hermes / "plugin-data" / "agent-plugin-meshy"
    for d in (home, data, tmp_path / "outside"):
        d.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("MESHY_API_KEY", "msy-test-not-a-real-key")

    def tripwire(*_a, **_k):
        raise AssertionError("network call from a route that must stay local")

    monkeypatch.setattr(urllib.request, "urlopen", tripwire)
    monkeypatch.setattr(urllib.request, "build_opener", tripwire)
    spec = importlib.util.spec_from_file_location("meshy_plugin_api_desktop_under_test", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.tmp, mod.data, mod.home = tmp_path, data, home
    return mod


@pytest.fixture
def client(api):
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def _status(fn, *args, **kwargs) -> int:
    with pytest.raises(HTTPException) as err:
        fn(*args, **kwargs)
    return err.value.status_code


def _project_saves(api) -> Path:
    """A project folder where /save put every format beside the model (OBJ with its .mtl and texture)."""
    proj = api.home / "game" / "props"
    proj.mkdir(parents=True)
    for fmt in (*_MODELS, "blend"):
        (proj / f"crate.{fmt}").write_bytes(f"{fmt} bytes".encode())
        api._record_asset(proj / f"crate.{fmt}", _TASK, export=True)
    for companion in ("crate.mtl", "crate_texture.png"):
        (proj / companion).write_bytes(b"companion")
        api._record_asset(proj / companion, _TASK, export=True)
    return proj


def test_gallery_lists_every_saved_model_format_with_its_task(api):
    proj = _project_saves(api)
    out = api.data / "meshy_output" / _TASK
    out.mkdir(parents=True)
    (out / "model.fbx").write_bytes(b"fbx")  # the MCP server's own download dir, any format
    (out / ".meshy-task").write_text(_TASK, encoding="utf-8")

    files = {f["name"]: f for f in api.local(limit=200)["files"]}
    for fmt in (*_MODELS, "blend"):
        assert files[f"crate.{fmt}"]["task"] == _TASK, fmt
        assert files[f"crate.{fmt}"]["path"] == str((proj / f"crate.{fmt}").resolve())
    assert files["model.fbx"]["task"] == _TASK
    # Viewable by three.js vs listed with a plain tile.
    assert all(files[f"crate.{fmt}"]["viewable"] for fmt in _MODELS)
    assert files["crate.blend"]["viewable"] is False
    # Companions are not models.
    assert "crate.mtl" not in files and "crate_texture.png" not in files


def test_storage_total_counts_every_saved_format_at_any_size(api, monkeypatch):
    proj = _project_saves(api)
    big = proj / "hero.obj"
    big.write_bytes(b"x" * 4096)
    api._record_asset(big, _TASK, export=True)
    monkeypatch.setattr(api, "_MAX_FILE_BYTES", 1024)  # the OBJ is now over the viewer's cap

    expected = sorted(p for p in proj.iterdir())
    report = api.storage()
    assert report["files"] == len(expected)
    assert report["bytes"] == sum(p.stat().st_size for p in expected)
    listed = {f["name"]: f for f in api.local(limit=200)["files"]}
    assert listed["hero.obj"]["viewable"] is False  # too big for the viewer, still listed


def test_file_serves_new_formats_inside_roots_only(api):
    proj = _project_saves(api)
    for fmt in ("fbx", "obj", "stl", "usdz", "3mf"):
        got = api.file(str(proj / f"crate.{fmt}"))
        assert got["name"] == f"crate.{fmt}"
        assert base64.b64decode(got["dataUrl"].split(",", 1)[1]) == f"{fmt} bytes".encode()
    mtl = api.file(str(proj / "crate.mtl"))  # an OBJ's material comes through the same guarded route
    assert mtl["dataUrl"].startswith("data:text/plain;base64,")
    assert _status(api.file, str(proj / "crate.blend")) == 415  # listed, never served (no loader)

    outside = api.tmp / "outside" / "stolen.fbx"
    outside.write_bytes(b"fbx")
    assert _status(api.file, str(outside)) == 403
    link = proj / "innocent.stl"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges on this Windows runner")
    assert _status(api.file, str(link)) == 403


def _electron_multipart(filename: str, data: bytes) -> tuple[bytes, str]:
    """The exact body apps/desktop/electron/main.ts::multipartBody builds for ctx.rest(..., { upload })."""
    boundary = "----hermes-0123456789abcdef01234567"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def test_upload_stores_a_model_the_remote_backend_can_then_serve(api, client):
    payload = b"glTF\x02\x00\x00\x00" + bytes(range(256)) * 4 + b"\r\n--not-the-boundary\r\n"
    body, ctype = _electron_multipart("Robot Arm.glb", payload)
    res = client.post("/upload", content=body, headers={"content-type": ctype})
    assert res.status_code == 200, res.text
    saved = res.json()
    path = Path(saved["path"])
    assert path.parent == (api.data / "uploads").resolve() or path.parent == api.data / "uploads"
    assert path.read_bytes() == payload and saved["bytes"] == len(payload)
    assert saved["name"] == "Robot_Arm.glb"
    # The returned path goes through /file like any other model, and the gallery lists it.
    assert base64.b64decode(api.file(saved["path"])["dataUrl"].split(",", 1)[1]) == payload
    assert saved["path"] in {f["path"] for f in api.local(limit=200)["files"]}

    # An older Desktop's JSON fallback: same bytes land on the same content-addressed file.
    res = client.post("/upload", json={"filename": "Robot Arm.glb", "data": base64.b64encode(payload).decode()})
    assert res.status_code == 200 and res.json()["path"] == saved["path"]
    # httpx's own multipart encoder (a different boundary/header layout) parses too.
    res = client.post("/upload", files={"file": ("part.stl", b"solid x\nendsolid x\n", "model/stl")})
    assert res.status_code == 200 and res.json()["name"] == "part.stl"


def test_upload_refusals(api, client, monkeypatch):
    # An older Desktop drops the upload option: the renderer keys its JSON retry on this exact detail.
    res = client.post("/upload")
    assert res.status_code == 400 and res.json()["detail"] == "no file in the request"
    body, ctype = _electron_multipart("notes.txt", b"hello")
    assert client.post("/upload", content=body, headers={"content-type": ctype}).status_code == 415
    body, ctype = _electron_multipart(".env", b"KEY=x")
    assert client.post("/upload", content=body, headers={"content-type": ctype}).status_code == 415
    assert client.post("/upload", json={"filename": "a.glb", "data": "%%%"}).status_code == 400
    body, ctype = _electron_multipart("empty.glb", b"")
    assert client.post("/upload", content=body, headers={"content-type": ctype}).status_code == 400

    # A traversal name is flattened into the uploads folder, never written outside it.
    body, ctype = _electron_multipart("../../../outside/evil.glb", b"glTF")
    res = client.post("/upload", content=body, headers={"content-type": ctype})
    assert res.status_code == 200
    assert Path(res.json()["path"]).parent.name == "uploads"
    assert not (api.tmp / "outside" / "evil.glb").exists()

    monkeypatch.setattr(api, "_MAX_FILE_BYTES", 16)
    body, ctype = _electron_multipart("big.glb", b"x" * 17)
    assert client.post("/upload", content=body, headers={"content-type": ctype}).status_code == 413


def test_upload_refuses_a_symlinked_uploads_folder(api, client):
    target = api.tmp / "outside"
    try:
        os.symlink(target, api.data / "uploads", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges on this Windows runner")
    body, ctype = _electron_multipart("m.glb", b"glTF")
    assert client.post("/upload", content=body, headers={"content-type": ctype}).status_code == 403
    assert list(target.iterdir()) == []


def test_upload_disk_failure_is_a_json_error(api, client, monkeypatch):
    def full(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(api, "_staging", full)
    res = client.post("/upload", json={"filename": "a.glb", "data": base64.b64encode(b"glTF-bytes").decode()})
    assert res.status_code == 500 and "No space left on device" in res.json()["detail"]
