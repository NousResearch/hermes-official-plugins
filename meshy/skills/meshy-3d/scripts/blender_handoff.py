"""Bridge-free Blender handoff for a downloaded Meshy model. Runs INSIDE Blender, never in a live scene:

    blender --background --factory-startup --python blender_handoff.py -- \
        --input model.glb --out-dir DIR [--tris N] [--export glb|fbx|both] [--name STEM] [--force]
    blender --background --factory-startup --python blender_handoff.py -- --probe
    blender --background --factory-startup --python blender_handoff.py -- --render IN.glb|IN.blend --png OUT.png

The handoff starts from Blender's factory-startup empty scene (it never opens or saves a user's
.blend), imports the GLB, optionally decimates every mesh proportionally to at most N triangles
(local Decimate/collapse: fewer triangles, NOT quad retopology, no additional Meshy credits), packs the
textures, saves a NEW <stem>_optimized.blend and exports <stem>_optimized.glb and/or .fbx. It then
re-opens the .blend and re-imports each export in FRESH Blender processes and prints one line:

    HANDOFF_RESULT {"ok": true, "source": {...}, "outputs": {...}, "verify": {...}}

The source file is never modified or overwritten. Existing outputs are refused unless --force.
Pure stdlib + bpy (Blender 4.2+).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile

import bpy

SKIP_IMAGES = {"Render Result", "Viewer Node"}


class HandoffRefused(RuntimeError):
    """A safety rule declined the job (the source is untouched)."""


def emit(tag: str, payload: dict) -> None:
    print(f"{tag} {json.dumps(payload, separators=(',', ':'))}", flush=True)


def fail(msg: str, code: int = 2, tag: str = "HANDOFF_RESULT") -> None:
    emit(tag, {"ok": False, "error": msg, "blender": bpy.app.version_string})
    sys.stdout.flush()
    os._exit(code)  # skip Blender's own exit noise; the JSON line is the contract


def parse_args() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    p = argparse.ArgumentParser(prog="blender_handoff.py")
    p.add_argument("--probe", action="store_true", help="print the Blender version and exit")
    p.add_argument("--input", help="source .glb/.gltf (never modified)")
    p.add_argument("--out-dir", help="directory for the new .blend and exports")
    p.add_argument("--tris", type=int, default=0, help="user-approved triangle budget (0 = no decimation)")
    p.add_argument("--export", choices=["glb", "fbx", "both", "none"], default="glb")
    p.add_argument("--lods", default="", help="also export lower-detail copies, as fractions of the result's "
                   "triangles (e.g. 0.5,0.25,0.1 -> <stem>_lod1..3.<ext>); same skinned/shape-key rules")
    p.add_argument("--max-texture", type=int, default=0,
                   help="downscale textures whose longer side exceeds N pixels (e.g. 2048); 0 = keep")
    p.add_argument("--name", help="output stem (default: input stem)")
    p.add_argument("--force", action="store_true", help="overwrite existing OUTPUT files (never the source)")
    p.add_argument("--allow-skinned", action="store_true",
                   help="decimate meshes bound to an armature (risky: weights/deformation can break)")
    p.add_argument("--allow-shape-keys", action="store_true",
                   help="decimate meshes with shape keys by DELETING the keys first (they are reported as lost)")
    p.add_argument("--allow-loss", action="store_true",
                   help="exit 0 even when rigs, clips or shape keys did not survive (still listed under lost)")
    p.add_argument("--no-verify", action="store_true", help="skip the fresh-process verification")
    p.add_argument("--render", help="render a preview PNG of a .glb/.gltf/.fbx/.blend")
    p.add_argument("--png", help="output PNG for --render")
    p.add_argument("--verify-file", help=argparse.SUPPRESS)  # internal: fresh-process check
    return p.parse_args(argv)


# ---------------------------------------------------------------- scene helpers

def reset_scene() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)


def import_any(path: str) -> None:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext == ".blend":
        bpy.ops.wm.open_mainfile(filepath=path)
    else:
        raise RuntimeError(f"unsupported input type {ext!r} (use .glb, .gltf, .fbx or .blend)")


def display_helpers() -> set:
    """Importer-made bone-display widgets (not asset geometry): the glTF importer puts an Icosphere in a
    hidden 'glTF_not_exported' collection and uses it as the bones' custom shape. Exported unfiltered it
    lands in an FBX as a visible ~2 m sphere over the character."""
    out = {o for o in bpy.context.scene.objects
           if any(c.name.startswith("glTF_not_exported") for c in o.users_collection)}
    for arm in bpy.context.scene.objects:
        if arm.type == "ARMATURE" and arm.pose:
            out.update(pb.custom_shape for pb in arm.pose.bones if pb.custom_shape)
    return out


def mesh_objects() -> list:
    helpers = display_helpers()
    return [o for o in bpy.context.scene.objects if o.type == "MESH" and o not in helpers]


def select_asset_objects() -> None:
    """Select everything but display helpers, so exports carry only the asset (meshes, rig, empties)."""
    helpers = display_helpers()
    for o in bpy.context.view_layer.objects:
        o.select_set(o not in helpers)


def tri_count(obj, evaluated: bool = True) -> int:
    if evaluated:
        dg = bpy.context.evaluated_depsgraph_get()
        eo = obj.evaluated_get(dg)
        me = eo.to_mesh()
        try:
            me.calc_loop_triangles()
            return len(me.loop_triangles)
        finally:
            eo.to_mesh_clear()
    return sum(max(len(p.vertices) - 2, 0) for p in obj.data.polygons)


def is_skinned(obj) -> bool:
    return any(m.type == "ARMATURE" and m.object for m in obj.modifiers) or (
        obj.parent is not None and obj.parent.type == "ARMATURE" and obj.parent_type in ("ARMATURE", "OBJECT")
        and len(obj.vertex_groups) > 0)


def stats(path: str | None = None) -> dict:
    meshes = mesh_objects()
    scene = bpy.context.scene
    arms = [o for o in scene.objects if o.type == "ARMATURE"]
    tris = sum(tri_count(o) for o in meshes)
    verts = sum(len(o.data.vertices) for o in meshes)
    mats = sorted({s.material.name for o in meshes for s in o.material_slots if s.material})
    images = []
    for img in bpy.data.images:
        if img.name in SKIP_IMAGES or img.users == 0:
            continue
        w, h = img.size[:]
        images.append({"name": img.name, "size": f"{w}x{h}", "packed": bool(img.packed_file),
                       "has_data": bool(w and h)})
    actions = [{"name": a.name, "frames": [round(a.frame_range[0], 3), round(a.frame_range[1], 3)],
                "fake_user": a.use_fake_user} for a in bpy.data.actions]
    fps = scene.render.fps / (scene.render.fps_base or 1.0)
    out = {"triangles": tris, "vertices": verts, "mesh_objects": len(meshes),
           "mesh_names": sorted(o.name for o in meshes),
           "materials": len(mats), "material_names": mats,
           "images": len(images), "image_list": images,
           "armatures": [{"name": a.name, "bones": len(a.data.bones)} for a in arms],
           "skinned_meshes": [o.name for o in meshes if is_skinned(o)],
           "shape_keys": {o.name: [k.name for k in o.data.shape_keys.key_blocks[1:]]
                          for o in meshes if o.data.shape_keys and len(o.data.shape_keys.key_blocks) > 1},
           "actions": actions, "scene_fps": round(fps, 3)}
    if path:
        out["path"] = path
        out["bytes"] = os.path.getsize(path)
    return out


# ---------------------------------------------------------------- decimation

KEEP_LOOSE = "hermes_source_loose"


def mark_source_loose(me) -> None:
    """Tag vertices that belong to no face BEFORE decimating (a glTF LINES/POINTS primitive): they are
    source geometry, not decimation debris, and delete_loose keeps them."""
    import numpy as np  # bundled with Blender
    vi = np.empty(len(me.loops), dtype=np.int32)
    me.loops.foreach_get("vertex_index", vi)
    loose = np.ones(len(me.vertices), dtype=np.int32)
    loose[vi] = 0
    if loose.any():
        me.attributes.new(KEEP_LOOSE, "INT", "POINT").data.foreach_set("value", loose)


def delete_loose(me) -> None:
    """Drop the vertices and edges Collapse left without a face. glTF meshes arrive split along every UV/normal
    seam (one glTF vertex per attribute set) and Collapse cannot merge across those splits: it removes the
    collapsed faces but keeps their vertices and edges. A 1M -> 15k triangle run kept ~1M of them in the saved
    .blend (20 MB, a misleading vertex count); the exporters already skip them."""
    import bmesh
    bm = bmesh.new()
    try:
        bm.from_mesh(me)
        layer = bm.verts.layers.int.get(KEEP_LOOSE)
        keep = (lambda v: v[layer] == 1) if layer is not None else (lambda v: False)
        bmesh.ops.delete(bm, geom=[e for e in bm.edges if not e.link_faces and not all(keep(v) for v in e.verts)],
                         context="EDGES")
        bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces and not keep(v)], context="VERTS")
        bm.to_mesh(me)
    finally:
        bm.free()
    if KEEP_LOOSE in me.attributes:
        me.attributes.remove(me.attributes[KEEP_LOOSE])
    me.update()


def decimate_to(budget: int, allow_skinned: bool = False, allow_shape_keys: bool = False) -> dict:
    meshes = [o for o in mesh_objects() if tri_count(o) > 0]
    before = sum(tri_count(o) for o in meshes)
    if budget <= 0 or before <= budget:
        return {"applied": False, "reason": "no budget" if budget <= 0 else "source already within budget",
                "budget": budget, "before": before, "after": before}
    skinned = [o.name for o in meshes if is_skinned(o)]
    if skinned and not allow_skinned:
        raise HandoffRefused(
            f"refusing to decimate skinned mesh(es) {skinned}: collapsing a mesh bound to an armature can "
            "tear weights and break deformation. Use Meshy remesh before rigging, or rerun with "
            "--allow-skinned after the user accepts the risk and plan to check deformation in a few poses")
    keyed = [o for o in meshes if o.data.shape_keys]
    if keyed and not allow_shape_keys:
        raise HandoffRefused(
            f"refusing to decimate mesh(es) with shape keys {[o.name for o in keyed]}: Blender cannot apply a "
            "Decimate modifier to a mesh with shape keys, so the keys (facial/morph animation) would have to "
            "be deleted. Keep the source triangle count, or rerun with --allow-shape-keys if the user accepts "
            "losing them")
    for o in keyed:
        o.shape_key_clear()
    for o in meshes:
        mark_source_loose(o.data)
    ratio = budget / before
    mods = []
    for o in meshes:
        m = o.modifiers.new("HermesDecimate", "DECIMATE")
        m.decimate_type = "COLLAPSE"
        m.use_collapse_triangulate = True
        m.ratio = ratio
        mods.append((o, m))
    # Collapse does not land exactly on the ratio: nudge it down until the total is inside the budget.
    after = before
    for _ in range(6):
        after = sum(tri_count(o) for o, _m in mods)
        if after <= budget:
            break
        ratio *= (budget / after) * 0.995
        for _o, m in mods:
            m.ratio = max(ratio, 1e-5)
    if after > budget:
        raise RuntimeError(f"decimation could not reach the budget ({after} > {budget})")
    for o, m in mods:
        bpy.context.view_layer.objects.active = o
        with bpy.context.temp_override(object=o, active_object=o):
            bpy.ops.object.modifier_apply(modifier=m.name)
    for o in meshes:
        delete_loose(o.data)
    after = sum(tri_count(o) for o in meshes)
    return {"applied": True, "method": "Decimate modifier (collapse), applied; not quad retopology",
            "budget": budget, "before": before, "after": after, "ratio": round(ratio, 6)}


def downscale_textures(max_px: int) -> list:
    """Shrink every image whose longer side exceeds max_px, keeping the aspect ratio."""
    changed = []
    if max_px <= 0:
        return changed
    for img in bpy.data.images:
        w, h = img.size[:2]
        if img.has_data and max(w, h) > max_px:
            k = max_px / max(w, h)
            img.scale(max(1, round(w * k)), max(1, round(h * k)))
            if img.packed_file:  # GLB textures arrive packed: re-pack the new pixels or the .blend keeps the original
                fmt = img.file_format  # keep JPEG as JPEG: a plain pack() re-encodes as PNG, several times larger
                tmp = os.path.join(tempfile.mkdtemp(prefix="meshy-tex-"), f"t.{'jpg' if fmt == 'JPEG' else 'png'}")
                img.filepath_raw, img.file_format = tmp, fmt
                img.save()
                img.unpack(method="REMOVE")
                img.filepath_raw = tmp
                img.reload()
                img.pack()
                shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)
            changed.append({"name": img.name, "from": f"{w}x{h}", "to": f"{img.size[0]}x{img.size[1]}"})
    return changed


def parse_lods(spec: str) -> list:
    try:
        fr = [float(x) for x in spec.split(",") if x.strip()]
    except ValueError:
        fail(f"--lods must be comma-separated fractions, got {spec!r}")
    if any(not 0 < f < 1 for f in fr) or fr != sorted(fr, reverse=True):
        fail("--lods fractions must be between 0 and 1, largest first (e.g. 0.5,0.25,0.1)")
    return fr


# ---------------------------------------------------------------- exports

def export_glb(path: str) -> None:
    select_asset_objects()
    bpy.ops.export_scene.gltf(filepath=path, export_format="GLB", use_selection=True)


def export_fbx(path: str) -> None:
    # Textures copied INTO the .fbx; -Z forward / Y up; FBX_SCALE_UNITS bakes Blender's metres so
    # Unity/Unreal import at scale 1 (check "Convert Units" in Unity if it comes in 100x).
    select_asset_objects()  # never the importer's bone-display helper mesh
    bpy.ops.export_scene.fbx(filepath=path, use_selection=True, path_mode="COPY", embed_textures=True,
                             axis_forward="-Z", axis_up="Y", apply_scale_options="FBX_SCALE_UNITS",
                             object_types={"MESH", "ARMATURE", "EMPTY"}, mesh_smooth_type="FACE",
                             add_leaf_bones=False, bake_anim=True)


def _action_match(name: str, names: list) -> str | None:
    # FBX round trips prefix the armature name ("Armature|Armature|walk"): match on the suffix.
    return next((n for n in names if n == name or n.endswith("|" + name) or name.endswith("|" + n)), None)


def compare_preserved(src: dict, got: dict) -> tuple[dict, list]:
    """Rigs, clips and shape keys of the source vs a fresh-process reopen of one output."""
    lost, notes = [], []
    got_arms = {a["name"]: a["bones"] for a in got.get("armatures", [])}
    for arm in src["armatures"]:
        bones = got_arms.get(arm["name"])
        if bones is None and len(got_arms) == len(src["armatures"]) == 1:
            bones = next(iter(got_arms.values()))
        if bones is None:
            lost.append(f"armature {arm['name']}")
        elif bones < arm["bones"]:
            lost.append(f"armature {arm['name']}: {bones} of {arm['bones']} bones")
    got_actions = {a["name"]: a["frames"] for a in got.get("actions", [])}
    for act in src["actions"]:
        hit = _action_match(act["name"], list(got_actions))
        if hit is None:
            lost.append(f"action {act['name']}")
        elif abs((got_actions[hit][1] - got_actions[hit][0]) - (act["frames"][1] - act["frames"][0])) > 1.01:
            notes.append(f"action {act['name']}: frames {act['frames']} -> {got_actions[hit]}")
    got_keys = got.get("shape_keys", {})
    for mesh, keys in src["shape_keys"].items():
        have = got_keys.get(mesh)
        if have is None and len(got_keys) == len(src["shape_keys"]) == 1:
            have = next(iter(got_keys.values()))
        missing = [k for k in keys if k not in (have or [])]
        if missing:
            lost.append(f"shape keys on {mesh}: {missing}")
    if got.get("scene_fps") and got["scene_fps"] != src["scene_fps"]:
        notes.append(f"scene FPS {src['scene_fps']} -> {got['scene_fps']}")
    block = {"armatures": f"{len(got_arms)}/{len(src['armatures'])}",
             "actions": f"{sum(1 for a in src['actions'] if _action_match(a['name'], list(got_actions)))}"
                        f"/{len(src['actions'])}",
             "shape_key_meshes": f"{len(got_keys)}/{len(src['shape_keys'])}", "lost": lost, "notes": notes}
    return block, lost


def verify_fresh(path: str) -> dict:
    """Open/import ``path`` in a NEW Blender process and return its stats."""
    cmd = [bpy.app.binary_path, "--background", "--factory-startup", "--python", os.path.abspath(__file__),
           "--", "--verify-file", path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900, check=False)
    except subprocess.TimeoutExpired:
        return {"pass": False, "error": "verification timed out"}
    for line in r.stdout.splitlines():
        if line.startswith("HANDOFF_VERIFY "):
            data = json.loads(line[len("HANDOFF_VERIFY "):])
            missing = [i["name"] for i in data.get("image_list", []) if not i.get("has_data")]
            data["pass"] = bool(data.pop("ok", False)) and data.get("triangles", 0) > 0 and not missing
            if missing:
                data["error"] = f"images without pixel data after reopen: {missing}"
            return data
    return {"pass": False, "error": f"no verify output (exit {r.returncode}): {r.stderr[-400:]}"}


def publish(tmp: str, final: str, force: bool) -> None:
    """Move a staged file into place. --force replaces the directory entry; without it publication is
    no-clobber (hard link, then drop the staged name), so an output that appeared after the preflight
    check (an overlapping handoff) raises FileExistsError instead of being replaced."""
    if force:
        os.replace(tmp, final)
        return
    try:
        os.link(tmp, final)
    except FileExistsError:
        raise
    except OSError:  # filesystem without hard links: exclusive create + copy, still never clobbers
        try:
            with open(tmp, "rb") as s, open(final, "xb") as d:
                shutil.copyfileobj(s, d)
        except FileExistsError:
            raise
        except OSError:
            if os.path.exists(final):
                os.unlink(final)
            raise
    os.unlink(tmp)


# ---------------------------------------------------------------- render preview

def render_preview(src: str, png: str) -> None:
    if os.path.splitext(src)[1].lower() == ".blend":
        bpy.ops.wm.open_mainfile(filepath=src)
    else:
        reset_scene()
        import_any(src)
    scene = bpy.context.scene
    meshes = mesh_objects()
    if not meshes:
        raise RuntimeError("nothing to render: no mesh objects")
    import mathutils
    pts = [o.matrix_world @ mathutils.Vector(c) for o in meshes for c in o.bound_box]
    lo = mathutils.Vector([min(p[i] for p in pts) for i in range(3)])
    hi = mathutils.Vector([max(p[i] for p in pts) for i in range(3)])
    center, radius = (lo + hi) / 2, max((hi - lo).length / 2, 1e-3)
    cam_data = bpy.data.cameras.new("PreviewCam")
    cam = bpy.data.objects.new("PreviewCam", cam_data)
    scene.collection.objects.link(cam)
    d = radius / math.tan(cam_data.angle / 2) * 1.15
    direction = mathutils.Vector((0.6, -1.0, 0.45)).normalized()
    cam.location = center + direction * d
    cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
    cam_data.clip_end = d * 10
    scene.camera = cam
    scene.render.engine = "BLENDER_WORKBENCH"
    shading = scene.display.shading
    shading.light, shading.color_type = "STUDIO", "TEXTURE"
    shading.show_cavity = False
    scene.render.resolution_x = scene.render.resolution_y = 768
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = "PNG"
    # Render to a private file beside the target, then rename it into place: never write through an
    # existing PNG path (it could be a hard link to the source).
    os.makedirs(os.path.dirname(png), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".render-", suffix=".png", dir=os.path.dirname(png))
    os.close(fd)
    try:
        scene.render.filepath = tmp
        bpy.ops.render.render(write_still=True)
        os.replace(tmp, png)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    emit("HANDOFF_RENDER", {"ok": True, "png": png, "triangles": sum(tri_count(o) for o in meshes)})


# ---------------------------------------------------------------- main

def main() -> None:
    a = parse_args()
    if a.probe:
        emit("HANDOFF_PROBE", {"ok": bpy.app.version >= (4, 2, 0), "blender": bpy.app.version_string,
                               "binary": bpy.app.binary_path, "min_supported": "4.2"})
        return
    if a.verify_file:
        try:
            if os.path.splitext(a.verify_file)[1].lower() == ".blend":
                bpy.ops.wm.open_mainfile(filepath=a.verify_file)
            else:
                reset_scene()
                import_any(a.verify_file)
            emit("HANDOFF_VERIFY", {"ok": True, **stats(a.verify_file)})
        except Exception as exc:  # noqa: BLE001 - report every failure as JSON
            fail(f"{type(exc).__name__}: {exc}", tag="HANDOFF_VERIFY")
        return
    if a.render:
        if not a.png:
            fail("--render needs --png", tag="HANDOFF_RENDER")
        rsrc, rpng = os.path.realpath(a.render), os.path.realpath(a.png)
        if rpng == rsrc or (os.path.exists(rpng) and os.path.exists(rsrc) and os.path.samefile(rpng, rsrc)):
            fail(f"refusing to overwrite the source file {rsrc} with the preview; pick another --png",
                 tag="HANDOFF_RENDER")
        try:
            render_preview(os.path.abspath(a.render), os.path.abspath(a.png))
        except Exception as exc:  # noqa: BLE001
            fail(f"{type(exc).__name__}: {exc}", tag="HANDOFF_RENDER")
        return

    if bpy.app.version < (4, 2, 0):
        fail(f"Blender {bpy.app.version_string} is too old; the handoff needs 4.2 or newer")
    if not a.input or not a.out_dir:
        fail("--input and --out-dir are required")
    src = os.path.realpath(a.input)
    if not os.path.isfile(src):
        fail(f"input not found: {a.input}")
    if os.path.splitext(src)[1].lower() not in (".glb", ".gltf"):
        fail("input must be a .glb or .gltf downloaded from Meshy")
    if a.tris < 0:
        fail("--tris must be >= 0")
    lod_fractions = parse_lods(a.lods) if a.lods else []
    if lod_fractions and a.export == "none":
        fail("--lods needs --export glb, fbx or both")
    out_dir = os.path.realpath(a.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    stem = a.name or os.path.splitext(os.path.basename(src))[0]
    if os.sep in stem or stem in ("", ".", ".."):
        fail(f"invalid --name {stem!r}")
    stem = stem if stem.endswith("_optimized") else f"{stem}_optimized"
    kinds = {"glb": ["glb"], "fbx": ["fbx"], "both": ["glb", "fbx"], "none": []}[a.export]
    outputs = {"blend": os.path.join(out_dir, f"{stem}.blend")}
    outputs.update({k: os.path.join(out_dir, f"{stem}.{k}") for k in kinds})
    for i, _f in enumerate(lod_fractions, 1):
        outputs.update({f"lod{i}_{k}": os.path.join(out_dir, f"{stem}_lod{i}.{k}") for k in kinds})
    for path in outputs.values():
        same = os.path.realpath(path) == src or (os.path.exists(path) and os.path.samefile(path, src))
        if same:  # samefile also catches a hard link to the source
            fail(f"refusing to overwrite the source file {src}; pick another --name or --out-dir")
        if os.path.exists(path) and not a.force:
            fail(f"output exists: {path} (pass --force to replace it, or pick another --name)")

    # Everything is written into a private staging folder and moved into place only once every step
    # (LODs included) succeeded, so a refusal or error publishes nothing, and --force replaces directory
    # entries instead of writing through an existing file.
    os.makedirs(out_dir, exist_ok=True)
    stage = tempfile.mkdtemp(prefix=".handoff-", dir=out_dir)
    final = outputs
    outputs = {k: os.path.join(stage, os.path.basename(p)) for k, p in final.items()}
    try:
        reset_scene()  # empty factory scene: no user .blend is ever opened or saved over
        import_any(src)
        source = stats(src)
        if not source["mesh_objects"]:
            raise HandoffRefused("the input has no mesh objects")
        dec = decimate_to(a.tris, a.allow_skinned, a.allow_shape_keys)
        textures = downscale_textures(a.max_texture)
        for act in bpy.data.actions:  # keep imported clips in the .blend even while unassigned
            act.use_fake_user = True
        bpy.ops.file.pack_all()
        bpy.ops.wm.save_as_mainfile(filepath=outputs["blend"], check_existing=False, compress=True)
        if "glb" in outputs:
            export_glb(outputs["glb"])
        if "fbx" in outputs:
            export_fbx(outputs["fbx"])
        result_scene = stats()
        lods = []
        for i, frac in enumerate(lod_fractions, 1):  # each LOD reduces the previous one: same result, cheaper
            lod = decimate_to(max(1, int(result_scene["triangles"] * frac)), a.allow_skinned, a.allow_shape_keys)
            for k in kinds:
                (export_glb if k == "glb" else export_fbx)(outputs[f"lod{i}_{k}"])
            lods.append({"lod": i, "fraction": frac, "triangles": lod["after"]})
        moved = []
        for k, p in outputs.items():
            try:
                publish(p, final[k], a.force)
            except FileExistsError:  # another handoff published this name after the preflight check
                shutil.rmtree(stage, ignore_errors=True)
                fail(f"output exists: {final[k]} (pass --force to replace it, or pick another --name); "
                     f"already saved: {moved or 'nothing'} (source untouched: {src})")
            except OSError as exc:
                shutil.rmtree(stage, ignore_errors=True)
                fail(f"could not move {final[k]} into place ({exc}); already saved: {moved or 'nothing'} "
                     f"(source untouched: {src})")
            moved.append(final[k])
    except SystemExit:
        raise
    except HandoffRefused as exc:
        shutil.rmtree(stage, ignore_errors=True)  # fail() exits at once: no finally runs after it
        fail(f"{exc} (source untouched: {src}; nothing saved)")
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(stage, ignore_errors=True)
        fail(f"{type(exc).__name__}: {exc} (source untouched: {src}; nothing saved)")
    shutil.rmtree(stage, ignore_errors=True)
    outputs = final

    outs = {k: {"path": p, "bytes": os.path.getsize(p)} for k, p in outputs.items()}
    verify = {}
    if not a.no_verify:
        verify = {k: verify_fresh(p) for k, p in outputs.items()}
    ok = all(v.get("pass") for v in verify.values()) if verify else True
    warnings = []
    for k, v in verify.items():
        if v.get("pass") and v.get("mesh_objects") != result_scene["mesh_objects"]:
            warnings.append(f"{k}: reopened with mesh objects {v.get('mesh_names')}, saved "
                            f"{result_scene['mesh_names']}")
        if not k.startswith("lod") and v.get("triangles") not in (None, result_scene["triangles"]):
            warnings.append(f"{k}: reopened with {v.get('triangles')} triangles, saved {result_scene['triangles']}")
        if v.get("images", 0) < result_scene["images"]:
            warnings.append(f"{k}: reopened with {v.get('images')} of {result_scene['images']} images "
                            "(FBX cannot carry glTF channel-packed metallic/roughness maps; re-link them in the engine)")
    preserved, lost = {}, []
    for k, v in verify.items():  # judged on the FRESH-process reopen, not the writing session
        if v.get("pass"):
            preserved[k], item_lost = compare_preserved(source, v)
            lost += [f"{k}: {x}" for x in item_lost]
    if lost and not a.allow_loss:
        ok = False
    emit("HANDOFF_RESULT", {
        "ok": ok, "blender": bpy.app.version_string, "source": source, "decimation": dec,
        "textures_downscaled": textures, "lods": lods,
        "result": {k: result_scene[k] for k in ("triangles", "vertices", "mesh_objects", "materials",
                                                 "images", "image_list", "armatures", "skinned_meshes",
                                                 "actions", "scene_fps")},
        "outputs": outs, "verify": verify, "preserved": preserved, "lost": lost, "warnings": warnings,
        "notes": [("optimized copy for game testing: decimation reduces triangles, it is not retopology; "
                   "Meshy remesh (5 credits) changes polygon count/topology but is not guaranteed artist-clean"),
                  "GLB is metres, +Y up; FBX exported -Z forward, Y up, FBX_SCALE_UNITS, textures embedded"],
    })
    sys.stdout.flush()
    os._exit(0 if ok else (4 if lost else 3))


if __name__ == "__main__":
    main()
