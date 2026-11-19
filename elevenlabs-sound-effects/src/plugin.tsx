/**
 * ElevenLabs Sound Effects for Hermes Desktop.
 *
 * - The sound card: on Desktops with the tool-media renderer seam (`TOOL_MEDIA_RENDERER_AREA`), the
 *   MEDIA: line of a sound `elevenlabs_sound_effect` made renders as an inline card: waveform,
 *   play/pause, gapless loop audition, download per format, reveal, and Next actions. Desktop decides
 *   which tool produced the file; anything else keeps the built-in player. Older Desktops play the
 *   sound in the built-in player. `::elevenlabs-sfx{id="<sound_id>"}` lines from earlier versions of
 *   this plugin still render the card.
 * - "ElevenLabs: open sound library" (⌘K) opens a workspace tab listing every sound this profile made,
 *   from the provenance record the tool writes per generation.
 *
 * Bytes come from this plugin's backend (`dashboard/plugin_api.py`) through `ctx.rest`, so the card and
 * the library work the same when Desktop is connected to a remote Hermes. No account-wide UI (balance,
 * usage) by design: other ElevenLabs integrations share the key.
 */

import type {
  HermesPlugin,
  PaletteContribution,
  PluginContribution,
  ToolMediaRendererProps,
  TranscriptDirectiveProps
} from '@hermes/plugin-sdk'
// A namespace import: on Desktops older than the tool-media seam the SDK has no
// TOOL_MEDIA_RENDERER_AREA export, and a named import of a missing export would fail to load.
import * as sdk from '@hermes/plugin-sdk'
import { host, PALETTE_AREA, TRANSCRIPT_DIRECTIVE_AREA } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'

import { clearSoundCache, player } from './audio.ts'
import { SoundCard, ToolMediaSoundCard } from './card.tsx'
import { SoundLibrary } from './library.tsx'
import { isSoundToolCall, TOOL } from './sound.ts'

const PLUGIN_ID = 'elevenlabs-sound-effects'
const WORKSPACE_ID = 'elevenlabs-sound-library'

const plugin: HermesPlugin = {
  id: PLUGIN_ID,
  name: 'ElevenLabs Sound Effects',
  description: 'Play, loop and download ElevenLabs sound effects inline in chat, and browse every sound in a library tab.',
  register(ctx) {
    // Bumped when a sound tool call completes or the plugin's settings change: the open library reloads
    // once per bump. Event-driven only; nothing polls.
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
      return <SoundLibrary ctx={ctx} refreshSignal={signal} />
    }

    const openLibrary = () => {
      if (typeof host.openWorkspace !== 'function') {
        host.notify({ kind: 'warning', message: 'Update Hermes Desktop to open the sound library.' })
        return
      }
      closeWorkspace = host.openWorkspace(WORKSPACE_ID, {
        title: 'Sound library',
        minWidth: '26rem',
        onClose: () => {
          closeWorkspace = null
        },
        render: () => <LibraryHost />
      })
    }

    ctx.onEvent('tool.complete', event => {
      const payload = (event.payload ?? {}) as { name?: unknown; args?: unknown }
      if (isSoundToolCall(payload.name, payload.args)) bump()
    })
    ctx.onEvent(`plugin.${PLUGIN_ID}.settings.changed`, bump)
    ctx.onDispose(() => {
      closeWorkspace?.()
      refreshListeners.clear()
      player.dispose()
      clearSoundCache()
    })

    const contributions: PluginContribution[] = [
      {
        // Old transcripts only: earlier versions had the agent write this line; nothing emits it now.
        id: 'sound-directive',
        area: TRANSCRIPT_DIRECTIVE_AREA,
        data: {
          name: 'elevenlabs-sfx',
          render: (props: TranscriptDirectiveProps) => (
            <SoundCard
              ctx={ctx}
              id={String(props.attrs.id ?? '').trim()}
              onOpenLibrary={openLibrary}
              // Hosts with the directive-owner seam name the chat that shows this card; undefined on older ones.
              sessionId={props.sessionId}
              source={props.source}
              streaming={props.streaming}
            />
          )
        }
      },
      {
        id: 'open-library',
        area: PALETTE_AREA,
        data: {
          id: 'elevenlabs-sfx.library',
          label: 'ElevenLabs: open sound library',
          keywords: ['elevenlabs', 'sound', 'sfx', 'audio', 'library', 'gallery', 'effects'],
          run: openLibrary
        } satisfies PaletteContribution
      },
      {
        id: 'open-settings',
        area: PALETTE_AREA,
        data: {
          id: 'elevenlabs-sfx.settings',
          label: 'ElevenLabs Sound Effects: settings',
          keywords: ['elevenlabs', 'sound', 'settings', 'api key', 'format', 'model'],
          run: () => host.navigate(`/settings?tab=plugins&agent=${PLUGIN_ID}`)
        } satisfies PaletteContribution
      }
    ]
    // The sound card for the tool's MEDIA: line. Desktop serves this claim only for tools this package
    // registered, and older Desktops don't have the area: they play the sound in the built-in player.
    if (typeof sdk.TOOL_MEDIA_RENDERER_AREA === 'string') {
      contributions.push({
        id: 'sound-card',
        area: sdk.TOOL_MEDIA_RENDERER_AREA,
        data: {
          tools: [TOOL],
          render: (props: ToolMediaRendererProps) => <ToolMediaSoundCard ctx={ctx} media={props} onOpenLibrary={openLibrary} />
        }
      })
    }
    ctx.registerMany(contributions)
  }
}

export default plugin
