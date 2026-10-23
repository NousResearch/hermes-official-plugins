---
name: meshy-3d
description: "Create 3D models with Meshy AI from text or images, then texture, remesh, rig, animate, convert, resize, or prepare them for 3D printing (printability repair, multicolor 3MF, split into parts, slicer handoff). Use when the user asks for a 3D model, game asset, rigged character, animation, AR/USDZ file or 3D print, or wants to view, convert, download another format of, or optimize (Blender) a Meshy model they already have."
version: 1.0.0
author: Nous Research
license: MIT
metadata:
  tags: "meshy, 3d, text-to-3d, image-to-3d, texturing, rigging, animation, 3d-printing, game-assets, mcp"
---

<!-- Workflow rules adapted from meshy-mcp-server 0.6.4 (https://github.com/meshy-dev/meshy-mcp-server, MIT, Copyright (c) 2026 Meshy AI). -->

# Meshy 3D

Generates and edits 3D assets on Meshy's cloud through the `meshy` MCP server bundled in this
plugin: text or image to 3D, texturing, remeshing, rigging, animation, format conversion,
real-world resizing, UV unwrapping, 2D concept images, printability analysis and repair,
multicolor 3MF, splitting into printable parts and Creative Lab products. Most calls spend
the user's Meshy credits.

## When to Use

- The user wants a 3D model, prop, character, game asset, AR file (USDZ) or 3D print.
- The user has a Meshy task or model URL to texture, rig, animate, convert, resize or repair.
- The user asks about their Meshy balance, spend or earlier generations.
- NOT for editing a local mesh by hand (use Blender or a mesh tool) or for 2D images unrelated
  to a 3D job.

## Prerequisites

- The `meshy` plugin installed and enabled, then a NEW Hermes session so the MCP tools load.
  Node.js 18+ with `npx` on PATH (the first start downloads the pinned package).
- `MESHY_API_KEY` in the Hermes profile's `.env` (`~/.hermes/.env`, or the profile's own).
  Keys start with `msy_` and come from meshy.ai → Settings → API (Pro plan or above). Hermes
  hands MCP servers only a filtered environment, so a key exported in a shell alone is not
  seen. Never ask for the key in chat; tell the user where to put it.

## How to Run

The tools sit behind Hermes' tool search, not in the visible tool list. Find them with
`tool_search` (for example `meshy text to 3d`, `meshy download model`, `meshy rig`), load
the schema with `tool_describe`, then invoke the exact returned name with `tool_call`.
Never guess a prefixed name. Never run `npx add-mcp`, `hermes mcp add meshy` or any other
setup step from Meshy's docs: the plugin already registers the server, and a second
`mcp_servers.meshy` entry in `config.yaml` replaces the plugin's pinned one.

The server is marked untrusted, so Hermes asks the user before every call that is not
read-only (every create, download, cancel). Read-only calls (balance, status, lists, slicer
detection) run without a prompt. When a call comes back "The user did not approve", stop:
the user declined, or the run is unattended (cron, `chat -q`, a gateway with no approver)
and cannot approve. Do not retry, inspect config or look for a workaround, and never suggest
changing the server's trust, editing config or "always allow" as the fix; tell the user the
exact call you would make and its cost, and that an interactive session (Desktop, CLI, or a
chat gateway that shows approval buttons) can approve it. For a finished task, the signed link
from `meshy_get_task_status` (read-only, no prompt) is the way to hand over the file meanwhile.

## Quick Reference

Full list with credits: [tools](references/tools.md). Scenario playbooks (printing, game
engine, character animation, AR, retexture, conversion, UV unwrap, Creative Lab, cheap
topology, print in parts): [workflows](references/workflows.md). Read the workflow for the
user's goal before the first paid call.

| Goal | Chain |
|---|---|
| Model from text | `meshy_text_to_3d` (preview, untextured) → `meshy_get_task_status` → `meshy_text_to_3d_refine` (textures) → `meshy_download_model` |
| Model from a photo | `meshy_image_to_3d` with `file_path` (textured by default) → status → download |
| Concept first | `meshy_text_to_image` → user approves → `meshy_image_to_3d` with `input_task_id` |
| Rigged + animated character | generate with `pose_mode: "t-pose"`, textured → `meshy_rig` → `meshy_list_animations` → `meshy_animate` |
| Print | `meshy_send_to_slicer` (detect, free) → generate with `target_formats: ["obj"]` → `meshy_analyze_printability` (free) → download with `print_ready: true` |
| A file type for a finished task | `meshy_get_task_status` (`model_urls`) → `meshy_download_model` `format: X` (free), else `meshy_convert` (1 credit) |
| Balance / spend | `meshy_check_balance`, `meshy_list_usage` (free) |

## Procedure

1. **Pick the scenario** from [workflows](references/workflows.md), the format from
   [Pick the right format](#pick-the-right-format) and the detail level from
   [Pick the detail level](#pick-the-detail-level) before generating: `target_formats` is
   fixed at creation, and `3mf` exists only when requested (or via `meshy_convert`).
2. **Confirm cost before every credit-spending call.** Name the tool, the `ai_model` where
   there is a choice, the credit cost from [tools](references/tools.md) and any surcharge (8K
   textures, Ultra geometry), and wait for the user's yes. Hermes' approval prompt is the
   second gate, not the first. Offer `meshy_check_balance` when the job is large.
3. **Submit once.** Every create returns `{task_id, status}` (Creative Lab waits and returns
   the model). Keep the `task_id` and task type.
4. **Wait** with `meshy_get_task_status` (`wait: true`, default 300 s per call). Generation
   usually takes 2–3 minutes. A wait that times out leaves the task running: poll the same
   `task_id` again. **On Hermes Desktop, don't wait when nothing else needs the result:** right after
   the create, end the reply with `::meshy-model{task="<task_id>" title="<short name>"}` on its own
   line. The card shows Meshy's progress, then the model with its Next and Download menus. Wait only
   when the next step needs the finished task (a chained refine/rig, a download into the project, a
   chat app).
5. **Never re-create on doubt.** If a create fails with a timeout, network error or 5xx, the
   task may exist and be billed: check `meshy_list_tasks` with that `task_type` first.
6. **Download** with `meshy_download_model`, one `format` per call (singular; there is no
   `formats`), the one the use case needs, not always GLB. Pass `save_to` as an ABSOLUTE path in
   the user's project or wherever they asked. Without it, files land under the plugin's data
   directory (`meshy_output/`), not the current project. Report every saved path and the file size.
7. **Report** the result: files and paths, formats, credits spent, the ACTUAL triangle count
   (see [Pick the detail level](#pick-the-detail-level)) and the `task_id` for follow-ups
   (refine, rig, convert reuse it instead of regenerating). **Describe what the file holds, not what
   the task claims:** a SUCCEEDED rig or animation task was billed, which doesn't prove the skeleton or
   clips landed in the file. Run `python3 scripts/glb_stats.py <saved .glb>` (this skill's folder) and
   say "rigged" only when `skinned_mesh_nodes` > 0 (`skins`/`joints` can be unused definitions), and
   "animated" only by naming the `clips` it lists.
7b. **Offer the optional Blender copy, before step 8** (local, no additional Meshy credits) after a `.glb` download or
   when showing a local Meshy model. First run `hermes config get plugins.entries.meshy.settings.blender_handoff`
   ("not set" means `ask`). Unless it is `never`, find Blender: `hermes config get plugins.entries.meshy.settings.blender_path` first, then PATH and the per-OS folders in [blender](references/blender.md) §2.
   If Blender is found and the setting is `ask`, put this offer, word for word, just above the `::meshy-model` line:
   "Blender is installed: want an optimized copy for game testing (packed textures, optional triangle budget)
   plus a .blend? Runs locally, no additional Meshy credits." Persist an always/never answer with
   `hermes config set`. Never decimate without the user's triangle budget. Rigs and clips:
   [blender-animation-handoff](references/blender-animation-handoff.md); rendered shots:
   [cinematic-preflight](references/cinematic-preflight.md). Report **downloaded**, **reopened/import-validated**,
   **visually reviewed**, **artist-approved** and **tested in the target engine/render** as different states;
   none implies the next.
8. **Deliver it for the surface you are on** (your system prompt names the platform): see
   [Delivery by platform](#delivery-by-platform). The card below is Desktop-only.
9. **Show it in Hermes Desktop (Desktop only).** After a `.glb` download, end the reply with a paragraph that is
   only the directive `::meshy-model{path="<absolute path of the .glb>" task="<task_id>"}` (one per
   model, each on its own line). Desktop renders it as an interactive 3D card whose **Download**
   menu lists every file type Meshy produced for that task (a refined model: GLB, FBX, OBJ, STL,
   USDZ) and saves the one the user picks beside the .glb, at no credit cost; without `task=` the
   card has no menu. Other clients show the line as text, so write the path in prose as well.
   To show a task the user has not downloaded (running or finished), use `::meshy-model{task="<task_id>"}`:
   the card downloads the GLB into the plugin's folder (free) only when the user opens it. A `url=`
   card with a signed Meshy link does not render in current Desktop builds.
   Every result (refine, remesh, retexture, rig, animation) gets its OWN card with ITS task id, so
   the card's **Next** menu offers what that task supports. **Next** sends a message such as
   `Meshy task <id>: remesh it with meshy_remesh (5 credits) ...`. The cost in that message is the
   user's confirmation of the cost. Still ask for any choice the message tells you to ask (style,
   triangle target, height, animation ids) before the call. Regenerate is always a new paid task:
   Meshy's free retries exist only on meshy.ai, and API-made tasks don't appear there.

## Pick the detail level

Unless the user already said, ask what the asset IS and where it goes, not for a triangle number.
First run `hermes config get plugins.entries.meshy.settings.default_detail` ("not set" means `ask`;
`game`, `mobile` or `detailed` picks that route without asking, but the user's own words win).

| Asset | Route and `target_polycount` (PC / console; mobile ≈ half) |
|---|---|
| Player character, boss, hero NPC | Smart: 15000 (up to 30000 for a stylised hero look) |
| NPC, common mob, creature | Smart: 5000–15000 |
| Weapon, tool, held item | Smart: 3000 (1000–5000) |
| Small prop (chest, barrel, crate) | Smart: 1500 (300–3000) |
| Building, large set piece | Standard, then split or reduce; 5000–50000 total |
| Close-up, renders, cinematics, 3D printing | Standard full detail (`latest`, no remesh) |
| Fine carved detail AND a budget | Standard, then `meshy_remesh` (5 credits) or the Blender copy |

Smart = `model_type: "smart-topology"`, `ai_model: "meshy-t2.5"` (100–30000). Text: 5 preview
(+10 refine for textures); image: 15 textured. Polycount never changes the price.

Put the choice in one line inside the cost confirmation, e.g. "Smart Topology at 1,500 triangles
for a game prop, 5 credits for the untextured preview; say if you want more detail." Smart
Topology is triangle-only (no quads), keeps textured characters and props close to Standard at game
distance, but smooths away engraving, filigree and crisp bevels, can drop thin hanging parts (cloth
tails) and returns several separate, partly open parts (merge them on import; one material = one draw
call). After generation, report the ACTUAL triangle count (it differs from the target), never the
requested one: details in [workflows](references/workflows.md) Scenario K.

**Many on screen (MMO, crowds, mobile):** the per-model count matters less than the total per frame.
Offer once, after the download, the Blender copy's game set (`--lods 0.5,0.25,0.1 --max-texture 2048`,
1024 for props and mobile): lower-detail versions for distance and smaller textures, which usually
run out before triangles do. Meshy textures are often 4K. Skinned meshes need the user's OK for
`--allow-skinned` first, and the result's `lost` list must be reported.

## Pick the right format

Every finished model task already holds signed links to ALL its formats:
`meshy_get_task_status` returns them in `structuredContent.model_urls` (a refined or textured
model: `glb, fbx, obj` + `mtl`, `usdz, stl`; a text-to-3d preview: often `glb` only) plus
`thumbnail_url`; Hermes passes that JSON to you. Links expire 3 days after the task finished
(the CDN `Expires=` in the URL is not the real limit); saved files are permanent. Ask what
the file is for, then:

| Use case | Format | Notes |
|---|---|---|
| Unity | FBX | GLB also works through the glTFast package |
| Unreal Engine | FBX | |
| Godot | GLB | glTF is Godot's native import |
| Web, three.js, Babylon, `<model-viewer>`, sharing a viewable file | GLB | one self-contained file |
| Apple AR (Quick Look, Vision Pro, iOS) | USDZ | |
| 3D print, single colour | STL (or OBJ with `print_ready: true`) | |
| 3D print, multicolour | 3MF | via `meshy_process_multicolor`, or `target_formats: ["3mf"]` at creation |
| Other DCC tools (Maya, 3ds Max, C4D) | FBX, or OBJ for a static mesh | |

- **Get it:** `meshy_download_model` with `format` = that type (free). If the task lacks it, the
  error lists what it has: `meshy_convert` with `target_formats: [type]` (1 credit), wait, then
  download from the convert task.
- **Rigging / animation tasks are not saved:** `meshy_download_model` returns signed GLB/FBX links
  (rig: character + free walk/run clips; animation: GLB, FBX, sometimes USDZ). To keep one, fetch
  the link into the target folder (for example `curl -L -o <abs path>.fbx '<url>'`).
- **Images** (text-to-image / image-to-image): `meshy_download_model` saves their own PNGs; omit
  `format`. It only takes 3D formats, so `format: "png"` fails validation before the call runs.
- **More than one type** (engine FBX plus a GLB to preview): one download call per type.
- **Never call a download "game-ready" or "Unity-ready":** it has no LODs, collision mesh or
  hand retopology, and scale/pivot may need fixing; say so when handing it to a game project
  (game workflow: Scenario C in [workflows](references/workflows.md)).

## Delivery by platform

Your system prompt says which surface you are on. Always state the task_id, format and why,
credits spent, and each saved path with its size. Whenever you hand out a signed Meshy link,
write next to it: "works for 3 days after the task finished" (or the exact `expires_at` when a
download result gave one), and that a saved file is permanent.

- **Hermes Desktop:** the `::meshy-model` card (step 9) for a GLB; `MEDIA:<abs path>` for any
  other file becomes a download card.
- **Messaging (Telegram, Discord, Slack, WhatsApp, Signal, Matrix ...):** never emit the
  `::meshy-model` line (it shows as raw text). Send the thumbnail as an image: `![preview](<thumbnail_url>)`
  on its own line (from `meshy_get_task_status`). Attach the file with a line that is only
  `MEDIA:<absolute path>`; the gateway uploads it as a document. Write each local path only on its
  `MEDIA:` line: the gateway also uploads any bare absolute file path in the reply, so repeating it in
  prose sends the file twice. Upload caps: Telegram 20 MB on the hosted Bot API,
  Discord 20 MB (more on boosted servers), others vary. A refined GLB is
  often 10-40 MB and OBJ far larger: when the file is near or over the cap, or the upload is
  reported as skipped, give the signed `model_urls` link for that format instead and say it stops
  working at `expires_at`. Gateways in strict media mode deliver only files under Hermes' cache
  folders or created in the last 10 minutes: download right before you reply, or `save_to` under
  `<HERMES_HOME>/cache/documents/`.
  For animated or rigged models (and as a quick look at any model), render a short preview video
  with [preview video](references/preview-video.md) and send it as an attachment.
  Cards can't be clicked here, so end with the next steps as a short numbered list with credits, the
  same set Desktop's Next menu shows for that task type, e.g. preview: "1. Texture it · 10 cr
  2. Regenerate · <its cost> 3. Remesh · 5 cr 4. Check printability · free"; textured or image:
  remesh 5, retexture 10, rig 5 (humanoids), resize 1, printability free; rig: animate 3 per action.
  A reply with just a number is that choice.
- **CLI / TUI:** plain text, no `MEDIA:` (printed literally): the absolute path, plus the signed
  link with its expiry if the user may want it elsewhere.
- **Approvals off Desktop:** every create and download asks first. On a gateway the approval
  arrives as buttons (Telegram, Discord, Slack, Matrix, Teams ...) or as a text `/approve` prompt
  in the chat. Cron jobs, `hermes chat -q` and API runs without an approver cannot approve: stop
  and say which call, format and cost you would run; never look for a way around the gate.

## Pitfalls

- A text-to-3d preview has no textures: refine it (10 credits) or use the image route, which
  is textured in one task.
- Rigging needs a textured humanoid at ≤300K faces; remesh first if it is denser.
- Animation `action_id`s come from `meshy_list_animations`; never invent one. In-place variants
  (`search: "inplace"`) exist for games that move the character: some library clips travel,
  the rig's free walk/run don't.
- UV unwrap needs a GLB with ≤40,000 faces.
- `meshy_cancel_task` on a finished task DELETES it (and needs `confirm_delete: true`);
  confirm with the user before calling it.
- `meshy_list_tasks` without `task_types` covers only the five generation types; an empty
  list does not mean there are no rigging, printing or Creative Lab tasks.
- Local inputs (`file_path`, `file_paths`) must be absolute paths the server can read; never
  base64-encode an image yourself.
- `meshy_send_to_slicer` returns a `launch_command` to run with the terminal tool. Run it only
  after the user picks the slicer.
- Errors mentioning `401` or an invalid key mean `MESHY_API_KEY` is missing or wrong in the
  profile `.env`; the user must fix it and start a new session.

## Verification

- `meshy_check_balance` answers with a credit balance: server connected and key valid.
- After a download, the reported path exists and its size is non-zero.
