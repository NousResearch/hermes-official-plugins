import { host } from '@hermes/plugin-sdk'

import { type EventOrigin, taskIdFrom } from './active.ts'

/**
 * Provenance: which tool call made which Meshy task. Meshy's own task record keeps a text prompt but not
 * an image task's input image, nor the task a remesh / retexture / rig / animation came from, so the
 * plugin records the creating call (from the tool.complete event) and the gallery shows it as "Made from".
 */

const CREATE_TOOLS = new Set([
  'meshy_text_to_3d', 'meshy_text_to_3d_refine', 'meshy_image_to_3d', 'meshy_multi_image_to_3d', 'meshy_remesh',
  'meshy_retexture', 'meshy_rig', 'meshy_animate', 'meshy_resize', 'meshy_convert', 'meshy_uv_unwrap',
  'meshy_repair_printability', 'meshy_auto_split', 'meshy_process_multicolor', 'meshy_text_to_image', 'meshy_image_to_image'
])

export interface Creation {
  task_id: string
  tool: string
  args: Record<string, unknown>
}

const bare = (name: unknown) => String(name ?? '').replace(/^(mcp__meshy__|mcp_meshy_)/, '')

/** Every Meshy task this tool call created: a direct call, or each call a `tool_call` bridge made
 *  (its result carries one entry per call, in order). */
export function creationsFrom(name: unknown, args: unknown, result: unknown): Creation[] {
  const direct = bare(name)
  if (CREATE_TOOLS.has(direct)) {
    const id = taskIdFrom(result)
    return id ? [{ task_id: id, tool: direct, args: (args ?? {}) as Record<string, unknown> }] : []
  }
  if (String(name ?? '') !== 'tool_call' || !args || typeof args !== 'object') {
    return []
  }
  const calls = (Array.isArray((args as { calls?: unknown }).calls) ? (args as { calls: unknown[] }).calls : [args]) as Array<{
    name?: unknown
    arguments?: unknown
  }>
  let results: unknown[] = []
  try {
    const parsed = typeof result === 'string' ? JSON.parse(result) : result
    const r = (parsed as { results?: unknown })?.results
    results = Array.isArray(r) ? r : [parsed]
  } catch {
    results = [result]
  }
  const out: Creation[] = []
  calls.forEach((call, i) => {
    const tool = bare(call?.name)
    if (!CREATE_TOOLS.has(tool)) {
      return
    }
    const one = results[i] as { result?: unknown; content?: unknown } | undefined
    const id = taskIdFrom(one?.result ?? one?.content ?? one)
    if (id) {
      out.push({ task_id: id, tool, args: (call.arguments ?? {}) as Record<string, unknown> })
    }
  })
  return out
}

export interface ProvenanceRecord extends Creation {
  session_id?: string
}

const profileKey = (p: unknown) => String(p ?? '').trim() || 'default'

/** True when an event is tagged with another profile/connection than the one ctx.rest reaches now. */
export function isForeignOrigin(origin?: EventOrigin): boolean {
  const activeProfile = host.state?.profile?.get()
  const activeConnection = host.state?.connectionId?.get()
  return Boolean(
    (origin?.profile && activeProfile !== undefined && profileKey(origin.profile) !== profileKey(activeProfile)) ||
      (origin?.connectionId && activeConnection && origin.connectionId !== activeConnection)
  )
}

/**
 * ctx.rest reaches whichever profile/connection is in front, and a background completion can belong to another
 * one. The full record (prompt, input URLs) therefore goes only to a backend that has just said it owns the chat:
 *
 * 1. An event tagged with another profile/connection than the active one is not sent right away; it waits here.
 *    Tags only delay a send, never exclude a backend: core stamps primary-path events with whatever is in front,
 *    so a tag can be wrong, and step 2 already keeps the arguments from a backend that does not own the chat.
 * 2. Otherwise an ID-only record (task id, tool name, session id; no arguments) goes first. The backend keeps a
 *    row only for its own chats and answers 409 for anyone else's, so a foreign backend learns no prompt.
 * 3. Only after that succeeds, and only if the profile/connection did not change meanwhile (same request scope),
 *    is the full record posted to the same backend.
 *
 * Refused records wait here (newest 200) and are offered again after each profile or connection switch. A switch
 * that lands while sends are in flight re-runs the flush once when they settle, so a refusal that arrives after
 * the switch is not stranded until the next one. Retries happen only on switches: no polling loop.
 */
export function createProvenanceWriter(post: (body: unknown) => Promise<unknown>) {
  let pending: Array<{ body: ProvenanceRecord; origin?: EventOrigin }> = []
  let timer: ReturnType<typeof setTimeout> | undefined
  let scope = 0 // bumped on every profile/connection change
  let inflight = 0
  let flushAgain = false
  let disposed = false

  const foreign = isForeignOrigin

  const send = async (body: ProvenanceRecord): Promise<boolean> => {
    const at = scope
    try {
      await post({ task_id: body.task_id, tool: body.tool, session_id: body.session_id })
      if (at !== scope || disposed) {
        return false // the backend changed under us: check ownership again with the next one
      }
      await post(body)
      return true
    } catch {
      return false
    }
  }

  const keep = (row: { body: ProvenanceRecord; origin?: EventOrigin }) => {
    if (!disposed) {
      pending = [...pending, row].slice(-200)
    }
  }

  const attempt = (row: { body: ProvenanceRecord; origin?: EventOrigin }, honorTags: boolean) => {
    if (honorTags && foreign(row.origin)) {
      keep(row)
      return
    }
    inflight += 1
    void send(row.body).then(ok => {
      inflight -= 1
      if (!ok) {
        keep(row)
      }
      if (inflight === 0 && flushAgain && !disposed) {
        flushAgain = false
        flush()
      }
    })
  }

  const flush = () => {
    if (inflight > 0) {
      flushAgain = true // a send started under the old scope may still be refused: retry once it settles
    }
    const rows = pending
    pending = []
    rows.forEach(row => attempt(row, false))
  }

  const onScope = () => {
    scope += 1
    clearTimeout(timer)
    // A tick later: core re-points ctx.rest from its own subscription to the same atoms.
    timer = setTimeout(flush, 0)
  }
  const offs = [host.state?.profile?.subscribe(onScope), host.state?.connectionId?.subscribe(onScope)]
  return {
    record(body: ProvenanceRecord, origin?: EventOrigin) {
      attempt({ body, origin }, true)
    },
    dispose() {
      disposed = true
      clearTimeout(timer)
      offs.forEach(off => off?.())
      pending = []
    }
  }
}
