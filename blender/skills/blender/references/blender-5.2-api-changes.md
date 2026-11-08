# Blender 5.2+ API Changes & Best Practices

## Render Settings

- `scene.render.image_format` → `scene.render.image_settings.file_format`
- `scene.render.file_format` removed in modern Blender
- Avoid setting `film_transparent = True` for chat/messaging platform delivery (Telegram, Discord, Slack) unless explicitly requested, as alpha transparency often renders as blank/white.

## Camera Creation & Framing

- `bpy.ops.camera.add()` removed in 5.2
- `bpy.ops.object.camera_add()` is the standard operator
- **Always constrain the camera with `TRACK_TO`** to prevent framing failures:
  ```python
  import bpy

  bpy.ops.object.camera_add(location=(0.0, -3.8, 3.2))
  camera = bpy.context.active_object
  bpy.context.scene.camera = camera

  # Target the subject at origin
  bpy.ops.object.empty_add(type='PLAIN_AXES', location=(0, 0, 0))
  target = bpy.context.active_object
  track = camera.constraints.new(type='TRACK_TO')
  track.target = target
  track.track_axis = 'TRACK_NEGATIVE_Z'
  track.up_axis = 'UP_Y'
  ```

## Primitive Mesh Creation

- `rot_euler` parameter is removed from `bpy.ops.mesh.primitive_torus_add()`.
  Use `location` and set `rotation_euler` on the active object directly.

## Object Removal & Scene Cleanup

- `bpy.data.objects.remove(obj, do_unlink=True)` works in 5.2+.
- Always iterate over `list(bpy.data.objects)` to avoid runtime errors from modifying collections during iteration.

## Principled BSDF Shading Inputs

- `Specular` and `Specular IOR` inputs were restructured in Blender 4.0+. Do not set `bsdf.inputs['Specular']` directly.
- Use `bsdf.inputs['Base Color']` and `bsdf.inputs['Roughness']` for core materials.
- For specular adjustments in Blender 4.0+, use `bsdf.inputs['Specular IOR Level']` if present.

## Render Execution

- `bpy.ops.render.render(write_still=True)` — **not** `write_studio=True` (removed).
- **Do not** pass `display_stage=True` — unrecognized keyword.
- Explicitly configure render output:
  ```python
  import bpy, os, tempfile

  scene = bpy.context.scene
  scene.render.image_settings.file_format = 'PNG'
  scene.render.film_transparent = False
  scene.render.resolution_x = 1080
  scene.render.resolution_y = 1080
  output_path = os.environ.get("BLENDER_RENDER_OUTPUT") or os.path.join(tempfile.gettempdir(), "render_output.png")
  scene.render.filepath = output_path
  bpy.ops.render.render(write_still=True)
  ```
