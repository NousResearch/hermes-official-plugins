import type { StorageStatus } from './gallery.ts'
/**
 * React surfaces over the three.js stage: an inline transcript card, the workspace gallery, and the
 * status-bar credit chip. Every byte comes from the plugin's own backend (`ctx.rest`) because the
 * renderer cannot read the backend machine's files or call Meshy directly.
 */

import type { PluginContext } from '@hermes/plugin-sdk'
import {
  Button,
  Codicon,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  host
} from '@hermes/plugin-sdk'
import { type CSSProperties, useCallback, useEffect, useRef, useState } from 'react'

import { createStage, dataUrlToBuffer, type ModelStats, type Stage } from './stage.ts'
import { TaskCard } from './task-card.tsx'
import {
  imageNextActions,
  matchesKind,
  reconcileSelection,
  sourceForAsset,
  sourceForLocalFile,
  type LocalFile,
  type RemoteAsset,
  type Source
} from './gallery.ts'
export type { LocalFile, RemoteAsset, Source } from './gallery.ts'

export type Rest = PluginContext['rest']
export type Os = PluginContext['os']

export interface FilePayload {
  name: string
  bytes: number
  dataUrl: string
}

export interface MeshySettings {
  show_credits: boolean
  inline_viewer: boolean
  auto_rotate: boolean
  gallery_limit: number
}

/** Mirrors plugin.json's config_schema defaults until the backend answers. */
export const DEFAULT_SETTINGS: MeshySettings = { show_credits: true, inline_viewer: true, auto_rotate: true, gallery_limit: 24 }

export interface Balance {
  configured: boolean
  balance?: number
  key_env?: string
  error?: string
  settings?: Partial<MeshySettings>
}

// Inline styles with theme variables: no injected <style> tag (nothing global to leak or clean up),
// and every surface reskins with the active theme.
export const V = {
  viewer: { position: 'relative', width: '100%', borderRadius: 8, overflow: 'hidden', background: 'var(--ui-bg-secondary, #1b1d22)', ['--meshy-stage-bg' as string]: 'var(--ui-bg-secondary, #1b1d22)' },
  stage: { position: 'absolute', inset: 0 },
  overlay: { position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8, color: 'var(--ui-text-secondary)', fontSize: 13, pointerEvents: 'none' },
  error: { color: 'var(--ui-danger, #e5484d)' },
  toolbar: { position: 'absolute', top: 6, right: 6, display: 'flex', gap: 2, padding: 2, borderRadius: 6, background: 'color-mix(in srgb, var(--ui-bg, #111) 70%, transparent)' },
  toolOn: { color: 'var(--ui-accent)' },
  scrub: { position: 'absolute', top: 44, right: 6, width: 'min(320px, calc(100% - 12px))', display: 'flex', alignItems: 'center', gap: 6, padding: '2px 8px', borderRadius: 6, fontSize: 11, color: '#e8e8ea', background: 'rgba(17, 18, 20, 0.62)', fontVariantNumeric: 'tabular-nums' },
  stats: { position: 'absolute', left: 6, bottom: 6, padding: '2px 6px', borderRadius: 4, fontSize: 11, color: '#e8e8ea', background: 'rgba(17, 18, 20, 0.62)', fontVariantNumeric: 'tabular-nums', pointerEvents: 'none' },
  card: { border: '1px solid var(--ui-stroke-secondary)', borderRadius: 10, padding: 8, margin: '6px 0', maxWidth: 640 },
  cardHead: { display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6, fontSize: 13, color: 'var(--ui-text-secondary)' },
  cardTitle: { fontWeight: 600, color: 'var(--ui-text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
  spacer: { flex: 1 },
  poster: { width: '100%', height: 160, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 6, borderRadius: 8, border: '1px dashed var(--ui-stroke-secondary)', background: 'transparent', color: 'var(--ui-text-secondary)', cursor: 'pointer', fontSize: 13 },
  gallery: { display: 'flex', height: '100%', minHeight: 0 },
  list: { width: 280, flexShrink: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden', borderRight: '1px solid var(--ui-stroke-secondary)', padding: 6 },
  listHead: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '2px 4px 6px', fontWeight: 600, fontSize: 13 },
  section: { padding: '8px 6px 4px', fontSize: 11, textTransform: 'uppercase', letterSpacing: 0.4, color: 'var(--ui-text-quaternary)' },
  hint: { padding: '6px', fontSize: 12, color: 'var(--ui-text-tertiary)' },
  item: { display: 'flex', alignItems: 'center', gap: 8, width: '100%', padding: 6, border: 0, borderRadius: 6, background: 'transparent', color: 'var(--ui-text)', textAlign: 'left', cursor: 'pointer', fontSize: 13 },
  itemOn: { background: 'var(--ui-bg-tertiary, rgba(127,127,127,.15))' },
  itemText: { display: 'flex', flexDirection: 'column', minWidth: 0, gap: 1 },
  itemSub: { fontSize: 11, color: 'var(--ui-text-tertiary)' },
  thumb: { width: 40, height: 40, objectFit: 'cover', borderRadius: 4, flexShrink: 0, background: 'var(--ui-bg-secondary)' },
  main: { flex: 1, minWidth: 0, padding: 8, display: 'flex', flexDirection: 'column', gap: 6 },
  mainHead: { display: 'flex', alignItems: 'center', gap: 6, fontSize: 13, color: 'var(--ui-text-secondary)', minHeight: 28 },
  empty: { flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 8, color: 'var(--ui-text-tertiary)' },
  search: { width: 'calc(100% - 8px)', margin: '0 4px 6px', padding: '4px 8px', borderRadius: 6, border: '1px solid var(--ui-stroke-secondary)', background: 'transparent', color: 'var(--ui-text)', fontSize: 12 },
  ellipsis: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', display: 'block' },
  taskLine: { fontSize: 13, color: 'var(--ui-text-secondary)', padding: '4px 2px' },
  taskRun: { display: 'flex', alignItems: 'center', gap: 10, padding: '6px 2px' },
  taskThumb: { width: 72, height: 72, objectFit: 'cover', borderRadius: 6, flexShrink: 0 },
  taskImage: { width: '100%', maxHeight: 320, objectFit: 'contain', borderRadius: 8 },
  bar: { height: 6, borderRadius: 3, background: 'var(--ui-bg-tertiary, rgba(127,127,127,.2))', overflow: 'hidden', marginTop: 4 },
  barFill: { height: '100%', background: 'var(--ui-accent)', transition: 'width .4s ease' },
  posterImage: { backgroundSize: 'cover', backgroundPosition: 'center', color: '#fff' },
  posterLabel: { padding: '2px 8px', borderRadius: 6, background: 'rgba(0,0,0,.55)' },
  madeFrom: { display: 'flex', flexDirection: 'column', gap: 6, padding: '8px 10px', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 8, fontSize: 12, color: 'var(--ui-text-secondary)', maxHeight: 220, overflowY: 'auto', flexShrink: 0 },
  madeHead: { display: 'flex', alignItems: 'center', gap: 6, minHeight: 24 },
  promptRow: { display: 'flex', alignItems: 'flex-start', gap: 4 },
  prompt: { flex: 1, color: 'var(--ui-text)', fontSize: 13, lineHeight: 1.4, whiteSpace: 'pre-wrap', userSelect: 'text' },
  sourceImg: { width: 56, height: 56, objectFit: 'cover', borderRadius: 6, flexShrink: 0 },
  chips: { display: 'flex', flexWrap: 'wrap', gap: 4, alignItems: 'center' },
  tag: { padding: '1px 6px', borderRadius: 4, fontSize: 11, background: 'var(--ui-bg-tertiary, rgba(127,127,127,.15))', color: 'var(--ui-text-secondary)' },
  tagOn: { padding: '1px 6px', borderRadius: 4, fontSize: 11, background: 'var(--ui-accent)', color: 'var(--ui-accent-foreground, #fff)' },
  link: { padding: '1px 6px', borderRadius: 4, fontSize: 11, border: '1px solid var(--ui-stroke-secondary)', background: 'transparent', color: 'var(--ui-text-secondary)', cursor: 'pointer' },
  cost: { fontSize: 11, color: 'var(--ui-text-tertiary)', fontVariantNumeric: 'tabular-nums', marginLeft: 12 },
  chip: { display: 'inline-flex', alignItems: 'center', gap: 4, border: 0, background: 'transparent', color: 'inherit', font: 'inherit', cursor: 'pointer', padding: '0 4px' }
} satisfies Record<string, CSSProperties>

const fmtBytes = (n: number) => (n > 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.round(n / 1024)} KB`)
/** Meshy/Blender clip names look like `Armature|running|baselayer`; show the action part. */
export const clipLabel = (name: string) => {
  const parts = name.split('|').filter(p => p && !/^(armature|baselayer|mixamo\.com)$/i.test(p))
  const word = (parts[0] ?? name).replace(/[_-]+/g, ' ').trim()
  return word ? word.charAt(0).toUpperCase() + word.slice(1) : name
}

const fmtCount = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(2)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : `${n}`)

/** `ctx.rest` rejects with `"<status>: <body>"`; show the backend's `detail` sentence, not raw JSON. */
export function errorText(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err)
  const match = /"detail"\s*:\s*"([^"]+)"/.exec(raw)
  return match ? match[1].charAt(0).toUpperCase() + match[1].slice(1) : raw
}

export function fetchSource(rest: Rest, source: Source): Promise<FilePayload> {
  return source.kind === 'file'
    ? rest<FilePayload>(`file?path=${encodeURIComponent(source.path)}${source.task ? `&task=${encodeURIComponent(source.task)}` : ''}`, { timeoutMs: 120_000 })
    : rest<FilePayload>(`remote?url=${encodeURIComponent(source.url)}`, { timeoutMs: 180_000 })
}

function ImageViewer({ rest, source, imageIndex, onImageIndexChange }: { rest: Rest; source: Source; imageIndex?: number; onImageIndexChange?: (index: number) => void }) {
  const images = source.images?.length ? source.images : [source.kind === 'file' ? source.path : source.url]
  const [index, setIndex] = useState(0)
  const [data, setData] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const current = Math.min(imageIndex ?? index, images.length - 1)
  const choose = (value: number) => {
    setIndex(value)
    onImageIndexChange?.(value)
  }
  const url = images[current]
  useEffect(() => {
    let live = true
    setData(null)
    setError(null)
    fetchSource(rest, source.kind === 'file' ? { ...source, path: url } : { ...source, url }).then(result => {
      if (live) setData(result.dataUrl)
    }).catch((err: unknown) => {
      if (live) setError(errorText(err))
    })
    return () => { live = false }
  }, [rest, url])
  return (
    <div data-meshy-image style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 12 }}>
      {error ? <div role="alert" style={V.error}>{error}</div> : data ? (
        <img alt={source.label || 'Saved image'} src={data} onError={() => setError('This image could not be decoded.')} style={{ maxWidth: '100%', maxHeight: 'calc(100% - 48px)', objectFit: 'contain' }} />
      ) : <span role="status">Loading image…</span>}
      {images.length > 1 && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <Button disabled={current === 0} onClick={() => choose(current - 1)} size="sm" variant="outline">Previous image</Button>
          <span>{current + 1} / {images.length}</span>
          <Button disabled={current === images.length - 1} onClick={() => choose(current + 1)} size="sm" variant="outline">Next image</Button>
        </div>
      )}
    </div>
  )
}

function AssetViewer(props: Parameters<typeof ModelViewer>[0] & { imageIndex?: number; onImageIndexChange?: (index: number) => void }) {
  const { source, rest } = props
  if (source.kind === 'remote' && source.expired) {
    return <div role="status" style={V.empty}>This asset’s download link has expired. Open a locally saved copy if you have one.</div>
  }
  return source.media === 'image'
    ? <ImageViewer key={source.kind === 'file' ? source.path : source.url} rest={rest} source={source} imageIndex={props.imageIndex} onImageIndexChange={props.onImageIndexChange} />
    : <ModelViewer {...props} />
}

function AssetThumbnail({ asset }: { asset: RemoteAsset }) {
  const [failed, setFailed] = useState(false)
  useEffect(() => setFailed(false), [asset.thumbnail_url])
  return asset.thumbnail_url && !asset.expired && !failed
    ? <img alt="" style={V.thumb} src={asset.thumbnail_url} onError={() => setFailed(true)} />
    : <Codicon name={asset.kind === 'image' ? 'file-media' : 'symbol-misc'} />
}

function ImageNext({ task, source, imageIndex }: { task: string; source: Source; imageIndex: number }) {
  return <DropdownMenu>
    <DropdownMenuTrigger asChild><Button data-meshy-image-next size="sm" variant="ghost"><Codicon name="sparkle" /> Next <Codicon name="chevron-down" size={12} /></Button></DropdownMenuTrigger>
    <DropdownMenuContent align="end" style={{ minWidth: 250 }}>
      {imageNextActions(task, source, imageIndex).map(row => <DropdownMenuItem key={row.id} onSelect={() => {
        if (!host.composer?.submit(null, row.message)) host.notify({ kind: 'warning', message: 'Open a chat to work with this image' })
      }}>{row.label}</DropdownMenuItem>)}
      <DropdownMenuSeparator /><DropdownMenuItem disabled>Choose options and approve credits in chat</DropdownMenuItem>
    </DropdownMenuContent>
  </DropdownMenu>
}

async function copyTaskId(task: string) {
  try {
    if (!navigator.clipboard) throw new Error('Clipboard unavailable')
    await navigator.clipboard.writeText(task)
    host.notify({ kind: 'success', message: 'Task ID copied' })
  } catch {
    host.notify({ kind: 'error', message: 'Could not copy. Select the task ID and copy it manually.' })
  }
}

function ImageDownload({ rest, os, task, source, onSaved }: { rest: Rest; os: Os; task: string; source: Source; onSaved: () => void }) {
  const [busy, setBusy] = useState(false)
  const count = source.images?.length || 1
  const download = async () => {
    if (source.kind === 'file' && !source.incomplete) {
      void os.revealPath(source.path)
      return
    }
    setBusy(true)
    try {
      const saved = await rest<{ paths: string[]; directory: string }>('save-images', { method: 'POST', body: { task }, timeoutMs: 600_000 })
      host.notify({ kind: 'success', message: `Saved ${saved.paths.length} image${saved.paths.length === 1 ? '' : 's'}` })
      void os.revealPath(saved.paths.length === 1 ? saved.paths[0] : saved.directory)
    } catch (err) {
      host.notify({ kind: 'error', message: `Image download: ${errorText(err)}` })
    } finally {
      setBusy(false)
      onSaved()
    }
  }
  return <Button disabled={busy || (source.kind === 'remote' && source.expired)} onClick={() => void download()} size="sm" variant="outline">
    <Codicon name={busy ? 'loading' : source.kind === 'file' ? 'folder-opened' : 'cloud-download'} spinning={busy} />
    {busy ? 'Saving images…' : source.kind === 'file' ? source.incomplete ? 'Retry image download' : 'Show saved images' : count > 1 ? `Download all ${count} images` : 'Download image'}
  </Button>
}

/** One live viewport. Mounting creates a WebGL context; unmounting frees it. */
export function ModelViewer({
  rest,
  os,
  source,
  height,
  compact,
  autoRotate = true
}: {
  rest: Rest
  os: Os
  source: Source
  height: number | string
  compact?: boolean
  autoRotate?: boolean
}) {
  const hostRef = useRef<HTMLDivElement>(null)
  const stageRef = useRef<Stage | null>(null)
  const [state, setState] = useState<{ phase: 'error' | 'loading' | 'ready'; message?: string }>({ phase: 'loading' })
  const [stats, setStats] = useState<ModelStats | null>(null)
  const [file, setFile] = useState<{ name: string; bytes: number } | null>(null)
  const [spin, setSpin] = useState(autoRotate)
  const [wire, setWire] = useState(false)
  const [grid, setGrid] = useState(true)
  const [clip, setClip] = useState(0)
  const [playing, setPlaying] = useState(true)
  const [loop, setLoop] = useState(true)
  const [clipTime, setClipTime] = useState({ time: 0, duration: 0 })
  const key = source.kind === 'file' ? source.path : source.url

  useEffect(() => {
    const el = hostRef.current
    if (!el) {
      return
    }
    const bg = getComputedStyle(el).getPropertyValue('--meshy-stage-bg').trim() || '#1b1d22'
    const stage = createStage(el, bg)
    stageRef.current = stage
    // A new model gets a new stage: carry the toolbar's current view settings over to it.
    stage.setAutoRotate(spin)
    stage.setGrid(grid)
    let live = true
    setState({ phase: 'loading' })
    setStats(null)
    setClip(0)
    setPlaying(true)
    fetchSource(rest, source)
      .then(async payload => {
        if (!live) {
          return
        }
        setFile({ name: payload.name, bytes: payload.bytes })
        const s = await stage.load(dataUrlToBuffer(payload.dataUrl))
        if (live) {
          setStats(s)
          setState({ phase: 'ready' })
        }
      })
      .catch((err: unknown) => {
        if (live) {
          setState({ phase: 'error', message: errorText(err) })
        }
      })
    return () => {
      live = false
      stageRef.current = null
      stage.dispose()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  useEffect(() => stageRef.current?.setAutoRotate(spin), [spin])
  useEffect(() => stageRef.current?.setWireframe(wire), [wire, stats])
  useEffect(() => stageRef.current?.setGrid(grid), [grid])
  const clips = stats?.animations ?? []
  useEffect(() => stageRef.current?.setLoop(loop), [loop, stats])

  // Mirror the clip clock into the scrub bar (~8 Hz is smooth enough for a slider and cheap); also
  // picks up a play-once clip finishing on its own.
  useEffect(() => {
    if (!clips.length) {
      return
    }
    const id = window.setInterval(() => {
      const s = stageRef.current?.clipState()
      if (s) {
        setClipTime(prev => (Math.abs(prev.time - s.time) < 0.01 && prev.duration === s.duration ? prev : { time: s.time, duration: s.duration }))
        setPlaying(prev => (prev === s.playing ? prev : s.playing))
      }
    }, 120)
    return () => window.clearInterval(id)
  }, [clips.length, stats])

  const pickClip = (index: number) => {
    stageRef.current?.playClip(index)
    setClip(index)
    setPlaying(true)
  }

  const flipPlaying = () => {
    stageRef.current?.setPlaying(!playing)
    setPlaying(!playing)
  }

  const toggle = (on: boolean, set: (v: boolean) => void, icon: string, label: string) => (
    <Button
      aria-label={label}
      aria-pressed={on}
      style={on ? V.toolOn : undefined}
      onClick={() => set(!on)}
      size="icon"
      title={label}
      variant="ghost"
    >
      <Codicon name={icon} />
    </Button>
  )

  return (
    <div data-meshy-phase={state.phase} style={{ ...V.viewer, height }}>
      <div ref={hostRef} style={V.stage} />
      {state.phase === 'loading' && (
        <div style={V.overlay}>
          <Codicon name="loading" spinning /> Loading {source.kind === 'remote' ? source.label : 'model'}…
        </div>
      )}
      {state.phase === 'error' && (
        <div style={{ ...V.overlay, ...V.error }}>
          <Codicon name="error" /> {state.message}
        </div>
      )}
      <div style={V.toolbar}>
        {clips.length > 0 && (
          <Button
            aria-label={playing ? 'Pause animation' : 'Play animation'}
            aria-pressed={playing}
            data-meshy-play={playing ? 'playing' : 'paused'}
            onClick={flipPlaying}
            size="icon"
            style={playing ? V.toolOn : undefined}
            title={playing ? 'Pause animation' : 'Play animation'}
            variant="ghost"
          >
            <Codicon name={playing ? 'debug-pause' : 'play'} />
          </Button>
        )}
        {clips.length > 0 && toggle(loop, setLoop, 'debug-restart', loop ? 'Loop (on)' : 'Loop (off: play once)')}
        {clips.length > 1 && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button data-meshy-clips size="sm" title="Animation clip" variant="ghost">
                <span style={{ maxWidth: 110, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {clipLabel(clips[clip] ?? '')}
                </span>
                <Codicon name="chevron-down" size={12} />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" style={{ minWidth: 160 }}>
              {clips.map((name, i) => (
                <DropdownMenuItem data-meshy-clip={i} key={`${i}-${name}`} onSelect={() => pickClip(i)} title={name}>
                  <span style={{ flex: 1 }}>{clipLabel(name)}</span>
                  {i === clip && <Codicon name="check" />}
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
        {toggle(spin, setSpin, 'sync', 'Auto-rotate')}
        {toggle(wire, setWire, 'symbol-structure', 'Wireframe')}
        {toggle(grid, setGrid, 'symbol-numeric', 'Grid')}
        <Button aria-label="Reset view" onClick={() => stageRef.current?.resetView()} size="icon" title="Reset view" variant="ghost">
          <Codicon name="screen-normal" />
        </Button>
        {source.kind === 'file' && (
          <Button
            aria-label="Show in folder"
            onClick={() => void os.revealPath(source.path)}
            size="icon"
            title="Show in folder"
            variant="ghost"
          >
            <Codicon name="folder-opened" />
          </Button>
        )}
      </div>
      {clips.length > 0 && clipTime.duration > 0 && (
        <div style={V.scrub}>
          <input
            aria-label="Animation position"
            data-meshy-scrub
            max={clipTime.duration}
            min={0}
            onChange={e => {
              const t = Number(e.currentTarget.value)
              stageRef.current?.seek(t)
              setClipTime(prev => ({ ...prev, time: t }))
            }}
            step={0.01}
            style={{ flex: 1, accentColor: 'var(--ui-accent)', cursor: 'pointer' }}
            type="range"
            value={Math.min(clipTime.time, clipTime.duration)}
          />
          <span style={{ minWidth: 70, textAlign: 'right' }}>
            {clipTime.time.toFixed(1)} / {clipTime.duration.toFixed(1)} s
          </span>
        </div>
      )}
      {stats && (
        <div data-meshy-stats style={V.stats}>
          {file ? `${file.name} · ${fmtBytes(file.bytes)} · ` : ''}
          {fmtCount(stats.triangles)} tris · {fmtCount(stats.vertices)} verts
          {clips.length ? ` · ${clips.length} clip${clips.length === 1 ? '' : 's'}` : ''}
          {compact ? '' : ` · ${stats.meshes} mesh${stats.meshes === 1 ? '' : 'es'} · ${stats.materials} material${stats.materials === 1 ? '' : 's'}`}
        </div>
      )}
    </div>
  )
}

/** Labels for the download menu, in the order Meshy's API lists them. */
const FORMAT_LABEL: Record<string, string> = {
  glb: 'GLB · web, Blender, three.js',
  fbx: 'FBX · Unity, Unreal, Maya',
  obj: 'OBJ · with .mtl + texture',
  stl: 'STL · 3D printing',
  usdz: 'USDZ · Apple AR Quick Look',
  '3mf': '3MF · 3D printing',
  blend: 'BLEND · Blender project'
}

/** `glb` → its label; `glb:walking` (a rig's extra clip file) → `GLB · walking`. */
const formatLabel = (format: string) => {
  const [base, clip] = format.split(':')
  return clip ? `${base.toUpperCase()} · ${clip.replace(/_/g, ' ')}` : (FORMAT_LABEL[base] ?? base.toUpperCase())
}

interface FormatRow {
  format: string
  saved: null | string
}

/** "Download ▾": one row per file type Meshy produced for this task. Picking one saves it beside the
 *  model on screen (no credits: Meshy made every listed format at generation time) and reveals it. */
export function FormatMenu({ rest, os, task, beside, onSaved }: { rest: Rest; os: Os; task: string; beside?: string; onSaved?: () => void }) {
  const [rows, setRows] = useState<FormatRow[] | null>(null)
  const [conversions, setConversions] = useState<{ format: string; cost: number; message: string }[]>([])
  const [isPreview, setIsPreview] = useState(false)
  const [error, setError] = useState<null | string>(null)
  const [busy, setBusy] = useState<null | string>(null)
  const query = `formats?task=${encodeURIComponent(task)}${beside ? `&beside=${encodeURIComponent(beside)}` : ''}`

  const load = useCallback(() => {
    setError(null)
    rest<{ formats: FormatRow[]; conversions?: { format: string; cost: number; message: string }[]; is_preview?: boolean }>(query, { timeoutMs: 30_000 })
      .then(r => {
        setRows(r.formats)
        setConversions(r.conversions || [])
        setIsPreview(!!r.is_preview)
      })
      .catch((err: unknown) => setError(errorText(err)))
  }, [rest, query])

  const pick = (fmt: string, saved: null | string) => {
    if (saved) {
      void os.revealPath(saved)
      return
    }
    setBusy(fmt)
    rest<{ path: string }>('save', { method: 'POST', body: { task, format: fmt, beside }, timeoutMs: 600_000 })
      .then(r => {
        setRows(prev => prev?.map(row => (row.format === fmt ? { ...row, saved: r.path } : row)) ?? prev)
        host.notify({ kind: 'success', message: `Saved ${r.path.split(/[\\/]/).pop()}` })
        void os.revealPath(r.path)
        onSaved?.()
      })
      .catch((err: unknown) => host.notify({ kind: 'error', message: `Meshy ${fmt.toUpperCase()}: ${errorText(err)}` }))
      .finally(() => setBusy(null))
  }

  return (
    <DropdownMenu onOpenChange={open => open && load()}>
      <DropdownMenuTrigger asChild>
        <Button data-meshy-formats size="sm" variant="ghost">
          <Codicon name={busy ? 'loading' : 'cloud-download'} spinning={!!busy} /> {busy ? `Saving ${busy.toUpperCase()}…` : 'Download'}
          <Codicon name="chevron-down" size={12} />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" style={{ minWidth: 260 }}>
        {error ? (
          <DropdownMenuItem disabled>{error}</DropdownMenuItem>
        ) : rows === null ? (
          <DropdownMenuItem disabled>
            <Codicon name="loading" spinning /> Checking formats…
          </DropdownMenuItem>
        ) : (
          rows.map(({ format, saved }) => (
            <DropdownMenuItem
              data-meshy-format={format}
              disabled={busy !== null}
              key={format}
              onSelect={() => pick(format, saved)}
              title={saved ?? undefined}
            >
              <span style={{ flex: 1 }}>{formatLabel(format)}</span>
              <Codicon name={saved ? 'check' : 'cloud-download'} style={{ opacity: saved ? 1 : 0.55 }} />
            </DropdownMenuItem>
          ))
        )}
        {rows !== null && rows.length === 1 && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem disabled>{isPreview ? 'Untextured preview: refine it to add textures' : 'This task returned only one download format'}</DropdownMenuItem>
          </>
        )}
        {rows !== null && !error && conversions.length > 0 && <>
          <DropdownMenuSeparator />
          {conversions.map(row => <DropdownMenuItem key={`convert:${row.format}`} disabled={busy !== null} onSelect={() => {
            if (!host.composer?.submit(null, row.message)) host.notify({ kind: 'warning', message: 'Open a chat to convert this model' })
          }}><span style={{ flex: 1 }}>Convert to {row.format.toUpperCase()}</span><span style={V.cost}>{row.cost} cr</span></DropdownMenuItem>)}
          <DropdownMenuItem disabled>Conversion creates a new task · sends a chat request</DropdownMenuItem>
        </>}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

interface ActionRow {
  id: string
  label: string
  cost: number
  affordable: boolean
  covers: null | number
  message: string
}
interface ActionsPayload {
  type: string
  spent: null | number
  balance: null | number
  actions: ActionRow[]
}

const ACTION_ICONS: Record<string, string> = {
  texture: 'paintcan',
  regenerate: 'refresh',
  remesh: 'type-hierarchy',
  retexture: 'symbol-color',
  rig: 'person',
  animate: 'play',
  resize: 'screen-full',
  printability: 'check-all'
}

/** "Next ▾": the follow-ups Meshy supports for this task, each labelled with its real credit cost. A pick
 *  SENDS a chat message: the agent runs the tool behind Hermes' approval card, so nothing here spends. */
export function ActionMenu({ rest, task }: { rest: Rest; task: string }) {
  const [data, setData] = useState<ActionsPayload | null>(null)
  const [error, setError] = useState<null | string>(null)

  const load = useCallback(() => {
    setError(null)
    rest<ActionsPayload>(`actions?task=${encodeURIComponent(task)}`, { timeoutMs: 30_000 })
      .then(setData)
      .catch((err: unknown) => setError(errorText(err)))
  }, [rest, task])

  const run = (row: ActionRow) => {
    if (!host.composer?.submit(null, row.message)) {
      host.notify({ kind: 'warning', message: `Open a chat to ${row.label.toLowerCase()} this model` })
    }
  }

  return (
    <DropdownMenu onOpenChange={open => open && load()}>
      <DropdownMenuTrigger asChild>
        <Button data-meshy-actions size="sm" variant="ghost">
          <Codicon name="sparkle" /> Next
          <Codicon name="chevron-down" size={12} />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" style={{ minWidth: 290 }}>
        {error ? (
          <DropdownMenuItem disabled>{error}</DropdownMenuItem>
        ) : data === null ? (
          <DropdownMenuItem disabled>
            <Codicon name="loading" spinning /> Checking what this model supports…
          </DropdownMenuItem>
        ) : (
          <>
            {data.actions.map(row => (
              <DropdownMenuItem
                data-meshy-action={row.id}
                disabled={!row.affordable}
                key={row.id}
                onSelect={() => run(row)}
                title={row.affordable ? row.message : `Needs ${row.cost} credits`}
              >
                <Codicon name={ACTION_ICONS[row.id] ?? 'circle-small'} />
                <span style={{ flex: 1 }}>{row.label}</span>
                <span style={V.cost}>{row.cost ? `${row.cost} cr` : 'free'}</span>
              </DropdownMenuItem>
            ))}
            {data.actions.length === 0 && <DropdownMenuItem disabled>No follow-ups for this kind of task</DropdownMenuItem>}
            <DropdownMenuSeparator />
            <DropdownMenuItem disabled>
              {[
                typeof data.spent === 'number' ? `This model cost ${data.spent} cr` : null,
                typeof data.balance === 'number' ? `balance ${data.balance.toLocaleString()} cr` : null
              ]
                .filter(Boolean)
                .join(' · ') || 'Credits unavailable'}
            </DropdownMenuItem>
            <DropdownMenuItem disabled>Sends a message; Hermes asks before any credits are spent</DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/** Transcript directive card: `::meshy-model{task="<id>"}` (live progress, then the model), `{path="/abs/model.glb"}`
 *  or `{url="https://assets.meshy.ai/..."}`.
 *  Shows a click-to-load poster first so a long transcript never opens N WebGL contexts at once. */
export function TranscriptCard({
  rest,
  os,
  attrs,
  streaming,
  openInWorkspace
}: {
  rest: Rest
  os: Os
  attrs: Readonly<Record<string, string>>
  streaming: boolean
  openInWorkspace: (source: Source, title: string) => void
}) {
  const [live, setLive] = useState(false)
  const path = (attrs.path || attrs.file || '').trim()
  const url = (attrs.url || '').trim()
  const title = attrs.title || (path ? path.split(/[\\/]/).pop() : 'Meshy model') || 'Meshy model'
  const task = (attrs.task || '').trim()
  const source: null | Source = path
    ? { kind: 'file', path, ...(task ? { task } : {}) }
    : url
      ? { kind: 'remote', url, label: title, ...(task ? { task } : {}) }
      : null

  if (!source && task) {
    return <TaskCard openInWorkspace={openInWorkspace} os={os} rest={rest} streaming={streaming} task={task} title={attrs.title} />
  }
  if (!source) {
    return <div style={{ ...V.card, ...V.error }}>Meshy model: give a task="...", path="..." or url="..."</div>
  }

  return (
    <div data-meshy-card style={V.card}>
      <div style={V.cardHead}>
        <Codicon name="symbol-misc" />
        <span style={V.cardTitle}>{title}</span>
        <span style={V.spacer} />
        {task && !streaming && <ActionMenu rest={rest} task={task} />}
        {task && !streaming && <FormatMenu beside={path || undefined} os={os} rest={rest} task={task} />}
        <Button onClick={() => openInWorkspace(source, title)} size="sm" variant="ghost">
          <Codicon name="link-external" /> Open
        </Button>
      </div>
      {live ? (
        <ModelViewer autoRotate={false} compact height={320} os={os} rest={rest} source={source} />
      ) : (
        <button
          style={V.poster}
          disabled={streaming}
          onClick={() => setLive(true)}
          type="button"
        >
          <Codicon name="play-circle" size={28} />
          <span>{streaming ? 'Waiting for the reply to finish…' : 'View in 3D'}</span>
        </button>
      )}
    </div>
  )
}

interface Details {
  task: string
  type: string
  prompt: null | string
  prompt_from: null | string
  texture_prompt: null | string
  style_prompt: null | string
  negative_prompt?: null | string
  image: null | string
  image_viewable: boolean
  settings: Record<string, unknown>
  seed?: null | number
  credits: null | number
  created_at: null | number
  parents: Array<{ task_id: string; tool: null | string }>
  children: Array<{ task_id: string; tool: null | string }>
  session_id: null | string
  recorded: boolean
}

const STEP: Record<string, string> = {
  meshy_text_to_3d: 'preview', 'text-to-3d': 'preview', meshy_text_to_3d_refine: 'textured', meshy_image_to_3d: 'from image',
  meshy_multi_image_to_3d: 'from images', meshy_remesh: 'remesh', remesh: 'remesh', meshy_retexture: 'retexture',
  meshy_rig: 'rig', meshy_animate: 'animation', meshy_resize: 'resize'
}
const shortId = (id: string) => id.slice(0, 8)

/** "Made from": the prompt, style, source image, settings and lineage behind the selected model. */
export function MadeFrom({ rest, task, onPick }: { rest: Rest; task: string; onPick: (task: string) => void }) {
  const [d, setD] = useState<Details | null>(null)
  const [img, setImg] = useState<null | string>(null)
  const [error, setError] = useState<null | string>(null)

  useEffect(() => {
    let live = true
    setD(null)
    setImg(null)
    setError(null)
    rest<Details>(`details?task=${encodeURIComponent(task)}`, { timeoutMs: 30_000 })
      .then(r => {
        if (!live) {
          return
        }
        setD(r)
        if (r.image && r.image_viewable) {
          rest<FilePayload>(`file?path=${encodeURIComponent(r.image)}`, { timeoutMs: 30_000 })
            .then(f => live && setImg(f.dataUrl))
            .catch(() => undefined)
        }
      })
      .catch((e: unknown) => live && setError(errorText(e)))
    return () => {
      live = false
    }
  }, [rest, task])

  if (error) {
    return <div style={{ ...V.madeFrom, ...V.itemSub }}>Couldn't load what made this model: {error}</div>
  }
  if (!d) {
    return <div style={{ ...V.madeFrom, ...V.itemSub }}>Loading what made this model…</div>
  }
  const copy = (text: string) => {
    void navigator.clipboard?.writeText(text).then(
      () => host.notify({ kind: 'success', message: 'Prompt copied' }),
      () => host.notify({ kind: 'error', message: 'Could not copy the prompt' })
    )
  }
  const reuse = (text: string) => {
    void host.composer?.insertText?.(null, text, { mode: 'block' }).then(ok => {
      if (!ok) {
        host.notify({ kind: 'warning', message: 'Open a chat to reuse this prompt' })
      }
    })
  }
  const settings = Object.entries(d.settings).filter(([, v]) => v !== '' && v !== null && v !== undefined)
  // The headline is whatever text described it: the prompt, else (image models) the texture prompt.
  const main = d.prompt || d.texture_prompt
  const lines: Array<[string, string]> = [
    ...(d.style_prompt ? [['Style', d.style_prompt] as [string, string]] : []),
    ...(d.prompt && d.texture_prompt && d.texture_prompt !== d.prompt ? [['Texture', d.texture_prompt] as [string, string]] : []),
    ...(d.negative_prompt ? [['Avoid', d.negative_prompt] as [string, string]] : [])
  ]

  return (
    <div data-meshy-made-from style={V.madeFrom}>
      <div style={V.madeHead}>
        <span style={{ fontWeight: 600, color: 'var(--ui-text)' }}>Made from</span>
        <span style={V.spacer} />
        {d.session_id && (
          <Button data-meshy-open-chat onClick={() => void host.openSession?.(d.session_id as string)} size="sm" variant="ghost">
            <Codicon name="comment-discussion" /> Open chat
          </Button>
        )}
      </div>
      {main ? (
        <div style={V.promptRow}>
          <div data-meshy-prompt style={V.prompt}>
            {main}
            {d.prompt_from && <span style={V.itemSub}> (from the original {shortId(d.prompt_from)})</span>}
          </div>
          <Button aria-label="Copy prompt" onClick={() => copy(main)} size="icon" title="Copy prompt" variant="ghost">
            <Codicon name="copy" />
          </Button>
          <Button aria-label="Reuse prompt" onClick={() => reuse(main)} size="icon" title="Put this prompt in the chat box" variant="ghost">
            <Codicon name="reply" />
          </Button>
        </div>
      ) : !d.image ? (
        <div style={V.itemSub}>
          {d.recorded ? 'No prompt for this step.' : 'Made outside this Desktop (or before v0.4), so its prompt was not recorded.'}
        </div>
      ) : null}
      {d.image && (
        <div style={V.promptRow}>
          {img ? <img alt="Source image" data-meshy-source-image src={img} style={V.sourceImg} /> : <Codicon name="file-media" />}
          <span style={{ ...V.itemSub, overflow: 'hidden', textOverflow: 'ellipsis' }} title={d.image}>
            Source image: {d.image.split(/[\\/]/).pop()}
          </span>
        </div>
      )}
      {lines.map(([k, t]) => (
        <div key={k} style={V.itemSub}>
          <b>{k}:</b> {t}
        </div>
      ))}
      <div style={V.chips}>
        {d.type && <span style={V.tag}>{STEP[d.type] ?? d.type}</span>}
        {settings.map(([k, val]) => (
          <span key={k} style={V.tag}>
            {k.replace(/_/g, ' ')}: {Array.isArray(val) ? val.join(', ') : String(val)}
          </span>
        ))}
        {typeof d.seed === 'number' && <span style={V.tag}>seed {d.seed}</span>}
        {typeof d.credits === 'number' && <span style={V.tag}>{d.credits} cr</span>}
        {d.created_at && <span style={V.tag}>{new Date(d.created_at).toLocaleString()}</span>}
      </div>
      {(d.parents.length > 0 || d.children.length > 0) && (
        <div data-meshy-lineage style={V.chips}>
          {d.parents.map(p => (
            <button key={p.task_id} onClick={() => onPick(p.task_id)} style={V.link} title={p.task_id} type="button">
              {STEP[p.tool ?? ''] ?? p.tool ?? 'task'} {shortId(p.task_id)}
            </button>
          ))}
          <span style={V.tagOn}>this model</span>
          {d.children.map(c => (
            <button key={c.task_id} onClick={() => onPick(c.task_id)} style={V.link} title={c.task_id} type="button">
              → {STEP[c.tool ?? ''] ?? c.tool ?? 'task'} {shortId(c.task_id)}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

/** Full workspace: recent Meshy generations + local downloads on the left, the viewer on the right. */
export function Gallery({
  rest,
  os,
  initial,
  autoRotate,
  onOpenSettings,
}: {
  rest: Rest
  os: Os
  initial: null | Source
  autoRotate: boolean
  onOpenSettings: () => void
}) {
  const [selected, setSelected] = useState<null | Source>(initial)
  const explicitSelection = useRef(Boolean(initial))
  const [imageIndex, setImageIndex] = useState(0)
  const selectedKey = selected?.kind === 'file' ? selected.path : selected?.url
  useEffect(() => setImageIndex(0), [selectedKey])
  // The Meshy task behind the selection (a generation's id, or a local file's recorded id): drives Download.
  const [selTask, setSelTask] = useState<null | { task: string; title: string }>(null)
  const [remote, setRemote] = useState<RemoteAsset[] | null>(null)
  const [local, setLocal] = useState<LocalFile[] | null>(null)
  const [storage, setStorage] = useState<StorageStatus | null>(null)
  const [error, setError] = useState<null | string>(null)
  const [configured, setConfigured] = useState(true)
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<'all' | 'image' | 'model'>('all')
  const q = query.trim().toLowerCase()
  const matches = (...texts: Array<null | string | undefined>) => !q || texts.some(t => (t ?? '').toLowerCase().includes(q))

  const refresh = useCallback(() => {
    setError(null)
    rest<{ configured: boolean; models: RemoteAsset[] }>('models', { timeoutMs: 60_000 })
      .then(r => {
        setConfigured(r.configured)
        setRemote(r.models)
      })
      .catch((e: unknown) => setError(errorText(e)))
    rest<StorageStatus>('storage').then(setStorage).catch(() => setStorage(null))
    rest<{ files: LocalFile[] }>('local')
      .then(r => setLocal(r.files))
      .catch(() => setLocal([]))
  }, [rest])

  useEffect(refresh, [refresh])
  useEffect(() => {
    if (initial) {
      explicitSelection.current = true
      setSelected(initial)
      setSelTask(
        initial.task
          ? { task: initial.task, title: initial.kind === 'file' ? initial.path.split(/[\\/]/).pop() || '' : initial.label }
          : null
      )
    }
  }, [initial])

  useEffect(() => {
    if (!selTask || !remote) return
    const saved = (local || []).map(sourceForLocalFile)
    if (explicitSelection.current) {
      // The initial-source effect may have queued a new clip in this same effect flush.
      setSelected(current => reconcileSelection(current, current?.task || selTask.task, true, remote, saved))
      return
    }
    const next = reconcileSelection(selected, selTask.task, false, remote, saved)
    setSelected(next)
    if (!next) setSelTask(null)
  }, [remote, local, selTask, initial])

  const pickTask = (task: string) => {
    const f = local?.find(x => x.task === task)
    if (f) {
      explicitSelection.current = false
      setSelected(sourceForLocalFile(f))
      setSelTask({ task, title: f.name })
      return
    }
    const r = remote?.find(m => m.task_id === task)
    const source = r && sourceForAsset(r)
    if (source) {
      explicitSelection.current = false
      setSelected(source)
      setSelTask({ task, title: r.prompt || r.task_type })
      return
    }
    host.notify({ kind: 'info', message: `Task ${task.slice(0, 8)} isn't in the gallery list (older than the list, or never downloaded)` })
  }

  const isSel = (s: Source) =>
    selected && selected.kind === s.kind && (s.kind === 'file' ? selected.kind === 'file' && selected.path === s.path : selected.kind === 'remote' && selected.url === s.url)

  return (
    <div data-meshy-gallery style={V.gallery}>
      <aside style={V.list}>
        <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
          <div style={V.listHead}>
            <span>Meshy assets</span>
            <span>
              <Button aria-label="Meshy settings" onClick={onOpenSettings} size="icon" title="Meshy settings" variant="ghost">
                <Codicon name="settings-gear" />
              </Button>
              <Button aria-label="Refresh" onClick={refresh} size="icon" title="Refresh" variant="ghost">
                <Codicon name="refresh" />
              </Button>
            </span>
          </div>
          <input
            aria-label="Search prompts"
            data-meshy-search
            onChange={e => setQuery(e.target.value)}
            placeholder="Search prompts…"
            style={V.search}
            type="search"
            value={query}
          />
          <div role="group" aria-label="Asset type" style={{ display: 'flex', gap: 4, padding: '6px 0' }}>
            {(['all', 'image', 'model'] as const).map(kind => (
              <Button key={kind} aria-pressed={filter === kind} onClick={() => setFilter(kind)} size="sm" variant={filter === kind ? 'outline' : 'ghost'}>
                {kind === 'all' ? 'All' : kind === 'image' ? 'Images' : 'Models'}
              </Button>
            ))}
          </div>
          {!configured && (
            <div style={V.hint}>
              Add your Meshy API key to list your generations.{' '}
              <Button onClick={onOpenSettings} size="sm" variant="outline">
                Open Meshy settings
              </Button>
            </div>
          )}
          {error && <div style={{ ...V.hint, ...V.error }}>{error}</div>}
          {local && local.some(f => matchesKind(filter, f.kind || 'model')) && <div style={V.section}>On this machine</div>}
          {local?.filter(f => matchesKind(filter, f.kind || 'model') && matches(f.prompt, f.name, f.project)).map(f => {
            const s = sourceForLocalFile(f)
            const pickLocal = () => {
              explicitSelection.current = true
              setSelected(s)
              setSelTask(f.task ? { task: f.task, title: f.name } : null)
            }
            return (
              <button data-meshy-item style={isSel(s) ? { ...V.item, ...V.itemOn } : V.item} key={f.path} onClick={pickLocal} title={f.path} type="button">
                <Codicon name="file-binary" />
                <span style={V.itemText}>
                  <span style={V.ellipsis}>{f.prompt || f.name}</span>
                  <small style={{ ...V.itemSub, ...V.ellipsis }}>
                    {f.prompt ? `${f.name} · ` : ''}
                    {f.project} · {fmtBytes(f.bytes)}
                  </small>
                </span>
              </button>
            )
          })}
          {remote && remote.length > 0 && <div style={V.section}>Recent generations</div>}
          {remote === null && configured && !error && <div style={V.hint}>Loading…</div>}
          {remote?.filter(m => !local?.some(f => f.task === m.task_id) && matchesKind(filter, m.kind) && matches(m.prompt, m.task_type)).map(m => {
            const s = sourceForAsset(m)
            return (
              <button
                data-meshy-item style={s && isSel(s) ? { ...V.item, ...V.itemOn } : V.item}
                disabled={!s}
                key={m.task_id}
                onClick={() => {
                  if (s) {
                    explicitSelection.current = false
                    setSelected(s)
                    setSelTask({ task: m.task_id, title: m.prompt || m.task_type })
                  }
                }}
                title={m.prompt ? `${m.prompt}\n${m.task_id}` : m.task_id}
                type="button"
              >
                <AssetThumbnail asset={m} />
                <span style={V.itemText}>
                  <span style={V.ellipsis}>{m.prompt || m.task_type}</span>
                  <small style={V.itemSub}>
                    {m.task_type}
                    {typeof m.credits === 'number' ? ` · ${m.credits} credits` : ''} · {new Date(m.created_at).toLocaleDateString()}
                    {m.expired ? m.local_path ? ' · Local copy' : ' · Expired' : ''}
                  </small>
                </span>
              </button>
            )
          })}
        </div>
        <div role="note" style={{ ...V.hint, borderTop: '1px solid var(--ui-stroke-secondary)', marginTop: 6 }}>
          {storage && <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span title="Includes Meshy downloads and known project copies">{fmtBytes(storage.bytes)} · {storage.files} local files</span>
            <Button onClick={() => void os.revealPath(storage.directory)} size="sm" variant="ghost">Show folder</Button>
          </div>}
          <div title="Viewing does not save an asset. Use Download or ask Hermes to save it. Expired entries without local copies are hidden.">Download to keep assets · API links normally expire after 3 days.</div>
        </div>
      </aside>
      <section style={V.main}>
        {selected && (
          <div data-meshy-gallery-head style={V.mainHead}>
            <span style={V.cardTitle}>{selected.kind === 'file' ? selected.path.split(/[\\/]/).pop() : selected.label}</span>
            <span style={V.spacer} />
            {selTask && selected.media === 'image' && <ImageNext task={selTask.task} source={selected} imageIndex={imageIndex} />}
            {selTask && selected.media === 'image' && <ImageDownload key={`images:${selTask.task}`} rest={rest} os={os} task={selTask.task} source={selected} onSaved={refresh} />}
            {selTask && !(selected.media === 'image' || (selected.kind === 'remote' && selected.expired)) && <ActionMenu key={`a:${selTask.task}`} rest={rest} task={selTask.task} />}
            {selTask && !(selected.media === 'image' || (selected.kind === 'remote' && selected.expired)) && (
              <FormatMenu
                onSaved={refresh}
                beside={selected.kind === 'file' ? selected.path : undefined}
                key={`${selTask.task}:${selected.kind === 'file' ? selected.path : ''}`}
                os={os}
                rest={rest}
                task={selTask.task}
              />
            )}
          </div>
        )}
        {selected && selTask && <div data-meshy-task-id style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8, padding: '0 12px 8px', fontSize: 12, color: 'var(--ui-text-tertiary)' }}>
          <span>Task ID</span><code style={{ userSelect: 'text', overflowWrap: 'anywhere' }}>{selTask.task}</code>
          <Button aria-label="Copy task ID" onClick={() => void copyTaskId(selTask.task)} size="sm" variant="ghost"><Codicon name="copy" /> Copy ID</Button>
        </div>}
        {selected ? (
          <div style={{ flex: 1, minHeight: 0, display: 'flex' }}>
            <AssetViewer imageIndex={imageIndex} onImageIndexChange={setImageIndex} autoRotate={autoRotate} height="100%" key={String(autoRotate)} os={os} rest={rest} source={selected} />
          </div>
        ) : null}
        {selected && selTask && <MadeFrom key={`m:${selTask.task}`} onPick={pickTask} rest={rest} task={selTask.task} />}
        {selected ? null : (
          <div style={V.empty}>
            <Codicon name="symbol-misc" size={32} />
            <span>Pick an image or a model to preview.</span>
          </div>
        )}
      </section>
    </div>
  )
}

/** Status-bar chip: remaining Meshy credits; refreshed on open and after any Meshy tool finishes. */
export function BalanceChip({ balance, onOpen }: { balance: Balance | null; onOpen: () => void }) {
  if (!balance) {
    return null
  }
  const label = !balance.configured
    ? 'no key'
    : balance.error
      ? /rejected/i.test(balance.error)
        ? 'key rejected'
        : 'offline'
      : `${(balance.balance ?? 0).toLocaleString()} cr`
  return (
    <button
      data-meshy-chip
      onClick={onOpen}
      style={V.chip}
      title={balance.error ? `Meshy: ${balance.error}` : 'Meshy credits — open the 3D gallery'}
      type="button"
    >
      <Codicon name="symbol-misc" />
      <span>Meshy {label}</span>
    </button>
  )
}

export { host }
