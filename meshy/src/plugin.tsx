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

import type { HermesPlugin, PaletteContribution, TranscriptDirectiveProps } from '@hermes/plugin-sdk'
import { host, PALETTE_AREA, STATUSBAR_AREAS, TRANSCRIPT_DIRECTIVE_AREA } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'

import { PICKABLE_EXTENSIONS } from './formats.ts'
import { createProgressTracker, isMeshyCall, type ProgressState } from './active.ts'
import { ProgressChip } from './progress.tsx'
import { createProvenanceWriter, creationsFrom, isForeignOrigin } from './provenance.ts'
import { createLocalityTracker, eventScopeKey, type Locality, locatingOs, scopeKey, watchScope } from './scope.ts'
import { type Balance, createStatusStore, type MeshySettings } from './status.ts'
import { uploadModel } from './upload.ts'
import { BalanceChip, errorText, Gallery, type Source, TranscriptCard } from './views.tsx'

const WORKSPACE_ID = 'meshy-gallery'
// Settings ▸ Plugins ▸ Meshy is generated from plugin.json's config_schema (agent key `meshy`).
const SETTINGS_HREF = '/settings?tab=plugins&agent=meshy'

const plugin: HermesPlugin = {
  id: 'meshy',
  name: 'Meshy 3D',
  description: 'View Meshy 3D models inline in chat and in a gallery tab, with your credit balance in the status bar.',
  register(ctx) {
    // Credits + preferences belong to the profile ctx.rest is routed to: the store re-reads them on a
    // profile/connection switch (status.ts).
    const status = createStatusStore(ctx.rest, host.state)
    const refreshBalance = () => void status.refresh()
    const useSettings = () => {
      const [value, setValue] = useState<MeshySettings>(status.settings())
      useEffect(() => status.onSettings(setValue), [])
      return value
    }
    // Backend paths are the backend machine's: on a remote one, "Show in folder" says where the file is
    // instead of opening this computer's file manager (scope.ts::locatingOs).
    const locality = createLocalityTracker(host)
    const os = locatingOs(ctx.os, locality.get, input => host.notify(input))
    let pendingSource: null | Source = null
    const galleryListeners = new Set<(s: null | Source) => void>()
    let closeWorkspace: (() => void) | null = null
    let scope = scopeKey(host.state)
    const scopeListeners = new Set<(key: string) => void>()

    // A profile or connection switch moves ctx.rest to another backend scope: reload an open gallery (its
    // lists and selection were the old profile's) and re-check where the backend runs. Older Desktops have
    // no host.state: nothing fires and the plugin keeps its one fixed backend.
    ctx.onDispose(
      watchScope(host.state, key => {
        scope = key
        void locality.refresh()
        pendingSource = null
        galleryListeners.forEach(fn => fn(null))
        scopeListeners.forEach(fn => fn(key))
        progress.rescope()
      })
    )
    void locality.refresh()

    function GalleryHost() {
      const [source, setSource] = useState<null | Source>(pendingSource)
      const [where, setWhere] = useState<Locality>(locality.get())
      const [scoped, setScoped] = useState(scope)
      useEffect(() => {
        galleryListeners.add(setSource)
        scopeListeners.add(setScoped)
        const stopLocality = locality.subscribe(setWhere)
        return () => {
          galleryListeners.delete(setSource)
          scopeListeners.delete(setScoped)
          stopLocality()
        }
      }, [])
      const prefs = useSettings()
      return (
        <Gallery
          autoRotate={prefs.auto_rotate}
          initial={source}
          key={scoped}
          locality={where}
          onOpenSettings={() => host.navigate(SETTINGS_HREF)}
          os={os}
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
        // Beside the chat, not over it: gallery buttons send to (and may open) the chat that made a model, which
        // must stay visible next to the gallery. Desktops before the dock option ignore it.
        dock: { pane: 'workspace', pos: 'right' },
        title: source ? `Meshy · ${title}` : title,
        minWidth: '28rem',
        onClose: () => {
          closeWorkspace = null
        },
        render: () => <GalleryHost />
      })
    }

    // The browser's own file input (a detached element of this plugin, not host DOM): it hands back the
    // file's bytes, which ctx.os.pickOpenPath (a path) cannot do.
    const pickAndUpload = () => {
      const input = document.createElement('input')
      input.type = 'file'
      input.accept = PICKABLE_EXTENSIONS.map(ext => `.${ext}`).join(',')
      input.addEventListener(
        'change',
        () => {
          const file = input.files?.[0]
          if (!file) {
            return
          }
          const startedIn = scope
          host.notify({ kind: 'info', message: `Sending ${file.name} to ${locality.get().label || 'the Hermes host'}…` })
          void file
            .arrayBuffer()
            .then(bytes => uploadModel(ctx.rest, { name: file.name, bytes, type: file.type }))
            .then(saved => {
              // The path is the old backend's if the user switched profile/connection meanwhile.
              if (scope !== startedIn) {
                host.notify({ kind: 'warning', message: `${file.name} was sent before you switched; open it from that profile's gallery` })
                return
              }
              openGallery({ kind: 'file', path: saved.path }, saved.name)
            })
            .catch((err: unknown) => host.notify({ kind: 'error', message: `Could not send ${file.name}: ${errorText(err)}` }))
        },
        { once: true }
      )
      input.click()
    }

    const progressListeners = new Set<(s: ProgressState) => void>()
    // A finished task changes the balance (credits are charged at creation, refunded on failure).
    const progress = createProgressTracker(ctx, s => progressListeners.forEach(fn => fn(s)), refreshBalance, {
      foreign: isForeignOrigin,
      scope: () => scopeKey(host.state),
      scopeOf: origin => eventScopeKey(origin, host.state)
    })

    function Chip() {
      const prefs = useSettings()
      const [b, setB] = useState<Balance | null>(status.balance())
      const [p, setP] = useState<ProgressState>(progress.get())
      useEffect(() => {
        const stopBalance = status.onBalance(setB)
        progressListeners.add(setP)
        return () => {
          stopBalance()
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
    function DirectiveCard({ attrs, source, streaming, sessionId }: TranscriptDirectiveProps) {
      const prefs = useSettings()
      if (!prefs.inline_viewer) {
        return <p>{source}</p>
      }
      return (
        <TranscriptCard
          attrs={attrs}
          openInWorkspace={(src, title) => openGallery(src, title)}
          os={os}
          rest={ctx.rest}
          sessionId={sessionId}
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
      status.dispose()
      galleryListeners.clear()
      scopeListeners.clear()
    })
    refreshBalance()

    ctx.registerMany([
      {
        id: 'model-directive',
        area: TRANSCRIPT_DIRECTIVE_AREA,
        data: {
          name: 'meshy-model',
          // `sessionId` names the chat showing the card (newer Desktops; undefined before them): card buttons send there.
          render: ({ attrs, source, streaming, sessionId }: TranscriptDirectiveProps) => (
            <DirectiveCard attrs={attrs} sessionId={sessionId} source={source} streaming={streaming} />
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
          label: 'Meshy: view a 3D model file…',
          keywords: ['meshy', '3d', 'model', 'open', 'file', ...PICKABLE_EXTENSIONS],
          run: () => {
            // A remote backend cannot open a path on this computer: send the file's bytes instead.
            if (locality.get().kind === 'remote') {
              pickAndUpload()
              return
            }
            void ctx.os
              .pickOpenPath({ title: 'Open a 3D model', filters: [{ name: '3D models', extensions: [...PICKABLE_EXTENSIONS] }] })
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
