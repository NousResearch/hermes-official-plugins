/**
 * When a task card asks the backend about its Meshy task again. Pure (no SDK import) so node tests can drive it
 * with fake clocks and timers.
 *
 * - A running task (PENDING / IN_PROGRESS) is re-read every 3 s; any other status ends polling.
 * - A permanent refusal stops at once and leaves a manual Retry: every 4xx except 404, 408 and 429 (a missing API
 *   key, a malformed id). 408/429 are "try later" by definition, so they back off like a 5xx.
 * - 404 ("Meshy has no finished task with that id") is permanent too, except for a task created moments ago, which
 *   can 404 before Meshy lists it. The card keeps retrying a 404 only during EARLY_404_MS after it first mounted.
 *   90 s covers that create-to-listed lag (seconds) plus a slow first poll with a wide margin, and is short enough
 *   that a dead id (deleted task, wrong account, a typo in the directive) stops within a minute and a half instead of
 *   costing a backend round trip (several Meshy calls) every 12 s for as long as the chat stays open.
 * - 5xx, timeouts and network failures are transient: keep polling, backing off 3 s → 6 → 12 … capped at 60 s.
 */

export const RUNNING = new Set(['PENDING', 'IN_PROGRESS'])
export const POLL_MS = 3000
export const MAX_BACKOFF_MS = 60_000
export const EARLY_404_MS = 90_000

/** HTTP status a `ctx.rest` rejection carries, or null (network failure, timeout, no status).
 *  Electron's transport sets `statusCode` in the main process, but the IPC bridge keeps only the message, which
 *  reads `"<status>: <body>"`, wrapped by `ipcRenderer.invoke` as `"Error invoking remote method 'hermes:api':
 *  Error: <status>: <body>"`. Read the structured field when present, else the message prefix. */
export function httpStatus(err: unknown): null | number {
  if (err && typeof err === 'object') {
    for (const key of ['statusCode', 'status'] as const) {
      const value = (err as Record<string, unknown>)[key]
      if (typeof value === 'number' && Number.isInteger(value) && value >= 100 && value <= 599) {
        return value
      }
    }
  }
  const message = err instanceof Error ? err.message : typeof err === 'string' ? err : ''
  const match = /^(?:Error invoking remote method '[^']*':\s*)?(?:Error:\s*)?([1-5]\d\d):/.exec(message)
  return match ? Number(match[1]) : null
}

export type PollFailure = 'permanent' | 'transient'

/** Whether a failed read is worth repeating. `sinceMount` = ms since the card first mounted. */
export function classifyFailure(status: null | number, sinceMount: number): PollFailure {
  if (status === null || status >= 500 || status === 408 || status === 429) {
    return 'transient'
  }
  if (status === 404) {
    return sinceMount < EARLY_404_MS ? 'transient' : 'permanent'
  }
  return status >= 400 ? 'permanent' : 'transient'
}

/** Delay before the next try after `failures` failed reads in a row (1 = the first failure). */
export function backoffMs(failures: number): number {
  return Math.min(MAX_BACKOFF_MS, POLL_MS * 2 ** Math.max(0, failures - 1))
}

export interface PollState<T> {
  info: null | T
  error: null | string
  /** True once polling has stopped on an error; the card offers Retry. */
  stopped: boolean
}

export interface PollClock {
  now: () => number
  setTimeout: (fn: () => void, ms: number) => unknown
  clearTimeout: (handle: unknown) => void
}

const realClock: PollClock = {
  now: () => Date.now(),
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: handle => clearTimeout(handle as ReturnType<typeof setTimeout>)
}

/** Polls `read()` under the policy above and reports each state. `retry()` runs one fresh read after a stop (the
 *  early-404 window still counts from the first mount). `dispose()` ends everything. */
export function createTaskPoller<T extends { status: string }>(
  read: () => Promise<T>,
  onState: (state: PollState<T>) => void,
  describe: (err: unknown) => string,
  clock: PollClock = realClock
): { retry: () => void; dispose: () => void } {
  const mountedAt = clock.now()
  let live = true
  let timer: unknown
  let inFlight = false
  let failures = 0
  let state: PollState<T> = { info: null, error: null, stopped: false }

  const publish = (next: PollState<T>) => {
    state = next
    onState(state)
  }
  const schedule = (ms: number) => {
    timer = clock.setTimeout(tick, ms)
  }

  function tick() {
    timer = undefined
    inFlight = true
    read().then(
      r => {
        inFlight = false
        if (!live) return
        failures = 0
        publish({ info: r, error: null, stopped: false })
        if (RUNNING.has(r.status)) schedule(POLL_MS)
      },
      (err: unknown) => {
        inFlight = false
        if (!live) return
        failures += 1
        const verdict = classifyFailure(httpStatus(err), clock.now() - mountedAt)
        publish({ info: state.info, error: describe(err), stopped: verdict === 'permanent' })
        if (verdict === 'transient') schedule(backoffMs(failures))
      }
    )
  }

  tick()
  return {
    retry() {
      if (!live || inFlight || timer !== undefined) return
      publish({ ...state, stopped: false })
      tick()
    },
    dispose() {
      live = false
      if (timer !== undefined) clock.clearTimeout(timer)
      timer = undefined
    }
  }
}
