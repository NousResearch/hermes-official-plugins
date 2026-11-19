// Native Memory Wiki. Backend aggregation: Abdullah Raja (@Araja119), hermes-agent #31244.
// Plain disk-plugin ESM; React and the UI kit are supplied by Desktop.
import { host, useValue, useQuery, queryClient, Button, SearchField, EmptyState,
  ErrorState, Skeleton, ROUTES_AREA, SIDEBAR_NAV_AREA, PALETTE_AREA } from '@hermes/plugin-sdk';
import { useState } from 'react';
import { jsx, jsxs } from 'react/jsx-runtime';

const root = ['memory-wiki', 'desktop'];
const time = value => value ? new Date(value * 1000).toLocaleString() : 'Unknown time';
const action = (label, onClick, { key, ...props } = {}) => jsx(Button, {
  type: 'button', variant: 'outline', onClick, ...props, children: label,
}, key);
function Section({ title, children }) {
  return jsxs('section', { className: 'space-y-3', children: [
    jsx('h2', { className: 'text-base font-semibold', children: title }), children,
  ] });
}
function Entries({ items, empty, render }) {
  return items?.length ? jsx('ul', { className: 'space-y-3', children: items.map((item, i) =>
    jsx('li', { className: 'whitespace-pre-wrap break-words', children: render(item) }, i)) })
    : jsx(EmptyState, { title: empty });
}
function Failure({ error, retry, detail }) {
  const missing = /404|403|disabled|not found/i.test(String(error?.message));
  return jsx('div', { role: 'alert', children: jsx(ErrorState, {
    title: detail && /404|not found/i.test(String(error?.message)) ? 'Entry not found' : 'Memory Wiki unavailable',
    description: missing
      ? 'If the backend is missing or disabled, install and enable Memory Wiki on the connected server for this profile, then restart its gateway. A missing detail may also have left the recent history window.'
      : 'Could not read this profile’s memory history. Check the gateway connection and retry.',
    children: jsxs('div', { className: 'space-y-3', children: [
      missing && jsx('pre', { className: 'whitespace-pre-wrap text-xs', children:
        'hermes plugins install hermes-memory-wiki --enable\nhermes plugins enable memory-wiki' }),
      action('Retry', retry),
    ] }),
  }) });
}
function Sessions({ sessions, current }) {
  return jsx(Entries, { items: sessions, empty: 'No sessions yet.', render: session =>
    jsxs('div', { children: [
      typeof host.openSession === 'function'
        ? action(session.title || session.id, () => {
          if (!current()) return;
          // Read live atoms in the handler; the row belongs to this mounted scope.
          void host.openSession(session.id, { profile: host.state.profile.get() })
            .catch(() => host.notify({ kind: 'error', message: 'Could not open session.' }));
        }) : jsx('span', { children: session.title || session.id }),
      jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children:
        `${session.source || 'session'} · ${time(session.last_active)}` }),
      session.preview && jsx('p', { children: session.preview }),
    ] }) });
}
function Detail({ kind, id, useRead, go, current }) {
  const path = `/${kind}/${encodeURIComponent(id)}`;
  const query = useRead(path);
  const item = kind === 'subjects' ? query.data?.subject : query.data?.daily_log;
  return jsxs('div', { className: 'space-y-5', children: [
    action('Back', () => go(null)),
    query.isPending ? jsx('div', { role: 'status', children: ['Loading detail…', jsx(Skeleton, { className: 'h-24 w-full' }, 'skeleton')] })
      : query.isError ? jsx(Failure, { error: query.error, retry: query.refetch, detail: true })
      : !item ? jsx(EmptyState, { title: 'Entry not found' })
      : jsxs('div', { className: 'space-y-5', children: [
        jsx('h2', { className: 'text-xl font-semibold', children: item.name || item.date }),
        jsx('p', { children: `${item.session_count} sessions · ${item.message_count} messages` }),
        kind === 'subjects'
          ? jsxs(Section, { title: 'Session snippets', children: [
            jsx('p', { children: (item.keywords || []).join(' · ') }),
            jsx(Entries, { items: item.snippets, empty: 'No snippets.', render: s =>
              jsx('blockquote', { children: `${s.role || 'message'} · ${time(s.timestamp)}: ${s.text}` }) }),
          ] })
          : jsxs(Section, { title: 'Activity from session history', children: [
            jsx('div', { className: 'flex flex-wrap gap-2', children: item.subjects.map(s =>
              action(s.name, () => go({ kind: 'subjects', id: s.slug }), { key: s.slug })) }),
            jsx(Entries, { items: item.work_items, empty: 'No activity items.', render: w => `${w.kind}: ${w.text}` }),
          ] }),
        jsx(Section, { title: 'Related sessions', children: jsx(Sessions, { sessions: item.sessions, current }) }),
      ] }),
  ] });
}
function Page({ useRead, current }) {
  const [tab, setTab] = useState('Subjects');
  const [search, setSearch] = useState('');
  const [detail, setDetail] = useState(null);
  const overview = useRead('/overview', !detail && (tab === 'Recent sessions' || tab === 'Persistent Memory'));
  const subjects = useRead(`/subjects?q=${encodeURIComponent(search.trim())}&limit=100`, !detail && tab === 'Subjects');
  const days = useRead('/days?limit=60', !detail && tab === 'Daily logs');
  const query = tab === 'Subjects' ? subjects : tab === 'Daily logs' ? days : overview;
  let body;
  if (detail) body = jsx(Detail, { ...detail, useRead, go: setDetail, current }, `${detail.kind}/${detail.id}`);
  else if (query.isPending) body = jsx('div', { role: 'status', children: [
    'Loading Memory Wiki…', jsx(Skeleton, { className: 'h-24 w-full' }, 'skeleton'),
  ] });
  else if (query.isError) body = jsx(Failure, { error: query.error, retry: query.refetch });
  else if (tab === 'Subjects') body = jsx(Entries, { items: query.data.subjects,
    empty: search.trim() ? 'No subjects match your search.' : 'No subjects yet — start a session first.',
    render: s => jsxs('div', { children: [action(s.name, () => setDetail({ kind: 'subjects', id: s.slug })),
      jsx('p', { children: `${s.session_count} sessions · ${s.message_count} messages · ${time(s.last_seen)}` })] }) });
  else if (tab === 'Daily logs') body = jsx(Entries, { items: query.data.daily_logs, empty: 'No daily logs yet.',
    render: d => action(`${d.date} · ${d.session_count} sessions`, () => setDetail({ kind: 'days', id: d.date })) });
  else if (tab === 'Recent sessions') body = jsx(Sessions, { sessions: query.data.recent_sessions, current });
  else body = jsxs('div', { className: 'space-y-5', children: [
    jsx('p', { children: 'Read-only entries from MEMORY.md / USER.md. Edit through the agent’s memory tool.' }),
    ...[['Agent notes', 'memory'], ['User profile', 'user']].map(([title, key]) =>
      jsx(Section, { title, children: jsx(Entries, { items: query.data.notes?.[key], empty: `No ${title.toLowerCase()} entries.`, render: entry => entry }) }, key)),
  ] });
  return jsxs('main', { className: 'h-full overflow-auto p-5 space-y-5 text-sm text-(--ui-text-primary)', children: [
    jsx('h1', { className: 'text-xl font-semibold', children: 'Memory Wiki' }),
    jsx('p', { className: 'text-(--ui-text-secondary)', children:
      'Subjects and daily logs derived deterministically from this profile’s session history. No LLM-generated wiki or Hindsight data.' }),
    jsx('nav', { 'aria-label': 'Memory Wiki sections', className: 'flex flex-wrap gap-2', children:
      ['Subjects', 'Daily logs', 'Recent sessions', 'Persistent Memory'].map(label =>
        action(label, () => { setTab(label); setDetail(null); }, { key: label, variant: tab === label ? 'secondary' : 'outline', 'aria-pressed': tab === label })) }),
    !detail && tab === 'Subjects' && jsx(SearchField, { placeholder: 'Search subjects', 'aria-label': 'Search subjects', value: search, onChange: setSearch }),
    body,
  ] });
}
export default {
  id: 'memory-wiki', name: 'Memory Wiki', defaultEnabled: false,
  register(ctx) {
    let disposed = false;
    // A registration owns its cache, so hot reload cannot reuse a pending old request.
    const cache = [...root, ctx.source];
    const scopeNow = () => JSON.stringify([host.state.connectionId.get() || 'local', host.state.profile.get() || 'default']);
    function ScopedPage() {
      const connection = useValue(host.state.connectionId) || 'local';
      const profile = useValue(host.state.profile) || 'default';
      const scope = JSON.stringify([connection, profile]);
      const current = () => !disposed && scopeNow() === scope;
      function useRead(path, enabled = true) {
        return useQuery({
          queryKey: [...cache, connection, profile, path],
          enabled: enabled && current(), retry: false, gcTime: 0,
          // No previous/placeholder data: both scope and resource changes show loading.
          placeholderData: undefined,
          queryFn: async () => {
            if (!current()) throw new Error('Memory Wiki scope changed');
            // ctx.rest captures the route profile and connection before its first await.
            const result = await ctx.rest(path);
            if (!current()) throw new Error('Memory Wiki scope changed');
            return result;
          },
        });
      }
      return jsx(Page, { useRead, current }, scope);
    }
    ctx.onDispose(() => {
      disposed = true;
      void queryClient.cancelQueries({ queryKey: cache });
      queryClient.removeQueries({ queryKey: cache });
    });
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: '/memory-wiki' }, render: () => jsx(ScopedPage, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: '/memory-wiki', label: 'Memory Wiki', codicon: 'book' } },
      { id: 'open', area: PALETTE_AREA, data: { id: 'memory-wiki.open', label: 'Open Memory Wiki', keywords: ['memory', 'subjects', 'daily logs'], run: () => host.navigate('/memory-wiki') } },
    ]);
  },
};
