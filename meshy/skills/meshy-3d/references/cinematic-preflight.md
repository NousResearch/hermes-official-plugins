# Cinematic preflight

For assembling Meshy assets into a rendered Blender shot or film. Rules from community review by
@123mikeyd (PR #115). Each rule: what to do; why.

## Files

- Keep the asset library .blend separate from the film .blend; save a NEW film file before adding
  cameras, NLA, lights or effects; why: the library stays reusable.
- Never write a background-generated file over a live or unsaved artist scene or their placement.
- Look-dev changes go on material copies; why: approved textures stay recoverable.

## Motion

- Keep body motion (skeletal Action), world travel (placement/motion root) and camera separate.
- Measure existing root motion before adding travel; why: avoids applying it twice.
- Accumulate travel across clips instead of snapping back to origin.
- Check facing vs travel direction, planted feet, and floor clearance of the deformed mesh (not the
  rig origin).

## Clip edges and seams

- Imported clips can carry bind-pose lead-ins, dead frames or recovery tails: trim the NLA strip,
  never the protected source Action.
- Preserve exact/fractional source endpoints when retiming.
- Inspect consecutive frames around every NLA join and loop seam; hold a reviewed baseline pose in
  gaps; why: one good middle frame is not a playback test and gaps flash a T-pose.

## Props and effects

- Attach props/VFX from the final evaluated pose in world space; check contact and visibility.
- Keep rigid accessories rigid. Key effect-light energy off outside its beat (hiding the mesh
  is not enough).
- Judge readability through the shot camera at delivery size.

## Approval and render

- Order: viewport choreography -> representative stills -> full render. Review one full unfamiliar
  retarget before batching more clips.
- Check framing, hands/feet, props, transition frames; an ensemble member in the Outliner may not be
  visible on screen. Optimize distant actors separately from the hero; compare stills and motion.
- Benchmark heavy frames, state the estimated render time and get approval before long renders.
- Render numbered image sequences (restartable), then encode a shareable video.
- Music pieces: extend choreography, extras and cameras through the whole audio.
- Verify the final video: FPS, duration, frame count, audio, coverage and seams.

## States

Report separately: downloaded / reopened-validated / visually reviewed / artist-approved /
tested in the target render or engine. None implies the next.
