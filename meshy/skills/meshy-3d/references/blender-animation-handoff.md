# Blender animation handoff

For several Meshy rig/animate downloads of one character (each import brings its own armature +
mesh + clip). Rules from community review by @123mikeyd (PR #115). Downstream handling advice,
not Meshy defects.

## Consolidate without breaking the source

- Inspect each clip on its original rig+mesh pair first; why: proves the clip works before any change.
- Pick ONE canonical performer; keep the other imports as protected clip carriers outside the
  active shot (own collection, hidden, not deleted); why: they are the only proven source.
- An animation-only armature is not a reason to rebind the canonical mesh.
- Do not delete a carrier until the consolidated result has been reopened and played.

## Matching bone names are not compatibility

- Compare hierarchy, full rest transforms, bone roll and object scale before reusing an Action;
  why: same names with different rests give twisted limbs.
- If they differ, retarget/bake a target-specific copy; never assign foreign local rotations.
- Do not zero non-identity pose-bone bases on imported glTF rigs; some depend on them.
- Do not edit bind/rest data to make bone display look tidy; why: it breaks the working skin.

## Keep an animation library

- Stable descriptive Action names (`knight_walk_loop`), and record: Meshy task id, frame range,
  FPS, loop yes/no, root motion yes/no, target rig.
- Set Fake User on retained Actions; why: unused Actions are purged on save.
- Blender 4.4+ (slotted Actions): assign the Action AND its compatible slot; why: an Action
  without the right slot plays nothing.
- Recheck scene FPS after every FBX import; why: the importer can change it.
- Reopen the .blend and play each clip before retiring carriers.

## Tools here

- `scripts/blender_handoff.py` reports armatures (bone count), actions (frame ranges, fake user),
  shape keys and scene FPS for source and every reopened output, with `preserved`/`lost`.
- It refuses to decimate skinned meshes (`--allow-skinned` overrides) and meshes with shape keys
  (`--allow-shape-keys` deletes them); check deformation in a few poses after any decimation.
- FBX export loses shape-key animation (Blender 4.5 live result): ship morphs as GLB.
- Reported state after the script: reopened-validated. Played, visually reviewed, artist-approved
  and engine-tested are separate steps.
