// Test-only SDK boundary. UI signatures mirror Desktop's component prop types.
import React, { useEffect, useState, useSyncExternalStore } from 'react';
export const ROUTES_AREA = 'routes', SIDEBAR_NAV_AREA = 'sidebar.nav', PALETTE_AREA = 'palette';
function atom(value) {
  const listeners = new Set();
  return { get: () => value, set(next) { if (Object.is(value, next)) return; value = next; listeners.forEach(fn => fn()); },
    listen(fn) { listeners.add(fn); return () => listeners.delete(fn); } };
}
export const host = { state: { connectionId: atom('local'), profile: atom('default') },
  navigate() {}, openSession: async () => {}, notify() {} };
export const useValue = store => useSyncExternalStore(store.listen, store.get);
// Contract: apps/desktop/src/i18n/plugin-i18n.ts + runtime.ts.
// The test locale models useI18n().locale and the host's runtime locale together;
// it is a test control, NOT a new property on host or ctx.i18n.
export const testLocale = atom('en');
const bundles = new Map();
const bundleVersion = atom(0);
const lookup = (tree, key) => key.split('.').reduce((node, part) => node?.[part], tree);
function translate(id, locale, key, args) {
  for (const language of [locale, 'en']) {
    const value = lookup(bundles.get(id)?.[language], key);
    if (typeof value === 'string') return value;
    if (typeof value === 'function') return value(...args);
  }
  return key;
}
const merge = (base, patch) => {
  const result = { ...base };
  for (const [key, value] of Object.entries(patch)) {
    result[key] = value && typeof value === 'object' && base?.[key] && typeof base[key] === 'object'
      ? merge(base[key], value) : value;
  }
  return result;
};
export function createTestI18n(id, track) {
  return {
    register(locales) {
      bundles.set(id, merge(bundles.get(id), locales));
      bundleVersion.set(bundleVersion.get() + 1);
      return track(() => { bundles.delete(id); bundleVersion.set(bundleVersion.get() + 1); });
    },
    t: (key, ...args) => translate(id, testLocale.get(), key, args),
    onLocaleChange: listener => track(testLocale.listen(listener)),
  };
}
export function usePluginI18n(id) {
  const locale = useValue(testLocale), version = useValue(bundleVersion);
  return React.useCallback((key, ...args) => translate(id, locale, key, args), [id, locale, version]);
}
export const testBundles = id => bundles.get(id);
const cache = new Map();
export const queryClient = {
  cancelQueries() { for (const entry of cache.values()) entry.cancelled = true; return Promise.resolve(); },
  removeQueries() { cache.clear(); },
};
export function useQuery(options) {
  const key = JSON.stringify(options.queryKey);
  const [, update] = useState(0);
  let entry = cache.get(key);
  if (!entry) { entry = { isPending: true, data: undefined, isError: false, listeners: new Set() }; cache.set(key, entry); }
  const run = () => {
    if (entry.cancelled) return;
    Object.assign(entry, { isPending: true, isError: false });
    entry.listeners.forEach(fn => fn());
    Promise.resolve().then(options.queryFn).then(data => {
      if (!entry.cancelled) Object.assign(entry, { data, isPending: false });
    }, error => {
      if (!entry.cancelled) Object.assign(entry, { error, isError: true, isPending: false });
    }).finally(() => entry.listeners.forEach(fn => fn()));
  };
  useEffect(() => {
    const listener = () => update(n => n + 1);
    entry.listeners.add(listener);
    if (options.enabled && !entry.started) { entry.started = true; run(); }
    return () => { entry.listeners.delete(listener); };
  }, [key, options.enabled]);
  return { ...entry, refetch: run };
}
export function Button({ variant, size, children, ...props }) {
  if (!['outline', 'secondary', 'ghost', undefined].includes(variant)) throw Error('Unsupported Button variant');
  return React.createElement('button', props, children);
}
export function SearchField({ placeholder, value, onChange, 'aria-label': label }) {
  return React.createElement('input', { placeholder, value, 'aria-label': label,
    onChange: event => onChange(event.target.value) });
}
export const Skeleton = props => React.createElement('div', props);
export const EmptyState = ({ title, description }) => React.createElement('div', null, title, description);
export const ErrorState = ({ title, description, children }) => React.createElement('div', null,
  React.createElement('h2', null, title), React.createElement('p', null, description), children);
