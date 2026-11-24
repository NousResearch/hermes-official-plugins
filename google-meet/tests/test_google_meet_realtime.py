"""Tests for google_meet_plugin.realtime.openai_client (v2).

Uses a scripted fake WebSocket — no network, no API key required.
"""

from __future__ import annotations

import base64
import json
import sys
import types

import pytest


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    yield hermes_home


# ---------------------------------------------------------------------------
# Fake WebSocket
# ---------------------------------------------------------------------------


class _FakeWS:
    """Scripted WS: send() records frames, recv() pops a queue."""

    def __init__(self, recv_frames: list):
        self.sent: list[dict] = []
        self._recv_q: list = list(recv_frames)
        self.closed = False

    def send(self, payload):
        # Always accept str payloads — client encodes JSON with json.dumps.
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode()
        self.sent.append(json.loads(payload))

    def recv(self, timeout=None):
        if not self._recv_q:
            raise RuntimeError("fake ws: no more frames")
        frame = self._recv_q.pop(0)
        if isinstance(frame, dict):
            return json.dumps(frame)
        return frame

    def close(self):
        self.closed = True


def _install_fake_websockets(monkeypatch, fake_ws):
    """Install a fake ``websockets.sync.client`` module in sys.modules."""
    mod_websockets = types.ModuleType("websockets")
    mod_sync = types.ModuleType("websockets.sync")
    mod_sync_client = types.ModuleType("websockets.sync.client")

    captured = {"url": None, "headers": None, "kwargs": None}

    def _connect(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        captured["headers"] = (
            kwargs.get("additional_headers") or kwargs.get("extra_headers")
        )
        return fake_ws

    mod_sync_client.connect = _connect
    mod_sync.client = mod_sync_client
    mod_websockets.sync = mod_sync

    monkeypatch.setitem(sys.modules, "websockets", mod_websockets)
    monkeypatch.setitem(sys.modules, "websockets.sync", mod_sync)
    monkeypatch.setitem(sys.modules, "websockets.sync.client", mod_sync_client)
    return captured


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------


def test_connect_sends_session_update_with_voice_and_instructions(monkeypatch):
    from google_meet_plugin.realtime.openai_client import RealtimeSession

    ws = _FakeWS(recv_frames=[])
    captured = _install_fake_websockets(monkeypatch, ws)

    sess = RealtimeSession(
        api_key="sk-test",
        model="gpt-realtime",
        voice="verse",
        instructions="Be brief.",
    )
    sess.connect()

    # Auth + beta headers set.
    assert captured["url"].startswith("wss://api.openai.com/v1/realtime")
    assert "model=gpt-realtime" in captured["url"]
    headers = captured["headers"] or []
    hdict = dict(headers)
    assert hdict.get("Authorization") == "Bearer sk-test"
    assert hdict.get("OpenAI-Beta") == "realtime=v1"

    # First frame sent must be session.update with the right shape.
    assert len(ws.sent) == 1
    update = ws.sent[0]
    assert update["type"] == "session.update"
    s = update["session"]
    assert s["voice"] == "verse"
    assert s["instructions"] == "Be brief."
    assert set(s["modalities"]) == {"audio", "text"}
    assert s["output_audio_format"] == "pcm16"
    assert s["input_audio_format"] == "pcm16"


# ---------------------------------------------------------------------------
# speak()
# ---------------------------------------------------------------------------


def test_speak_sends_create_and_response_and_writes_audio(monkeypatch, tmp_path):
    from google_meet_plugin.realtime.openai_client import RealtimeSession

    audio_bytes = b"\x01\x02\x03\x04PCM!"
    b64 = base64.b64encode(audio_bytes).decode()

    recv_frames = [
        {"type": "response.created"},
        {"type": "response.audio.delta", "delta": b64},
        {"type": "response.audio.delta", "delta": base64.b64encode(b"more").decode()},
        {"type": "response.done"},
    ]
    ws = _FakeWS(recv_frames=recv_frames)
    _install_fake_websockets(monkeypatch, ws)

    sink = tmp_path / "out.pcm"
    sess = RealtimeSession(api_key="sk-test", audio_sink_path=sink)
    sess.connect()
    result = sess.speak("Hello everyone.")

    # Frames sent after session.update: conversation.item.create then response.create.
    types_sent = [f["type"] for f in ws.sent]
    assert types_sent == ["session.update", "conversation.item.create", "response.create"]

    item = ws.sent[1]["item"]
    assert item["role"] == "user"
    assert item["content"][0]["type"] == "input_text"
    assert item["content"][0]["text"] == "Hello everyone."

    resp = ws.sent[2]["response"]
    assert resp["modalities"] == ["audio"]

    # Audio file got decoded + appended bytes.
    data = sink.read_bytes()
    assert data == audio_bytes + b"more"
    assert result["ok"] is True
    assert result["bytes_written"] == len(audio_bytes) + len(b"more")
    assert result["duration_ms"] >= 0.0


def test_close_is_idempotent_and_closes_ws(monkeypatch):
    from google_meet_plugin.realtime.openai_client import RealtimeSession

    ws = _FakeWS(recv_frames=[])
    _install_fake_websockets(monkeypatch, ws)

    sess = RealtimeSession(api_key="sk-test")
    sess.connect()
    sess.close()
    assert ws.closed is True
    # Second close is a no-op.
    sess.close()


# ---------------------------------------------------------------------------
# websockets dependency missing
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# RealtimeSpeaker
# ---------------------------------------------------------------------------


class _StubSession:
    def __init__(self):
        self.spoken: list[str] = []

    def speak(self, text, timeout=30.0):
        self.spoken.append(text)
        return {"ok": True, "bytes_written": len(text), "duration_ms": 1.0}


def test_speaker_run_until_stopped_processes_queue(tmp_path):
    from google_meet_plugin.realtime.openai_client import RealtimeSpeaker

    queue = tmp_path / "queue.jsonl"
    processed = tmp_path / "processed.jsonl"
    queue.write_text(
        json.dumps({"id": "a", "text": "hello one"}) + "\n"
        + json.dumps({"id": "b", "text": "hello two"}) + "\n"
    )

    stub = _StubSession()
    speaker = RealtimeSpeaker(stub, queue_path=queue, processed_path=processed)

    # Stop once the queue is empty.
    def _stop():
        return queue.exists() and queue.read_text().strip() == ""

    speaker.run_until_stopped(_stop, poll_interval=0.01)

    assert stub.spoken == ["hello one", "hello two"]

    # Processed file has both entries, in order.
    lines = [json.loads(l) for l in processed.read_text().splitlines() if l.strip()]
    assert [l["id"] for l in lines] == ["a", "b"]
    assert all(l["result"]["ok"] for l in lines)

    # Queue is empty (possibly empty string) after processing.
    assert queue.read_text().strip() == ""




def _wait_until(predicate, timeout=10.0, interval=0.01, what=None):
    """Return ``predicate()``'s first truthy value; fail after ``timeout`` s (a deadline, not a delay)."""
    import time

    deadline = time.monotonic() + timeout
    while not (value := predicate()):
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what or 'the condition'}")
        time.sleep(interval)
    return value


def _watch_file_lock_contention(monkeypatch):
    """An event set when any thread is refused by ``hermes_cli.auth._file_lock`` (proof a writer waits)."""
    import threading

    from hermes_cli import auth

    contended = threading.Event()
    real_kernel_lock = auth._kernel_lock

    def kernel_lock(lock_file, acquire):
        try:
            return real_kernel_lock(lock_file, acquire)
        except OSError as exc:
            if acquire and auth._is_lock_contention(exc):
                contended.set()
            raise

    monkeypatch.setattr(auth, "_kernel_lock", kernel_lock)
    return contended


def test_speaker_drain_keeps_a_say_enqueued_during_the_rewrite(tmp_path, monkeypatch):
    """A meet_say append landing between the speaker's post-speak re-read and its queue rewrite must
    survive the rewrite and be spoken (it was reported "enqueued" to the agent)."""
    import threading

    from google_meet_plugin import process_manager as pm
    from google_meet_plugin.realtime.openai_client import RealtimeSpeaker
    contended = _watch_file_lock_contention(monkeypatch)

    queue = tmp_path / "say_queue.jsonl"
    queue.write_text(json.dumps({"id": "a", "text": "first"}) + "\n")
    pm._write_active({"pid": 0, "meeting_id": "abc-defg-hij", "out_dir": str(tmp_path), "mode": "realtime",
                      "url": "https://meet.google.com/abc-defg-hij", "started_at": 0})
    stub = _StubSession()
    speaker = RealtimeSpeaker(stub, queue_path=queue)
    real_read = speaker._read_queue
    reads = {"n": 0}
    appended = threading.Event()
    peers: list[threading.Thread] = []

    def read_then_peer_appends():
        out = real_read()
        reads["n"] += 1
        if reads["n"] == 2:
            # The post-speak re-read: the agent process enqueues right after it, before the rewrite.
            peer = threading.Thread(target=lambda: (pm.enqueue_say("second"), appended.set()), daemon=True)
            peer.start()
            peers.append(peer)
            # Unserialized, the append lands now; serialized, it is seen blocking on the queue lock.
            _wait_until(lambda: appended.is_set() or contended.is_set(),
                       what="the append landed or blocked on the say-queue lock")
        return out

    speaker._read_queue = read_then_peer_appends

    def _stop():
        # Done once both are spoken, or once the peer's append has settled and nothing is left to speak.
        if len(stub.spoken) >= 2:
            return True
        return bool(peers) and not peers[0].is_alive() and not real_read()

    speaker.run_until_stopped(_stop, poll_interval=0.01)
    for peer in peers:
        peer.join(10)
    assert stub.spoken == ["first", "second"]
