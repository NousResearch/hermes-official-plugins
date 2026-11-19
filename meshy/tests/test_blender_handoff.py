"""blender_handoff.py decimation, run in a real background Blender (skipped when Blender is not installed)."""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "meshy-3d" / "scripts" / "blender_handoff.py"
BLENDER = shutil.which("blender") or "blender"
pytestmark = pytest.mark.skipif(shutil.which("blender") is None, reason="blender not installed")


def _split_grid_glb(path: Path, n: int = 40, wire: bool = False) -> Path:
    """n x n quads whose vertices are not shared between quads, the way a glTF splits along every UV/normal
    seam: 2 * n * n triangles, 4 * n * n vertices. ``wire`` adds a LINES primitive: one edge, two vertices
    that belong to no face."""
    pos, idx = [], []
    for y in range(n):
        for x in range(n):
            a = len(pos) // 3
            pos += [x / n, y / n, 0, (x + 1) / n, y / n, 0, (x + 1) / n, (y + 1) / n, 0, x / n, (y + 1) / n, 0]
            idx += [a, a + 1, a + 2, a, a + 2, a + 3]
    pb, ib = struct.pack(f"<{len(pos)}f", *pos), struct.pack(f"<{len(idx)}I", *idx)
    lb = struct.pack("<6f", 0, 0, 0.5, 1, 1, 0.5) if wire else b""
    blob = pb + ib + lb
    doc = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
           "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
           "buffers": [{"byteLength": len(blob)}],
           "bufferViews": [{"buffer": 0, "byteLength": len(pb)},
                           {"buffer": 0, "byteOffset": len(pb), "byteLength": len(ib)}],
           "accessors": [{"bufferView": 0, "componentType": 5126, "count": len(pos) // 3, "type": "VEC3",
                          "min": [0, 0, 0], "max": [1, 1, 0]},
                         {"bufferView": 1, "componentType": 5125, "count": len(idx), "type": "SCALAR"}]}
    if wire:
        doc["bufferViews"].append({"buffer": 0, "byteOffset": len(pb) + len(ib), "byteLength": len(lb)})
        doc["accessors"].append({"bufferView": 2, "componentType": 5126, "count": 2, "type": "VEC3",
                                 "min": [0, 0, 0.5], "max": [1, 1, 0.5]})
        doc["meshes"][0]["primitives"].append({"attributes": {"POSITION": 2}, "mode": 1})
    j = json.dumps(doc).encode()
    j += b" " * (-len(j) % 4)
    path.write_bytes(struct.pack("<4sII", b"glTF", 2, 28 + len(j) + len(blob)) + struct.pack("<I4s", len(j), b"JSON")
                     + j + struct.pack("<I4s", len(blob), b"BIN\0") + blob)
    return path


def _handoff(*args: str) -> dict:
    r = subprocess.run([BLENDER, "--background", "--factory-startup", "--python", str(SCRIPT), "--", *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600, check=False)
    line = next((x for x in r.stdout.splitlines() if x.startswith("HANDOFF_RESULT ")), None)
    assert line, f"no HANDOFF_RESULT (exit {r.returncode}): {r.stdout[-1500:]} {r.stderr[-1500:]}"
    return json.loads(line[len("HANDOFF_RESULT "):])


def test_saved_blend_holds_only_the_decimated_mesh(tmp_path):
    # Collapse cannot merge across split vertices: it drops the collapsed faces and used to leave their vertices
    # and edges in the mesh. The GLB export skipped them; the saved .blend and the FBX kept them.
    src = _split_grid_glb(tmp_path / "grid.glb")
    res = _handoff("--input", str(src), "--out-dir", str(tmp_path / "out"), "--tris", "200", "--export", "both")
    assert res["ok"], res
    assert res["source"]["triangles"] == 3200 and res["decimation"]["after"] <= 200
    blend = res["verify"]["blend"]  # the .blend reopened in a fresh Blender
    assert blend["triangles"] == res["decimation"]["after"]
    # Every vertex of a pure triangle mesh belongs to some triangle: never more than three per triangle.
    assert blend["vertices"] <= 3 * blend["triangles"], blend
    assert res["result"]["vertices"] == blend["vertices"]
    assert res["verify"]["glb"]["triangles"] == blend["triangles"]
    fbx = res["verify"]["fbx"]  # the FBX exporter wrote the leftovers too
    assert fbx["triangles"] == blend["triangles"] and fbx["vertices"] <= 3 * fbx["triangles"], fbx


def test_decimation_keeps_source_geometry_that_has_no_face(tmp_path):
    # Only the collapse's debris goes: a source wire edge (a glTF LINES primitive) is kept in the .blend.
    plain = _handoff("--input", str(_split_grid_glb(tmp_path / "plain.glb")), "--out-dir", str(tmp_path / "a"),
                     "--tris", "200", "--export", "none")
    wired = _handoff("--input", str(_split_grid_glb(tmp_path / "wired.glb", wire=True)),
                     "--out-dir", str(tmp_path / "b"), "--tris", "200", "--export", "none")
    assert plain["ok"] and wired["ok"], (plain, wired)
    assert wired["verify"]["blend"]["triangles"] == plain["verify"]["blend"]["triangles"]
    assert wired["verify"]["blend"]["vertices"] == plain["verify"]["blend"]["vertices"] + 2
