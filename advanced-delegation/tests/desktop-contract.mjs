// Execute the unchanged disk ESM against the public Desktop host contract. No npm dependencies.
// React is a hook/element recorder here; browser rendering is a separate integration gate.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'

const [file, scenario] = process.argv.slice(2)
const source = await readFile(file, 'utf8')
const registrations = [], settings = [], calls = [], mutations = [], navigations = []
const data = {
  mode: 'model_map', mode_label: 'Model routes',
  routes: [{ name: 'FastSub', description: 'Fast worker' }, { name: 'VisualProwess', description: 'Vision' }],
  route_default: 'FastSub', decisions: [],
  decider: { enabled: false, excluded_options: ['VisualProwess'], url: 'http://localhost:11434/v1/systemone', model: 'pplx-decider:27b-nvfp4' }
}
let hookIndex = 0, hookState = []
let query = { data, isLoading: false }
const profile = { get: () => 'default' }
// Only names the current Desktop SDK exports; deliberately no SETTINGS_TABS_AREA.
const sdk = {
  PALETTE_AREA: 'palette', ROUTES_AREA: 'routes', SIDEBAR_NAV_AREA: 'sidebar.nav', SETTINGS_PLUGINS_AREA: 'settings.plugins',
  pluginSettingsHref: (plugin, page) => `/settings?tab=plugins&plugin=${plugin}${page ? `&ppage=${page}` : ''}`,
  host: { state: { profile }, navigate: path => navigations.push(path) },
  queryClient: { setQueryData() {}, invalidateQueries() {}, removeQueries() {} },
  useValue(atom) { assert.equal(atom, profile, 'profile must come from host.state.profile'); return atom.get() },
  useQuery() { return query },
  useMutation(options) {
    mutations.push(options)
    return { isPending: false, mutate: payload => options.mutationFn(payload) }
  }
}
if (scenario === 'old-host') {
  delete sdk.pluginSettingsHref
}
const react = {
  useState(initial) {
    const i = hookIndex++
    if (!(i in hookState)) hookState[i] = typeof initial === 'function' ? initial() : initial
    return [hookState[i], next => { hookState[i] = typeof next === 'function' ? next(hookState[i]) : next }]
  },
  useEffect() {}
}
const runtime = { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) }
const context = vm.createContext({ console, Set, JSON, Error, Number, Math, String, Array, Boolean })
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
  register: item => registrations.push(item),
  registerMany: items => registrations.push(...items),
  registerSettingsPage: page => settings.push(page),
  onEvent() {}, onDispose() {}
}
if (scenario === 'old-host') delete ctx.registerSettingsPage
mod.namespace.default.register(ctx)
function render() {
  hookIndex = 0; mutations.length = 0
  return mod.namespace.DeciderSettings()
}
function flatten(node) {
  if (node == null || typeof node !== 'object') return [node]
  if (Array.isArray(node)) return node.flatMap(flatten)
  // Keep the component element itself (its props are the contract) and what it renders.
  if (typeof node.type === 'function') return [node, ...flatten(node.type(node.props))]
  return [node, ...flatten(node.props?.children)]
}
const textOf = tree => flatten(tree).filter(x => typeof x === 'string').join(' ')
const findAll = (tree, pred) => flatten(tree).filter(n => n && typeof n === 'object' && pred(n))

if (scenario === 'settings-only') {
  assert.equal(settings.length, 1, 'one Settings ▸ Plugins page')
  assert.equal(settings[0].id, 'main')
  assert.equal(typeof settings[0].render, 'function')
  assert.ok(!registrations.some(r => r.area === 'sidebar.nav'), 'no sidebar row')
  assert.ok(!registrations.some(r => r.area === 'routes'), 'no standalone page')
  const palette = registrations.find(r => r.area === 'palette')
  assert.ok(palette, 'palette entry opens the settings page')
  palette.data.run()
  assert.equal(navigations.at(-1), '/settings?tab=plugins&plugin=advanced-delegation')
} else if (scenario === 'old-host') {
  assert.ok(registrations.some(r => r.area === 'settings.plugins' && r.id === 'main'), 'spelled-out settings page on hosts without registerSettingsPage')
  assert.ok(!registrations.some(r => r.area === 'sidebar.nav'))
  registrations.find(r => r.area === 'palette').data.run()
  assert.equal(navigations.at(-1), '/settings?tab=plugins&plugin=advanced-delegation')
} else if (scenario === 'no-mode-picker') {
  const text = textOf(render())
  assert.doesNotMatch(text, /Delegation Mode/i, 'mode lives in Model ▸ Subagents, not here')
  assert.match(text, /System-1 Decider/)
  assert.match(text, /Routes it may pick \(1\/2\)/)
} else if (scenario === 'mode-hint') {
  query = { data: { ...data, mode: 'default', mode_label: 'Default' }, isLoading: false }
  const tree = render()
  assert.match(textOf(tree), /Needs Model routes mode/)
  const open = findAll(tree, n => typeof n.type === 'function' && n.props?.onClick && /Open Model/.test(textOf(n.props.children)))
  assert.equal(open.length, 1)
  open[0].props.onClick()
  assert.equal(navigations.at(-1), '/settings?tab=config:model&page=delegation')
} else if (scenario === 'toggle-saves') {
  const tree = render()
  const toggle = findAll(tree, n => n.props?.label === 'Auto-route subagents')[0]
  assert.ok(toggle, 'auto-route ToggleRow present')
  toggle.props.onChange(true)
  const call = calls.find(c => c.path === '/decider')
  assert.ok(call, 'toggle PUTs /decider')
  assert.equal(call.opts.method, 'PUT')
  assert.equal(typeof call.opts.body, 'object', 'host JSON-encodes object bodies')
  assert.equal(call.opts.body.enabled, true)
  assert.deepEqual([...call.opts.body.excluded_options], ['VisualProwess'], 'unrelated settings are kept')
} else if (scenario === 'route-eligibility') {
  const tree = render()
  const sw = findAll(tree, n => n.props?.['aria-label'] === 'Let the Decider pick VisualProwess')[0]
  assert.ok(sw)
  assert.equal(sw.props.checked, false)
  sw.props.onCheckedChange(true)
  const call = calls.find(c => c.path === '/decider')
  assert.deepEqual([...call.opts.body.excluded_options], [])
} else if (scenario === 'probe-body') {
  render()
  // Mutation order in DeciderSettings: save, probe, clear.
  await mutations[1].mutationFn({ goal: 'Inspect screenshot', has_images: true })
  const call = calls.find(c => c.path === '/probe')
  assert.ok(call)
  assert.equal(typeof call.opts.body, 'object')
  assert.equal(call.opts.body.goal, 'Inspect screenshot')
} else if (scenario === 'error') {
  let refetched = 0
  query = { isLoading: false, error: new Error('503: {"detail": "backend unavailable"}'), refetch: () => { refetched += 1 } }
  const tree = render()
  assert.match(textOf(tree), /backend unavailable/i)
  assert.equal(findAll(tree, n => n.props?.role === 'alert').length, 1, 'a failed load is announced as an error')
  const retry = findAll(tree, n => typeof n.type === 'function' && n.props?.onClick && /Retry/.test(textOf(n.props.children)))
  assert.equal(retry.length, 1, 'a failed load can be retried')
  retry[0].props.onClick()
  assert.equal(refetched, 1)
} else if (scenario === 'mutation-error') {
  render()
  assert.ok(mutations.length >= 3)
  for (const opts of mutations) assert.equal(typeof opts.onError, 'function', 'mutations must report REST failures')
} else if (scenario === 'field-width') {
  // An input left to size itself in a settings row is too narrow for an endpoint URL.
  const tree = render()
  const rem = label => Number.parseFloat(findAll(tree, n => n.props?.['aria-label'] === label)[0]?.props.style?.width)
  for (const label of ['Endpoint', 'Fallback endpoint']) assert.ok(rem(label) >= 16, `${label} field has room for a URL`)
  assert.ok(rem('Timeout') < rem('Endpoint'), 'number fields are compact')
} else if (scenario === 'number-fields') {
  const form = { ...mod.namespace.toForm(data.decider), specialist_threshold: '0,75', confidence_threshold: ' 0.6 ', timeout: '18,5' }
  const sent = mod.namespace.toPayload(form)
  assert.equal(sent.specialist_threshold, 0.75, 'a decimal comma is a decimal point')
  assert.equal(sent.confidence_threshold, 0.6)
  assert.equal(sent.timeout, 18.5)
  // Text that is not a number is sent as typed, so the backend refuses it with a reason instead of the page
  // saving a value the user never chose. A blank field asks for the default.
  assert.equal(mod.namespace.toPayload({ ...form, timeout: '18s' }).timeout, '18s')
  assert.equal(mod.namespace.toPayload({ ...form, timeout: ' ' }).timeout, '')
} else if (scenario === 'paths') {
  assert.throws(() => mod.namespace.assertBackendPath('/mode'), /Unsupported/, 'mode switching belongs to core Settings')
  assert.equal(mod.namespace.assertBackendPath('/state?limit=30'), '/state?limit=30')
} else throw new Error(`Unknown scenario: ${scenario}`)
console.log(`PASS ${scenario}`)
