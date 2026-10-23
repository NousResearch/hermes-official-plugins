#!/usr/bin/env python3
"""Print what one or more GLB files actually hold (stdlib only, no Blender).

    python3 glb_stats.py model.glb [more.glb ...]

One JSON line per file: {"path", "triangles", "vertices", "meshes", "primitives", "textures", "skins",
"joints", "skinned_mesh_nodes", "clips", "bytes"}. Counts every triangle primitive (mode 4) of every mesh,
once per mesh (not per instance). ``skins``/``joints`` count skin DEFINITIONS, which a file can carry unused;
``skinned_mesh_nodes`` > 0 (scene nodes with a mesh AND a skin whose primitives carry JOINTS_0/WEIGHTS_0)
is what means the mesh is bound to a skeleton. ``clips`` names every animation.
"""
from __future__ import annotations

import json
import os
import struct
import sys


def stats(path: str) -> dict:
    with open(path, "rb") as fh:
        head = fh.read(20)
        if len(head) < 20 or head[:4] != b"glTF":
            raise ValueError("not a binary glTF (.glb) file")
        (json_len,) = struct.unpack("<I", head[12:16])
        doc = json.loads(fh.read(json_len))
    accessors = doc.get("accessors", [])
    tris = verts = prims = 0
    for mesh in doc.get("meshes", []):
        for prim in mesh.get("primitives", []):
            prims += 1
            n_pos = accessors[prim["attributes"]["POSITION"]]["count"]
            verts += n_pos
            mode = prim.get("mode", 4)
            n_idx = accessors[prim["indices"]]["count"] if "indices" in prim else n_pos
            if mode == 4:
                tris += n_idx // 3
            elif mode in (5, 6):  # strip / fan
                tris += max(n_idx - 2, 0)
    skins = doc.get("skins", [])
    nodes, meshes = doc.get("nodes", []), doc.get("meshes", [])
    roots = [n for s in doc.get("scenes", []) for n in s.get("nodes", [])] or list(range(len(nodes)))
    seen, todo, skinned = set(), list(roots), 0
    while todo:  # nodes reachable from a scene: what a viewer or engine actually shows
        i = todo.pop()
        if i in seen or not 0 <= i < len(nodes):
            continue
        seen.add(i)
        node = nodes[i]
        todo += node.get("children", [])
        mesh, skin = node.get("mesh"), node.get("skin")
        if isinstance(mesh, int) and isinstance(skin, int) and 0 <= skin < len(skins) and 0 <= mesh < len(meshes) and any(
                {"JOINTS_0", "WEIGHTS_0"} <= set(p.get("attributes", {}))
                for p in meshes[mesh].get("primitives", [])):
            skinned += 1
    clips = [a.get("name") or f"clip {i + 1}" for i, a in enumerate(doc.get("animations", []))]
    return {"path": path, "triangles": tris, "vertices": verts, "meshes": len(doc.get("meshes", [])),
            "primitives": prims, "textures": len(doc.get("images", [])), "skins": len(skins),
            "joints": len({j for s in skins for j in s.get("joints", [])}), "skinned_mesh_nodes": skinned,
            "clips": clips,
            "bytes": os.path.getsize(path)}


def main(argv: list[str]) -> int:
    if not argv:
        print((__doc__ or "").strip(), file=sys.stderr)
        return 2
    rc = 0
    for p in argv:
        try:
            print(json.dumps(stats(p)))
        except (OSError, ValueError, KeyError, IndexError) as exc:
            print(json.dumps({"path": p, "error": str(exc)}))
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
