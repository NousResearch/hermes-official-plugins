/**
 * Live progress for in-flight Meshy tasks: the status-bar chip reads "Meshy 42% · refining" while a
 * generation runs instead of the bare "tool running" row.
 *
 * Two sources, merged:
 *  1. Gateway progress events: a host that forwards MCP `notifications/progress` sends a
 *     `tool.progress`-style event ({name | tool_id, progress, total?, message?}). Used when present.
 *  2. Fallback: the plugin backend's GET /active (Meshy's own task list: PENDING / IN_PROGRESS + %).
 *     Polled every few seconds ONLY while a Meshy tool runs or a task is still in flight; idle = no polls.
 *
 * Meshy tools sit behind tool search, so a call usually arrives as `tool_call` with
 * `args.calls[].name = mcp_meshy_*`, not as a top-level `mcp_meshy_*` name; both are recognised.
 */

import type { GatewayEvent, PluginContext } from '@hermes/plugin-sdk'
import { Codicon } from '@hermes/plugin-sdk'
import type { CSSProperties } from 'react'

export interface ActiveTask {
  task_id: string
  task_type: string
  stage: string
  status: string
  progress: number
  prompt: string
}

export interface ProgressState {
  /** A Meshy tool call is running right now. */
  running: boolean
  /** In-flight tasks from Meshy (newest first). */
  tasks: ActiveTask[]
  /** Latest pushed progress from the gateway, if the host forwards MCP progress. */
  live: { progress: number; message: string } | null
}

const POLL_MS = 4000
// Meshy lists a task a moment after the create call returns; keep polling briefly after the last tool.
const GRACE_MS = 15_000
const PROGRESS_EVENTS = ['tool.progress', 'mcp.progress', 'tool.progress.update']

const isMeshyName = (name: unknown) => {
  const n = String(name ?? '')
  return n.startsWith('mcp__meshy__') || n.startsWith('mcp_meshy_') || n.startsWith('meshy_')
}

/** True for a direct Meshy tool name, or a `tool_call` bridge whose calls include one. */
export function isMeshyCall(name: unknown, args: unknown): boolean {
  if (isMeshyName(name)) {
    return true
  }
  if (String(name ?? '') !== 'tool_call' || !args || typeof args !== 'object') {
    return false
  }
  const a = args as { calls?: unknown; name?: unknown }
  const calls = Array.isArray(a.calls) ? a.calls : [a]
  return calls.some(c => !!c && typeof c === 'object' && isMeshyName((c as { name?: unknown }).name))
}

const STAGE_WORDS: Record<string, string> = {
  preview: 'previewing',
  refine: 'texturing',
  'text-to-3d-preview': 'previewing',
  'text-to-3d-refine': 'texturing',
  'image-to-3d': 'modelling',
  'multi-image-to-3d': 'modelling',
  remesh: 'remeshing',
  retexture: 'retexturing',
  rig: 'rigging',
  rigging: 'rigging',
  animation: 'animating',
  'text-to-image': 'drawing'
}

export const stageWord = (t: ActiveTask) => STAGE_WORDS[t.stage] ?? STAGE_WORDS[t.task_type] ?? t.stage.replace(/-/g, ' ')

export function createProgressTracker(ctx: Pick<PluginContext, 'onEvent' | 'rest'>, onChange: (s: ProgressState) => void, onIdle: () => void) {
  let state: ProgressState = { running: false, tasks: [], live: null }
  const runningIds = new Set<string>()
  let lastToolAt = 0
  let timer: ReturnType<typeof setTimeout> | null = null
  let inFlight = false
  let disposed = false

  const set = (patch: Partial<ProgressState>) => {
    const wasBusy = state.running || state.tasks.length > 0
    state = { ...state, ...patch }
    onChange(state)
    if (wasBusy && !state.running && state.tasks.length === 0) {
      onIdle()
    }
  }

  const wanted = () => !disposed && (runningIds.size > 0 || state.tasks.length > 0 || Date.now() - lastToolAt < GRACE_MS)

  const poll = async () => {
    timer = null
    if (inFlight || !wanted()) {
      return
    }
    inFlight = true
    try {
      const res = await ctx.rest<{ tasks?: ActiveTask[] }>('active', { timeoutMs: 15_000 })
      if (!disposed) {
        set({ tasks: Array.isArray(res.tasks) ? res.tasks : [] })
      }
    } catch {
      // backend not mounted / Meshy unreachable: keep the last value, try again next tick
    } finally {
      inFlight = false
      schedule()
    }
  }

  const schedule = (delay = POLL_MS) => {
    if (timer || !wanted()) {
      return
    }
    timer = setTimeout(() => void poll(), delay)
  }

  const payloadOf = (e: GatewayEvent) => (e.payload ?? {}) as Record<string, unknown>

  const offs = [
    ctx.onEvent('tool.start', e => {
      const p = payloadOf(e)
      if (!isMeshyCall(p.name, p.args)) {
        return
      }
      runningIds.add(String(p.tool_id ?? p.name))
      lastToolAt = Date.now()
      set({ running: true })
      if (timer) {
        clearTimeout(timer)
        timer = null
      }
      schedule(1500)
    }),
    ctx.onEvent('tool.complete', e => {
      const p = payloadOf(e)
      const id = String(p.tool_id ?? p.name)
      if (!runningIds.delete(id) && !isMeshyCall(p.name, p.args)) {
        return
      }
      lastToolAt = Date.now()
      set({ running: runningIds.size > 0, live: runningIds.size > 0 ? state.live : null })
      schedule(1000)
    }),
    ...PROGRESS_EVENTS.map(type =>
      ctx.onEvent(type, e => {
        const p = payloadOf(e)
        const ours = runningIds.has(String(p.tool_id ?? '')) || isMeshyName(p.name) || isMeshyCall(p.name, p.args)
        const raw = Number(p.progress)
        if (!ours || !Number.isFinite(raw)) {
          return
        }
        const total = Number(p.total)
        const pct = Number.isFinite(total) && total > 0 ? (raw / total) * 100 : raw
        set({ live: { progress: Math.max(0, Math.min(100, Math.round(pct))), message: String(p.message ?? '') } })
      })
    )
  ]

  // A task still running from before this window opened (or another client) shows up once on load.
  lastToolAt = Date.now()
  schedule(500)

  return {
    get: () => state,
    dispose() {
      disposed = true
      if (timer) {
        clearTimeout(timer)
      }
      offs.forEach(off => off())
    }
  }
}

const S = {
  chip: { display: 'inline-flex', alignItems: 'center', gap: 5, whiteSpace: 'nowrap', flexShrink: 0, border: 0, background: 'transparent', color: 'inherit', font: 'inherit', cursor: 'pointer', padding: '0 4px' },
  track: { display: 'inline-block', flexShrink: 0, width: 42, height: 4, borderRadius: 2, background: 'color-mix(in srgb, currentColor 18%, transparent)', overflow: 'hidden' },
  fill: { display: 'block', height: '100%', borderRadius: 2, background: 'var(--accent, currentColor)', transition: 'width .6s ease' }
} satisfies Record<string, CSSProperties>

/** Busy chip: "Meshy 42% · texturing" with a thin bar; tooltip lists every in-flight task. */
export function ProgressChip({ state, onOpen }: { state: ProgressState; onOpen: () => void }) {
  const top = state.tasks[0]
  const pct = top ? top.progress : state.live?.progress
  const word = top ? stageWord(top) : state.live?.message ? state.live.message.split(' - ')[0].toLowerCase() : 'working'
  const label = top?.status === 'PENDING' && !pct ? 'queued' : pct === undefined ? word : `${pct}% · ${word}`
  const more = state.tasks.length > 1 ? ` +${state.tasks.length - 1}` : ''
  const title = state.tasks.length
    ? state.tasks.map(t => `${stageWord(t)} ${t.progress}% — ${t.prompt || t.task_id}`).join('\n')
    : 'Meshy tool running'
  return (
    <button data-meshy-progress={pct ?? ''} onClick={onOpen} style={S.chip} title={title} type="button">
      <Codicon name="loading" spinning />
      <span>
        Meshy {label}
        {more}
      </span>
      {pct !== undefined && (
        <span style={S.track}>
          <span style={{ ...S.fill, width: `${pct}%` }} />
        </span>
      )}
    </button>
  )
}
