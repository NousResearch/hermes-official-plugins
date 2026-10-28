// Pure helpers of the Desktop half (src/music.ts), run with `node --experimental-strip-types --test`.
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { isMusicToolCall, lyricWindow, nextActions, sections, trackIdOf } from '../src/music.ts'

const base = {
  id: 'a1b2c3d4e5f6', created_at: null, kind: 'song', tool: 'elevenlabs_music_compose', title: 'Morning Run',
  prompt: 'upbeat synthwave, 120 BPM', description: null, genres: ['synthwave'], languages: null, explicit: false,
  model: 'music_v2_5', instrumental: false, finetune_id: null, seed: null, requested_seconds: 40, length_seconds: 40.1,
  output_format: 'mp3_44100_128', file_name: 'music_morning-run_20261028_120000_a1b2c3d4e5f6.mp3', bytes: 1,
  song_id: 'GO0gWJf8dZosxuzBk9OJ', stored_for_editing: true, parent_id: null, stem: null, session_id: null,
  ext: 'mp3', exists: true, path: '/x', size: 1
}
const plan = { chunks: [
  { text: '[Verse]\nrunning at dawn', duration_ms: 20000, positive_styles: ['synthwave', '120 BPM'] },
  { text: '[Chorus]\nfaster', duration_ms: 20000, positive_styles: ['big chorus'] }
] }

test('the card claims only files the tools wrote, and never a failed call', () => {
  assert.equal(trackIdOf({ success: true }, base.file_name), 'a1b2c3d4e5f6')
  assert.equal(trackIdOf(JSON.stringify({ success: true }), 'stem_vocals-x_20261028_120000_0123456789ab.mp3'), '0123456789ab')
  assert.equal(trackIdOf({ error: 'boom' }, base.file_name), null)
  assert.equal(trackIdOf({ success: true }, 'tts_reply_20261028.mp3'), null)
})

test('Next actions send only arguments the tools accept and keep the song when editing', () => {
  const actions = nextActions(base, { plan }, 40.1)
  const ids = actions.map(a => a.id)
  assert.deepEqual(ids.slice(0, 3), ['variations', 'instrumental', 'model'])
  const extend = actions.find(a => a.id === 'extend')
  const chunks = JSON.parse(/chunks (\[.*\])\. It bills/.exec(extend.message)[1])
  assert.deepEqual(chunks[0], { song_id: base.song_id, range: { start_ms: 0, end_ms: 40100 } })
  assert.equal(chunks[1].duration_ms, 30000)
  // Section edits are drafts the user finishes, one per plan section.
  assert.deepEqual(actions.filter(a => a.id === 'edit-section').map(a => a.draft), [true, true])
  // A song that was not stored at ElevenLabs offers no edits; a stem offers nothing.
  assert.ok(!nextActions({ ...base, stored_for_editing: false }, { plan }).some(a => a.id === 'extend'))
  assert.deepEqual(nextActions({ ...base, kind: 'stem' }, null), [])
})

test('sections and the karaoke window follow the plan and the word timings', () => {
  assert.deepEqual(sections(plan).map(s => [s.label, s.start_ms, s.end_ms]), [['Verse', 0, 20000], ['Chorus', 20000, 40000]])
  const words = [{ word: 'a', start_ms: 0, end_ms: 400 }, { word: 'b', start_ms: 500, end_ms: 900 }, { word: 'c', start_ms: 9000, end_ms: 9400 }]
  assert.deepEqual(lyricWindow(words, 600), { words: ['a', 'b', 'c'], current: 1 })
  assert.equal(lyricWindow(words, 3000), null) // instrumental gap before "c"
  assert.equal(lyricWindow(words, 10000), null) // past the last word
})

test('tool events count when wrapped by tool search', () => {
  assert.ok(isMusicToolCall('elevenlabs_music_job', {}))
  assert.ok(isMusicToolCall('tool_call', { calls: [{ name: 'elevenlabs_music_compose', arguments: {} }] }))
  assert.ok(!isMusicToolCall('tool_call', { calls: [{ name: 'elevenlabs_music_finetunes' }] }))
})
