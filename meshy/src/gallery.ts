/** Gallery decisions shared by the list and provenance navigation. No network or rendering. */
import { bestForTask } from './formats.ts'
import type { Locality } from './scope.ts'

export interface RemoteAsset {
  task_id: string
  task_type: string
  prompt: string
  created_at: number
  thumbnail_url: null | string
  glb_url: null | string
  kind: 'image' | 'model'
  image_urls: string[]
  expired: boolean
  local_path?: null | string
  credits?: null | number
}

export interface LocalFile {
  kind?: 'image' | 'model'
  download_incomplete?: boolean
  images?: string[]
  path: string
  name: string
  project: string
  bytes: number
  modified: number
  /** Meshy task id when recorded (MCP metadata.json, /save, or a card that opened it); enables Download. */
  task?: null | string
  /** What made it (its prompt, the nearest ancestor's, or its source image), when recorded. */
  prompt?: null | string
  /** Backend's answer: the viewer can draw it (a format three.js loads, under the size cap). */
  viewable?: boolean
}

export type Source =
  | {
      kind: 'file'
      path: string
      task?: string
      media?: 'image' | 'model'
      images?: string[]
      label?: string
      incomplete?: boolean
    }
  | {
      kind: 'remote'
      url: string
      label: string
      task?: string
      media?: 'image' | 'model'
      images?: string[]
      expired?: boolean
    }

export function sourceForLocalFile(file: LocalFile): Source {
  return {
    kind: 'file',
    path: file.path,
    task: file.task || undefined,
    media: file.kind || 'model',
    images: file.images,
    incomplete: file.download_incomplete,
    label: file.prompt || file.name
  }
}

export function sourceForAsset(asset: RemoteAsset): Source | null {
  if (asset.kind === 'model' && asset.local_path) {
    return { kind: 'file', path: asset.local_path, task: asset.task_id }
  }
  const url = asset.kind === 'image' ? asset.image_urls[0] : asset.glb_url
  return url ? {
    kind: 'remote',
    url,
    label: asset.prompt || asset.task_type,
    task: asset.task_id,
    media: asset.kind,
    images: asset.kind === 'image' ? asset.image_urls : undefined,
    expired: asset.expired
  } : null
}

/** A task may contain several clips/files. Only generic task selections may change files. */
export function reconcileSelection(
  selected: Source | null,
  task: string,
  explicit: boolean,
  remote: RemoteAsset[],
  saved: Source[]
): Source | null {
  const exact = selected?.kind === 'file'
    ? saved.find(source => source.kind === 'file' && source.path === selected.path && source.task === task)
    : undefined
  if (explicit) return exact || selected
  const local = exact || bestForTask(saved.filter((source): source is Extract<Source, { kind: 'file' }> => source.kind === 'file'), task)
  const asset = remote.find(row => row.task_id === task)
  return local || (asset && sourceForAsset(asset)) || (selected?.kind === 'file' ? selected : null)
}

/** The saved file a task opens from the list: its GLB before an FBX/STL saved later (formats.ts). */
export function bestLocalForTask(files: readonly LocalFile[], task: string): LocalFile | undefined {
  return bestForTask(files, task)
}

export function matchesKind(filter: 'all' | 'image' | 'model', kind: 'image' | 'model'): boolean {
  return filter === 'all' || filter === kind
}

/** A product route `/models` could not list this time (plan-gated, or that route's outage); the rest still list. */
export interface ListFailure {
  task_type: string
  error: string
}

/** One notice line per distinct reason, naming the products it hid; none when every product listed. */
export function listFailureNotices(failed: unknown): string[] {
  const byReason = new Map<string, string[]>()
  for (const f of Array.isArray(failed) ? (failed as Partial<ListFailure>[]) : []) {
    if (typeof f?.task_type !== 'string' || typeof f.error !== 'string') continue
    byReason.set(f.error, [...(byReason.get(f.error) ?? []), f.task_type])
  }
  return [...byReason].map(([reason, types]) => `Couldn't list ${types.join(', ')}: ${reason}`)
}

export interface StorageStatus {
  bytes: number
  files: number
  directory: string
}

export function imageNextActions(task: string, source: Source, imageIndex = 0) {
  const images = source.images?.length ? source.images : [source.kind === 'file' ? source.path : source.url]
  const index = Math.max(0, Math.min(imageIndex, images.length - 1))
  const reference = source.kind === 'file'
    ? `Use only the saved local image ${JSON.stringify(images[index])}; the task ID is for reference.`
    : images.length === 1 ? `Use input_task_id ${JSON.stringify(task)}.`
    : `Use only image ${index + 1} of ${images.length} from this task. Retrieve its image_urls and use that image URL, not the whole task's image set.`
  const rows = [
    {
      id: 'model',
      label: 'Make a 3D model',
      message: 'Make a 3D model from the displayed image with meshy_image_to_3d. Ask about intended use and mesh detail first.'
    },
    {
      id: 'edit',
      label: 'Edit image',
      message: 'Edit the displayed image with meshy_image_to_image. Ask what changes I want first.'
    },
    {
      id: 'views',
      label: 'Generate multiple views',
      message: 'Generate front, side and back views using meshy_image_to_image with generate_multi_view true. Ask about any desired changes first.'
    }
  ].map(row => ({ ...row, reference }))
  if (images.length > 1) {
    rows.push({
      id: 'all-model',
      label: 'Make 3D from all views',
      message: 'Make a model with meshy_multi_image_to_3d. Ask about intended use and select up to four suitable views first.',
      reference: source.kind === 'file'
        ? `Use the saved image files ${JSON.stringify(images)}.`
        : `Use input_task_id ${JSON.stringify(task)} for the image set.`
    })
  }
  return rows.map(row => ({
    ...row,
    message: `Meshy image task ${task}: ${row.message} ${row.reference} Quote the credit cost for the chosen settings and ask for my approval before generating.`
  }))
}

/** The saved-files heading. Those files are on the backend's machine, which is this computer only for a
 *  local backend; an older Desktop that cannot say gets a heading that is true either way. */
export function localHeading(locality?: Locality): string {
  if (locality?.kind === 'local') return 'On this machine'
  if (locality?.kind === 'remote') return `On ${locality.label || 'the Hermes host'}`
  return 'Saved files'
}
