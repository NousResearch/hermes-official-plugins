"""Input and local-disk hardening of the Meshy router, plus the Regenerate button's price.

Same harness as test_plugin_api.py: real router code against a temp Hermes home + temp user home, with a
network tripwire (every refusal here must happen before the router reaches the network); downloads that
should happen are served by a stand-in for the router's one network seam (``_open``).
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import urllib.request
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

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
    monkeypatch.setenv("MESHY_API_KEY", "msy-test-not-a-real-key")

    def tripwire(*_a, **_k):
        raise AssertionError("network call reached before input validation")

    monkeypatch.setattr(urllib.request, "urlopen", tripwire)
    monkeypatch.setattr(urllib.request, "build_opener", tripwire)
    spec = importlib.util.spec_from_file_location("meshy_plugin_api_hardening_under_test", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.tmp, mod.data = tmp_path, data
    return mod


class _Resp(io.BytesIO):
    headers: dict = {}


def _serve(monkeypatch, api, files: dict[str, bytes]) -> None:
    """Asset links ``https://assets.meshy.ai/<name>`` answer with ``files[name]``."""
    monkeypatch.setattr(api, "_open", lambda url, _timeout, _allowed: _Resp(files[str(url).rsplit("/", 1)[-1]]))


def _status(fn, *args, **kwargs) -> int:
    with pytest.raises(HTTPException) as err:
        fn(*args, **kwargs)
    return err.value.status_code


def _symlink(target: Path, link: Path, *, directory: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges on this Windows runner")


# --- (a) ids and names must match whole -------------------------------------------------------------------

def test_a_trailing_newline_never_passes_id_or_format_validation(api):
    bad = _TASK + "\n"
    assert _status(api.formats, bad) == 400
    assert _status(api.save, {"task": bad, "format": "glb"}) == 400
    assert _status(api.save, {"task": _TASK, "format": "glb:walking\n"}) == 400
    assert _status(api.task_state, bad) == 400
    assert _status(api.actions, bad) == 400
    assert _status(api.details, bad) == 400
    assert _status(api.save_images, {"task": bad}) == 400
    assert _status(api.provenance, {"task_id": bad, "tool": "meshy_remesh", "session_id": "s"}) == 400
    folder = api.data / "meshy_output" / "project"
    folder.mkdir(parents=True)
    (folder / "metadata.json").write_text(json.dumps({"root_task_id": bad}), encoding="utf-8")
    (folder / "model.glb").write_bytes(b"glTF")
    assert api._local_task(folder / "model.glb") is None


# --- (c) clean errors -------------------------------------------------------------------------------------

def test_bad_body_types_get_a_json_400_not_a_server_error(api):
    client = TestClient(_app(api))
    res = client.post("/save", json={"task": _TASK, "format": "stl", "beside": ["/etc/passwd"]})
    assert res.status_code == 400 and res.json()["detail"]


def _app(api) -> FastAPI:
    app = FastAPI()
    app.include_router(api.router)
    return app


def test_local_disk_failures_are_reported_not_raised(api, monkeypatch):
    monkeypatch.setattr(api, "_task", lambda _t, _k: {"id": _TASK, "model_urls": {"glb": "https://assets.meshy.ai/m.glb"}})
    _serve(monkeypatch, api, {"m.glb": b"glTF"})
    (api.data / "meshy_output").mkdir()
    (api.data / "meshy_output" / _TASK).write_text("a file where the folder goes", encoding="utf-8")
    with pytest.raises(HTTPException) as err:
        api.save({"task": _TASK, "format": "glb"})
    assert err.value.status_code == 500 and "could not write" in err.value.detail

    import sqlite3
    with sqlite3.connect(api.tmp / "hermes" / "state.db") as db:
        db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
        db.execute("INSERT INTO sessions VALUES ('s')")
    api._prov_path().mkdir()  # provenance.json is a folder: the write fails
    assert _status(api.provenance, {"task_id": _TASK, "tool": "meshy_remesh", "args": {}, "session_id": "s"}) == 500


# --- (d) links under the data root ---------------------------------------------------------------------------

def test_save_never_writes_through_a_planted_link(api, monkeypatch):
    monkeypatch.setattr(api, "_task", lambda _t, _k: {"id": _TASK, "model_urls": {"glb": "https://assets.meshy.ai/m.glb"}})
    _serve(monkeypatch, api, {"m.glb": b"glTF-saved"})
    victim = api.tmp / "outside" / "keep.txt"
    victim.write_text("unchanged", encoding="utf-8")
    folder = api.data / "meshy_output" / _TASK
    folder.mkdir(parents=True)
    _symlink(victim, folder / ".meshy-task")
    saved = Path(api.save({"task": _TASK, "format": "glb"})["path"])
    assert victim.read_text(encoding="utf-8") == "unchanged"
    assert saved.read_bytes() == b"glTF-saved" and not (folder / ".meshy-task").is_symlink()
    assert (folder / ".meshy-task").read_text(encoding="utf-8") == _TASK


def test_save_refuses_a_linked_download_folder(api, monkeypatch):
    monkeypatch.setattr(api, "_task", lambda _t, _k: {"id": _TASK, "model_urls": {"glb": "https://assets.meshy.ai/m.glb"}})
    _serve(monkeypatch, api, {"m.glb": b"glTF"})
    outside = api.tmp / "outside"
    (api.data / "meshy_output").mkdir()
    _symlink(outside, api.data / "meshy_output" / _TASK, directory=True)
    assert _status(api.save, {"task": _TASK, "format": "glb"}) == 403
    assert list(outside.iterdir()) == []


def test_image_set_refuses_linked_folders_and_files(api, monkeypatch):
    monkeypatch.setattr(api, "_task", lambda *_a, **_k: {"image_urls": ["https://assets.meshy.ai/a.png"]})
    _serve(monkeypatch, api, {"a.png": b"image"})
    outside = api.tmp / "outside"
    root = api._saved_assets_root()
    root.mkdir(parents=True)
    _symlink(outside, root / _TASK, directory=True)
    assert _status(api.save_images, {"task": _TASK}) == 403
    assert list(outside.iterdir()) == []
    (root / _TASK).unlink()
    (root / _TASK).mkdir()
    victim = outside / "keep.png"
    victim.write_bytes(b"unchanged")
    _symlink(victim, root / _TASK / "image-1.png")
    assert _status(api.save_images, {"task": _TASK}) == 403
    assert victim.read_bytes() == b"unchanged"


# --- item 11: Regenerate runs on the original's model and quotes that model's price -------------------------

def _sessions(api) -> None:
    import sqlite3
    with sqlite3.connect(api.tmp / "hermes" / "state.db") as db:
        db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY)")
        db.execute("INSERT OR IGNORE INTO sessions VALUES ('s')")


@pytest.mark.parametrize("record,args,model,cost", [
    ({"model_type": "standard", "consumed_credits": 5}, None, "meshy-6-lite", 5),
    ({"model_type": "smart-topology", "consumed_credits": 5}, None, "meshy-t2", 5),
    ({"model_type": "standard", "consumed_credits": 20}, None, "latest", 20),
    ({"consumed_credits": 20}, {"ai_model": "meshy-6"}, "meshy-6", 20),
    ({"consumed_credits": 5}, {"ai_model": "meshy-t2.5", "model_type": "smart-topology"}, "meshy-t2.5", 5),
    ({"consumed_credits": 25}, {"ai_model": "latest", "geometry_resolution": "4k"}, "latest", 25),
    ({"model_type": "smart-topology"}, {}, "meshy-t2", 5),  # recorded without ai_model: the tool's default
])
def test_regenerate_keeps_the_original_model_and_quotes_its_price(api, monkeypatch, record, args, model, cost):
    row = {"id": _TASK, "type": "text-to-3d-preview", "prompt": "owl", **record}
    monkeypatch.setattr(api, "_task", lambda _t, _k: row)
    monkeypatch.setattr(api, "_meshy_get", lambda *_a, **_k: {"balance": 100})
    if args is not None:
        _sessions(api)
        api.provenance({"task_id": _TASK, "tool": "meshy_text_to_3d", "session_id": "s", "args": {"prompt": "owl", **args}})
    regen = next(a for a in api.actions(_TASK)["actions"] if a["id"] == "regenerate")
    assert regen["cost"] == cost
    assert f'ai_model "{model}"' in regen["message"] and f"({cost} credits)" in regen["message"]
    assert "seed" not in regen["message"]  # meshy_text_to_3d has no seed field
    if args and args.get("geometry_resolution"):
        assert 'geometry_resolution "4k"' in regen["message"]
