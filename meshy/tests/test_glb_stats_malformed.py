"""A GLB whose JSON chunk is not the object the spec requires.

stats() indexes into the document without checking its shape, and main() only
catches OSError/ValueError/KeyError/IndexError, so a malformed model escapes as
AttributeError/TypeError. The script's contract is one JSON line per file: a
traceback and an empty stdout break that, and the loop stops so every later file
is skipped too.
"""

from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "meshy-3d" / "scripts"
SCRIPT = SCRIPTS / "glb_stats.py"


def _glb_with_json(payload: bytes, path: Path) -> Path:
    """A .glb wrapper whose JSON chunk is whatever bytes the caller supplies."""
    chunk = payload + b" " * (-len(payload) % 4)
    path.write_bytes(struct.pack("<4sII", b"glTF", 2, 28 + len(chunk))
                     + struct.pack("<I4s", len(chunk), b"JSON") + chunk
                     + struct.pack("<I4s", 0, b"BIN\0"))
    return path


MALFORMED = {
    "json-is-a-list": b"[]",
    "json-is-a-number": b"5",
    "json-is-a-string": b'"hello"',
    "json-is-null": b"null",
    "meshes-is-a-mapping": b'{"meshes": {"a": 1}}',
    "mesh-entry-is-a-number": b'{"meshes": [1]}',
    "primitive-entry-is-a-number": b'{"meshes": [{"primitives": [1]}]}',
    "attributes-is-a-list": b'{"meshes": [{"primitives": [{"attributes": ["POSITION"]}]}]}',
    "accessor-entry-is-a-number": b'{"meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}], "accessors": [1]}',
    "scene-nodes-is-a-number": b'{"scenes": [{"nodes": 5}]}',
    "node-entry-is-a-number": b'{"nodes": [1]}',
    "children-is-a-number": b'{"nodes": [{"children": 7}]}',
    "animation-entry-is-a-number": b'{"animations": [5]}',
    "skin-entry-is-a-number": b'{"skins": [5]}',
}


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_a_malformed_document_still_prints_one_json_line(tmp_path, name):
    """The documented contract: one JSON object per file, never a traceback."""
    model = _glb_with_json(MALFORMED[name], tmp_path / f"{name}.glb")

    result = subprocess.run([sys.executable, str(SCRIPT), str(model)],
                            capture_output=True, text=True)

    assert result.stderr.count("Traceback") == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected one JSON line, got {lines!r}"
    reported = json.loads(lines[0])
    assert reported["path"] == str(model)
    assert reported["error"]
    assert result.returncode == 1


def test_a_bad_file_does_not_stop_the_files_after_it(tmp_path):
    """Each argument is handled independently, so one bad model cannot hide the rest."""
    bad = _glb_with_json(b"[]", tmp_path / "bad.glb")
    good = tmp_path / "good.glb"
    good.write_bytes(struct.pack("<4sII", b"glTF", 2, 28 + 4)
                     + struct.pack("<I4s", 4, b"JSON") + b"{}  "
                     + struct.pack("<I4s", 0, b"BIN\0"))

    result = subprocess.run([sys.executable, str(SCRIPT), str(bad), str(good)],
                            capture_output=True, text=True)

    assert result.stderr.count("Traceback") == 0, result.stderr
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert [Path(item["path"]).name for item in lines] == ["bad.glb", "good.glb"]
    assert "error" in lines[0]
    assert "error" not in lines[1]
    assert result.returncode == 1
