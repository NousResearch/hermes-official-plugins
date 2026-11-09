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
import { $accentOverride, PALETTE_AREA, setAccentOverride, STATUSBAR_AREAS } from '@hermes/plugin-sdk'

import { AccentPickerTrigger } from './picker.tsx'

const plugin: HermesPlugin = {
  id: 'accent',
  name: 'Accent Picker',
  description:
    'Pick the theme accent from an OKLCH color picker in the status bar; the palette re-derives live. The choice persists across reloads via ctx.storage.',
  defaultEnabled: false,
  register(ctx) {
    ctx.registerMany([
      {
        id: 'picker',
        area: STATUSBAR_AREAS.right,
        order: 90,
        render: () => <AccentPickerTrigger ctx={ctx} />
      },
      {
        id: 'reset',
        area: PALETTE_AREA,
        data: {
          id: 'accent.reset',
          label: 'Accent: reset to the theme default',
          keywords: ['accent', 'color', 'theme', 'reset', 'default'],
          run: () => setAccentOverride(null)
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
