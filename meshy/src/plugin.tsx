/**
 * Meshy for Hermes Desktop: view what the Meshy MCP server makes without leaving the app.
 *
 * - `::meshy-model{path="/abs/x.glb"}` (or `url="https://assets.meshy.ai/..."`) renders an inline
 *   3D card in the transcript. The meshy-3d skill tells the agent to emit it after a download.
 * - "Meshy: open 3D gallery" (⌘K) opens a workspace tab listing recent generations and local
 *   downloads beside a full viewer.
 * - A status-bar chip shows remaining credits, refreshed after every Meshy tool call; while a Meshy
 *   task runs it shows live progress instead ("Meshy 42% · texturing", see progress.tsx).
 *
 * Bytes and API calls go through this plugin's own backend (`dashboard/plugin_api.py`) via
 * `ctx.rest`; the renderer never reads files or holds the API key.
 */

import type { HermesPlugin, PaletteContribution } from '@hermes/plugin-sdk'
import { host, PALETTE_AREA, STATUSBAR_AREAS, TRANSCRIPT_DIRECTIVE_AREA } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'

import { createProgressTracker, isMeshyCall, ProgressChip, type ProgressState } from './progress.tsx'
import { createProvenanceWriter, creationsFrom } from './provenance.ts'
import { type Balance, BalanceChip, DEFAULT_SETTINGS, Gallery, type MeshySettings, type Source, TranscriptCard } from './views.tsx'

const WORKSPACE_ID = 'meshy-gallery'
// Settings ▸ Plugins ▸ Meshy is generated from plugin.json's config_schema (agent key `meshy`).
const SETTINGS_HREF = '/settings?tab=plugins&agent=meshy'

const plugin: HermesPlugin = {
  id: 'meshy',
  name: 'Meshy 3D',
  description: 'View Meshy 3D models inline in chat and in a gallery tab, with your credit balance in the status bar.',
  register(ctx) {
    let balance: Balance | null = null
    let settings: MeshySettings = DEFAULT_SETTINGS
    const listeners = new Set<(b: Balance | null) => void>()
    const settingsListeners = new Set<(s: MeshySettings) => void>()
    const useSettings = () => {
      const [value, setValue] = useState<MeshySettings>(settings)
      useEffect(() => {
        settingsListeners.add(setValue)
        return () => {
          settingsListeners.delete(setValue)
        }
      }, [])
      return value
    }
    let pendingSource: null | Source = null
    const galleryListeners = new Set<(s: null | Source) => void>()
    let closeWorkspace: (() => void) | null = null

    const refreshBalance = () => {
      ctx
        .rest<Balance>('status', { timeoutMs: 20_000 })
        .then(b => {
          balance = b
          listeners.forEach(fn => fn(b))
          if (b.settings) {
            settings = { ...DEFAULT_SETTINGS, ...b.settings }
            settingsListeners.forEach(fn => fn(settings))
          }
        })
        .catch(() => undefined) // backend not mounted yet (plugin just enabled) or Meshy down: keep the last value
    }

    function GalleryHost() {
      const [source, setSource] = useState<null | Source>(pendingSource)
      useEffect(() => {
        galleryListeners.add(setSource)
        return () => {
          galleryListeners.delete(setSource)
        }
      }, [])
      const prefs = useSettings()
      return (
        <Gallery
          autoRotate={prefs.auto_rotate}
          initial={source}
          onOpenSettings={() => host.navigate(SETTINGS_HREF)}
          os={ctx.os}
          rest={ctx.rest}
        />
      )
    }

    const openGallery = (source: null | Source = null, title = 'Meshy 3D') => {
      pendingSource = source
      galleryListeners.forEach(fn => fn(source))
      if (typeof host.openWorkspace !== 'function') {
        host.notify({ kind: 'error', message: 'Update Hermes Desktop to open the Meshy gallery.' })
        return
      }
      closeWorkspace = host.openWorkspace(WORKSPACE_ID, {
        title: source ? `Meshy · ${title}` : title,
        minWidth: '28rem',
        onClose: () => {
          closeWorkspace = null
        },
        render: () => <GalleryHost />
      })
    }

    const progressListeners = new Set<(s: ProgressState) => void>()
    // A finished task changes the balance (credits are charged at creation, refunded on failure).
    const progress = createProgressTracker(ctx, s => progressListeners.forEach(fn => fn(s)), refreshBalance)

    function Chip() {
      const prefs = useSettings()
      const [b, setB] = useState<Balance | null>(balance)
      const [p, setP] = useState<ProgressState>(progress.get())
      useEffect(() => {
        listeners.add(setB)
        progressListeners.add(setP)
        return () => {
          listeners.delete(setB)
          progressListeners.delete(setP)
        }
      }, [])
      if (p.running || p.tasks.length > 0) {
        return <ProgressChip onOpen={() => openGallery()} state={p} />
      }
      if (!prefs.show_credits) {
        return null
      }
      return <BalanceChip balance={b} onOpen={() => openGallery()} />
    }

    const provenance = createProvenanceWriter(body => ctx.rest('provenance', { method: 'POST', body }))
    ctx.onDispose(provenance.dispose)

    ctx.onEvent('tool.complete', event => {
      const payload = (event.payload ?? {}) as { name?: unknown; args?: unknown; result?: unknown }
      // Meshy tools sit behind tool search: most calls arrive as `tool_call` wrapping mcp_meshy_*.
      if (isMeshyCall(payload.name, payload.args)) {
        refreshBalance()
        for (const rec of creationsFrom(payload.name, payload.args, payload.result)) {
          // The renderer's source tags let a completion from a background profile/connection wait locally.
          provenance.record({ ...rec, session_id: event.session_id }, { profile: event.profile, connectionId: event.connectionId })
        }
      }
    })
    function DirectiveCard({ attrs, source, streaming }: { attrs: Readonly<Record<string, string>>; source: string; streaming: boolean }) {
      const prefs = useSettings()
      if (!prefs.inline_viewer) {
        return <p>{source}</p>
      }
      return (
        <TranscriptCard
          attrs={attrs}
          openInWorkspace={(src, title) => openGallery(src, title)}
          os={ctx.os}
          rest={ctx.rest}
          streaming={streaming}
        />
      )
    }

    // Saved from Settings ▸ Plugins ▸ Meshy (a new API key, a toggle): re-read and apply live.
    ctx.onEvent('plugin.meshy.settings.changed', () => refreshBalance())
    ctx.onDispose(() => {
      closeWorkspace?.()
      progress.dispose()
      progressListeners.clear()
      listeners.clear()
      galleryListeners.clear()
      settingsListeners.clear()
    })
    refreshBalance()

    ctx.registerMany([
      {
        id: 'model-directive',
        area: TRANSCRIPT_DIRECTIVE_AREA,
        data: {
          name: 'meshy-model',
          render: ({ attrs, source, streaming }: { attrs: Readonly<Record<string, string>>; source: string; streaming: boolean }) => (
            <DirectiveCard attrs={attrs} source={source} streaming={streaming} />
          )
        }
      },
      {
        id: 'balance',
        area: STATUSBAR_AREAS.right,
        order: 80,
        render: () => <Chip />
      },
      {
        id: 'open-gallery',
        area: PALETTE_AREA,
        data: {
          id: 'meshy.gallery',
          label: 'Meshy: open 3D gallery',
          keywords: ['meshy', '3d', 'model', 'glb', 'gallery', 'viewer'],
          run: () => openGallery()
        } satisfies PaletteContribution
      },
      {
        id: 'open-settings',
        area: PALETTE_AREA,
        data: {
          id: 'meshy.settings',
          label: 'Meshy: settings',
          keywords: ['meshy', 'settings', 'api key', 'credits', 'preferences'],
          run: () => host.navigate(SETTINGS_HREF)
        } satisfies PaletteContribution
      },
      {
        id: 'open-file',
        area: PALETTE_AREA,
        data: {
          id: 'meshy.open-file',
          label: 'Meshy: view a .glb file…',
          keywords: ['meshy', '3d', 'glb', 'gltf', 'open', 'file'],
          run: () => {
            void ctx.os
              .pickOpenPath({ title: 'Open a 3D model', filters: [{ name: '3D models (.glb, self-contained .gltf)', extensions: ['glb', 'gltf'] }] })
              .then(path => {
                if (path) {
                  openGallery({ kind: 'file', path }, path.split(/[\\/]/).pop() || 'model')
                }
              })
          }
        } satisfies PaletteContribution
      }
    ])
  }
}

export default plugin
