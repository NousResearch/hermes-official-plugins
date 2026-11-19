// Desktop behaviours that do not need a renderer: profile/connection switches, local vs remote backend,
// "Show in folder" on a remote backend, sending a model to a remote backend, and saved-format handling.
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { cardChat, OPEN_RETRIES, recordedChat } from '../src/chat.ts'
import { bestForTask, canRender, openFormat, formatOf, mtlLibOf, mtlTextures, siblingPath } from '../src/formats.ts'
import { localHeading, reconcileSelection } from '../src/gallery.ts'
import { createLocalityTracker, eventScopeKey, localityOf, locatingOs, scopeKey, watchScope } from '../src/scope.ts'
import { balanceLabel, createStatusStore, DEFAULT_SETTINGS } from '../src/status.ts'
import { createTaskPoller, EARLY_404_MS, httpStatus, MAX_BACKOFF_MS, POLL_MS } from '../src/task-poll.ts'
import { JSON_UPLOAD_MAX_BYTES, UPLOAD_MAX_BYTES, uploadModel } from '../src/upload.ts'

/** nanostores-shaped atom: subscribe() calls back immediately with the current value, then on each set. */
function atom(value) {
  const listeners = new Set()
  return {
    get: () => value,
    subscribe(fn) {
      listeners.add(fn)
      fn(value)
      return () => listeners.delete(fn)
    },
    set(next) {
      value = next
      listeners.forEach(fn => fn(value))
    },
    listeners
  }
}

const tick = () => new Promise(resolve => setImmediate(resolve))

/** A backend per profile, like ctx.rest after the host re-routes it. */
function profileBackends(state, byProfile) {
  const calls = []
  const pending = []
  const rest = (path, _opts) => {
    const profile = state.profile.get()
    calls.push(`${profile}:${path}`)
    return new Promise((resolve, reject) => pending.push({ profile, resolve, reject, answer: byProfile[profile] }))
  }
  const answerAll = () => pending.splice(0).forEach(p => p.resolve(p.answer))
  return { rest, calls, pending, answerAll }
}

test('a profile switch re-reads the credit balance and settings from the new profile', async () => {
  const state = { profile: atom('default'), connectionId: atom('local') }
  const b = profileBackends(state, {
    default: { configured: true, balance: 900, settings: { show_credits: false, gallery_limit: 10 } },
    work: { configured: true, balance: 25, settings: { auto_rotate: false } }
  })
  const store = createStatusStore(b.rest, state)
  const seen = []
  store.onBalance(v => seen.push(v?.balance ?? null))
  void store.refresh()
  b.answerAll()
  await tick()
  assert.equal(store.balance().balance, 900)
  assert.equal(store.settings().show_credits, false)

  state.profile.set('work')
  // The old profile's numbers are gone at once (no "900 cr" under the work profile while it loads).
  assert.equal(store.balance(), null)
  assert.deepEqual(store.settings(), DEFAULT_SETTINGS)
  b.answerAll()
  await tick()
  assert.deepEqual(b.calls, ['default:status', 'work:status'])
  assert.equal(store.balance().balance, 25)
  assert.equal(store.settings().auto_rotate, false)
  assert.equal(store.settings().show_credits, true) // not carried over from the default profile
  assert.deepEqual(seen, [900, null, 25])
  store.dispose()
})

test('a reply from the old profile that lands after the switch is dropped', async () => {
  const state = { profile: atom('default'), connectionId: atom('local') }
  const b = profileBackends(state, { default: { configured: true, balance: 900 }, work: { configured: true, balance: 25 } })
  const store = createStatusStore(b.rest, state)
  void store.refresh() // in flight for "default"
  state.profile.set('work')
  const [old, fresh] = b.pending.splice(0)
  old.resolve(old.answer) // the profile we left answers first...
  await tick()
  assert.equal(store.balance(), null) // ...and is not shown under "work"
  fresh.resolve(fresh.answer)
  await tick()
  assert.equal(store.balance().balance, 25)
  // ...or last: still never painted over the new profile's numbers.
  void store.refresh()
  state.profile.set('default')
  state.profile.set('work')
  const [stale, current] = [b.pending.shift(), b.pending.pop()]
  current.resolve(current.answer)
  await tick()
  stale.resolve({ configured: true, balance: 900 })
  await tick()
  assert.equal(store.balance().balance, 25)
  store.dispose()
})

test('a connection switch with the same profile name also re-reads status', async () => {
  const state = { profile: atom('default'), connectionId: atom('local') }
  let n = 0
  const store = createStatusStore(async () => ({ configured: true, balance: ++n }), state)
  await store.refresh()
  state.connectionId.set('homelab')
  await tick()
  assert.equal(store.balance().balance, 2)
  store.dispose()
})

test('older Desktops (no host.state) keep one backend and never rescope', async () => {
  let n = 0
  const store = createStatusStore(async () => ({ configured: true, balance: ++n }), undefined)
  await store.refresh()
  assert.equal(store.balance().balance, 1)
  assert.doesNotThrow(() => watchScope(undefined, () => assert.fail('no scope to watch'))())
})

test('watchScope fires once per real switch, not on subscribe or repeats, and unsubscribes', () => {
  const state = { profile: atom('a'), connectionId: atom(null) }
  const keys = []
  const stop = watchScope(state, key => keys.push(key))
  state.profile.set('a')
  state.profile.set('b')
  state.connectionId.set('remote-1')
  assert.deepEqual(keys, ['\nb', 'remote-1\nb'])
  stop()
  assert.equal(state.profile.listeners.size, 0)
  assert.equal(state.connectionId.listeners.size, 0)
})

test('locality: primary and the local registry entry are local, any other source is remote', () => {
  const rows = [{ id: 'local', kind: 'local', label: 'This Mac' }, { id: 'homelab', kind: 'remote', label: 'Homelab' }, { id: 'box', kind: 'ssh' }]
  assert.deepEqual(localityOf({ connectionId: atom(null) }, rows), { kind: 'local' })
  assert.deepEqual(localityOf({ connectionId: atom('local') }, rows), { kind: 'local' })
  assert.deepEqual(localityOf({ connectionId: atom('homelab') }, rows), { kind: 'remote', label: 'Homelab' })
  assert.deepEqual(localityOf({ connectionId: atom('box') }, rows), { kind: 'remote', label: undefined })
  assert.deepEqual(localityOf({ connectionId: atom('homelab') }, null), { kind: 'remote', label: undefined })
  assert.deepEqual(localityOf({ connectionId: atom('mac-2') }, [{ id: 'mac-2', kind: 'local', label: 'Studio' }]), { kind: 'local', label: 'Studio' })
  assert.deepEqual(localityOf(undefined, rows), { kind: 'unknown' })
  assert.deepEqual(localityOf({ profile: atom('x') }, rows), { kind: 'unknown' })
})

test('the locality tracker reads labels from host.connections and follows a switch', async () => {
  const state = { profile: atom('default'), connectionId: atom('local') }
  const host = { state, connections: async () => [{ id: 'local', kind: 'local' }, { id: 'homelab', kind: 'remote', label: 'Homelab' }] }
  const tracker = createLocalityTracker(host)
  const seen = []
  tracker.subscribe(l => seen.push(l.kind))
  await tracker.refresh()
  assert.equal(tracker.get().kind, 'local')
  state.connectionId.set('homelab')
  await tracker.refresh()
  assert.deepEqual(tracker.get(), { kind: 'remote', label: 'Homelab' })
  assert.deepEqual(seen, ['remote'])
  // A Desktop without host.connections still answers from the id.
  const bare = createLocalityTracker({ state: { connectionId: atom('homelab') } })
  await bare.refresh()
  assert.equal(bare.get().kind, 'remote')
})

function fakeOs(revealed) {
  const calls = { reveal: [], clipboard: [] }
  return {
    calls,
    os: {
      revealPath: async path => (calls.reveal.push(path), revealed),
      writeClipboard: async text => (calls.clipboard.push(text), true),
      pickOpenPath: async () => null
    }
  }
}

test('remote backend: Show in folder never opens this computer\'s file manager; it says where the file is', async () => {
  const { os, calls } = fakeOs(true)
  const notes = []
  const wrapped = locatingOs(os, () => ({ kind: 'remote', label: 'Homelab' }), n => notes.push(n))
  assert.equal(await wrapped.revealPath('/srv/hermes/model.glb'), false)
  assert.deepEqual(calls.reveal, []) // the same path could name an unrelated local file
  assert.deepEqual(calls.clipboard, ['/srv/hermes/model.glb'])
  assert.equal(notes.length, 1)
  assert.match(notes[0].message, /on Homelab, not on this computer: \/srv\/hermes\/model\.glb \(path copied\)/)
})

test('local backend: a refused reveal (revealPath → false) is reported, not ignored', async () => {
  const notes = []
  const ok = fakeOs(true)
  assert.equal(await locatingOs(ok.os, () => ({ kind: 'local' }), n => notes.push(n)).revealPath('/h/m.glb'), true)
  assert.deepEqual(notes, [])
  const refused = fakeOs(false)
  assert.equal(await locatingOs(refused.os, () => ({ kind: 'local' }), n => notes.push(n)).revealPath('/h/m.glb'), false)
  assert.deepEqual(refused.calls.reveal, ['/h/m.glb'])
  assert.equal(notes.length, 1)
  assert.match(notes[0].message, /\/h\/m\.glb/)
  // An older Desktop (unknown locality) still tries the file manager first, then says where the file is.
  const old = fakeOs(false)
  delete old.os.writeClipboard
  const oldNotes = []
  await locatingOs(old.os, () => ({ kind: 'unknown' }), n => oldNotes.push(n)).revealPath('/h/m.glb')
  assert.deepEqual(old.calls.reveal, ['/h/m.glb'])
  assert.match(oldNotes[0].message, /\/h\/m\.glb on the machine Hermes runs on/)
})

test('the gallery heading names the backend machine, never "this machine" for a remote one', () => {
  assert.equal(localHeading({ kind: 'local' }), 'On this machine')
  assert.equal(localHeading({ kind: 'remote', label: 'Homelab' }), 'On Homelab')
  assert.equal(localHeading({ kind: 'remote' }), 'On the Hermes host')
  assert.equal(localHeading(undefined), 'Saved files')
  assert.equal(localHeading({ kind: 'unknown' }), 'Saved files')
})

const bytesOf = text => new TextEncoder().encode(text).buffer

test('upload sends the picked file as multipart bytes, never a local path', async () => {
  const calls = []
  const rest = async (path, opts) => (calls.push({ path, opts }), { path: '/srv/uploads/ab-robot.glb', name: 'robot.glb', bytes: 4 })
  const saved = await uploadModel(rest, { name: 'robot.glb', bytes: bytesOf('glTF'), type: '' })
  assert.equal(saved.path, '/srv/uploads/ab-robot.glb')
  assert.equal(calls.length, 1)
  assert.equal(calls[0].path, 'upload')
  assert.equal(calls[0].opts.method, 'POST')
  assert.equal(calls[0].opts.upload.filename, 'robot.glb')
  assert.equal(new TextDecoder().decode(calls[0].opts.upload.bytes), 'glTF')
  assert.equal(calls[0].opts.body, undefined)
})

test('an older Desktop (multipart dropped) or an OAuth remote falls back to base64 JSON', async () => {
  for (const refusal of ['400: {"detail":"no file in the request"}', 'File uploads are not supported against OAuth-gated remote backends yet.']) {
    const calls = []
    const rest = async (path, opts) => {
      calls.push(opts)
      if (opts.upload) throw new Error(refusal)
      return { path: '/srv/uploads/x.stl', name: 'x.stl', bytes: 5 }
    }
    await uploadModel(rest, { name: 'x.stl', bytes: bytesOf('solid'), type: 'model/stl' })
    assert.equal(calls.length, 2)
    assert.deepEqual(calls[1].body, { filename: 'x.stl', data: Buffer.from('solid').toString('base64') })
    assert.equal(calls[1].upload, undefined)
  }
})

test('upload never re-sends after an unknown outcome, and refuses oversize files before sending', async () => {
  let sent = 0
  const timeout = async () => {
    sent += 1
    throw new Error('Request timed out')
  }
  await assert.rejects(uploadModel(timeout, { name: 'a.glb', bytes: bytesOf('glTF') }), /timed out/)
  assert.equal(sent, 1)
  const huge = { byteLength: UPLOAD_MAX_BYTES + 1 }
  await assert.rejects(uploadModel(timeout, { name: 'a.glb', bytes: huge }), /up to 128 MB/)
  assert.equal(sent, 1)
  const midsize = new ArrayBuffer(JSON_UPLOAD_MAX_BYTES + 1)
  const old = async opts => {
    sent += 1
    throw new Error('400: {"detail":"no file in the request"}')
  }
  await assert.rejects(uploadModel((_p, o) => old(o), { name: 'a.glb', bytes: midsize }), /Update Hermes Desktop/)
})

test('saved formats: which render, and companions of an OBJ', () => {
  assert.equal(formatOf('/p/Robot.FBX'), 'fbx')
  assert.equal(formatOf('C:\\p\\crate.3mf'), '3mf')
  assert.equal(formatOf('/p/noext'), '')
  for (const f of ['glb', 'gltf', 'fbx', 'obj', 'stl', 'usdz', '3mf']) assert.ok(canRender(`/p/m.${f}`), f)
  assert.equal(canRender('/p/m.blend'), false)
  assert.equal(mtlLibOf('# Meshy\nmtllib robot.mtl\nv 0 0 0\n'), 'robot.mtl')
  assert.equal(mtlLibOf('v 0 0 0\n'), null)
  assert.deepEqual(mtlTextures('newmtl m\nmap_Kd robot_texture.png\nmap_Bump -bm 0.5 normal.png\nKd 1 1 1\n'), ['robot_texture.png', 'normal.png'])
  assert.equal(siblingPath('/home/u/game/robot.obj', 'robot.mtl'), '/home/u/game/robot.mtl')
  assert.equal(siblingPath('C:\\u\\game\\robot.obj', 'tex/robot.png'), 'C:\\u\\game\\tex\\robot.png')
  assert.equal(siblingPath('/home/u/game/robot.obj', '../../.ssh/id_rsa'), null)
  assert.equal(siblingPath('/home/u/game/robot.obj', '/etc/passwd'), null)
  assert.equal(siblingPath('/home/u/game/robot.obj', 'C:/x.png'), null)
})

test('a task with several saved formats opens its GLB, not the STL saved after it', () => {
  const files = [
    { path: '/p/crate.stl', task: 't' }, // newest first, like /local
    { path: '/p/crate.fbx', task: 't' },
    { path: '/p/crate.glb', task: 't' },
    { path: '/p/other.glb', task: 'u' }
  ]
  assert.equal(bestForTask(files, 't').path, '/p/crate.glb')
  assert.equal(bestForTask(files.slice(0, 2), 't').path, '/p/crate.fbx')
  assert.equal(bestForTask(files, 'none'), undefined)
  const saved = files.map(f => ({ kind: 'file', path: f.path, task: f.task }))
  assert.equal(reconcileSelection(null, 't', false, [], saved).path, '/p/crate.glb')
})

// ---- Task card polling (task-poll.ts): stop on permanent errors, back off on transient ones ----

/** Manual clock + timer queue: `advance(ms)` fires due timers in order. */
function fakeClock() {
  let now = 1_000_000
  let seq = 0
  const timers = new Map()
  return {
    now: () => now,
    setTimeout(fn, ms) {
      const id = ++seq
      timers.set(id, { at: now + ms, fn, ms })
      return id
    },
    clearTimeout: id => timers.delete(id),
    pending: () => [...timers.values()].map(t => t.ms),
    async advance(ms) {
      const end = now + ms
      for (;;) {
        const due = [...timers.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0]
        if (!due) break
        timers.delete(due[0])
        now = due[1].at
        due[1].fn()
        await tick()
      }
      now = end
    }
  }
}

/** A `/task` reader answering from a script of outcomes (last one repeats). */
function scriptedRead(outcomes) {
  let reads = 0
  const read = () => {
    const o = outcomes[Math.min(reads, outcomes.length - 1)]
    reads += 1
    return o instanceof Error ? Promise.reject(o) : Promise.resolve(o)
  }
  return { read, count: () => reads }
}

// The two shapes a ctx.rest rejection reaches a plugin in: the IPC-wrapped message (Electron keeps only the text)
// and an error carrying `statusCode` (the transport's structured field).
const ipcError = (status, detail) => new Error(`Error invoking remote method 'hermes:api': Error: ${status}: {"detail":"${detail}"}`)
const statusError = (status, detail) => Object.assign(new Error(`${status}: {"detail":"${detail}"}`), { statusCode: status })

test('httpStatus reads the status from both ctx.rest error shapes and nothing from a network failure', () => {
  assert.equal(httpStatus(ipcError(404, 'x')), 404)
  assert.equal(httpStatus(new Error('400: {"detail":"no Meshy API key"}')), 400)
  assert.equal(httpStatus(statusError(502, 'x')), 502)
  assert.equal(httpStatus({ status: 401 }), 401)
  assert.equal(httpStatus(new Error('Request timed out after 20000ms')), null)
  assert.equal(httpStatus(new Error('fetch failed: 500 sockets')), null)
})

for (const [label, err] of [
  ['IPC-wrapped message', ipcError(400, 'no Meshy API key')],
  ['statusCode field', statusError(403, 'outside the allowed folders')]
]) {
  test(`a task card stops polling at once on a non-404 4xx (${label}) and offers a manual retry`, async () => {
    const clock = fakeClock()
    const r = scriptedRead([err, { status: 'IN_PROGRESS' }, { status: 'SUCCEEDED' }])
    const states = []
    const poller = createTaskPoller(r.read, s => states.push(s), e => e.message, clock)
    await tick()
    assert.equal(r.count(), 1)
    assert.deepEqual(clock.pending(), [], 'no retry is scheduled after a permanent refusal')
    assert.equal(states.at(-1).stopped, true)
    await clock.advance(10 * 60_000)
    assert.equal(r.count(), 1, 'still one read ten minutes later')

    poller.retry() // the card's Retry button: one fresh read, then normal polling resumes
    await tick()
    assert.equal(r.count(), 2)
    assert.equal(states.at(-1).stopped, false)
    assert.equal(states.at(-1).info.status, 'IN_PROGRESS')
    await clock.advance(POLL_MS)
    assert.equal(r.count(), 3)
    assert.equal(states.at(-1).info.status, 'SUCCEEDED')
    assert.deepEqual(clock.pending(), [])
    poller.dispose()
  })
}

test('a 404 is retried only during the early window after mount, then polling stops', async () => {
  const clock = fakeClock()
  const r = scriptedRead([ipcError(404, 'Meshy has no finished task with that id')])
  const states = []
  const poller = createTaskPoller(r.read, s => states.push(s), e => e.message, clock)
  await tick()
  assert.equal(states.at(-1).stopped, false, 'a just-created task can 404 for a moment')
  assert.equal(clock.pending().length, 1)
  await clock.advance(EARLY_404_MS + MAX_BACKOFF_MS)
  const reads = r.count()
  assert.ok(reads >= 3 && reads <= 8, `a handful of early retries, got ${reads}`)
  assert.equal(states.at(-1).stopped, true)
  assert.deepEqual(clock.pending(), [])
  await clock.advance(60 * 60_000)
  assert.equal(r.count(), reads, 'an hour later: no more reads')
  // A manual retry after the window that 404s again stops right away (the window counts from the first mount).
  poller.retry()
  await tick()
  assert.equal(r.count(), reads + 1)
  assert.equal(states.at(-1).stopped, true)
  assert.deepEqual(clock.pending(), [])
  poller.dispose()
})

test('a task created a moment ago that 404s first is picked up once Meshy lists it', async () => {
  const clock = fakeClock()
  const r = scriptedRead([ipcError(404, 'x'), ipcError(404, 'x'), { status: 'PENDING' }, { status: 'SUCCEEDED' }])
  const states = []
  createTaskPoller(r.read, s => states.push(s), e => e.message, clock)
  await tick()
  await clock.advance(30_000)
  assert.equal(states.at(-1).info.status, 'SUCCEEDED')
  assert.equal(states.at(-1).error, null)
})

test('5xx and network failures keep polling with a capped backoff; a success resets it', async () => {
  const clock = fakeClock()
  const fail = [ipcError(502, 'Meshy API unreachable'), new Error('Request timed out'), statusError(503, 'x')]
  const outcomes = [...Array(9)].map((_, i) => fail[i % 3])
  const r = scriptedRead([...outcomes, { status: 'IN_PROGRESS' }, { status: 'IN_PROGRESS' }])
  const states = []
  createTaskPoller(r.read, s => states.push(s), e => e.message, clock)
  const delays = []
  for (let i = 0; i < 9; i++) {
    await tick()
    delays.push(clock.pending()[0])
    await clock.advance(clock.pending()[0])
  }
  assert.deepEqual(delays.slice(0, 5), [3000, 6000, 12000, 24000, 48000])
  assert.ok(delays.slice(5).every(d => d === MAX_BACKOFF_MS), 'backoff is capped')
  assert.equal(states.some(s => s.stopped), false, 'transient failures never stop the card')
  await tick()
  assert.equal(states.at(-1).info.status, 'IN_PROGRESS')
  assert.deepEqual(clock.pending(), [POLL_MS], 'back to the normal 3 s poll after a success')
})

test('a disposed card makes no further reads', async () => {
  const clock = fakeClock()
  const r = scriptedRead([{ status: 'IN_PROGRESS' }])
  const poller = createTaskPoller(r.read, () => undefined, e => e.message, clock)
  await tick()
  poller.dispose()
  await clock.advance(60_000)
  assert.equal(r.count(), 1)
})

// ---- Card and gallery buttons send to the right chat (chat.ts) ----

/** A Desktop composer bus: `null` = whichever chat is focused; an id reaches only a visible composer for it. */
function fakeDesktop({ visible = ['A', 'B'], focused = 'A', withOpenSession = true, withInsert = true, openMountsAfter = 0 } = {}) {
  const sent = []
  const calls = []
  const desk = {
    focused,
    visible: new Set(visible),
    sent,
    calls,
    composer: {
      submit(id, text) {
        calls.push(['submit', id, text])
        const target = id ?? desk.focused
        if (!desk.visible.has(target)) return false
        sent.push([target, text])
        return true
      },
      ...(withInsert
        ? {
            async insertText(id, text, opts) {
              calls.push(['insertText', id, text, opts?.mode])
              const target = id ?? desk.focused
              if (!desk.visible.has(target)) return false
              sent.push([target, `draft:${text}`])
              return true
            }
          }
        : {})
    }
  }
  if (withOpenSession) {
    desk.openSession = async id => {
      calls.push(['openSession', id])
      let polls = 0
      // The composer mounts a few polls after the open resolves, like a real hydrating chat.
      const realSubmit = desk.composer.submit
      if (openMountsAfter === 0) desk.visible.add(id)
      else
        desk.composer.submit = (sid, text) => {
          if (sid === id && ++polls > openMountsAfter) {
            desk.visible.add(id)
            desk.composer.submit = realSubmit
          }
          return realSubmit(sid, text)
        }
    }
  }
  return desk
}

test('a card button sends to the chat showing the card, even when focus moved during async work first', async () => {
  const desk = fakeDesktop({ focused: 'A' })
  const chat = cardChat(desk, 'A') // captured when the card in chat A rendered
  await new Promise(resolve => setTimeout(resolve, 5)) // e.g. a Next row loading its options
  desk.focused = 'B' // the user clicked into chat B meanwhile
  assert.equal(await chat.submit('Remesh task 019a to 10k triangles'), true)
  assert.deepEqual(desk.sent, [['A', 'Remesh task 019a to 10k triangles']])
  assert.deepEqual(desk.calls[0], ['submit', 'A', 'Remesh task 019a to 10k triangles'])
})

test('on a Desktop without the sessionId prop a card makes the exact old call: submit(null, text)', async () => {
  const desk = fakeDesktop({ focused: 'B' })
  const chat = cardChat(desk, undefined)
  assert.equal(await chat.submit('Convert to FBX'), true)
  assert.deepEqual(desk.calls, [['submit', null, 'Convert to FBX']])
  // A chat with no session yet (null) also addresses the active composer.
  desk.calls.length = 0
  await cardChat(desk, null).submit('x')
  assert.deepEqual(desk.calls, [['submit', null, 'x']])
})

test('a card button resolves false when its chat has no composer on screen, so the card warns instead of misrouting', async () => {
  const desk = fakeDesktop({ visible: ['B'], focused: 'B' })
  assert.equal(await cardChat(desk, 'A').submit('Rig this model'), false)
  assert.deepEqual(desk.sent, [], 'nothing lands in the focused chat B')
  assert.equal(await cardChat({}, 'A').submit('x'), false, 'no composer bus at all')
})

test('a gallery button opens the chat that made the model when it is not on screen, then sends there', async () => {
  const desk = fakeDesktop({ visible: ['B'], focused: 'B', openMountsAfter: 2 })
  const chat = recordedChat(desk, async () => 'A', async () => undefined)
  assert.equal(await chat.submit('Texture task 019a'), true)
  assert.deepEqual(desk.sent, [['A', 'Texture task 019a']])
  const opened = desk.calls.findIndex(c => c[0] === 'openSession')
  assert.deepEqual(desk.calls[opened], ['openSession', 'A'])
  assert.deepEqual(desk.calls[0], ['submit', 'A', 'Texture task 019a'], 'tries the open chat first')
  assert.ok(desk.calls.slice(0, opened).every(c => c[1] === 'A'))
})

test('a gallery button does not reopen a chat that is already on screen', async () => {
  const desk = fakeDesktop({ visible: ['A', 'B'], focused: 'B' })
  assert.equal(await recordedChat(desk, async () => 'A').submit('x'), true)
  assert.equal(desk.calls.some(c => c[0] === 'openSession'), false)
  assert.deepEqual(desk.sent, [['A', 'x']])
})

test('a gallery button keeps the old call with no recorded chat, without openSession, or when the lookup fails', async () => {
  for (const [desk, resolve] of [
    [fakeDesktop({ focused: 'B' }), async () => null],
    [fakeDesktop({ focused: 'B', withOpenSession: false }), async () => 'A'],
    [fakeDesktop({ focused: 'B' }), async () => Promise.reject(new Error('404'))]
  ]) {
    assert.equal(await recordedChat(desk, resolve).submit('x'), true)
    assert.deepEqual(desk.calls, [['submit', null, 'x']])
  }
})

test('a gallery button gives up (false) when the opened chat never takes the text, and never opens a chat for a missing verb', async () => {
  const desk = fakeDesktop({ visible: ['B'], focused: 'B', openMountsAfter: 1e9 })
  assert.equal(await recordedChat(desk, async () => 'A', async () => undefined).submit('x'), false)
  assert.equal(desk.calls.filter(c => c[0] === 'submit').length, 1 + OPEN_RETRIES)
  assert.deepEqual(desk.sent, [])

  const noInsert = fakeDesktop({ visible: ['B'], withInsert: false })
  assert.equal(await recordedChat(noInsert, async () => 'A').insert('a prompt'), false)
  assert.equal(noInsert.calls.some(c => c[0] === 'openSession'), false)
})

test('reuse-prompt inserts into the recorded chat (opening it) and into the card chat as a block', async () => {
  const desk = fakeDesktop({ visible: ['B'], focused: 'B' })
  assert.equal(await recordedChat(desk, async () => 'A', async () => undefined).insert('a red fox'), true)
  assert.deepEqual(desk.sent, [['A', 'draft:a red fox']])
  assert.ok(desk.calls.some(c => c[0] === 'openSession' && c[1] === 'A'))
  const card = fakeDesktop({ focused: 'B' })
  await cardChat(card, 'A').insert('p')
  assert.deepEqual(card.calls, [['insertText', 'A', 'p', 'block']])
})

test('a task card opens the format the backend picked, and GLB on older backends', () => {
  assert.equal(openFormat({ view_format: 'fbx' }), 'fbx') // a Convert-to-FBX task carries no GLB
  assert.equal(openFormat({ view_format: 'stl' }), 'stl')
  assert.equal(openFormat({}), 'glb') // older backend: no view_format
  assert.equal(openFormat(null), 'glb')
  assert.equal(openFormat({ view_format: 'blend' }), 'glb') // never ask /save for a format the viewer cannot draw
})

test('the balance chip blames the key only for a rejected key, whatever Meshy writes in a 403', () => {
  const refused = 'Meshy refused the request: Request rejected: IP not allowed'
  assert.equal(balanceLabel({ configured: true, error: refused, error_code: 'refused' }), 'refused')
  assert.equal(balanceLabel({ configured: true, error: refused }), 'refused')
  assert.equal(balanceLabel({ configured: true, error: 'Meshy rejected the API key', error_code: 'key_rejected' }), 'key rejected')
  assert.equal(balanceLabel({ configured: true, error: 'Meshy rejected the API key' }), 'key rejected')
  assert.equal(balanceLabel({ configured: true, error: 'Meshy API error 503', error_code: 'offline' }), 'offline')
  assert.equal(balanceLabel({ configured: false }), 'no key')
  assert.equal(balanceLabel({ configured: true, balance: 1200 }), `${(1200).toLocaleString()} cr`)
})

test("an event's scope comes from its tags; untagged events belong to the scope in front", () => {
  const at = (profile, connectionId) => ({ profile: { get: () => profile }, connectionId: { get: () => connectionId } })
  const local = at('default', null)
  const remote = at('default', 'homelab')
  assert.equal(eventScopeKey({ profile: 'default' }, remote), scopeKey(local), 'no connection tag = the primary connection')
  assert.notEqual(eventScopeKey({ profile: 'default' }, remote), scopeKey(remote))
  assert.equal(eventScopeKey({ profile: 'default', connectionId: 'homelab' }, local), scopeKey(remote))
  assert.equal(eventScopeKey({}, remote), scopeKey(remote))
  assert.equal(eventScopeKey({ profile: ' ' }, at('', null)), scopeKey(at(undefined, null)), "a blank profile is 'default'")
  assert.equal(eventScopeKey({ connectionId: 'homelab' }, at('work', null)), scopeKey(at('work', 'homelab')))
})
