<!-- Adapted for Hermes Agent from the server instructions of meshy-mcp-server 0.6.4 (https://github.com/meshy-dev/meshy-mcp-server, src/instructions.ts, MIT, Copyright (c) 2026 Meshy AI). Hermes does not forward MCP server instructions to the model, so the skill carries them. Changes: title; Scenario C picks the format per engine and adds approval checkpoints; Scenario K adds detail-level defaults, face-count reporting and Hermes measurements. -->

# Meshy workflow guide (from the server)

Every create tool returns {task_id, status}, except meshy_creative_lab, which waits and returns the finished model. Wait for it with meshy_get_task_status (task_type is inferred when omitted), then meshy_download_model.
Earlier results: meshy_list_models lists finished 3D models (image-to-3d by default; task_types widens it); meshy_list_tasks lists tasks of any status (default: text-to-3d, image-to-3d, multi-image-to-3d, text-to-image, image-to-image; task_types widens it, so an empty default list does not mean there are no printing, rigging or Creative Lab tasks).

## Rule 1: Cost Confirmation
Before calling ANY tool that costs credits, ask which ai_model to use where there is a choice (prefer an explicit one), present the cost and wait for user confirmation.

| Tool | Credits |
|------|---------|
| meshy_text_to_3d (preview, mesh only) | 20 (meshy-7.1 / latest / meshy-6), 5 (meshy-6-lite, meshy-t2, meshy-t2.5) |
| meshy_image_to_3d / meshy_multi_image_to_3d | table below |
| meshy_text_to_3d_refine / meshy_retexture | 10 (15 with texture_resolution "8k") |
| meshy_remesh / meshy_rig (incl. walking + running) / meshy_uv_unwrap | 5 |
| meshy_animate | 3 per action |
| meshy_text_to_motion | 10 (prime) / 3 (swift) |
| meshy_repair_printability / meshy_process_multicolor / meshy_auto_split | 10 |
| meshy_convert / meshy_resize | 1 |
| meshy_creative_lab | 36; keycap 62; fidget-collapsible 6 |
| meshy_text_to_image / meshy_image_to_image | nano-banana 3, nano-banana-2 6, nano-banana-2-lite 3, nano-banana-2-1 4, nano-banana-pro 9; GPT Image models (gpt-image-2, gpt-image-2-5-flare, gpt-image-2-5-sunburst) 9 text / 12 image |
| analyze printability, balance, usage, animation library, status, list, download | free |

| image / multi-image ai_model | mesh only | + 2K/4K texture | + 8K texture |
|---|---|---|---|
| meshy-7.1 / latest / meshy-6 (and deprecated meshy-7) | 20 | 30 | 35 |
| meshy-6-lite | 5 | 15 | — (2K only) |
| meshy-t2 / meshy-t2.5 (single-image Smart Topology) | 5 | 15 | 20 |

Ultra geometry (geometry_resolution "2k"/"4k") adds 5 on meshy-7.1 / latest. Confirm the 8K and Ultra surcharges explicitly.
Prices match https://docs.meshy.ai/en/api/pricing as of 2026-10-12; point the user there if a number looks off.

## Rule 2: Never Re-create a Task You Are Unsure About
If a create call fails with a timeout, network error or 5xx, the task may still have been created and charged. Check meshy_list_tasks (task_type = that task's type) for it before calling the create tool again. A wait that times out leaves the task running: poll the same task_id again.

## Rule 3: Determine Output Format BEFORE Generating
target_formats is fixed at creation time (3mf only when listed), so ask about the intended use first.

## Rule 4: Identify the Use Case, Then Confirm with User
Determine which scenario below best matches, present a suggested plan to the user, and confirm before executing. When a request spans several scenarios (e.g. A then L), combine their steps and confirm the combined plan.

---

## Scenario A: 3D Printing (white model)
Trigger: user mentions print, slicer, figurine, miniature, statue, physical model — single-color.
Suggested flow:
1. Detect installed slicers (meshy_send_to_slicer, slicer_type:"auto") — present list, save for later
2. Generate an untextured model with target_formats:["obj"]: meshy_text_to_3d (preview), or meshy_image_to_3d with should_texture:false
3. Wait for completion
4. Optionally check printability (meshy_analyze_printability, free). If it reports errors, either repair with model_url = the generation task's OBJ URL (model_urls.obj) so the result stays OBJ — on that route pass printer_type and intended_print_longest_side_mm, since intended_print_height_mm only works with task / GLB input — or repair by the analyze task, then meshy_convert the repaired GLB to obj (1 credit). print_ready only fixes OBJ
5. Download the OBJ (the repair task's, if repaired) with print_ready=true and print_height_mm (local Z-up fix and scaling)
6. Present slicer list from step 1, ask user which to use, run its launch_command
If the model must be printed in pieces (too big for the bed, separate colors or materials), use Scenario L.

## Scenario B: 3D Printing (multicolor)
Trigger: user wants multicolor/color print.
Suggested flow:
1. Detect multicolor-capable slicers (meshy_send_to_slicer, is_multicolor:true) — present list, save for later
2. Generate a textured model — image-to-3d / multi-image-to-3d (textured by default), or text-to-3d preview + refine
3. meshy_process_multicolor with input_task_id of the textured task (confirm its options)
4. Wait, then download with format "3mf"
5. Present slicer list from step 1, ask user which to use, run its launch_command

## Scenario C: Game Engine (Unity/Unreal/Godot)
Trigger: user mentions game, Unity, Unreal, Godot, game-ready.
Format by engine (Hermes change: the server text picks FBX for every engine): Unity → FBX (GLB also works through glTFast); Unreal → FBX; Godot → GLB (glTF is Godot's native import); a Blender step in between → GLB; web engines → GLB.
Suggested flow:
1. Before any paid call, ask: which engine; triangle budget per asset; units and forward/up axis the project expects (Meshy exports are metres, Y-up; scale with meshy_resize or in the engine); whether it needs textures, and whether it needs a rig or animation.
2. Generate with target_formats for that engine (["fbx"] for Unity/Unreal, ["glb"] for Godot); Smart Topology by default (model_type "smart-topology", ai_model "meshy-t2.5", target_polycount = the budget, else per asset from "Pick the detail level" in SKILL.md: ~15000 characters, ~3000 weapons, ~1500 props; Scenario K), Standard for hero / close-up assets.
3. Checkpoint: show the preview geometry (thumbnail / viewer card) and get approval BEFORE paid texturing, remeshing or rigging.
4. Textures: image-to-3d / multi-image output is already textured (unless should_texture:false); a text-to-3d preview needs meshy_text_to_3d_refine
5. Inspect topology and face count. Smart Topology may already meet the budget; remesh only if needed and separately approved
6. Checkpoint: get the user's approval of the final model (and animation) BEFORE writing into an existing game project; then download that engine's format with save_to inside the project's asset folder (one download per format).
7. Say plainly that a downloaded model is not game-ready yet: no LODs, no collision mesh, no hand retopology, and pivot/scale may need fixing in the engine.

Engine notes:
- Unreal works in centimetres and Meshy exports metres: keep "Convert Scene Unit" on at FBX import (or scale ×100), else the model arrives 100× too small.
- Roblox: a MeshPart takes at most 20,000 triangles. Set `target_polycount` at or under that (Smart Topology) or remesh first; export FBX or OBJ.
- Locomotion clips: some library clips travel (root motion; a sprint moves about a metre per cycle), while the rig's free walk/run stay in place. When the game moves the character itself, use the in-place variants: `meshy_list_animations` with `search: "inplace"` lists them (`Casual_Walk_inplace`, `Lean_Forward_Sprint_inplace`, jumps, flips; about 90). If only a travelling clip fits, lock root motion on import (Unity: Root Transform Position (XZ) → Bake Into Pose; Unreal: Force Root Lock in the animation asset). Check by playing the clip and watching whether the character leaves its spot.
- Godot 4: drop the `.glb` into the project; clips become an AnimationPlayer.

## Scenario D: Character Animation
Trigger: user mentions rig, animate, walking, running, character animation.
Suggested flow:
1. Generate model with pose_mode:"t-pose"
2. Textures (rigging needs a textured humanoid): image-to-3d / multi-image output is already textured (unless should_texture:false); a text-to-3d preview needs meshy_text_to_3d_refine
3. Rig the model (meshy_rig, ≤300K faces; if over, remesh to reduce polycount first)
4. Download — rigging includes walking+running free, as separate files. For other actions use meshy_animate:
   - preset actions: look up action ids with meshy_list_animations (free) — never guess ids. Ask whether the game moves the character; if so pick the `_inplace` variants (`search: "inplace"`), since some library clips travel. action_ids puts up to 10 clips in one file (include the walk/run ids to bundle them too)
   - a described motion ("a backflip into a crouch"): meshy_text_to_motion, then meshy_animate with motion_task_id (biped rigs only)

## Scenario E: AR / Apple
Trigger: user mentions AR, USDZ, Vision Pro, Quick Look.
Suggested flow:
1. Generate model with target_formats:["usdz"]
2. Textures: image-to-3d / multi-image output is already textured (unless should_texture:false); a text-to-3d preview needs meshy_text_to_3d_refine
3. If the model has no USDZ yet, convert it (meshy_convert, target_formats:["usdz"])
4. Download USDZ

## Scenario F: Retexture
Trigger: user wants to change textures/style of existing model.
Suggested flow:
1. Ask the user for EXACTLY ONE style input (see meshy_retexture), then call it.

## Scenario H: Cheap Format Conversion / Resize
Trigger: user has an existing model (task or URL) and only needs another file format, or wants it scaled to real-world dimensions — no re-generation.
Suggested flow:
1. Format only → meshy_convert (supports 3mf; cheaper than remesh).
2. Real-world size → meshy_resize.
3. Wait, then meshy_download_model.

## Scenario I: UV Unwrap (before external texturing)
Trigger: user wants to texture a model in Blender / Substance Painter / Unreal, or needs a clean UV layout / "UV white model".
Suggested flow:
1. The source must be a GLB with ≤40,000 faces — if denser, meshy_remesh to a lower target_polycount first.
2. meshy_uv_unwrap, wait, then meshy_download_model (format "glb").

## Scenario J: Creative Lab Consumer Products
Trigger: user wants a stylized physical-product model from a photo — chibi / vinyl / brick figure, keychain, fridge magnet, keycap, lamp, pixel-art or collapsible fidget.
Suggested flow:
1. Confirm the product, its cost and options (see meshy_creative_lab), then call it once with a photo.
2. Download with the task_type it reports; omit format to save every part.

## Scenario K: Cleaner Topology / Part-Separated Geometry (cheaply)
Trigger: user wants an editable, part-separated, or lower-poly model from an image or text, or a game / mobile asset (see "Pick the detail level" in SKILL.md).
Suggested flow:
1. Confirm the model (meshy-t2.5 by default: newest, sharper detail, target_polycount 100–30,000, default 15,000; meshy-t2: 100–15,000, default 4,000), cost, face target, and whether textures are wanted.
2. Call meshy_image_to_3d or meshy_text_to_3d with model_type:"smart-topology", that ai_model, and target_polycount. Set should_texture false on the image route when only geometry is approved.
3. Text preview is untextured; confirm refine separately, without a refine ai_model override.
4. Report the actual triangle count: face_count from meshy_get_task_status when it is present; otherwise count it from the downloaded GLB: `python3 <plugin dir>/skills/meshy-3d/scripts/glb_stats.py <file.glb>` (stdlib, prints triangles/vertices; Desktop's card stats line also shows "N tris"). Meshy lands near, not on, the target (measured: 14,860 and 15,929 for a 15,000 target).
Hermes notes (measured 2026-10-20, Smart meshy-t2.5 @15k vs Standard latest + remesh @15k, same prompt / image):
- Textured character from an image: near parity at viewing distance (face, hands, shield, belt) for 15 vs 30 credits; Smart dropped a thin cloth tail and came back as 8 separate parts.
- Ornate prop from text (engraved runes, filigree guard): Standard kept crisp engraving and bevels; Smart smoothed the runes away and returned 11 parts with open edges.
- So: Smart for game assets seen at distance; Standard (optionally remeshed, 5 credits, or the Blender copy) when fine sculpted detail, close-ups, renders or printing matter. Smart is triangle-only: quad topology needs Standard + meshy_remesh with topology "quad".

## Scenario L: Print in Parts (Auto Split)
Trigger: model is too big for the print bed, or parts should be printed separately / in different colors and assembled.
Suggested flow:
1. Check the source qualifies (see meshy_auto_split).
2. Confirm the cost and how to cut: mode "auto", "by_parts" with a prompt naming 1–10 parts, or "by_color" (image-to-3d / multi-image-to-3d sources only). There is no bed-size parameter: resize to the print size first if parts must fit a bed, then check part sizes and re-split by_parts if one is still too big. Offer connectors so the pieces fit together, layout "on_plate" for print-ready placement, and target_formats ["3mf"] for separately selectable parts in a slicer.
3. meshy_auto_split, wait, meshy_download_model, then open the file in a slicer (meshy_send_to_slicer).

## Scenario G: General 3D Model (default)
Trigger: none of the above.
Suggested flow:
1. Ask user about intended use to determine format
2. From text, offer the image route (meshy_text_to_image → user approves the image → meshy_image_to_3d with input_task_id; textured in one task), or a quick meshy_text_to_3d preview (+ meshy_text_to_3d_refine for textures)
3. Download in chosen format
