// Native Memory Wiki. Backend aggregation: Abdullah Raja (@Araja119), hermes-agent #31244.
// Plain disk-plugin ESM; React and the UI kit are supplied by Desktop.
import { host, useValue, useQuery, queryClient, usePluginI18n, Button, SearchField, EmptyState,
  ErrorState, Skeleton, ROUTES_AREA, SIDEBAR_NAV_AREA, PALETTE_AREA } from '@hermes/plugin-sdk';
import { useState } from 'react';
import { jsx, jsxs } from 'react/jsx-runtime';

const ID = 'memory-wiki';
const root = [ID, 'desktop'];
// Host locale ids and fallback are owned by Desktop: zh is Simplified Chinese.
// Every leaf is a string or positional interpolator (PluginLocaleBundles).
const locales = {
  en: {
    title: 'Memory Wiki', open: 'Open Memory Wiki', sections: 'Memory Wiki sections',
    description: 'Subjects and daily logs derived deterministically from this profile’s session history. No LLM-generated wiki or Hindsight data.',
    tabs: { subjects: 'Subjects', days: 'Daily logs', sessions: 'Recent sessions', memory: 'Persistent Memory' },
    search: 'Search subjects', back: 'Back', retry: 'Retry', loading: 'Loading Memory Wiki…', loadingDetail: 'Loading detail…',
    notFound: 'Entry not found', unavailable: 'Memory Wiki unavailable',
    missingBackend: 'If the backend is missing or disabled, install and enable Memory Wiki on the connected server for this profile, then restart its gateway. A missing detail may also have left the recent history window.',
    readError: 'Could not read this profile’s memory history. Check the gateway connection and retry.',
    openError: 'Could not open session.',
    noSubjects: 'No subjects yet — start a session first.', noMatches: 'No subjects match your search.',
    noDays: 'No daily logs yet.', noSessions: 'No sessions yet.', noSnippets: 'No snippets.', noActivity: 'No activity items.',
    snippets: 'Session snippets', activity: 'Activity from session history', related: 'Related sessions',
    notesHelp: 'Read-only entries from MEMORY.md / USER.md. Edit through the agent’s memory tool.',
    agentNotes: 'Agent notes', userProfile: 'User profile', noAgentNotes: 'No agent notes entries.', noUserProfile: 'No user profile entries.',
    counts: (sessions, messages) => `${sessions} sessions · ${messages} messages`,
    sessionCount: count => `${count} sessions`,
    time: value => value ? new Date(value * 1000).toLocaleString('en') : 'Unknown time',
    role: { user: 'user', assistant: 'assistant', system: 'system', developer: 'developer', tool: 'tool', message: 'message' },
    kind: { coding: 'coding', research: 'research', planning: 'planning', conversation: 'conversation', tool: 'tool', decision: 'decision', task: 'task', question: 'question' },
    toolText: label => `Tool: ${label}`,
    source: { session: 'session', cli: 'cli', desktop: 'desktop', web: 'web', local: 'local', telegram: 'telegram', discord: 'discord', whatsapp: 'whatsapp', whatsapp_cloud: 'whatsapp_cloud', slack: 'slack', signal: 'signal', mattermost: 'mattermost', matrix: 'matrix', email: 'email', sms: 'sms', dingtalk: 'dingtalk', api_server: 'api_server', webhook: 'webhook', msgraph_webhook: 'msgraph_webhook', feishu: 'feishu', wecom: 'wecom', wecom_callback: 'wecom_callback', weixin: 'weixin', bluebubbles: 'bluebubbles', qqbot: 'qqbot', yuanbao: 'yuanbao', relay: 'relay' },
    keyword: { memory: 'memory', subjects: 'subjects', days: 'daily logs' },
  },
  zh: {
    title: '记忆百科', open: '打开记忆百科', sections: '记忆百科栏目',
    description: '按确定性规则从当前配置的会话历史中整理主题和每日记录；不是 AI 重写的百科，也不读取 Hindsight 数据。',
    tabs: { subjects: '主题', days: '每日记录', sessions: '最近会话', memory: '固定记忆' },
    search: '搜索主题', back: '返回', retry: '重试', loading: '正在加载记忆百科…', loadingDetail: '正在加载详情…',
    notFound: '未找到条目', unavailable: '记忆百科暂不可用',
    missingBackend: '如果后端未安装或已停用，请在当前连接的服务器上为此配置安装并启用记忆百科，然后重启该服务器的网关。详情缺失也可能是因为条目已超出近期历史范围。',
    readError: '无法读取当前配置的记忆历史，请检查网关连接后重试。',
    openError: '无法打开会话。',
    noSubjects: '暂无主题，请先开始一个会话。', noMatches: '没有匹配搜索条件的主题。',
    noDays: '暂无每日记录。', noSessions: '暂无会话。', noSnippets: '暂无片段。', noActivity: '暂无活动条目。',
    snippets: '会话片段', activity: '会话历史中的活动', related: '相关会话',
    notesHelp: '这里只读展示 MEMORY.md / USER.md 中的条目，请通过智能体的记忆工具修改。',
    agentNotes: '智能体笔记', userProfile: '用户资料', noAgentNotes: '暂无智能体笔记。', noUserProfile: '暂无用户资料。',
    counts: (sessions, messages) => `${sessions} 个会话 · ${messages} 条消息`,
    sessionCount: count => `${count} 个会话`,
    time: value => value ? new Date(value * 1000).toLocaleString('zh-CN') : '时间未知',
    role: { user: '用户', assistant: '助手', system: '系统', developer: '开发者', tool: '工具', message: '消息' },
    kind: { coding: '编程', research: '研究', planning: '规划', conversation: '对话', tool: '工具', decision: '决策', task: '任务', question: '问题' },
    toolText: label => label === 'tool call' ? '工具调用' : label,
    source: { session: '会话', cli: '命令行', desktop: '桌面端', web: '网页端', local: '本地', telegram: '电报', discord: 'Discord', whatsapp: 'WhatsApp', whatsapp_cloud: 'WhatsApp 云', slack: 'Slack', signal: 'Signal', mattermost: 'Mattermost', matrix: 'Matrix', email: '电子邮件', sms: '短信', dingtalk: '钉钉', api_server: 'API 服务', webhook: 'Webhook 接入', msgraph_webhook: '微软 Graph 通知', feishu: '飞书', wecom: '企业微信', wecom_callback: '企业微信回调', weixin: '微信', bluebubbles: 'BlueBubbles', qqbot: 'QQ 机器人', yuanbao: '元宝', relay: '中继平台' },
    keyword: { memory: '记忆', subjects: '主题', days: '每日记录' },
  },
};
// Protocol tokens are display-only; unknown values and all user content stay intact.
const protocol = (t, group, value) => Object.hasOwn(locales.en[group], value) ? t(`${group}.${value}`) : value;
const activityText = (t, item) => item.kind === 'tool' && typeof item.text === 'string' && item.text.startsWith('Tool: ')
  ? t('toolText', item.text.slice(6)) : item.text;
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
  const t = usePluginI18n(ID);
  const missing = /404|403|disabled|not found/i.test(String(error?.message));
  return jsx('div', { role: 'alert', children: jsx(ErrorState, {
    title: t(detail && /404|not found/i.test(String(error?.message)) ? 'notFound' : 'unavailable'),
    description: t(missing ? 'missingBackend' : 'readError'),
    children: jsxs('div', { className: 'space-y-3', children: [
      missing && jsx('pre', { className: 'whitespace-pre-wrap text-xs', children:
        'hermes plugins install hermes-memory-wiki --enable\nhermes plugins enable memory-wiki' }),
      action(t('retry'), retry),
    ] }),
  }) });
}
function Sessions({ sessions, current, translateNow }) {
  const t = usePluginI18n(ID);
  return jsx(Entries, { items: sessions, empty: t('noSessions'), render: session =>
    jsxs('div', { children: [
      typeof host.openSession === 'function'
        ? action(session.title || session.id, () => {
          if (!current()) return;
          // Read live atoms in the handler; the row belongs to this mounted scope.
          void host.openSession(session.id, { profile: host.state.profile.get() })
            .catch(() => { if (current()) host.notify({ kind: 'error', message: translateNow('openError') }); });
        }) : jsx('span', { children: session.title || session.id }),
      jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children:
        `${protocol(t, 'source', session.source || 'session')} · ${t('time', session.last_active)}` }),
      session.preview && jsx('p', { children: session.preview }),
    ] }) });
}
function Detail({ kind, id, useRead, go, current, translateNow }) {
  const t = usePluginI18n(ID);
  const path = `/${kind}/${encodeURIComponent(id)}`;
  const query = useRead(path);
  const item = kind === 'subjects' ? query.data?.subject : query.data?.daily_log;
  return jsxs('div', { className: 'space-y-5', children: [
    action(t('back'), () => go(null)),
    query.isPending ? jsx('div', { role: 'status', children: [t('loadingDetail'), jsx(Skeleton, { className: 'h-24 w-full' }, 'skeleton')] })
      : query.isError ? jsx(Failure, { error: query.error, retry: query.refetch, detail: true })
      : !item ? jsx(EmptyState, { title: t('notFound') })
      : jsxs('div', { className: 'space-y-5', children: [
        jsx('h2', { className: 'text-xl font-semibold', children: item.name || item.date }),
        jsx('p', { children: t('counts', item.session_count, item.message_count) }),
        kind === 'subjects'
          ? jsxs(Section, { title: t('snippets'), children: [
            jsx('p', { children: (item.keywords || []).join(' · ') }),
            jsx(Entries, { items: item.snippets, empty: t('noSnippets'), render: s =>
              jsx('blockquote', { children: `${protocol(t, 'role', s.role || 'message')} · ${t('time', s.timestamp)}: ${s.text}` }) }),
          ] })
          : jsxs(Section, { title: t('activity'), children: [
            jsx('div', { className: 'flex flex-wrap gap-2', children: item.subjects.map(s =>
              action(s.name, () => go({ kind: 'subjects', id: s.slug }), { key: s.slug })) }),
            jsx(Entries, { items: item.work_items, empty: t('noActivity'), render: w => `${protocol(t, 'kind', w.kind)}: ${activityText(t, w)}` }),
          ] }),
        jsx(Section, { title: t('related'), children: jsx(Sessions, { sessions: item.sessions, current, translateNow }) }),
      ] }),
  ] });
}
function Page({ useRead, current, translateNow }) {
  const t = usePluginI18n(ID);
  // Locale is presentation only: never part of tab identity, route keys or query keys.
  const [tab, setTab] = useState('subjects');
  const [search, setSearch] = useState('');
  const [detail, setDetail] = useState(null);
  const overview = useRead('/overview', !detail && (tab === 'sessions' || tab === 'memory'));
  const subjects = useRead(`/subjects?q=${encodeURIComponent(search.trim())}&limit=100`, !detail && tab === 'subjects');
  const days = useRead('/days?limit=60', !detail && tab === 'days');
  const query = tab === 'subjects' ? subjects : tab === 'days' ? days : overview;
  let body;
  if (detail) body = jsx(Detail, { ...detail, useRead, go: setDetail, current, translateNow }, `${detail.kind}/${detail.id}`);
  else if (query.isPending) body = jsx('div', { role: 'status', children: [
    t('loading'), jsx(Skeleton, { className: 'h-24 w-full' }, 'skeleton'),
  ] });
  else if (query.isError) body = jsx(Failure, { error: query.error, retry: query.refetch });
  else if (tab === 'subjects') body = jsx(Entries, { items: query.data.subjects,
    empty: t(search.trim() ? 'noMatches' : 'noSubjects'),
    render: s => jsxs('div', { children: [action(s.name, () => setDetail({ kind: 'subjects', id: s.slug })),
      jsx('p', { children: `${t('counts', s.session_count, s.message_count)} · ${t('time', s.last_seen)}` })] }) });
  else if (tab === 'days') body = jsx(Entries, { items: query.data.daily_logs, empty: t('noDays'),
    render: d => action(`${d.date} · ${t('sessionCount', d.session_count)}`, () => setDetail({ kind: 'days', id: d.date })) });
  else if (tab === 'sessions') body = jsx(Sessions, { sessions: query.data.recent_sessions, current, translateNow });
  else body = jsxs('div', { className: 'space-y-5', children: [
    jsx('p', { children: t('notesHelp') }),
    ...[['agentNotes', 'noAgentNotes', 'memory'], ['userProfile', 'noUserProfile', 'user']].map(([title, empty, key]) =>
      jsx(Section, { title: t(title), children: jsx(Entries, { items: query.data.notes?.[key], empty: t(empty), render: entry => entry }) }, key)),
  ] });
  return jsxs('main', { className: 'h-full overflow-auto p-5 space-y-5 text-sm text-(--ui-text-primary)', children: [
    jsx('h1', { className: 'text-xl font-semibold', children: t('title') }),
    jsx('p', { className: 'text-(--ui-text-secondary)', children: t('description') }),
    jsx('nav', { 'aria-label': t('sections'), className: 'flex flex-wrap gap-2', children:
      ['subjects', 'days', 'sessions', 'memory'].map(id =>
        action(t(`tabs.${id}`), () => { setTab(id); setDetail(null); }, { key: id, variant: tab === id ? 'secondary' : 'outline', 'aria-pressed': tab === id })) }),
    !detail && tab === 'subjects' && jsx(SearchField, { placeholder: t('search'), 'aria-label': t('search'), value: search, onChange: setSearch }),
    body,
  ] });
}
export default {
  id: ID, name: 'Memory Wiki', defaultEnabled: false,
  register(ctx) {
    ctx.i18n.register(locales);
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
      return jsx(Page, { useRead, current, translateNow: ctx.i18n.t }, scope);
    }
    ctx.onDispose(() => {
      disposed = true;
      void queryClient.cancelQueries({ queryKey: cache });
      queryClient.removeQueries({ queryKey: cache });
    });
    // Keep the route/render identity alive when static contribution labels change.
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: '/memory-wiki' }, render: () => jsx(ScopedPage, {}) },
    ]);
    let removeLabels;
    const registerLabels = () => {
      removeLabels?.();
      const t = ctx.i18n.t;
      removeLabels = ctx.registerMany([
        { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: '/memory-wiki', label: t('title'), codicon: 'book' } },
        { id: 'open', area: PALETTE_AREA, data: { id: 'memory-wiki.open', label: t('open'),
          keywords: ['memory', 'subjects', 'daily logs', t('keyword.memory'), t('keyword.subjects'), t('keyword.days')],
          run: () => host.navigate('/memory-wiki') } },
      ]);
    };
    registerLabels();
    ctx.i18n.onLocaleChange(registerLabels);
  },
};
