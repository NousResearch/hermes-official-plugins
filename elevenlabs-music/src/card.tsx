// The in-chat music card. Desktop draws it for a MEDIA: line that one of this plugin's tools produced (the
// tool-media renderer seam): songs, stems and video scores alike.

import type { PluginContext, ToolMediaRendererProps } from '@hermes/plugin-sdk'
import { Button, Codicon } from '@hermes/plugin-sdk'
import { type ReactNode, useEffect, useState } from 'react'

import { backendIsRemote, errorText, onScopeChange, statusOf } from './audio.ts'
import {
  durationLabel,
  formatLabel,
  type HostInfo,
  kindLabel,
  lyricsText,
  lyricWindow,
  modelLabel,
  showsFallback,
  type TrackDetail,
  TRACK_ID,
  trackIdOf,
  trackTitle
} from './music.ts'
import { Chip, Clock, DownloadMenu, NextMenu, S, T, Transport, useTrack, Waveform, Where } from './ui.tsx'

// Songs are bigger than sound effects: decode up front only when the file is small (about 4 minutes of
// 128 kbps MP3); a longer one loads on Play.
const AUTO_LOAD_BYTES = 4 * 1024 * 1024

type State =
  | { kind: 'loading' }
  | { kind: 'error'; message: string; retry: boolean }
  | ({ kind: 'ready'; info: HostInfo } & TrackDetail)

/**
 * The card for a `MEDIA:` file Desktop attributes to one of this plugin's tools. Anything it can't draw as a
 * music card (a failed call, a file that is not one of these tracks, a record this profile's backend does
 * not have) gets the built-in player, so the track always plays.
 */
export function ToolMediaMusicCard({ ctx, media, onOpenLibrary }: { ctx: PluginContext; media: ToolMediaRendererProps; onOpenLibrary: () => void }) {
  const id = trackIdOf(media.result, media.file?.name)
  if (!id || media.file?.kind !== 'audio') return <>{media.fallback}</>
  return <MusicCard ctx={ctx} fallback={media.fallback} id={id} onOpenLibrary={onOpenLibrary} sessionId={media.sessionId} />
}

export function MusicCard({
  ctx,
  id,
  sessionId,
  onOpenLibrary,
  fallback
}: {
  ctx: PluginContext
  id: string
  sessionId: null | string | undefined
  onOpenLibrary: () => void
  fallback?: ReactNode
}) {
  const valid = TRACK_ID.test(id)
  const [state, setState] = useState<State>({ kind: 'loading' })
  const [remote, setRemote] = useState<boolean | null>(null)
  const [generation, setGeneration] = useState(0)

  useEffect(() => onScopeChange(() => setGeneration(g => g + 1)), [])

  // One request per mount / retry / profile-or-connection switch; never a timer.
  useEffect(() => {
    if (!valid) return
    let live = true
    setState({ kind: 'loading' })
    void backendIsRemote().then(r => live && setRemote(r))
    ctx
      .rest<TrackDetail & HostInfo>(`track?id=${id}`, { timeoutMs: 20_000 })
      .then(({ track, words, plan, ...info }) => live && setState({ kind: 'ready', track, words: words ?? [], plan: plan ?? null, info }))
      .catch(err => {
        if (!live) return
        const status = statusOf(err)
        const message =
          status === 404 && /no track/.test(errorText(err))
            ? 'This track is not in the active profile’s library (it was made in another profile, or its record was removed).'
            : errorText(err)
        setState({ kind: 'error', message, retry: status === 0 || status >= 500 || status === 404 })
      })
    return () => {
      live = false
    }
  }, [ctx, id, valid, generation])

  if (!valid) return <>{fallback}</>
  if (state.kind === 'loading') {
    return (
      <div data-music-card="loading" style={S.card}>
        <span style={S.muted}>
          <Codicon name="loading" spinning /> Loading track…
        </span>
      </div>
    )
  }
  if (state.kind === 'error') {
    if (showsFallback({ record: 'error' }, fallback !== undefined)) return <>{fallback}</>
    return (
      <div data-music-card="error" style={S.card}>
        <span style={S.error}>{state.message}</span>
        {state.retry && (
          <div style={S.row}>
            <Button onClick={() => setGeneration(g => g + 1)} size="sm" variant="outline">
              <Codicon name="refresh" /> Retry
            </Button>
          </div>
        )}
      </div>
    )
  }
  return <ReadyCard ctx={ctx} detail={state} fallback={fallback} info={state.info} onOpenLibrary={onOpenLibrary} remote={remote} sessionId={sessionId} />
}

function ReadyCard({
  ctx,
  detail,
  info,
  remote,
  sessionId,
  onOpenLibrary,
  fallback
}: {
  ctx: PluginContext
  detail: TrackDetail
  info: HostInfo
  remote: boolean | null
  sessionId: null | string | undefined
  onOpenLibrary: () => void
  fallback?: ReactNode
}) {
  const rec = detail.track
  const sound = useTrack(ctx, rec, `card:${rec.id}`)
  const [showLyrics, setShowLyrics] = useState(false)
  useEffect(() => {
    if (rec.exists && (rec.size ?? 0) <= AUTO_LOAD_BYTES) void sound.load()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- load once per record
  }, [rec.id])
  if (showsFallback({ record: 'ready', exists: rec.exists, audio: sound.loaded.state }, fallback !== undefined)) {
    return <>{fallback}</>
  }
  const wave = sound.loaded.state === 'ready' ? sound.loaded.wave : null
  const lyrics = lyricsText(detail.plan)
  const position = sound.duration ? sound.duration * sound.progress : 0
  const karaoke = sound.playing || position > 0 ? lyricWindow(detail.words, position * 1000) : null
  return (
    <div data-music-card="ready" data-music-id={rec.id} style={S.card}>
      <div style={{ ...S.row, justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'nowrap' }}>
        <span style={{ display: 'flex', gap: 10, alignItems: 'flex-start', minWidth: 0 }}>
          <span style={{ color: T.accent, lineHeight: 1, paddingTop: 2 }}>
            <Codicon name={rec.kind === 'stem' ? 'layers' : rec.kind === 'score' ? 'device-camera-video' : 'music'} />
          </span>
          <span style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
            <span data-music-title="" style={{ ...S.prompt, fontSize: 15, fontWeight: 600 }}>
              {trackTitle(rec)}
            </span>
            {rec.description && <span style={{ ...S.muted, lineHeight: 1.4 }}>{rec.description}</span>}
          </span>
        </span>
        <Button aria-label="Open the music library" data-music-open-library="" onClick={onOpenLibrary} size="sm" title="Every track made in this profile" variant="ghost">
          <Codicon name="library" /> Library
        </Button>
      </div>
      <div style={S.row}>
        <Chip title="Length of the track (the length asked for, when ElevenLabs returned a different one)">
          {durationLabel(sound.duration ?? rec.length_seconds ?? rec.requested_seconds)}
        </Chip>
        {rec.kind !== 'song' && <Chip>{kindLabel(rec.kind)}</Chip>}
        {(rec.genres ?? []).slice(0, 3).map(g => (
          <Chip key={g}>{g}</Chip>
        ))}
        {rec.instrumental && <Chip>instrumental</Chip>}
        {rec.explicit && <Chip>explicit</Chip>}
        <Chip title={rec.model ?? ''}>{modelLabel(rec.model)}</Chip>
        <Chip>{formatLabel(rec.output_format)}</Chip>
        {rec.song_id && rec.stored_for_editing && <Chip title={`ElevenLabs song id ${rec.song_id}: it can be edited and extended`}>editable</Chip>}
      </div>
      {karaoke && (
        <div data-music-karaoke="" style={{ fontSize: 14, lineHeight: 1.5, minHeight: 21 }}>
          {karaoke.words.map((w, i) => (
            <span key={i} style={{ color: i === karaoke.current ? T.accent : i < karaoke.current ? T.secondary : T.muted, fontWeight: i === karaoke.current ? 600 : 400 }}>
              {w}{' '}
            </span>
          ))}
        </div>
      )}
      {wave ? (
        <Waveform data={wave} onSeek={f => void sound.seek(f)} progress={sound.progress} />
      ) : (
        <div style={{ ...S.muted, height: 44, display: 'flex', alignItems: 'center' }}>
          {sound.loaded.state === 'error' ? (
            <span style={S.error}>{sound.loaded.message}</span>
          ) : rec.exists ? (
            sound.loaded.state === 'loading' ? 'Loading audio…' : 'Press Play to load the audio'
          ) : (
            'The track file is no longer on the Hermes host.'
          )}
        </div>
      )}
      <div style={S.row}>
        <Transport sound={sound} />
        <Clock sound={sound} />
        <span style={{ flex: 1 }} />
        {lyrics && (
          <Button aria-pressed={showLyrics} data-music-lyrics-toggle="" onClick={() => setShowLyrics(v => !v)} size="sm" variant={showLyrics ? 'default' : 'outline'}>
            <Codicon name="note" /> Lyrics
          </Button>
        )}
        <DownloadMenu ctx={ctx} info={info} rec={rec} />
        <Where ctx={ctx} info={info} rec={rec} remote={remote} />
        <NextMenu cardSession={sessionId} detail={detail} measured={sound.duration} rec={rec} />
      </div>
      {showLyrics && lyrics && (
        <pre data-music-lyrics="" style={{ ...S.muted, fontSize: 12.5, whiteSpace: 'pre-wrap', fontFamily: 'inherit', margin: 0, padding: 0, background: 'transparent', border: 0, borderRadius: 0, maxHeight: 280, overflow: 'auto', color: T.secondary }}>
          {lyrics}
        </pre>
      )}
    </div>
  )
}
