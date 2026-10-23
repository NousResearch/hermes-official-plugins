#!/usr/bin/env node
// Render a short MP4 preview of a local .glb for chat apps (Discord, Telegram, Slack...).
// Static model: 360° turntable. Animated model: plays a clip (first, or --clip NAME) with a
// slow orbit. Headless Chromium + three.js on software WebGL (SwiftShader), so no GPU is
// needed. Frames are stepped deterministically (frame i at t = i/fps), never by wall clock.
//
//   node render_preview.mjs <model.glb> [--out file.mp4] [--clip NAME] [--seconds 6]
//        [--fps 30] [--size 720] [--label "Knight"] [--no-label] [--max-mb 8] [--list-clips]
//
// Needs: Node 22+ (built-in WebSocket/fetch), a Chromium/Chrome binary, ffmpeg, three@0.180.0.
// Every dependency is discovered; see references/preview-video.md. Prints one JSON line.

import { spawn, execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const THREE_VERSION = '0.180.0'
const THREE_TARBALL = `https://registry.npmjs.org/three/-/three-${THREE_VERSION}.tgz`
const THREE_SRI = 'sha512-o+qycAMZrh+TsE01GqWUxUIKR1AL0S8pq7zDkYOQw8GqfX8b8VoCKYUoHbhiX5j+7hr8XsuHDVU6+gkQJQKg9w=='
const dbg = (...m) => { if (process.env.MESHY_PREVIEW_DEBUG) console.error('[preview]', ...m) }
const HERE = path.dirname(fileURLToPath(import.meta.url))

function die(msg, extra = {}) {
  console.log(JSON.stringify({ ok: false, error: msg, ...extra }))
  process.exit(1)
}

function parseArgs(argv) {
  const a = { size: 720, fps: 30, seconds: null, maxMb: 8, label: null, noLabel: false }
  const rest = []
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i]
    const v = () => { if (i + 1 >= argv.length) die(`missing value for ${k}`); return argv[++i] }
    if (k === '--out') a.out = v()
    else if (k === '--clip') a.clip = v()
    else if (k === '--seconds') a.seconds = +v()
    else if (k === '--fps') a.fps = +v()
    else if (k === '--size') a.size = +v()
    else if (k === '--label') a.label = v()
    else if (k === '--no-label') a.noLabel = true
    else if (k === '--max-mb') a.maxMb = +v()
    else if (k === '--chrome') a.chrome = v()
    else if (k === '--list-clips') a.listClips = true
    else if (k === '--keep-frames') a.keepFrames = true
    else if (k === '-h' || k === '--help') { console.log(fs.readFileSync(fileURLToPath(import.meta.url), 'utf8').split('\n').slice(1, 11).join('\n')); process.exit(0) }
    else if (k.startsWith('--')) die(`unknown option ${k}`)
    else rest.push(k)
  }
  if (rest.length !== 1) die('usage: node render_preview.mjs <model.glb> [--out file.mp4] [--clip NAME]')
  if (a.out && !/\.mp4$/i.test(a.out)) die('--out must end in .mp4 (the encoder writes H.264 MP4)')
  a.input = path.resolve(rest[0])
  a.size = Math.max(256, Math.min(1080, Math.round(a.size / 2) * 2))
  a.fps = Math.max(10, Math.min(60, Math.round(a.fps) || 30))
  return a
}

const hermesHome = () => path.resolve(process.env.HERMES_HOME || path.join(os.homedir(), '.hermes'))

// Hermes' managed tool store: HERMES_RUNTIME_DIR when set (Hermes exports it to its children),
// else <hermes root>/tools. Profiles live under <root>/profiles/<name>, the store at <root>/tools.
function toolStores() {
  const out = []
  if (process.env.HERMES_RUNTIME_DIR) out.push(process.env.HERMES_RUNTIME_DIR)
  const home = hermesHome()
  out.push(path.join(home, 'tools'))
  const m = home.match(/^(.*)[\\/]profiles[\\/][^\\/]+$/)
  if (m) out.push(path.join(m[1], 'tools'))
  out.push(path.join(os.homedir(), '.hermes', 'tools'))
  return [...new Set(out)].filter(d => fs.existsSync(d))
}

function globDirs(dir, prefix) {
  try { return fs.readdirSync(dir).filter(n => n.startsWith(prefix)).sort().reverse().map(n => path.join(dir, n)) } catch { return [] }
}

function which(name) {
  const exts = process.platform === 'win32' ? ['.exe', '.cmd', ''] : ['']
  for (const d of (process.env.PATH || '').split(path.delimiter)) for (const e of exts) {
    const p = path.join(d, name + e)
    try { if (fs.statSync(p).isFile()) return p } catch {}
  }
  return null
}

const firstFile = list => list.find(p => { try { return p && fs.statSync(p).isFile() } catch { return false } }) || null

function findFfmpeg() {
  const exe = process.platform === 'win32' ? 'ffmpeg.exe' : 'ffmpeg'
  const c = [process.env.FFMPEG_PATH]
  for (const s of toolStores()) for (const d of globDirs(s, 'ffmpeg')) c.push(path.join(d, 'bin', exe), path.join(d, exe))
  return firstFile(c) || which('ffmpeg')
}

function findChrome(explicit) {
  const c = [explicit, process.env.MESHY_PREVIEW_CHROME, process.env.AGENT_BROWSER_EXECUTABLE_PATH, process.env.CHROME_PATH]
  const rel = {
    linux: ['chrome-linux64/chrome', 'chrome-linux/chrome', 'chrome-headless-shell-linux64/chrome-headless-shell'],
    darwin: ['chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing',
      'chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing',
      'chrome-mac/Chromium.app/Contents/MacOS/Chromium',
      'chrome-headless-shell-mac-arm64/chrome-headless-shell', 'chrome-headless-shell-mac-x64/chrome-headless-shell'],
    win32: ['chrome-win64/chrome.exe', 'chrome-win/chrome.exe', 'chrome-headless-shell-win64/chrome-headless-shell.exe'],
  }[process.platform] || []
  const pwRoots = [process.env.PLAYWRIGHT_BROWSERS_PATH, ...toolStores(),
    path.join(os.homedir(), '.cache', 'ms-playwright'), path.join(os.homedir(), 'Library', 'Caches', 'ms-playwright'),
    process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, 'ms-playwright')].filter(Boolean)
  for (const r of pwRoots) for (const pre of ['chromium-', 'chromium_headless_shell-', 'chromium'])
    for (const d of globDirs(r, pre)) for (const x of rel) c.push(path.join(d, x))
  c.push('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', '/Applications/Chromium.app/Contents/MacOS/Chromium',
    process.env.PROGRAMFILES && path.join(process.env.PROGRAMFILES, 'Google/Chrome/Application/chrome.exe'),
    process.env['PROGRAMFILES(X86)'] && path.join(process.env['PROGRAMFILES(X86)'], 'Microsoft/Edge/Application/msedge.exe'))
  return firstFile(c) || ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'chrome', 'msedge'].map(which).find(Boolean) || null
}

// three@0.180.0: the plugin's own node_modules when present (dev checkout), else a verified copy
// of the npm tarball cached once under <HERMES_HOME>/cache/meshy-preview/.
async function findThree() {
  const want = d => { try { return JSON.parse(fs.readFileSync(path.join(d, 'package.json'), 'utf8')).version === THREE_VERSION ? d : null } catch { return null } }
  for (let d = HERE; d !== path.dirname(d); d = path.dirname(d)) {
    const hit = want(path.join(d, 'node_modules', 'three'))
    if (hit) return hit
  }
  const cache = path.join(hermesHome(), 'cache', 'meshy-preview', `three-${THREE_VERSION}`)
  if (want(path.join(cache, 'package'))) return path.join(cache, 'package')
  const res = await fetch(THREE_TARBALL)
  if (!res.ok) die(`could not download three ${THREE_VERSION}: HTTP ${res.status}`)
  const buf = Buffer.from(await res.arrayBuffer())
  if ('sha512-' + createHash('sha512').update(buf).digest('base64') !== THREE_SRI) die('three tarball integrity mismatch')
  fs.mkdirSync(cache, { recursive: true })
  const tgz = path.join(cache, 'three.tgz')
  fs.writeFileSync(tgz, buf)
  execFileSync('tar', ['-xzf', tgz, '-C', cache, 'package/package.json', 'package/build', 'package/examples/jsm'])
  fs.rmSync(tgz)
  return want(path.join(cache, 'package')) || die('three extraction failed')
}

const MAIN = `
import * as THREE from 'three'
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js'
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js'
const P = window.__P
const S = P.size
const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true })
renderer.setPixelRatio(1); renderer.setSize(S, S, false)
renderer.outputColorSpace = THREE.SRGBColorSpace
renderer.toneMapping = THREE.ACESFilmicToneMapping; renderer.toneMappingExposure = 1.0
renderer.shadowMap.enabled = true; renderer.shadowMap.type = THREE.PCFSoftShadowMap
const scene = new THREE.Scene()
const bg = document.createElement('canvas'); bg.width = 2; bg.height = 256
{ const g = bg.getContext('2d'), gr = g.createLinearGradient(0, 0, 0, 256)
  gr.addColorStop(0, '#d9dce1'); gr.addColorStop(1, '#9ea3ab'); g.fillStyle = gr; g.fillRect(0, 0, 2, 256) }
const bgTex = new THREE.CanvasTexture(bg); bgTex.colorSpace = THREE.SRGBColorSpace; scene.background = bgTex
const pmrem = new THREE.PMREMGenerator(renderer)
scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture
scene.environmentIntensity = 0.9
scene.add(new THREE.HemisphereLight(0xffffff, 0x8d8d8d, 0.6))
const key = new THREE.DirectionalLight(0xffffff, 1.6); key.castShadow = true
key.shadow.mapSize.set(1024, 1024); key.shadow.bias = -0.0005; key.shadow.normalBias = 0.02
scene.add(key, key.target)
// No network, no local files: a GLB can still name absolute image/buffer URIs. Only its own embedded
// resources (data:, and the blob: URLs three.js makes for bufferView images) load; anything else gets
// a 1x1 white PNG and is reported under blocked_urls.
const blocked = []
const manager = new THREE.LoadingManager()
manager.setURLModifier(u => {
  if (/^(data|blob):/i.test(u)) return u
  blocked.push(u)
  return 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC'
})
let gltf
try { gltf = await new GLTFLoader(manager).parseAsync(window.__glb.buffer, '') } catch (e) {
  window.__info = { error: 'GLB load failed: ' + (e && e.message || e) + (blocked.length ? '; external resources are not fetched: ' + blocked.join(', ') : ''), clips: [] }
  throw e
}
window.__glb = null
const model = gltf.scene
let tris = 0
model.traverse(o => { if (o.isMesh) { o.castShadow = true; o.receiveShadow = false; o.frustumCulled = false
  const g = o.geometry; tris += (g.index ? g.index.count : g.attributes.position.count) / 3 } })
scene.add(model)
const clips = gltf.animations.map(c => ({ name: c.name, duration: +c.duration.toFixed(3) }))
let clip = null, mixer = null, action = null
if (gltf.animations.length && !P.listOnly) {
  clip = P.clip ? gltf.animations.find(c => c.name === P.clip) || gltf.animations.find(c => c.name.toLowerCase().includes(P.clip.toLowerCase())) : gltf.animations[0]
  if (!clip) { window.__info = { error: 'clip not found: ' + P.clip, clips }; throw new Error('clip') }
  mixer = new THREE.AnimationMixer(model); action = mixer.clipAction(clip); action.play(); mixer.setTime(0)
}
model.updateMatrixWorld(true)
// Frame on the rest/first pose: drop the model onto y=0 and centre it on x/z.
const box = new THREE.Box3().setFromObject(model)
const c0 = box.getCenter(new THREE.Vector3())
model.position.sub(new THREE.Vector3(c0.x, box.min.y, c0.z)); model.updateMatrixWorld(true)
box.setFromObject(model)
const sz = box.getSize(new THREE.Vector3())
const radius = Math.max(0.5 * Math.hypot(sz.x, sz.y, sz.z), 1e-3)
// Root-motion follow: track the skeleton root's horizontal drift so a walking clip stays framed.
let root = null
model.traverse(o => { if (!root && o.isSkinnedMesh && o.skeleton.bones.length) root = o.skeleton.bones[0] })
const rootPos = () => root ? root.getWorldPosition(new THREE.Vector3()) : new THREE.Vector3()
const root0 = rootPos()
const cam = new THREE.PerspectiveCamera(35, 1, radius / 100, radius * 100)
const fov = THREE.MathUtils.degToRad(cam.fov)
const dist = radius / Math.sin(fov / 2) * 1.08
const lookY = sz.y * 0.5
// Ground: soft shadow catcher + faint grid that fades at the edges.
const ground = new THREE.Mesh(new THREE.PlaneGeometry(radius * 40, radius * 40), new THREE.ShadowMaterial({ opacity: 0.28 }))
ground.rotation.x = -Math.PI / 2; ground.receiveShadow = true; scene.add(ground)
const grid = new THREE.GridHelper(radius * 8, 16, 0x6b7078, 0x868b93)
grid.material.transparent = true; grid.material.opacity = 0.22; grid.material.depthWrite = false; scene.add(grid)
scene.fog = new THREE.Fog(0xb8bcc3, dist * 1.4, dist * 3.2)
const sc = key.shadow.camera; sc.left = sc.bottom = -radius * 1.6; sc.right = sc.top = radius * 1.6; sc.near = radius * 0.1; sc.far = radius * 12
// 2D composite for the label.
const out = document.createElement('canvas'); out.width = out.height = S
const ctx = out.getContext('2d')
window.__info = { clips, clip: clip && clip.name, duration: clip ? clip.duration : 0, triangles: Math.round(tris), size: [sz.x, sz.y, sz.z].map(v => +v.toFixed(3)), blocked_urls: blocked }
window.renderFrame = (i) => {
  const t = i / P.fps, u = i / P.frames
  if (mixer) mixer.setTime(t % clip.duration)
  model.updateMatrixWorld(true)
  const r = rootPos().sub(root0); r.y = 0
  const az = P.az0 + (mixer ? u * Math.PI / 2 : u * Math.PI * 2)
  const el = THREE.MathUtils.degToRad(18)
  const target = new THREE.Vector3(r.x, lookY, r.z)
  cam.position.set(target.x + dist * Math.cos(el) * Math.sin(az), target.y + dist * Math.sin(el), target.z + dist * Math.cos(el) * Math.cos(az))
  cam.lookAt(target)
  grid.position.set(Math.round(r.x / (radius / 2)) * (radius / 2), 0.0005 * radius, Math.round(r.z / (radius / 2)) * (radius / 2))
  key.position.set(target.x + radius * 2.5, radius * 5, target.z + radius * 3); key.target.position.copy(target)
  renderer.render(scene, cam)
  ctx.drawImage(renderer.domElement, 0, 0)
  if (P.label) {
    const fs = Math.round(S * 0.026); ctx.font = '600 ' + fs + 'px sans-serif'
    const w = ctx.measureText(P.label).width
    ctx.fillStyle = 'rgba(20,22,26,0.55)'; ctx.fillRect(fs * 0.6, S - fs * 2.4, w + fs * 1.2, fs * 1.7)
    ctx.fillStyle = '#fff'; ctx.textBaseline = 'middle'; ctx.fillText(P.label, fs * 1.2, S - fs * 1.55)
  }
  return out.toDataURL('image/png')
}
window.setParams = (p) => Object.assign(P, p)
window.__ready = true
`

// The page never navigates or touches the network: it stays on about:blank, three.js modules
// are turned into blob: URLs behind an import map, the GLB bytes are pushed in over CDP, and the
// loader refuses external resource URLs a GLB may name (see setURLModifier in MAIN).
// (Some hosts' Chromium builds hang on any http/file navigation in headless mode.)
function moduleSources(threeDir) {
  const rd = r => fs.readFileSync(path.join(threeDir, r), 'utf8')
  const fix = src => src.replace(/from '\.\.\/utils\/BufferGeometryUtils\.js'/g, "from 'three/addons/utils/BufferGeometryUtils.js'")
  return {
    core: rd('build/three.core.js'),
    three: rd('build/three.module.js'),
    bgu: rd('examples/jsm/utils/BufferGeometryUtils.js'),
    gltf: fix(rd('examples/jsm/loaders/GLTFLoader.js')),
    room: rd('examples/jsm/environments/RoomEnvironment.js'),
  }
}

const BOOT = `(async (M, P) => {
  const url = s => URL.createObjectURL(new Blob([s], { type: 'text/javascript' }))
  const core = url(M.core)
  const imports = {
    'three': url(M.three.split("'./three.core.js'").join(JSON.stringify(core))),
    'three/addons/utils/BufferGeometryUtils.js': url(M.bgu),
    'three/addons/loaders/GLTFLoader.js': url(M.gltf),
    'three/addons/environments/RoomEnvironment.js': url(M.room),
  }
  const map = document.createElement('script'); map.type = 'importmap'
  map.textContent = JSON.stringify({ imports }); document.head.appendChild(map)
  document.body.style.cssText = 'margin:0;background:#000'
  window.__P = P
  return true
})`

async function cdpConnect(chromeBin, profileDir, size) {
  const args = ['--headless=new', '--no-sandbox', '--no-first-run', '--no-default-browser-check', '--disable-extensions',
    '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist', '--mute-audio',
    '--disable-background-timer-throttling', '--disable-renderer-backgrounding', '--hide-scrollbars',
    `--window-size=${size},${size}`, `--user-data-dir=${profileDir}`, '--remote-debugging-port=0', 'about:blank']
  // Chromium gets a private TMPDIR, removed with it (a SIGKILLed browser leaves its
  // org.chromium.* dirs behind). Kept short: Chromium puts its singleton unix socket there and
  // aborts (SIGTRAP) past the 108-byte sockaddr limit, as with deep per-profile scratch dirs.
  const env = { ...process.env }
  let shortTmp = null
  if (process.platform !== 'win32') {
    // <base>/meshy-XXXXXX/org.chromium.Chromium.XXXXXX/SingletonSocket must fit in 107 bytes.
    const base = Buffer.byteLength(os.tmpdir()) + '/meshy-XXXXXX/org.chromium.Chromium.XXXXXX/SingletonSocket'.length > 100 ? '/tmp' : os.tmpdir()
    env.TMPDIR = shortTmp = fs.mkdtempSync(path.join(base, 'meshy-'))
  }
  const proc = spawn(chromeBin, args, { stdio: ['ignore', 'ignore', 'pipe'], detached: false, env })
  // Never leave a browser behind, even on Ctrl-C / timeout.
  const reap = () => {
    try { proc.kill('SIGKILL') } catch {}
    if (shortTmp) try { fs.rmSync(shortTmp, { recursive: true, force: true }) } catch {}
  }
  process.on('exit', reap)
  for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP']) process.on(sig, () => { reap(); process.exit(130) })
  let stderr = ''
  proc.stderr.on('data', d => { stderr = (stderr + d).slice(-4000) })
  const portFile = path.join(profileDir, 'DevToolsActivePort')
  const t0 = Date.now()
  while (!fs.existsSync(portFile) || fs.readFileSync(portFile, 'utf8').split('\n').length < 2) {
    if (proc.exitCode !== null || Date.now() - t0 > 30000) { proc.kill('SIGKILL'); die('Chromium did not start', { chrome: chromeBin, stderr: stderr.slice(-800) }) }
    await new Promise(r => setTimeout(r, 100))
  }
  const port = fs.readFileSync(portFile, 'utf8').split('\n')[0].trim()
  dbg('chrome up, cdp port', port)
  let page
  for (let i = 0; i < 100 && !page; i++) {
    try { page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(t => t.type === 'page') } catch {}
    if (!page) await new Promise(r => setTimeout(r, 100))
  }
  if (!page) { proc.kill('SIGKILL'); die('no page target in Chromium') }
  dbg('page target', page.url)
  const ws = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((r, j) => { ws.onopen = r; ws.onerror = () => j(new Error('CDP websocket failed')) })
  let id = 0
  const pending = new Map()
  const pageErrors = []
  ws.onmessage = e => {
    const d = JSON.parse(e.data)
    if (d.id && pending.has(d.id)) { pending.get(d.id)(d); pending.delete(d.id); return }
    if (d.method === 'Runtime.exceptionThrown') pageErrors.push(d.params.exceptionDetails.exception?.description || d.params.exceptionDetails.text)
    else if (d.method === 'Log.entryAdded' && d.params.entry.level === 'error') pageErrors.push(`${d.params.entry.text} ${d.params.entry.url || ''}`.trim())
  }
  const send = (method, params = {}) => new Promise(r => { const my = ++id; pending.set(my, r); ws.send(JSON.stringify({ id: my, method, params })) })
  const evaluate = async (expression) => {
    const d = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
    if (d.error) throw new Error(d.error.message)
    if (d.result.exceptionDetails) throw new Error(d.result.exceptionDetails.exception?.description || d.result.exceptionDetails.text)
    return d.result.result.value
  }
  await send('Runtime.enable'); await send('Log.enable')
  const close = () => { try { ws.close() } catch {} ; reap() }
  return { evaluate, close, proc, stderr: () => stderr, pageErrors }
}

function ffprobeInfo(ffmpeg, file) {
  const probe = ffmpeg.replace(/ffmpeg(\.exe)?$/, 'ffprobe$1')
  if (!fs.existsSync(probe) && !which('ffprobe')) return null
  try {
    const j = JSON.parse(execFileSync(fs.existsSync(probe) ? probe : 'ffprobe', ['-v', 'error', '-show_entries',
      'stream=codec_name,width,height,r_frame_rate,pix_fmt:format=duration,size', '-of', 'json', file], { encoding: 'utf8' }))
    const s = j.streams[0]
    return { codec: s.codec_name, width: s.width, height: s.height, fps: s.r_frame_rate, pix_fmt: s.pix_fmt, duration: +(+j.format.duration).toFixed(2) }
  } catch { return null }
}

// The same file by path, symlink or hard link (stat follows links; ino+dev identify the inode).
const sameFile = (x, y) => {
  if (path.resolve(x) === path.resolve(y)) return true
  try { const s = fs.statSync(x), t = fs.statSync(y); return s.ino === t.ino && s.dev === t.dev } catch { return false }
}

async function main() {
  const a = parseArgs(process.argv.slice(2))
  if (!fs.existsSync(a.input)) die(`no such file: ${a.input}`)
  if (!/\.glb$/i.test(a.input)) die('input must be a .glb (download the GLB from Meshy first)')
  if (a.out && sameFile(a.out, a.input)) die(`refusing to overwrite the source file ${a.input}; pick another --out`)
  const chrome = findChrome(a.chrome)
  if (!chrome) die('no Chromium/Chrome found; install one (e.g. `npx -y playwright@1.56.1 install chromium-headless-shell`) or pass --chrome PATH')
  const ffmpeg = findFfmpeg()
  const threeDir = await findThree()
  const t0 = Date.now()
  const params = { size: a.size, fps: a.fps, frames: 1, az0: Math.PI / 5, clip: a.clip || null, listOnly: !!a.listClips, label: null }
  const work = fs.mkdtempSync(path.join(os.tmpdir(), 'meshy-preview-'))
  if (!a.keepFrames) process.on('exit', () => { try { fs.rmSync(work, { recursive: true, force: true }) } catch {} })
  const b = await cdpConnect(chrome, path.join(work, 'profile'), a.size)
  const cleanup = () => { b.close(); if (!a.keepFrames) fs.rmSync(work, { recursive: true, force: true }) }
  try {
    const tl = Date.now()
    await b.evaluate(`${BOOT}(${JSON.stringify(moduleSources(threeDir))}, ${JSON.stringify(params)})`)
    // GLB bytes in 8 MB base64 chunks (a 57 MB scan must not become one giant CDP message).
    const glb = fs.readFileSync(a.input)
    await b.evaluate(`window.__glb = new Uint8Array(${glb.length}); 0`)
    const CH = 8 * 1024 * 1024
    for (let o = 0; o < glb.length; o += CH) {
      const b64 = glb.subarray(o, Math.min(glb.length, o + CH)).toString('base64')
      await b.evaluate(`(() => { const s = atob(${JSON.stringify(b64)}), u = window.__glb, o = ${o}; for (let i = 0; i < s.length; i++) u[o + i] = s.charCodeAt(i); return 0 })()`)
    }
    await b.evaluate(`(() => { const s = document.createElement('script'); s.type = 'module'; s.src = URL.createObjectURL(new Blob([${JSON.stringify(MAIN)}], { type: 'text/javascript' })); document.body.appendChild(s); return 0 })()`)
    dbg('booted', ((Date.now() - tl) / 1000).toFixed(1), 's')
    let info
    for (;;) {
      info = await b.evaluate('window.__ready ? window.__info : (window.__info && window.__info.error ? window.__info : null)')
      if (info) break
      if (b.pageErrors.length) throw new Error(`page error: ${b.pageErrors.slice(0, 3).join(' | ').slice(0, 600)}`)
      if (Date.now() - tl > 300000) throw new Error('model load timed out (300 s)')
      await new Promise(r => setTimeout(r, 250))
    }
    if (info.error) throw new Error(`${info.error}; clips: ${info.clips.map(c => c.name).join(', ')}`)
    const blockedUrls = info.blocked_urls && info.blocked_urls.length ? { blocked_urls: info.blocked_urls } : {}
    if (a.listClips) { console.log(JSON.stringify({ ok: true, clips: info.clips, triangles: info.triangles, ...blockedUrls })); return }
    const loadS = (Date.now() - tl) / 1000
    // Duration: turntable 6 s; a clip loops whole cycles to reach >= 4 s, capped at 8 s.
    let seconds = a.seconds
    if (!seconds) {
      if (info.duration > 0) { const n = Math.max(1, Math.ceil(4 / info.duration)); seconds = Math.min(8, n * info.duration) }
      else seconds = 6
    }
    seconds = Math.max(1, Math.min(20, seconds))
    const frames = Math.round(seconds * a.fps)
    const stem = path.basename(a.input).replace(/\.glb$/i, '')
    const pretty = s => s.replace(/^Armature\|/, '').replace(/\|baselayer$/, '').replace(/[_|]+/g, ' ').trim()
    const label = a.noLabel ? null : (a.label || stem.replace(/[_-]+/g, ' ')) + (info.clip ? ` · ${pretty(info.clip)}` : '')
    await b.evaluate(`window.setParams(${JSON.stringify({ frames, label })})`)
    const framesDir = path.join(work, 'frames')
    fs.mkdirSync(framesDir)
    const tr = Date.now()
    for (let i = 0; i < frames; i++) {
      const url = await b.evaluate(`window.renderFrame(${i})`)
      fs.writeFileSync(path.join(framesDir, `f${String(i).padStart(5, '0')}.png`), Buffer.from(url.slice(url.indexOf(',') + 1), 'base64'))
    }
    const renderS = (Date.now() - tr) / 1000
    b.close()
    // Clip and file names come from the GLB: reduce them to a plain slug so a name like "../../x" can't
    // steer the default output out of the videos folder (ffmpeg runs with -y).
    const slug = s => s.toLowerCase().replace(/[^a-z0-9._-]+/g, '_').replace(/^[._]+/, '').slice(0, 80) || 'model'
    const videos = path.join(hermesHome(), 'cache', 'videos', 'meshy')
    // The clip's position keeps names apart when two clip names slug alike (non-Latin names, "Walk"/"walk").
    const clipPart = info.clip ? `clip${Math.max(0, info.clips.findIndex(c => c.name === info.clip)) + 1}-${slug(pretty(info.clip))}` : 'turntable'
    const outDefault = path.join(videos, `${slug(stem)}-${clipPart}.mp4`)
    if (!a.out && path.dirname(path.resolve(outDefault)) !== path.resolve(videos)) {
      throw new Error(`refusing to write outside ${videos}`)
    }
    const out = path.resolve(a.out || outDefault)
    fs.mkdirSync(path.dirname(out), { recursive: true })
    if (sameFile(out, a.input)) throw new Error(`refusing to overwrite the source file ${a.input}; pick another --out`)
    if (!ffmpeg) {
      const keep = out.replace(/\.mp4$/i, '') + '_frames'
      fs.rmSync(keep, { recursive: true, force: true }); fs.cpSync(framesDir, keep, { recursive: true })
      console.log(JSON.stringify({ ok: false, error: 'ffmpeg not found: wrote PNG frames instead', frames_dir: keep, frames, fps: a.fps }))
      return
    }
    // Encode into a private file we create beside the target, then rename it into place: ffmpeg -y
    // never writes through an existing output path (which could be a hard link to the source).
    const tmp = path.join(path.dirname(out), `.${path.basename(out, path.extname(out))}.${process.pid}-${createHash('sha1').update(String(Math.random())).digest('hex').slice(0, 8)}.part.mp4`)
    fs.closeSync(fs.openSync(tmp, 'wx'))
    process.on('exit', () => { try { fs.rmSync(tmp, { force: true }) } catch {} })
    const enc = (extra) => execFileSync(ffmpeg, ['-y', '-loglevel', 'error', '-framerate', String(a.fps), '-i', path.join(framesDir, 'f%05d.png'),
      '-an', '-c:v', 'libx264', '-preset', 'medium', '-pix_fmt', 'yuv420p', '-profile:v', 'high', '-movflags', '+faststart', ...extra, tmp])
    const cap = a.maxMb * 1024 * 1024
    let mode = 'crf20'
    enc(['-crf', '20'])
    if (fs.statSync(tmp).size > cap) {
      const kbps = Math.floor(cap * 8 * 0.9 / seconds / 1000)
      mode = `${kbps}k`
      enc(['-b:v', `${kbps}k`, '-maxrate', `${kbps}k`, '-bufsize', `${kbps * 2}k`])
    }
    fs.renameSync(tmp, out)
    const bytes = fs.statSync(out).size
    console.log(JSON.stringify({ ok: true, out, bytes, mb: +(bytes / 1048576).toFixed(2), seconds: +seconds.toFixed(2), fps: a.fps, frames, size: a.size,
      clip: info.clip, clips: info.clips.map(c => c.name), triangles: info.triangles, encode: mode,
      load_s: +loadS.toFixed(1), render_s: +renderS.toFixed(1), total_s: +((Date.now() - t0) / 1000).toFixed(1),
      probe: ffprobeInfo(ffmpeg, out), chrome, ffmpeg, three: threeDir, ...blockedUrls }))
  } catch (e) {
    die(String(e.message || e), { chrome_stderr: b.stderr().slice(-600) })
  } finally { cleanup() }
}

main()
