// Render the actual tracked bundle with React DOM and the public SDK boundary.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/memory-wiki', runScripts: 'outside-only' });
global.window = dom.window;
global.document = window.document;
global.navigator = window.navigator;
global.IS_REACT_ACT_ENVIRONMENT = true;
const React = require('react');
const { act } = React;
const { createRoot } = require('react-dom/client');
const subject = (name) => ({ slug: name, name, keywords: [], snippets: [], session_count: 1 });
const overview = { subjects: [subject('Overview topic')], daily_logs: [], recent_sessions: [] };
const requests = [];
const timers = new Map();
let nextTimer = 0;
window.setTimeout = (fn) => { timers.set(++nextTimer, fn); return nextTimer; };
window.clearTimeout = (id) => timers.delete(id);
const component = (tag) => ({ children, tone, ghost, size, ...props }) => React.createElement(tag, props, children);
window.__HERMES_PLUGIN_SDK__ = {
  React, hooks: React,
  components: Object.fromEntries(['Card', 'CardHeader', 'CardTitle', 'CardContent', 'Badge', 'Button', 'Input'].map(name => [name, component(name === 'Input' ? 'input' : name === 'Button' ? 'button' : 'div')])),
  utils: { cn: (...args) => args.filter(Boolean).join(' ') },
  fetchJSON: (url, init) => new Promise((resolve, reject) => requests.push({ url, init, resolve, reject })),
};
let Page;
window.__HERMES_PLUGINS__ = { register: (_, page) => { Page = page; } };
window.eval(readFileSync('dashboard/dist/index.js', 'utf8'));
const content = () => document.body.textContent;
async function type(value) {
  await act(async () => {
    const input = document.querySelector('input');
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, value);
    input.dispatchEvent(new window.Event('input', { bubbles: true }));
  });
}
async function debounce() {
  await act(async () => { for (const [id, fn] of timers) { timers.delete(id); fn(); } });
}

test('loading, errors, empty, stale replies, cancellation, clearing and unmount', async () => {
  const root = createRoot(document.getElementById('root'));
  await act(async () => root.render(React.createElement(Page)));
  await act(async () => requests.shift().resolve(overview));
  assert.match(content(), /Overview topic/);
  await type('alpha');
  assert.match(document.querySelector('[role="status"]').textContent, /Searching/);
  assert.doesNotMatch(content(), /Overview topic|No subjects match/);
  assert.equal(requests.length, 0, 'status appears during debounce');
  await debounce();
  const alpha = requests.shift();
  assert.match(alpha.url, /q=alpha/);
  assert.equal(alpha.init.signal.aborted, false);
  await type('beta');
  assert.equal(alpha.init.signal.aborted, true);
  await debounce();
  const beta = requests.shift();
  await act(async () => beta.resolve({ subjects: [subject('Beta topic')] }));
  assert.match(content(), /Beta topic/);
  await act(async () => alpha.resolve({ subjects: [subject('Stale alpha')] }));
  assert.doesNotMatch(content(), /Stale alpha/);
  await type('failure');
  assert.doesNotMatch(content(), /Beta topic/);
  await debounce();
  await act(async () => requests.shift().reject(new Error('Network down')));
  assert.match(document.querySelector('[role="alert"]').textContent, /search/i);
  assert.doesNotMatch(content(), /No subjects match/);
  await type('empty');
  await debounce();
  await act(async () => requests.shift().resolve({ subjects: [] }));
  assert.match(content(), /No subjects match/);
  await type('pending');
  await debounce();
  const pending = requests.shift();
  await type('');
  assert.equal(pending.init.signal.aborted, true);
  assert.match(content(), /Overview topic/);
  await act(async () => pending.reject(new Error('SDK wraps abort')));
  assert.equal(document.querySelector('[role="alert"]'), null);
  await type('unmount');
  await debounce();
  const last = requests.shift();
  await act(async () => root.unmount());
  assert.equal(last.init.signal.aborted, true);
  await act(async () => last.resolve({ subjects: [subject('Too late')] }));
  assert.equal(document.getElementById('root').textContent, '');
});

test('unmount cancels overview transport and pending debounce', async () => {
  let root = createRoot(document.getElementById('root'));
  await act(async () => root.render(React.createElement(Page)));
  const initial = requests.shift();
  await act(async () => root.unmount());
  assert.equal(initial.init.signal.aborted, true);
  await act(async () => initial.resolve(overview));
  assert.equal(document.getElementById('root').textContent, '');
  root = createRoot(document.getElementById('root'));
  await act(async () => root.render(React.createElement(Page)));
  await act(async () => requests.shift().resolve(overview));
  await type('debouncing');
  assert.equal(timers.size, 1);
  await act(async () => root.unmount());
  assert.equal(timers.size, 0);
  assert.equal(requests.length, 0);
});
