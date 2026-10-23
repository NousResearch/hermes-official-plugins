# Meshy MCP tools (meshy-mcp-server 0.6.4)

Generated from a live `tools/list` of `@meshy-ai/meshy-mcp-server@0.6.4`. Credits are the server's own figures (2026-10-12 pricing); `free` tools never spend.

| Tool | Read-only | What it does |
|---|---|---|
| `meshy_text_to_3d` | no | Generate an untextured 3D PREVIEW from a text prompt; texture it afterwards with meshy_text_to_3d_refine once the user approves. Typically 2–3 minutes. |
| `meshy_image_to_3d` | no | Generate a 3D model from one image, textured by default (no refine step; should_texture false for a bare mesh). Typically 2–3 minutes. |
| `meshy_text_to_3d_refine` | no | Texture a SUCCEEDED meshy_text_to_3d preview. Cost: 10 credits (15 at 8K). Get the user's approval first; omit ai_model to inherit the preview's model. |
| `meshy_multi_image_to_3d` | no | Generate a 3D model from 1–4 views of the same object (better reconstruction), textured by default. |
| `meshy_get_task_status` | yes | Wait for a task to finish (wait=true, default) or check it once (wait=false). Reports the task's real task_type; pass it to the download and cancel tools. |
| `meshy_list_tasks` | yes | List tasks of any status, newest first by default, merged across the types queried (one request per type). Default: text-to-3d, image-to-3d,… |
| `meshy_cancel_task` | no | Cancel a queued task, or delete a finished one. What happens depends on the task: |
| `meshy_download_model` | no | Save a finished task's output under meshy_output/ (project folders with history), or at save_to. |
| `meshy_list_models` | yes | List your finished 3D models (not AI models), newest first, each with its task_id, task_type and formats ready for meshy_download_model. Queries only… |
| `meshy_remesh` | no | Remesh an existing model: change its polygon count or topology. Cost: 5 credits. For a format change only use meshy_convert (1 credit); to scale,… |
| `meshy_retexture` | no | Apply new AI textures to an existing model. Cost: 10 credits (15 at 8K). |
| `meshy_rig` | no | Auto-rig a humanoid character for animation. Cost: 5 credits, including free walking + running animations (GLB + FBX, separate files). |
| `meshy_animate` | no | Animate a rigged character (rig_task_id). Cost: 3 credits per action. Give exactly ONE of action_id, action_ids or motion_task_id. |
| `meshy_text_to_motion` | no | Generate a character motion clip (skeleton motion, not a model) from text. Cost: 10 credits (prime) or 3 (swift) — confirm first. Clips are kept for 3 days. |
| `meshy_list_animations` | yes | Look up preset animations: the action ids meshy_animate takes. Free. Filters combine; with none the whole (long) library comes back, so prefer search or… |
| `meshy_convert` | no | Convert a model to other file formats. Cost: 1 credit (cheaper than remesh for a format-only change). |
| `meshy_resize` | no | Rescale a model to real-world size on Meshy. Cost: 1 credit. Give exactly one of resize_height, resize_longest_side or auto_size. |
| `meshy_uv_unwrap` | no | Make a clean, non-overlapping UV layout ("UV white model") before texturing in Blender, Substance Painter or Unreal. Cost: 5 credits. |
| `meshy_text_to_image` | no | Generate a 2D image from text, e.g. a design for the user to approve before 3D. Typically ~30 s. |
| `meshy_image_to_image` | no | Edit or restyle images with a text prompt. Cost: nano-banana 3, nano-banana-2 6, nano-banana-2-lite 3, nano-banana-2-1 4, nano-banana-pro 9, GPT Image… |
| `meshy_send_to_slicer` | yes | Detect installed 3D printing slicers and return their launch commands. Call it at the START of a print workflow and keep the result; at the END, replace… |
| `meshy_analyze_printability` | no | Check whether a model is printable: watertightness, non-manifold edges, holes, degenerate faces and volume — plus thin walls when printer_type and/or a… |
| `meshy_repair_printability` | no | Repair a model for 3D printing (FDM, SLA/resin or full-color): fixes non-manifold edges, degenerate faces and holes and makes it watertight. Cost: 10… |
| `meshy_process_multicolor` | no | Turn a textured model into a multi-color 3MF that opens ready to print in the slicer of printer_brand. Cost: 10 credits. |
| `meshy_auto_split` | no | Cut a model into separately printable parts: when it is too big for the bed, parts need different colors/materials, or it should be assembled. Cost: 10… |
| `meshy_creative_lab` | no | Turn a photo (file_path or image_url; no text prompt) into a finished Creative Lab product 3D model, end to end. |
| `meshy_check_balance` | yes | Check the Meshy credit balance. Free. For team members it is the team's shared pool. |
| `meshy_list_usage` | yes | List the team's finished API tasks with the credits each consumed, newest first. Free. Use it to break down spend per endpoint or API key. Only for API keys… |
