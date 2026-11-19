<p align="center"><img src="../assets/accent-picker.jpg" alt="Accent Picker: official Hermes Desktop plugin" width="100%"></p>

# Accent Picker — Hermes Desktop plugin

> Part of [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent. Formerly [`NousResearch/hermes-desktop-accent-picker`](https://github.com/NousResearch/hermes-desktop-accent-picker), which is archived; this directory carries its full history.

An OKLCH colour wheel in the Hermes Desktop status bar. Drag it and the whole
theme retints live; the palette re-derives from the one colour you pick. The
pick is saved in the plugin's own storage (and a copy in its data folder) and
restored on the next launch; *reset* returns to the theme's own accent, and
disabling the plugin paints the authored theme again. Two palette commands come with it: *Accent: reset to the theme
default* and *Accent: copy the current color*.

It used to ship bundled with the app (off by default). It now lives here as a
desktop plugin so the app's plugin list only carries things most
users want.

## Install

From the Hermes plugin catalog: Hermes Desktop → **Capabilities → Plugins**, search **accent-picker**, install. Or from a terminal:

```bash
hermes plugins install accent-picker
```

The app copies `desktop/plugin.js` into `~/.hermes/desktop-plugins/` and loads it within a few seconds; flip the switch in Capabilities → Plugins to enable it.

## Layout

- `desktop/plugin.js` — the loadable artifact. Plain ESM, only imports
  `@hermes/plugin-sdk`, `react`, and `react/jsx-runtime` (the three specifiers
  the desktop plugin loader resolves).
- `src/plugin.tsx`, `src/picker.tsx` — the TSX source it is built from.
- `plugin.yaml` — the manifest the catalog and `hermes plugins` read.

## Build

```bash
npm install
npm run build   # esbuild src/plugin.tsx → desktop/plugin.js
```

Commit the rebuilt `desktop/plugin.js`; the desktop loads files uncompiled, so the
artifact is what ships.

## SDK

Everything the picker uses comes from `@hermes/plugin-sdk`: `$accentOverride`,
`setAccentOverride`, the OKLCH helpers (`hexToOklch`, `oklchToHex`,
`oklchToSrgb255`, `maxChroma`, `contrastRatio`), `Popover*`, `useTheme`,
`useValue`, `STATUSBAR_AREAS`, `PALETTE_AREA`. Reference:
https://hermes-agent.nousresearch.com/docs/developer-guide/desktop-plugin-sdk

MIT.
