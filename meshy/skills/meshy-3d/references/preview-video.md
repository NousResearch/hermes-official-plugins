# Preview video for chat apps

Meshy returns GLB/FBX/USDZ files and a thumbnail image, never a video. Discord, Telegram,
Slack, WhatsApp, Signal and the CLI cannot spin a 3D model, so on any messaging platform
render a short MP4 locally and send it as an attachment. A static model becomes a 360°
turntable; a rigged/animated model plays its animation clip (walking, running, any
`meshy_animate` result) with a slow orbit.

## When to use

- The user is on a messaging platform (Discord, Telegram, Slack, WhatsApp, Signal, email...)
  and a model just finished, or they ask to "see", "show" or "preview" a model.
- Always for rigged or animated results: the thumbnail is a T-pose and shows no motion.
- On the CLI when the user wants a quick look without opening a 3D viewer.
- Hermes Desktop renders models inline: skip the video there unless asked.

## Render

Download the GLB first (`meshy_download_model`, or the `*_glb_url` of the task). For a rig,
the animation lives in its own GLB (`basic_animations.walking_glb_url`,
`running_glb_url`, or the `meshy_animate` result), not in `rigged_character_glb_url`.

```bash
node <skill_dir>/scripts/render_preview.mjs /abs/path/model.glb --label "Knight"
```

`<skill_dir>` is the directory of this skill (the folder holding SKILL.md). Options:
`--clip NAME` (default: first clip; substring match), `--list-clips`, `--seconds N`
(default: 6 s turntable; animations loop whole cycles to at least 4 s, at most 8 s),
`--fps N` (default 30, 10 to 60), `--size N` (square, default 720), `--max-mb N` (default 8),
`--out /abs/file.mp4`, `--no-label`.

It prints one JSON line. On success: `{"ok": true, "out": "<abs .mp4>", "mb": ..., "seconds":
..., "clip": ..., "render_s": ...}`. Default output:
`<HERMES_HOME>/cache/videos/meshy/<model>-<clipN-name|turntable>.mp4`. That directory is on the
gateway's media allowlist (`gateway/platforms/base.py` `MEDIA_DELIVERY_SAFE_ROOTS` includes
`<HERMES_HOME>/cache/videos`), so delivery works even with `HERMES_MEDIA_DELIVERY_STRICT` on.
Keep the default unless the user names a place.

Rendering is CPU-only (SwiftShader WebGL). Rough cost on a desktop CPU: 60k-triangle
character ~20 s; a 1.75M-triangle scan a few minutes. Run it with a terminal timeout of
600 s for big scans. The output is H.264 yuv420p, faststart, no audio; encoded at CRF 20
and re-encoded at a capped bitrate when that would exceed `--max-mb`, so frame rate and
length are never cut to fit.

The render itself is offline: the page never navigates, and only resources embedded in the GLB
load. An external image/buffer URI inside the GLB is not fetched (a missing texture renders white
and the JSON lists it under `blocked_urls`; an external buffer makes the load fail). The output
is encoded to a private file and renamed into place; an `--out` that is the source GLB (or a
hard link to it) is refused.

## Deliver

Reply with a short sentence and the video on its own line, plus the thumbnail if useful:

```
Here's your knight walking (4.3 s loop).
MEDIA:/home/me/.hermes/cache/videos/meshy/knight_walking-clip1-walking_man.mp4
```

The gateway strips the `MEDIA:` line and uploads the file natively (`.mp4` goes to the
platform's video send). Use the exact `out` path from the JSON; never invent one. Put the
path ONLY on the `MEDIA:` line: the gateway also uploads any bare absolute file path it finds
in a reply, so repeating it ("The file is at /...mp4") sends the video twice.

Upload limits: Discord 10 MB without Nitro (the 8 MB default leaves headroom), Telegram
bots 50 MB, Slack 1 GB, WhatsApp 16 MB, Signal 100 MB. For long clips or `--size 1080`, keep
`--max-mb 8` unless the platform allows more.

## If rendering is unavailable

The script exits with `{"ok": false, "error": ...}` when a dependency is missing:

- No Chromium/Chrome: Hermes' browser tool bundles one (found automatically under
  `HERMES_RUNTIME_DIR`/`~/.hermes/tools/chromium-*`); otherwise any Chrome, Chromium or Edge
  install works, or `npx -y playwright@1.56.1 install chromium-headless-shell`, or
  `--chrome /path`.
- No ffmpeg: Hermes ships one (`~/.hermes/tools/ffmpeg-*/bin`); else ffmpeg on PATH or
  `FFMPEG_PATH`. Without it the script writes numbered PNG frames and says so (send one).
- three.js: taken from the plugin's `node_modules` when present, else `three@0.180.0` is
  downloaded once from the npm registry (integrity-checked) into
  `<HERMES_HOME>/cache/meshy-preview/`. Needs Node 22+ (built-in `fetch`/`WebSocket`).

Then fall back: send the task's `thumbnail_url` image (download it into
`<HERMES_HOME>/cache/images/` and attach with `MEDIA:`) and the signed GLB URL, telling the
user the link expires (Meshy signs asset URLs for a limited time; re-fetch the task for a
fresh one). Do not loop retrying the render.
