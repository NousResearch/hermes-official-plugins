// Pure helpers shared by the sound card and the library: record shape, labels, and the Next actions.
// No SDK imports here, so `node --test` can exercise this file directly.

export interface SoundRecord {
  id: string
  created_at: null | string
  prompt: null | string
  /** The length requested (what ElevenLabs bills); null = automatic length. */
  duration_seconds: null | number
  /** The length of the audio ElevenLabs returned (loops can come back longer); null when unknown
   *  (a record from an earlier build, or a file the tool could not read). */
  length_seconds?: null | number
  loop: boolean | null
  prompt_influence: null | number
  model: null | string
  output_format: null | string
  file_name: null | string
  bytes: null | number
  billed_characters: null | string
  session_id: null | string
  /** Added by the backend: container the tool wrote (mp3 | opus | wav). */
  kind: string
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

export const MODELS = ['eleven_text_to_sound_v2', 'eleven_text_to_sound_v3'] as const
export const TOOL = 'elevenlabs_sound_effect'
/** Every container the tool can write (plugin.yaml output_format choices). */
export const KINDS = ['mp3', 'opus', 'wav'] as const

export const modelLabel = (model: null | string): string =>
  model === 'eleven_text_to_sound_v3' ? 'v3' : model === 'eleven_text_to_sound_v2' ? 'v2' : model || 'model ?'

export const kindLabel = (kind: string): string => (kind === 'opus' ? 'Ogg Opus' : kind.toUpperCase() || '?')

export function formatLabel(format: null | string): string {
  const m = /^(mp3|opus|wav)_(\d+)(?:_(\d+))?$/.exec(format || '')
  if (!m) return format || '?'
  const rate = `${Number(m[2]) / 1000} kHz`
  return m[3] ? `${kindLabel(m[1])} ${rate} ${m[3]} kbps` : `${kindLabel(m[1])} ${rate}`
}

export function bytesLabel(n: null | number): string {
  if (!n || n < 0) return '0 B'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

export const secondsLabel = (s: null | number | undefined): string =>
  typeof s === 'number' && Number.isFinite(s) ? `${s < 10 ? s.toFixed(2).replace(/\.?0+$/, '') : Math.round(s)} s` : 'auto length'

/**
 * A tool-media card hands the sound back to Desktop's built-in player (`fallback`) whenever it could not
 * play it itself: the record could not be read, the backend will not serve the file (gone, or refused,
 * e.g. hard-linked by a backup tool), or loading the audio failed. The built-in player streams the MEDIA:
 * path on its own, so the sound stays playable. Directive cards (old transcripts) have no fallback.
 */
export function showsFallback(card: { record: 'error' | 'loading' | 'ready'; exists?: boolean; audio?: string }, hasFallback: boolean): boolean {
  if (!hasFallback) return false
  return card.record === 'error' || (card.record === 'ready' && (card.exists === false || card.audio === 'error'))
}

/** A playback clock: `0:01.5`, `1:04.0`. */
export function clockLabel(seconds: number): string {
  const t = Number.isFinite(seconds) && seconds > 0 ? Math.round(seconds * 10) / 10 : 0
  const m = Math.floor(t / 60)
  return `${m}:${(t - m * 60).toFixed(1).padStart(4, '0')}`
}

/** The Length chip: the real length (decoded here, else the tool's measurement), plus what was asked for
 *  when ElevenLabs returned a different length (it bills the request). */
export function lengthLabel(rec: Pick<SoundRecord, 'duration_seconds' | 'length_seconds'>, decoded: null | number): string {
  const real = decoded ?? (typeof rec.length_seconds === 'number' ? rec.length_seconds : null)
  const asked = rec.duration_seconds
  if (real == null) return secondsLabel(asked)
  return typeof asked === 'number' && Math.abs(real - asked) >= 0.05
    ? `${secondsLabel(real)} (asked ${secondsLabel(asked)})`
    : secondsLabel(real)
}
/** Credits the record was billed, from ElevenLabs' `character-cost` header (null when it sent none). */
export function billed(rec: Pick<SoundRecord, 'billed_characters'>): null | number {
  const n = Number(rec.billed_characters)
  return rec.billed_characters != null && Number.isFinite(n) && n >= 0 ? n : null
}

/** Credits per second, derived only from a sound that had an explicit duration (an auto-length sound is
 *  billed a flat amount per generation, ~50 credits measured, so it says nothing about the per-second
 *  price). */
export function ratePerSecond(rec: Pick<SoundRecord, 'billed_characters' | 'duration_seconds'>): null | number {
  const b = billed(rec)
  const d = rec.duration_seconds
  return b != null && typeof d === 'number' && d > 0 ? b / d : null
}

export interface NextAction {
  id: 'longer' | 'loop' | 'model' | 'shorter' | 'variations'
  label: string
  /** Cost line shown on the row, from this sound's real bill. */
  cost: string
  /** The chat message the row sends (the agent runs the tool; its approval and billing apply). */
  message: string
}

interface ToolArgs {
  prompt: string
  duration_seconds?: number
  loop: boolean
  prompt_influence?: number
  model: string
}

const round1 = (n: number) => Math.round(n * 10) / 10
const clampDuration = (n: number) => Math.min(30, Math.max(0.5, round1(n)))
const credits = (n: number) => `${Math.round(n)} credit${Math.round(n) === 1 ? '' : 's'}`

/** Only argument names the tool schema accepts, in the order the model reads them. */
export function argsText(args: ToolArgs): string {
  const parts = [`prompt ${JSON.stringify(args.prompt)}`]
  if (args.duration_seconds != null) parts.push(`duration_seconds ${args.duration_seconds}`)
  parts.push(`loop ${args.loop}`)
  if (args.prompt_influence != null) parts.push(`prompt_influence ${args.prompt_influence}`)
  parts.push(`model ${args.model}`)
  return parts.join(', ')
}

function costFor(rec: SoundRecord, seconds: null | number): { line: string; total: null | number } {
  const rate = ratePerSecond(rec)
  const b = billed(rec)
  if (rate != null && seconds != null) {
    return { line: `≈ ${credits(rate * seconds)} (${round1(rate)}/s, from this sound's bill)`, total: rate * seconds }
  }
  if (seconds == null && b != null) {
    return { line: `≈ ${credits(b)} (what this auto-length sound billed)`, total: b }
  }
  if (b != null) {
    return { line: `billed per second; this auto-length sound billed ${credits(b)}, so the rate is unknown`, total: null }
  }
  return { line: 'cost unknown (ElevenLabs reported no bill for this sound)', total: null }
}

/**
 * The card's Next menu. Each row's message names the tool and only arguments its schema accepts
 * (prompt, duration_seconds, loop, prompt_influence, model) and states the cost derived from this
 * sound's billed result. `measured` is the decoded length, used as the base when the sound had an
 * automatic length.
 */
export function nextActions(rec: SoundRecord, measured: null | number = null): NextAction[] {
  // Verbatim: the tool refuses an over-long prompt itself, and rewriting whitespace would change the sound.
  const prompt = (rec.prompt || '').trim()
  if (!prompt) return []
  const model = rec.model && (MODELS as readonly string[]).includes(rec.model) ? rec.model : MODELS[0]
  const base: ToolArgs = {
    prompt,
    loop: Boolean(rec.loop),
    model,
    ...(typeof rec.duration_seconds === 'number' ? { duration_seconds: rec.duration_seconds } : {}),
    ...(typeof rec.prompt_influence === 'number' ? { prompt_influence: rec.prompt_influence } : {})
  }
  const seconds = typeof rec.duration_seconds === 'number' ? rec.duration_seconds : null
  const length = seconds ?? (measured != null && measured > 0 ? measured : null)
  const ref = `(from sound ${rec.id})`
  const out: NextAction[] = []

  const same = costFor(rec, seconds)
  out.push({
    id: 'variations',
    label: '3 variations',
    cost: same.total != null ? `≈ ${credits(same.total * 3)} total (3 × ${credits(same.total)})` : same.line,
    message:
      `Use ${TOOL} to make 3 variations of this sound ${ref}, one call each with ${argsText(base)}. ` +
      `Cost: ${same.total != null ? `about ${credits(same.total)} each, ${credits(same.total * 3)} in total` : same.line}. ` +
      "Reply with each result's media_tag line."
  })

  const resize = (id: 'longer' | 'shorter', target: number) => {
    const args = { ...base, duration_seconds: target }
    const cost = costFor(rec, target)
    out.push({
      id,
      label: `${id === 'shorter' ? 'Shorter' : 'Longer'} (${secondsLabel(target)})`,
      cost: cost.line,
      message: `Use ${TOOL} to make a ${id} version of this sound ${ref} with ${argsText(args)}. Cost: ${cost.line}. Reply with its media_tag line.`
    })
  }
  if (length != null && length > 0.5) resize('shorter', clampDuration(length / 2))
  if (length != null && length < 30) resize('longer', clampDuration(length * 2))

  if (!rec.loop) {
    const args = { ...base, loop: true }
    out.push({
      id: 'loop',
      label: 'Make it loop',
      cost: same.line,
      message:
        `Use ${TOOL} to make a seamless loop of this sound ${ref} with ${argsText(args)}. Cost: ${same.line}. ` +
        'Reply with its media_tag line.'
    })
  }

  const other = model === MODELS[0] ? MODELS[1] : MODELS[0]
  out.push({
    id: 'model',
    label: `Try ${modelLabel(other)}`,
    cost: same.line,
    message:
      `Use ${TOOL} to make this sound ${ref} again with the ${modelLabel(other)} model: ${argsText({ ...base, model: other })}. ` +
      `Cost: ${same.line}. Reply with its media_tag line.`
  })
  return out
}

/** This plugin's tool calls inside a tool event: direct, or wrapped by tool search's `tool_call` (its
 *  `{calls: [{name, arguments}]}` batch, a JSON-string batch, or the legacy single `{name, arguments}`). */
function soundCalls(name: unknown, args: unknown): Record<string, unknown>[] {
  const obj = (v: unknown): null | Record<string, unknown> => {
    if (typeof v === 'string') {
      try {
        v = JSON.parse(v)
      } catch {
        return null
      }
    }
    return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null
  }
  const a = obj(args)
  if (name === TOOL) return a ? [a] : [{}]
  if (name !== 'tool_call' || !a) return []
  let calls: unknown = a.calls ?? [{ name: a.name, arguments: a.arguments }]
  if (typeof calls === 'string') {
    try {
      calls = JSON.parse(calls)
    } catch {
      calls = []
    }
  }
  const list = Array.isArray(calls) ? calls : [calls]
  return list.flatMap(c => {
    const call = obj(c)
    return call && call.name === TOOL ? [obj(call.arguments) ?? {}] : []
  })
}

/** Does a tool event belong to this plugin's tool (direct, or wrapped by tool search's `tool_call`)? */
export const isSoundToolCall = (name: unknown, args: unknown): boolean => soundCalls(name, args).length > 0

/** Matches the files the tool writes: `sfx_<slug>_<date>_<time>_<sound id>.<ext>`. */
const SOUND_FILE = /_([0-9a-f]{12})\.(?:mp3|opus|wav)$/

/**
 * The sound id behind a `MEDIA:` file Desktop attributes to this plugin's tool: the result's
 * `sound_id` (the result arrives as an object live and can be a JSON string in a reopened chat), else
 * the id the tool puts at the end of every file name. `null` (draw the built-in player) for a failed
 * call or anything that is not one of this tool's sounds.
 */
export function soundIdOf(result: unknown, fileName: unknown): null | string {
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
  if (obj && typeof obj.sound_id === 'string' && SOUND_ID.test(obj.sound_id)) return obj.sound_id
  const m = typeof fileName === 'string' ? SOUND_FILE.exec(fileName) : null
  return m ? m[1] : null
}

/**
 * Promise cache with a small LRU bound: concurrent loads of one key share a single request, a few
 * recent results stay for remounts (virtualised scroll, two cards of one sound), and a failure is
 * dropped so a retry really retries.
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

export const SOUND_ID = /^[0-9a-f]{12}$/

/** Text filter for the library: every whitespace-separated term must appear in the prompt or model/format. */
export function matches(rec: SoundRecord, query: string): boolean {
  const hay = `${rec.prompt || ''} ${rec.model || ''} ${rec.output_format || ''}`.toLowerCase()
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every(term => hay.includes(term))
}

export type SendResult = 'no-composer' | 'not-visible' | 'sent'

/**
 * Submit a card's follow-up through `host.composer.submit` (passed in, so this stays SDK-free). A
 * directive-owner host hands the card a `sessionId`: a string, or null for a chat with no session yet,
 * which is sent as `'new'` (that fresh draft only; `null` would mean the ACTIVE composer, possibly
 * another chat). `undefined` is an older host, where the active composer is the only address.
 */
export function submitToChat(submit: unknown, directiveSession: null | string | undefined, text: string): SendResult {
  if (typeof submit !== 'function') return 'no-composer'
  const address = directiveSession === undefined ? null : (directiveSession ?? 'new')
  return (submit as (session: null | string, text: string) => boolean)(address, text) ? 'sent' : 'not-visible'
}

export const SEND_FAILED: Record<Exclude<SendResult, 'sent'>, string> = {
  'no-composer': 'This Hermes Desktop cannot send a message for a plugin: update Desktop, or type the request yourself.',
  'not-visible': 'This card’s chat is not open on screen: open it, then try again.'
}
