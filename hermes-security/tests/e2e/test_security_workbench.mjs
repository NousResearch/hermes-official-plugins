// Executable ESM integration tests; fake only the host SDK and React hooks.
// The plugin's request, route guards, mutations, copy and component functions are real.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const source = await readFile(new URL('../../desktop/plugin.js', import.meta.url), 'utf8')
const uri = (code) => 'data:text/javascript;base64,' + Buffer.from(code).toString('base64')
const env = (globalThis.__securityTest = {
  calls: [],
  events: new Map(),
  registrations: [],
  invalidations: [],
  queryOptions: [],
  draft: null,
  draftReady: true,
  profile: 'remote-profile',
  fixtures: {
    '/summary': { profile: 'remote-profile', scans: 2, findings: 1 },
    '/scans': {
      items: [
        { scanId: 'scan_test', mode: 'deep', status: 'interrupted', target: { root: '/remote/project' } }
      ],
      total: 1
    },
    '/findings': {
      items: [
        {
          findingId: 'hsf_test',
          title: 'A member reads another tenant invoice',
          evidenceState: 'source_supported',
          severity: { level: 'high' }
        }
      ],
      total: 1
    },
    '/scans/scan_test': {
      scanId: 'scan_test',
      status: 'awaiting_analysis',
      target: { root: '/remote/project', snapshotDigest: 'sha256:fixture' }
    },
    '/findings/hsf_test': {
      findingId: 'hsf_test',
      scan: { scanId: 'scan_test', target: { root: '/remote/project' } },
      triage: { state: 'open' }
    },
    '/findings/hsf_test/patch': { findingId: 'hsf_test', readOnly: true, content: 'Add an ownership check.' }
  }
})
const jsxModule = uri(
  'export function jsx(type,props,key){ return {type,props:props||{},key} }; export const jsxs=jsx;'
)
const reactModule = uri(
  'export const useState = v => [globalThis.__securityTest.states?.length ? globalThis.__securityTest.states.shift() : typeof v === "function" ? v() : v, value => {globalThis.__securityTest.stateWrites?.push(value)}]; export const useEffect = fn => {if(globalThis.__securityTest.effects)globalThis.__securityTest.effects.push(fn)}; export const useRef=v=>({current:v});'
)
const sdkModule = uri(`
const e=globalThis.__securityTest;
export const Button='button', Input='input', Textarea='textarea', Dialog='dialog', DialogContent='div', DialogHeader='header', DialogTitle='h2', DialogDescription='p', DialogFooter='footer';
export const ContextMenu='context-menu', ContextMenuTrigger='context-menu-trigger', ContextMenuContent='context-menu-content', ContextMenuItem='context-menu-item', ContextMenuSeparator='hr';
export const ROUTES_AREA='routes', SIDEBAR_NAV_AREA='sidebar.nav', PALETTE_AREA='palette';
export const host={state:{profile:{get:()=>e.profile},connectionId:{get:()=>e.connectionId||null},focusedSessionOwner:{get:()=>e.owner||null},focusedStoredSessionId:{get:()=>null},activeSessionId:{get:()=>null}},notify:v=>e.notice=v,navigate:p=>e.navigation=p,openSession:async id=>e.session=id,request:async (method,params)=>{(e.rpc=e.rpc||[]).push([method,params]);if(e.noSessions)throw new Error('unsupported');if(e.rpcHandler)return e.rpcHandler(method,params);return method==='session.create'?{session_id:'rt_1',stored_session_id:'st_1'}:{}},onEvent:(type,fn)=>{e.gatewayListener=fn;return ()=>{}},composer:{insertText:async (id,text,options)=>{e.draft={id,text,options};return e.draftReady}}};
export const queryClient={invalidateQueries:async v=>e.invalidations.push(v),removeQueries:v=>e.removed=v};
export const useValue=atom=>atom.get();
export const useQuery=options=>{e.queryOptions.push(options);return {data:options.enabled === false ? undefined : e.fixtures[options.queryKey[2]+'#'+(new URLSearchParams(options.queryKey[3]).get('section')||'')] || e.fixtures[options.queryKey[2]],isLoading:false,refetch:()=>options.queryFn()}};
export const useMutation=options=>({mutate:options.mutationFn,isPending:false});
`)
const moduleCode = source
  .replaceAll("'@hermes/plugin-sdk'", JSON.stringify(sdkModule))
  .replaceAll("'react/jsx-runtime'", JSON.stringify(jsxModule))
  .replaceAll("'react'", JSON.stringify(reactModule))
const plugin = await import(uri(moduleCode))
const context = {
  registerMany: (rows) => env.registrations.push(...rows),
  onEvent: (name, fn) => env.events.set(name, fn),
  onDispose: (fn) => {
    env.dispose = fn
  },
  rest: async (path, options) => {
    env.calls.push({ path, options })
    if (env.error) throw env.error
    if (env.response !== undefined) return env.response
    if (options.method === 'POST') {
      if (path === '/scans') return { scanId: 'scan_new', status: 'awaiting_analysis', target: { root: options.body.path } }
      if (path.endsWith('/session')) return { scanId: path.split('/')[2], options: { session: options.body } }
      if (path.endsWith('/cancel')) env.fixtures['/scans/scan_test'].status = 'canceled'
      if (path.endsWith('/resume')) env.fixtures['/scans/scan_test'].status = 'awaiting_analysis'
      if (path.endsWith('/triage')) env.fixtures['/findings/hsf_test'].triage = { ...options.body }
      return structuredClone(env.fixtures[path.replace(/\/(cancel|resume|triage)$/, '')])
    }
    return structuredClone(env.fixtures[path.split('?')[0]] || { items: [], total: 0 })
  }
}
plugin.default.register(context)
function render(node) {
  if (Array.isArray(node)) return node.map(render).join(' ')
  if (node === null || node === undefined || typeof node === 'boolean') return ''
  if (typeof node !== 'object') return String(node)
  if (typeof node.type === 'function') return render(node.type(node.props))
  return render(node.props?.children)
}

test('deployment evidence separates file completion from unresolved private ingress', () => {
  const coverage = { completeness: 'partial', files: { total: 1, reviewed: 1 }, gaps: [], units: [
    { kind: 'deployment', state: 'reviewed', deployment: { component: 'collector', ingress: 'public',
      actor: 'internet user', asset: 'telemetry', disposition: 'resolved', reviewNote: 'No public ingress.', missingEvidence: [] } },
    { kind: 'deployment', state: 'deferred', deployment: { component: 'collector', ingress: 'private',
      actor: 'customer-controlled workload', asset: 'organization-wide telemetry', disposition: 'deferred',
      reviewNote: 'Private path not established.', missingEvidence: ['effective image config', 'network isolation'] } }
  ] }
  const view = render(plugin.Coverage({ data: coverage }))
  for (const text of ['Deployment evidence', 'public ingress', 'Evidence reviewed', 'private ingress', 'Unresolved',
    'customer-controlled workload', 'organization-wide telemetry', 'effective image config', 'network isolation']) assert.ok(view.includes(text), text)
  for (const extra of [{}, { workers: [{ status: 'missing' }], gaps: ['Worker packet has no accepted result.'] },
    { gaps: ['lane:A01:2025 is deferred'], units: [...coverage.units, { kind: 'lane', state: 'deferred' }] }]) {
    const summary = plugin.reportSummary({}, { ...coverage, ...extra }).join(' ')
    assert.match(summary, /All in-scope files read; deployment security unresolved\./)
    assert.doesNotMatch(summary, /Repository review complete|Partly reviewed: 1 of 1/)
    assert.match(summary, /review work was not finished|deployment security unresolved/)
  }
})

test('validation controls explain tests and offer optional app testing on open scans', () => {
  const local = render(plugin.ValidationControls({ id: 'scan_test', scan: { safetyLevel: 'local-safe', status: 'awaiting_analysis' } }))
  assert.match(local, /runs small tests from this repo/)
  assert.match(local, /Allow testing this app/)
  assert.doesNotMatch(local, /paste/i)
  const staticView = render(plugin.ValidationControls({ id: 'scan_test', scan: { safetyLevel: 'static', status: 'awaiting_analysis' } }))
  assert.match(staticView, /Run tests to confirm findings/)
  assert.match(staticView, /Allow testing this app/)
  const sealed = render(plugin.ValidationControls({ id: 'scan_test', scan: { safetyLevel: 'local-safe', status: 'completed', sealedAt: 'x' } }))
  assert.doesNotMatch(sealed, /Allow testing this app/)
})

test('validation click cancellation sends nothing; accepted click sends exact scan confirmation', async () => {
  const before = env.calls.length
  globalThis.window = { confirm: () => false }
  assert.equal(await plugin.confirmValidation('scan_test', 'run-validation', { plans: [] }, 'Run?'), null)
  assert.equal(env.calls.length, before)
  window.confirm = () => true
  env.response = { scanId: 'scan_test', receipts: [] }
  await plugin.confirmValidation('scan_test', 'run-validation', { plans: [] }, 'Run?')
  assert.equal(env.calls.at(-1).path, '/scans/scan_test/run-validation')
  assert.deepEqual(env.calls.at(-1).options.body, { plans: [], confirm: 'scan_test' })
  delete env.response
  delete globalThis.window
})

function nodes(node) {
  if (Array.isArray(node)) return node.flatMap(nodes)
  if (!node || typeof node !== 'object') return []
  if (typeof node.type === 'function') return nodes(node.type(node.props))
  return [node, ...nodes(node.props?.children)]
}

test('start form matches the simple flow and launches the scan in a background chat', async () => {
  env.fixtures['/repositories'] = { items: [{ repoKey: 'r1', root: '/remote/repo' }], total: 1 }
  env.fixtures['/folders'] = { root: '/remote/repo', items: ['src', 'tests'] }
  const view = nodes(plugin.NewScan({ onClose() {} }))
  assert.deepEqual(view.filter((n) => n.type === 'button' && n.props['aria-pressed'] !== undefined).map((n) => n.props.children), ['Whole codebase', 'Only my changes'])
  assert.deepEqual(view.filter((n) => n.type === 'option').map((n) => n.props.children), ['repo', 'Entire codebase', 'src/', 'tests/', 'Same as my chats'])
  // No advanced-delegation plugin: no subagent picker at all.
  assert.equal(view.some((n) => n.props?.['aria-label'] === 'Subagents'), false)
  const text = render(plugin.NewScan({ onClose() {} }))
  for (const gone of [/Worker/, /Budget/, /Detectors/, /revision/i, /safety/i]) assert.doesNotMatch(text, gone)
  for (const shown of [/Deep scan/, /Run tests to confirm findings/, /Additional context/, /Scan area/]) assert.match(text, shown)
  for (const [options, expected] of [
    [{ path: '/remote/repo', otherPath: '', changes: false, deep: false, tests: false, folder: '', context: '  ' }, { path: '/remote/repo', mode: 'standard' }],
    [{ path: '__other', otherPath: ' /other/app ', changes: false, deep: true, tests: true, folder: 'src', context: ' Billing app ' },
     { path: '/other/app', mode: 'deep', scope: ['src/**'], allow_tests: true, context: 'Billing app' }],
    [{ path: '/remote/repo', otherPath: '', changes: true, deep: true, tests: false, folder: 'src', context: '' }, { path: '/remote/repo', mode: 'diff' }]
  ]) {
    env.states = [{ model: '', delegate: '', ...options }, null, false]
    env.rpc = []
    let started
    const start = nodes(plugin.NewScan({ onClose() {}, onStarted: (id) => { started = id } })).find((n) => n.type === 'button' && n.props.children === 'Start scan')
    assert.equal(start.props.disabled, false)
    await start.props.onClick()
    const create = env.calls.filter((c) => c.path === '/scans').at(-1)
    assert.deepEqual(create.options.body, expected)
    assert.equal(create.options.timeoutMs, 60000)
    assert.equal(started, 'scan_new')
    assert.deepEqual(env.rpc.map(([m]) => m), ['session.create', 'prompt.submit'])
    assert.match(env.rpc[1][1].text, /scan_new/)
    assert.deepEqual(env.calls.at(-1).options.body, { sessionId: 'rt_1', storedSessionId: 'st_1' })
    delete env.states
  }
  env.noSessions = true
  env.states = [{ path: '/remote/repo', otherPath: '', changes: false, deep: false, tests: false, folder: '', context: '', model: '', delegate: '' }, null, false]
  await nodes(plugin.NewScan({})).find((n) => n.type === 'button' && n.props.children === 'Start scan').props.onClick()
  assert.match(env.draft.text, /hermes-security:security-audit.*scan_new/)
  delete env.noSessions
  delete env.states
})

test('the repository is picked by browsing folders on the gateway machine, not a typed path', async () => {
  env.fixtures['/repositories'] = { items: [{ repoKey: 'r1', root: '/remote/repo' }], total: 1 }
  env.fixtures['/folders'] = { root: '/remote/repo', items: ['src', 'tests'] }
  env.fixtures['/browse'] = { path: '/remote', parent: '/', home: '/home/me', roots: ['/'],
    items: [{ name: 'app', path: '/remote/app', repo: true }, { name: 'repo', path: '/remote/repo', repo: true }] }
  const button = (view, label) => view.find((n) => n.type === 'button' && n.props.children === label)
  const base = { path: '/remote/repo', otherPath: '', changes: false, deep: false, tests: false, folder: 'src', context: '', model: '', delegate: '' }
  assert.equal(plugin.assertBackendPath('/browse?path=%2Fremote'), '/browse?path=%2Fremote')
  env.stateWrites = []
  try {
    let view = nodes(plugin.NewScan({}))
    assert.equal(view.some((n) => n.type === 'input' && n.props.type === 'text'), false)
    assert.equal(view.some((n) => n.props.className === 'hs-browse'), false)
    button(view, 'Browse…').props.onClick()
    assert.deepEqual(env.stateWrites, ['browse'])
    env.stateWrites = []
    env.queryOptions.length = 0
    env.states = [base, null, false, '', 'browse']
    view = nodes(plugin.NewScan({}))
    const listing = env.queryOptions.find((o) => o.queryKey[2] === '/browse')
    assert.equal(new URLSearchParams(listing.queryKey[3]).get('path'), '/remote/repo')
    const entries = view.filter((n) => n.props.className === 'hs-browse-item')
    assert.deepEqual(entries.map((n) => n.props.children[0]), ['app', 'repo'])
    entries[0].props.onClick()
    button(view, 'Up').props.onClick()
    assert.deepEqual(env.stateWrites, ['/remote/app', '/'])
    env.stateWrites = []
    button(view, 'Use this folder').props.onClick()
    const next = env.stateWrites[0](base)
    assert.deepEqual(next, { ...base, path: '__other', otherPath: '/remote', folder: '' })
    assert.equal(env.stateWrites[1], null)
    env.states = [next, null, false, '', null]
    view = nodes(plugin.NewScan({}))
    assert.deepEqual(view.filter((n) => n.type === 'option').map((n) => n.props.children), ['repo', 'remote', 'Entire codebase', 'src/', 'tests/', 'Same as my chats'])
    assert.equal(view.find((n) => n.type === 'select').props.value, '__other')
    // With no repository to offer, the browser is the only control and cannot be dismissed.
    env.fixtures['/repositories'] = { items: [], total: 0 }
    view = nodes(plugin.NewScan({}))
    assert.equal(view.some((n) => n.type === 'select' && n.props['aria-label'] === 'Repository'), false)
    assert.equal(view.some((n) => n.props.className === 'hs-browse'), true)
    assert.equal(button(view, 'Cancel'), undefined)
  } finally {
    env.fixtures['/repositories'] = { items: [{ repoKey: 'r1', root: '/remote/repo' }], total: 1 }
    delete env.stateWrites
    delete env.states
  }
})

test('app permission allow and stop buttons ask before sending requests', async () => {
  env.fixtures['/scans/scan_test/grants'] = { items: [{ grantId: 'grt_test', scanId: 'scan_test', origins: ['http://localhost:8000'], used: 0, maxRequests: 2, expiresAt: '2099-01-01T00:00:00Z' }] }
  globalThis.window = { confirm: () => false }
  for (const level of ['local-safe', 'active-authorized']) {
    env.states = [0, 'http://localhost:8000', null, false, null]
    const view = nodes(plugin.ValidationControls({ id: 'scan_test', scan: { safetyLevel: level, status: 'awaiting_analysis' } }))
    const buttons = view.filter((n) => n.type === 'button' && ['Allow testing this app', 'Stop'].includes(n.props.children))
    assert.equal(buttons.length, 2)
    for (const button of buttons) {
      const before = env.calls.length
      await button.props.onClick()
      assert.equal(env.calls.length, before)
      window.confirm = () => true
      env.response = { scanId: 'scan_test', grantId: 'grt_test', revoked: button.props.children === 'Stop' }
      await button.props.onClick()
      assert.ok(env.calls.slice(before).some((call) => call.options?.body?.confirm === 'scan_test'))
      window.confirm = () => false
      delete env.response
    }
    delete env.states
  }
  delete env.fixtures['/scans/scan_test/grants']
  delete globalThis.window
})

test('test results and app permission state use everyday language', () => {
  env.fixtures['/scans/scan_test#validations'] = { items: [{ candidateId: 'cand_test', receiptId: 'vrc_test', status: 'passed', outputExcerpt: 'Observed output', cleanup: { done: true } }], total: 1 }
  for (const [grant, shown] of [
    [{ expiresAt: '2099-01-01T00:00:00Z', used: 0, maxRequests: 2 }, true],
    [{ expiresAt: '2000-01-01T00:00:00Z', used: 0, maxRequests: 2 }, false],
    [{ expiresAt: '2099-01-01T00:00:00Z', used: 2, maxRequests: 2 }, false],
    [{ expiresAt: '2099-01-01T00:00:00Z', used: 0, maxRequests: 2, revoked: true }, false]
  ]) {
    env.fixtures['/scans/scan_test/grants'] = { items: [{ grantId: 'grt_test', origins: ['http://localhost:8000'], ...grant }] }
    const output = render(plugin.ValidationControls({ id: 'scan_test', scan: { safetyLevel: 'local-safe', status: 'awaiting_analysis' } }))
    assert.equal(output.includes('requests used'), shown)
    assert.match(output, /1 test results.*Confirmed by test.*Observed output/)
    assert.doesNotMatch(output.replace(/https?:\/\/[^\s,)]+/g, ''), /\b(grant|receipt|validation|probe|authorize|HTTP)\b/i)
  }
  delete env.fixtures['/scans/scan_test#validations']
  delete env.fixtures['/scans/scan_test/grants']
})

test('registers exactly one route, sidebar and palette entry, opt-in', () => {
  assert.equal(plugin.default.defaultEnabled, false)
  assert.equal(env.registrations.length, 3)
  assert.equal(env.registrations.find((r) => r.area === 'routes').data.path, '/security')
  env.registrations.find((r) => r.area === 'palette').data.run()
  assert.equal(env.navigation, '/security')
})
test('state labels preserve uncertainty and NOT RUN', () => {
  assert.deepEqual(Object.values(plugin.EVIDENCE_LABELS), [
    'Needs review',
    'Supported by code',
    'Confirmed by test',
    'Not an issue',
    "Couldn't confirm"
  ])
  assert.equal(plugin.label('NOT_RUN'), 'NOT RUN')
  assert.equal(plugin.label(null), 'Unknown')
})
test('scan time reads as a duration, finished or still running', () => {
  assert.equal(plugin.duration(null), '')
  assert.equal(plugin.duration(30), 'under a minute')
  assert.equal(plugin.duration(3900), '1h 5m')
  assert.equal(plugin.duration(2 * 86400 + 3 * 3600), '2d 3h')
  assert.equal(plugin.scanTime({ timing: { durationSeconds: 15330, running: false } }), 'took 4h 15m')
  assert.equal(plugin.scanTime({ timing: { durationSeconds: 2280, running: true } }), 'running 38m')
  assert.equal(plugin.scanTime({}), '')
})
test('query keys partition profiles, filters and pagination', () => {
  assert.notDeepEqual(plugin.queryKey('a', '/findings'), plugin.queryKey('b', '/findings'))
  assert.deepEqual(
    plugin.queryKey('a', '/findings', { q: 'foo', offset: 0 }),
    plugin.queryKey('a', '/findings', { offset: 0, q: 'foo' })
  )
  const filters = { q: 'a&b / exact', chained: false, limit: 40, offset: 0, severity: '', repo: undefined }
  const query = new URLSearchParams(plugin.filtersToQuery(filters))
  assert.equal(query.get('q'), filters.q)
  assert.equal(query.get('chained'), 'false')
  assert.equal(query.get('offset'), '0')
  assert.equal(query.has('repo'), false)
})
test('only contract paths pass; traversal, external and authority routes fail', () => {
  for (const path of [
    '/summary',
    '/scans?limit=40',
    '/scans/scan_test',
    '/scans/scan_test/cancel',
    '/scans/scan_test/resume',
    '/scans/scan_test/activity',
    '/scans/scan_test/coverage',
    '/findings',
    '/findings/hsf_test',
    '/findings/hsf_test/triage',
    '/findings/hsf_test/patch',
    '/repositories',
    '/delegation',
    '/clone',
    '/clones',
    '/finding-folders?repo=r1',
    '/scans/scan_test/delete',
    '/exports/scan_test/md',
    '/exports/scan_test/sarif',
    '/exports/scan_test/json',
    '/exports/scan_test/csv'
  ])
    assert.equal(plugin.assertBackendPath(path), path)
  for (const path of [
    '/health',
    '/authorize',
    '/scans/../summary',
    '/scans/%2e%2e',
    'https://elsewhere/api',
    '/findings/id/apply',
    '/exports/id/pdf'
  ])
    assert.throws(() => plugin.assertBackendPath(path))
})
test('scans and findings read from the mocked scoped backend', async () => {
  assert.equal((await plugin.request('/scans?q=remote&offset=0')).items[0].status, 'interrupted')
  assert.equal((await plugin.request('/findings')).items[0].evidenceState, 'source_supported')
  assert.equal(env.calls.at(-1).options.timeoutMs, 15000)
})
test('stop and resume use actual readback, then invalidate queries', async () => {
  const result = await plugin.change('/scans/scan_test/cancel', {}, { status: 'canceled' })
  assert.equal(result.status, 'canceled')
  assert.equal((await plugin.request('/scans/scan_test')).status, 'canceled')
  assert.equal(
    (await plugin.change('/scans/scan_test/resume', {}, { status: ['running', 'awaiting_analysis'] })).status,
    'awaiting_analysis'
  )
  assert.deepEqual(env.invalidations.at(-1), { queryKey: ['hermes-security'] })
})
test('triage readback confirms saved state and note; patch remains GET only', async () => {
  const saved = await plugin.change(
    '/findings/hsf_test/triage',
    { state: 'accepted_risk', note: 'Owner accepted' },
    { state: 'accepted_risk' }
  )
  assert.equal(saved.triage.note, 'Owner accepted')
  assert.equal((await plugin.request('/findings/hsf_test')).triage.state, 'accepted_risk')
  assert.equal((await plugin.request('/findings/hsf_test/patch')).readOnly, true)
  assert.equal(env.calls.at(-1).options.method, undefined)
})
test('missing, wrong or error readbacks do not claim success', async () => {
  const before = env.invalidations.length
  for (const response of [
    { ok: true },
    { status: 'running' },
    { scanId: 'scan_other', status: 'canceled' },
    { error: { code: 'conflict', message: 'Scan is sealed; use a new scan.' } }
  ]) {
    env.response = response
    await assert.rejects(plugin.change('/scans/scan_test/cancel', {}, { status: 'canceled' }))
  }
  delete env.response
  assert.equal(env.invalidations.length, before)
  assert.throws(() => plugin.readback(null), /did not return saved state/)
})
test('backend error message is visible, including JSON error envelopes', async () => {
  env.error = new Error(
    JSON.stringify({ error: { code: 'conflict', message: 'Snapshot changed. Start a new scan.' } })
  )
  await assert.rejects(plugin.request('/scans/scan_test'), /Snapshot changed. Start a new scan./)
  delete env.error
})
test('draft preserves backend paths and options, never submits or starts scan', async () => {
  const before = env.calls.length
  const options = {
    path: '/remote/path with spaces',
    mode: 'diff',
    safetyLevel: 'static',
    context: 'User context',
    scope: ['src/**']
  }
  await plugin.putDraft(plugin.scanDraft(options))
  assert.match(env.draft.text, /hermes-security:security-audit/)
  assert.match(env.draft.text, /\/remote\/path with spaces/)
  assert.equal(env.draft.id, 'new')
  assert.equal(env.calls.length, before)
  env.draftReady = false
  await assert.rejects(plugin.putDraft('retry'), /No scan was started/)
  env.draftReady = true
})
test('high confidence without excerpt has warning and proof-gap explanation', () => {
  const output = render(
    plugin.Evidence({ finding: { confidence: { level: 'high' }, validation: { status: 'NOT_RUN' } } })
  )
  assert.match(output, /High confidence was recorded without a code excerpt/)
  assert.match(output, /No test result recorded/)
  assert.doesNotMatch(output, /supported by reading the code/)
})
test('secret evidence renders only type and fingerprint, never raw excerpt', () => {
  const output = render(
    plugin.Evidence({
      finding: {
        secret: { type: 'test-key', fingerprint: 'sha256:fixture', value: 'must-not-display' },
        codeEvidence: [{ code: 'must-not-display' }]
      }
    })
  )
  assert.match(output, /test-key/)
  assert.match(output, /sha256:fixture/)
  assert.doesNotMatch(output, /must-not-display/)
})
test('every chain edge renders evidence, assumptions and defeating controls', () => {
  const edges = [1, 2].map((n) => ({
    from: `from-${n}`,
    to: `to-${n}`,
    evidenceRefs: [`evidence-${n}`],
    assumptions: [`required-${n}`],
    counterEvidence: [`defeating-${n}`],
    state: 'supported'
  }))
  const output = render(
    plugin.Chains({
      chains: [{ chainId: 'chain-1', edges, conditional: true, disposition: 'candidate_chain' }]
    })
  )
  for (const n of [1, 2])
    for (const prefix of ['from', 'to', 'evidence', 'required', 'defeating'])
      assert.ok(output.includes(`${prefix}-${n}`))
  assert.match(output, /Conditional/)
})
test('coverage keeps missing work and zero-finding uncertainty visible', () => {
  const output = render(
    plugin.Coverage({
      data: {
        completeness: 'partial',
        files: { reviewed: 1, total: 3 },
        gaps: ['Worker missing', 'Detector failed']
      }
    })
  )
  assert.match(output, /partial/)
  assert.match(output, /1 of 3/)
  assert.match(output, /Worker missing/)
  assert.match(output, /Zero findings does not establish/)
})
test('long gap lists collapse behind a count but stay readable', () => {
  const gaps = Array.from({ length: 266 }, (_, i) => `Control ${i} deferred`)
  const output = render(plugin.Coverage({ data: { completeness: 'partial', files: { reviewed: 0, total: 1 }, gaps } }))
  assert.match(output, /266 coverage gaps recorded/)
  assert.match(output, /Control 265 deferred/)
})
test('progress never invents completion and mounts aria-live stepper', () => {
  const node = plugin.Progress({ scan: {} })
  assert.equal(node.props['aria-live'], 'polite')
  assert.ok(node.props.children.every((li) => li.props['data-state'] === 'pending'))
  assert.match(render(node), /Prepare.*Review.*Verify.*Report/)
  const live = plugin.Progress({ scan: { progress: { steps: [{ label: 'Preparing scan', state: 'done' }, { label: 'Building threat model', state: 'done' }, { label: 'Reviewing code', state: 'active' }] } } })
  assert.deepEqual(live.props.children.map((li) => li.props['data-state']), ['done', 'active', 'pending', 'pending'])
  assert.match(render(live), /Reviewing code/)
  const half = plugin.stages([{ label: 'Validating findings', state: 'done' }, { label: 'Tracing attack paths', state: 'pending' }])
  assert.equal(half[2].state, 'active')
  const unknown = plugin.stages([{ label: 'Something new', state: 'active' }])
  assert.deepEqual(unknown, [{ name: 'Something new', state: 'active' }])
})
test('full page evaluates through SDK React shim and uses six-second queries', () => {
  env.states = ['Scans', null, false, {}]
  const output = render(plugin.SecurityPage())
  delete env.states
  for (const tab of ['Scans', 'Findings', 'New scan']) assert.match(output, new RegExp(tab))
  assert.doesNotMatch(output, /Repositories/)
  assert.match(output, /project/)
  // Scan state polls every six seconds; the model list is gateway inventory, cached for a minute.
  for (const q of env.queryOptions) assert.equal(q.refetchInterval, q.queryKey[2] === 'model-options' ? undefined : 6000)
  assert.ok(env.queryOptions.some((q) => q.queryKey[2] === '/scans'))
})
test('legacy store projections normalize metadata without rewriting evidence', async () => {
  env.response = {
    items: [
      {
        scan_id: 'scan_exact',
        finding_id: 'hsf_exact',
        evidence_state: 'source_supported',
        created_at: '2026-01-01T00:00:00Z',
        last_scan_id: 'scan_latest',
        coverage_completeness: 'partial',
        codeEvidence: [{ code: 'scan_id must remain exact' }]
      }
    ]
  }
  const row = (await plugin.request('/scans')).items[0]
  assert.equal(row.scanId, 'scan_exact')
  assert.equal(row.findingId, 'hsf_exact')
  assert.equal(row.evidenceState, 'source_supported')
  assert.equal(row.latestScanId, 'scan_latest')
  assert.equal(row.coverageCompleteness, 'partial')
  assert.equal(row.codeEvidence[0].code, 'scan_id must remain exact')
  delete env.response
})
test('finding chains exclude unrelated scan chains and retain broken edges', () => {
  const result = plugin.relevantChains(
    {
      chains: [{ findingIds: ['a', 'b'] }, { findingIds: ['c'] }],
      broken: [{ edge: { from: 'b', to: 'z' } }]
    },
    'b'
  )
  assert.equal(result.chains.length, 1)
  assert.equal(result.broken.length, 1)
})
test('completed scan uses sealed manifest counts beside coverage', () => {
  env.fixtures['/scans/scan_done'] = {
    scanId: 'scan_done',
    status: 'completed',
    sealedAt: '2026-01-01T00:00:00Z',
    target: { root: '/remote/done' }
  }
  env.fixtures['/scans/scan_done#manifest'] = {
    counts: { critical: 0, high: 2, medium: 1, low: 0, informational: 0, chains: 1 },
    runtime: { model: 'fixture-model', provider: 'fixture-provider', profile: 'remote-profile' }
  }
  env.fixtures['/scans/scan_done/coverage'] = { completeness: 'partial', gaps: ['Review worker missing'] }
  env.fixtures['/scans/scan_done#findings'] = { items: [], total: 0 }
  const output = render(plugin.ScanDetail({ id: 'scan_done', onBack() {}, onFinding() {} }))
  assert.match(output, /2 high · 1 medium/)
  assert.match(output, /Deep scan|Scan/)
  assert.match(output, /Review worker missing/)
  assert.match(output, /fixture-model/)
  assert.match(output, /Run in chat/)
})
test('all finding tabs and repository metadata render without missing SDK references', async () => {
  env.fixtures['/findings/hsf_test'] = {
    ...env.fixtures['/findings/hsf_test'],
    title: 'Test finding',
    summary: 'Effect',
    rootCause: 'Missing guard',
    evidenceState: 'source_supported',
    confidence: { level: 'high' },
    validation: { status: 'NOT_RUN' }
  }
  env.states = ['', null]
  const output = render(plugin.FindingDetail({ id: 'hsf_test', onBack() {} }))
  assert.match(output, /Test finding/)
  assert.match(output, /Missing guard/)
  assert.match(output, /Not tested/)
  assert.match(output, /Add an ownership check/)
  assert.match(output, /Fix in chat/)
  // Each fix starts its own chat: the title names the finding and the second it started.
  const at = new Date(2026, 10, 9, 1, 45, 7)
  assert.equal(plugin.fixChatTitle('Test finding', 'hsf_32c904e04b66b939ad4e5fb8', at), 'Fix: Test finding · 32c904 · Nov 9, 01:45:07')
  // Same title on another finding, or a retry a second later, never shares a chat.
  assert.notEqual(plugin.fixChatTitle('Missing authorization', 'hsf_aaaaaa', at), plugin.fixChatTitle('Missing authorization', 'hsf_bbbbbb', at))
  assert.notEqual(plugin.fixChatTitle('Test finding', 'hsf_32c904', at),
    plugin.fixChatTitle('Test finding', 'hsf_32c904', new Date(2026, 10, 9, 1, 45, 8)))
  assert.equal(plugin.fixChatTitle('', '', at), 'Fix: security finding · Nov 9, 01:45:07')
  env.rpc = []
  const fixButton = () => nodes(plugin.FindingDetail({ id: 'hsf_test', onBack() {} }))
    .find((n) => n.type === 'button' && /Fix in chat/.test(render(n.props.children ?? '')))
  // A double click while the chat is being created sends one prompt, not two.
  await Promise.all([fixButton().props.onClick(), fixButton().props.onClick()])
  assert.equal(env.rpc.filter(([method]) => method === 'session.create').length, 1)
  assert.equal(env.rpc.filter(([method]) => method === 'prompt.submit').length, 1)
  assert.match(env.rpc.find(([method]) => method === 'session.create')[1].title,
    /^Fix: Test finding · test · [A-Z][a-z]{2} \d{1,2}, \d{2}:\d{2}:\d{2}$/)
  assert.equal(plugin.fixesStarting.size, 0)
  // Once it finished, a later click starts another chat.
  await fixButton().props.onClick()
  assert.equal(env.rpc.filter(([method]) => method === 'session.create').length, 2)
  env.states = []
  const page = render(plugin.BrowserView({ kind: 'Findings', selected: null, setSelected() {}, onFinding: {}, newScan: false, setNewScan() {} }))
  assert.match(page, /A member reads another tenant invoice/)
  assert.match(page, /high/)
  assert.match(page, /Any severity/)
  assert.ok(env.queryOptions.some((q) => q.queryKey[2] === '/findings' && new URLSearchParams(q.queryKey[3]).get('status') === 'open'))
})
test('page owner and queries distinguish same-named remote profiles', async () => {
  env.connectionId = 'remote-a'
  const before = plugin.SecurityPage()
  render(before)
  const staleQuery = env.queryOptions.at(-1)
  env.connectionId = 'remote-b'
  const after = plugin.SecurityPage()
  assert.notEqual(before.key, after.key)
  await assert.rejects(staleQuery.queryFn(), /Backend changed/)
  assert.notDeepEqual(
    plugin.queryKey('default', '/summary', {}, 'remote-a'),
    plugin.queryKey('default', '/summary', {}, 'remote-b')
  )
  env.connectionId = null
})
test('draft refuses a chat belonging to another backend and reports failure after navigation', async () => {
  env.owner = { profile: 'other', connectionId: 'remote' }
  await assert.rejects(plugin.putDraft('must not insert'), /this backend profile/)
  assert.equal(env.notice.kind, 'error')
  env.owner = null
})
test('every export format downloads the backend content with its filename', async () => {
  const saved = []
  globalThis.document = { createElement: () => ({ click() { saved.push([this.download, this.href]) } }) }
  for (const format of ['md', 'sarif', 'json', 'csv']) {
    env.fixtures[`/exports/scan_done/${format}`] = { content: `fixture-${format}`, contentType: 'text/plain', filename: `scan.${format}` }
    assert.equal(await plugin.downloadExport('scan_done', format), `scan.${format}`)
  }
  assert.deepEqual(saved.map(([name]) => name), ['scan.md', 'scan.sarif', 'scan.json', 'scan.csv'])
  assert.ok(saved.every(([, href]) => href.startsWith('blob:')))
  assert.match(render(plugin.ExportPanel({ id: 'scan_done' })), /Report \(Markdown\).*SARIF/)
  delete globalThis.document
})
test('finding readback keeps the finding when it embeds scan metadata', () => {
  const row = {
    findingId: 'hsf_nested',
    triage: { state: 'closed' },
    scan: { scanId: 'scan_nested', target: { root: '/remote/nested' } }
  }
  assert.equal(plugin.readback(row).findingId, 'hsf_nested')
  assert.equal(plugin.findingData(row).scanId, 'scan_nested')
  assert.equal(plugin.findingData(row).target.root, '/remote/nested')
})
test('finding editor restores the saved triage note on reopening', () => {
  env.fixtures['/findings/hsf_test'].triage = { state: 'accepted_risk', note: 'Preserve owner rationale' }
  env.effects = []
  env.stateWrites = []
  plugin.FindingDetail({ id: 'hsf_test', onBack() {} })
  env.effects.forEach(effect => effect())
  assert.ok(env.stateWrites.includes('Preserve owner rationale'))
  delete env.effects
  delete env.stateWrites
})

test('scan/finding events invalidate and unload removes cache', () => {
  for (const name of ['plugin.hermes-security.scan.updated', 'plugin.hermes-security.finding.updated']) {
    assert.ok(env.events.has(name))
    env.events.get(name)({ payload: {} })
    assert.deepEqual(env.invalidations.at(-1), { queryKey: ['hermes-security'] })
  }
  env.dispose()
  assert.deepEqual(env.removed, { queryKey: ['hermes-security'] })
})

test('a paused open scan says why and continues in its own chat', async () => {
  plugin.default.register(context)   // the previous test unloads the plugin
  env.fixtures['/scans/scan_paused'] = {
    scanId: 'scan_paused', status: 'awaiting_analysis', reason: 'paused: needs attention',
    target: { root: '/remote/paused' }, options: { session: { sessionId: 'rt_1', storedSessionId: 'st_1' } }
  }
  env.fixtures['/scans/scan_paused#findings'] = { items: [], total: 0 }
  const output = render(plugin.ScanDetail({ id: 'scan_paused', onBack() {}, onFinding() {} }))
  assert.match(output, /Paused — needs attention/)
  assert.doesNotMatch(output, /Running/)
  const button = () => nodes(plugin.ScanDetail({ id: 'scan_paused', onBack() {}, onFinding() {} }))
    .find((n) => n.type === 'button' && n.props.children === 'Continue scan')
  env.rpc = []
  await button().props.onClick()
  assert.deepEqual(env.rpc, [['prompt.submit', { session_id: 'rt_1', text: plugin.continuePrompt('scan_paused') }]])
  assert.match(plugin.continuePrompt('scan_paused'), /^Continue security scan scan_paused\. Read security_scan_get section next/)
  // A chat the backend no longer holds is reopened from its saved id, then relinked.
  env.rpc = []
  env.rpcHandler = (method) => {
    if (method === 'prompt.submit' && env.rpc.length === 1) throw new Error('session not found')
    return method === 'session.resume' ? { session_id: 'rt_2' } : {}
  }
  await button().props.onClick()
  delete env.rpcHandler
  assert.deepEqual(env.rpc.map(([m, p]) => [m, p.session_id]), [['prompt.submit', 'rt_1'], ['session.resume', 'st_1'], ['prompt.submit', 'rt_2']])
  assert.deepEqual(env.calls.filter((c) => c.path === '/scans/scan_paused/session').at(-1).options.body, { sessionId: 'rt_2', storedSessionId: 'st_1' })
  // Finished or stopped scans offer no continue; running scans without the reason read as running.
  env.fixtures['/scans/scan_paused'].reason = null
  assert.match(render(plugin.ScanDetail({ id: 'scan_paused', onBack() {}, onFinding() {} })), /Running/)
  env.fixtures['/scans/scan_paused'].status = 'canceled'
  assert.equal(button(), undefined)
})

test('a stalled open scan says so and why instead of waiting for the agent', () => {
  plugin.default.register(context)
  const since = new Date(Date.now() - 65 * 60000).toISOString()
  env.fixtures['/scans/scan_stalled'] = {
    scanId: 'scan_stalled', status: 'awaiting_analysis', reason: null,
    stall: { since, why: 'Code review has not finished and no worker is running.' },
    target: { root: '/remote/stalled' }, options: { session: { sessionId: 'rt_1', storedSessionId: 'st_1' } }
  }
  env.fixtures['/scans/scan_stalled#findings'] = { items: [], total: 0 }
  const output = render(plugin.ScanDetail({ id: 'scan_stalled', onBack() {}, onFinding() {} }))
  assert.match(output, /Stalled/)
  assert.match(output, /Code review has not finished and no worker is running\./)
  assert.match(output, /Last progress 1h ago/)
  assert.doesNotMatch(output, /Waiting for the agent/)
  assert.doesNotMatch(output, /Running/)
  assert.ok(nodes(plugin.ScanDetail({ id: 'scan_stalled', onBack() {}, onFinding() {} }))
    .some((n) => n.type === 'button' && n.props.children === 'Continue scan'))
  // Paused wins over stalled; no stall reads as running again.
  env.fixtures['/scans/scan_stalled'].reason = 'paused: needs attention'
  assert.match(render(plugin.ScanDetail({ id: 'scan_stalled', onBack() {}, onFinding() {} })), /Paused — needs attention/)
  // A refused automatic continue names the setting that turns it on.
  env.fixtures['/scans/scan_stalled'].reason = 'paused: could not continue automatically'
  const off = render(plugin.ScanDetail({ id: 'scan_stalled', onBack() {}, onFinding() {} }))
  assert.match(off, /Automatic continue is off, or its chat is closed/)
  assert.ok(off.includes(plugin.AUTO_CONTINUE_SETTING))
  assert.equal(plugin.AUTO_CONTINUE_SETTING, 'hermes config set plugins.entries.hermes-security.allow_gateway_injection true')
  assert.doesNotMatch(off, /Code review has not finished/)
  env.fixtures['/scans/scan_stalled'].reason = null
  env.fixtures['/scans/scan_stalled'].stall = null
  assert.match(render(plugin.ScanDetail({ id: 'scan_stalled', onBack() {}, onFinding() {} })), /Running/)
})

test('model and subagent pickers reach the scan and its chat; subagents only with advanced-delegation', async () => {
  env.fixtures['/repositories'] = { items: [{ repoKey: 'r1', root: '/remote/repo' }], total: 1 }
  env.fixtures['model-options'] = { model: 'claude-opus-5-5', provider: 'anthropic', providers: [
    { slug: 'anthropic', name: 'Anthropic', models: ['claude-opus-5-5', 'claude-sonnet-5-5'] },
    { slug: 'openrouter', name: 'OpenRouter', models: ['x/fast'], authenticated: true },
    { slug: 'nokey', name: 'No key', models: ['m'], authenticated: false }] }
  env.fixtures['/delegation'] = { installed: true, backend: 'model_map', available: true, default: 'fast',
    choices: [{ name: 'fast', description: 'Cheap and quick', detail: 'openrouter/x/fast' }, { name: 'deep', description: '', detail: '' }] }
  try {
    const view = nodes(plugin.NewScan({}))
    const model = view.find((n) => n.props?.['aria-label'] === 'Model')
    const groups = nodes(model.props.children).filter((n) => n.type === 'optgroup').map((n) => n.props.label)
    assert.deepEqual(groups, ['Anthropic', 'OpenRouter'])
    assert.match(render(model), /Same as my chats \(claude-opus-5-5\)/)
    const routes = view.find((n) => n.props?.['aria-label'] === 'Subagents')
    assert.deepEqual(nodes(routes.props.children).filter((n) => n.type === 'option').map((n) => n.props.value), ['', 'fast', 'deep'])
    assert.match(render(routes), /Default \(fast\)/)
    assert.match(render(routes), /fast · openrouter\/x\/fast — Cheap and quick/)
    env.states = [{ path: '/remote/repo', otherPath: '', changes: false, deep: false, tests: false, folder: '', context: '',
      model: JSON.stringify(['openrouter', 'x/fast']), delegate: 'deep' }, null, false]
    env.rpc = []
    await nodes(plugin.NewScan({})).find((n) => n.type === 'button' && n.props.children === 'Start scan').props.onClick()
    const create = env.calls.filter((c) => c.path === '/scans').at(-1)
    assert.deepEqual(create.options.body, { path: '/remote/repo', mode: 'standard', model: 'x/fast', provider: 'openrouter',
      delegate: { backend: 'model_map', name: 'deep' } })
    assert.deepEqual(env.rpc[0], ['session.create', { cwd: '/remote/repo', title: 'Security scan · repo · new', model: 'x/fast', provider: 'openrouter' }])
    // Default model: the scan records it and its chat is created on it, not on the profile's default.
    env.states = [{ path: '/remote/repo', otherPath: '', changes: false, deep: false, tests: false, folder: '', context: '', model: '', delegate: '' }, null, false]
    env.rpc = []
    await nodes(plugin.NewScan({})).find((n) => n.type === 'button' && n.props.children === 'Start scan').props.onClick()
    assert.deepEqual(env.calls.filter((c) => c.path === '/scans').at(-1).options.body, { path: '/remote/repo', mode: 'standard', model: 'claude-opus-5-5' })
    assert.deepEqual(env.rpc[0], ['session.create', { cwd: '/remote/repo', title: 'Security scan · repo · new', model: 'claude-opus-5-5', provider: 'anthropic' }])
    assert.deepEqual(plugin.chatRoute('x/fast', env.fixtures['model-options']), { model: 'x/fast', provider: 'openrouter' })
    assert.deepEqual(plugin.chatRoute('unlisted', env.fixtures['model-options']), { model: 'unlisted' })
    assert.deepEqual(plugin.chatRoute('', env.fixtures['model-options']), {})
    // A titled create reopens the chat holding that title, so each scan of a repo gets its own.
    assert.equal(plugin.scanChatTitle('scan_c9bf4ff6a0f8d33338f432c7', '/remote/repo'), 'Security scan · repo · c9bf4f')
    assert.notEqual(plugin.scanChatTitle('scan_c9bf4ff6a0f8d33338f432c7', '/remote/repo'),
      plugin.scanChatTitle('scan_669d51173ae3c7043e63b0e2', '/remote/repo'))
    env.fixtures['/delegation'] = { installed: true, backend: 'default', available: true, choices: [], default: null }
    const off = nodes(plugin.NewScan({})).find((n) => n.props?.['aria-label'] === 'Subagents')
    assert.equal(off.props.disabled, true)
    assert.match(render(plugin.NewScan({})), /Turn on Model routes or Profile routes/)
  } finally {
    delete env.fixtures['model-options']
    delete env.fixtures['/delegation']
    delete env.states
  }
})

test('a repository URL is cloned on the gateway and selected for the scan', async () => {
  env.fixtures['/clone'] = { path: '/data/repos/github.com/owner/repo', url: 'https://github.com/owner/repo', action: 'cloned', revision: 'abc' }
  env.stateWrites = []
  try {
    const button = (view, label) => view.find((n) => n.type === 'button' && n.props.children === label)
    button(nodes(plugin.NewScan({})), 'From URL…').props.onClick()
    assert.deepEqual(env.stateWrites, ['clone'])
    env.states = [{ path: '/remote/repo', otherPath: '', changes: false, deep: false, tests: false, folder: '', context: '', model: '', delegate: '' }, null, false, '', 'clone']
    const view = nodes(plugin.NewScan({}))
    assert.ok(view.some((n) => n.props?.['aria-label'] === 'Repository URL'))
    let picked
    env.states = [' owner/repo ', false, null]
    await button(nodes(plugin.CloneRepo({ onCloned: (r) => { picked = r } })), 'Clone').props.onClick()
    assert.deepEqual(env.calls.at(-1), { path: '/clone', options: { timeoutMs: 660000, method: 'POST', body: { url: 'owner/repo' } } })
    assert.equal(picked.path, '/data/repos/github.com/owner/repo')
    env.error = new Error(JSON.stringify({ error: { code: 'clone_failed', message: 'Could not clone the repository: fatal: repository not found' } }))
    env.stateWrites = []
    env.states = ['https://github.com/o/missing', false, null]
    await button(nodes(plugin.CloneRepo({ onCloned: () => assert.fail('no success') })), 'Clone').props.onClick()
    assert.match(env.stateWrites.find((w) => w instanceof Error).message, /repository not found/)
  } finally {
    delete env.error
    delete env.states
    delete env.stateWrites
    delete env.fixtures['/clone']
  }
})

test('delete asks first, only offers stopped or finished scans, and reads back the deletion', async () => {
  const before = env.calls.length
  globalThis.window = { confirm: () => false }
  try {
    assert.equal(await plugin.deleteScan('scan_test', 'project'), null)
    assert.equal(env.calls.length, before)
    window.confirm = (message) => /Delete this scan of project\?.*cannot be undone/.test(message)
    env.response = { scanId: 'scan_test', deleted: true, findings: 2 }
    assert.equal((await plugin.deleteScan('scan_test', 'project')).deleted, true)
    assert.deepEqual(env.calls.at(-1), { path: '/scans/scan_test/delete', options: { timeoutMs: 15000, method: 'POST', body: { confirm: 'scan_test' } } })
    env.response = { scanId: 'scan_test' }
    await assert.rejects(plugin.deleteScan('scan_test', 'project'), /did not confirm/)
  } finally {
    delete env.response
    delete globalThis.window
  }
  const labels = (status) => {
    env.fixtures['/scans/scan_test'].status = status
    return nodes(plugin.ScanDetail({ id: 'scan_test' })).filter((n) => n.type === 'button').map((n) => n.props.children)
  }
  try {
    assert.equal(labels('awaiting_analysis').includes('Delete'), false)
    assert.equal(labels('canceled').includes('Delete'), true)
    assert.equal(labels('completed').includes('Delete'), true)
  } finally {
    env.fixtures['/scans/scan_test'].status = 'awaiting_analysis'
  }
})

test('findings filter by repository and folder; scans by repository', () => {
  env.fixtures['/repositories'] = { items: [{ repoKey: 'r1', root: '/remote/repo' }, { repoKey: 'r2', root: '/remote/app' }], total: 2 }
  env.fixtures['/finding-folders'] = { items: [{ folder: 'src', count: 3 }, { folder: 'src/api', count: 2 }], total: 2 }
  env.stateWrites = []
  env.queryOptions.length = 0
  try {
    const props = { selected: 'x', setSelected() {}, newScan: false, setNewScan() {} }
    let view = nodes(plugin.BrowserView({ kind: 'Findings', ...props }))
    const repo = view.find((n) => n.props?.['aria-label'] === 'Repository')
    const folder = view.find((n) => n.props?.['aria-label'] === 'Folder')
    assert.deepEqual(nodes(repo.props.children).map((n) => n.props.children), ['All repositories', 'repo', 'app'])
    assert.deepEqual(nodes(folder.props.children).map((n) => n.props.children), ['All folders', 'src/ (3)', 'src/api/ (2)'])
    repo.props.onChange({ target: { value: 'r2' } })
    assert.deepEqual(env.stateWrites[0]({ q: '', status: 'open', folder: 'src' }), { q: '', status: 'open', repo: 'r2', folder: '' })
    env.stateWrites = []
    folder.props.onChange({ target: { value: 'src/api' } })
    assert.deepEqual(env.stateWrites[0]({ q: '' }), { q: '', folder: 'src/api' })
    env.queryOptions.length = 0
    env.states = [{ q: '', status: 'open', severity: '', repo: 'r2', folder: 'src/api' }, 0]
    nodes(plugin.BrowserView({ kind: 'Findings', ...props }))
    const list = env.queryOptions.find((o) => o.queryKey[2] === '/findings')
    assert.equal(new URLSearchParams(list.queryKey[3]).get('repo'), 'r2')
    assert.equal(new URLSearchParams(list.queryKey[3]).get('folder'), 'src/api')
    assert.equal(new URLSearchParams(env.queryOptions.find((o) => o.queryKey[2] === '/finding-folders').queryKey[3]).get('repo'), 'r2')
    env.states = [{ q: '' }, 0]
    view = nodes(plugin.BrowserView({ kind: 'Scans', ...props }))
    assert.ok(view.some((n) => n.props?.['aria-label'] === 'Repository'))
    assert.equal(view.some((n) => n.props?.['aria-label'] === 'Folder'), false)
  } finally {
    env.fixtures['/repositories'] = { items: [{ repoKey: 'r1', root: '/remote/repo' }], total: 1 }
    delete env.fixtures['/finding-folders']
    delete env.states
    delete env.stateWrites
  }
})

test('a finished scan opens with a short summary and a link to the Markdown report', async () => {
  const partial = { completeness: 'partial', files: { total: 213, reviewed: 174, deferred: 38, unsupported: 1 } }
  const scan = { counts: { medium: 9, low: 9, chains: 0 }, dependencyCheck: { status: 'ok', packages: 826, vulnerablePackages: 16 } }
  assert.deepEqual(plugin.reportSummary(scan, partial), [
    '18 findings: 9 medium, 9 low.',
    "Partly reviewed: 174 of 213 files were read. Not covered: 38 files not read in full to stay within the review budget; 1 file in a format it can't review (such as a binary).",
    'Dependencies: 16 of 826 packages have published advisories.'
  ])
  assert.deepEqual(plugin.reportSummary({}, { coverage: { completeness: 'partial', files: { total: 3, reviewed: 3 } } }), [
    'No reportable findings.', 'Partly reviewed: 3 of 3 files were read. Some review work was not finished; see Coverage below. No findings here does not mean the code is safe.'])
  assert.deepEqual(plugin.reportSummary({ counts: { high: 1 } }, { completeness: 'complete', files: { total: 5 } }), ['1 finding: 1 high.', 'Fully reviewed: all 5 files in scope were read.'])
  env.fixtures['/scans/scan_test'].status = 'completed'
  env.fixtures['/scans/scan_test/coverage'] = partial
  try {
    const view = nodes(plugin.ScanDetail({ id: 'scan_test' }))
    const section = view.find((n) => n.props?.className === 'hs-report')
    assert.match(render(section), /Partly reviewed: 174 of 213/)
    const link = nodes(section).find((n) => n.type === 'button' && n.props.children === 'Full report (.md)')
    env.response = { scanId: 'scan_test', content: '# Report', contentType: 'text/markdown', filename: 'report.md' }
    let saved
    globalThis.document = { createElement: () => ({ click() { saved = this.download } }) }
    const urls = globalThis.URL
    globalThis.URL = { createObjectURL: () => 'blob:x', revokeObjectURL() {} }
    try {
      await link.props.onClick()
    } finally {
      globalThis.URL = urls
      delete globalThis.document
    }
    assert.equal(env.calls.at(-1).path, '/exports/scan_test/md')
    assert.equal(saved, 'report.md')
    env.fixtures['/scans/scan_test'].status = 'awaiting_analysis'
    assert.equal(nodes(plugin.ScanDetail({ id: 'scan_test' })).some((n) => n.props?.className === 'hs-report'), false)
  } finally {
    delete env.response
    delete env.fixtures['/scans/scan_test/coverage']
    env.fixtures['/scans/scan_test'].status = 'awaiting_analysis'
  }
})

test('scan rows offer Open and Delete on right-click; running scans cannot be deleted', async () => {
  const opened = []
  const deleted = []
  const done = { scanId: 'scan_done', status: 'completed', target: { root: '/remote/project' } }
  const row = (r) => nodes(plugin.ScanRow({ row: r, selected: false, onSelect: (id) => opened.push(id), onDeleted: (id) => deleted.push(id) }))
  const view = row(done)
  const trigger = view.find((n) => n.type === 'context-menu-trigger')
  assert.equal(trigger.props.asChild, true)
  assert.equal(view.filter((n) => n.type === 'button' && n.props.className === 'hs-item').length, 1)
  const items = view.filter((n) => n.type === 'context-menu-item')
  assert.deepEqual(items.map((n) => n.props.children), ['Open', 'Delete'])
  items[0].props.onSelect()
  assert.deepEqual(opened, ['scan_done'])

  globalThis.window = { confirm: () => false }
  const before = env.calls.length
  await items[1].props.onSelect()
  assert.equal(env.calls.length, before)
  assert.deepEqual(deleted, [])
  window.confirm = () => true
  env.response = { scanId: 'scan_done', deleted: true }
  await items[1].props.onSelect()
  assert.equal(env.calls.at(-1).path, '/scans/scan_done/delete')
  assert.deepEqual(env.calls.at(-1).options.body, { confirm: 'scan_done' })
  assert.deepEqual(deleted, ['scan_done'])
  delete env.response
  delete globalThis.window

  const running = row({ ...done, status: 'running' }).filter((n) => n.type === 'context-menu-item').at(-1)
  assert.equal(running.props.disabled, true)
  assert.match(running.props.children, /stop the scan first/)
})

test('select values ellipsize through the slot Radix keeps, not the className it drops', () => {
  assert.match(source, /\.hs-select-trigger>\[data-slot=select-value\]\{min-width:0;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap/)
})

test('back-to-back repeated activity reads as one line with a count', () => {
  const rows = [
    { id: 1, kind: 'worker.accepted', message: 'Worker result recorded.' },
    { id: 2, kind: 'coverage.updated', message: 'Coverage updated' },
    { id: 3, kind: 'coverage.updated', message: 'Coverage updated' },
    { id: 4, kind: 'coverage.updated', message: 'Coverage updated' },
    { id: 5, kind: 'autoresume.paused', message: 'Could not continue automatically.' },
    { id: 6, kind: 'worker.accepted', message: 'Worker result recorded.' },
    { id: 7, kind: 'autoresume.paused', message: 'Could not continue automatically.' },
    { id: 8, kind: 'note', message: 'Coverage updated' }
  ]
  const lines = plugin.collapseEvents(rows).map((e) => [e.id, e.text, e.count])
  // Only consecutive repeats of the same kind merge, so the order of events is kept.
  assert.deepEqual(lines, [
    [1, 'Worker result recorded.', 1], [2, 'Coverage updated', 3], [5, 'Could not continue automatically.', 1],
    [6, 'Worker result recorded.', 1], [7, 'Could not continue automatically.', 1], [8, 'Coverage updated', 1]
  ])
  assert.deepEqual(plugin.collapseEvents([{ id: 9, kind: 'x' }]).map((e) => e.text), ['x'])
  assert.deepEqual(plugin.collapseEvents([]), [])
})
