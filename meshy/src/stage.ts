/**
 * The three.js stage: owns one WebGLRenderer, a camera with orbit controls, studio lighting, and
 * whichever GLB is loaded. Plain DOM + three, no React: the React side mounts a <div>, hands it to
 * `createStage`, and calls `dispose()` on unmount, so a scrolled-away transcript card frees its GPU
 * context instead of leaking one per message (Chromium caps live WebGL contexts at ~16).
 */

import {
  ACESFilmicToneMapping,
  AmbientLight,
  type AnimationAction,
  type AnimationClip,
  AnimationMixer,
  Box3,
  LoopOnce,
  LoopRepeat,
  Clock,
  Color,
  DirectionalLight,
  GridHelper,
  type Material,
  type Mesh,
  type Object3D,
  PerspectiveCamera,
  PMREMGenerator,
  Scene,
  SRGBColorSpace,
  Vector3,
  WebGLRenderer
} from 'three'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js'

export interface ModelStats {
  triangles: number
  vertices: number
  meshes: number
  materials: number
  sizeMeters: [number, number, number]
  /** Animation clip names, in file order (rigged/animated Meshy GLBs carry walk, run, ...). */
  animations: string[]
}

export interface Stage {
  load: (bytes: ArrayBuffer) => Promise<ModelStats>
  setAutoRotate: (on: boolean) => void
  setWireframe: (on: boolean) => void
  setGrid: (on: boolean) => void
  resetView: () => void
  /** Clip names of the loaded model (empty for a static mesh). */
  listClips: () => string[]
  /** Switch to a clip by name or index; it loops. Returns the index now playing, or -1. */
  playClip: (clip: number | string) => number
  /** Pause or resume the current clip (keeps its pose while paused). */
  setPlaying: (on: boolean) => void
  /** Loop the clip (default) or play it once and hold the last pose. */
  setLoop: (on: boolean) => void
  /** Jump the current clip to `seconds` (clamped); keeps the paused/playing state. */
  seek: (seconds: number) => void
  /** Where the current clip is: time and duration in seconds; `playing` turns false when a play-once clip ends. */
  clipState: () => { time: number; duration: number; playing: boolean }
  snapshot: () => string
  dispose: () => void
}

export function createStage(host: HTMLElement, background: string): Stage {
  const renderer = new WebGLRenderer({ antialias: true, alpha: false, preserveDrawingBuffer: true })
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
  renderer.outputColorSpace = SRGBColorSpace
  renderer.toneMapping = ACESFilmicToneMapping
  renderer.domElement.style.display = 'block'
  renderer.domElement.style.width = '100%'
  renderer.domElement.style.height = '100%'
  renderer.domElement.style.outline = 'none'
  host.appendChild(renderer.domElement)

  const scene = new Scene()
  scene.background = new Color(background)
  const pmrem = new PMREMGenerator(renderer)
  const envTexture = pmrem.fromScene(new RoomEnvironment(), 0.04).texture
  scene.environment = envTexture
  scene.add(new AmbientLight(0xffffff, 0.25))
  const key = new DirectionalLight(0xffffff, 1.6)
  key.position.set(3, 5, 4)
  scene.add(key)

  const camera = new PerspectiveCamera(40, 1, 0.01, 1000)
  camera.position.set(2, 1.5, 2.5)
  const controls = new OrbitControls(camera, renderer.domElement)
  controls.enableDamping = true
  controls.autoRotateSpeed = 1.2

  const grid = new GridHelper(4, 16, 0x888888, 0x444444)
  ;(grid.material as Material).transparent = true
  ;(grid.material as Material).opacity = 0.35
  scene.add(grid)

  let model: Object3D | null = null
  let clips: AnimationClip[] = []
  let mixer: AnimationMixer | null = null
  let action: AnimationAction | null = null
  let playing = false
  let loop = true
  const clock = new Clock()
  let dirty = true
  let home = { pos: camera.position.clone(), target: new Vector3() }
  let frame = 0
  let disposed = false

  const resize = () => {
    const w = Math.max(1, host.clientWidth)
    const h = Math.max(1, host.clientHeight)
    renderer.setSize(w, h, false)
    camera.aspect = w / h
    camera.updateProjectionMatrix()
    dirty = true
  }
  const observer = new ResizeObserver(resize)
  observer.observe(host)
  resize()

  // Render on demand: a frame is drawn only when the camera moved (orbit, damping, auto-rotate) or
  // something visible changed, and never while the viewport is off-screen or the window hidden. A
  // dense Meshy mesh is ~1M triangles; drawing it 60x/s per idle card starves the renderer.
  let onScreen = true
  const invalidate = () => {
    dirty = true
  }
  const visibility = new IntersectionObserver(entries => {
    onScreen = entries.some(e => e.isIntersecting)
    dirty = true
  })
  visibility.observe(host)
  controls.addEventListener('change', invalidate)

  const tick = () => {
    if (disposed) {
      return
    }
    frame = requestAnimationFrame(tick)
    if (!onScreen || document.hidden) {
      return
    }
    const moved = controls.update()
    // A playing clip needs every frame; the clock is read each tick so a resume after pause or an
    // off-screen stretch does not jump the pose forward by the time spent away.
    const delta = clock.getDelta()
    const animating = playing && mixer !== null && action !== null
    if (animating) {
      mixer!.update(Math.min(delta, 0.1))
    }
    if (moved || dirty || animating) {
      dirty = false
      renderer.render(scene, camera)
    }
  }
  tick()

  const disposeModel = () => {
    if (!model) {
      return
    }
    mixer?.stopAllAction()
    if (mixer) {
      mixer.uncacheRoot(model)
    }
    mixer = null
    action = null
    clips = []
    playing = false
    scene.remove(model)
    model.traverse(node => {
      const mesh = node as Mesh
      if (!mesh.isMesh) {
        return
      }
      mesh.geometry?.dispose()
      for (const mat of Array.isArray(mesh.material) ? mesh.material : [mesh.material]) {
        for (const value of Object.values(mat ?? {})) {
          if (value && typeof value === 'object' && 'isTexture' in value) {
            ;(value as { dispose: () => void }).dispose()
          }
        }
        mat?.dispose()
      }
    })
    model = null
  }

  const frameModel = (root: Object3D) => {
    const box = new Box3().setFromObject(root)
    const size = box.getSize(new Vector3())
    const center = box.getCenter(new Vector3())
    // Sit the model on the grid, centred; scale the grid to the model's footprint.
    root.position.sub(new Vector3(center.x, box.min.y, center.z))
    const radius = Math.max(size.x, size.y, size.z) || 1
    grid.scale.setScalar(radius / 2)
    const dist = radius / (2 * Math.tan((camera.fov * Math.PI) / 360)) * 1.35
    const target = new Vector3(0, size.y / 2, 0)
    camera.near = radius / 100
    camera.far = radius * 100
    camera.position.copy(target).add(new Vector3(0.75, 0.45, 1).normalize().multiplyScalar(dist))
    camera.updateProjectionMatrix()
    controls.target.copy(target)
    controls.update()
    home = { pos: camera.position.clone(), target: target.clone() }
    return size
  }

  const playClipAt = (index: number): number => {
    if (!mixer || index < 0 || index >= clips.length) {
      return -1
    }
    const next = mixer.clipAction(clips[index])
    if (next !== action) {
      action?.fadeOut(0.25)
      next.setLoop(loop ? LoopRepeat : LoopOnce, Infinity)
      next.clampWhenFinished = true
      next.reset().fadeIn(action ? 0.25 : 0).play()
      action = next
    }
    if (!loop && action.time >= action.getClip().duration) {
      action.reset().play()
    }
    action.paused = false
    playing = true
    clock.getDelta()
    dirty = true
    return index
  }

  return {
    async load(bytes) {
      if (externalResources(bytes).length) {
        // The viewer receives one file's bytes from the backend, so sibling .bin/texture files cannot load (they
        // would resolve against the app page). Say so instead of failing with a fetch error.
        throw new Error('This .gltf references external files. Open the .glb, or a .gltf with embedded data.')
      }
      const gltf = await new GLTFLoader().parseAsync(bytes, '')
      if (disposed) {
        throw new Error('viewer closed')
      }
      disposeModel()
      model = gltf.scene
      scene.add(model)
      clips = gltf.animations ?? []
      if (clips.length) {
        mixer = new AnimationMixer(model)
        // Play-once: hold the last pose and let the UI flip back to "paused".
        // Only the CURRENT clip ends playback: a clip fading out after a switch also fires `finished`.
        mixer.addEventListener('finished', (e: { action?: unknown }) => {
          if (e.action !== action) {
            return
          }
          playing = false
          dirty = true
        })
        // Frame on the first clip's first pose: a rig's bind pose can differ from its animated stance.
        playClipAt(0)
        mixer.update(0)
      }
      const size = frameModel(model)
      dirty = true
      clock.getDelta()
      const stats: ModelStats = {
        triangles: 0,
        vertices: 0,
        meshes: 0,
        materials: 0,
        sizeMeters: [size.x, size.y, size.z],
        animations: clips.map((c, i) => c.name || `Clip ${i + 1}`)
      }
      const mats = new Set<string>()
      model.traverse(node => {
        const mesh = node as Mesh
        if (!mesh.isMesh) {
          return
        }
        stats.meshes += 1
        const pos = mesh.geometry.getAttribute('position')
        stats.vertices += pos?.count ?? 0
        stats.triangles += mesh.geometry.index ? mesh.geometry.index.count / 3 : (pos?.count ?? 0) / 3
        for (const m of Array.isArray(mesh.material) ? mesh.material : [mesh.material]) {
          mats.add(m.uuid)
        }
      })
      stats.materials = mats.size
      return stats
    },
    setAutoRotate(on) {
      controls.autoRotate = on
      dirty = true
    },
    setWireframe(on) {
      model?.traverse(node => {
        const mesh = node as Mesh
        if (mesh.isMesh) {
          for (const m of Array.isArray(mesh.material) ? mesh.material : [mesh.material]) {
            ;(m as Material & { wireframe?: boolean }).wireframe = on
          }
        }
      })
      dirty = true
    },
    setGrid(on) {
      grid.visible = on
      dirty = true
    },
    resetView() {
      camera.position.copy(home.pos)
      controls.target.copy(home.target)
      controls.update()
      dirty = true
    },
    listClips() {
      return clips.map((c, i) => c.name || `Clip ${i + 1}`)
    },
    playClip(clip) {
      const index = typeof clip === 'number' ? clip : clips.findIndex((c, i) => (c.name || `Clip ${i + 1}`) === clip)
      return playClipAt(index)
    },
    setPlaying(on) {
      if (!action) {
        return
      }
      if (on && !loop && action.time >= action.getClip().duration - 1e-3) {
        action.reset().play()  // a finished play-once clip restarts from the top
      }
      playing = on
      action.paused = !on
      action.enabled = true
      clock.getDelta()
      dirty = true
    },
    setLoop(on) {
      loop = on
      if (action) {
        action.setLoop(on ? LoopRepeat : LoopOnce, Infinity)
        action.clampWhenFinished = true
      }
    },
    seek(seconds) {
      if (!action || !mixer) {
        return
      }
      const duration = action.getClip().duration
      // Stop everything (ends a crossfade; stop() rewinds), then place this clip at full weight.
      mixer.stopAllAction()
      action.play()
      action.time = Math.max(0, Math.min(duration, seconds))
      action.setEffectiveWeight(1)
      action.paused = !playing
      mixer.update(0)
      dirty = true
    },
    clipState() {
      return action
        ? { time: action.time, duration: action.getClip().duration, playing }
        : { time: 0, duration: 0, playing: false }
    },
    snapshot() {
      renderer.render(scene, camera)
      return renderer.domElement.toDataURL('image/png')
    },
    dispose() {
      disposed = true
      cancelAnimationFrame(frame)
      observer.disconnect()
      visibility.disconnect()
      controls.removeEventListener('change', invalidate)
      controls.dispose()
      disposeModel()
      envTexture.dispose()
      pmrem.dispose()
      renderer.dispose()
      renderer.forceContextLoss()
      renderer.domElement.remove()
    }
  }
}

export function dataUrlToBuffer(dataUrl: string): ArrayBuffer {
  const comma = dataUrl.indexOf(',')
  const bin = atob(dataUrl.slice(comma + 1))
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i += 1) {
    out[i] = bin.charCodeAt(i)
  }
  return out.buffer
}

/** URIs of buffers/images a glTF (JSON or GLB) loads from outside its own bytes; `data:` URIs are embedded. */
export function externalResources(bytes: ArrayBuffer): string[] {
  let json: unknown
  try {
    const view = new DataView(bytes)
    if (bytes.byteLength >= 20 && view.getUint32(0, true) === 0x46546c67) {
      // GLB: 12-byte header, then the JSON chunk (length, type 'JSON', data).
      const length = view.getUint32(12, true)
      json = JSON.parse(new TextDecoder().decode(new Uint8Array(bytes, 20, Math.min(length, bytes.byteLength - 20))))
    } else {
      json = JSON.parse(new TextDecoder().decode(bytes))
    }
  } catch {
    return [] // not parseable here: let the loader report it
  }
  const doc = json as { buffers?: Array<{ uri?: unknown }>; images?: Array<{ uri?: unknown }> }
  return [...(Array.isArray(doc?.buffers) ? doc.buffers : []), ...(Array.isArray(doc?.images) ? doc.images : [])]
    .map(entry => entry?.uri)
    .filter((uri): uri is string => typeof uri === 'string' && !/^data:/i.test(uri))
}
