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

import { errorText, fetchAudio, loadSound, player, saveDownload, SEND_FAILED, sendToChat } from './audio.ts'
import { clockLabel, type HostInfo, kindLabel, nextActions, type SoundRecord } from './sound.ts'

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
      data-sfx-waveform=""
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

type Loaded = { state: 'idle' } | { state: 'loading' } | { state: 'error'; message: string } | { state: 'ready'; buffer: AudioBuffer; wave: number[] }

/**
 * Loads (once) and plays one sound. Decoded audio comes from a small shared cache (`loadSound`); nothing polls. While it
 * plays, one animation frame per paint advances the progress bar, and the loop stops when playback does.
 */
export function useSound(ctx: PluginContext, rec: null | SoundRecord, ownerPrefix: string) {
  // One owner per mount: two cards for the same sound must not stop each other on unmount.
  const owner = `${ownerPrefix}:${useId()}`
  const [loaded, setLoaded] = useState<Loaded>({ state: 'idle' })
  const [, setTick] = useState(0)
  const [loop, setLoop] = useState(Boolean(rec?.loop))
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
    if (loaded.state === 'ready') return loaded.buffer
    if (!rec || !rec.exists) return null
    setLoaded({ state: 'loading' })
    try {
      const { buffer, wave } = await loadSound(ctx, rec.id)
      if (alive.current) setLoaded({ state: 'ready', buffer, wave })
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

  const duration = loaded.state === 'ready' ? loaded.buffer.duration : null
  const progress = duration ? player.position(owner) / duration : 0
  return { loaded, load, toggle, seek, playing, loop, setLooping, duration, progress }
}

/** Playback position and length, e.g. `0:00.4 / 0:01.0`. */
export function Clock({ sound }: { sound: ReturnType<typeof useSound> }) {
  if (sound.duration == null) return null
  return (
    <span data-sfx-clock="" style={S.time}>
      {clockLabel(sound.duration * sound.progress)} / {clockLabel(sound.duration)}
    </span>
  )
}

export function Transport({ sound, compact }: { sound: ReturnType<typeof useSound>; compact?: boolean }) {
  return (
    <>
      <Button
        aria-label={sound.playing ? 'Pause' : 'Play'}
        data-sfx-play={sound.playing ? 'playing' : 'paused'}
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
        data-sfx-loop={sound.loop ? 'on' : 'off'}
        onClick={() => sound.setLooping(!sound.loop)}
        size="sm"
        title="Play the sound on repeat, gaplessly, to hear the loop seam"
        variant={sound.loop ? 'default' : 'outline'}
      >
        <Codicon name="sync" />
        {!compact && 'Loop'}
      </Button>
    </>
  )
}

/** Download per format, converted by the plugin backend on the Hermes host; saved on THIS machine. */
export function DownloadMenu({ ctx, rec, info }: { ctx: PluginContext; rec: SoundRecord; info: HostInfo | null }) {
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
  const conversions = (info?.conversions ?? []).filter(f => f !== rec.kind)
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button data-sfx-download="" disabled={!rec.exists || busy !== null} size="sm" variant="outline">
          <Codicon name={busy ? 'loading' : 'cloud-download'} spinning={busy !== null} />
          Download
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start">
        <DropdownMenuItem data-sfx-format="original" onSelect={() => void run('original')}>
          {kindLabel(rec.kind)} · original
        </DropdownMenuItem>
        {conversions.length > 0 && <DropdownMenuSeparator />}
        {conversions.map(f => (
          <DropdownMenuItem data-sfx-format={f} key={f} onSelect={() => void run(f)}>
            {f === 'ogg' ? 'OGG Vorbis (game engines)' : f === 'wav' ? 'WAV 16-bit (video, engines)' : 'MP3 (chat, web)'}
          </DropdownMenuItem>
        ))}
        {info && !info.ffmpeg && (
          <DropdownMenuItem data-sfx-format="none" disabled title="Install ffmpeg on the Hermes host for conversions">
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
export function Where({ ctx, rec, info, remote }: { ctx: PluginContext; rec: SoundRecord; info: HostInfo | null; remote: boolean | null }) {
  const [note, setNote] = useState<null | string>(null)
  if (!rec.exists || !rec.path) {
    return <span style={S.muted}>File no longer on the Hermes host</span>
  }
  if (remote) {
    return (
      <span data-sfx-remote="" style={S.muted} title={rec.path}>
        Saved on {info?.host ?? 'the Hermes host'} (remote); use Download to get a copy here
      </span>
    )
  }
  return (
    <>
      <Button
        data-sfx-reveal=""
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

/** Next actions: each sends a chat message (the agent runs the tool, its approval and billing apply). */
export function NextMenu({ rec, measured, directiveSession }: { rec: SoundRecord; measured: null | number; directiveSession: null | string | undefined }) {
  // `directiveSession` is this render's prop: the chat that shows the card, fixed for the card's life.
  const actions = nextActions(rec, measured)
  if (actions.length === 0) return null
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button data-sfx-next="" size="sm" variant="outline">
          <Codicon name="sparkle" />
          Next
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" style={{ maxWidth: 380 }}>
        {actions.map(a => (
          <DropdownMenuItem
            data-sfx-action={a.id}
            key={a.id}
            onSelect={() => {
              const sent = sendToChat(directiveSession, a.message)
              if (sent !== 'sent') host.notify({ kind: 'warning', message: SEND_FAILED[sent] })
            }}
            title={a.message}
          >
            <span style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
              <span>{a.label}</span>
              <span style={S.muted}>{a.cost}</span>
            </span>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
