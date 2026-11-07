// Hermes desktop UI for va-claims: a full-page benefits tracker.
// Plain ESM, loaded uncompiled: no JSX, only @hermes/plugin-sdk + react imports.
// Backed by the plugin's Python API at /api/plugins/va-claims/ via ctx.rest.

import { host, Button, Input, Badge, StatusDot, EmptyState, Codicon } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx as rawJsx, jsxs as rawJsxs } from 'react/jsx-runtime'

// React wants `key` as the third argument of jsx(), not inside props.
const keyed = fn => (type, props) => {
  if (props && 'key' in props) {
    const { key, ...rest } = props
    return fn(type, rest, key)
  }
  return fn(type, props)
}
const jsx = keyed(rawJsx)
const jsxs = keyed(rawJsxs)

const ID = 'va-claims'
const ROUTE = '/va-benefits'

let CTX = null

const PROGRAMS = [
  { id: 'disability', label: 'Disability' },
  { id: 'education', label: 'GI Bill' },
  { id: 'health', label: 'VA Health' },
  { id: 'champva', label: 'CHAMPVA' },
  { id: 'life_insurance', label: 'SGLI/VGLI' },
  { id: 'pension', label: 'Pension' },
  { id: 'voc_rehab', label: 'VR&E' },
]

const PHASES = [
  'Claim received',
  'Initial review',
  'Evidence gathering, review, and decision',
  'Preparation for notification',
  'Complete',
]

const GUIDE_TOPICS = ['file', 'evidence', 'status', 'ratings', 'representatives', 'timeline', 'appeals', 'programs']

function errText(e) {
  const raw = e instanceof Error ? e.message : String(e ?? '')
  return raw.replace(/^\d{3}:\s*/, '')
}

async function api(path, opts) {
  if (!CTX) throw new Error('plugin backend not ready')
  return CTX.rest(path, opts)
}

// ------------------------------------------------------------------ summary

function useSummary(icn) {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const reload = async () => {
    try {
      const r = await api(`/summary?icn=${encodeURIComponent(icn || '')}`)
      setData(r)
      setError('')
    } catch (e) {
      setError(errText(e))
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { reload() }, [icn])
  return { data, error, loading, reload }
}

// ------------------------------------------------------------------ widgets

function PhaseTrack({ status }) {
  const idx = PHASES.findIndex(p => (status || '').toUpperCase().replace(/[_ ]/g, ' ').includes(p.toLowerCase().split(',')[0]))
  return jsx('div', {
    className: 'flex items-center gap-1',
    children: PHASES.map((p, i) =>
      jsx('div', {
        title: p,
        className: `h-1.5 flex-1 rounded-full ${i <= idx ? 'bg-(--ui-accent)' : 'bg-(--ui-stroke-secondary)'}`,
      }, p)
    ),
  })
}

function ClaimCard({ claim }) {
  const done = (claim.status || '').toUpperCase() === 'COMPLETE'
  return jsxs('div', {
    className: 'flex flex-col gap-2 rounded-md border border-(--ui-stroke-secondary) p-3',
    children: [
      jsxs('div', { className: 'flex items-center justify-between gap-2', children: [
        jsxs('div', { className: 'flex items-center gap-2', children: [
          jsx(StatusDot, { status: done ? 'idle' : 'active' }),
          jsx('span', { className: 'font-medium', children: claim.claim_type || 'claim' }),
          jsx('span', { className: 'text-xs text-(--ui-text-quaternary)', children: `#${claim.claim_id}` }),
        ] }),
        jsx(Badge, { variant: done ? 'secondary' : 'default', children: claim.status || 'unknown' }),
      ] }),
      jsx(PhaseTrack, { status: claim.status }),
      claim.documents_needed ? jsx('div', { className: 'text-xs text-(--ui-accent)', children: 'VA needs documents from you' }) : null,
      claim.development_letter_sent ? jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: 'Development letter sent — respond within 1 year' }) : null,
      claim.max_est ? jsx('div', { className: 'text-xs text-(--ui-text-quaternary)', children: `Estimated completion: ${claim.max_est}` }) : null,
      claim.contentions && claim.contentions.length
        ? jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: `Contentions: ${(claim.contentions || []).map(c => c.diagnostic_text || c.name || c).join(', ')}` })
        : null,
      jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: `snapshot ${claim.taken_at || ''}` }),
    ],
  })
}

function ProgramGrid({ benefits }) {
  return jsx('div', {
    className: 'grid grid-cols-2 gap-2',
    children: PROGRAMS.map(p => {
      const b = benefits[p.id]
      return jsxs('div', {
        className: `rounded-md border p-3 ${b ? 'border-(--ui-stroke-secondary)' : 'border-dashed border-(--ui-stroke-secondary) opacity-60'}`,
        children: [
          jsxs('div', { className: 'flex items-center justify-between', children: [
            jsx('span', { className: 'font-medium text-sm', children: p.label }),
            b ? jsx(StatusDot, { status: 'active' }) : null,
          ] }),
          b && b.status ? jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: String(b.status) }) : null,
          b && b.detail ? jsx('div', { className: 'text-xs text-(--ui-text-quaternary)', children: String(b.detail) }) : null,
          b && b.expires_on ? jsx('div', { className: 'text-xs text-(--ui-accent)', children: `expires ${b.expires_on}` }) : null,
          !b ? jsx('div', { className: 'text-xs text-(--ui-text-quaternary)', children: 'not tracked' }) : null,
        ],
      }, p.id)
    }),
  })
}

function Deadlines({ items }) {
  if (!items || !items.length) return jsx('div', { className: 'text-xs text-(--ui-text-quaternary)', children: 'No deadlines recorded. Add one below or ask the agent: "remind me my VGLI window closes".' })
  return jsx('div', {
    className: 'flex flex-col gap-1',
    children: items.map(d => jsxs('div', {
      className: 'flex items-center justify-between rounded border border-(--ui-stroke-secondary) px-2 py-1.5 text-xs',
      children: [
        jsxs('div', { children: [
          jsx('span', { children: d.label }),
          d.note ? jsx('span', { className: 'text-(--ui-text-quaternary)', children: ` — ${d.note}` }) : null,
        ] }),
        jsx(Badge, {
          variant: d.days_left < 0 ? 'destructive' : d.days_left < 60 ? 'default' : 'secondary',
          children: d.days_left < 0 ? `overdue ${-d.days_left}d` : `${d.days_left}d left`,
        }),
      ],
    }, d.label + d.due)),
  })
}

function DeadlineForm({ onDone }) {
  const [label, setLabel] = useState('')
  const [due, setDue] = useState('')
  const [busy, setBusy] = useState(false)
  const add = async () => {
    if (!label || !due) return
    setBusy(true)
    try {
      await api('/deadline', { method: 'POST', body: { icn: '', label, due } })
      setLabel(''); setDue('')
      onDone()
    } catch (e) { host.notify({ kind: 'error', message: errText(e) }) }
    finally { setBusy(false) }
  }
  return jsxs('div', { className: 'flex items-center gap-2', children: [
    jsx(Input, { placeholder: 'Deadline label (e.g. VGLI conversion)', value: label, onValueChange: setLabel, className: 'h-8 flex-1' }),
    jsx(Input, { placeholder: 'YYYY-MM-DD', value: due, onValueChange: setDue, className: 'h-8 w-32' }),
    jsx(Button, { size: 'sm', onClick: add, disabled: busy || !label || !due, children: 'Add' }),
  ] })
}

// ------------------------------------------------------------------ tabs

function OverviewTab({ icn, summary, reload }) {
  if (summary.error) return jsx('div', { className: 'text-sm text-(--ui-accent)', children: summary.error })
  const s = summary.data
  if (!s) return jsx('div', { className: 'text-xs text-(--ui-text-quaternary)', children: 'Loading…' })
  return jsxs('div', { className: 'flex flex-col gap-4', children: [
    jsxs('div', { className: 'flex flex-col gap-2', children: [
      jsx('div', { className: 'text-xs font-medium uppercase text-(--ui-text-tertiary)', children: 'Benefits' }),
      jsx(ProgramGrid, { benefits: s.benefits || {} }),
    ] }),
    jsxs('div', { className: 'flex flex-col gap-2', children: [
      jsx('div', { className: 'text-xs font-medium uppercase text-(--ui-text-tertiary)', children: `Disability claims (${(s.open_claims || []).length} open)` }),
      (s.open_claims || []).length || (s.closed_claims || []).length
        ? jsxs('div', { className: 'flex flex-col gap-2', children: [
            ...(s.open_claims || []).map(c => jsx(ClaimCard, { claim: c }, c.claim_id)),
            ...(s.closed_claims || []).map(c => jsx(ClaimCard, { claim: c }, c.claim_id)),
          ] })
        : jsx('div', { className: 'text-xs text-(--ui-text-quaternary)', children: 'No claim snapshots yet. Ask the agent to run va_claim_track.' }),
    ] }),
    jsxs('div', { className: 'flex flex-col gap-2', children: [
      jsx('div', { className: 'text-xs font-medium uppercase text-(--ui-text-tertiary)', children: 'Deadlines' }),
      jsx(Deadlines, { items: s.deadlines }),
      jsx(DeadlineForm, { onDone: reload }),
    ] }),
    (s.intents || []).length ? jsxs('div', { className: 'flex flex-col gap-1', children: [
      jsx('div', { className: 'text-xs font-medium uppercase text-(--ui-text-tertiary)', children: 'Intents to file' }),
      ...s.intents.map((it, i) => jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: `${it.filed_on}: ${(it.conditions || []).join(', ')} (expires ${it.expires_on})` }, String(i))),
    ] }) : null,
  ] })
}

function GuideTab() {
  const [topic, setTopic] = useState('file')
  const [text, setText] = useState('')
  useEffect(() => {
    api(`/guide?topic=${topic}`).then(r => setText(r.text || r.error || '')).catch(e => setText(errText(e)))
  }, [topic])
  return jsxs('div', { className: 'flex gap-3', children: [
    jsx('div', { className: 'flex w-36 shrink-0 flex-col gap-1', children: GUIDE_TOPICS.map(t =>
      jsx('button', {
        type: 'button',
        onClick: () => setTopic(t),
        className: `rounded px-2 py-1 text-left text-xs capitalize ${t === topic ? 'bg-(--chrome-action-hover) text-foreground' : 'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover)'}`,
        children: t,
      }, t)) }),
    jsx('div', { className: 'flex-1 whitespace-pre-wrap text-xs leading-relaxed text-(--ui-text-secondary)', children: text }),
  ] })
}

// ------------------------------------------------------------------ page

function BenefitsPage() {
  const [icn, setIcn] = useState('')
  const [tab, setTab] = useState('overview')
  const summary = useSummary(icn)
  return jsxs('div', { className: 'flex h-full flex-col gap-3 overflow-auto p-4 text-sm', children: [
    jsxs('div', { className: 'flex items-center gap-2', children: [
      jsx('span', { className: 'font-medium', children: 'VA Benefits' }),
      jsx(Input, { placeholder: 'ICN (blank = default)', value: icn, onValueChange: setIcn, className: 'h-8 w-56' }),
      jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => summary.reload(), children: 'Refresh' }),
    ] }),
    jsxs('div', {
      className: 'flex min-h-0 flex-1 flex-col gap-3',
      children: [
        jsxs('div', { className: 'flex gap-1', children: [
          jsx('button', { type: 'button', onClick: () => setTab('overview'), className: `rounded px-2 py-1 text-xs ${tab === 'overview' ? 'bg-(--chrome-action-hover) text-foreground' : 'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover)'}`, children: 'Overview' }),
          jsx('button', { type: 'button', onClick: () => setTab('guide'), className: `rounded px-2 py-1 text-xs ${tab === 'guide' ? 'bg-(--chrome-action-hover) text-foreground' : 'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover)'}`, children: 'Guide' }),
        ] }),
        tab === 'overview'
          ? jsx(OverviewTab, { icn, summary, reload: summary.reload })
          : jsx(GuideTab, {}),
      ],
    }),
  ] })
}

function Chip() {
  return jsx('button', {
    type: 'button',
    className: 'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem] text-(--ui-text-tertiary) transition-colors hover:bg-(--chrome-action-hover) hover:text-foreground',
    onClick: () => host.navigate(ROUTE),
    children: 'va-benefits',
  })
}

export default {
  id: ID,
  name: 'VA Benefits',
  register(ctx) {
    CTX = ctx
    ctx.register({
      id: 'page',
      area: 'routes',
      data: { path: ROUTE },
      render: () => jsx(BenefitsPage, {}),
    })
    ctx.register({
      id: 'nav',
      area: 'sidebar.nav',
      data: { path: ROUTE, label: 'VA Benefits', codicon: 'shield' },
    })
    ctx.register({ id: 'chip', area: 'statusBar.right', order: 140, render: () => jsx(Chip, {}) })
  },
}
