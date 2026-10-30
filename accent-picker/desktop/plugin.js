// Accent Picker+ — OKLCH accent picker with persistence and full palette editing.
//
// Improvements over the stock accent-picker:
//  1. The chroma/lightness square is always a full square: out-of-gamut pixels
//     are gamut-clipped (chroma reduced until they fit sRGB) instead of being
//     painted transparent, which carved a wedge out of the panel.
//  2. "Save as theme" persists the picked accent as a real user theme
//     (`hermes-desktop-user-themes-v1` in localStorage — the app reads that
//     synchronously at boot, so it survives restarts and plugin reloads).
//  3. A Colors tab: every palette slot (sidebar, card, popover, border, ...)
//     can be recolored with the same OKLCH field, in the active mode's palette.
import {
  $accentOverride,
  PALETTE_AREA,
  requestTheme,
  retintTheme,
  setAccentOverride,
  STATUSBAR_AREAS,
  THEMES_AREA,
  contrastRatio,
  hexToOklch,
  maxChroma,
  oklchToHex,
  oklchToSrgb255,
  Popover,
  PopoverContent,
  PopoverTrigger,
  useTheme,
  useValue
} from "@hermes/plugin-sdk";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Fragment, jsx, jsxs } from "react/jsx-runtime";

// Popular UI accents, grouped by where the colour actually comes from. Each
// entry is a shipped UI accent or a documented brand accent, not a decorative
// hue: clicking one seeds the OKLCH picker and puts that colour on screen.
var ACCENT_GROUPS = [
  {
    label: "Editors",
    items: [
      { hex: "#007ACC", name: "VS Code" },
      { hex: "#68217A", name: "Visual Studio" },
      { hex: "#FE2857", name: "JetBrains" },
      { hex: "#0969DA", name: "GitHub" },
      { hex: "#5E6AD2", name: "Linear" }
    ]
  },
  {
    label: "AI",
    items: [
      { hex: "#D97757", name: "Claude" },
      { hex: "#10A37F", name: "OpenAI" },
      { hex: "#4285F4", name: "Google" }
    ]
  },
  {
    label: "Productivity",
    items: [
      { hex: "#337EA9", name: "Notion" },
      { hex: "#36C5F0", name: "Slack" },
      { hex: "#F24E1E", name: "Figma" },
      { hex: "#635BFF", name: "Stripe" }
    ]
  },
  {
    label: "Web & social",
    items: [
      { hex: "#FF7139", name: "Firefox" },
      { hex: "#5865F2", name: "Discord" },
      { hex: "#96BF48", name: "Shopify" },
      { hex: "#1DB954", name: "Spotify" }
    ]
  }
];
var UI_PRESETS = [
  { name: "nous", label: "Nous", light: "#0053FD", dark: "#4A84FE" },
  { name: "github", label: "GitHub", light: "#196D31", dark: "#4F9E5E" },
  { name: "catppuccin", label: "Catppuccin", light: "#6D2EBF", dark: "#CBA6F7" },
  { name: "everforest", label: "Everforest", light: "#586B35", dark: "#A7C080" },
  { name: "solarized", label: "Solarized", light: "#675E34", dark: "#6EA1C4" },
  { name: "nous-alt", label: "Nous Alt", light: "#0053FD", dark: "#FFE6CB" },
  { name: "classic", label: "Classic Hermes", light: "#FFBF00", dark: "#FFBF00" },
  { name: "midnight", label: "Midnight", light: "#DDD6FF", dark: "#DDD6FF" },
  { name: "ember", label: "Ember", light: "#FFD8B0", dark: "#FFD8B0" },
  { name: "mono", label: "Mono", light: "#EAEAEA", dark: "#EAEAEA" },
  { name: "slate", label: "Slate", light: "#C9D1D9", dark: "#C9D1D9" },
  { name: "cyberpunk", label: "Cyberpunk", light: "#00FF41", dark: "#00FF41" }
];
var UI_PRESET_MAP = {};
UI_PRESETS.forEach((preset) => { UI_PRESET_MAP[preset.name] = preset; });
var APPEARANCE_MODES = [
  { id: "system", label: "Auto" },
  { id: "light", label: "Light" },
  { id: "dark", label: "Dark" }
];
var FIELD_W = 224;
var FIELD_H = 130;
var RAIL_H = 12;
var HEX_CH = 7;
var CHROMA_MAX = 0.33;
var OUR_NAME = "Custom (Accent Picker)";
var BASE_KEY = "accent-picker-plus:base";
var UNDO_KEY = "accent-picker-plus:undo";
var DEFAULTS_KEY = "accent-picker-plus:defaults";
var USER_THEMES_KEY = "hermes-desktop-user-themes-v1";
var THEME_KEY = "hermes-desktop-theme-v2";

var clamp01 = (n) => Math.min(1, Math.max(0, n));

function hexToRgb255(hex) {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
  return m ? [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)] : null;
}

// ── value readouts (Google color-picker style boxes) ─────────────────────────

function rgbNorm(rgb) {
  return [rgb[0] / 255, rgb[1] / 255, rgb[2] / 255];
}

function rgbToHsv(rgb) {
  const [r, g, b] = rgbNorm(rgb);
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const v = max;
  let h = 0;
  if (max !== min) {
    h = max === r ? ((g - b) / (max - min) + (max < g ? 6 : 0)) : max === g ? ((b - r) / (max - min) + 2) : ((r - g) / (max - min) + 4);
    h = (h / 6) % 1;
    if (h < 0) h += 1;
  }
  return { h: Math.round(h * 360), s: Math.round(max === 0 ? 0 : (max - min) / max * 100), v: Math.round(v * 100) };
}

function rgbToHsl(rgb) {
  const [r, g, b] = rgbNorm(rgb);
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const l = (max + min) / 2;
  let h = 0;
  let s = 0;
  if (max !== min) {
    const d = max - min;
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    h = max === r ? ((g - b) / d + (max < g ? 6 : 0)) : max === g ? ((b - r) / d + 2) : ((r - g) / d + 4);
    h = (h / 6) % 1;
    if (h < 0) h += 1;
  }
  return { h: Math.round(h * 360), s: Math.round(s * 100), l: Math.round(l * 100) };
}

function rgbToCmyk(rgb) {
  const [r, g, b] = rgbNorm(rgb);
  const k = 1 - Math.max(r, g, b);
  const c = k === 1 ? 0 : Math.round((1 - r - k) / (1 - k) * 100);
  const m = k === 1 ? 0 : Math.round((1 - g - k) / (1 - k) * 100);
  const y = k === 1 ? 0 : Math.round((1 - b - k) / (1 - k) * 100);
  return `${c}%, ${m}%, ${y}%, ${Math.round(k * 100)}%`;
}

function ValueGrid({ hex }) {
  const rgb = hexToRgb255(hex) || [0, 0, 0];
  const hsv = rgbToHsv(rgb);
  const hsl = rgbToHsl(rgb);
  const cell = (label, value) => /* @__PURE__ */ jsxs("div", {
    className: "min-w-0 rounded-sm border border-(--dt-border) px-1.5 py-1 text-[10px] leading-tight",
    children: [
      /* @__PURE__ */ jsx("span", { className: "block text-(--ui-text-tertiary)", children: label }),
      /* @__PURE__ */ jsx("span", { className: "block font-mono tabular-nums", children: value })
    ]
  }, label);
  return /* @__PURE__ */ jsxs("div", {
    className: "grid grid-cols-4 gap-1",
    children: [
      cell("RGB", `${rgb[0]}, ${rgb[1]}, ${rgb[2]}`),
      cell("CMYK", rgbToCmyk(rgb)),
      cell("HSV", `${hsv.h}°, ${hsv.s}%, ${hsv.v}%`),
      cell("HSL", `${hsl.h}°, ${hsl.s}%, ${hsl.l}%`)
    ]
  });
}

// ── persistence ──────────────────────────────────────────────────────────────

var ctxRef = { current: null };

function notify(message) {
  const ctx = ctxRef.current;
  const host = ctx && (ctx.host || ctx);
  if (host && typeof host.notify === "function") {
    try {
      host.notify({ kind: "info", message });
    } catch {}
  }
}

function rememberBase(name) {
  // Derived themes carry a -light/-dark suffix; live installs carry a ·hex
  // selector suffix. Anything that starts with our name is our own theme.
  if (!name || name.startsWith(OUR_NAME)) {
    return;
  }
  try {
    localStorage.setItem(BASE_KEY, name.replace(/-(light|dark)$/, ""));
  } catch {}
}

function baseName() {
  try {
    return localStorage.getItem(BASE_KEY) || "nous";
  } catch {
    return "nous";
  }
}

// Try the stored base, then its bare form (deriveFrom names end -light/-dark).
// Never "restores" into our own custom theme.
function restoreBase() {
  const stored = baseName();
  const bare = stored.replace(/-(light|dark)$/, "");
  for (const candidate of [bare, stored]) {
    if (!candidate || candidate.startsWith(OUR_NAME)) {
      continue;
    }
    if (requestTheme(candidate)) {
      return true;
    }
  }
  return false;
}

// ── undo stack + base defaults ───────────────────────────────────────────────

function readUndo() {
  try {
    return JSON.parse(localStorage.getItem(UNDO_KEY) || "[]") || [];
  } catch {
    return [];
  }
}

function writeUndo(list) {
  try {
    localStorage.setItem(UNDO_KEY, JSON.stringify(list.slice(-25)));
  } catch {}
}

function pushUndo(entry) {
  if (!entry) return;
  const list = readUndo();
  list.push(entry);
  writeUndo(list);
}

/** Pop the most recent entry, optionally limited to one slot. */
function popUndo(slotKey) {
  const list = readUndo();
  for (let i = list.length - 1; i >= 0; i--) {
    if (!slotKey || list[i].slot === slotKey) {
      const entry = list[i];
      writeUndo(list.slice(0, i).concat(list.slice(i + 1)));
      return entry;
    }
  }
  return null;
}

function readDefaults() {
  try {
    return JSON.parse(localStorage.getItem(DEFAULTS_KEY) || "null");
  } catch {
    return null;
  }
}

/** Snapshot the base palettes the first time we branch off the base theme. */
function captureDefaults(theme, resolvedMode) {
  try {
    const d = readDefaults() || {};
    if (resolvedMode === "dark") {
      d.dark = theme.colors;
    } else {
      d.light = theme.colors;
      if (theme.darkColors) d.dark = theme.darkColors;
    }
    localStorage.setItem(DEFAULTS_KEY, JSON.stringify(d));
  } catch {}
}

// The machine's shared UI-preferences store. A bare localStorage.setItem on a
// shared key is clobbered by the next adopt() from the sync layer (the store
// snapshot overwrites the cache), so route the record through the host bridge.
// Live dragging fires many commits — the durable write is debounced to the
// store and the trailing re-setItem heals an adopt() of a stale snapshot.
var bridgeTimer = null;
var bridgeValue = null;
var themesCache = null;

function readThemes() {
  if (themesCache === null) {
    try {
      const parsed = JSON.parse(localStorage.getItem(USER_THEMES_KEY) || "{}");
      themesCache = parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
    } catch {
      themesCache = {};
    }
  }
  return themesCache;
}

function persistUserThemeRecord(theme) {
  let ok = true;
  try {
    readThemes()[theme.name] = theme;
    const serialized = JSON.stringify(readThemes());
    localStorage.setItem(USER_THEMES_KEY, serialized);
    const bridge = typeof window !== "undefined" && window.hermesDesktop && window.hermesDesktop.uiPreferences;
    if (bridge && typeof bridge.write === "function") {
      bridgeValue = serialized;
      if (!bridgeTimer) {
        bridgeTimer = setTimeout(() => {
          bridgeTimer = null;
          const value = bridgeValue;
          bridgeValue = null;
          if (!value) return;
          Promise.resolve(bridge.write({ values: { [USER_THEMES_KEY]: value } }))
            .then(() => {
              try {
                localStorage.setItem(USER_THEMES_KEY, value);
              } catch {}
            })
            .catch(() => {});
        }, 250);
      }
    }
  } catch {
    ok = false;
  }
  return ok;
}

function installCustom(theme) {
  let ok = true;
  try {
    persistUserThemeRecord(theme);
  } catch {
    ok = false;
  }
  try {
    // Re-registering the same contribution id replaces it (registry contract),
    // so requestTheme() resolves the fresh copy in-session even though the
    // app's $userThemes atom was initialized before this write.
    ctxRef.current.register({ id: "custom-theme", area: THEMES_AREA, data: theme });
  } catch {}
  return requestTheme(theme.name) && ok;
}

// Latest theme context seen by the picker, for palette commands that run
// outside React (revert needs the derived theme + resolved mode).
var liveRef = { current: null };

/** Live selection names must change per tick: requestTheme(sameName) is a no-op. */
function liveThemeName(hex) {
  return OUR_NAME + " · " + hex;
}

/**
 * Clone the active theme with one slot recolored, persisted, and selected.
 *
 * `finalize` installs the stable OUR_NAME selection (the one that survives
 * restarts); live ticks use a per-color selector name because requestTheme()
 * with the currently-active name is a no-op — the preference setter sees the
 * same value and the app never re-renders the new palette. The durable record
 * is always written under OUR_NAME so the on-disk theme stays single.
 */
function installSlotHex(theme, resolvedMode, key, hex, finalize) {
  try {
    const nextTheme = { ...theme };
    if (resolvedMode === "dark" && theme.darkColors) {
      nextTheme.darkColors = { ...theme.darkColors, [key]: hex };
    } else {
      nextTheme.colors = { ...theme.colors, [key]: hex };
    }
    const base = baseOf(theme);
    // ALWAYS a unique selector: requestTheme with the already-active name is a
    // no-op that resolves stale data, which snapped every slot back to its old
    // color on release. The durable record keeps OUR_NAME; boot restores it.
    const selectorName = liveThemeName(hex);
    nextTheme.name = selectorName;
    nextTheme.label = "Custom (Accent Picker)";
    nextTheme.description = `Edited in Accent Picker+ (base: ${base})`;
    nextTheme.accentOf = base;
    let ok = true;
    try {
      persistUserThemeRecord({ ...nextTheme, name: OUR_NAME });
    } catch {
      ok = false;
    }
    try {
      ctxRef.current.register({ id: "custom-theme", area: THEMES_AREA, data: nextTheme });
    } catch {}
    const resolved = requestTheme(selectorName);
    if (window.__accentPickerPlus) {
      window.__accentPickerPlus.lastInstall = { key, hex, selectorName, resolved, ok };
    }
    return resolved && ok;
  } catch (e) {
    if (window.__accentPickerPlus) {
      window.__accentPickerPlus.lastErr = String(e && e.stack || e);
      window.__accentPickerPlus.lastInstall = { key, hex, error: true };
    }
    return false;
  }
}

/** Peek (without popping) the newest undo entry, optionally for one slot. */
function peekUndo(slotKey) {
  const list = readUndo();
  for (let i = list.length - 1; i >= 0; i--) {
    if (!slotKey || list[i].slot === slotKey) return list[i];
  }
  return null;
}

function isOurs(name) {
  return typeof name === "string" && name.startsWith(OUR_NAME);
}

/** The base theme a derived/custom theme branched from. */
function baseOf(src) {
  return isOurs(src.name) ? src.accentOf ?? baseName() : src.name;
}

// ── throttled latest-value runner ────────────────────────────────────────────
//
// Live dragging used to run the full theme install on every pointermove. That
// queued hundreds of installs per second: each one re-registered the theme,
// pushed a preference selection and serialized the user-theme record, and the
// queue drained long after the drag stopped (the "filling up with garbage"
// lag). One trailing timer + a single latest value keeps the queue bounded.

function throttleLatest(fn, ms) {
  let timer = null;
  let last = 0;
  let pending = null;
  let hasPending = false;
  const tick = () => {
    timer = null;
    last = Date.now();
    if (hasPending) {
      const value = pending;
      hasPending = false;
      fn(value);
    }
  };
  const run = (value) => {
    const now = Date.now();
    if (!hasPending && now - last >= ms) {
      last = now;
      fn(value);
      return;
    }
    pending = value;
    hasPending = true;
    const wait = Math.max(0, ms - (now - last));
    if (timer) clearTimeout(timer);
    timer = setTimeout(tick, wait);
  };
  run.cancel = () => {
    if (timer) clearTimeout(timer);
    timer = null;
    hasPending = false;
    pending = null;
  };
  run.flush = (value) => {
    if (timer) clearTimeout(timer);
    timer = null;
    hasPending = false;
    pending = null;
    last = Date.now();
    if (value !== undefined) fn(value);
  };
  return run;
}

// ── pointer surface (React-driven drag; capture-safe) ────────────────────────
//
// Each element owns pointerdown/move/up through React props *and* takes pointer
// capture, so moves continue outside the element and arrive as element events.
// Nothing persists after pointerup/cancel: there is no listener to accumulate,
// which is what produced the "filling up with garbage" lag.

var dragSurfaceActive = false;

function PointerSurface({ className, children, onEnd, onPick, style }) {
  const boxRef = useRef(null);
  const dragRef = useRef(false);
  const emit = (e) => {
    const box = boxRef.current;
    if (!box) {
      return;
    }
    const r = box.getBoundingClientRect();
    if (!r.width || !r.height) {
      return;
    }
    onPick(clamp01((e.clientX - r.left) / r.width), clamp01((e.clientY - r.top) / r.height));
  };
  const onPointerDown = (e) => {
    if (e.button !== undefined && e.button !== 0) {
      return;
    }
    dragRef.current = true;
    dragSurfaceActive = true;
    try {
      if (e.currentTarget.setPointerCapture) {
        e.currentTarget.setPointerCapture(e.pointerId);
      }
    } catch {}
    emit(e);
  };
  const onPointerMove = (e) => {
    if (!dragRef.current) {
      return;
    }
    if (window.__accentPickerPlus) {
      window.__accentPickerPlus.moves = (window.__accentPickerPlus.moves || 0) + 1;
    }
    emit(e);
  };
  const stop = () => {
    if (!dragRef.current) {
      return;
    }
    dragRef.current = false;
    dragSurfaceActive = false;
    if (onEnd) {
      onEnd();
    }
  };
  return /* @__PURE__ */ jsx("div", {
    className,
    onPointerCancel: stop,
    onPointerDown,
    onPointerMove,
    onPointerUp: stop,
    ref: boxRef,
    style,
    children
  });
}

// ── chroma/lightness field (gamut-clipped: always a full square) ─────────────

function ChromaLightnessField({ hue, lch, onPick, onEnd }) {
  const canvas = useRef(null);
  useEffect(() => {
    const el = canvas.current;
    if (!el) {
      return;
    }
    const ctx = el.getContext("2d");
    if (!ctx) {
      return;
    }
    const img = ctx.createImageData(FIELD_W, FIELD_H);
    for (let y = 0; y < FIELD_H; y += 1) {
      const l = 1 - y / (FIELD_H - 1);
      // Gamut-clip per row: beyond maxChroma the color leaves sRGB, and the
      // stock picker painted those pixels transparent (the broken wedge).
      // Clipping chroma keeps hue + lightness and fills the square.
      const cMax = maxChroma(l, hue);
      for (let x = 0; x < FIELD_W; x += 1) {
        const color = { l, c: Math.min((x / (FIELD_W - 1)) * CHROMA_MAX, cMax), h: hue };
        const rgb = oklchToSrgb255(color);
        const i = (y * FIELD_W + x) * 4;
        const out = rgb || hexToRgb255(oklchToHex(color));
        if (out) {
          img.data[i] = out[0];
          img.data[i + 1] = out[1];
          img.data[i + 2] = out[2];
          img.data[i + 3] = 255;
        } else {
          img.data[i + 3] = 0;
        }
      }
    }
    ctx.clearRect(0, 0, FIELD_W, FIELD_H);
    ctx.putImageData(img, 0, 0);
  }, [hue]);
  return /* @__PURE__ */ jsx(PointerSurface, {
    className: "relative cursor-crosshair overflow-hidden rounded-sm",
    onEnd,
    onPick: (x, y) => onPick(1 - y, x * CHROMA_MAX),
    style: { height: FIELD_H, width: FIELD_W },
    children: [
        /* @__PURE__ */ jsx("canvas", { className: "block size-full", height: FIELD_H, ref: canvas, width: FIELD_W }),
        /* @__PURE__ */ jsx(
          "div",
          {
            className: "pointer-events-none absolute size-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-white shadow-[0_0_0_1px_rgba(0,0,0,.6)]",
            style: { left: `${lch.c / CHROMA_MAX * 100}%`, top: `${(1 - lch.l) * 100}%` }
          }
        )
      ]
    }
  );
}

// Static rainbow: the rail shows the true hues at a fixed lightness/chroma and
// never changes with the marker. Moving the rail recolors the square (its hue),
// moving inside the square must not recolor the rail back — the Google/chrome
// picker contract.
var RAINBOW_GRADIENT = null;

function rainbowGradient() {
  if (!RAINBOW_GRADIENT) {
    const stops = [];
    for (let i = 0; i <= 36; i += 1) {
      const h = i / 36 * 360;
      const c = Math.min(0.33, maxChroma(0.5, h));
      stops.push(`${oklchToHex({ l: 0.5, c, h })} ${i / 36 * 100}%`);
    }
    RAINBOW_GRADIENT = `linear-gradient(to right, ${stops.join(", ")})`;
  }
  return RAINBOW_GRADIENT;
}

function HueRail({ lch, onPick, onEnd }) {
  return /* @__PURE__ */ jsx(PointerSurface, {
    className: "relative cursor-ew-resize rounded-full",
    onEnd,
    onPick: (x) => onPick(x * 360),
    style: { background: rainbowGradient(), height: RAIL_H, width: FIELD_W },
    children: /* @__PURE__ */ jsx(
        "div",
        {
          className: "pointer-events-none absolute top-1/2 size-3.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-white shadow-[0_0_0_1px_rgba(0,0,0,.6)]",
          style: { left: `${lch.h / 360 * 100}%` }
        }
      )
    }
  );
}

// ── palette slots ────────────────────────────────────────────────────────────

var SLOTS = [
  ["background", "Background"],
  ["foreground", "Foreground"],
  ["sidebarBackground", "Sidebar"],
  ["sidebarBorder", "Sidebar border"],
  ["card", "Card"],
  ["popover", "Popover"],
  ["muted", "Muted"],
  ["border", "Border"],
  ["input", "Input"],
  ["primary", "Primary (accent)"],
  ["secondary", "Secondary"],
  ["accent", "Accent soft"],
  ["userBubble", "User bubble"],
  ["destructive", "Destructive"]
];

function SlotEditor({ baseTheme, palette, slotKey, resolvedMode, onApply, onBack }) {
  const start = hexToOklch(palette[slotKey]) ?? { l: 0.5, c: 0.1, h: 260 };
  const lchRef = useRef(start);
  const [lch, setLch] = useState(start);
  const undoArmed = useRef(false);
  const applyRef = useRef(onApply);
  const baseRef = useRef(baseTheme);
  applyRef.current = onApply;
  baseRef.current = baseTheme;
  // Every live commit installs the slot ON TOP OF the session's base theme,
  // not on top of the previous commit's result. Chaining installs made each
  // tick a heavier theme (and let stale copies of the derived palettes win), so
  // only the accent (which paints via setAccentOverride) kept up.
  const liveRef2 = useRef(null);
  if (!liveRef2.current) {
    liveRef2.current = throttleLatest((value) => {
      applyRef.current(slotKey, value.hex, baseRef.current, value.finalize);
    }, 120);
  }
  const update = (patch) => {
    lchRef.current = { ...lchRef.current, ...patch };
    setLch(lchRef.current);
  };
  // One undo entry per editing session: the color the slot had when the
  // editor opened. Armed right before the first commit of the session.
  const armUndo = () => {
    if (!undoArmed.current) {
      undoArmed.current = true;
      pushUndo({ slot: slotKey, mode: resolvedMode, prevHex: palette[slotKey] });
    }
  };
  const commit = (throttled) => {
    armUndo();
    const hex = oklchToHex(lchRef.current);
    if (window.__accentPickerPlus) {
      window.__accentPickerPlus.lastCommitHex = hex;
      window.__accentPickerPlus.lastCommitLch = { ...lchRef.current };
    }
    if (throttled) {
      liveRef2.current({ hex, finalize: false });
    } else {
      liveRef2.current.flush({ hex, finalize: true });
    }
  };
  useEffect(() => () => {
    liveRef2.current.cancel();
  }, []);
  const defaults = readDefaults();
  const defaultHex = defaults ? (resolvedMode === "dark" ? (defaults.dark ?? defaults.light) : (defaults.light ?? defaults.dark))?.[slotKey] : null;
  const revertEntry = peekUndo(slotKey);
  return /* @__PURE__ */ jsxs("div", { className: "flex flex-col gap-2 p-2", style: { width: FIELD_W + 16 }, children: [
    /* @__PURE__ */ jsxs("div", { className: "flex items-center gap-1.5", children: [
      /* @__PURE__ */ jsx(
        "button",
        {
          className: "shrink-0 rounded-sm border border-(--dt-border) px-1.5 py-0.5 text-[11px]",
          onClick: onBack,
          type: "button",
          children: "\u2190"
        }
      ),
      /* @__PURE__ */ jsx("span", { className: "min-w-0 flex-1 truncate text-[11px]", children: (SLOTS.find((s) => s[0] === slotKey) || [null, slotKey])[1] }),
      /* @__PURE__ */ jsx("span", { className: "size-5 shrink-0 rounded-sm border border-(--dt-border)", style: { background: palette[slotKey] } }),
      /* @__PURE__ */ jsx("span", { className: "font-mono text-[10px] text-(--ui-text-tertiary)", children: palette[slotKey] })
    ] }),
    /* @__PURE__ */ jsx(
      ChromaLightnessField,
      {
        hue: lch.h,
        lch,
        onEnd: () => commit(false),
        onPick: (l, c) => {
          update({ l, c });
          commit(true);
        }
      }
    ),
    /* @__PURE__ */ jsx(HueRail, { lch, onEnd: () => commit(false), onPick: (h) => { update({ h }); commit(true); } }),
    /* @__PURE__ */ jsxs("div", { className: "flex gap-1.5", children: [
      /* @__PURE__ */ jsx(
        "button",
        {
          className: "flex-1 rounded-sm border border-(--dt-border) px-1.5 py-0.5 text-[11px] hover:bg-(--chrome-action-hover)",
          onClick: () => { if (defaultHex) { armUndo(); onApply(slotKey, defaultHex, baseRef.current, true); const l = hexToOklch(defaultHex) ?? start; lchRef.current = l; setLch(l); } },
          title: defaultHex ? `Base theme default: ${defaultHex}` : "No default captured",
          type: "button",
          children: "Reset to default"
        }
      ),
      /* @__PURE__ */ jsx(
        "button",
        {
          className: "flex-1 rounded-sm border border-(--dt-border) px-1.5 py-0.5 text-[11px] hover:bg-(--chrome-action-hover)",
          disabled: !revertEntry,
          onClick: () => { if (revertEntry) { armUndo(); onApply(slotKey, revertEntry.prevHex, baseRef.current, true); const l = hexToOklch(revertEntry.prevHex) ?? start; lchRef.current = l; setLch(l); } },
          title: revertEntry ? `Back to ${revertEntry.prevHex}` : "Nothing to revert",
          type: "button",
          children: "Revert edit"
        }
      )
    ] }),
    /* @__PURE__ */ jsx("p", { className: "h-3 font-mono text-[10px] leading-tight text-(--ui-text-tertiary)", children: "live while you drag \xB7 saved with the theme" })
  ] });
}

// ── curated accent strip + UI presets ───────────────────────────────────────

function PopularAccents({ onPick }) {
  return /* @__PURE__ */ jsxs("div", { className: "flex flex-col gap-0.5", children: [
    /* @__PURE__ */ jsxs("div", { className: "mt-1 flex items-baseline justify-between", children: [
      /* @__PURE__ */ jsx("span", { className: "text-[9px] font-medium uppercase tracking-wide text-(--ui-text-tertiary)", children: "Popular UI accents" }),
      /* @__PURE__ */ jsx("span", { className: "font-mono text-[8px] text-(--ui-text-quaternary)", children: "click to seed" })
    ] }),
    ACCENT_GROUPS.map((group) => /* @__PURE__ */ jsxs("div", { className: "flex flex-col gap-0.5", children: [
      /* @__PURE__ */ jsx("div", { className: "mt-0.5 text-[8px] uppercase tracking-wide text-(--ui-text-quaternary)", children: group.label }),
      /* @__PURE__ */ jsx("div", { className: "grid grid-cols-2 gap-1", children: group.items.map((item) => /* @__PURE__ */ jsxs(
        "button",
        {
          className: "flex min-w-0 items-center gap-1 rounded-sm border border-(--dt-border) px-1 py-0.5 text-left text-[10px] leading-tight hover:bg-(--chrome-action-hover)",
          onClick: () => onPick(item.hex),
          title: `${group.label} \u00B7 ${item.name} \u00B7 ${item.hex} \u00B7 seed this accent`,
          type: "button",
          children: [
            /* @__PURE__ */ jsx("span", { className: "size-3.5 shrink-0 rounded-sm border border-(--dt-border)", style: { background: item.hex } }),
            /* @__PURE__ */ jsx("span", { className: "min-w-0 flex-1 truncate", children: item.name })
          ]
        },
        item.hex
      )) })
    ] }, group.label))
  ] });
}

function ThemePresets({ availableThemes, currentAccent, mode, onApply, resolvedMode, setMode, themeName }) {
  const activeName = typeof themeName === "string" ? themeName.replace(/-(light|dark)$/, "") : "";
  const themes = availableThemes
    .filter((item) => item && typeof item.name === "string" && typeof item.label === "string")
    .sort((a, b) => Number(b.name === activeName) - Number(a.name === activeName));
  return /* @__PURE__ */ jsxs("div", { className: "flex flex-col gap-1.5", children: [
    /* @__PURE__ */ jsxs("div", { className: "mt-1 flex items-baseline justify-between", children: [
      /* @__PURE__ */ jsx("span", { className: "text-[9px] font-medium uppercase tracking-wide text-(--ui-text-tertiary)", children: "UI presets" }),
      /* @__PURE__ */ jsx("span", { className: "font-mono text-[8px] text-(--ui-text-quaternary)", children: "Settings \u25B8 Appearance" })
    ] }),
    /* @__PURE__ */ jsx("div", { className: "grid grid-cols-3 gap-1", children: APPEARANCE_MODES.map((item) => /* @__PURE__ */ jsx(
      "button",
      {
        className: mode === item.id ? "rounded-sm border border-(--dt-border) bg-(--chrome-action-hover) px-1 py-0.5 text-[10px]" : "rounded-sm border border-(--dt-border) px-1 py-0.5 text-[10px] hover:bg-(--chrome-action-hover)",
        onClick: () => setMode(item.id),
        title: `Appearance mode: ${item.label}`,
        type: "button",
        children: item.label
      },
      item.id
    )) }),
    /* @__PURE__ */ jsx("div", { className: "flex max-h-[196px] flex-col gap-1 overflow-y-auto pr-0.5", children: themes.map((item) => {
      const preset = UI_PRESET_MAP[item.name];
      const active = item.name === activeName;
      const accent = preset ? (resolvedMode === "dark" ? preset.dark : preset.light) : active ? currentAccent : "var(--ui-text-quaternary)";
      return /* @__PURE__ */ jsxs(
        "button",
        {
          className: active ? "flex min-w-0 items-center gap-1.5 rounded-sm border border-(--dt-border) bg-(--chrome-action-hover) px-1 py-1 text-left text-[11px] leading-tight" : "flex min-w-0 items-center gap-1.5 rounded-sm border border-(--dt-border) px-1 py-1 text-left text-[11px] leading-tight hover:bg-(--chrome-action-hover)",
          onClick: () => onApply(item.name, item.label),
          title: `${item.name} \u00B7 ${item.description}`,
          type: "button",
          children: [
            /* @__PURE__ */ jsx("span", { className: "size-3 shrink-0 rounded-full border border-(--dt-border)", style: { background: accent } }),
            /* @__PURE__ */ jsx("span", { className: "min-w-0 flex-1 truncate", children: item.label }),
            active ? /* @__PURE__ */ jsx("span", { className: "font-mono text-[8px] uppercase text-(--ui-text-tertiary)", children: "on" }) : null
          ]
        },
        item.name
      );
    }) }),
    /* @__PURE__ */ jsx("p", { className: "h-3 font-mono text-[10px] leading-tight text-(--ui-text-tertiary)", children: `${resolvedMode} preset accent dots \u00B7 click to switch` })
  ] });
}

// ── main picker ──────────────────────────────────────────────────────────────

function AccentPicker() {
  const { availableThemes, mode, setMode, setTheme, theme, themeName, renderedMode, resolvedMode } = useTheme();
  const override = useValue($accentOverride);
  const painted = theme.colors.primary;
  const [tab, setTab] = useState("accent");
  const [slot, setSlot] = useState(null);
  const [lch, setLch] = useState(() => hexToOklch(painted) ?? { l: 0.5, c: 0.15, h: 260 });
  const [text, setText] = useState(painted);
  const dragging = useRef(false);
  const accentLive = useRef(null);
  const lastHex = useRef(painted);
  const slotBase = useRef(null);
  const rafRef = useRef(0);
  if (!accentLive.current) {
    accentLive.current = throttleLatest((hex) => setAccentOverride(hex), 120);
  }
  useEffect(() => {
    rememberBase(theme.name);
  }, [theme.name]);
  useEffect(() => {
    if (!dragging.current) {
      setLch(hexToOklch(painted) ?? { l: 0.5, c: 0.15, h: 260 });
      setText(painted);
    }
  }, [painted]);
  useEffect(() => () => {
    accentLive.current.cancel();
  }, []);
  const commit = (next, final) => {
    dragging.current = true;
    setLch(next);
    const hex = oklchToHex(next);
    lastHex.current = hex;
    setText(hex);
    if (final) {
      accentLive.current.flush(hex);
    } else {
      accentLive.current(hex);
    }
    const rafId = rafRef.current;
    if (rafId) cancelAnimationFrame(rafId);
    rafRef.current = requestAnimationFrame(() => {
      dragging.current = false;
      rafRef.current = 0;
    });
  };
  const surface = (renderedMode === "dark" ? theme.darkColors : theme.colors) ?? theme.colors;
  const ratio = contrastRatio(painted, surface.sidebarBackground ?? surface.background);
  const palette = (resolvedMode === "dark" ? theme.darkColors : theme.colors) ?? theme.colors;
  // Snapshot the base palettes the first time an edit branches off the base.
  useEffect(() => {
    liveRef.current = { theme, resolvedMode };
    if (!isOurs(theme.name)) captureDefaults(theme, resolvedMode);
  }, [theme, resolvedMode]);
  const saveAccent = () => {
    const hex = oklchToHex(lch);
    const next = {
      ...retintTheme(theme, hex),
      name: OUR_NAME,
      label: "Custom (Accent Picker)",
      description: `Saved from Accent Picker+ (base: ${baseOf(theme)})`,
      accentOf: baseOf(theme)
    };
    if (installCustom(next)) {
      setAccentOverride(null);
      notify("Accent saved as a theme \u2014 it survives restarts now.");
    } else {
      notify("Could not save the theme (storage write failed).");
    }
  };
  const onApply = (key, hex, baseTheme, finalize) => {
    if (installSlotHex(baseTheme ?? theme, resolvedMode, key, hex, finalize)) {
      notify(`${key} updated.`);
    } else {
      notify("Could not save (storage write failed).");
    }
  };
  const openSlot = (key) => {
    slotBase.current = (liveRef.current && liveRef.current.theme) || theme;
    setSlot(key);
  };
  const saved = isOurs(theme.name);
  const applyPreset = (name, label) => {
    setAccentOverride(null);
    setTheme(name);
    notify(`UI preset \u201C${label}\u201D applied.`);
  };
  const seedAccent = (hex) => {
    const next = hexToOklch(hex) ?? lch;
    commit(next, true);
  };
  if (tab === "colors" && slot) {
    return /* @__PURE__ */ jsx(SlotEditor, { baseTheme: slotBase.current ?? theme, onApply, onBack: () => setSlot(null), palette, resolvedMode, slotKey: slot });
  }
  return /* @__PURE__ */ jsxs("div", { className: "flex flex-col gap-2 p-2", style: { width: FIELD_W + 16 }, children: [
    /* @__PURE__ */ jsxs("div", { className: "flex gap-1", children: [
      /* @__PURE__ */ jsx(
        "button",
        {
          className: tab === "accent" ? "flex-1 rounded-sm border border-(--dt-border) bg-(--chrome-action-hover) px-1.5 py-0.5 text-[11px]" : "flex-1 rounded-sm border border-(--dt-border) px-1.5 py-0.5 text-[11px]",
          onClick: () => setTab("accent"),
          type: "button",
          children: "Accent"
        }
      ),
      /* @__PURE__ */ jsx(
        "button",
        {
          className: tab === "colors" ? "flex-1 rounded-sm border border-(--dt-border) bg-(--chrome-action-hover) px-1.5 py-0.5 text-[11px]" : "flex-1 rounded-sm border border-(--dt-border) px-1.5 py-0.5 text-[11px]",
          onClick: () => setTab("colors"),
          type: "button",
          children: "Colors"
        }
      ),
      /* @__PURE__ */ jsx(
        "button",
        {
          className: tab === "themes" ? "flex-1 rounded-sm border border-(--dt-border) bg-(--chrome-action-hover) px-1.5 py-0.5 text-[11px]" : "flex-1 rounded-sm border border-(--dt-border) px-1.5 py-0.5 text-[11px]",
          onClick: () => setTab("themes"),
          title: "Switch a built-in UI preset from Settings \u25B8 Appearance",
          type: "button",
          children: "Presets"
        }
      )
    ] }),
    tab === "accent" ? /* @__PURE__ */ jsxs(Fragment, { children: [
      /* @__PURE__ */ jsx(ChromaLightnessField, { hue: lch.h, lch, onEnd: () => accentLive.current.flush(lastHex.current), onPick: (l, c) => commit({ ...lch, c, l }) }),
      /* @__PURE__ */ jsx(HueRail, { lch, onEnd: () => accentLive.current.flush(lastHex.current), onPick: (h) => commit({ ...lch, h }) }),
      /* @__PURE__ */ jsxs("div", { className: "flex items-center gap-1.5", children: [
        /* @__PURE__ */ jsx("span", { className: "size-5 shrink-0 rounded-sm border border-(--dt-border)", style: { background: painted } }),
        /* @__PURE__ */ jsx(
          "input",
          {
            className: "min-w-0 flex-1 rounded-sm border border-(--dt-border) bg-transparent px-1.5 py-0.5 font-mono text-[11px] uppercase",
            onChange: (event) => {
              setText(event.target.value);
              setAccentOverride(event.target.value);
            },
            spellCheck: false,
            value: text
          }
        ),
        /* @__PURE__ */ jsx(
          "button",
          {
            className: "shrink-0 rounded-sm border border-(--dt-border) px-1.5 py-0.5 text-[11px]",
            onClick: () => setAccentOverride(null),
            title: "Clear the live preview",
            type: "button",
            children: "reset"
          }
        )
      ] }),
      /* @__PURE__ */ jsx(ValueGrid, { hex: text && /^#?[0-9a-f]{6}$/i.test(text) ? text : oklchToHex(lch) }),
      /* @__PURE__ */ jsx(PopularAccents, { onPick: seedAccent }),
      /* @__PURE__ */ jsx(
        "button",
        {
          className: "w-full rounded-sm border border-(--dt-border) px-1.5 py-1 text-[11px] hover:bg-(--chrome-action-hover)",
          onClick: saveAccent,
          type: "button",
          children: saved ? "Re-save theme with this accent" : "Save as theme (persists across restarts)"
        }
      ),
      /* @__PURE__ */ jsxs("div", { className: "flex items-center justify-between font-mono text-[10px] tabular-nums text-(--ui-text-tertiary)", children: [
        /* @__PURE__ */ jsxs("span", { children: [
          "H",
          /* @__PURE__ */ jsx("span", { className: "inline-block w-[3ch] text-right", children: Math.round(lch.h) }),
          " L",
          lch.l.toFixed(2),
          " C",
          lch.c.toFixed(3)
        ] }),
        /* @__PURE__ */ jsxs("span", { className: ratio >= 4.5 ? "" : "text-(--dt-destructive)", children: [
          renderedMode,
          " ",
          ratio.toFixed(1),
          ":1"
        ] })
      ] }),
      /* @__PURE__ */ jsx("p", { className: "h-3 font-mono text-[10px] leading-tight text-(--ui-text-tertiary)", children: saved ? "theme saved \u2014 active across restarts" : override && override.toLowerCase() !== painted.toLowerCase() ? `previewing ${override} \u2192 not saved yet` : "\xA0" })
    ] }) : tab === "themes" ? /* @__PURE__ */ jsx(ThemePresets, { availableThemes, currentAccent: painted, mode, onApply: applyPreset, resolvedMode, setMode, themeName }) : /* @__PURE__ */ jsxs("div", { className: "flex flex-col gap-1", children: [
      SLOTS.filter((s) => typeof palette[s[0]] === "string").map((s) => /* @__PURE__ */ jsxs(
        "button",
        {
          className: "flex items-center gap-1.5 rounded-sm border border-(--dt-border) px-1.5 py-1 text-left text-[11px] hover:bg-(--chrome-action-hover)",
          onClick: () => openSlot(s[0]),
          type: "button",
          children: [
            /* @__PURE__ */ jsx("span", { className: "min-w-0 flex-1 truncate", children: s[1] }),
            /* @__PURE__ */ jsx("span", { className: "font-mono text-[10px] text-(--ui-text-tertiary)", children: palette[s[0]] }),
            /* @__PURE__ */ jsx("span", { className: "size-4 shrink-0 rounded-sm border border-(--dt-border)", style: { background: palette[s[0]] } })
          ]
        },
        s[0]
      )),
      /* @__PURE__ */ jsx(
        "button",
        {
          className: "w-full rounded-sm border border-(--dt-border) px-1.5 py-1 text-[11px] hover:bg-(--chrome-action-hover)",
          onClick: () => {
            setAccentOverride(null);
            restoreBase();
          },
          type: "button",
          children: "Restore base theme"
        }
      ),
      /* @__PURE__ */ jsx("p", { className: "h-3 font-mono text-[10px] leading-tight text-(--ui-text-tertiary)", children: `${resolvedMode} palette \xB7 edits save into the theme` })
    ] })
  ] });
}

function AccentPickerTrigger() {
  const { theme } = useTheme();
  useEffect(() => {
    rememberBase(theme.name);
  }, [theme.name]);
  return /* @__PURE__ */ jsxs(Popover, { children: [
    /* @__PURE__ */ jsx(PopoverTrigger, { asChild: true, children: /* @__PURE__ */ jsxs(
      "button",
      {
        className: "inline-flex h-full items-center gap-1.5 px-1.5 text-[0.6875rem] text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground",
        title: "Accent color + palette editor",
        type: "button",
        children: [
          /* @__PURE__ */ jsx(
            "span",
            {
              className: "size-2.5 shrink-0 rounded-full border border-(--dt-border)",
              style: { background: theme.colors.primary }
            }
          ),
          /* @__PURE__ */ jsx("span", { className: "text-center font-mono tabular-nums", style: { width: `${HEX_CH}ch` }, children: theme.colors.primary })
        ]
      }
    ) }),
    /* @__PURE__ */ jsx(PopoverContent, { align: "end", className: "w-auto p-0", side: "top", children: /* @__PURE__ */ jsx(AccentPicker, {}) })
  ] });
}

// Debug hook: lets the verification script inspect drag teardown and cache size.
if (typeof window !== "undefined") {
  window.__accentPickerPlus = {
    dragInFlight: () => dragSurfaceActive,
    cacheSize: () => themesCache && typeof themesCache === "object" ? Object.keys(themesCache).length : 0,
    undoSize: () => readUndo().length,
    themeName: () => liveRef.current && liveRef.current.theme ? liveRef.current.theme.name : null,
    install: (key, hex, finalize) => {
      const live = liveRef.current;
      if (!live || !live.theme) {
        return "no theme context";
      }
      return installSlotHex(live.theme, live.resolvedMode, key, hex, finalize);
    }
  };
}

var plugin = {
  id: "accent",
  name: "Accent Picker+",
  description: "OKLCH accent picker that can SAVE the accent as a real theme (survives restarts) and recolor every palette slot (sidebar, card, popover, ...).",
  defaultEnabled: false,
  register(ctx) {
    ctxRef.current = ctx;
    ctx.onDispose(() => setAccentOverride(null));
    // A live tick may have survived in the selection store if the app was
    // killed mid-drag; the variant name isn't in the durable record, so the
    // next boot would silently fall back. Re-register the durable record and
    // switch the selection back to the stable theme name.
    try {
      const sel = localStorage.getItem(THEME_KEY);
      if (typeof sel === "string" && sel.startsWith(OUR_NAME + " · ")) {
        const record = readThemes()[OUR_NAME];
        if (record && record.name === OUR_NAME) {
          setTimeout(() => {
            try {
              ctx.register({ id: "custom-theme", area: THEMES_AREA, data: record });
              requestTheme(OUR_NAME);
            } catch {}
          }, 800);
        }
      }
    } catch {}
    ctx.registerMany([
      {
        id: "picker",
        area: STATUSBAR_AREAS.right,
        order: 90,
        render: () => /* @__PURE__ */ jsx(AccentPickerTrigger, {})
      },
      {
        id: "reset",
        area: PALETTE_AREA,
        data: {
          id: "accent.reset",
          label: "Accent: reset to the theme default",
          keywords: ["accent", "color", "theme", "reset", "default"],
          run: () => setAccentOverride(null)
        }
      },
      {
        id: "basetheme",
        area: PALETTE_AREA,
        data: {
          id: "accent.base",
          label: "Accent+: switch back to the base theme",
          keywords: ["accent", "theme", "base", "restore", "custom"],
          run: () => {
            setAccentOverride(null);
            if (!restoreBase()) {
              notify(`Base theme "${baseName()}" not found.`);
            }
          }
        }
      },
      {
        id: "revertEdit",
        area: PALETTE_AREA,
        data: {
          id: "accent.revert",
          label: "Accent+: revert the last color edit",
          keywords: ["accent", "revert", "undo", "color", "slot"],
          run: () => {
            const entry = popUndo(null);
            if (!entry) {
              notify("Nothing to revert.");
              return;
            }
            const live = liveRef.current;
            if (!live || !live.theme) {
              notify("No theme context.");
              return;
            }
            if (installSlotHex(live.theme, live.resolvedMode, entry.slot, entry.prevHex, true)) {
              notify(`Reverted ${entry.slot} to ${entry.prevHex}.`);
            }
          }
        }
      },
      {
        id: "copy",
        area: PALETTE_AREA,
        data: {
          id: "accent.copy",
          label: "Accent: copy the current color",
          keywords: ["accent", "color", "hex", "copy", "clipboard"],
          run: () => {
            const hex = $accentOverride.get();
            if (hex) {
              void navigator.clipboard?.writeText(hex);
            }
          }
        }
      }
    ]);
  }
};
var plugin_default = plugin;
export {
  plugin_default as default
};

// touched 1793213868
