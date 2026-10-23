/**
 * A card given only a Meshy task id (`::meshy-model{task="..."}`), posted right after a create: it shows Meshy's
 * progress while the task runs and the model once it is done, so the agent can reply at once instead of waiting.
 * The finished model is downloaded (free) into the plugin's own folder only when the user opens it.
 */

import { Button, Codicon } from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'

import { stageWord } from './progress.tsx'
import { ActionMenu, errorText, type FilePayload, FormatMenu, ModelViewer, type Os, type Rest, type Source, V } from './views.tsx'

interface TaskInfo {
  task: string
  status: string
  progress: number
  type: string
  stage: string
  prompt: null | string
  thumbnail: null | string
  credits: null | number
  has_model: boolean
  error: null | string
}

const RUNNING = new Set(['PENDING', 'IN_PROGRESS'])
const POLL_MS = 3000

function useTask(rest: Rest, task: string): { info: TaskInfo | null; error: null | string } {
  const [info, setInfo] = useState<TaskInfo | null>(null)
  const [error, setError] = useState<null | string>(null)
  useEffect(() => {
    let live = true
    let timer: ReturnType<typeof setTimeout> | undefined
    const tick = () =>
      rest<TaskInfo>(`task?task=${encodeURIComponent(task)}`, { timeoutMs: 20_000 })
        .then(r => {
          if (!live) {
            return
          }
          setInfo(r)
          setError(null)
          if (RUNNING.has(r.status)) {
            timer = setTimeout(tick, POLL_MS)
          }
        })
        .catch((e: unknown) => {
          if (live) {
            setError(errorText(e))
            timer = setTimeout(tick, POLL_MS * 4) // a just-created task can 404 for a moment
          }
        })
    void tick()
    return () => {
      live = false
      clearTimeout(timer)
    }
  }, [rest, task])
  return { info, error }
}

function useThumbnail(rest: Rest, url: null | string | undefined): null | string {
  const [src, setSrc] = useState<null | string>(null)
  useEffect(() => {
    let live = true
    if (url) {
      rest<FilePayload>(`remote?url=${encodeURIComponent(url)}`, { timeoutMs: 30_000 })
        .then(f => live && setSrc(f.dataUrl))
        .catch(() => undefined)
    }
    return () => {
      live = false
    }
  }, [rest, url])
  return src
}

export function TaskCard({
  rest,
  os,
  task,
  title,
  streaming,
  openInWorkspace
}: {
  rest: Rest
  os: Os
  task: string
  title?: string
  streaming: boolean
  openInWorkspace: (source: Source, title: string) => void
}) {
  const { info, error } = useTask(rest, task)
  const thumb = useThumbnail(rest, info?.thumbnail)
  const [source, setSource] = useState<null | Source>(null)
  // Busy and failure are separate: a failed download shows its reason but leaves View/Open clickable to retry.
  const [opening, setOpening] = useState(false)
  const [openError, setOpenError] = useState<null | string>(null)
  const busy = useRef(false) // state lags a render: two clicks in one task would both pass an `opening` check
  const name = title || info?.prompt || `Meshy task ${task.slice(0, 8)}`
  const done = info?.status === 'SUCCEEDED'

  // Download the GLB (free; reused when already saved) the first time the user wants to see it.
  const open = async (then: (s: Source) => void) => {
    if (source) {
      then(source)
      return
    }
    if (busy.current) {
      return
    }
    busy.current = true
    setOpening(true)
    setOpenError(null)
    try {
      const r = await rest<{ path: string }>('save', { method: 'POST', body: { task, format: 'glb' }, timeoutMs: 180_000 })
      const s: Source = { kind: 'file', path: r.path, task }
      setSource(s)
      then(s)
    } catch (e) {
      setOpenError(`Could not download the model: ${errorText(e)}. Click to try again.`)
    } finally {
      busy.current = false
      setOpening(false)
    }
  }

  let body
  if (!info) {
    body = <div style={V.taskLine}>{error ? `Meshy: ${error}` : 'Checking the Meshy task…'}</div>
  } else if (info.error) {
    body = <div style={{ ...V.taskLine, ...V.error }}>Meshy task {info.status.toLowerCase()}: {info.error}</div>
  } else if (!done) {
    const pct = Math.max(0, Math.min(100, info.progress))
    body = (
      <div data-meshy-task-progress style={V.taskRun}>
        {thumb && <img alt="" src={thumb} style={V.taskThumb} />}
        <div style={{ flex: 1 }}>
          <div style={V.taskLine}>
            Meshy {pct}% · {stageWord({ stage: info.stage, task_type: info.type } as Parameters<typeof stageWord>[0])}
          </div>
          <div style={V.bar}>
            <div style={{ ...V.barFill, width: `${pct}%` }} />
          </div>
        </div>
      </div>
    )
  } else if (source && !opening) {
    body = <ModelViewer autoRotate={false} compact height={320} os={os} rest={rest} source={source} />
  } else if (info.has_model) {
    body = (
      <button
        data-meshy-task-poster
        disabled={streaming || opening}
        onClick={() => void open(s => setSource(s))}
        style={{ ...V.poster, ...(thumb ? { backgroundImage: `url(${thumb})`, ...V.posterImage } : {}) }}
        type="button"
      >
        <Codicon name="play-circle" size={28} />
        <span data-meshy-task-error={openError ? '' : undefined} style={{ ...(thumb ? V.posterLabel : {}), ...(openError ? V.error : {}) }}>
          {opening ? 'Downloading the model…' : streaming ? 'Waiting for the reply to finish…' : (openError ?? 'View in 3D')}
        </span>
      </button>
    )
  } else {
    body = thumb ? <img alt={name} src={thumb} style={V.taskImage} /> : <div style={V.taskLine}>Done. This task has no 3D file.</div>
  }

  return (
    <div data-meshy-card data-meshy-task-card={info?.status ?? 'loading'} style={V.card}>
      <div style={V.cardHead}>
        <Codicon name="symbol-misc" />
        <span style={V.cardTitle}>{name}</span>
        {done && typeof info?.credits === 'number' && <span style={V.itemSub}>{info.credits} cr</span>}
        <span style={V.spacer} />
        {done && !streaming && <ActionMenu rest={rest} task={task} />}
        {done && info?.has_model && !streaming && <FormatMenu os={os} rest={rest} task={task} />}
        {done && info?.has_model && (
          <Button disabled={opening} onClick={() => void open(s => openInWorkspace(s, name))} size="sm" variant="ghost">
            <Codicon name="link-external" /> Open
          </Button>
        )}
      </div>
      {body}
    </div>
  )
}
