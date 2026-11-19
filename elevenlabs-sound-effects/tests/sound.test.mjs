// node --experimental-strip-types --test tests/sound.test.mjs
import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  clockLabel,
  isSoundToolCall,
  lengthLabel,
  matches,
  nextActions,
  PromiseCache,
  ratePerSecond,
  SEND_FAILED,
  showsFallback,
  soundIdOf,
  submitToChat
} from '../src/sound.ts'

const rec = over => ({
  id: 'abcdef012345',
  created_at: '2026-10-23T12:00:00+00:00',
  prompt: 'Glass bottle shattering on a stone floor',
  duration_seconds: 2,
  loop: false,
  prompt_influence: 0.3,
  model: 'eleven_text_to_sound_v2',
  output_format: 'mp3_44100_128',
  file_name: 'sfx_glass_20261023_120000_abcdef012345.mp3',
  bytes: 33061,
  billed_characters: '20',
  session_id: '20261023_120000_aaaaaa',
  kind: 'mp3',
  exists: true,
  path: '/h/cache/generated/audio/x.mp3',
  size: 33061,
  ...over
})

// Exactly the tool schema's argument names (elevenlabs_sound_effect): anything else is a refused call.
const SCHEMA_ARGS = new Set(['prompt', 'duration_seconds', 'loop', 'prompt_influence', 'model'])

test('every action quotes the cost from this sound’s bill and sends only schema arguments', () => {
  const actions = nextActions(rec())
  assert.deepEqual(actions.map(a => a.id), ['variations', 'shorter', 'longer', 'loop', 'model'])
  const byId = Object.fromEntries(actions.map(a => [a.id, a]))
  assert.match(byId.variations.cost, /60 credits total \(3 × 20 credits\)/)
  assert.match(byId.shorter.label, /1 s/)
  assert.match(byId.shorter.cost, /≈ 10 credits \(10\/s/)
  assert.match(byId.longer.cost, /≈ 40 credits/)
  assert.match(byId.loop.message, /loop true/)
  assert.match(byId.model.label, /Try v3/)
  assert.match(byId.model.message, /model eleven_text_to_sound_v3/)
  for (const a of actions) {
    assert.match(a.message, /elevenlabs_sound_effect/)
    const names = [...a.message.matchAll(/\b([a-z]+(?:_[a-z]+)*) (?=["\d]|true|false|eleven_)/g)].map(m => m[1])
    for (const n of names.filter(n => n.includes('_') || n === 'prompt' || n === 'loop' || n === 'model')) {
      assert.ok(SCHEMA_ARGS.has(n), `${a.id} sends ${n}`)
    }
    assert.doesNotMatch(a.message, /\b(seed|output_format|format|model_id|text)\b(?= )/)
  }
})

test('an auto-length sound never claims a per-second price it cannot know', () => {
  const r = rec({ duration_seconds: null, billed_characters: '50' })
  assert.equal(ratePerSecond(r), null)
  const actions = nextActions(r, 2.0)
  const byId = Object.fromEntries(actions.map(a => [a.id, a]))
  assert.match(byId.variations.cost, /150 credits total \(3 × 50 credits\)/)
  assert.match(byId.shorter.cost, /rate is unknown/)
  assert.doesNotMatch(byId.variations.message, /duration_seconds/)
  assert.match(byId.shorter.message, /duration_seconds 1/)
})

test('bounds and loops: no shorter below 0.5 s, no longer past 30 s, no make-it-loop on a loop', () => {
  assert.ok(!nextActions(rec({ duration_seconds: 0.5, billed_characters: '5' })).some(a => a.id === 'shorter'))
  assert.ok(!nextActions(rec({ duration_seconds: 30, billed_characters: '300' })).some(a => a.id === 'longer'))
  const loop = nextActions(rec({ loop: true, model: 'eleven_text_to_sound_v3' }))
  assert.ok(!loop.some(a => a.id === 'loop'))
  assert.match(loop.find(a => a.id === 'model').label, /Try v2/)
  assert.deepEqual(nextActions(rec({ prompt: '' })), [])
})

test('a missing bill is said plainly', () => {
  const v = nextActions(rec({ billed_characters: null }))[0]
  assert.match(v.cost, /cost unknown/)
})

test('tool-call detection covers direct calls and tool search wrappers', () => {
  assert.ok(isSoundToolCall('elevenlabs_sound_effect', {}))
  assert.ok(isSoundToolCall('tool_call', { name: 'elevenlabs_sound_effect' }))
  assert.ok(isSoundToolCall('tool_call', { calls: [{ name: 'x' }, { name: 'elevenlabs_sound_effect' }] }))
  assert.ok(!isSoundToolCall('tool_call', { name: 'web_search' }))
  assert.ok(!isSoundToolCall('terminal', null))
})

test('library search matches every term', () => {
  assert.ok(matches(rec(), 'glass stone'))
  assert.ok(matches(rec(), 'v2 mp3'))
  assert.ok(!matches(rec(), 'glass rain'))
})

test('Next keeps the prompt verbatim: no whitespace rewrite, no code-unit slice through an emoji', () => {
  const prompt = `${'🔔'.repeat(300)}\nthen a door slam`
  const variations = nextActions(rec({ prompt }))[0]
  assert.ok(variations.message.includes(JSON.stringify(prompt)), variations.message.slice(0, 120))
})

test('the Length chip shows the real length and the length asked for when they differ', () => {
  assert.equal(lengthLabel(rec({ duration_seconds: 1 }), 1.5), '1.5 s (asked 1 s)')
  assert.equal(lengthLabel(rec({ duration_seconds: 2, length_seconds: 2.25 }), null), '2.25 s (asked 2 s)')
  assert.equal(lengthLabel(rec({ duration_seconds: 2, length_seconds: 2.0 }), null), '2 s')
  assert.equal(lengthLabel(rec({ duration_seconds: null, length_seconds: 3.1 }), null), '3.1 s')
  assert.equal(lengthLabel(rec({ duration_seconds: null }), null), 'auto length')
})

test('a card in a chat with no session yet addresses that fresh draft, never the active composer', () => {
  const calls = []
  const submit = (session, text) => (calls.push([session, text]), session !== 'gone')
  assert.equal(submitToChat(submit, null, 'hi'), 'sent')
  assert.equal(submitToChat(submit, 'sess-1', 'hi'), 'sent')
  assert.equal(submitToChat(submit, undefined, 'hi'), 'sent') // older host: the active composer
  assert.deepEqual(calls.map(c => c[0]), ['new', 'sess-1', null])
  assert.equal(submitToChat(submit, 'gone', 'hi'), 'not-visible')
})

test('a host without composer.submit gets its own notice, not the chat-not-open one', () => {
  assert.equal(submitToChat(undefined, 'sess-1', 'hi'), 'no-composer')
  assert.match(SEND_FAILED['no-composer'], /update Desktop/)
  assert.match(SEND_FAILED['not-visible'], /not open on screen/)
})

test('decoded sounds are shared, bounded and never cache a failure', async () => {
  const cache = new PromiseCache(2)
  let loads = 0
  const load = v => () => (loads++, Promise.resolve(v))
  const [a1, a2] = await Promise.all([cache.get('a', load('A')), cache.get('a', load('A'))])
  assert.equal(a1, 'A')
  assert.equal(a2, 'A')
  assert.equal(loads, 1) // two cards of one sound: one request
  await cache.get('b', load('B'))
  await cache.get('a', load('A')) // a is now the most recent
  await cache.get('c', load('C')) // evicts b
  assert.equal(loads, 3)
  await cache.get('b', load('B'))
  assert.equal(loads, 4)
  await assert.rejects(cache.get('x', () => Promise.reject(new Error('boom'))))
  await new Promise(r => setTimeout(r, 0))
  assert.equal(await cache.get('x', load('X')), 'X')
})

test('the card draws only this tool\u2019s successful sounds: id from the result, else from the file name', () => {
  const name = 'sfx_door-knock_20261024_101500_0123456789ab.mp3'
  assert.equal(soundIdOf({ success: true, sound_id: 'abcdef012345' }, name), 'abcdef012345')
  assert.equal(soundIdOf(JSON.stringify({ success: true, sound_id: 'abcdef012345' }), name), 'abcdef012345') // reopened chat
  assert.equal(soundIdOf(null, name), '0123456789ab') // result not kept: the id the tool writes into every name
  assert.equal(soundIdOf('not json', name), '0123456789ab')
  assert.equal(soundIdOf({ success: false, error: 'x' }, name), null) // a failed call produced nothing
  assert.equal(soundIdOf({ error: 'ElevenLabs returned HTTP 400' }, name), null)
  assert.equal(soundIdOf({ success: true, sound_id: '../../etc' }, 'voice.mp3'), null)
  assert.equal(soundIdOf({}, 'tts_20261024_101500.mp3'), null)
  assert.equal(soundIdOf({}, 'sfx_x_20261024_101500_0123456789ab.flac'), null)
})

test('Next asks the agent to reply with the MEDIA: line, which Desktop draws as the card', () => {
  for (const a of nextActions(rec())) assert.match(a.message, /media_tag line/)
})

test('a tool-media card hands the sound to the built-in player whenever it cannot play it', () => {
  const card = (record, more = {}) => showsFallback({ record, ...more }, true)
  assert.equal(card('loading'), false)
  assert.equal(card('ready', { exists: true, audio: 'ready' }), false)
  assert.equal(card('error'), true) // record not readable (another profile, plugin backend off)
  assert.equal(card('ready', { exists: false, audio: 'idle' }), true) // gone or refused (hard link)
  assert.equal(card('ready', { exists: true, audio: 'error' }), true) // the audio would not load
  // A directive card from an old transcript has no built-in player to hand back to.
  assert.equal(showsFallback({ record: 'error' }, false), false)
})

test('the card clock reads position and length in tenths', () => {
  assert.equal(clockLabel(0), '0:00.0')
  assert.equal(clockLabel(0.44), '0:00.4')
  assert.equal(clockLabel(59.96), '1:00.0') // rounds into the next minute, never 0:60.0
  assert.equal(clockLabel(64.25), '1:04.3')
  assert.equal(clockLabel(Number.NaN), '0:00.0')
})
