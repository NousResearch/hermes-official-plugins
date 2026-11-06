// Exercise the production web entry with the web SDK (not the Desktop SDK).
import React, { act } from 'react';
import { JSDOM } from 'jsdom';
import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';

const bundle = await build({
  entryPoints: [fileURLToPath(new URL('../dashboard/src/index.jsx', import.meta.url))],
  bundle: true, write: false, format: 'iife', platform: 'browser',
  jsxFactory: 'React.createElement', jsxFragment: 'React.Fragment',
});
export const raw = '原始中文：不要翻译 Memory Wiki / user / tool。';
export function fixture(name = '中文主题') {
  const session = { id: 'session-1', title: raw, preview: raw, source: 'cli', last_active: 1720000000 };
  const subject = { slug: 'stable-subject', name, session_count: 1, message_count: 2,
    first_seen: 1720000000, last_seen: 1720000000, keywords: ['中文关键词'], sessions: [session],
    snippets: [{ session_id: session.id, message_id: 1, timestamp: 1720000000, role: 'user', text: raw }] };
  const day = { date: '2026-10-01', session_count: 1, message_count: 2, last_active_max: 1720000000,
    subjects: [subject], sessions: [session], work_items: [{ session_id: session.id, timestamp: 1720000000, kind: 'tool', text: raw }] };
  return { subjects: [subject], daily_logs: [day], recent_sessions: [session],
    notes: { memory_count: 1, user_count: 1, memory: [raw], user: [raw] } };
}

export async function mount({ lang = 'zh-CN', languages = ['en-US'], saved, blockedStorage = false, hash = '', data = fixture(), respond } = {}) {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: `https://dashboard.invalid/memory-wiki${hash}`, runScripts: 'outside-only',
  });
  const w = dom.window;
  w.document.documentElement.lang = lang;
  Object.defineProperty(w.navigator, 'languages', { value: languages, configurable: true });
  Object.defineProperty(w.navigator, 'language', { value: languages[0] || '', configurable: true });
  if (saved !== undefined) w.localStorage.setItem('memory-wiki.locale', saved);
  if (blockedStorage) Object.defineProperty(w, 'localStorage', { get() { throw new Error('blocked'); } });
  globalThis.window = w;
  globalThis.document = w.document;
  Object.defineProperty(globalThis, 'navigator', { value: w.navigator, configurable: true });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const calls = [];
  let Page;
  const element = (tag) => ({ children, ghost, size, tone, ...props }) => React.createElement(tag, props, children);
  w.__HERMES_PLUGIN_SDK__ = {
    React, hooks: React,
    components: { Card: element('div'), CardHeader: element('div'), CardTitle: element('h3'),
      CardContent: element('div'), Badge: element('span'), Button: element('button'), Input: element('input') },
    utils: { cn: (...args) => args.filter(Boolean).join(' ') },
    fetchJSON: async (url) => {
      calls.push(url);
      if (respond) return respond(url, calls.length, data);
      if (url.endsWith('/overview')) return data;
      if (url.includes('/subjects?')) return { subjects: data.subjects };
      if (url.includes('/subjects/')) return { subject: data.subjects[0] };
      if (url.includes('/days/')) return { daily_log: data.daily_logs[0] };
      throw new Error(`Unexpected endpoint: ${url}`);
    },
  };
  w.__HERMES_PLUGINS__ = { register(name, component) {
    if (name !== 'memory-wiki') throw new Error('Unexpected plugin registration');
    Page = component;
  } };
  w.eval(bundle.outputFiles[0].text);
  const { createRoot } = await import('react-dom/client');
  const root = createRoot(w.document.getElementById('root'));
  await act(async () => root.render(React.createElement(Page)));
  const tick = async (ms = 0) => act(async () => { await new Promise(r => setTimeout(r, ms)); });
  return {
    w, calls, data, tick,
    text: () => w.document.body.textContent,
    query: (selector) => w.document.querySelector(selector),
    async locale(value) {
      const select = w.document.querySelector('select');
      if (!select) throw new Error('Language selector missing');
      await act(async () => { select.value = value; select.dispatchEvent(new w.Event('change', { bubbles: true })); });
    },
    async search(value) {
      const input = w.document.querySelector('input');
      await act(async () => {
        Object.getOwnPropertyDescriptor(w.HTMLInputElement.prototype, 'value').set.call(input, value);
        input.dispatchEvent(new w.Event('input', { bubbles: true }));
      });
      await tick(300);
    },
    async click(selector) {
      await act(async () => w.document.querySelector(selector).click());
      await tick(10);
    },
    async close() { await act(async () => root.unmount()); dom.window.close(); },
  };
}
