// Pure helpers shared by the music card and the library: record shape, labels, lyrics timing and the
// Next actions. No SDK imports here, so `node --test` can exercise this file directly.

export interface TrackRecord {
  id: string
  created_at: null | string
  /** song (compose / compose plan), stem (one stem of a split) or score (video to music). */
  kind: null | string
  tool: null | string
  title: null | string
  prompt: null | string
  description: null | string
  genres: null | string[]
  languages: null | string[]
  explicit: boolean | null
  model: null | string
  instrumental: boolean | null
  finetune_id: null | string
  seed: null | number
  /** The length asked for; null = ElevenLabs chose. */
  requested_seconds: null | number
  /** The file's real length, measured by the tool; null when it could not read it. */
  length_seconds: null | number
  output_format: null | string
  file_name: null | string
  bytes: null | number
  /** ElevenLabs' id for the song, usable in composition plans when it was stored for editing. */
  song_id: null | string
  stored_for_editing: boolean | null
  /** For stems and edits: the track it came from. */
  parent_id: null | string
  stem: null | string
  session_id: null | string
  /** Added by the backend. */
  ext: string
  exists: boolean
  /** Absolute path on the Hermes host (which may be another machine). */
  path: null | string
  size: null | number
}

export interface HostInfo {
  host: string
  ffmpeg: boolean
  conversions: string[]
}

export interface WordTiming {
  word: string
  start_ms: number
  end_ms: number
}

export interface PlanChunk {
  text?: string
  duration_ms?: number
  positive_styles?: string[]
  negative_styles?: string[]
  song_id?: string
  range?: { start_ms: number; end_ms: number }
}

export interface TrackDetail {
  track: TrackRecord
  words: WordTiming[]
  plan: null | { chunks?: PlanChunk[] }
}

export const MODELS = ['music_v2_5', 'music_v2', 'music_v1'] as const
export const PLAN_MODELS = ['music_v2_5', 'music_v2'] as const
export const COMPOSE = 'elevenlabs_music_compose'
export const COMPOSE_PLAN = 'elevenlabs_music_compose_plan'
export const STEMS = 'elevenlabs_music_stems'
/** Every tool of this plugin whose result can carry a MEDIA: file (a finished job returns its tool's result). */
export const MEDIA_TOOLS = [COMPOSE, COMPOSE_PLAN, STEMS, 'elevenlabs_music_from_video', 'elevenlabs_music_job'] as const
export const KINDS = ['song', 'stem', 'score'] as const

export const modelLabel = (model: null | string): string =>
  model === 'music_v2_5' ? 'v2.5' : model === 'music_v2' ? 'v2' : model === 'music_v1' ? 'v1' : model?.includes('stems') ? (model.startsWith('two') ? '2 stems' : '6 stems') : model || 'model ?'

export const kindLabel = (kind: null | string): string => (kind === 'stem' ? 'Stem' : kind === 'score' ? 'Video score' : 'Song')

export const extLabel = (ext: string): string => (ext === 'opus' ? 'Ogg Opus' : ext.toUpperCase() || '?')

export function formatLabel(format: null | string): string {
  const m = /^(mp3|opus)_(\d+)_(\d+)$/.exec(format || '')
  if (!m) return format || '?'
  return `${extLabel(m[1])} ${Number(m[2]) / 1000} kHz ${m[3]} kbps`
}

export function bytesLabel(n: null | number): string {
  if (!n || n < 0) return '0 B'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

/** `0:07`, `3:05`. */
export function durationLabel(seconds: null | number | undefined): string {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return 'auto length'
  const t = Math.round(seconds)
  return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, '0')}`
}

/** A playback clock: `0:01.5`, `1:04.0`. */
export function clockLabel(seconds: number): string {
  const t = Number.isFinite(seconds) && seconds > 0 ? Math.round(seconds * 10) / 10 : 0
  const m = Math.floor(t / 60)
  return `${m}:${(t - m * 60).toFixed(1).padStart(4, '0')}`
}

/** The card's heading: the song title, else what it is. */
export function trackTitle(rec: Pick<TrackRecord, 'kind' | 'title' | 'prompt' | 'stem'>): string {
  const stem = rec.stem ? rec.stem.charAt(0).toUpperCase() + rec.stem.slice(1) : ''
  if (rec.kind === 'stem') return `${stem || 'Stem'} · ${rec.title || rec.prompt || 'track'}`
  if (rec.kind === 'score') return `Score: ${rec.prompt || 'video'}`
  return rec.title || rec.prompt || 'Untitled'
}

/**
 * A tool-media card hands the track back to Desktop's built-in player (`fallback`) whenever it could not
 * play it itself: the record could not be read, the backend will not serve the file, or loading failed.
 */
export function showsFallback(card: { record: 'error' | 'loading' | 'ready'; exists?: boolean; audio?: string }, hasFallback: boolean): boolean {
  if (!hasFallback) return false
  return card.record === 'error' || (card.record === 'ready' && (card.exists === false || card.audio === 'error'))
}

/** The words around the playhead for the karaoke strip: up to `span` words, with the sung one marked. */
export function lyricWindow(words: WordTiming[], ms: number, span = 9): { words: string[]; current: number } | null {
  if (!words.length) return null
  let at = words.findIndex(w => ms < w.end_ms)
  if (at < 0) return null
  if (ms < words[at].start_ms - 1500) return null // a gap (instrumental passage): show nothing
  const start = Math.max(0, at - Math.floor(span / 3))
  const slice = words.slice(start, start + span)
  return { words: slice.map(w => w.word), current: ms >= words[at].start_ms ? at - start : -1 }
}

/** Lyrics text from the plan's chunks (section names included), or '' for an instrumental. */
export function lyricsText(plan: TrackDetail['plan']): string {
  return (plan?.chunks ?? [])
    .map(c => (typeof c.text === 'string' ? c.text.trim() : ''))
    .filter(t => t && !/^\[[^\]]*\]$/.test(t))
    .join('\n\n')
}

export interface Section {
  index: number
  label: string
  start_ms: number
  end_ms: number
}

/** Each chunk's label and time range in the song (what a section edit keeps or regenerates). */
export function sections(plan: TrackDetail['plan']): Section[] {
  const out: Section[] = []
  let at = 0
  for (const [index, c] of (plan?.chunks ?? []).entries()) {
    const length = c.range ? c.range.end_ms - c.range.start_ms : Number(c.duration_ms) || 0
    const first = (c.text ?? '').split('\n', 1)[0].trim()
    const label = c.range ? 'kept slice' : first.startsWith('[') && first.endsWith(']') ? first.slice(1, -1) : first.slice(0, 40) || 'section'
    out.push({ index, label, start_ms: at, end_ms: at + length })
    at += length
  }
  return out
}

export interface NextAction {
  id: 'edit-section' | 'extend' | 'instrumental' | 'model' | 'stems' | 'variations'
  label: string
  detail: string
  /** The chat message the row sends (the agent runs the tool; its approval and billing apply). */
  message: string
  /** Put the message in the composer for the user to finish instead of sending it. */
  draft?: boolean
}

const seconds = (n: number) => `${Math.round(n)} s`

/**
 * The card's Next menu. Each row's message names the tool and arguments its schema accepts, and how much
 * music it bills (ElevenLabs bills music by length).
 */
export function nextActions(rec: TrackRecord, detail: Pick<TrackDetail, 'plan'> | null, measured: null | number = null): NextAction[] {
  if (rec.kind !== 'song') {
    return []
  }
  const out: NextAction[] = []
  const length = measured ?? rec.length_seconds ?? rec.requested_seconds ?? null
  const ref = `track ${rec.id}${rec.title ? ` ("${rec.title}")` : ''}`
  const prompt = (rec.prompt || '').trim()
  const fromPrompt = rec.tool === COMPOSE && prompt
  const model = rec.model && (MODELS as readonly string[]).includes(rec.model) ? rec.model : MODELS[0]
  const args = (extra: Record<string, unknown> = {}) => {
    const parts = [`prompt ${JSON.stringify(prompt)}`]
    const len = typeof rec.requested_seconds === 'number' ? rec.requested_seconds : null
    if (len != null) parts.push(`length_seconds ${len}`)
    const all = { instrumental: Boolean(rec.instrumental), model, ...extra }
    for (const [k, v] of Object.entries(all)) parts.push(`${k} ${typeof v === 'string' ? v : JSON.stringify(v)}`)
    return parts.join(', ')
  }
  const billed = length != null ? `bills about ${seconds(length)} of music` : 'billed by length'

  if (fromPrompt) {
    out.push({
      id: 'variations',
      label: '3 variations',
      detail: length != null ? `bills about ${seconds(length * 3)} of music` : 'billed by length, 3 times',
      message: `Use ${COMPOSE} to make 3 variations of ${ref}, one call each with ${args()}. Each ${billed}. Reply with each result's media_tag line.`
    })
    if (!rec.instrumental) {
      out.push({
        id: 'instrumental',
        label: 'Instrumental version',
        detail: billed,
        message: `Use ${COMPOSE} to make an instrumental version of ${ref} with ${args({ instrumental: true })}. It ${billed}. Reply with its media_tag line.`
      })
    }
    const other = model === 'music_v2_5' ? 'music_v2' : 'music_v2_5'
    out.push({
      id: 'model',
      label: `Try ${modelLabel(other)}`,
      detail: billed,
      message: `Use ${COMPOSE} to make ${ref} again with the ${modelLabel(other)} model: ${args({ model: other })}. It ${billed}. Reply with its media_tag line.`
    })
  }

  const editable = Boolean(rec.song_id && rec.stored_for_editing)
  const totalMs = length != null ? Math.round(length * 1000) : null
  if (editable && totalMs != null && totalMs <= 570_000) {
    const styles = detail?.plan?.chunks?.find(c => Array.isArray(c.positive_styles) && c.positive_styles.length)?.positive_styles ?? rec.genres ?? []
    const chunks = [
      { song_id: rec.song_id, range: { start_ms: 0, end_ms: totalMs } },
      { text: '[Outro]', duration_ms: 30000, positive_styles: [...styles.slice(0, 8), 'natural ending'] }
    ]
    out.push({
      id: 'extend',
      label: 'Extend by 30 s',
      detail: `keeps the song, adds a 30 s ending; bills about ${seconds(totalMs / 1000 + 30)}`,
      message: `Use ${COMPOSE_PLAN} to extend ${ref}: keep the whole song and add a 30 s ending in the same style, with model ${PLAN_MODELS.includes(model as never) ? model : 'music_v2_5'} and chunks ${JSON.stringify(chunks)}. It bills about ${seconds(totalMs / 1000 + 30)} of music. Reply with its media_tag line.`
    })
    for (const s of sections(detail?.plan ?? null).filter(s => s.label !== 'kept slice').slice(0, 8)) {
      out.push({
        id: 'edit-section',
        label: `Redo ${s.label} (${durationLabel(s.start_ms / 1000)}–${durationLabel(s.end_ms / 1000)})`,
        detail: 'opens a message for you to say what to change',
        draft: true,
        message: `Regenerate the "${s.label}" section (${s.start_ms}–${s.end_ms} ms) of ${ref} with ${COMPOSE_PLAN}, keeping the rest of the song as slices of elevenlabs_song_id ${rec.song_id}. Change: `
      })
    }
  }

  out.push({
    id: 'stems',
    label: 'Split into stems',
    detail: `vocals, drums, bass, guitar, piano, other; ${billed}`,
    message: `Use ${STEMS} to split ${ref} into six stems (track_id ${rec.id}, stems six). It ${billed}. Reply with each media_tag line.`
  })
  return out
}

/** Matches the files the tools write: `<music|stem|score>_<slug>_<date>_<time>_<track id>.<ext>`. */
const TRACK_FILE = /_([0-9a-f]{12})\.(?:mp3|opus|wav|flac|m4a|aac|ogg)$/
export const TRACK_ID = /^[0-9a-f]{12}$/

/**
 * The track id behind a `MEDIA:` file Desktop attributes to this plugin's tools: the id at the end of every
 * file name the tools write. `null` (draw the built-in player) for a failed call or anything else.
 */
export function trackIdOf(result: unknown, fileName: unknown): null | string {
  let value = result
  if (typeof value === 'string') {
    try {
      value = JSON.parse(value)
    } catch {
      value = null
    }
  }
  const obj = value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : null
  if (obj && (obj.success === false || obj.error != null)) return null
  const m = typeof fileName === 'string' ? TRACK_FILE.exec(fileName) : null
  return m ? m[1] : null
}

/** This plugin's tool calls inside a tool event: direct, or wrapped by tool search's `tool_call`. */
export function isMusicToolCall(name: unknown, args: unknown): boolean {
  const tools = MEDIA_TOOLS as readonly string[]
  if (typeof name === 'string' && tools.includes(name)) return true
  if (name !== 'tool_call') return false
  let a: unknown = args
  if (typeof a === 'string') {
    try {
      a = JSON.parse(a)
    } catch {
      return false
    }
  }
  if (!a || typeof a !== 'object') return false
  const obj = a as Record<string, unknown>
  let calls: unknown = obj.calls ?? [{ name: obj.name }]
  if (typeof calls === 'string') {
    try {
      calls = JSON.parse(calls)
    } catch {
      return false
    }
  }
  return (Array.isArray(calls) ? calls : [calls]).some(
    c => !!c && typeof c === 'object' && tools.includes(String((c as Record<string, unknown>).name))
  )
}

/**
 * Promise cache with a small LRU bound: concurrent loads of one key share a single request, a few recent
 * results stay for remounts, and a failure is dropped so a retry really retries.
 */
export class PromiseCache<T> {
  private entries = new Map<string, Promise<T>>()
  private readonly limit: number
  constructor(limit: number) {
    this.limit = limit
  }

  get(key: string, load: () => Promise<T>): Promise<T> {
    const hit = this.entries.get(key)
    if (hit) {
      this.entries.delete(key)
      this.entries.set(key, hit)
      return hit
    }
    const pending = load()
    this.entries.set(key, pending)
    pending.catch(() => {
      if (this.entries.get(key) === pending) this.entries.delete(key)
    })
    while (this.entries.size > this.limit) this.entries.delete(this.entries.keys().next().value as string)
    return pending
  }

  clear(): void {
    this.entries.clear()
  }
}

/** Library search: every whitespace-separated term must appear in the title, prompt, genres or stem. */
export function matches(rec: TrackRecord, query: string): boolean {
  const hay = `${rec.title || ''} ${rec.prompt || ''} ${(rec.genres || []).join(' ')} ${rec.stem || ''} ${rec.model || ''}`.toLowerCase()
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every(term => hay.includes(term))
}

export type SendResult = 'no-composer' | 'not-visible' | 'sent'

/**
 * Submit a card's follow-up through `host.composer.submit` (passed in, so this stays SDK-free). A host
 * that names the card's chat hands a `sessionId`: a string, or null for a chat with no session yet, sent as
 * `'new'`. `undefined` is an older host, where the active composer is the only address.
 */
export function submitToChat(submit: unknown, cardSession: null | string | undefined, text: string): SendResult {
  if (typeof submit !== 'function') return 'no-composer'
  const address = cardSession === undefined ? null : (cardSession ?? 'new')
  return (submit as (session: null | string, text: string) => boolean)(address, text) ? 'sent' : 'not-visible'
}

export const SEND_FAILED: Record<Exclude<SendResult, 'sent'>, string> = {
  'no-composer': 'This Hermes Desktop cannot send a message for a plugin: update Desktop, or type the request yourself.',
  'not-visible': 'This card’s chat is not open on screen: open it, then try again.'
}
