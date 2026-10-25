import assert from 'node:assert/strict'
import { test } from 'node:test'
import { imageNextActions, listFailureNotices, matchesKind, reconcileSelection, sourceForAsset } from '../src/gallery.ts'

const model = {
  task_id: 'task', task_type: 'image-to-3d', prompt: 'Mech', kind: 'model',
  image_urls: [], expired: false, glb_url: 'https://assets.meshy.ai/mech.glb'
}

for (const [name, asset, expected] of [
  ['model from an image remains a model', model, 'model'],
  ['concept image is not passed to the GLTF loader', { ...model, kind: 'image', image_urls: ['https://assets.meshy.ai/concept.png'] }, 'image'],
  ['saved model survives remote expiry', { ...model, expired: true, local_path: '/project/mech.glb' }, 'file'],
  ['remote expiry reaches the viewer without guessing from task age', { ...model, expired: true }, 'expired'],
  ['missing output has no selectable preview', { ...model, glb_url: null }, 'none']
]) {
  test(name, () => {
    const source = sourceForAsset(asset)
    const actual = !source ? 'none' : source.kind === 'file' ? 'file' : source.expired ? 'expired' : source.media
    assert.equal(actual, expected)
  })
}

test('image set keeps every view for navigation', () => {
  const images = ['https://assets.meshy.ai/front.png', 'https://assets.meshy.ai/back.png']
  assert.deepEqual(sourceForAsset({ ...model, kind: 'image', image_urls: images }).images, images)
})

for (const [filter, expected] of [['all', ['image', 'model']], ['image', ['image']], ['model', ['model']]]) {
  test(`${filter} filter selects the intended assets`, () => {
    assert.deepEqual(['image', 'model'].filter(kind => matchesKind(filter, kind)), expected)
  })
}

test('image Next passes the source task to the generation tools', () => {
  const rows = imageNextActions('image-task', {
    kind: 'remote', url: 'https://assets.meshy.ai/a.png', label: 'Mech', media: 'image'
  })
  assert.ok(rows.find(row => row.id === 'model').message.includes('meshy_image_to_3d'))
  assert.ok(rows.find(row => row.id === 'edit').message.includes('meshy_image_to_image'))
  assert.ok(rows.every(row => row.message.includes('input_task_id "image-task"')))
  assert.ok(!rows.some(row => row.id === 'all-model'))
})

test('saved image Next uses local files rather than an expired task reference', () => {
  const rows = imageNextActions('old-task', {
    kind: 'file', path: '/project/front.png', media: 'image',
    images: ['/project/front.png', '/project/back.png']
  })
  const single = rows.find(row => row.id === 'model')
  const all = rows.find(row => row.id === 'all-model')
  assert.ok(single.message.includes('meshy_image_to_3d'))
  assert.ok(single.message.includes('/project/front.png'))
  assert.ok(!single.message.includes('/project/back.png'))
  assert.ok(all.message.includes('meshy_multi_image_to_3d'))
  assert.ok(all.message.includes('/project/front.png') && all.message.includes('/project/back.png'))
  assert.ok(rows.every(row => !row.message.includes('input_task_id')))
})

test('image actions change the selected-view reference without changing the all-views reference', () => {
  const source = {
    kind: 'remote', url: 'https://assets.meshy.ai/front.png', media: 'image', label: 'Mech',
    images: ['https://assets.meshy.ai/front.png', 'https://assets.meshy.ai/back.png']
  }
  const front = imageNextActions('task', source, 0)
  const back = imageNextActions('task', source, 1)
  for (const id of ['model', 'edit', 'views']) {
    assert.notEqual(front.find(row => row.id === id).reference, back.find(row => row.id === id).reference)
  }
  assert.equal(front.find(row => row.id === 'all-model').reference, back.find(row => row.id === 'all-model').reference)
  assert.ok(back.find(row => row.id === 'all-model').message.includes('input_task_id "task"'))
})

test('opening a remote gallery source retains its task ID for workspace actions', () => {
  assert.equal(sourceForAsset(model).task, model.task_id)
})

const clip = { kind: 'remote', task: 'task', url: 'https://assets.meshy.ai/running.glb', label: 'Running' }
const bindPose = { kind: 'file', task: 'task', path: '/saved/bind-pose.glb' }
const savedClip = { kind: 'file', task: 'task', path: '/project/running.glb' }
for (const [name, selected, explicit, remote, saved, expected] of [
  ['explicit remote clip survives a different default and saved sibling', clip, true, [model], [bindPose], clip],
  ['explicit remote clip survives an absent gallery task', clip, true, [], [], clip],
  ['explicit local clip outside the bounded list survives a saved sibling',
    savedClip, true, [model], [bindPose], savedClip],
  ['explicit local selection receives metadata only for its own file',
    savedClip, true, [model], [bindPose, { ...savedClip, label: 'Running' }], { ...savedClip, label: 'Running' }],
  ['generic task selection switches to its saved copy', clip, false, [model], [bindPose], bindPose],
  ['generic task refresh preserves the chosen saved variant',
    savedClip, false, [model], [bindPose, savedClip], savedClip],
  ['generic remote selection clears when no output remains', clip, false, [], [], null],
  ['selecting another task does not restore the initial clip',
    sourceForAsset({ ...model, task_id: 'other' }), false, [{ ...model, task_id: 'other' }], [bindPose], sourceForAsset({ ...model, task_id: 'other' })]
]) {
  test(name, () => {
    assert.deepEqual(reconcileSelection(selected, selected.task, explicit, remote, saved), expected)
  })
}

test('a product the gallery could not list gets one notice per reason, and a malformed list none', () => {
  const plan = 'Meshy refused the request: Your plan does not include Creative Lab'
  assert.deepEqual(listFailureNotices([
    { task_type: 'creative-lab-lamp-build', error: plan },
    { task_type: 'remesh', error: 'Meshy API error 503' },
    { task_type: 'creative-lab-keycap-build', error: plan }
  ]), [`Couldn't list creative-lab-lamp-build, creative-lab-keycap-build: ${plan}`, "Couldn't list remesh: Meshy API error 503"])
  for (const failed of [undefined, null, [], {}, [null, 7, { task_type: 'x' }]]) assert.deepEqual(listFailureNotices(failed), [])
})
