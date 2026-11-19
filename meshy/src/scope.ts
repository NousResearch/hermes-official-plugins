/**
 * Which backend `ctx.rest` talks to, and whether it runs on this computer. Pure (no SDK import): the
 * plugin passes `host` in, the node tests pass a fake with nanostore-like atoms.
 *
 * Newer Desktops expose `host.state.profile` / `host.state.connectionId` (read-only atoms) and
 * `host.connections()` (registry rows with a `kind`). Older ones have neither: every helper then
 * keeps the old behaviour (one fixed backend, assumed local).
 */

export interface AtomLike<T> {
  get: () => T
  subscribe?: (fn: (value: T) => void) => () => void
}

export interface ScopeState {
  profile?: AtomLike<string>
  connectionId?: AtomLike<null | string>
}

export interface ConnectionRow {
  id: string
  kind?: string
  label?: string
}

export interface ScopeHost {
  state?: ScopeState
  connections?: () => Promise<ConnectionRow[]>
}

const read = <T>(atom: AtomLike<T> | undefined): T | undefined => {
  try {
    return typeof atom?.get === 'function' ? atom.get() : undefined
  } catch {
    return undefined
  }
}

const profileName = (p: unknown) => String(p ?? '').trim() || 'default'

/** One string per (connection, profile): equal keys mean `ctx.rest` still reaches the same backend scope. */
export function scopeKey(state: ScopeState | undefined): string {
  return `${read(state?.connectionId) ?? ''}\n${profileName(read(state?.profile))}`
}

/** The scopeKey of the backend a gateway event came from. Desktop tags each event with its `profile`, and with its
 *  `connectionId` only off the primary connection, so a profile tag without a connection means the primary one. An
 *  untagged event (older Desktop) belongs to whichever scope is in front as it arrives. */
export function eventScopeKey(origin: { profile?: string; connectionId?: string } | undefined, state: ScopeState | undefined): string {
  if (!origin?.profile && !origin?.connectionId) {
    return scopeKey(state)
  }
  const connection = origin.connectionId ?? (origin.profile ? '' : (read(state?.connectionId) ?? ''))
  return `${connection}\n${profileName(origin.profile ?? read(state?.profile))}`
}

/** Calls `onChange` once per real profile/connection switch (not on subscribe, not on a repeated value).
 *  No atoms (older Desktop): never calls it, which is the old single-backend behaviour. */
export function watchScope(state: ScopeState | undefined, onChange: (key: string) => void): () => void {
  if (!state) {
    return () => undefined
  }
  let last = scopeKey(state)
  const check = () => {
    const key = scopeKey(state)
    if (key !== last) {
      last = key
      onChange(key)
    }
  }
  const unsubscribe: Array<() => void> = []
  for (const atom of [state.profile, state.connectionId]) {
    if (typeof atom?.subscribe === 'function') {
      try {
        unsubscribe.push(atom.subscribe(check))
      } catch {
        // an atom that cannot be observed: that half of the scope stays fixed
      }
    }
  }
  return () => unsubscribe.forEach(fn => fn())
}

export interface Locality {
  /** `unknown`: an older Desktop that cannot say; callers keep the old (local) behaviour. */
  kind: 'local' | 'remote' | 'unknown'
  /** The connection's label in Desktop ("Homelab"), when the registry has one. */
  label?: string
}

/** Local = the backend runs on this computer, so its paths are this computer's paths. The primary path
 *  (`connectionId` null) and the `local` registry entry are local; any other source (remote, ssh, cloud)
 *  is another machine unless the registry row says `kind: 'local'`. */
export function localityOf(state: ScopeState | undefined, rows: readonly ConnectionRow[] | null): Locality {
  if (typeof state?.connectionId?.get !== 'function') {
    return { kind: 'unknown' }
  }
  const id = read(state.connectionId)
  if (id === undefined) {
    return { kind: 'unknown' }
  }
  if (!id || id === 'local') {
    return { kind: 'local' }
  }
  const row = rows?.find(r => r.id === id)
  if (row?.kind === 'local') {
    return { kind: 'local', label: row.label }
  }
  return { kind: 'remote', label: row?.label }
}

export interface LocalityTracker {
  get: () => Locality
  subscribe: (fn: (l: Locality) => void) => () => void
  /** Recompute now and re-read the connection registry (labels/kinds); call on a scope switch. */
  refresh: () => Promise<void>
}

export function createLocalityTracker(host: ScopeHost): LocalityTracker {
  let rows: ConnectionRow[] | null = null
  let current = localityOf(host.state, rows)
  const listeners = new Set<(l: Locality) => void>()
  const recompute = () => {
    const next = localityOf(host.state, rows)
    if (next.kind !== current.kind || next.label !== current.label) {
      current = next
      listeners.forEach(fn => fn(current))
    }
  }
  return {
    get: () => current,
    subscribe(fn) {
      listeners.add(fn)
      return () => {
        listeners.delete(fn)
      }
    },
    refresh() {
      recompute()
      if (typeof host.connections !== 'function') {
        return Promise.resolve()
      }
      return Promise.resolve()
        .then(() => host.connections!())
        .then(list => {
          rows = Array.isArray(list) ? list : null
          recompute()
        })
        .catch(() => undefined) // registry unreadable: the id-based answer stands
    }
  }
}

export interface RevealOs {
  revealPath: (path: string) => Promise<boolean>
  writeClipboard?: (text: string) => Promise<boolean>
}

export type Notify = (input: { kind: 'error' | 'info' | 'success' | 'warning'; message: string }) => unknown

const copyText = async (os: RevealOs, text: string): Promise<boolean> => {
  try {
    if (typeof os.writeClipboard === 'function') {
      return await os.writeClipboard(text)
    }
    if (typeof navigator !== 'undefined' && navigator.clipboard) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // fall through: the path is still in the notice
  }
  return false
}

/** `ctx.os` with a `revealPath` that never fails silently. Backend paths are the BACKEND's paths: on a
 *  remote backend it does not ask this computer's file manager at all (the same path could name an
 *  unrelated local file) and says where the file is instead, with the path copied. When the file manager
 *  refuses (no shell, file gone) it says so the same way. Every view's "Show in folder" goes through it. */
export function locatingOs<T extends RevealOs>(os: T, locality: () => Locality, notify: Notify): T {
  const explain = async (path: string, where: Locality) => {
    const copied = await copyText(os, path)
    const text =
      where.kind === 'remote'
        ? `This file is on ${where.label || 'the Hermes host'}, not on this computer: ${path}`
        : where.kind === 'local'
          ? `The file manager could not show it: ${path}`
          : `Could not show it here. It is at ${path} on the machine Hermes runs on`
    notify({ kind: where.kind === 'local' ? 'warning' : 'info', message: `${text}${copied ? ' (path copied)' : ''}` })
  }
  return {
    ...os,
    revealPath: async (path: string) => {
      const where = locality()
      if (where.kind === 'remote') {
        await explain(path, where)
        return false
      }
      let shown = false
      try {
        shown = await os.revealPath(path)
      } catch {
        shown = false
      }
      if (!shown) {
        await explain(path, where)
      }
      return shown
    }
  }
}
