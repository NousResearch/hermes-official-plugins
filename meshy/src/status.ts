/**
 * The credit balance and the Settings ▸ Plugins ▸ Meshy preferences, both read from the backend's
 * `GET /status`. Pure (no SDK, no React) so the node tests can drive it with a fake `rest`.
 *
 * Both belong to the profile `ctx.rest` is routed to. `rescope()` runs when the user switches
 * profile or connection: it forgets the old profile's values and refetches, and a reply that was
 * still in flight for the old profile is dropped instead of painting over the new one.
 */

export interface MeshySettings {
  show_credits: boolean
  inline_viewer: boolean
  auto_rotate: boolean
  gallery_limit: number
}

/** Mirrors plugin.json's config_schema defaults until the backend answers. */
export const DEFAULT_SETTINGS: MeshySettings = { show_credits: true, inline_viewer: true, auto_rotate: true, gallery_limit: 24 }

export interface Balance {
  configured: boolean
  balance?: number
  key_env?: string
  error?: string
  /** Why there is no balance; `error` quotes Meshy's own 403 text, so never pattern-match that. */
  error_code?: 'key_rejected' | 'offline' | 'refused'
  settings?: Partial<MeshySettings>
}

/** The chip's word for a balance: credits, or why there are none. */
export function balanceLabel(balance: Balance): string {
  if (!balance.configured) {
    return 'no key'
  }
  if (!balance.error) {
    return `${(balance.balance ?? 0).toLocaleString()} cr`
  }
  // A backend without error_code: only its exact 401 wording means the key.
  const code = balance.error_code ?? (balance.error === 'Meshy rejected the API key' ? 'key_rejected' : balance.error.startsWith('Meshy refused') ? 'refused' : 'offline')
  return { key_rejected: 'key rejected', refused: 'refused', offline: 'offline' }[code] ?? 'offline'
}

import { type ScopeState, watchScope } from './scope.ts'

type StatusRest = <T>(path: string, opts?: { timeoutMs?: number }) => Promise<T>

export interface StatusStore {
  balance: () => Balance | null
  settings: () => MeshySettings
  onBalance: (fn: (b: Balance | null) => void) => () => void
  onSettings: (fn: (s: MeshySettings) => void) => () => void
  /** Re-read /status for the current scope (after a Meshy tool call, a settings save). */
  refresh: () => Promise<void>
  /** The backend scope changed (profile or connection switch): drop the old values, then refresh. Runs by
   *  itself when the store was given `host.state`. */
  rescope: () => Promise<void>
  dispose: () => void
}

/** `state` is `host.state` (newer Desktops): the store re-reads /status on every profile/connection switch.
 *  Without it (older Desktops) the store keeps its single backend scope, as before. */
export function createStatusStore(rest: StatusRest, state?: ScopeState): StatusStore {
  let balance: Balance | null = null
  let settings: MeshySettings = DEFAULT_SETTINGS
  // `scope` invalidates replies from before a switch; `issued`/`applied` keep an older reply in the same
  // scope from overwriting a newer one that landed first.
  let scope = 0
  let issued = 0
  let applied = 0
  const balanceListeners = new Set<(b: Balance | null) => void>()
  const settingsListeners = new Set<(s: MeshySettings) => void>()
  const emitBalance = () => balanceListeners.forEach(fn => fn(balance))
  const emitSettings = () => settingsListeners.forEach(fn => fn(settings))

  const refresh = () => {
    const mine = scope
    const request = ++issued
    return rest<Balance>('status', { timeoutMs: 20_000 })
      .then(b => {
        if (mine !== scope || request < applied) {
          return
        }
        applied = request
        balance = b
        emitBalance()
        if (b.settings) {
          settings = { ...DEFAULT_SETTINGS, ...b.settings }
          emitSettings()
        }
      })
      .catch(() => undefined) // backend not mounted yet (plugin just enabled) or Meshy down: keep the last value
  }

  const rescope = () => {
    scope += 1
    // The old profile's credits and preferences are wrong for the new one: show nothing / the schema
    // defaults until its own /status answers, never the previous profile's numbers.
    balance = null
    settings = DEFAULT_SETTINGS
    emitBalance()
    emitSettings()
    return refresh()
  }
  const stopWatching = watchScope(state, () => void rescope())

  return {
    balance: () => balance,
    settings: () => settings,
    onBalance(fn) {
      balanceListeners.add(fn)
      return () => {
        balanceListeners.delete(fn)
      }
    },
    onSettings(fn) {
      settingsListeners.add(fn)
      return () => {
        settingsListeners.delete(fn)
      }
    },
    refresh,
    rescope,
    dispose() {
      stopWatching()
      scope += 1
      balanceListeners.clear()
      settingsListeners.clear()
    }
  }
}
