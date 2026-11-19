// Advanced Delegation, Desktop half: one page under Settings ▸ Plugins for the System-1 Decider
// auto-router (on/off, endpoint, fallback, thresholds), the model routes it may pick, a live test
// and recent decisions. The delegation mode and the routes themselves stay where core edits them:
// Settings ▸ Model ▸ Subagents.
//
// SDK names are read off the namespace, never imported by name: a named import the running host
// does not export fails the whole module at link time. UI kit pieces fall back to plain elements.
import * as sdk from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'advanced-delegation'
const ICON = 'type-hierarchy-sub'
const SUBAGENTS_HREF = '/settings?tab=config:model&page=delegation'
let pluginCtx = null

export const settingsHref = () => sdk.pluginSettingsHref?.(ID) ?? `/settings?tab=plugins&plugin=${ID}`

const S = {
  section: { marginBottom: 24 },
  heading: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    paddingTop: 8,
    marginBottom: 4,
    fontSize: 'var(--conversation-text-font-size)',
    fontWeight: 500
  },
  caption: {
    fontSize: 'var(--conversation-caption-font-size)',
    lineHeight: 'var(--conversation-caption-line-height)',
    color: 'var(--ui-text-tertiary)'
  },
  error: { fontSize: 'var(--conversation-caption-font-size)', color: 'var(--ui-red)' },
  actions: { display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' },
  item: { borderTop: '1px solid var(--ui-stroke-tertiary)', padding: '8px 0' }
}

// Plain stand-ins for UI kit pieces a host may not export.
const Fallback = {
  Badge: ({ children }) =>
    jsx('span', { style: { fontSize: 11, padding: '1px 6px', borderRadius: 3, background: 'var(--ui-bg-tertiary)' }, children }),
  Button: ({ variant, size, ...props }) => jsx('button', { type: 'button', ...props }),
  Input: ({ size, ...props }) => jsx('input', props),
  Switch: ({ checked, onCheckedChange, ...props }) =>
    jsx('input', { ...props, type: 'checkbox', role: 'switch', checked: Boolean(checked), onChange: e => onCheckedChange?.(e.currentTarget.checked) }),
  Checkbox: ({ checked, onCheckedChange, ...props }) =>
    jsx('input', { ...props, type: 'checkbox', checked: Boolean(checked), onChange: e => onCheckedChange?.(e.currentTarget.checked) }),
  Skeleton: () => jsx('div', { style: S.caption, children: 'Loading…' }),
  EmptyState: ({ title, description }) =>
    jsxs('div', { style: { padding: '24px 0' }, children: [
      jsx('div', { style: S.heading, children: title }),
      description ? jsx('div', { style: S.caption, children: description }) : null
    ] }),
  ListRow: ({ title, description, hint, action, below }) =>
    jsxs('div', { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, padding: '12px 0' }, children: [
      jsxs('div', { style: { minWidth: 0, flex: 1 }, children: [
        jsx('div', { style: { fontWeight: 500 }, children: title }),
        description ? jsx('div', { style: S.caption, children: description }) : null,
        hint ? jsx('div', { style: { ...S.caption, fontFamily: 'var(--font-mono, monospace)' }, children: hint }) : null,
        below ?? null
      ] }),
      action ? jsx('div', { style: S.actions, children: action }) : null
    ] }),
  ToggleRow: ({ label, description, hint, checked, disabled, onChange }) =>
    jsx(ListRow, { title: label, description, hint, action: jsx(Switch, { 'aria-label': label, checked, disabled, onCheckedChange: onChange }) })
}
const ui = name => sdk[name] ?? Fallback[name]
const Badge = ui('Badge')
const Button = ui('Button')
const Input = ui('Input')
const Switch = ui('Switch')
const Checkbox = ui('Checkbox')
const Skeleton = ui('Skeleton')
const EmptyState = ui('EmptyState')
const ListRow = ui('ListRow')
const ToggleRow = ui('ToggleRow')

// --- backend -------------------------------------------------------------------------------------

const ALLOWED_PATHS = new Set(['/state', '/decider', '/probe', '/decisions'])

export function assertBackendPath(path) {
  const base = String(path || '').split('?')[0]
  if (!ALLOWED_PATHS.has(base)) {
    throw new Error(`Unsupported backend path: ${base}`)
  }
  return path
}

/** A readable message from a host REST error or a backend `{ "detail": ... }` refusal. */
export function errorText(error) {
  const text = error instanceof Error ? error.message : String(error ?? '')
  const detail = /"detail"\s*:\s*"((?:[^"\\]|\\.)*)"/.exec(text)
  return (detail ? detail[1] : text).slice(0, 300)
}

export async function request(path, options = {}) {
  assertBackendPath(path)
  if (!pluginCtx) {
    throw new Error('Advanced Delegation is not loaded')
  }
  // `body` stays an object: the host JSON-encodes it.
  const res = await pluginCtx.rest(path, { timeoutMs: 25000, ...options })
  if (res && res.error) {
    throw new Error(res.error.message || String(res.error))
  }
  return res
}

export const api = {
  state: () => request('/state?limit=30'),
  saveDecider: settings => request('/decider', { method: 'PUT', body: settings }),
  probe: payload => request('/probe', { method: 'POST', body: payload }),
  clearDecisions: () => request('/decisions', { method: 'DELETE' })
}

// --- form ----------------------------------------------------------------------------------------

export function toForm(decider) {
  const d = decider && typeof decider === 'object' ? decider : {}
  return {
    enabled: Boolean(d.enabled),
    url: d.url ?? '',
    model: d.model ?? '',
    allow_fallback: d.allow_fallback !== false,
    fallback_url: d.fallback_url ?? '',
    fallback_model: d.fallback_model ?? '',
    specialist_threshold: String(d.specialist_threshold ?? 0.7),
    confidence_threshold: String(d.confidence_threshold ?? 0.6),
    timeout: String(d.timeout ?? 18),
    excluded_options: Array.isArray(d.excluded_options) ? [...d.excluded_options] : []
  }
}

export function toPayload(form) {
  const num = (value, fallback) => {
    const n = Number.parseFloat(value)
    return Number.isFinite(n) ? n : fallback
  }
  return {
    ...form,
    url: form.url.trim(),
    model: form.model.trim(),
    fallback_url: form.fallback_url.trim(),
    fallback_model: form.fallback_model.trim(),
    specialist_threshold: num(form.specialist_threshold, 0.7),
    confidence_threshold: num(form.confidence_threshold, 0.6),
    timeout: num(form.timeout, 18)
  }
}

const fixed = value => Number(value || 0).toFixed(2)
const openSubagents = () => sdk.host.navigate(SUBAGENTS_HREF)

function Section({ title, aside, children }) {
  return jsxs('section', { style: S.section, children: [
    jsxs('div', { style: S.heading, children: [jsx('span', { style: { flex: 1 }, children: title }), aside ?? null] }),
    children
  ] })
}

// A settings row sizes its control to the control's own content, which leaves a bare text field too narrow
// for an endpoint URL. Fields get a width (core's row controls are 18rem) and still fill a stacked, narrow row.
const FIELD = { width: '18rem', flexGrow: 1, maxWidth: '100%' }
const NUMBER_FIELD = { width: '5rem' }

function TextRow({ title, description, value, onChange, style = FIELD, ...props }) {
  return jsx(ListRow, {
    title,
    description,
    action: jsx(Input, { 'aria-label': title, value, style, onChange: e => onChange(e.currentTarget.value), ...props })
  })
}

// --- page ----------------------------------------------------------------------------------------

export function DeciderSettings() {
  const { host, queryClient, useMutation, useQuery, useValue } = sdk
  const profile = useValue(host.state.profile) || 'default'
  const key = [ID, profile, 'state']
  const state = useQuery({ queryKey: key, queryFn: api.state })
  const [draft, setDraft] = useState(null)
  const [goal, setGoal] = useState('Analyze screenshot.png and list the UI layout bugs')
  const [images, setImages] = useState(true)
  const [probe, setProbe] = useState(null)
  const [notice, setNotice] = useState(null)

  const failed = error => setNotice({ error: true, text: errorText(error) })
  const save = useMutation({
    mutationFn: api.saveDecider,
    onError: failed,
    onSuccess: next => {
      queryClient.setQueryData(key, next)
      setDraft(null)
      setNotice({ error: false, text: 'Saved. New subagent spawns use it right away.' })
    }
  })
  const runProbe = useMutation({ mutationFn: api.probe, onError: failed, onSuccess: setProbe })
  const clear = useMutation({
    mutationFn: api.clearDecisions,
    onError: failed,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: key })
  })

  if (state.error) {
    return jsxs('div', { role: 'alert', style: { padding: '24px 0' }, children: [
      jsx('div', { style: S.heading, children: 'Advanced Delegation backend is not reachable' }),
      jsx('div', { style: S.error, children: errorText(state.error) }),
      jsx('div', { style: { ...S.caption, paddingTop: 4 }, children: 'Turn the plugin on for this profile in Capabilities ▸ Plugins.' }),
      jsx('div', { style: { ...S.actions, paddingTop: 8 }, children:
        jsx(Button, { size: 'sm', variant: 'outline', disabled: state.isFetching, onClick: () => state.refetch(), children: 'Retry' }) })
    ] })
  }
  if (state.isLoading || !state.data) {
    return jsx(Skeleton, { style: { height: 160 } })
  }

  const data = state.data
  const form = draft ?? toForm(data.decider)
  const edit = patch => setDraft({ ...form, ...patch })
  const commit = patch => save.mutate(toPayload({ ...form, ...patch }))
  const routes = Array.isArray(data.routes) ? data.routes : []
  const decisions = Array.isArray(data.decisions) ? data.decisions : []
  const excluded = new Set(form.excluded_options)
  const eligibleCount = routes.filter(r => !excluded.has(r.name)).length
  const setEligible = (name, on) => {
    const next = new Set(excluded)
    on ? next.delete(name) : next.add(name)
    commit({ excluded_options: [...next] })
  }

  return jsxs('div', { children: [
    jsxs(Section, {
      title: 'System-1 Decider',
      aside: jsx(Badge, { variant: form.enabled ? 'success' : 'muted', children: form.enabled ? 'On' : 'Off' }),
      children: [
        jsx('div', { style: S.caption, children:
          'Picks a model route for subagent tasks that name none, in one /v1/systemone call. A specialist route is used only when both thresholds pass; otherwise the default route.' }),
        notice ? jsx('div', { role: 'status', style: { ...(notice.error ? S.error : S.caption), paddingTop: 6 }, children: notice.text }) : null,
        data.mode !== 'model_map'
          ? jsx(ListRow, {
              title: 'Needs Model routes mode',
              description: `Subagents currently use ${data.mode_label || data.mode}. The Decider only routes in Model routes mode.`,
              action: jsx(Button, { size: 'sm', variant: 'outline', onClick: openSubagents, children: 'Open Model ▸ Subagents' })
            })
          : null,
        jsx(ToggleRow, {
          label: 'Auto-route subagents',
          description: 'For delegate_task calls that leave the route out.',
          checked: form.enabled,
          disabled: save.isPending,
          onChange: on => commit({ enabled: on })
        }),
        jsx(TextRow, { title: 'Endpoint', description: 'Your /v1/systemone server, e.g. Ollama.', value: form.url, onChange: url => edit({ url }) }),
        jsx(TextRow, { title: 'Decision model', value: form.model, onChange: model => edit({ model }) }),
        jsx(ToggleRow, {
          label: 'Cloud fallback',
          description: 'Used when the endpoint is busy or unreachable.',
          checked: form.allow_fallback,
          disabled: save.isPending,
          onChange: on => commit({ allow_fallback: on })
        }),
        form.allow_fallback
          ? jsx(TextRow, { title: 'Fallback endpoint', value: form.fallback_url, onChange: fallback_url => edit({ fallback_url }) })
          : null,
        form.allow_fallback
          ? jsx(TextRow, { title: 'Fallback model', value: form.fallback_model, onChange: fallback_model => edit({ fallback_model }) })
          : null,
        jsx(TextRow, {
          title: 'Specialist threshold',
          description: '0–1: how sure the Decider must be that the task needs a specialist route.',
          inputMode: 'decimal',
          style: NUMBER_FIELD,
          value: form.specialist_threshold,
          onChange: specialist_threshold => edit({ specialist_threshold })
        }),
        jsx(TextRow, {
          title: 'Route confidence',
          description: '0–1: how sure it must be about which specialist route.',
          inputMode: 'decimal',
          style: NUMBER_FIELD,
          value: form.confidence_threshold,
          onChange: confidence_threshold => edit({ confidence_threshold })
        }),
        jsx(TextRow, {
          title: 'Timeout',
          description: 'Seconds before the fallback (or the default route) takes over.',
          inputMode: 'decimal',
          style: NUMBER_FIELD,
          value: form.timeout,
          onChange: timeout => edit({ timeout })
        }),
        draft
          ? jsxs('div', { style: { ...S.actions, paddingTop: 4 }, children: [
              jsx(Button, { size: 'sm', disabled: save.isPending, onClick: () => commit({}), children: save.isPending ? 'Saving…' : 'Save' }),
              jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => setDraft(null), children: 'Discard' })
            ] })
          : null
      ]
    }),

    jsxs(Section, {
      title: `Routes it may pick (${eligibleCount}/${routes.length})`,
      children: [
        jsxs('div', { style: S.caption, children: [
          'Switched-off routes run only when a task names them. Keep paid and advisor routes off. ',
          jsx(Button, { size: 'inline', variant: 'textStrong', onClick: openSubagents, children: 'Edit routes' })
        ] }),
        routes.length
          ? jsx('div', { children: routes.map(r =>
              jsx(ListRow, {
                title: jsxs('span', { style: { display: 'inline-flex', alignItems: 'center', gap: 6 }, children: [
                  r.name,
                  data.route_default === r.name ? jsx(Badge, { variant: 'muted', children: 'default' }) : null
                ] }),
                description: r.description,
                hint: r.summary,
                action: jsx(Switch, {
                  'aria-label': `Let the Decider pick ${r.name}`,
                  checked: !excluded.has(r.name),
                  disabled: save.isPending,
                  onCheckedChange: on => setEligible(r.name, on)
                })
              }, r.name)) })
          : jsx(EmptyState, { title: 'No model routes yet', description: 'Add them in Model ▸ Subagents.' })
      ]
    }),

    jsxs(Section, {
      title: 'Test a goal',
      children: [
        jsx('div', { style: S.caption, children: 'Runs one live routing decision. Nothing is spawned.' }),
        jsxs('div', { style: { ...S.actions, paddingTop: 8 }, children: [
          jsx('div', { style: { flex: 1, minWidth: '16rem' }, children:
            jsx(Input, { 'aria-label': 'Subagent goal', value: goal, onChange: e => setGoal(e.currentTarget.value) }) }),
          jsx(Button, {
            size: 'sm',
            disabled: !goal.trim() || runProbe.isPending,
            onClick: () => runProbe.mutate({ goal: goal.trim(), has_images: images }),
            children: runProbe.isPending ? 'Deciding…' : 'Test'
          })
        ] }),
        jsxs('label', { style: { ...S.actions, ...S.caption, paddingTop: 6 }, children: [
          jsx(Checkbox, { checked: images, onCheckedChange: on => setImages(on === true) }),
          'The task has images attached'
        ] }),
        probe
          ? jsx('div', { style: { ...S.item, marginTop: 8 }, children: probe.ok
              ? jsxs('div', { children: [
                  jsxs('div', { style: S.actions, children: [
                    jsx('b', { children: probe.effective_route }),
                    jsx(Badge, { variant: probe.upgraded ? 'success' : 'muted', children: probe.upgraded ? 'specialist' : 'default route' }),
                    jsx('span', { style: S.caption, children:
                      `${Math.round(probe.latency_ms || 0)} ms · ${probe.model}${probe.fallback_used ? ' (fallback)' : ''}` })
                  ] }),
                  jsx('div', { style: S.caption, children:
                    `candidate ${probe.candidate_route || '—'} · needs specialist ${fixed(probe.needs_specialist)} · confidence ${fixed(probe.confidence)}` })
                ] })
              : jsx('div', { style: S.error, children: `Test failed: ${probe.error}` }) })
          : null
      ]
    }),

    jsx(Section, {
      title: `Recent decisions (${decisions.length})`,
      aside: decisions.length
        ? jsx(Button, { size: 'sm', variant: 'ghost', disabled: clear.isPending, onClick: () => clear.mutate(), children: 'Clear' })
        : null,
      children: decisions.length
        ? jsx('div', { children: decisions.map((d, i) =>
            jsxs('div', { style: S.item, children: [
              jsxs('div', { style: S.actions, children: [
                jsx(Badge, { variant: d.action === 'upgraded' ? 'success' : 'muted', children: d.route }),
                jsx('span', { style: S.caption, children:
                  `specialist ${fixed(d.needs_specialist)} · confidence ${fixed(d.confidence)} · ${Math.round(d.latency_ms || 0)} ms${d.fallback_used ? ' · fallback' : ''}` })
              ] }),
              jsx('div', { style: { ...S.caption, color: 'var(--ui-text-secondary)', paddingTop: 2, overflowWrap: 'anywhere' }, children: d.goal })
            ] }, `${d.timestamp ?? ''}-${i}`)) })
        : jsx('div', { style: S.caption, children: 'No automatic routing decisions yet.' })
    })
  ] })
}

export default {
  id: ID,
  name: 'Advanced Delegation',
  description: 'System-1 Decider auto-router settings for delegate_task.',
  defaultEnabled: true,
  register(ctx) {
    pluginCtx = ctx
    const render = () => jsx(DeciderSettings, {})
    if (ctx.registerSettingsPage) {
      ctx.registerSettingsPage({ id: 'main', title: 'Advanced Delegation', icon: ICON, order: 25, render })
    } else {
      ctx.register({ id: 'main', area: sdk.SETTINGS_PLUGINS_AREA ?? 'settings.plugins', title: 'Advanced Delegation', render, data: { icon: ICON } })
    }
    if (sdk.PALETTE_AREA) {
      ctx.register({
        id: 'open',
        area: sdk.PALETTE_AREA,
        data: {
          id: 'advanced-delegation.open',
          label: 'Advanced Delegation settings',
          keywords: ['delegation', 'subagents', 'decider', 'systemone', 'routes'],
          run: () => sdk.host.navigate(settingsHref())
        }
      })
    }
    ctx.onEvent('plugin.advanced-delegation.updated', () => sdk.queryClient.invalidateQueries({ queryKey: [ID] }))
    ctx.onEvent('gateway.ready', () => sdk.queryClient.removeQueries({ queryKey: [ID] }))
    ctx.onDispose(() => {
      sdk.queryClient.removeQueries({ queryKey: [ID] })
      pluginCtx = null
    })
  }
}
