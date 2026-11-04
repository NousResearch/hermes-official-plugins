"""The plugin, installed into a temp HERMES_HOME and enabled there, is loaded by core's real
PluginManager; its tool is dispatched through the real registry against loopback stand-ins for
``POST /v1/sound-generation`` that enforce the request shape of ElevenLabs' published OpenAPI spec.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import hermes_yaml as yaml
import pytest

PLUGIN = "elevenlabs-sound-effects"
TOOL = "elevenlabs_sound_effect"
AUDIO = b"RIFF\x24\x00\x00\x00WAVEfmt fake-sound-effect-bytes"
# From https://api.elevenlabs.io/openapi.json: Body_Sound_Generation_v1_sound_generation_post.
BODY_FIELDS = {"text", "loop", "duration_seconds", "prompt_influence", "model_id"}
MODELS = {"eleven_text_to_sound_v2", "eleven_text_to_sound_v3"}


class StandIn:
    """Records each request; answers ``reply`` = (status, headers, body bytes), or calls it with the
    request handler when it is a function (misbehaving-server scenarios)."""

    def __init__(self):
        self.requests: list[dict] = []
        self.reply = (200, {"Content-Type": "audio/wav", "character-cost": "120"}, AUDIO)
        stand_in = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                url = urlsplit(self.path)
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stand_in.requests.append({"path": url.path, "query": parse_qs(url.query),
                                          "key": self.headers.get("xi-api-key"), "body": body})
                problem = (url.path != "/v1/sound-generation" and "path"
                           or set(body) - BODY_FIELDS and f"unknown fields {sorted(set(body) - BODY_FIELDS)}"
                           or not isinstance(body.get("text"), str) and "text"
                           or body.get("model_id", "eleven_text_to_sound_v2") not in MODELS and "model_id")
                if callable(stand_in.reply) and not problem:
                    try:
                        stand_in.reply(self)
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # the client hung up on purpose
                    return
                status, headers, payload = (
                    (422, {"Content-Type": "application/json"},
                     json.dumps({"detail": [{"loc": ["body"], "msg": problem}]}).encode())
                    if problem else stand_in.reply)
                self.send_response(status)
                for name, value in {"Content-Length": str(len(payload)), **headers}.items():
                    self.send_header(name, value)
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client hung up on purpose (oversize body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads, self.server.block_on_close = True, False  # slow handlers
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stand_ins():
    servers = [StandIn(), StandIn()]
    yield servers
    for server in servers:
        server.close()


def _write_settings(home: Path, **settings) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {
        "enabled": [PLUGIN], "entries": {PLUGIN: {"settings": settings}}}}), encoding="utf-8")


@pytest.fixture
def loaded(_hermes_home, plugin_root, stand_ins, monkeypatch):
    """Install + enable the plugin with user settings, load it through core, yield a dispatcher."""
    from hermes_cli import plugins as plugins_mod
    from hermes_cli.plugins import PluginManager
    from tools.registry import registry

    shutil.copytree(plugin_root, _hermes_home / "plugins" / PLUGIN,
                    ignore=shutil.ignore_patterns("tests", "__pycache__", ".pytest_cache"))
    _write_settings(_hermes_home, output_format="wav_48000", sound_model="eleven_text_to_sound_v3",
                    api_base_url=stand_ins[0].url)
    empty_bundled = _hermes_home.parent / "bundled"
    empty_bundled.mkdir()
    monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: empty_bundled)
    manager = PluginManager()
    manager.discover_and_load()
    entry = registry.get_entry(TOOL, scope=manager.scope_key)
    assert entry is not None, "the plugin's tool was not registered through real discovery"

    def call(args: dict) -> dict:
        return json.loads(registry.dispatch(TOOL, args, scope=manager.scope_key))

    try:
        yield entry, call
    finally:
        manager.unload()


def test_generates_a_sound_effect_with_the_users_settings(loaded, stand_ins, monkeypatch, _hermes_home):
    entry, call = loaded
    assert entry.toolset == "elevenlabs_sound_effects"
    assert entry.check_fn() is False  # no key: the tool stays out of the model's tool list

    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-test-key")
    assert entry.check_fn() is True
    result = call({"prompt": "Glass shattering on a tile floor!", "duration_seconds": 2.5,
                   "loop": "true", "prompt_influence": 0.7})

    (request,) = stand_ins[0].requests
    assert request["key"] == "xi-test-key"
    assert request["query"] == {"output_format": ["wav_48000"]}
    assert request["body"] == {"text": "Glass shattering on a tile floor!", "loop": True,
                               "duration_seconds": 2.5, "prompt_influence": 0.7,
                               "model_id": "eleven_text_to_sound_v3"}
    path = Path(result["file_path"])
    assert result["success"] is True and result["media_tag"] == f"MEDIA:{path}"
    assert path.parent == _hermes_home / "cache" / "generated" / "audio"
    assert path.name.startswith("sfx_glass-shattering-on-a-tile-floor_") and path.suffix == ".wav"
    assert path.read_bytes() == AUDIO
    assert result["billed_characters"] == "120"


def test_each_profile_gets_its_own_key_settings_and_output_dir(loaded, stand_ins, _hermes_home):
    """One process serving two profiles (the multiplexed gateway): key, settings and the output dir
    are resolved per call from the calling profile, never cached at registration."""
    from agent.secret_scope import (
        reset_multiplex_context,
        reset_secret_scope,
        set_multiplex_context,
        set_secret_scope,
    )
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    _, call = loaded
    other = _hermes_home / "profiles" / "b"
    _write_settings(other, output_format="opus_48000_128", api_base_url=stand_ins[1].url)
    results = []
    for home, key in ((_hermes_home, "key-a"), (other, "key-b"), (_hermes_home, "key-a")):
        tokens = (set_multiplex_context(True), set_hermes_home_override(home),
                  set_secret_scope({"ELEVENLABS_API_KEY": key}, profile_home=str(home)))
        try:
            results.append((home, call({"prompt": "door creak"})))
        finally:
            reset_secret_scope(tokens[2])
            reset_hermes_home_override(tokens[1])
            reset_multiplex_context(tokens[0])

    assert [r["key"] for r in stand_ins[0].requests] == ["key-a", "key-a"]
    assert [r["key"] for r in stand_ins[1].requests] == ["key-b"]
    assert stand_ins[1].requests[0]["query"] == {"output_format": ["opus_48000_128"]}
    assert stand_ins[1].requests[0]["body"]["model_id"] == "eleven_text_to_sound_v2"  # B's default
    for home, result in results:
        path = Path(result["file_path"])
        assert path.parent == home / "cache" / "generated" / "audio"
        assert path.suffix == (".opus" if home == other else ".wav")


def test_bad_input_never_bills_and_errors_reach_the_model(loaded, stand_ins, monkeypatch, _hermes_home):
    entry, call = loaded
    monkeypatch.setattr(entry.handler.__globals__["_sfx"], "_DEADLINE_SECONDS", 10.0)  # bound the endless body
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-test-key")

    for args in ({"prompt": "   "}, {"prompt": "thunder", "duration_seconds": 45},
                 {"prompt": "thunder", "duration_seconds": True},
                 {"prompt": "thunder", "prompt_influence": "lots"}, {"prompt": "thunder", "loop": {"x": 1}}):
        assert "error" in call(args), args
    _write_settings(_hermes_home, output_format="pcm_16000", api_base_url=stand_ins[0].url)
    assert "output_format" in call({"prompt": "thunder"})["error"]
    assert stand_ins[0].requests == []  # every refusal happened before a (billed) request

    _write_settings(_hermes_home, api_base_url=stand_ins[0].url)
    stand_ins[0].reply = (401, {"Content-Type": "application/json"},
                          json.dumps({"detail": {"status": "quota_exceeded",
                                                 "message": "You have 0 credits remaining."}}).encode())
    error = call({"prompt": "thunder"})["error"]
    assert "HTTP 401" in error and "0 credits remaining" in error

    stand_ins[0].reply = (200, {"Content-Type": "audio/mpeg", "Content-Length": str(60 * 1024 * 1024)}, b"x")
    assert "byte cap" in call({"prompt": "thunder"})["error"]
    stand_ins[0].reply = _endless_chunked_body
    assert "byte cap" in call({"prompt": "thunder"})["error"]
    assert not list((_hermes_home / "cache" / "generated" / "audio").glob("*"))


def _endless_chunked_body(handler) -> None:
    handler.send_response(200)
    handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()
    chunk = b"x" * (1 << 20)
    while True:
        handler.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))


def test_slow_server_hits_the_deadline_and_bad_keys_never_leave(loaded, stand_ins, monkeypatch):
    entry, call = loaded
    plugin = entry.handler.__globals__["_sfx"]  # the module core's loader imported
    monkeypatch.setattr(plugin, "_DEADLINE_SECONDS", 1.0)

    def trickle_headers(handler):  # never finishes its status line; only the deadline ends it
        handler.wfile.write(b"HTTP/1.1 200")
        for _ in range(100):
            handler.wfile.write(b" ")
            handler.wfile.flush()
            time.sleep(0.1)

    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-test-key")
    stand_ins[0].reply = trickle_headers
    started = time.monotonic()
    assert "within 1 seconds" in call({"prompt": "thunder"})["error"]
    assert time.monotonic() - started < 5

    for key in ("xi-key\nSECRETTAIL", "xi-kéy"):
        monkeypatch.setenv("ELEVENLABS_API_KEY", key)
        error = call({"prompt": "thunder"})["error"]
        assert "re-enter the key" in error and "SECRETTAIL" not in error
    assert len(stand_ins[0].requests) == 1  # only the deadline case reached the server


def _detail(status: str, message: str) -> bytes:
    """An error body in the shape api.elevenlabs.io sends (captured live)."""
    return json.dumps({"detail": {"type": "authentication_error", "code": status, "message": message,
                                  "status": status, "request_id": "0" * 32}}).encode()


def test_overlong_prompts_never_leave_and_key_errors_say_which_fix(loaded, stand_ins, monkeypatch):
    """The live API refuses descriptions over 450 characters (v2 and v3) and answers a bad key, a
    key without the sound permission and an empty balance with different bodies: the model gets
    the limit up front and each error names its own fix."""
    entry, call = loaded
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-test-key")
    assert entry.schema["parameters"]["properties"]["prompt"]["maxLength"] == 450
    assert "450 characters" in call({"prompt": "k" * 451})["error"]
    assert stand_ins[0].requests == []
    assert call({"prompt": "k" * 450})["success"] is True

    json_type = {"Content-Type": "application/json"}
    for status, code, message, fix in (
            (400, "invalid_api_key", "API key is invalid.", "not a valid ElevenLabs key"),
            (401, "missing_permissions",
             "The API key you used is missing the permission sound_generation to execute this operation.",
             "enable Sound Effects access"),
            (401, "quota_exceeded", "You have 0 credits remaining.", "out of credits")):
        stand_ins[0].reply = (status, json_type, _detail(code, message))
        error = call({"prompt": "thunder"})["error"]
        assert message in error and fix in error, error
        assert code == "quota_exceeded" or "credits" not in error, error


def test_unanswered_calls_say_dont_retry_and_missing_keys_name_the_profile(loaded, stand_ins, monkeypatch,
                                                                           _hermes_home):
    """Live, a model retried an unanswered call 3-4 times (7-9 minutes) when the error only said
    "Could not reach ElevenLabs", which is also wrong for a server that answered and then stalled.
    A missing key names the calling profile's .env, not the default profile's."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    entry, call = loaded
    plugin = entry.handler.__globals__["_sfx"]
    monkeypatch.setattr(plugin, "_CONNECT_READ_TIMEOUT", 0.5)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-test-key")

    def stall_after_headers(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "1000")
        handler.end_headers()
        handler.wfile.flush()
        time.sleep(3)

    stand_ins[0].reply = stall_after_headers
    error = call({"prompt": "thunder"})["error"]
    assert "did not answer within 0.5 seconds" in error and "Could not reach" not in error, error
    assert "Retrying right away will not help" in error
    monkeypatch.setattr(plugin, "_DEADLINE_SECONDS", 0.2)
    assert "Retrying right away will not help" in call({"prompt": "thunder"})["error"]

    from hermes_constants import display_hermes_home

    monkeypatch.delenv("ELEVENLABS_API_KEY")
    default_env = f"{display_hermes_home()}/.env"  # host-specific form (Windows shows the temp-dir path)
    assert default_env in call({"prompt": "thunder"})["error"]
    token = set_hermes_home_override(_hermes_home / "profiles" / "b")
    try:
        error = call({"prompt": "thunder"})["error"]
        assert "profiles/b/.env" in error.replace("\\", "/") and default_env not in error, error
    finally:
        reset_hermes_home_override(token)


def test_a_length_written_into_the_prompt_must_be_passed_as_duration(loaded, stand_ins, monkeypatch):
    """Live, models wrote the user's length into the prompt ("... about 1 second") and left
    duration_seconds out: ElevenLabs ignores it, bills its flat automatic-length rate and returns a
    longer clip the model then regenerates. That call is refused before it is billed."""
    _, call = loaded
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-test-key")
    for prompt in ("soft UI click, about 1 second", "a 2-second whoosh", "rain for 10 seconds"):
        error = call({"prompt": prompt})["error"]
        assert "duration_seconds" in error, error
    assert stand_ins[0].requests == []

    assert call({"prompt": "soft UI click, about 1 second", "duration_seconds": 1})["success"] is True
    assert call({"prompt": "90s hip-hop drum loop, 90 BPM", "loop": True})["success"] is True
    assert [r["body"].get("duration_seconds") for r in stand_ins[0].requests] == [1.0, None]


def test_the_key_never_travels_over_plain_http_except_to_this_machine(loaded, stand_ins, monkeypatch,
                                                                       _hermes_home):
    """``xi-api-key`` is a header: an ``http://`` endpoint would send it in cleartext. Only a loopback
    stand-in (localhost, 127.0.0.0/8, ::1) may use http; anything else is refused before any request."""
    _, call = loaded
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-test-key")
    for url in ("http://api.elevenlabs.io", "http://10.0.0.5:8080", "http://example.com", "ftp://api.elevenlabs.io"):
        _write_settings(_hermes_home, api_base_url=url)
        assert "https" in call({"prompt": "thunder"})["error"], url
    port = stand_ins[0].url.rsplit(":", 1)[1]
    for url in (f"http://localhost:{port}", f"http://127.0.0.1:{port}"):
        _write_settings(_hermes_home, api_base_url=url)
        assert call({"prompt": "thunder"})["success"] is True, url
    assert len(stand_ins[0].requests) == 2
