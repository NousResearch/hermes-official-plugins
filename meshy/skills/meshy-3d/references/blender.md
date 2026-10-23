# Blender handoff: optimized copy for game testing

Optional, local, no additional Meshy credits. After a Meshy download, a Blender 4.2+ install can
make an **optimized copy for game testing**: optional triangle budget, packed textures, a NEW
`.blend` and an engine export (GLB/FBX), each reopened in a fresh Blender process and measured.
It never opens, saves over or touches the user's live Blender scene or the downloaded source.

Intake and decimation rules adapted from community review by @123mikeyd (PR #115).

## 1. Settings and consent

Two plugin settings (Settings > Plugins > Meshy, stored in `plugins.entries.meshy.settings`):

| Setting | Values | Meaning |
|---|---|---|
| `blender_handoff` | `ask` (default), `always`, `never` | Offer the copy after a download |
| `blender_path` | path or empty | Blender executable; empty = auto-detect |

Read them with the terminal tool:

```
hermes config get plugins.entries.meshy.settings.blender_handoff   # "Config key not set" (exit 1) = ask
hermes config get plugins.entries.meshy.settings.blender_path
```

- `never`: do not offer. `always`: offer the copy after every download without the first-time
  question (still ask for the budget and format; never decimate without an approved budget).
- `ask` and Blender found: after a successful download, offer ONCE per session:
  "Blender is installed: want an optimized copy for game testing (packed textures, optional
  triangle budget) plus a .blend? Runs locally, no additional Meshy credits. Should I always
  offer this, or never?" If the user answers always/never, persist it:
  `hermes config set plugins.entries.meshy.settings.blender_handoff always` (or `never`).
  Settings > Plugins > Meshy shows the same value.

## 2. Find Blender

Stop at the first hit, then probe it (below). Use the terminal tool.

1. `blender_path` setting, if non-empty.
2. `command -v blender` (Windows: `where blender`).
3. macOS: `/Applications/Blender.app/Contents/MacOS/Blender`, `~/Applications/Blender.app/Contents/MacOS/Blender`.
4. Windows: newest `C:\Program Files\Blender Foundation\Blender *\blender.exe`
   (PowerShell: `Get-ChildItem 'C:\Program Files\Blender Foundation' -Recurse -Filter blender.exe`); Steam:
   `C:\Program Files (x86)\Steam\steamapps\common\Blender\blender.exe`.
5. Linux: `/snap/bin/blender`; Flatpak `flatpak run org.blender.Blender` (when `flatpak info
   org.blender.Blender` succeeds; it can only read paths the sandbox allows, home by default);
   `~/Applications/blender*/blender`, `/opt/blender*/blender`.

Nothing found: say Blender was not found, keep the download as is, and mention `blender_path`.
Do not install Blender unless the user asks.

Probe (the script lives at `<plugin dir>/skills/meshy-3d/scripts/blender_handoff.py`; the
plugin dir is `$HERMES_HOME/plugins/meshy`, usually `~/.hermes/plugins/meshy`):

```
"<blender>" --background --factory-startup --python "<script>" -- --probe
```

It prints `HANDOFF_PROBE {"ok": true, "blender": "4.5.14 LTS", ...}`. `ok: false` = older than 4.2:
tell the user the version and stop.

## 3. Inspect first, then agree the job

- Inventory before modifying: run once with `--tris 0 --export none` into a scratch out-dir, or
  read the source block of the real run. Report meshes, triangles, materials, images (+ size),
  armatures, actions (frame ranges), shape keys, scene FPS.
- Keep the original download and a versioned working copy; every run writes
  `<stem>_optimized.*` next to (never over) the source. Do not remesh, rerig, reset transforms or
  touch a working skin/rest pose just because the import looks unfamiliar.
- Budget is per asset and needs the user's number: use the table in SKILL.md "Pick the detail
  level" (characters ~15k, weapons ~3k, props ~1.5k on PC, about half on mobile; hero close-ups up
  to the source count). Keep the high-detail master.
- Many on screen (MMO, crowds, mobile): offer `--lods 0.5,0.25,0.1` (distance copies) and
  `--max-texture 2048` (1024 for props and mobile); textures usually run out before triangles.
- Format by engine: Godot / Blender / web: GLB. Unity / Unreal: FBX (or both).
- Wording: decimation reduces triangles; it is not retopology. If the user wants clean topology,
  Meshy remesh (5 credits) changes polygon count/topology; check the result, it is not
  guaranteed artist-clean retopology. Never promise remesh gives "clean quads" or a
  "retopologized base". Animation or deformation work belongs to
  [blender-animation-handoff](blender-animation-handoff.md).

## 4. Run

```
"<blender>" --background --factory-startup --python "<script>" -- \
  --input /abs/model.glb --out-dir /abs/dir [--tris N] [--export glb|fbx|both|none] [--name stem] \\
  [--lods 0.5,0.25,0.1] [--max-texture 2048]
```

| Flag | Effect |
|---|---|
| `--tris N` | Decimate (collapse) all meshes proportionally to at most N; 0/omitted = none; at or below source = none |
| `--export` | `glb` (default), `fbx`, `both`, `none` (.blend only) |
| `--lods F,F,..` | Also export `<stem>_lod1..N` at those fractions of the result's triangles (largest first); same skinned / shape-key refusals |
| `--max-texture N` | Downscale any texture whose longer side exceeds N px (aspect kept); reported under `textures_downscaled` |
| `--name` | Output stem (default input stem); `_optimized` is appended |
| `--force` | Replace existing OUTPUT files (the source is always refused) |
| `--allow-skinned` | Decimate meshes bound to an armature (weights can tear; check poses after) |
| `--allow-shape-keys` | Decimate meshes with shape keys by deleting the keys (reported as lost) |
| `--allow-loss` | Exit 0 even when rigs/clips/shape keys were lost (still listed) |
| `--render IN --png OUT` | Workbench PNG preview of a .glb/.fbx/.blend for a visual check |

A 1.75M-triangle textured GLB takes about 30 s plus verification. FBX is written -Z forward,
Y up, `FBX_SCALE_UNITS`, textures embedded; GLB stays metres, +Y up.

## 5. Read `HANDOFF_RESULT`

One JSON line; exit 0 = ok. Report real numbers from it, never the requested budget.

- `source` / `result`: triangles, vertices, materials, images (`image_list` sizes, `packed`),
  armatures (bones), actions (frames, fake_user), shape_keys, scene_fps, bytes.
- `decimation`: `applied`, `before`, `after`, `budget`, or `reason` (no budget / within budget).
- `outputs`: path + bytes for blend/glb/fbx.
- `verify.<kind>`: the same stats measured after reopening in a FRESH Blender process; `pass`
  needs triangles > 0 and every image to have pixel data after reopen.
- `preserved.<kind>`: armatures / actions / shape-key meshes kept (`"1/1"`); `lost` lists anything
  dropped; non-empty `lost` exits 4 unless `--allow-loss`.
- `warnings`: e.g. FBX keeps 2 of 3 images (glTF channel-packed metallic/roughness does not map to
  FBX; re-link in the engine), or a GLB reimport triangle count that differs slightly (split normals/UV seams).

Known live results (Blender 4.5.14): FBX drops shape-key (morph) animation, so a model with
morphs should go out as GLB. Exit 2 = refused or failed: the error says why and the
source is untouched. Exit 2 can follow a PARTIAL publication (a later output could not be moved into
place, or appeared meanwhile from an overlapping run): trust the error's "already saved: [...]" list,
not "nothing was written". Without `--force` an output is never replaced, even one created during the run.
Display-only helpers the glTF importer adds (the bone-shape Icosphere in `glTF_not_exported`) are not
counted, decimated or exported.

## 6. Check and report

- Render source and copy with `--render` and look at both (silhouette, thin parts, texture seams).
  If skinned, check deformation in a few poses before calling it usable.
- Report: path of each output with size, source -> output triangles, materials, image count and
  resolution, packed state after reopen, preserved/lost, warnings, Blender version.
- Call it an "optimized copy for game testing". Never "game-ready", "ready for Godot/Unity" or
  "drop it in": nobody has tested it in the engine yet; say so and suggest importing it there.
- Report states separately: downloaded / reopened-validated / visually reviewed / artist-approved /
  tested in the target engine. None implies the next; this script reaches reopened-validated.

## Failure handling

| Case | Do |
|---|---|
| Blender not found | Say so; keep the download; mention `blender_path` |
| Probe `ok: false` | Name the version; needs 4.2+ |
| `refusing to overwrite the source` / `output exists` | Pick another `--name`/`--out-dir`; `--force` only for old outputs the user agrees to replace |
| `refusing to decimate skinned mesh` / `shape keys` | Explain the risk; offer no decimation, Meshy remesh before rigging, or the allow flag if the user accepts it |
| Import error / no meshes | Report the error line; the source is untouched |
| exit 4 (`lost`) | List what was lost per output; suggest the format that kept it |
