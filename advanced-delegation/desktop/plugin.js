import {
  PALETTE_AREA,
  ROUTES_AREA,
  SETTINGS_TABS_AREA,
  SIDEBAR_NAV_AREA,
  host,
  queryClient,
  useMutation,
  useQuery,
  useValue
} from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'advanced-delegation'
let pluginCtx = null

const ALLOWED_PATHS = new Set(['/state', '/mode', '/decider', '/probe', '/decisions'])

export function assertBackendPath(path) {
  const base = String(path || '').split('?')[0]
  if (!ALLOWED_PATHS.has(base)) {
    throw new Error(`Unsupported backend path: ${base}`)
  }
  return path
}

export async function request(path, options = {}) {
  assertBackendPath(path)
  if (!pluginCtx) {
    throw new Error('Plugin context is not initialized')
  }
  const res = await pluginCtx.rest(path, { timeoutMs: 25000, ...options })
  if (res && res.error) {
    throw new Error(res.error.message || String(res.error))
  }
  return res
}

const cardStyle = {
  background: 'var(--card)',
  border: '1px solid var(--border)',
  borderRadius: '10px',
  padding: '16px',
  marginBottom: '16px'
}

const inputStyle = {
  width: '100%',
  padding: '8px 10px',
  borderRadius: '6px',
  border: '1px solid var(--border)',
  background: 'transparent',
  color: 'var(--foreground)',
  fontSize: '13px'
}

const btnPrimary = {
  padding: '8px 14px',
  borderRadius: '6px',
  border: 'none',
  background: 'var(--accent)',
  color: '#fff',
  fontWeight: 600,
  fontSize: '13px',
  cursor: 'pointer'
}

const btnSecondary = {
  padding: '6px 12px',
  borderRadius: '6px',
  border: '1px solid var(--border)',
  background: 'transparent',
  color: 'var(--foreground)',
  fontSize: '12px',
  cursor: 'pointer'
}

function Badge({ text, tone = 'neutral' }) {
  const bg =
    tone === 'success'
      ? 'rgba(34, 197, 94, 0.16)'
      : tone === 'warn'
        ? 'rgba(234, 179, 8, 0.16)'
        : 'rgba(148, 163, 184, 0.16)'
  const fg =
    tone === 'success'
      ? '#4ade80'
      : tone === 'warn'
        ? '#facc15'
        : 'var(--muted-foreground)'
  return jsx('span', {
    style: {
      display: 'inline-block',
      padding: '2px 8px',
      borderRadius: '999px',
      fontSize: '11px',
      fontWeight: 600,
      background: bg,
      color: fg
    },
    children: text
  })
}

export function DelegationWorkbench() {
  const profile = useValue(host.profile) || 'default'
  const stateQuery = useQuery({
    queryKey: [ID, profile, 'state'],
    queryFn: () => request('/state?limit=30')
  })

  const data = stateQuery.data || {}
  const dec = data.decider || {}
  const routes = Array.isArray(data.routes) ? data.routes : []
  const decisions = Array.isArray(data.decisions) ? data.decisions : []
  const modes = Array.isArray(data.modes) ? data.modes : []

  const [form, setForm] = useState(null)
  const [probeGoal, setProbeGoal] = useState(
    'Analyze screenshot.png and extract the rendered UI error dialog'
  )
  const [probeImages, setProbeImages] = useState(true)
  const [probeResult, setProbeResult] = useState(null)
  const [notice, setNotice] = useState('')

  useEffect(() => {
    if (data.decider) {
      setForm({
        enabled: Boolean(data.decider.enabled),
        url: data.decider.url || 'http://localhost:11434/v1/systemone',
        model: data.decider.model || 'pplx-decider:27b-nvfp4',
        allow_fallback: Boolean(data.decider.allow_fallback ?? true),
        fallback_url: data.decider.fallback_url || 'https://api.typesafe.ai/v1/systemone',
        fallback_model: data.decider.fallback_model || 'jev-latest',
        specialist_threshold: Number(data.decider.specialist_threshold ?? 0.7),
        confidence_threshold: Number(data.decider.confidence_threshold ?? 0.6),
        timeout: Number(data.decider.timeout ?? 18.0),
        excluded_options: Array.isArray(data.decider.excluded_options)
          ? [...data.decider.excluded_options]
          : []
      })
    }
  }, [
    data.decider?.enabled,
    data.decider?.url,
    data.decider?.model,
    data.decider?.allow_fallback,
    data.decider?.fallback_url,
    data.decider?.fallback_model,
    data.decider?.specialist_threshold,
    data.decider?.confidence_threshold,
    data.decider?.timeout
  ])

  const modeMutation = useMutation({
    mutationFn: (mode) =>
      request('/mode', {
        method: 'POST',
        body: JSON.stringify({ mode })
      }),
    onSuccess: (next) => {
      queryClient.setQueryData([ID, profile, 'state'], next)
      setNotice(`Saved delegation mode: ${next.mode_label}. New sessions pick this up.`)
    }
  })

  const deciderMutation = useMutation({
    mutationFn: (payload) =>
      request('/decider', {
        method: 'PUT',
        body: JSON.stringify(payload)
      }),
    onSuccess: (next) => {
      queryClient.setQueryData([ID, profile, 'state'], next)
      setNotice('Saved System-1 Decider settings.')
    }
  })

  const probeMutation = useMutation({
    mutationFn: (payload) =>
      request('/probe', {
        method: 'POST',
        body: JSON.stringify(payload)
      }),
    onSuccess: (res) => {
      setProbeResult(res)
    }
  })

  const clearHistoryMutation = useMutation({
    mutationFn: () => request('/decisions', { method: 'DELETE' }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: [ID, profile, 'state'] })
      setNotice('Cleared Decider routing history.')
    }
  })

  if (stateQuery.isLoading || !form) {
    return jsx('div', {
      style: { padding: '24px', color: 'var(--muted-foreground)' },
      children: 'Loading Advanced Delegation & System-1 Decider...'
    })
  }

  const toggleExcludedRoute = (routeName) => {
    const current = new Set(form.excluded_options || [])
    if (current.has(routeName)) {
      current.delete(routeName)
    } else {
      current.add(routeName)
    }
    const nextForm = { ...form, excluded_options: Array.from(current) }
    setForm(nextForm)
    deciderMutation.mutate(nextForm)
  }

  return jsxs('div', {
    style: { padding: '20px', maxWidth: '920px', color: 'var(--foreground)' },
    children: [
      jsxs('div', {
        style: {
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: '16px'
        },
        children: [
          jsxs('div', {
            children: [
              jsx('h2', {
                style: { margin: 0, fontSize: '20px', fontWeight: 700 },
                children: 'Advanced Delegation & System-1 Decider'
              }),
              jsx('p', {
                style: { margin: '4px 0 0', fontSize: '13px', color: 'var(--muted-foreground)' },
                children:
                  'Configure subagent delegation modes, Model routes, and automatic System-1 Decider (/v1/systemone) routing.'
              })
            ]
          }),
          jsx(Badge, {
            text: form.enabled ? 'Decider Auto-Router: ON' : 'Decider Auto-Router: OFF',
            tone: form.enabled ? 'success' : 'neutral'
          })
        ]
      }),

      notice
        ? jsx('div', {
            style: {
              padding: '10px 14px',
              borderRadius: '8px',
              marginBottom: '16px',
              background: 'rgba(59, 130, 246, 0.14)',
              border: '1px solid var(--border)',
              fontSize: '13px'
            },
            children: notice
          })
        : null,

      jsxs('div', {
        style: cardStyle,
        children: [
          jsx('h3', {
            style: { margin: '0 0 10px', fontSize: '15px' },
            children: '1. Delegation Mode'
          }),
          jsx('div', {
            style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: '10px' },
            children: modes.map((m) => {
              const active = data.mode === m.id
              return jsxs(
                'button',
                {
                  type: 'button',
                  onClick: () => modeMutation.mutate(m.id),
                  style: {
                    textAlign: 'left',
                    padding: '12px',
                    borderRadius: '8px',
                    border: active ? '2px solid var(--accent)' : '1px solid var(--border)',
                    background: 'transparent',
                    color: 'var(--foreground)',
                    cursor: 'pointer'
                  },
                  children: [
                    jsxs('div', {
                      style: { display: 'flex', justifyContent: 'space-between', fontWeight: 600, fontSize: '13px' },
                      children: [
                        jsx('span', { children: m.label }),
                        active ? jsx(Badge, { text: 'Active', tone: 'success' }) : null
                      ]
                    }),
                    jsx('div', {
                      style: { marginTop: '6px', fontSize: '12px', color: 'var(--muted-foreground)' },
                      children: m.blurb
                    })
                  ]
                },
                m.id
              )
            })
          })
        ]
      }),

      jsxs('div', {
        style: cardStyle,
        children: [
          jsxs('div', {
            style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' },
            children: [
              jsx('h3', {
                style: { margin: 0, fontSize: '15px' },
                children: '2. System-1 Decider Auto-Router (/v1/systemone)'
              }),
              jsxs('label', {
                style: { display: 'flex', alignItems: 'center', gap: '8px', fontSize: '13px', cursor: 'pointer' },
                children: [
                  jsx('input', {
                    type: 'checkbox',
                    checked: form.enabled,
                    onChange: (e) => {
                      const next = { ...form, enabled: e.target.checked }
                      setForm(next)
                      deciderMutation.mutate(next)
                    }
                  }),
                  jsx('span', {
                    style: { fontWeight: 600 },
                    children: 'Enable automatic subagent routing when option is omitted'
                  })
                ]
              })
            ]
          }),

          jsxs('div', {
            style: { display: 'grid', gridTemplateColumns: '2fr 1fr', gap: '12px', marginBottom: '12px' },
            children: [
              jsxs('div', {
                children: [
                  jsx('label', {
                    style: { display: 'block', fontSize: '12px', color: 'var(--muted-foreground)', marginBottom: '4px' },
                    children: 'Primary Endpoint URL (Ollama / LAN / Local)'
                  }),
                  jsx('input', {
                    style: inputStyle,
                    value: form.url,
                    onChange: (e) => setForm({ ...form, url: e.target.value })
                  })
                ]
              }),
              jsxs('div', {
                children: [
                  jsx('label', {
                    style: { display: 'block', fontSize: '12px', color: 'var(--muted-foreground)', marginBottom: '4px' },
                    children: 'Primary Decision Model'
                  }),
                  jsx('input', {
                    style: inputStyle,
                    value: form.model,
                    onChange: (e) => setForm({ ...form, model: e.target.value })
                  })
                ]
              })
            ]
          }),

          jsxs('div', {
            style: { display: 'grid', gridTemplateColumns: '2fr 1fr', gap: '12px', marginBottom: '12px' },
            children: [
              jsxs('div', {
                children: [
                  jsx('label', {
                    style: { display: 'block', fontSize: '12px', color: 'var(--muted-foreground)', marginBottom: '4px' },
                    children: 'Cloud Fallback URL (TypeSafe ZDR)'
                  }),
                  jsx('input', {
                    style: inputStyle,
                    value: form.fallback_url,
                    onChange: (e) => setForm({ ...form, fallback_url: e.target.value })
                  })
                ]
              }),
              jsxs('div', {
                children: [
                  jsx('label', {
                    style: { display: 'block', fontSize: '12px', color: 'var(--muted-foreground)', marginBottom: '4px' },
                    children: 'Fallback Model'
                  }),
                  jsx('input', {
                    style: inputStyle,
                    value: form.fallback_model,
                    onChange: (e) => setForm({ ...form, fallback_model: e.target.value })
                  })
                ]
              })
            ]
          }),

          jsxs('div', {
            style: { display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '12px', marginBottom: '14px' },
            children: [
              jsxs('div', {
                children: [
                  jsx('label', {
                    style: { display: 'block', fontSize: '12px', color: 'var(--muted-foreground)', marginBottom: '4px' },
                    children: `Specialist Threshold (${Number(form.specialist_threshold).toFixed(2)})`
                  }),
                  jsx('input', {
                    type: 'number',
                    step: '0.05',
                    min: '0',
                    max: '1',
                    style: inputStyle,
                    value: form.specialist_threshold,
                    onChange: (e) => setForm({ ...form, specialist_threshold: parseFloat(e.target.value || '0.7') })
                  })
                ]
              }),
              jsxs('div', {
                children: [
                  jsx('label', {
                    style: { display: 'block', fontSize: '12px', color: 'var(--muted-foreground)', marginBottom: '4px' },
                    children: `Confidence Threshold (${Number(form.confidence_threshold).toFixed(2)})`
                  }),
                  jsx('input', {
                    type: 'number',
                    step: '0.05',
                    min: '0',
                    max: '1',
                    style: inputStyle,
                    value: form.confidence_threshold,
                    onChange: (e) => setForm({ ...form, confidence_threshold: parseFloat(e.target.value || '0.6') })
                  })
                ]
              }),
              jsxs('div', {
                children: [
                  jsx('label', {
                    style: { display: 'block', fontSize: '12px', color: 'var(--muted-foreground)', marginBottom: '4px' },
                    children: 'Timeout (seconds)'
                  }),
                  jsx('input', {
                    type: 'number',
                    step: '1',
                    min: '1',
                    max: '120',
                    style: inputStyle,
                    value: form.timeout,
                    onChange: (e) => setForm({ ...form, timeout: parseFloat(e.target.value || '18') })
                  })
                ]
              })
            ]
          }),

          jsxs('div', {
            style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center' },
            children: [
              jsxs('label', {
                style: { display: 'flex', alignItems: 'center', gap: '8px', fontSize: '12px', cursor: 'pointer' },
                children: [
                  jsx('input', {
                    type: 'checkbox',
                    checked: form.allow_fallback,
                    onChange: (e) => setForm({ ...form, allow_fallback: e.target.checked })
                  }),
                  jsx('span', {
                    children: 'Allow automatic failover to TypeSafe Cloud when LAN Decider is busy or unreachable'
                  })
                ]
              }),
              jsx('button', {
                type: 'button',
                style: btnPrimary,
                disabled: deciderMutation.isPending,
                onClick: () => deciderMutation.mutate(form),
                children: deciderMutation.isPending ? 'Saving...' : 'Save Decider Settings'
              })
            ]
          })
        ]
      }),

      jsxs('div', {
        style: cardStyle,
        children: [
          jsx('h3', {
            style: { margin: '0 0 8px', fontSize: '15px' },
            children: `3. Configured Model Routes (${routes.length}) & Auto-Routing Eligibility`
          }),
          jsx('p', {
            style: { margin: '0 0 12px', fontSize: '12px', color: 'var(--muted-foreground)' },
            children:
              'Check which routes the System-1 Decider is allowed to select automatically when delegate_task omits option. Paid or advisor-only routes should stay unchecked so they are only used when explicitly requested.'
          }),
          routes.length === 0
            ? jsx('div', {
                style: { fontSize: '13px', color: 'var(--muted-foreground)' },
                children: 'No model routes configured yet. Add routes in Settings → Model → Subagents or run `hermes delegation config`.'
              })
            : jsx('div', {
                style: { display: 'flex', flexDirection: 'column', gap: '8px' },
                children: routes.map((r) => {
                  const isExcluded = (form.excluded_options || []).includes(r.name)
                  const isDefault = data.route_default === r.name
                  return jsxs(
                    'div',
                    {
                      style: {
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'center',
                        padding: '10px 12px',
                        borderRadius: '8px',
                        border: '1px solid var(--border)'
                      },
                      children: [
                        jsxs('div', {
                          style: { flex: 1, paddingRight: '12px' },
                          children: [
                            jsxs('div', {
                              style: { display: 'flex', alignItems: 'center', gap: '8px', fontWeight: 600, fontSize: '13px' },
                              children: [
                                jsx('span', { children: r.name }),
                                isDefault ? jsx(Badge, { text: 'default', tone: 'success' }) : null,
                                jsx('span', {
                                  style: { fontWeight: 400, fontSize: '12px', color: 'var(--muted-foreground)' },
                                  children: r.summary
                                })
                              ]
                            }),
                            jsx('div', {
                              style: { marginTop: '4px', fontSize: '12px', color: 'var(--muted-foreground)' },
                              children: r.description
                            })
                          ]
                        }),
                        jsxs('label', {
                          style: { display: 'flex', alignItems: 'center', gap: '6px', fontSize: '12px', cursor: 'pointer', whiteSpace: 'nowrap' },
                          children: [
                            jsx('input', {
                              type: 'checkbox',
                              checked: !isExcluded,
                              onChange: () => toggleExcludedRoute(r.name)
                            }),
                            jsx('span', { children: !isExcluded ? 'Auto-eligible' : 'Explicit only' })
                          ]
                        })
                      ]
                    },
                    r.name
                  )
                })
              })
        ]
      }),

      jsxs('div', {
        style: cardStyle,
        children: [
          jsx('h3', {
            style: { margin: '0 0 10px', fontSize: '15px' },
            children: '4. Live Routing Probe (Test a Subagent Goal)'
          }),
          jsxs('div', {
            style: { display: 'flex', gap: '10px', marginBottom: '10px' },
            children: [
              jsx('input', {
                style: { ...inputStyle, flex: 1 },
                placeholder: 'Enter a sample subagent goal to test...',
                value: probeGoal,
                onChange: (e) => setProbeGoal(e.target.value)
              }),
              jsx('button', {
                type: 'button',
                style: btnPrimary,
                disabled: probeMutation.isPending,
                onClick: () =>
                  probeMutation.mutate({
                    goal: probeGoal,
                    has_images: probeImages
                  }),
                children: probeMutation.isPending ? 'Probing...' : 'Test Route'
              })
            ]
          }),
          jsxs('label', {
            style: { display: 'flex', alignItems: 'center', gap: '8px', fontSize: '12px', cursor: 'pointer' },
            children: [
              jsx('input', {
                type: 'checkbox',
                checked: probeImages,
                onChange: (e) => setProbeImages(e.target.checked)
              }),
              jsx('span', { children: 'Simulate image/screenshot attachments on the task (has_images: true)' })
            ]
          }),
          probeResult
            ? jsx('div', {
                style: {
                  marginTop: '12px',
                  padding: '12px',
                  borderRadius: '8px',
                  border: '1px solid var(--border)',
                  fontSize: '13px'
                },
                children: probeResult.ok
                  ? jsxs('div', {
                      children: [
                        jsxs('div', {
                          style: { display: 'flex', alignItems: 'center', gap: '8px', fontWeight: 600 },
                          children: [
                            jsx('span', { children: `Effective Route: ${probeResult.effective_route}` }),
                            jsx(Badge, {
                              text: probeResult.upgraded ? 'UPGRADED TO SPECIALIST' : 'KEPT ON DEFAULT',
                              tone: probeResult.upgraded ? 'success' : 'neutral'
                            }),
                            jsx('span', {
                              style: { fontWeight: 400, fontSize: '12px', color: 'var(--muted-foreground)' },
                              children: `${Math.round(probeResult.latency_ms || 0)} ms on ${probeResult.model}${probeResult.fallback_used ? ' (cloud fallback)' : ''}`
                            })
                          ]
                        }),
                        jsx('div', {
                          style: { marginTop: '6px', fontSize: '12px', color: 'var(--muted-foreground)' },
                          children: `Candidate: ${probeResult.candidate_route} · needs_specialist: ${Number(probeResult.needs_specialist).toFixed(2)} · confidence: ${Number(probeResult.confidence).toFixed(2)}`
                        })
                      ]
                    })
                  : jsx('div', {
                      style: { color: '#f87171' },
                      children: `Probe failed: ${probeResult.error}`
                    })
              })
            : null
        ]
      }),

      jsxs('div', {
        style: cardStyle,
        children: [
          jsxs('div', {
            style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '10px' },
            children: [
              jsx('h3', {
                style: { margin: 0, fontSize: '15px' },
                children: `5. Recent Routing Decisions (${decisions.length})`
              }),
              decisions.length > 0
                ? jsx('button', {
                    type: 'button',
                    style: btnSecondary,
                    onClick: () => clearHistoryMutation.mutate(),
                    children: 'Clear History'
                  })
                : null
            ]
          }),
          decisions.length === 0
            ? jsx('div', {
                style: { fontSize: '13px', color: 'var(--muted-foreground)' },
                children: 'No automatic routing decisions logged yet.'
              })
            : jsx('div', {
                style: { display: 'flex', flexDirection: 'column', gap: '6px' },
                children: decisions.map((d, i) =>
                  jsxs(
                    'div',
                    {
                      style: {
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'center',
                        padding: '8px 10px',
                        borderRadius: '6px',
                        border: '1px solid var(--border)',
                        fontSize: '12px'
                      },
                      children: [
                        jsxs('div', {
                          style: { flex: 1, paddingRight: '12px' },
                          children: [
                            jsxs('div', {
                              style: { display: 'flex', alignItems: 'center', gap: '8px', fontWeight: 600 },
                              children: [
                                jsx(Badge, {
                                  text: d.route,
                                  tone: d.action === 'upgraded' ? 'success' : 'neutral'
                                }),
                                jsx('span', {
                                  style: { color: 'var(--muted-foreground)', fontWeight: 400 },
                                  children: `spec=${Number(d.needs_specialist || 0).toFixed(2)} · conf=${Number(d.confidence || 0).toFixed(2)} · ${Math.round(d.latency_ms || 0)}ms${d.fallback_used ? ' (fallback)' : ''}`
                                })
                              ]
                            }),
                            jsx('div', {
                              style: { marginTop: '4px', color: 'var(--foreground)' },
                              children: d.goal
                            })
                          ]
                        })
                      ]
                    },
                    `${d.timestamp || i}-${i}`
                  )
                )
              })
        ]
      })
    ]
  })
}

export default {
  id: ID,
  name: 'Advanced Delegation & System-1 Decider',
  defaultEnabled: true,
  register(ctx) {
    pluginCtx = ctx
    ctx.registerMany([
      {
        id: 'settings-tab',
        area: SETTINGS_TABS_AREA,
        data: {
          id: 'delegation-decider',
          label: 'System-1 Decider',
          group: 'Model',
          order: 25
        },
        render: () => jsx(DelegationWorkbench, {})
      },
      {
        id: 'route',
        area: ROUTES_AREA,
        data: { path: '/delegation' },
        render: () => jsx(DelegationWorkbench, {})
      },
      {
        id: 'sidebar',
        area: SIDEBAR_NAV_AREA,
        data: { path: '/delegation', label: 'Delegation', codicon: 'type-hierarchy-sub' }
      },
      {
        id: 'palette',
        area: PALETTE_AREA,
        data: {
          id: 'advanced-delegation.open',
          label: 'Open Advanced Delegation & System-1 Decider',
          keywords: ['delegation', 'subagents', 'decider', 'systemone', 'routes', 'model_map'],
          run: () => host.navigate('/delegation')
        }
      }
    ])
    ctx.onEvent('plugin.advanced-delegation.updated', () =>
      queryClient.invalidateQueries({ queryKey: [ID] })
    )
    ctx.onEvent('gateway.ready', () => queryClient.removeQueries({ queryKey: [ID] }))
    ctx.onDispose(() => {
      queryClient.removeQueries({ queryKey: [ID] })
      pluginCtx = null
    })
  }
}
