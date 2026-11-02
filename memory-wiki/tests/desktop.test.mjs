import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';

// npm test bundles the production plugin with the SDK boundary before starting node:test.
const { default: plugin, testHost: host, testQueryClient: queryClient, createTestI18n, testLocale, testBundles } = await import('../node_modules/.memory-wiki-desktop-test.mjs');
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
async function mount(rest = async path => response(path), locale = 'en') {
  queryClient.removeQueries(); host.state.connectionId.set('local'); host.state.profile.set('default'); testLocale.set(locale);
  const contributions = [], cleanups = [];
  const track = fn => { cleanups.push(fn); return fn; };
  // registry.ts keys contributions/disposers by area + id, not object identity.
  const remove = row => {
    const i = contributions.findIndex(item => item.id === row.id && item.area === row.area);
    if (i >= 0) contributions.splice(i, 1);
  };
  const registerMany = rows => {
    rows.forEach(row => { remove(row); contributions.push(row); });
    return track(() => rows.forEach(remove));
  };
  plugin.register({ source: 'plugin:memory-wiki', rest, onDispose: track,
    i18n: createTestI18n(plugin.id, track), registerMany });
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

const searchFor = async value => settle(() => {
  const input = rootNode.querySelector('input');
  Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, value);
  input.dispatchEvent(new window.Event('input', { bubbles: true }));
});
const switchLocale = locale => settle(() => testLocale.set(locale));
const chrome = (page, zh) => {
  assert.equal(rootNode.querySelector('h1').textContent, zh ? '记忆百科' : 'Memory Wiki');
  assert.equal(rootNode.querySelector('nav').getAttribute('aria-label'), zh ? '记忆百科栏目' : 'Memory Wiki sections');
  assert.equal(page.contributions.find(row => row.area === 'sidebar.nav').data.label, zh ? '记忆百科' : 'Memory Wiki');
  assert.equal(page.contributions.find(row => row.area === 'palette').data.label, zh ? '打开记忆百科' : 'Open Memory Wiki');
  assert.equal(page.contributions.length, 3);
};
test('Chinese bundles cover all English keys, chrome, aria, details and untouched user content', async () => {
  const page = await mount(undefined, 'zh');
  try {
    chrome(page, true);
    const bundles = testBundles(plugin.id);
    const leaves = (tree, prefix = '') => Object.entries(tree).flatMap(([key, value]) =>
      typeof value === 'object' ? leaves(value, `${prefix}${key}.`) : [`${prefix}${key}`]);
    assert.deepEqual(leaves(bundles.zh).sort(), leaves(bundles.en).sort());
    assert.match(text(), /不是 AI 重写的百科，也不读取 Hindsight 数据/);
    assert.equal(rootNode.querySelector('input').getAttribute('aria-label'), '搜索主题');
    assert.equal(rootNode.querySelector('input').placeholder, '搜索主题');
    await click('React'); assert.match(text(), /会话片段/); assert.match(text(), /Snippet text/);
    assert.match(text(), /1 个会话 · 2 条消息/); assert.match(text(), /用户/);
    await click('返回'); await click('每日记录'); await click('2026-10-21 · 1 个会话');
    assert.match(text(), /会话历史中的活动/); assert.match(text(), /Implemented UI/);
    await click('最近会话'); assert.match(text(), /Build UI/);
    await click('固定记忆'); assert.match(text(), /智能体笔记/); assert.match(text(), /用户资料/);
    assert.match(text(), /Agent entry/); assert.match(text(), /User entry/);
    assert.equal(subject.snippets[0].role, 'user'); assert.equal(day.work_items[0].kind, 'coding');
  } finally { await page.close(); }
});
test('English and unsupported host locales fall back without changing navigation contracts', async () => {
  for (const locale of ['en', 'fr', 'zh-hant']) {
    const page = await mount(undefined, locale);
    try { chrome(page, false); assert.ok(button('Subjects')); assert.equal(rootNode.querySelector('input').placeholder, 'Search subjects'); }
    finally { await page.close(); }
  }
});
test('live locale changes preserve each active tab, search and both details with zero new reads', async () => {
  const calls = [];
  const page = await mount(async path => { calls.push(path); return response(path); });
  const route = page.contributions.find(row => row.area === 'routes');
  const roundtrip = async (enTab, zhTab, expected) => {
    const before = [...calls], main = rootNode.querySelector('main');
    for (const [locale, label] of [['zh', zhTab], ['en', enTab]]) {
      await switchLocale(locale); chrome(page, locale === 'zh');
      assert.equal(button(label).getAttribute('aria-pressed'), 'true');
      assert.equal(rootNode.querySelector('main'), main);
      assert.equal(page.contributions.find(row => row.area === 'routes'), route);
      assert.match(text(), expected); assert.deepEqual(calls, before);
    }
  };
  try {
    await searchFor('React'); await roundtrip('Subjects', '主题', /React/);
    assert.equal(rootNode.querySelector('input').value, 'React');
    await click('React'); await roundtrip('Subjects', '主题', /Snippet text/);
    await click('Back'); assert.equal(rootNode.querySelector('input').value, 'React');
    await click('Daily logs'); await roundtrip('Daily logs', '每日记录', /2026-10-21/);
    await click('2026-10-21 · 1 sessions'); await roundtrip('Daily logs', '每日记录', /Implemented UI/);
    await click('React'); await roundtrip('Daily logs', '每日记录', /Snippet text/);
    await click('Recent sessions'); await roundtrip('Recent sessions', '最近会话', /Build UI/);
    await click('Persistent Memory'); await roundtrip('Persistent Memory', '固定记忆', /Agent entry/);
    await click('Subjects'); assert.equal(rootNode.querySelector('input').value, 'React');
  } finally { await page.close(); }
  await switchLocale('zh'); assert.equal(page.contributions.length, 0); assert.equal(testBundles(plugin.id), undefined);
});
test('Chinese loading, empty results, backend errors and missing-detail retry are localized', async () => {
  let resolve, fail = true;
  const page = await mount(path => path.startsWith('/subjects?')
    ? new Promise(done => { resolve = done; })
    : fail ? Promise.reject(Error('HTTP 404')) : Promise.resolve(response(path)), 'zh');
  try {
    assert.match(text(), /正在加载记忆百科/);
    await settle(() => resolve({ subjects: [subject] }));
    await click('React'); assert.match(text(), /未找到条目/); assert.ok(button('返回')); assert.ok(button('重试'));
    assert.match(text(), /hermes plugins enable memory-wiki/);
    fail = false; await click('重试'); assert.match(text(), /Snippet text/);
    await click('返回'); await searchFor('absent'); await settle(() => resolve({ subjects: [] }));
    assert.match(text(), /没有匹配搜索条件的主题/);
  } finally { await page.close(); }
  for (const error of ['HTTP 403 plugin disabled', 'HTTP 500']) {
    const broken = await mount(async () => { throw Error(error); }, 'zh');
    try { assert.match(text(), /记忆百科暂不可用/); assert.ok(button('重试')); assert.doesNotMatch(text(), /Could not read|If the backend/); }
    finally { await broken.close(); }
  }
});
test('Chinese empty states and unknown protocol values preserve backend text', async () => {
  let empty = true;
  const page = await mount(async path => {
    if (path.startsWith('/subjects?')) return { subjects: empty ? [] : [subject] };
    if (path.startsWith('/subjects/')) return { subject: { ...subject, snippets: [], sessions: [] } };
    if (path === '/days?limit=60') return { daily_logs: empty ? [] : [day] };
    if (path.startsWith('/days/')) return { daily_log: { ...day, work_items: [], sessions: [] } };
    return { recent_sessions: [], notes: {} };
  }, 'zh');
  try {
    assert.match(text(), /暂无主题/);
    await click('每日记录'); assert.match(text(), /暂无每日记录/);
    await click('最近会话'); assert.match(text(), /暂无会话/);
    await click('固定记忆'); assert.match(text(), /暂无智能体笔记/); assert.match(text(), /暂无用户资料/);
    empty = false; await click('主题'); await searchFor('React'); await click('React');
    assert.match(text(), /暂无片段/); assert.match(text(), /暂无会话/);
  } finally { await page.close(); }
  const raw = await mount(async path => path === '/subjects/react' ? { subject: { ...subject,
    snippets: [{ role: 'custom-role', timestamp: 0, text: 'User original 中文' }],
    sessions: [{ id: 's2', title: 'Original title', source: 'custom-source', preview: 'Original preview' }],
  } } : path.startsWith('/days/') ? { daily_log: { ...day, work_items: [] } } : response(path), 'zh');
  try {
    await click('React'); assert.match(text(), /custom-role · 时间未知: User original 中文/);
    assert.match(text(), /custom-source · 时间未知/); assert.match(text(), /Original title/); assert.match(text(), /Original preview/);
    await click('每日记录'); await click('2026-10-21 · 1 个会话'); assert.match(text(), /暂无活动条目/);
  } finally { await raw.close(); }
});
test('actual activity kinds, gateway sources and generated tool labels localize without rewriting raw text', async () => {
  const detail = { ...day, sessions: [
    { id: 'wx', title: '微信演示原文', source: 'weixin', last_active: 1 },
    { id: 'mail', title: '邮件演示原文', source: 'email', last_active: 1 },
    { id: 'extension', title: '扩展平台原文', source: 'custom-platform', last_active: 1 },
  ], work_items: [
    { kind: 'coding', text: 'fix 原文' }, { kind: 'research', text: 'research 原文' },
    { kind: 'planning', text: 'plan roadmap' }, { kind: 'conversation', text: '日常沟通' },
    { kind: 'tool', text: 'Tool: web_search' }, { kind: 'tool', text: 'Tool: tool call' },
    { kind: 'conversation', text: 'Tool: literal user text' }, { kind: 'tool', text: 'unprefixed raw text' },
  ] };
  const before = JSON.stringify(detail);
  const page = await mount(path => Promise.resolve(path.startsWith('/days/') ? { daily_log: detail } : response(path)), 'zh');
  try {
    await click('每日记录'); await click('2026-10-21 · 1 个会话');
    for (const value of ['编程: fix 原文', '研究: research 原文', '规划: plan roadmap', '对话: 日常沟通',
      '工具: web_search', '工具: 工具调用', 'Tool: literal user text', 'unprefixed raw text',
      '微信 ·', '电子邮件 ·', 'custom-platform ·']) assert.ok(text().includes(value), value);
    assert.doesNotMatch(text(), /Tool: web_search|Tool: tool call/);
    await switchLocale('en');
    for (const value of ['planning: plan roadmap', 'conversation: 日常沟通', 'tool: Tool: web_search',
      'tool: Tool: tool call', 'weixin ·', 'email ·', 'custom-platform ·']) assert.ok(text().includes(value), value);
    assert.equal(JSON.stringify(detail), before);
  } finally { await page.close(); }
});

test('locale switching keeps pending detail requests and translates asynchronous open errors at notification time', async () => {
  let resolve, rejectOpen, reads = 0;
  const oldOpen = host.openSession, oldNotify = host.notify;
  const page = await mount(path => { reads++; return path === '/subjects/react' ? new Promise(done => { resolve = done; }) : Promise.resolve(response(path)); });
  try {
    await click('React'); const before = reads;
    await switchLocale('zh'); assert.match(text(), /正在加载详情/); assert.equal(reads, before);
    await settle(() => resolve({ subject })); assert.match(text(), /Snippet text/);
    let notice; host.notify = value => { notice = value; };
    host.openSession = () => new Promise((_, reject) => { rejectOpen = reject; });
    await switchLocale('en'); await click('Build UI'); await switchLocale('zh');
    await settle(() => rejectOpen(Error('synthetic failure')));
    assert.equal(notice.message, '无法打开会话。');
  } finally { host.openSession = oldOpen; host.notify = oldNotify; await page.close(); }
});
