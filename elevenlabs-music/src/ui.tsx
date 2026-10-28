// Shared UI pieces: waveform, transport (play/pause/loop), download menu, reveal, Next menu.

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
import { type CSSProperties, type ReactNode, useEffect, useId, useRef, useState } from 'react'

import { errorText, fetchAudio, loadTrack, player, saveDownload, SEND_FAILED, sendToChat } from './audio.ts'
import { clockLabel, CONVERTED_CAP_MB, convertedTooBig, extLabel, type HostInfo, nextActions, type TrackDetail, type TrackRecord } from './music.ts'

// Desktop's documented theme variables for plugins (`--ui-*`), so the card reskins with every theme.
// The `--color-*` names exist only inside Desktop's own Tailwind build, not as CSS variables.
export const T = {
  text: 'var(--ui-text-primary)',
  secondary: 'var(--ui-text-secondary)',
  muted: 'var(--ui-text-tertiary)',
  faint: 'var(--ui-text-quaternary)',
  border: 'var(--ui-stroke-secondary)',
  card: 'var(--ui-bg-card)',
  chip: 'var(--ui-bg-tertiary)',
  input: 'var(--ui-bg-input)',
  accent: 'var(--ui-accent)',
  danger: 'var(--ui-red)'
} as const

export const S = {
  card: {
    border: `1px solid ${T.border}`,
    borderRadius: 12,
    background: T.card,
    color: T.text,
    padding: '12px 14px',
    margin: '8px 0',
    maxWidth: 600,
    display: 'flex',
    flexDirection: 'column',
    gap: 10
  } as CSSProperties,
  row: { display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' } as CSSProperties,
  muted: { color: T.muted, fontSize: 12 } as CSSProperties,
  chip: {
    borderRadius: 999,
    padding: '2px 9px',
    fontSize: 11,
    lineHeight: '16px',
    background: T.chip,
    color: T.secondary,
    whiteSpace: 'nowrap'
  } as CSSProperties,
  error: { color: T.danger, fontSize: 12 } as CSSProperties,
  prompt: { fontSize: 13.5, fontWeight: 500, lineHeight: 1.4, overflowWrap: 'anywhere', color: T.text } as CSSProperties,
  time: { color: T.muted, fontSize: 12, fontVariantNumeric: 'tabular-nums', padding: '0 4px', whiteSpace: 'nowrap' } as CSSProperties
}

export function Chip({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span style={S.chip} title={title}>
      {children}
    </span>
  )
}

/** Clickable waveform with a progress fill and playhead; seeking calls `onSeek(fraction)`. */
export function Waveform({ data, progress, onSeek, height = 44 }: { data: number[]; progress: number; onSeek: (f: number) => void; height?: number }) {
  const width = data.length * 3
  // Peaks scaled to the loudest bucket: a quiet sound still draws a readable shape.
  const top = Math.max(0.05, ...data)
  return (
    <svg
      aria-label="Waveform; click to seek"
      data-music-waveform=""
      onClick={e => {
        const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect()
        onSeek(Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)))
      }}
      preserveAspectRatio="none"
      role="img"
      style={{ width: '100%', height, cursor: 'pointer', display: 'block' }}
      viewBox={`0 0 ${width} ${height}`}
    >
      {data.map((v, i) => {
        const h = Math.max(2, (v / top) * (height - 4))
        const played = i / data.length < progress
        return <rect fill={played ? T.accent : T.muted} height={h} key={i} rx={1} width={2} x={i * 3} y={(height - h) / 2} />
      })}
      {progress > 0 && progress < 1 && <rect fill={T.accent} height={height} width={1} x={progress * width} y={0} />}
    </svg>
  )
}

// Only the waveform and length stay in component state; the decoded audio lives in the bounded cache in audio.ts,
// so a chat full of song cards does not pin every decoded song in memory.
type Loaded = { state: 'idle' } | { state: 'loading' } | { state: 'error'; message: string } | { state: 'ready'; duration: number; wave: number[] }

/**
 * Loads (once) and plays one sound. Decoded audio comes from a small shared cache (`loadSound`); nothing polls. While it
 * plays, one animation frame per paint advances the progress bar, and the loop stops when playback does.
 */
export function useTrack(ctx: PluginContext, rec: null | TrackRecord, ownerPrefix: string) {
  // One owner per mount: two cards for the same sound must not stop each other on unmount.
  const owner = `${ownerPrefix}:${useId()}`
  const [loaded, setLoaded] = useState<Loaded>({ state: 'idle' })
  const [, setTick] = useState(0)
  const [loop, setLoop] = useState(false)
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    const off = player.subscribe(() => setTick(t => t + 1))
    return () => {
      alive.current = false
      off()
      player.release(owner)
    }
  }, [owner])

  const playing = player.playing(owner)
  useEffect(() => {
    if (!playing) return
    let raf = 0
    const step = () => {
      setTick(t => t + 1)
      raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [playing])

  const load = async (): Promise<AudioBuffer | null> => {
    if (!rec || !rec.exists) return null
    if (loaded.state === 'ready') {
      try {
        return (await loadTrack(ctx, rec.id)).buffer // cache hit, or one re-fetch after eviction
      } catch (err) {
        if (alive.current) setLoaded({ state: 'error', message: errorText(err) })
        return null
      }
    }
    setLoaded({ state: 'loading' })
    try {
      const { buffer, wave } = await loadTrack(ctx, rec.id)
      if (alive.current) setLoaded({ state: 'ready', duration: buffer.duration, wave })
      return buffer
    } catch (err) {
      if (alive.current) setLoaded({ state: 'error', message: errorText(err) })
      return null
    }
  }

  const toggle = async () => {
    if (playing) {
      player.pause(owner)
      return
    }
    const buffer = await load()
    if (buffer) player.play(owner, buffer, loop, player.position(owner))
  }

  const seek = async (fraction: number) => {
    const buffer = await load()
    if (buffer) player.play(owner, buffer, loop, fraction * buffer.duration)
  }

  const setLooping = (value: boolean) => {
    setLoop(value)
    player.setLoop(owner, value)
  }

  const duration = loaded.state === 'ready' ? loaded.duration : null
  const progress = duration ? player.position(owner) / duration : 0
  return { loaded, load, toggle, seek, playing, loop, setLooping, duration, progress }
}

/** Playback position and length, e.g. `0:00.4 / 0:01.0`. */
export function Clock({ sound }: { sound: ReturnType<typeof useTrack> }) {
  if (sound.duration == null) return null
  return (
    <span data-music-clock="" style={S.time}>
      {clockLabel(sound.duration * sound.progress)} / {clockLabel(sound.duration)}
    </span>
  )
}

export function Transport({ sound, compact }: { sound: ReturnType<typeof useTrack>; compact?: boolean }) {
  return (
    <>
      <Button
        aria-label={sound.playing ? 'Pause' : 'Play'}
        data-music-play={sound.playing ? 'playing' : 'paused'}
        disabled={sound.loaded.state === 'loading'}
        onClick={() => void sound.toggle()}
        size="sm"
        variant={compact ? 'outline' : 'default'}
      >
        <Codicon name={sound.loaded.state === 'loading' ? 'loading' : sound.playing ? 'debug-pause' : 'play'} spinning={sound.loaded.state === 'loading'} />
        {!compact && (sound.playing ? 'Pause' : 'Play')}
      </Button>
      <Button
        aria-label="Loop audition"
        aria-pressed={sound.loop}
        data-music-loop={sound.loop ? 'on' : 'off'}
        onClick={() => sound.setLooping(!sound.loop)}
        size="sm"
        title="Play on repeat, gaplessly (hear a loop's seam)"
        variant={sound.loop ? 'default' : 'outline'}
      >
        <Codicon name="sync" />
        {!compact && 'Loop'}
      </Button>
    </>
  )
}

/** Download per format, converted by the plugin backend on the Hermes host; saved on THIS machine. */
export function DownloadMenu({ ctx, rec, info }: { ctx: PluginContext; rec: TrackRecord; info: HostInfo | null }) {
  const [busy, setBusy] = useState<null | string>(null)
  const run = async (format: string) => {
    setBusy(format)
    try {
      await saveDownload(await fetchAudio(ctx, rec.id, format))
    } catch (err) {
      host.notify({ kind: 'error', message: `Download failed: ${errorText(err)}` })
    } finally {
      setBusy(null)
    }
  }
  const conversions = (info?.conversions ?? []).filter(f => f !== rec.ext)
  const tooBig = (f: string) => convertedTooBig(f, rec.length_seconds)
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button data-music-download="" disabled={!rec.exists || busy !== null} size="sm" variant="outline">
          <Codicon name={busy ? 'loading' : 'cloud-download'} spinning={busy !== null} />
          Download
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start">
        <DropdownMenuItem data-music-format="original" onSelect={() => void run('original')}>
          {extLabel(rec.ext)} · original
        </DropdownMenuItem>
        {conversions.length > 0 && <DropdownMenuSeparator />}
        {conversions.map(f => (
          <DropdownMenuItem
            data-music-format={f}
            disabled={tooBig(f)}
            key={f}
            onSelect={() => void run(f)}
            title={tooBig(f) ? `Too long for ${f.toUpperCase()} here (over ${CONVERTED_CAP_MB} MB); use MP3 or the original` : undefined}
          >
            {f === 'ogg' ? 'OGG Vorbis (game engines)' : f === 'wav' ? 'WAV 16-bit (video editors, DAWs)' : f === 'flac' ? 'FLAC (lossless archive)' : 'MP3 (chat, web)'}
          </DropdownMenuItem>
        ))}
        {info && !info.ffmpeg && (
          <DropdownMenuItem data-music-format="none" disabled title="Install ffmpeg on the Hermes host for conversions">
            Other formats need ffmpeg on {info.host}
          </DropdownMenuItem>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/**
 * Reveal in the file manager, only when the file can be on this machine. On a remote backend it says where
 * the file really is instead of a button that silently does nothing.
 */
export function Where({ ctx, rec, info, remote }: { ctx: PluginContext; rec: TrackRecord; info: HostInfo | null; remote: boolean | null }) {
  const [note, setNote] = useState<null | string>(null)
  if (!rec.exists || !rec.path) {
    return <span style={S.muted}>File no longer on the Hermes host</span>
  }
  if (remote) {
    return (
      <span data-music-remote="" style={S.muted} title={rec.path}>
        Saved on {info?.host ?? 'the Hermes host'} (remote); use Download to get a copy here
      </span>
    )
  }
  return (
    <>
      <Button
        data-music-reveal=""
        onClick={() => {
          void ctx.os.revealPath(rec.path as string).then(ok => {
            setNote(ok ? null : `Not on this computer: the file is on ${info?.host ?? 'the Hermes host'} at ${rec.path}`)
          })
        }}
        size="sm"
        variant="outline"
      >
        <Codicon name="folder-opened" />
        Reveal
      </Button>
      {note && <span style={S.muted}>{note}</span>}
    </>
  )
}

/** Next actions: each sends a chat message (the agent runs the tool; its approval and billing apply), or
 *  for section edits puts a started message in the composer for the user to finish. */
export function NextMenu({ rec, detail, measured, cardSession }: { rec: TrackRecord; detail: Pick<TrackDetail, 'plan'> | null; measured: null | number; cardSession: null | string | undefined }) {
  // `cardSession` is this render's prop: the chat that shows the card, fixed for the card's life.
  const actions = nextActions(rec, detail, measured)
  if (actions.length === 0) return null
  const run = async (message: string, draft: boolean | undefined) => {
    if (draft && typeof host.composer?.insertText === 'function') {
      const address = cardSession === undefined ? null : (cardSession ?? 'new')
      if (await host.composer.insertText(address, message)) return
    }
    const sent = sendToChat(cardSession, message)
    if (sent !== 'sent') host.notify({ kind: 'warning', message: SEND_FAILED[sent] })
  }
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button data-music-next="" size="sm" variant="outline">
          <Codicon name="sparkle" />
          Next
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" style={{ maxWidth: 400 }}>
        {actions.map((a, i) => (
          <DropdownMenuItem data-music-action={a.id} key={`${a.id}-${i}`} onSelect={() => void run(a.message, a.draft)} title={a.message}>
            <span style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
              <span>{a.label}</span>
              <span style={S.muted}>{a.detail}</span>
            </span>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
