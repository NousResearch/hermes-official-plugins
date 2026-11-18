// Execute the unchanged disk ESM against the public Desktop host contract. No npm dependencies.
// React is a hook/element recorder here; browser rendering is a separate integration gate.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'

const [file, scenario] = process.argv.slice(2)
const source = await readFile(file, 'utf8')
const registrations = [], settings = [], calls = [], mutations = []
const data = {
  mode: 'model_map', mode_label: 'Model routes', modes: [{ id: 'default', label: 'Default' }],
  routes: [], decisions: [], decider: { enabled: false, excluded_options: [], url: 'http://localhost:11434/v1/systemone' }
}
let hookIndex = 0, effects = [], hookState = []
let query = { data, isLoading: false }
const profile = { get: () => 'default' }
const sdk = {
  PALETTE_AREA: 'palette', ROUTES_AREA: 'routes', SIDEBAR_NAV_AREA: 'sidebar.nav',
  host: { state: { profile }, navigate() {} },
  queryClient: { setQueryData() {}, invalidateQueries() {}, removeQueries() {} },
  useValue(atom) { assert.equal(atom, profile, 'profile must come from host.state.profile'); return atom.get() },
  useQuery() { return query },
  useMutation(options) {
    mutations.push(options)
    return { isPending: false, mutate: payload => options.mutationFn(payload) }
  }
}
// The legacy shim isolates later failures on the old plugin, not compatibility claims.
if (scenario !== 'link') sdk.SETTINGS_TABS_AREA = 'legacy.settings.tabs'
const react = {
  useState(initial) {
    const i = hookIndex++
    if (!(i in hookState)) hookState[i] = typeof initial === 'function' ? initial() : initial
    return [hookState[i], next => { hookState[i] = typeof next === 'function' ? next(hookState[i]) : next }]
  },
  useEffect(effect) { effects.push(effect) }
}
const runtime = { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) }
const context = vm.createContext({ console, Set, JSON, Error })
function moduleFor(name, values) {
  return new vm.SyntheticModule(Object.keys(values), function () {
    for (const [key, value] of Object.entries(values)) this.setExport(key, value)
  }, { context, identifier: name })
}
const deps = new Map([
  ['@hermes/plugin-sdk', moduleFor('sdk', sdk)], ['react', moduleFor('react', react)],
  ['react/jsx-runtime', moduleFor('jsx-runtime', runtime)]
])
const mod = new vm.SourceTextModule(source, { context, identifier: file })
await mod.link(name => { assert.ok(deps.has(name), `unsupported import: ${name}`); return deps.get(name) })
await mod.evaluate()
const ctx = {
  rest: async (path, opts) => { calls.push({ path, opts }); return { ok: true } },
  registerMany: items => registrations.push(...items), registerSettingsPage: page => settings.push(page),
  onEvent() {}, onDispose() {}
}
mod.namespace.default.register(ctx)
function render() {
  hookIndex = 0; effects = []; mutations.length = 0
  const tree = mod.namespace.DelegationWorkbench()
  for (const effect of effects) effect()
  return tree
}
function flatten(node) {
  if (node == null || typeof node !== 'object') return [node]
  if (Array.isArray(node)) return node.flatMap(flatten)
  if (typeof node.type === 'function') return flatten(node.type(node.props))
  return [node, ...flatten(node.props?.children)]
}
if (scenario === 'link') {
  assert.ok(registrations.some(r => r.area === 'routes' && r.data.path === '/delegation'))
  assert.equal(settings.length, 1)
} else if (scenario === 'profile') {
  render(); render()
} else if (scenario === 'body') {
  // Bypass only the already separately-tested legacy atom bug to inspect actual UI mutationFns.
  sdk.host.profile = profile
  render(); render()
  const payloads = ['default', { enabled: true }, { goal: 'Inspect screenshot', has_images: true }]
  for (let i = 0; i < payloads.length; i++) await mutations[i].mutationFn(payloads[i])
  for (let i = 0; i < calls.length; i++) {
    assert.equal(typeof calls[i].opts.body, 'object', `${calls[i].path}: host JSON-encodes object bodies`)
    assert.equal(JSON.stringify(calls[i].opts.body), JSON.stringify(i === 0 ? { mode: 'default' } : payloads[i]))
  }
} else if (scenario === 'error') {
  sdk.host.profile = profile
  query = { isLoading: false, error: new Error('503: backend unavailable') }
  const tree = flatten(render())
  const text = tree.filter(x => typeof x === 'string').join(' ')
  assert.match(text, /backend unavailable/i)
  assert.doesNotMatch(text, /Loading Advanced Delegation/)
} else if (scenario === 'mutation-error') {
  sdk.host.profile = profile
  render(); render()
  for (const opts of mutations) {
    assert.equal(typeof opts.onError, 'function', 'mutations must report REST failures')
  }
} else if (scenario === 'no-settings') {
  delete ctx.registerSettingsPage
  mod.namespace.default.register(ctx)
  assert.ok(registrations.some(r => r.area === 'routes'))
} else throw new Error(`Unknown scenario: ${scenario}`)
console.log(`PASS ${scenario}`)
