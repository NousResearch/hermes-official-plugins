// node --test tests/plugin.test.mjs
// Registers the real Desktop half (src/plugin.tsx, bundled by esbuild) against a stub SDK, once as a
// Desktop with the tool-media renderer seam and once as an older Desktop without it.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

import { build } from 'esbuild'

const root = fileURLToPath(new URL('..', import.meta.url))

// Stub modules: the SDK (with or without the seam's export) and just enough of React to register.
const sdkStub = withArea => `
  export const host = { notify() {}, navigate() {}, state: {} }
  export const PALETTE_AREA = 'palette'
  export const TRANSCRIPT_DIRECTIVE_AREA = 'transcript.directive'
  ${withArea ? "export const TOOL_MEDIA_RENDERER_AREA = 'transcript.toolMedia'" : ''}
  const C = () => null
  export const Button = C, Codicon = C, DropdownMenu = C, DropdownMenuContent = C, DropdownMenuItem = C,
    DropdownMenuSeparator = C, DropdownMenuTrigger = C
`
const reactStub = `
  export const useEffect = () => {}, useMemo = f => f(), useCallback = f => f, useId = () => 'id', useRef = v => ({ current: v }), useState = v => [v, () => {}]
  export const Fragment = 'Fragment'
  export const jsx = (type, props) => ({ type, props }), jsxs = jsx
`

async function loadPlugin(withArea) {
  const stubs = {
    name: 'stubs',
    setup(b) {
      b.onResolve({ filter: /^(@hermes\/plugin-sdk|react|react\/jsx-runtime)$/ }, a => ({ path: a.path, namespace: 'stub' }))
      b.onLoad({ filter: /.*/, namespace: 'stub' }, a => ({
        contents: a.path === '@hermes/plugin-sdk' ? sdkStub(withArea) : reactStub,
        loader: 'js'
      }))
    }
  }
  const out = await build({
    entryPoints: [`${root}src/plugin.tsx`],
    bundle: true,
    format: 'esm',
    jsx: 'automatic',
    write: false,
    plugins: [stubs],
    logLevel: 'error'
  })
  const code = Buffer.from(out.outputFiles[0].contents).toString('base64')
  const plugin = (await import(`data:text/javascript;base64,${code}`)).default
  const registered = []
  const events = []
  plugin.register({
    source: 'test',
    registerMany: cs => (registered.push(...cs), () => {}),
    onDispose() {},
    onEvent: type => (events.push(type), () => {}),
    rest: async () => ({}),
    os: { revealPath: async () => true }
  })
  return { registered, events }
}

const media = over => ({
  file: { path: '/h/cache/generated/audio/x.mp3', name: 'sfx_door_20261024_101500_0123456789ab.mp3', kind: 'audio', mime: 'audio/mpeg', resolveUrl: async () => '' },
  toolName: 'elevenlabs_sound_effect',
  toolCallId: 'call-1',
  args: { prompt: 'door' },
  result: { success: true, sound_id: 'abcdef012345' },
  sessionId: 's-1',
  fallback: 'BUILT-IN PLAYER',
  ...over
})

// Render one level: the renderer's element is ToolMediaSoundCard, which picks the card or the fallback.
const pick = element => element.type(element.props)

test('a Desktop with the seam gets a sound-card renderer for this plugin\u2019s own tool only', async () => {
  const { registered } = await loadPlugin(true)
  const renderers = registered.filter(c => c.area === 'transcript.toolMedia')
  assert.equal(renderers.length, 1)
  assert.deepEqual(renderers[0].data.tools, ['elevenlabs_sound_effect'])
  const card = pick(renderers[0].data.render(media()))
  assert.equal(card.type.name, 'SoundCard')
  assert.equal(card.props.id, 'abcdef012345')
  assert.equal(card.props.sessionId, 's-1') // Next actions go to the chat that shows the card
  assert.equal(card.props.fallback, 'BUILT-IN PLAYER') // shown whenever the card can't play the sound
  // Nothing it can't draw as a sound card loses the built-in player.
  for (const over of [{ result: { success: false, error: 'HTTP 400' } }, { file: { ...media().file, name: 'voice.mp3' }, result: null }, { file: { ...media().file, kind: 'video' } }]) {
    assert.equal(pick(renderers[0].data.render(media(over))).props.children, 'BUILT-IN PLAYER')
  }
  // Cards in chats from before the seam still render.
  assert.ok(registered.some(c => c.area === 'transcript.directive' && c.data.name === 'elevenlabs-sfx'))
})

test('an older Desktop without the seam loads the plugin and keeps its built-in player', async () => {
  const { registered } = await loadPlugin(false)
  assert.ok(!registered.some(c => c.area === 'transcript.toolMedia' || c.area === undefined))
  assert.ok(registered.some(c => c.area === 'transcript.directive'))
  assert.ok(registered.some(c => c.area === 'palette'))
})

test('every theme colour comes from Desktop\u2019s documented plugin variables (--ui-*)', async () => {
  // Desktop compiles its own --color-* tokens into its stylesheet (Tailwind `@theme inline`), so a plugin
  // reading var(--color-border) gets nothing: the card lost its border and its waveform colours that way.
  const out = await build({ entryPoints: [`${root}src/plugin.tsx`], bundle: true, format: 'esm', jsx: 'automatic', write: false, external: ['@hermes/plugin-sdk', 'react', 'react/jsx-runtime'], logLevel: 'error' })
  const code = Buffer.from(out.outputFiles[0].contents).toString()
  const used = [...new Set([...code.matchAll(/var\((--[a-z0-9-]+)/g)].map(m => m[1]))]
  assert.ok(used.length > 3, `found ${used}`)
  assert.deepEqual(used.filter(v => !v.startsWith('--ui-')), [])
})
