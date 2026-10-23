"""Invariants of the skill's stdlib/Node helper scripts (no Blender, Chromium or ffmpeg needed)."""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "meshy-3d" / "scripts"


def _glb(path: Path, **doc) -> Path:
    """One triangle; extra glTF fields (skins, nodes, attributes) merged over the default document."""
    blob = struct.pack("<9f", -1, 0, 0, 1, 0, 0, 0, 1, 0)
    base = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
            "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
            "buffers": [{"byteLength": len(blob)}], "bufferViews": [{"buffer": 0, "byteLength": len(blob)}],
            "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3",
                           "min": [-1, 0, 0], "max": [1, 1, 0]}]}
    base.update(doc)
    j = json.dumps(base).encode()
    j += b" " * (-len(j) % 4)
    path.write_bytes(struct.pack("<4sII", b"glTF", 2, 28 + len(j) + len(blob)) + struct.pack("<I4s", len(j), b"JSON")
                     + j + struct.pack("<I4s", len(blob), b"BIN\0") + blob)
    return path


def _stats(path: Path) -> dict:
    out = subprocess.run([sys.executable, str(SCRIPTS / "glb_stats.py"), str(path)], capture_output=True, text=True,
                         check=True).stdout
    return json.loads(out)


def test_glb_stats_counts_skin_bindings_not_skin_definitions(tmp_path):
    # A valid file can carry a skin no mesh uses: that is not a rigged model.
    unused = _stats(_glb(tmp_path / "unused.glb", nodes=[{"mesh": 0}, {"name": "J"}], skins=[{"joints": [1]}]))
    assert (unused["skins"], unused["joints"], unused["skinned_mesh_nodes"]) == (1, 1, 0)
    # Bound and weighted: the mesh node references the skin and its primitive has JOINTS_0/WEIGHTS_0.
    bound = _stats(_glb(tmp_path / "bound.glb", nodes=[{"mesh": 0, "skin": 0}, {"name": "J"}],
                        scenes=[{"nodes": [0, 1]}], skins=[{"joints": [1]}],
                        meshes=[{"primitives": [{"attributes": {"POSITION": 0, "JOINTS_0": 0, "WEIGHTS_0": 0}}]}]))
    assert bound["skinned_mesh_nodes"] == 1


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_render_preview_refuses_an_output_that_is_the_source(tmp_path):
    src = _glb(tmp_path / "model.glb")
    alias = tmp_path / "model.mp4"
    os.link(src, alias)  # a hard link: a different name for the same file
    before = src.read_bytes()
    r = subprocess.run(check=False, args=["node", str(SCRIPTS / "render_preview.mjs"), str(src), "--out", str(alias)],
                       capture_output=True, text=True, timeout=60, env={**os.environ, "HERMES_HOME": str(tmp_path)})
    result = json.loads(r.stdout.strip().splitlines()[-1])
    assert r.returncode == 1 and not result["ok"] and "refusing to overwrite the source" in result["error"]
    assert src.read_bytes() == before
