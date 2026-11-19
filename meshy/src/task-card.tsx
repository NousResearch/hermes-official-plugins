/**
 * A card given only a Meshy task id (`::meshy-model{task="..."}`), posted right after a create: it shows Meshy's
 * progress while the task runs and the model once it is done, so the agent can reply at once instead of waiting.
 * The finished model is downloaded (free) into the plugin's own folder only when the user opens it.
 */

import { Button, Codicon } from '@hermes/plugin-sdk'
import { useCallback, useEffect, useRef, useState } from 'react'

import type { ChatSender } from './chat.ts'
import { stageWord } from './progress.tsx'
import { openFormat } from './formats.ts'
import { createTaskPoller, type PollState } from './task-poll.ts'
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
  /** Best format the viewer can open (backend 0.5+); a Convert task may carry FBX/STL and no GLB. */
  view_format?: null | string
  /** Any downloadable file, viewable or not (a Convert-to-BLEND task); older backends omit it. */
  has_files?: boolean
  error: null | string
}

/** Polls the backend's `/task` under task-poll.ts's policy: stops on a finished task, on a permanent refusal, and on a
 *  404 once the early window after mount has passed; `retry` is the card's manual Retry. */
function useTask(rest: Rest, task: string): PollState<TaskInfo> & { retry: () => void } {
  const [state, setState] = useState<PollState<TaskInfo>>({ info: null, error: null, stopped: false })
  const poller = useRef<null | { retry: () => void }>(null)
  useEffect(() => {
    setState({ info: null, error: null, stopped: false })
    const p = createTaskPoller<TaskInfo>(
      () => rest<TaskInfo>(`task?task=${encodeURIComponent(task)}`, { timeoutMs: 20_000 }),
      setState,
      errorText
    )
    poller.current = p
    return () => {
      p.dispose()
      if (poller.current === p) poller.current = null
    }
  }, [rest, task])
  const retry = useCallback(() => poller.current?.retry(), [])
  return { ...state, retry }
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
  chat,
  openInWorkspace
}: {
  rest: Rest
  os: Os
  task: string
  title?: string
  streaming: boolean
  /** Sends Next/Convert requests to the chat this card is in (chat.ts::cardChat). */
  chat: ChatSender
  openInWorkspace: (source: Source, title: string) => void
}) {
  const { info, error, stopped, retry } = useTask(rest, task)
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
      const r = await rest<{ path: string }>('save', { method: 'POST', body: { task, format: openFormat(info) }, timeoutMs: 180_000 })
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
  if (stopped && error) {
    // A permanent refusal (or a 404 past the early window): polling has stopped; the user can ask again.
    body = (
      <div data-meshy-task-stopped style={{ ...V.taskLine, display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ ...V.error, flex: 1 }}>Meshy: {error}</span>
        <Button data-meshy-task-retry onClick={retry} size="sm" variant="outline">
          <Codicon name="refresh" /> Retry
        </Button>
      </div>
    )
  } else if (!info) {
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
    <div data-meshy-card data-meshy-task-card={stopped ? 'stopped' : (info?.status ?? 'loading')} style={V.card}>
      <div style={V.cardHead}>
        <Codicon name="symbol-misc" />
        <span style={V.cardTitle}>{name}</span>
        {done && typeof info?.credits === 'number' && <span style={V.itemSub}>{info.credits} cr</span>}
        <span style={V.spacer} />
        {done && !streaming && <ActionMenu chat={chat} rest={rest} task={task} />}
        {done && (info?.has_files ?? info?.has_model) && !streaming && <FormatMenu chat={chat} os={os} rest={rest} task={task} />}
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
