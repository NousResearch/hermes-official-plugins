// The in-chat sound card. Desktop draws it for the MEDIA: line of a sound this plugin's tool made (the
// tool-media renderer seam); `::elevenlabs-sfx{id="<sound_id>"}` lines in chats from earlier versions
// render it too.

import type { PluginContext, ToolMediaRendererProps } from '@hermes/plugin-sdk'
import { Button, Codicon } from '@hermes/plugin-sdk'
import { type ReactNode, useEffect, useState } from 'react'

import { backendIsRemote, errorText, onScopeChange, statusOf } from './audio.ts'
import { billed, formatLabel, type HostInfo, lengthLabel, modelLabel, SOUND_ID, soundIdOf, type SoundRecord, showsFallback } from './sound.ts'
import { Chip, Clock, DownloadMenu, NextMenu, S, T, Transport, useSound, Waveform, Where } from './ui.tsx'

const AUTO_LOAD_BYTES = 4 * 1024 * 1024

type State =
  | { kind: 'loading' }
  | { kind: 'error'; message: string; retry: boolean }
  | { kind: 'ready'; rec: SoundRecord; info: HostInfo }

/**
 * The card for a `MEDIA:` file Desktop attributes to this plugin's tool. Anything it can't draw as a
 * sound card (a failed call, a file that is not one of this tool's sounds, a record this profile's
 * backend does not have) gets the built-in player, so the sound always plays.
 */
export function ToolMediaSoundCard({
  ctx,
  media,
  onOpenLibrary
}: {
  ctx: PluginContext
  media: ToolMediaRendererProps
  onOpenLibrary: () => void
}) {
  const id = soundIdOf(media.result, media.file?.name)
  if (!id || media.file?.kind !== 'audio') return <>{media.fallback}</>
  return (
    <SoundCard
      ctx={ctx}
      fallback={media.fallback}
      id={id}
      onOpenLibrary={onOpenLibrary}
      sessionId={media.sessionId}
      source=""
      streaming={false}
    />
  )
}

export function SoundCard({
  ctx,
  id,
  sessionId,
  streaming,
  source,
  onOpenLibrary,
  fallback
}: {
  ctx: PluginContext
  id: string
  sessionId: null | string | undefined
  streaming: boolean
  source: string
  onOpenLibrary: () => void
  /** Tool-media cards: Desktop's built-in player, shown whenever this card can't play the sound. */
  fallback?: ReactNode
}) {
  const valid = SOUND_ID.test(id)
  const [state, setState] = useState<State>({ kind: 'loading' })
  const [remote, setRemote] = useState<boolean | null>(null)
  const [generation, setGeneration] = useState(0)

  useEffect(() => onScopeChange(() => setGeneration(g => g + 1)), [])

  // One request per mount / retry / profile-or-connection switch; never a timer.
  useEffect(() => {
    if (!valid || streaming) return
    let live = true
    setState({ kind: 'loading' })
    void backendIsRemote().then(r => live && setRemote(r))
    ctx
      .rest<{ sound: SoundRecord } & HostInfo>(`sound?id=${id}`, { timeoutMs: 20_000 })
      .then(({ sound, ...info }) => live && setState({ kind: 'ready', rec: sound, info }))
      .catch(err => {
        if (!live) return
        const status = statusOf(err)
        const message =
          status === 404 && /no sound/.test(errorText(err))
            ? 'This sound is not in the active profile’s library (it was made in another profile, or its record was removed).'
            : errorText(err)
        setState({ kind: 'error', message, retry: status === 0 || status >= 500 || status === 404 })
      })
    return () => {
      live = false
    }
  }, [ctx, id, valid, streaming, generation])

  if (!valid) {
    return <p>{source}</p>
  }
  if (streaming || state.kind === 'loading') {
    return (
      <div data-sfx-card="loading" style={S.card}>
        <span style={S.muted}>
          <Codicon name="loading" spinning /> Loading sound…
        </span>
      </div>
    )
  }
  if (state.kind === 'error') {
    if (showsFallback({ record: 'error' }, fallback !== undefined)) return <>{fallback}</>
    return (
      <div data-sfx-card="error" style={S.card}>
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
  return (
    <ReadyCard
      ctx={ctx}
      fallback={fallback}
      info={state.info}
      onOpenLibrary={onOpenLibrary}
      rec={state.rec}
      remote={remote}
      sessionId={sessionId}
    />
  )
}

function ReadyCard({
  ctx,
  rec,
  info,
  remote,
  sessionId,
  onOpenLibrary,
  fallback
}: {
  ctx: PluginContext
  rec: SoundRecord
  info: HostInfo
  remote: boolean | null
  sessionId: null | string | undefined
  onOpenLibrary: () => void
  fallback?: ReactNode
}) {
  const sound = useSound(ctx, rec, `card:${rec.id}`)
  useEffect(() => {
    if (rec.exists && (rec.size ?? 0) <= AUTO_LOAD_BYTES) void sound.load()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- load once per record
  }, [rec.id])
  const cost = billed(rec)
  const wave = sound.loaded.state === 'ready' ? sound.loaded.wave : null
  if (showsFallback({ record: 'ready', exists: rec.exists, audio: sound.loaded.state }, fallback !== undefined)) {
    return <>{fallback}</>
  }
  return (
    <div data-sfx-card="ready" data-sfx-id={rec.id} style={S.card}>
      <div style={{ ...S.row, justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <span style={{ ...S.prompt, display: 'flex', gap: 8, alignItems: 'flex-start' }}>
          <span style={{ color: T.accent, lineHeight: 1 }}>
            <Codicon name="unmute" />
          </span>
          <span>{rec.prompt}</span>
        </span>
        <Button aria-label="Open the sound library" data-sfx-open-library="" onClick={onOpenLibrary} size="sm" title="Every sound made in this profile" variant="ghost">
          <Codicon name="library" /> Library
        </Button>
      </div>
      <div style={S.row}>
        <Chip title="Length of the sound (and the length asked for, when ElevenLabs returned a different one; it bills the request)">
          {lengthLabel(rec, sound.duration)}
        </Chip>
        {rec.loop && <Chip>loopable</Chip>}
        <Chip title={rec.model ?? ''}>{modelLabel(rec.model)}</Chip>
        <Chip>{formatLabel(rec.output_format)}</Chip>
        {cost != null && <Chip title="ElevenLabs character-cost header for this generation">{cost} credits</Chip>}
      </div>
      {wave ? (
        <Waveform data={wave} onSeek={f => void sound.seek(f)} progress={sound.progress} />
      ) : (
        <div style={{ ...S.muted, height: 44, display: 'flex', alignItems: 'center' }}>
          {sound.loaded.state === 'error' ? (
            <span style={S.error}>{sound.loaded.message}</span>
          ) : rec.exists ? (
            sound.loaded.state === 'loading' ? 'Loading audio…' : 'Press Play to load the audio'
          ) : (
            'The sound file is no longer on the Hermes host.'
          )}
        </div>
      )}
      <div style={S.row}>
        <Transport sound={sound} />
        <Clock sound={sound} />
        <span style={{ flex: 1 }} />
        <DownloadMenu ctx={ctx} info={info} rec={rec} />
        <Where ctx={ctx} info={info} rec={rec} remote={remote} />
        <NextMenu directiveSession={sessionId} measured={sound.duration} rec={rec} />
      </div>
    </div>
  )
}
