"""render_preview.mjs launch and failure contracts, driven through real `node` (no Chromium, ffmpeg or network)."""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "meshy-3d" / "scripts" / "render_preview.mjs"
NODE = shutil.which("node") or "node"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _node_version() -> tuple[int, ...]:
    out = subprocess.run([NODE, "-p", "process.versions.node"], capture_output=True, text=True, encoding="utf-8",
                         check=True).stdout
    return tuple(int(x) for x in out.strip().split("."))


def _glb(path: Path) -> Path:
    """A one-triangle GLB: enough to pass the script's input checks."""
    blob = struct.pack("<9f", -1, 0, 0, 1, 0, 0, 0, 1, 0)
    doc = {"asset": {"version": "2.0"}, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
           "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}], "buffers": [{"byteLength": len(blob)}],
           "bufferViews": [{"buffer": 0, "byteLength": len(blob)}],
           "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3"}]}
    j = json.dumps(doc).encode()
    j += b" " * (-len(j) % 4)
    path.write_bytes(struct.pack("<4sII", b"glTF", 2, 28 + len(j) + len(blob)) + struct.pack("<I4s", len(j), b"JSON")
                     + j + struct.pack("<I4s", len(blob), b"BIN\0") + blob)
    return path


def _env(home: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HERMES_", "MESHY_PREVIEW_"))}
    return {**env, "HERMES_HOME": str(home), **extra}


def _last_json(stdout: str) -> dict:
    return json.loads(stdout.strip().splitlines()[-1])


def test_offline_first_run_reports_a_json_error_not_a_stack_trace(tmp_path):
    # First run with no three.js cache and no network: the download fails. The script must still end in its
    # one {"ok": false} line. Copied out of the plugin tree so a dev checkout's node_modules/three is not found.
    if not (_node_version() >= (24,) or (22, 21) <= _node_version() < (23,)):
        pytest.skip("this node ignores HTTPS_PROXY for fetch (NODE_USE_ENV_PROXY needs 22.21+/24+)")
    script = tmp_path / "scripts" / SCRIPT.name
    script.parent.mkdir()
    shutil.copy(SCRIPT, script)
    home = tmp_path / "home"
    home.mkdir()
    env = _env(home, HTTPS_PROXY="http://127.0.0.1:9", https_proxy="http://127.0.0.1:9", NODE_USE_ENV_PROXY="1",
               NO_PROXY="", no_proxy="")
    # --chrome only has to name an existing file: the three.js download runs before the browser starts.
    r = subprocess.run([NODE, str(script), str(_glb(tmp_path / "m.glb")), "--chrome", sys.executable],
                       capture_output=True, text=True, encoding="utf-8", timeout=120, env=env, check=False)
    assert r.returncode != 0
    result = _last_json(r.stdout)
    assert result["ok"] is False and "could not download three" in result["error"]
    assert "triggerUncaughtException" not in r.stderr


def _cached_three(home: Path) -> None:
    """A cached three.js the script accepts without a download (only its package.json is read before the browser)."""
    three = home / "cache" / "meshy-preview" / "three-0.180.0" / "package"
    three.mkdir(parents=True)
    (three / "package.json").write_text('{"version": "0.180.0"}', encoding="utf-8")


def test_any_unexpected_failure_still_ends_in_one_json_error_line(tmp_path):
    # Not every failure is one the script names: here its scratch dir cannot be created (temp dir is gone).
    home = tmp_path / "home"
    _cached_three(home)
    gone = str(tmp_path / "no-such-tmp")
    r = subprocess.run([NODE, str(SCRIPT), str(_glb(tmp_path / "m.glb")), "--chrome", sys.executable],
                       capture_output=True, text=True, encoding="utf-8", timeout=120, check=False,
                       env=_env(home, TMPDIR=gone, TEMP=gone, TMP=gone))
    assert r.returncode != 0
    result = _last_json(r.stdout)
    assert result["ok"] is False and "no-such-tmp" in result["error"]
    assert "triggerUncaughtException" not in r.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="the fake browser is a POSIX sh script")
def test_chromium_is_launched_without_the_desktop_keyring(tmp_path):
    # A fresh profile otherwise asks kwallet/gnome-keyring (D-Bus) or the macOS Keychain for its cookie key at
    # startup, which can block past the start timeout. The fake browser records its argv and exits.
    home = tmp_path / "home"
    _cached_three(home)  # no network
    argv_file = tmp_path / "argv.txt"
    chrome = tmp_path / "fake-chrome"
    chrome.write_text(f"#!/bin/sh\nfor a in \"$@\"; do printf '%s\\n' \"$a\"; done > '{argv_file}'\n",
                      encoding="utf-8")
    chrome.chmod(0o755)
    r = subprocess.run([NODE, str(SCRIPT), str(_glb(tmp_path / "m.glb")), "--chrome", str(chrome)],
                       capture_output=True, text=True, encoding="utf-8", timeout=120, env=_env(home), check=False)
    assert r.returncode != 0 and _last_json(r.stdout)["error"] == "Chromium did not start"
    argv = argv_file.read_text(encoding="utf-8").splitlines()
    assert "--password-store=basic" in argv
    if sys.platform == "darwin":
        assert "--use-mock-keychain" in argv
