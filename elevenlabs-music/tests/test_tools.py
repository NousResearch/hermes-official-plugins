"""The plugin, installed into a temp HERMES_HOME and enabled there, is loaded by core's real PluginManager;
its tools are dispatched through the real registry against a loopback stand-in for ElevenLabs' Music API
that enforces the request shape of the published OpenAPI spec (https://api.elevenlabs.io/openapi.json).
"""

from __future__ import annotations

import io
import json
import shutil
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import hermes_yaml as yaml
import pytest

PLUGIN = "elevenlabs-music"
# Body_Compose_Music_with_a_detailed_response_v1_music_detailed_post
DETAILED_FIELDS = {"prompt", "composition_plan", "music_length_ms", "model_id", "seed", "force_instrumental",
                   "finetune_id", "respect_sections_durations", "store_for_inpainting", "with_timestamps",
                   "with_waveform_visual", "sign_with_c2pa"}
BOUNDARY = "----boundarytest"
PLAN = {"chunks": [{"text": "[Hook]\nhello from Hermes", "duration_ms": 6000, "positive_styles": ["acoustic"],
                    "negative_styles": []}, {"text": "[Outro]", "duration_ms": 4000, "positive_styles": ["fade"]}]}


def detailed_reply(audio: bytes) -> tuple[int, dict, bytes]:
    meta = {"composition_plan": PLAN, "song_metadata": {"title": "Hello From Hermes", "genres": ["folk"]},
            "words_timestamps": [{"word": "hello", "start_ms": 200, "end_ms": 600}]}
    body = (f"--{BOUNDARY}\r\nContent-Type: application/json\r\n\r\n{json.dumps(meta)}\r\n"
            f"--{BOUNDARY}\r\nContent-Type: audio/mpeg\r\nContent-Disposition: attachment; filename=\"music.mp3\"\r\n\r\n"
            ).encode() + audio + f"\r\n--{BOUNDARY}--\r\n".encode()
    return 200, {"Content-Type": f"multipart/mixed; boundary={BOUNDARY}", "song-id": "SongIdFromStandIn01"}, body


class StandIn:
    """Records each request; answers per path from ``replies`` (status, headers, body), after waiting on
    ``gate`` when set (a slow generation)."""

    def __init__(self, audio: bytes):
        self.requests: list[dict] = []
        self.gate: threading.Event | None = None
        zipped = io.BytesIO()
        with zipfile.ZipFile(zipped, "w") as archive:
            archive.writestr("vocals.mp3", audio)
            archive.writestr("instrumental.mp3", audio)
            archive.writestr("../../escape.txt", b"not audio")
        self.replies = {"/v1/music/detailed": detailed_reply(audio),
                        "/v1/music/stem-separation": (200, {"Content-Type": "application/zip"}, zipped.getvalue())}
        stand_in = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                url = urlsplit(self.path)
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                ctype = self.headers.get("Content-Type", "")
                body = json.loads(raw) if ctype.startswith("application/json") else raw
                stand_in.requests.append({"path": url.path, "query": parse_qs(url.query),
                                          "key": self.headers.get("xi-api-key"), "body": body, "type": ctype})
                problem = isinstance(body, dict) and url.path == "/v1/music/detailed" and (
                    set(body) - DETAILED_FIELDS and f"unknown fields {sorted(set(body) - DETAILED_FIELDS)}"
                    or ("prompt" in body) == ("composition_plan" in body) and "prompt xor composition_plan")
                if stand_in.gate is not None:
                    stand_in.gate.wait(10)
                status, headers, payload = (
                    (422, {"Content-Type": "application/json"}, json.dumps({"detail": [{"loc": ["body"], "msg": problem}]}).encode())
                    if problem else stand_in.replies.get(url.path, (404, {}, b"{}")))
                self.send_response(status)
                for name, value in {"Content-Length": str(len(payload)), **headers}.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads, self.server.block_on_close = True, False
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        if self.gate is not None:
            self.gate.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def loaded(_hermes_home, plugin_root, monkeypatch, audio):
    """The plugin copied into the temp home, enabled with settings pointing at a stand-in, and loaded by a
    fresh PluginManager; yields (dispatch, stand_in, home)."""
    from hermes_cli import plugins as plugins_mod
    from hermes_cli.plugins import PluginManager
    from tools.registry import registry

    stand_in = StandIn(audio.mp3(40))
    shutil.copytree(plugin_root, _hermes_home / "plugins" / PLUGIN,
                    ignore=shutil.ignore_patterns("node_modules", "tests", "__pycache__"))
    (_hermes_home / "config.yaml").write_text(yaml.safe_dump({"plugins": {
        "enabled": [PLUGIN],
        "entries": {PLUGIN: {"settings": {"api_base_url": stand_in.url, "chat_app_wait_seconds": 0}}}}}))
    (_hermes_home / ".env").write_text("ELEVENLABS_API_KEY=xi-test-key\n")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-test-key")
    empty_bundled = _hermes_home.parent / "bundled"
    empty_bundled.mkdir()
    monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: empty_bundled)
    manager = PluginManager()
    manager.discover_and_load()
    assert registry.get_entry("elevenlabs_music_compose", scope=manager.scope_key) is not None

    def dispatch(tool, args):
        return json.loads(registry.dispatch(tool, args, scope=manager.scope_key))

    try:
        yield dispatch, stand_in, _hermes_home
    finally:
        manager.unload()
        stand_in.close()


def test_compose_saves_the_song_and_its_library_record(loaded):
    dispatch, stand_in, home = loaded
    result = dispatch("elevenlabs_music_compose", {"prompt": "warm acoustic hook", "length_seconds": 10,
                                                   "instrumental": True})
    assert result["success"] is True, result
    request = stand_in.requests[-1]
    assert request["path"] == "/v1/music/detailed" and request["key"] == "xi-test-key"
    assert request["query"] == {"output_format": ["mp3_44100_128"]}
    assert request["body"] == {"prompt": "warm acoustic hook", "model_id": "music_v2_5", "force_instrumental": True,
                               "store_for_inpainting": True, "with_timestamps": True, "music_length_ms": 10000}
    path = Path(result["file_path"])
    assert result["media_tag"] == f"MEDIA:{path}" and path.parent == home / "cache" / "generated" / "audio"
    assert path.read_bytes().startswith(b"ID3") and path.name.endswith(f"_{result['track_id']}.mp3")
    assert result["title"] == "Hello From Hermes" and result["elevenlabs_song_id"] == "SongIdFromStandIn01"
    assert [s["label"] for s in result["sections"]] == ["Hook", "Outro"]
    # The Desktop card's record and details, from the profile's own plugin data dir.
    from elevenlabs_music_plugin import provenance

    row = provenance.find(result["track_id"])
    assert row["kind"] == "song" and row["file_name"] == path.name and row["stored_for_editing"] is True
    assert provenance.details(result["track_id"])["words_timestamps"][0]["word"] == "hello"
    assert provenance.audio_path(row) == path.resolve()


def test_chat_apps_get_a_job_id_and_a_wake_instead_of_a_held_chat(loaded):
    from gateway.session_context import clear_session_vars, set_session_vars

    dispatch, stand_in, _home = loaded
    stand_in.gate = threading.Event()
    tokens = set_session_vars(platform="telegram", chat_id="42", session_key="agent:main:telegram:dm:42")
    try:
        started = dispatch("elevenlabs_music_compose", {"prompt": "slow lofi", "length_seconds": 30})
        assert started["status"] == "running", started
        assert "background=true" in started["next_step"] and started["job_id"] in started["next_step"]
        stand_in.gate.set()
        finished = dispatch("elevenlabs_music_job", {"job_id": started["job_id"], "wait_seconds": 10})
    finally:
        clear_session_vars(tokens)
    assert finished["success"] is True and finished["media_tag"].startswith("MEDIA:"), finished
    assert len(stand_in.requests) == 1  # the job never bills twice


@pytest.mark.parametrize("tool,args,fragment", [
    ("elevenlabs_music_compose", {"prompt": ""}, "prompt is required"),
    ("elevenlabs_music_compose", {"prompt": "x", "length_seconds": True}, "length_seconds"),
    ("elevenlabs_music_compose", {"prompt": "x", "length_seconds": 601}, "between 3 and 600"),
    ("elevenlabs_music_compose_plan", {"chunks": [{"text": "[A]", "duration_ms": 2000, "positive_styles": ["a"]}]},
     "duration_ms"),
    ("elevenlabs_music_compose_plan", {"chunks": [{"text": "[A]", "duration_ms": 9000, "positive_styles": ["a"],
                                                   "conditioning_ref": {"song_id": "abcdefgh", "range": {"start_ms": 0, "end_ms": 31000}}}]},
     "at most 30 s"),
    ("elevenlabs_music_compose_plan", {"chunks": [{"text": "[A]", "duration_ms": 9000, "positive_styles": ["a"]}],
                                       "model": "music_v1"}, "model must be one of"),
    ("elevenlabs_music_stems", {"track_id": "a1b2c3d4e5f6", "file_path": "/x.mp3"}, "exactly one"),
    ("elevenlabs_music_job", {"job_id": "../../etc"}, "job_id"),
])
def test_bad_input_never_reaches_elevenlabs(loaded, tool, args, fragment):
    dispatch, stand_in, _home = loaded
    result = dispatch(tool, args)
    assert fragment in result.get("error", ""), result
    assert stand_in.requests == []


def test_stems_unpack_each_audio_file_and_skip_anything_else(loaded):
    dispatch, stand_in, home = loaded
    song = dispatch("elevenlabs_music_compose", {"prompt": "acoustic"})
    result = dispatch("elevenlabs_music_stems", {"track_id": song["track_id"], "stems": "two"})
    assert result["success"] is True, result
    request = stand_in.requests[-1]
    assert request["path"] == "/v1/music/stem-separation" and request["type"].startswith("multipart/form-data")
    assert b'name="stem_variation_id"\r\n\r\ntwo_stems_v1' in request["body"]
    assert sorted(s["stem"] for s in result["stems"]) == ["instrumental", "vocals"]
    audio_dir = home / "cache" / "generated" / "audio"
    assert all(Path(s["file_path"]).parent == audio_dir for s in result["stems"])
    assert not list(home.rglob("escape.txt"))
    from elevenlabs_music_plugin import provenance

    assert {provenance.find(s["track_id"])["parent_id"] for s in result["stems"]} == {song["track_id"]}


def test_a_setting_outside_its_choices_is_refused(loaded):
    dispatch, stand_in, home = loaded
    config = yaml.safe_load((home / "config.yaml").read_text())
    config["plugins"]["entries"][PLUGIN]["settings"]["output_format"] = "wav_44100"
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    result = dispatch("elevenlabs_music_compose", {"prompt": "x"})
    assert "output_format" in result["error"] and stand_in.requests == []
