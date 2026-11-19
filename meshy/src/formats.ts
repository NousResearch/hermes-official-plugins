/**
 * Which saved model files the viewer can draw, and how. Pure (no three.js) so node tests cover it.
 *
 * Meshy exports GLB, FBX, OBJ (+ .mtl + texture), STL, USDZ, 3MF and BLEND. three.js ships a loader for
 * every one but BLEND; those files are still listed in the gallery, with a plain tile instead of a 3D view.
 */

/** Formats the viewer renders with three.js's own loaders. */
export const VIEWABLE_FORMATS = ['glb', 'gltf', 'fbx', 'obj', 'stl', 'usdz', '3mf'] as const
export type ViewableFormat = (typeof VIEWABLE_FORMATS)[number]

/** Best first: when one task has several saved formats, the gallery opens the richest one (textures,
 *  animation) instead of whichever was saved last. */
const RANK = ['glb', 'gltf', 'fbx', 'usdz', 'obj', '3mf', 'stl', 'blend']

/** Extensions the file picker offers for "view a model file" (and the backend's /upload accepts). */
export const PICKABLE_EXTENSIONS: readonly string[] = VIEWABLE_FORMATS

/** `"/a/Robot.FBX"` → `"fbx"`; no extension → `""`. */
export function formatOf(path: string): string {
  const name = path.split(/[\\/]/).pop() ?? ''
  const dot = name.lastIndexOf('.')
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : ''
}

/** The format a task card opens: the backend's pick when it is one the viewer draws, else GLB (older backends
 *  send no `view_format`, and their cards only showed View for a GLB). */
export function openFormat(info: { view_format?: null | string } | null | undefined): string {
  const pick = info?.view_format
  return pick && (VIEWABLE_FORMATS as readonly string[]).includes(pick) ? pick : 'glb'
}

export function canRender(path: string): boolean {
  return (VIEWABLE_FORMATS as readonly string[]).includes(formatOf(path))
}

/** 3D-printing formats are Z-up by convention; three.js (and glTF/FBX/USD) are Y-up. */
export function isZUp(format: string): boolean {
  return format === 'stl' || format === '3mf'
}

/** The `mtllib` an OBJ names in its header (Meshy writes one; /save repoints it to `<stem>.mtl`). */
export function mtlLibOf(objHead: string): null | string {
  const match = /^[ \t]*mtllib[ \t]+(.+?)[ \t]*$/m.exec(objHead)
  return match ? match[1] : null
}

/** Texture files an .mtl references (`map_Kd tex.png`, options like `-s 1 1 1` skipped). */
export function mtlTextures(mtl: string): string[] {
  const out = new Set<string>()
  for (const line of mtl.split(/\r?\n/)) {
    const match = /^[ \t]*(?:map_\w+|bump|disp|norm|refl)[ \t]+(.+?)[ \t]*$/i.exec(line)
    if (!match) {
      continue
    }
    // The file name is the last token; options (-bm 1, -s 1 1 1 ...) come first.
    const tokens = match[1].split(/[ \t]+/)
    out.add(tokens[tokens.length - 1])
  }
  return [...out]
}

/** A companion file beside `modelPath` (`robot.obj` + `robot.mtl` → `/dir/robot.mtl`). Only plain relative
 *  names: anything absolute or climbing out with `..` is refused here (the backend checks roots again). */
export function siblingPath(modelPath: string, rel: string): null | string {
  const clean = rel.trim().replace(/\\/g, '/')
  if (!clean || clean.startsWith('/') || /^[a-z]:/i.test(clean) || clean.split('/').includes('..')) {
    return null
  }
  const cut = Math.max(modelPath.lastIndexOf('/'), modelPath.lastIndexOf('\\'))
  const sep = modelPath.includes('\\') && !modelPath.includes('/') ? '\\' : '/'
  return `${modelPath.slice(0, cut + 1)}${clean.split('/').join(sep)}`
}

/** The saved file to open for a task: GLB over FBX over ... over STL, newest first within a format. */
export function bestForTask<T extends { path: string; task?: null | string }>(files: readonly T[], task: string): T | undefined {
  let best: T | undefined
  let bestRank = Infinity
  for (const file of files) {
    if (file.task !== task) {
      continue
    }
    const rank = RANK.indexOf(formatOf(file.path))
    const score = rank < 0 ? RANK.length : rank
    if (score < bestRank) {
      best = file
      bestRank = score
    }
  }
  return best
}
