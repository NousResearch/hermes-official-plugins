/**
 * The status-bar progress chip: "Meshy 42% · refining" while a generation runs instead of the bare "tool running"
 * row. What it shows comes from the tracker in active.ts.
 */

import { Codicon } from '@hermes/plugin-sdk'
import type { CSSProperties } from 'react'

import type { ActiveTask, ProgressState } from './active.ts'

const STAGE_WORDS: Record<string, string> = {
  preview: 'previewing',
  refine: 'texturing',
  'text-to-3d-preview': 'previewing',
  'text-to-3d-refine': 'texturing',
  'image-to-3d': 'modelling',
  'multi-image-to-3d': 'modelling',
  remesh: 'remeshing',
  retexture: 'retexturing',
  rig: 'rigging',
  rigging: 'rigging',
  animation: 'animating',
  'text-to-image': 'drawing'
}

export const stageWord = (t: ActiveTask) => STAGE_WORDS[t.stage] ?? STAGE_WORDS[t.task_type] ?? t.stage.replace(/-/g, ' ')

const S = {
  chip: { display: 'inline-flex', alignItems: 'center', gap: 5, whiteSpace: 'nowrap', flexShrink: 0, border: 0, background: 'transparent', color: 'inherit', font: 'inherit', cursor: 'pointer', padding: '0 4px' },
  track: { display: 'inline-block', flexShrink: 0, width: 42, height: 4, borderRadius: 2, background: 'color-mix(in srgb, currentColor 18%, transparent)', overflow: 'hidden' },
  fill: { display: 'block', height: '100%', borderRadius: 2, background: 'var(--accent, currentColor)', transition: 'width .6s ease' }
} satisfies Record<string, CSSProperties>

/** Busy chip: "Meshy 42% · texturing" with a thin bar; tooltip lists every in-flight task. */
export function ProgressChip({ state, onOpen }: { state: ProgressState; onOpen: () => void }) {
  const top = state.tasks[0]
  const pct = top ? top.progress : state.live?.progress
  const word = top ? stageWord(top) : state.live?.message ? state.live.message.split(' - ')[0].toLowerCase() : 'working'
  const label = top?.status === 'PENDING' && !pct ? 'queued' : pct === undefined ? word : `${pct}% · ${word}`
  const more = state.tasks.length > 1 ? ` +${state.tasks.length - 1}` : ''
  const title = state.tasks.length
    ? state.tasks.map(t => `${stageWord(t)} ${t.progress}% — ${t.prompt || t.task_id}`).join('\n')
    : 'Meshy tool running'
  return (
    <button data-meshy-progress={pct ?? ''} onClick={onOpen} style={S.chip} title={title} type="button">
      <Codicon name="loading" spinning />
      <span>
        Meshy {label}
        {more}
      </span>
      {pct !== undefined && (
        <span style={S.track}>
          <span style={{ ...S.fill, width: `${pct}%` }} />
        </span>
      )}
    </button>
  )
}
