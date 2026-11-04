/**
 * ElevenLabs Music for Hermes Desktop.
 *
 * - The music card: on Desktops with the tool-media renderer seam (`TOOL_MEDIA_RENDERER_AREA`), the MEDIA:
 *   line of a track one of this plugin's tools made renders as an inline card: title, genres, synced lyrics,
 *   waveform, play/pause, loop, download per format, reveal and Next actions. Desktop decides which tool
 *   produced the file; anything else keeps the built-in player, and older Desktops play every track in the
 *   built-in player.
 * - "ElevenLabs: open music library" (⌘K) opens a workspace tab listing every track this profile made, from
 *   the provenance record the tools write per file.
 *
 * Bytes come from this plugin's backend (`dashboard/plugin_api.py`) through `ctx.rest`, so the card and the
 * library work the same when Desktop is connected to a remote Hermes. No account-wide UI (balance, usage) by
 * design: other ElevenLabs integrations share the key.
 */

import type { HermesPlugin, PaletteContribution, PluginContribution, ToolMediaRendererProps } from '@hermes/plugin-sdk'
// A namespace import: on Desktops older than the tool-media seam the SDK has no TOOL_MEDIA_RENDERER_AREA
// export, and a named import of a missing export would fail to load.
import * as sdk from '@hermes/plugin-sdk'
import { host, PALETTE_AREA } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'

import { clearTrackCache, player } from './audio.ts'
import { ToolMediaMusicCard } from './card.tsx'
import { MusicLibrary } from './library.tsx'
import { isMusicToolCall, MEDIA_TOOLS } from './music.ts'

const PLUGIN_ID = 'elevenlabs-music'
const WORKSPACE_ID = 'elevenlabs-music-library'

const plugin: HermesPlugin = {
  id: PLUGIN_ID,
  name: 'ElevenLabs Music',
  description: 'Play, download and follow up on ElevenLabs songs inline in chat, and browse every track in a library tab.',
  register(ctx) {
    // Bumped when a music tool call completes or the plugin's settings change: the open library reloads once
    // per bump. Event-driven only; nothing polls.
    let refresh = 0
    const refreshListeners = new Set<(n: number) => void>()
    const bump = () => {
      refresh += 1
      refreshListeners.forEach(fn => fn(refresh))
    }
    let closeWorkspace: (() => void) | null = null

    function LibraryHost() {
      const [signal, setSignal] = useState(refresh)
      useEffect(() => {
        refreshListeners.add(setSignal)
        return () => {
          refreshListeners.delete(setSignal)
        }
      }, [])
      return <MusicLibrary ctx={ctx} refreshSignal={signal} />
    }

    const openLibrary = () => {
      if (typeof host.openWorkspace !== 'function') {
        host.notify({ kind: 'warning', message: 'Update Hermes Desktop to open the music library.' })
        return
      }
      closeWorkspace = host.openWorkspace(WORKSPACE_ID, {
        title: 'Music library',
        minWidth: '28rem',
        onClose: () => {
          closeWorkspace = null
        },
        render: () => <LibraryHost />
      })
    }

    ctx.onEvent('tool.complete', event => {
      const payload = (event.payload ?? {}) as { name?: unknown; args?: unknown }
      if (isMusicToolCall(payload.name, payload.args)) bump()
    })
    ctx.onEvent(`plugin.${PLUGIN_ID}.settings.changed`, bump)
    ctx.onDispose(() => {
      closeWorkspace?.()
      refreshListeners.clear()
      player.dispose()
      clearTrackCache()
    })

    const contributions: PluginContribution[] = [
      {
        id: 'open-library',
        area: PALETTE_AREA,
        data: {
          id: 'elevenlabs-music.library',
          label: 'ElevenLabs: open music library',
          keywords: ['elevenlabs', 'music', 'song', 'songs', 'library', 'tracks', 'stems', 'audio'],
          run: openLibrary
        } satisfies PaletteContribution
      },
      {
        id: 'open-settings',
        area: PALETTE_AREA,
        data: {
          id: 'elevenlabs-music.settings',
          label: 'ElevenLabs Music: settings',
          keywords: ['elevenlabs', 'music', 'settings', 'api key', 'format', 'model'],
          run: () => host.navigate(`/settings?tab=plugins&agent=${PLUGIN_ID}`)
        } satisfies PaletteContribution
      }
    ]
    // The music card for the tools' MEDIA: lines. Desktop serves this claim only for tools this package
    // registered, and older Desktops don't have the area: they play the track in the built-in player.
    if (typeof sdk.TOOL_MEDIA_RENDERER_AREA === 'string') {
      contributions.push({
        id: 'music-card',
        area: sdk.TOOL_MEDIA_RENDERER_AREA,
        data: {
          tools: [...MEDIA_TOOLS],
          render: (props: ToolMediaRendererProps) => <ToolMediaMusicCard ctx={ctx} media={props} onOpenLibrary={openLibrary} />
        }
      })
    }
    ctx.registerMany(contributions)
  }
}

export default plugin
