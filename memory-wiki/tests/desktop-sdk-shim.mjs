// Test-only SDK boundary. UI signatures mirror Desktop's component prop types.
import React, { useEffect, useState, useSyncExternalStore } from 'react';
export const ROUTES_AREA = 'routes', SIDEBAR_NAV_AREA = 'sidebar.nav', PALETTE_AREA = 'palette';
function atom(value) {
  const listeners = new Set();
  return { get: () => value, set(next) { value = next; listeners.forEach(fn => fn()); },
    listen(fn) { listeners.add(fn); return () => listeners.delete(fn); } };
}
export const host = { state: { connectionId: atom('local'), profile: atom('default') },
  navigate() {}, openSession: async () => {}, notify() {} };
export const useValue = store => useSyncExternalStore(store.listen, store.get);
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
