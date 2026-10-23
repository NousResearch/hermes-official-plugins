import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';

// npm test bundles the production plugin with the SDK boundary before starting node:test.
const { default: plugin, testHost: host, testQueryClient: queryClient } = await import('../node_modules/.memory-wiki-desktop-test.mjs');
const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
Object.defineProperty(globalThis, 'navigator', { value: dom.window.navigator, configurable: true });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const { act } = await import('react');
const { createRoot } = await import('react-dom/client');
const rootNode = document.getElementById('root');
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
const settle = async fn => { await act(async () => { await fn?.(); await tick(); }); };
const text = () => rootNode.textContent;
const button = label => [...rootNode.querySelectorAll('button')].find(el => el.textContent === label);
const click = async label => { assert.ok(button(label), `button ${label}`); await settle(() => button(label).click()); };
const subject = { slug: 'react', name: 'React', session_count: 1, message_count: 2,
  keywords: ['React'], last_seen: 1, sessions: [{ id: 's1', title: 'Build UI', last_active: 1 }],
  snippets: [{ role: 'user', timestamp: 1, text: 'Snippet text' }] };
const day = { date: '2026-10-21', session_count: 1, message_count: 2, subjects: [subject],
  work_items: [{ kind: 'coding', text: 'Implemented UI' }], sessions: subject.sessions };
const overview = { recent_sessions: subject.sessions, notes: { memory: ['Agent entry'], user: ['User entry'] } };
function response(path) {
  if (path.startsWith('/subjects?')) return { subjects: path.includes('absent') ? [] : [subject] };
  if (path.startsWith('/subjects/')) return { subject };
  if (path === '/days?limit=60') return { daily_logs: [day] };
  if (path.startsWith('/days/')) return { daily_log: day };
  if (path === '/overview') return overview;
  throw Error(`Unexpected endpoint ${path}`);
}
async function mount(rest = async path => response(path)) {
  queryClient.removeQueries(); host.state.connectionId.set('local'); host.state.profile.set('default');
  const contributions = [], cleanups = [];
  plugin.register({ source: 'plugin:memory-wiki', rest, onDispose: fn => cleanups.push(fn),
    registerMany: rows => contributions.push(...rows) });
  const root = createRoot(rootNode);
  await settle(() => root.render(contributions.find(row => row.area === 'routes').render()));
  return { contributions, async close() { await settle(() => { cleanups.forEach(fn => fn()); root.unmount(); }); } };
}
test('production disk plugin registers opt-in route, navigation and command; browses all views', async () => {
  const page = await mount();
  try {
    assert.equal(plugin.id, 'memory-wiki'); assert.equal(plugin.defaultEnabled, false);
    assert.equal(page.contributions.length, 3);
    assert.equal(page.contributions[0].data.path, '/memory-wiki');
    let navigated; host.navigate = path => { navigated = path; };
    page.contributions[2].data.run(); assert.equal(navigated, '/memory-wiki');
    assert.match(text(), /React/);
    assert.equal(button('Subjects').getAttribute('aria-pressed'), 'true');
    await click('React'); assert.match(text(), /Snippet text/);
    await click('Back'); assert.ok(rootNode.querySelector('input'));
    const input = rootNode.querySelector('input'); assert.equal(input.getAttribute('aria-label'), 'Search subjects');
    await settle(() => {
      Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, 'absent');
      input.dispatchEvent(new window.Event('input', { bubbles: true }));
    });
    assert.match(text(), /No subjects match/);
    await click('Daily logs'); await click('2026-10-21 · 1 sessions'); assert.match(text(), /Implemented UI/);
    await click('React'); assert.match(text(), /Snippet text/); await click('Back');
    await click('Recent sessions'); let opened; host.openSession = async (id, opts) => { opened = [id, opts]; };
    await click('Build UI'); assert.deepEqual(opened, ['s1', { profile: 'default' }]);
    await click('Persistent Memory'); assert.match(text(), /Agent entry/); assert.match(text(), /User entry/);
    assert.equal(rootNode.querySelectorAll('textarea').length, 0);
  } finally { await page.close(); }
});
test('errors are retryable, disabled backend has commands, empty history and notes are explicit', async () => {
  let fail = true;
  const page = await mount(async path => { if (fail) throw Error('HTTP 403 plugin disabled'); return path === '/overview' ? { notes: {}, recent_sessions: [] } : { subjects: [] }; });
  try {
    assert.match(text(), /hermes plugins enable memory-wiki/);
    fail = false; await click('Retry'); assert.match(text(), /No subjects yet/);
    await click('Recent sessions'); assert.match(text(), /No sessions yet/);
    await click('Persistent Memory'); assert.match(text(), /No agent notes entries/); assert.match(text(), /No user profile entries/);
  } finally { await page.close(); }
  const broken = await mount(async () => { throw Error('HTTP 500'); });
  try { assert.match(text(), /Check the gateway connection/); assert.ok(button('Retry')); }
  finally { await broken.close(); }
});
test('profile and connection changes reset details/search and discard delayed old reads; dispose drops work', async () => {
  const pending = []; const calls = [];
  const page = await mount(path => {
    calls.push([host.state.connectionId.get(), host.state.profile.get(), path]);
    if (path === '/subjects/react') return new Promise(resolve => pending.push(resolve));
    return Promise.resolve(response(path));
  });
  try {
    await click('React'); assert.match(text(), /Back/);
    await settle(() => host.state.profile.set('research'));
    assert.ok(rootNode.querySelector('input')); assert.doesNotMatch(text(), /Snippet text|Back/);
    await settle(() => pending.shift()({ subject: { ...subject, name: 'PRIVATE OLD DETAIL' } }));
    assert.doesNotMatch(text(), /PRIVATE OLD DETAIL/);
    await click('React');
    await settle(() => host.state.connectionId.set('remote-b'));
    assert.ok(rootNode.querySelector('input')); assert.equal(rootNode.querySelector('input').value, '');
    await settle(() => pending.shift()({ subject: { ...subject, name: 'PRIVATE REMOTE DETAIL' } }));
    assert.doesNotMatch(text(), /PRIVATE REMOTE DETAIL/);
    assert.ok(calls.some(([c, p]) => c === 'remote-b' && p === 'research'));
    await click('React');
  } finally { await page.close(); }
  await settle(() => pending.shift()({ subject })); assert.equal(text(), '');
});

test('search responses cannot replace newer results or cross an active scope switch', async () => {
  const pending = [];
  const page = await mount(path => path.includes('q=private')
    ? new Promise(resolve => pending.push(resolve)) : Promise.resolve(response(path)));
  const search = async value => settle(() => {
    const input = rootNode.querySelector('input');
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, value);
    input.dispatchEvent(new window.Event('input', { bubbles: true }));
  });
  try {
    await search('private'); assert.match(text(), /Loading Memory Wiki/); assert.doesNotMatch(text(), /React/);
    await search('absent'); assert.match(text(), /No subjects match/);
    await settle(() => pending.shift()({ subjects: [{ ...subject, name: 'PRIVATE SEARCH' }] }));
    assert.doesNotMatch(text(), /PRIVATE SEARCH/);
    await search('private2');
    await settle(() => { host.state.profile.set('other'); host.state.connectionId.set('remote-c'); });
    assert.equal(rootNode.querySelector('input').value, ''); assert.match(text(), /React/);
    await settle(() => pending.shift()({ subjects: [{ ...subject, name: 'PRIVATE OTHER PROFILE' }] }));
    assert.doesNotMatch(text(), /PRIVATE OTHER PROFILE/);
  } finally { await page.close(); }
});
test('missing details keep Back and Retry; older hosts display sessions without an open action', async () => {
  const oldOpen = host.openSession; host.openSession = undefined;
  const page = await mount(async path => {
    if (path === '/subjects/react') throw Error('HTTP 404');
    if (path === '/days?limit=60') return { daily_logs: [] };
    return response(path);
  });
  try {
    await click('React'); assert.match(text(), /Entry not found/); assert.ok(button('Back')); assert.ok(button('Retry'));
    await click('Back'); await click('Daily logs'); assert.match(text(), /No daily logs yet/);
    await click('Recent sessions'); assert.match(text(), /Build UI/); assert.equal(button('Build UI'), undefined);
  } finally { host.openSession = oldOpen; await page.close(); }
});
