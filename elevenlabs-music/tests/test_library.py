"""The Desktop backend (``dashboard/plugin_api.py``) serves only files the tools wrote, per profile."""

from __future__ import annotations

import base64
import importlib.util
import os

import pytest


@pytest.fixture
def client(plugin_root):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    spec = importlib.util.spec_from_file_location("music_plugin_api_under_test", plugin_root / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = fastapi.FastAPI()
    app.include_router(module.router)
    return TestClient(app), module._prov


def _track(prov, audio: bytes, track_id: str, **extra) -> str:
    name = f"music_test-song_20261028_120000_{track_id}.mp3"
    prov.audio_dir().mkdir(parents=True, exist_ok=True)
    (prov.audio_dir() / name).write_bytes(audio)
    prov.record({"id": track_id, "kind": "song", "title": "Test Song", "file_name": name, "length_seconds": 2.0,
                 **extra}, {"words_timestamps": [{"word": "la", "start_ms": 0, "end_ms": 300}],
                            "composition_plan": {"chunks": [{"text": "[Verse]\nla", "duration_ms": 3000}]}})
    return name


def test_library_card_and_audio_routes(client, audio):
    http, prov = client
    data = audio.mp3(30)
    _track(prov, data, "0123456789ab")
    listing = http.get("/tracks").json()
    assert listing["total"] == 1 and listing["tracks"][0]["exists"] is True
    assert listing["totals"]["song"]["count"] == 1
    card = http.get("/track", params={"id": "0123456789ab"}).json()
    assert card["track"]["title"] == "Test Song" and card["words"][0]["word"] == "la"
    assert card["plan"]["chunks"][0]["duration_ms"] == 3000
    payload = http.get("/audio", params={"id": "0123456789ab"}).json()
    assert base64.b64decode(payload["data_url"].split(",", 1)[1]) == data and payload["mime"] == "audio/mpeg"
    assert http.get("/track", params={"id": "ffffffffffff"}).status_code == 404
    assert http.get("/track", params={"id": "../etc"}).status_code == 400


def test_a_record_cannot_point_outside_the_audio_folder(client, audio, tmp_path):
    http, prov = client
    prov.record({"id": "aaaaaaaaaaaa", "kind": "song", "file_name": "../../../etc/passwd"})
    assert http.get("/audio", params={"id": "aaaaaaaaaaaa"}).status_code == 410
    name = _track(prov, audio.mp3(5), "bbbbbbbbbbbb")
    os.link(prov.audio_dir() / name, tmp_path / "backup.mp3")  # a backup tool's hard link
    response = http.get("/audio", params={"id": "bbbbbbbbbbbb"})
    assert response.status_code == 410 and "hard link" in response.json()["detail"]
