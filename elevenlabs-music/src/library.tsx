// The music library workspace: every track this profile's tools made, from the provenance records.

import type { PluginContext } from '@hermes/plugin-sdk'
import { Button, Codicon, host } from '@hermes/plugin-sdk'
import { useEffect, useMemo, useState } from 'react'

import { backendIsRemote, currentScope, errorText, onScopeChange } from './audio.ts'
import { bytesLabel, durationLabel, formatLabel, type HostInfo, KINDS, kindLabel, matches, modelLabel, type TrackRecord, trackTitle } from './music.ts'
import { Chip, DownloadMenu, S, T, Transport, useTrack, Waveform, Where } from './ui.tsx'

const PAGE = 100
// The newest few rows load their audio up front so their waveforms show; the rest load on Play.
const AUTO_LOAD_ROWS = 4
const AUTO_LOAD_BYTES = 3 * 1024 * 1024

interface Library extends HostInfo {
  tracks: TrackRecord[]
  readable?: number
  total: number
  totals: Record<string, { count: number; bytes: number; missing: number; seconds: number }>
}

const L = {
  pane: { height: '100%', overflow: 'auto', color: T.text },
  column: { display: 'flex', flexDirection: 'column', gap: 10, padding: 16, maxWidth: 960, margin: '0 auto' },
  input: {
    flex: 1,
    minWidth: 160,
    background: T.input,
    color: T.text,
    border: `1px solid ${T.border}`,
    borderRadius: 8,
    padding: '5px 10px',
    fontSize: 13
  },
  row: {
    border: `1px solid ${T.border}`,
    background: T.card,
    borderRadius: 10,
    padding: '10px 12px',
    display: 'flex',
    flexDirection: 'column',
    gap: 8
  }
} as const

const minutes = (s: number) => (s >= 60 ? `${Math.round(s / 60)} min` : `${Math.round(s)} s`)

/**
 * `refreshSignal` changes when a music tool call completes (or settings change) so the list reloads once;
 * a profile or connection switch reloads it too. No timers: each trigger is one request, and an error stays
 * on screen with a Retry button instead of retrying on its own.
 */
export function MusicLibrary({ ctx, refreshSignal }: { ctx: PluginContext; refreshSignal: number }) {
  const [data, setData] = useState<Library | null>(null)
  const [error, setError] = useState<null | string>(null)
  const [remote, setRemote] = useState<boolean | null>(null)
  const [scope, setScope] = useState(currentScope())
  const [reload, setReload] = useState(0)
  const [kind, setKind] = useState<string>('all')
  const [query, setQuery] = useState('')
  const [shownCount, setShownCount] = useState(PAGE)

  useEffect(
    () =>
      onScopeChange(() => {
        setData(null)
        setScope(currentScope())
      }),
    []
  )

  useEffect(() => {
    let live = true
    setError(null)
    void backendIsRemote().then(r => live && setRemote(r))
    ctx
      .rest<Library>('tracks?limit=5000', { timeoutMs: 20_000 })
      .then(d => live && setData(d))
      .catch(err => live && setError(errorText(err)))
    return () => {
      live = false
    }
  }, [ctx, scope.profile, scope.connection, reload, refreshSignal])

  const shown = useMemo(
    () => (data?.tracks ?? []).filter(t => (kind === 'all' || (t.kind ?? 'song') === kind) && matches(t, query)),
    [data, kind, query]
  )

  const where =
    remote === true
      ? `on ${data?.host ?? 'the remote Hermes host'} (remote). Playback and downloads stream through Hermes.`
      : remote === false
        ? 'on this computer.'
        : `on ${data?.host ?? 'the Hermes host'}, the machine running this profile’s Hermes.`

  return (
    <div data-music-library="" style={L.pane as React.CSSProperties}>
      <div style={L.column as React.CSSProperties}>
        <div style={{ ...S.row, justifyContent: 'space-between' }}>
          <strong style={{ fontSize: 15 }}>
            <Codicon name="library" /> Music library
          </strong>
          <Button aria-label="Reload" onClick={() => setReload(r => r + 1)} size="sm" variant="ghost">
            <Codicon name="refresh" />
          </Button>
        </div>
        <span data-music-scope="" style={S.muted}>
          Profile <b>{scope.profile || 'default'}</b>: music made with ElevenLabs, stored {where}
        </span>
        {error && (
          <div style={S.row}>
            <span style={S.error}>{error}</span>
            <Button onClick={() => setReload(r => r + 1)} size="sm" variant="outline">
              Retry
            </Button>
          </div>
        )}
        {data && (
          <>
            <div data-music-totals="" style={S.row}>
              <Chip>
                {data.total} track{data.total === 1 ? '' : 's'}
                {data.tracks.length < data.total ? ` (newest ${data.tracks.length} listed)` : ''}
              </Chip>
              {KINDS.map(k => {
                const t = data.totals[k]
                return t ? (
                  <Chip key={k} title={t.missing ? `${t.missing} file(s) no longer on the host` : undefined}>
                    {kindLabel(k)}s {t.count} · {minutes(t.seconds)} · {bytesLabel(t.bytes)}
                  </Chip>
                ) : null
              })}
              {!data.ffmpeg && <span style={S.muted}>Conversions need ffmpeg on {data.host}.</span>}
            </div>
            <div style={S.row}>
              {['all', ...KINDS].map(k => (
                <Button aria-pressed={kind === k} data-music-filter={k} key={k} onClick={() => setKind(k)} size="sm" variant={kind === k ? 'default' : 'outline'}>
                  {k === 'all' ? 'All' : `${kindLabel(k)}s`}
                </Button>
              ))}
              <input aria-label="Search titles and prompts" onChange={e => setQuery(e.currentTarget.value)} placeholder="Search titles, prompts, genres…" style={L.input} value={query} />
            </div>
            {data.total === 0 ? (
              <span style={S.muted}>
                No music yet in this profile. Ask Hermes for a song (“a 30 second upbeat synthwave intro for my podcast”);
                every track the ElevenLabs Music tools make is listed here with the prompt and settings that made it.
              </span>
            ) : shown.length === 0 ? (
              <span style={S.muted}>No tracks match.</span>
            ) : (
              <>
                {shown.slice(0, shownCount).map((rec, i) => (
                  <LibraryRow autoLoad={i < AUTO_LOAD_ROWS} ctx={ctx} info={data} key={rec.id} rec={rec} remote={remote} />
                ))}
                {shown.length > shownCount && (
                  <Button onClick={() => setShownCount(c => c + PAGE)} size="sm" variant="outline">
                    Show more ({shown.length - shownCount} more)
                  </Button>
                )}
              </>
            )}
          </>
        )}
        {!data && !error && <span style={S.muted}>Loading…</span>}
      </div>
    </div>
  )
}

function LibraryRow({ ctx, rec, info, remote, autoLoad }: { ctx: PluginContext; rec: TrackRecord; info: HostInfo; remote: boolean | null; autoLoad: boolean }) {
  const sound = useTrack(ctx, rec, `lib:${rec.id}`)
  useEffect(() => {
    if (autoLoad && rec.exists && (rec.size ?? 0) <= AUTO_LOAD_BYTES) void sound.load()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once per record
  }, [rec.id, autoLoad])
  const date = rec.created_at ? new Date(rec.created_at) : null
  const reuse = async () => {
    const text = rec.prompt || ''
    const inserted = typeof host.composer?.insertText === 'function' ? await host.composer.insertText(null, text) : false
    if (inserted) {
      host.notify({ kind: 'success', message: 'Prompt added to the composer.' })
    } else if (ctx.os.writeClipboard && (await ctx.os.writeClipboard(text))) {
      host.notify({ kind: 'info', message: 'No composer open: prompt copied to the clipboard.' })
    } else {
      host.notify({ kind: 'warning', message: 'Open a chat to reuse the prompt.' })
    }
  }
  const openChat = () => {
    if (rec.session_id && typeof host.openSession === 'function') {
      host.openSession(rec.session_id).catch(err => host.notify({ kind: 'error', message: `Could not open the chat: ${errorText(err)}` }))
    }
  }
  return (
    <div data-music-row={rec.kind ?? 'song'} style={L.row as React.CSSProperties}>
      <div style={{ ...S.row, flexWrap: 'nowrap' }}>
        <Transport compact sound={sound} />
        <span style={{ display: 'flex', flexDirection: 'column', flex: 1, minWidth: 0 }}>
          <span style={{ ...S.prompt, fontWeight: 600 }}>{trackTitle(rec)}</span>
          {rec.title && rec.prompt && rec.kind === 'song' && <span style={{ ...S.muted, overflowWrap: 'anywhere' }}>{rec.prompt}</span>}
        </span>
        {date && !Number.isNaN(date.getTime()) && (
          <span style={{ ...S.muted, whiteSpace: 'nowrap' }} title={date.toLocaleString()}>
            {date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })}
          </span>
        )}
      </div>
      {sound.loaded.state === 'ready' && <Waveform data={sound.loaded.wave} height={30} onSeek={f => void sound.seek(f)} progress={sound.progress} />}
      {sound.loaded.state === 'error' && <span style={S.error}>{sound.loaded.message}</span>}
      <div style={S.row}>
        <Chip>{durationLabel(sound.duration ?? rec.length_seconds ?? rec.requested_seconds)}</Chip>
        <Chip>{kindLabel(rec.kind)}</Chip>
        {(rec.genres ?? []).slice(0, 2).map(g => (
          <Chip key={g}>{g}</Chip>
        ))}
        {rec.instrumental && <Chip>instrumental</Chip>}
        <Chip title={rec.model ?? ''}>{modelLabel(rec.model)}</Chip>
        <Chip title={rec.size != null ? bytesLabel(rec.size) : undefined}>{formatLabel(rec.output_format)}</Chip>
        <span style={{ flex: 1 }} />
        {rec.prompt && (
          <Button data-music-reuse="" onClick={() => void reuse()} size="sm" title="Put this prompt in the composer" variant="outline">
            <Codicon name="edit" /> Reuse prompt
          </Button>
        )}
        <Button
          data-music-open-chat=""
          disabled={!rec.session_id || typeof host.openSession !== 'function'}
          onClick={openChat}
          size="sm"
          title={rec.session_id ? 'Open the chat that made this track' : 'No chat recorded for this track'}
          variant="outline"
        >
          <Codicon name="comment-discussion" /> Open chat
        </Button>
        <DownloadMenu ctx={ctx} info={info} rec={rec} />
        <Where ctx={ctx} info={info} rec={rec} remote={remote} />
      </div>
    </div>
  )
}
