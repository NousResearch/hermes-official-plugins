import test from 'node:test';
import assert from 'node:assert/strict';
import { act } from 'react';
import { mount, fixture, raw } from './dashboard-entry.mjs';

async function withPage(options, run) {
  const page = await mount(options);
  try { await run(page); } finally { await page.close(); }
}

test('HTML Chinese starts translated with accessible controls and unchanged source content', () => withPage({}, async p => {
  for (const text of ['记忆百科', '主题', '每日记录', '最近会话', '固定记忆', '非 AI 生成', 'Hindsight', '1 个会话', raw]) assert.ok(p.text().includes(text), text);
  assert.equal(p.query('input').getAttribute('aria-label'), '搜索主题');
  assert.equal(p.query('select').getAttribute('aria-label'), '记忆百科语言');
  assert.equal(p.query('select').value, 'auto');
  assert.equal(p.w.document.documentElement.lang, 'zh-CN');
}));

for (const [name, options, title] of [
  ['HTML English outranks navigator Chinese', { lang: 'en-GB', languages: ['zh-CN'] }, 'Memory Wiki'],
  ['navigator Chinese without HTML lang', { lang: '', languages: ['zh-TW'] }, '记忆百科'],
  ['unknown HTML locale falls back to English', { lang: 'fr', languages: ['zh-CN'] }, 'Memory Wiki'],
  ['unknown navigator falls back to English', { lang: '', languages: ['ja-JP'] }, 'Memory Wiki'],
  ['saved English overrides host', { saved: 'en' }, 'Memory Wiki'],
  ['invalid saved preference uses automatic', { saved: 'invalid' }, '记忆百科'],
]) test(name, () => withPage(options, p => assert.equal(p.query('h1').textContent, title)));

test('switch preserves search, stable URLs, detail and exact original text without requests', () => withPage({}, async p => {
  await p.search('中文');
  assert.ok(p.calls.some(url => url.includes('q=%E4%B8%AD%E6%96%87')));
  const count = p.calls.length;
  await p.locale('en');
  assert.equal(p.query('input').value, '中文');
  assert.equal(p.calls.length, count);
  assert.equal(p.w.localStorage.getItem('memory-wiki.locale'), 'en');
  assert.equal(p.w.localStorage.length, 1);
  await p.click('a[href="#/subjects/stable-subject"]');
  const detailCalls = p.calls.length;
  assert.equal(p.query('h1').textContent, '中文主题');
  await p.locale('zh-CN');
  assert.equal(p.w.location.hash, '#/subjects/stable-subject');
  assert.ok(p.text().includes('相关会话'));
  assert.ok(p.text().includes('用户'));
  assert.ok(p.text().includes(raw));
  assert.equal(p.calls.length, detailCalls);
  await p.click('button');
  assert.equal(p.query('input').value, '中文');
}));

test('automatic locale observes host lang without changing host or reloading data', () => withPage({}, async p => {
  const count = p.calls.length;
  await act(async () => { p.w.document.documentElement.lang = 'en'; });
  await p.tick();
  assert.equal(p.query('h1').textContent, 'Memory Wiki');
  await p.locale('zh-CN');
  assert.equal(p.w.document.documentElement.lang, 'en');
  await p.locale('auto');
  assert.equal(p.query('h1').textContent, 'Memory Wiki');
  assert.equal(p.calls.length, count);
}));

test('storage unavailable does not prevent startup or language selection', () => withPage({ blockedStorage: true }, async p => {
  assert.equal(p.query('h1').textContent, '记忆百科');
  await p.locale('en');
  assert.equal(p.query('h1').textContent, 'Memory Wiki');
}));

test('translated loading remains switchable', () => withPage({ respond: () => new Promise(() => {}) }, async p => {
  assert.ok(p.text().includes('正在加载记忆百科'));
  assert.ok(p.query('[role="status"]'));
  await p.locale('en');
  assert.ok(p.text().includes('Loading memory wiki'));
  assert.equal(p.calls.length, 1);
}));

for (const [name, hash, key] of [['overview', '', 'overview'], ['subject', '#/subjects/stable-subject', 'subject'], ['day', '#/days/2026-10-01', 'daily_log']]) {
  test(`${name} localized error and retry use original endpoint`, () => withPage({ hash, respond: (url, count, data) => {
    if (count === 1) throw new Error('backend failure');
    return key === 'overview' ? data : { [key]: key === 'subject' ? data.subjects[0] : data.daily_logs[0] };
  } }, async p => {
    assert.ok(p.query('[role="alert"]'));
    assert.ok(p.text().includes('加载失败'));
    await p.locale('en');
    assert.ok(p.text().includes('Failed to load'));
    const button = [...p.w.document.querySelectorAll('button')].find(b => b.textContent === 'Retry');
    assert.ok(button);
    await act(async () => button.click());
    assert.ok(p.query('h1'));
    assert.equal(p.calls.length, 2);
    assert.equal(p.calls[0], p.calls[1]);
  }));
}

test('source IDs and generated tool labels localize in overview and detail without altering data', async () => {
  const data = fixture();
  data.recent_sessions[0].source = 'weixin';
  data.daily_logs[0].sessions[0].source = 'weixin';
  data.daily_logs[0].work_items = [
    { kind: 'tool', text: 'Tool: web_search', session_id: 'demo', timestamp: 1 },
    { kind: 'tool', text: 'Tool: tool call', session_id: 'demo', timestamp: 2 },
    { kind: 'conversation', text: 'Tool: literal user text', session_id: 'demo', timestamp: 3 },
  ];
  const before = JSON.stringify(data);
  for (const hash of ['', '#/days/2026-10-01']) await withPage({ data, hash }, async p => {
    assert.ok(p.text().includes('微信'));
    assert.ok(p.text().includes('工具: web_search'));
    assert.ok(p.text().includes('工具: 工具调用'));
    assert.ok(p.text().includes('Tool: literal user text'));
    assert.ok(!p.text().includes('Tool: web_search'));
    assert.ok(!p.text().includes('Tool: tool call'));
    await p.locale('en');
    assert.ok(p.text().includes('weixin'));
    assert.ok(p.text().includes('Tool: web_search'));
    assert.ok(p.text().includes('Tool: tool call'));
    assert.equal(JSON.stringify(p.data), before);
  });
});

test('daily detail localizes kind and counts only, preserving data and route', () => withPage({ hash: '#/days/2026-10-01' }, async p => {
  const before = JSON.stringify(p.data);
  assert.ok(p.text().includes('工具'));
  assert.ok(p.text().includes('1 个会话'));
  assert.ok(p.text().includes(raw));
  await p.locale('en');
  assert.ok(p.text().includes('1 session'));
  assert.equal(JSON.stringify(p.data), before);
  assert.equal(p.w.location.hash, '#/days/2026-10-01');
  assert.equal(p.calls.length, 1);
}));

test('empty states translated and search failure is retryable rather than empty', () => withPage({ data: { ...fixture(), subjects: [], daily_logs: [], recent_sessions: [] }, respond: (url, count, data) => {
  if (url.includes('/subjects?') && count === 2) throw new Error('search failed');
  return url.includes('/subjects?') ? { subjects: [] } : data;
} }, async p => {
  for (const text of ['暂无主题', '暂无每日记录', '未找到会话']) assert.ok(p.text().includes(text), text);
  await p.search('missing');
  assert.ok(p.text().includes('搜索失败'));
  assert.ok(!p.text().includes('没有匹配的主题'));
  await p.click('button');
  await p.tick(300);
  assert.ok(p.text().includes('没有匹配的主题'));
}));

test('all backend kind IDs translate for display; unknown IDs fall back unchanged', async () => {
  const data = fixture();
  data.daily_logs[0].work_items = ['coding', 'research', 'planning', 'conversation', 'future-kind', 'constructor'].map((kind, i) => ({ kind, text: raw, session_id: 's', timestamp: i }));
  await withPage({ data, hash: '#/days/2026-10-01' }, async p => {
    for (const value of ['编程', '研究', '规划', '对话', 'future-kind', 'constructor']) assert.ok(p.text().includes(value), value);
    await p.locale('en');
    for (const value of ['coding', 'research', 'planning', 'conversation']) assert.ok(p.text().includes(value), value);
    assert.equal(p.calls.length, 1);
  });
});

test('automatic mode follows navigator languagechange when HTML lang is absent', () => withPage({ lang: '', languages: ['en'] }, async p => {
  await act(async () => {
    Object.defineProperty(p.w.navigator, 'languages', { value: ['zh-CN'], configurable: true });
    p.w.dispatchEvent(new p.w.Event('languagechange'));
  });
  assert.equal(p.query('h1').textContent, '记忆百科');
  assert.equal(p.calls.length, 1);
}));

test('late subject response cannot replace a newer detail route', async () => {
  let resolveOld;
  await withPage({ hash: '#/subjects/old', respond: url => url.endsWith('/old')
    ? new Promise(resolve => { resolveOld = resolve; })
    : { subject: fixture('new-route-content').subjects[0] } }, async p => {
    await act(async () => { p.w.location.hash = '#/subjects/new'; });
    await p.tick(10);
    assert.equal(p.query('h1').textContent, 'new-route-content');
    await act(async () => resolveOld({ subject: fixture('old-route-content').subjects[0] }));
    assert.equal(p.query('h1').textContent, 'new-route-content');
  });
});

test('separate profile mounts fetch their own data and never persist records', async () => {
  await withPage({ data: fixture('profile-A-only') }, async p => {
    await p.locale('en');
    assert.ok(p.text().includes('profile-A-only'));
    assert.deepEqual([...Array(p.w.localStorage.length)].map((_, i) => p.w.localStorage.key(i)), ['memory-wiki.locale']);
  });
  await withPage({ saved: 'en', data: fixture('profile-B-only') }, p => {
    assert.ok(p.text().includes('profile-B-only'));
    assert.ok(!p.text().includes('profile-A-only'));
    assert.deepEqual(p.calls, ['/api/plugins/memory-wiki/overview']);
  });
});
