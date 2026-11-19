/**
 * Accent — pick the theme's accent color and watch the whole app retint live.
 *
 * The COLOR MATH is not here. `themes/retint.ts` owns re-seeding a palette from
 * one color, and it works on any theme with any accent; this plugin is only the
 * control surface for it. That split is deliberate: the retint is core behavior
 * that Appearance settings and ⌘K should be able to drive too, while the
 * statusbar picker is an authoring tool most users never need.
 *
 * Ships OFF (`defaultEnabled: false`): it inventories in Capabilities → Plugins and
 * registers nothing until the switch is flipped. With it off, `$accentOverride`
 * stays null and `retintTheme` is never called — themes paint exactly as
 * authored.
 */

import type { HermesPlugin, PaletteContribution } from '@hermes/plugin-sdk'
import { $accentOverride, normalizeHex, PALETTE_AREA, setAccentOverride, STATUSBAR_AREAS } from '@hermes/plugin-sdk'

import { AccentPickerTrigger } from './picker.tsx'

const STORAGE_KEY = 'accent'
const FILE_NAME = 'accent.txt'

const plugin: HermesPlugin = {
  id: 'accent',
  name: 'Accent Picker',
  description:
    'Pick the theme accent from an OKLCH color picker in the status bar; the palette re-derives live. The pick is saved and restored on the next launch.',
  defaultEnabled: false,
  register(ctx) {
    // The pick is the plugin's own saved setting, applied as the live override.
    // Disabling the plugin clears the override (the saved pick stays), so no
    // colour is stranded without a control; re-enabling restores it.
    const restore = (value: unknown) => {
      const hex = typeof value === 'string' ? normalizeHex(value) : null

      if (hex) {
        setAccentOverride(hex)
      }
    }

    restore(ctx.storage.get(STORAGE_KEY, null))
    // ctx.files is the disk copy, for shells whose localStorage did not survive.
    void ctx.files.readText(ctx.files.dataFolder, FILE_NAME).then(result => {
      if (result.ok && !$accentOverride.get()) {
        restore(result.text.trim())
      }
    })

    // A drag commits on every pointer move: write storage at once, the file
    // once the pointer settles.
    let fileTimer: ReturnType<typeof setTimeout> | undefined

    const persist = (value: null | string) => {
      const hex = value === null ? null : normalizeHex(value)

      if (value !== null && !hex) {
        return
      }

      setAccentOverride(hex)
      ctx.storage.set(STORAGE_KEY, hex)
      clearTimeout(fileTimer)
      fileTimer = setTimeout(() => void ctx.files.writeText(ctx.files.dataFolder, FILE_NAME, hex ?? ''), 400)
    }

    ctx.onDispose(() => {
      clearTimeout(fileTimer)
      setAccentOverride(null)
    })

    ctx.registerMany([
      {
        id: 'picker',
        area: STATUSBAR_AREAS.right,
        order: 90,
        render: () => <AccentPickerTrigger persist={persist} />
      },
      {
        id: 'reset',
        area: PALETTE_AREA,
        data: {
          id: 'accent.reset',
          label: 'Accent: reset to the theme default',
          keywords: ['accent', 'color', 'theme', 'reset', 'default'],
          run: () => persist(null)
        } satisfies PaletteContribution
      },
      {
        id: 'copy',
        area: PALETTE_AREA,
        data: {
          id: 'accent.copy',
          label: 'Accent: copy the current color',
          keywords: ['accent', 'color', 'hex', 'copy', 'clipboard'],
          run: () => {
            const hex = $accentOverride.get()

            if (hex) {
              void navigator.clipboard?.writeText(hex)
            }
          }
        } satisfies PaletteContribution
      }
    ])
  }
}

export default plugin
