/**
 * Bytes of one saved model → a three.js object, with three.js's own loader for each format Meshy
 * exports (glTF/GLB, FBX, OBJ + .mtl, STL, USDZ, 3MF). Kept apart from the WebGL stage so a plain page
 * (or test harness) can parse without a renderer.
 *
 * The viewer gets bytes from the plugin backend, never a URL, so nothing may load relative to the app
 * page: every loader runs through a LoadingManager that maps a companion file name (an OBJ's .mtl and
 * texture) to the data URL the caller fetched, keeps the data:/blob: URLs loaders make for embedded
 * textures, and turns anything else into an empty data URL (a missing texture, not a request).
 */

import {
  type AnimationClip,
  Group,
  LoadingManager,
  Mesh,
  MeshStandardMaterial,
  type Object3D
} from 'three'
import { FBXLoader } from 'three/examples/jsm/loaders/FBXLoader.js'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import { MTLLoader } from 'three/examples/jsm/loaders/MTLLoader.js'
import { OBJLoader } from 'three/examples/jsm/loaders/OBJLoader.js'
import { STLLoader } from 'three/examples/jsm/loaders/STLLoader.js'
import { ThreeMFLoader } from 'three/examples/jsm/loaders/3MFLoader.js'
import { USDLoader } from 'three/examples/jsm/loaders/USDLoader.js'

import { formatOf, isZUp, mtlLibOf } from './formats.ts'

export interface ParseOptions {
  /** File name (or path): its extension picks the loader. Missing → glTF/GLB. */
  name?: string
  /** Companion files by the name the model uses for them (`robot.mtl`, `robot_texture.png`) → data URL. */
  siblings?: Readonly<Record<string, string>>
}

export interface ParsedModel {
  root: Object3D
  clips: AnimationClip[]
}

const EMPTY = 'data:,'

function companionManager(siblings: Readonly<Record<string, string>>): LoadingManager {
  const manager = new LoadingManager()
  manager.setURLModifier(url => {
    if (/^(data|blob):/i.test(url)) {
      return url
    }
    const clean = url.replace(/\\/g, '/').replace(/^\.\//, '')
    return siblings[clean] ?? siblings[clean.split('/').pop() ?? ''] ?? EMPTY
  })
  return manager
}

function dataUrlText(dataUrl: string): string {
  const comma = dataUrl.indexOf(',')
  const body = dataUrl.slice(comma + 1)
  if (!/;base64$/i.test(dataUrl.slice(0, comma))) {
    return decodeURIComponent(body)
  }
  const bin = atob(body)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i += 1) {
    out[i] = bin.charCodeAt(i)
  }
  return new TextDecoder().decode(out)
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

/** USDZ archives are stored uncompressed (USDZ spec) with the root layer first, so a text layer's header,
 *  where `upAxis` lives, is readable in the first bytes. three.js's USD reader ignores it; Blender writes Z. */
function usdzIsZUp(bytes: ArrayBuffer): boolean {
  const head = new TextDecoder('latin1').decode(new Uint8Array(bytes, 0, Math.min(bytes.byteLength, 8192)))
  return /\bupAxis\s*=\s*"Z"/.test(head)
}

const countMeshes = (root: Object3D) => {
  let n = 0
  root.traverse(node => {
    if ((node as Mesh).isMesh) {
      n += 1
    }
  })
  return n
}

export async function parseModel(bytes: ArrayBuffer, options: ParseOptions = {}): Promise<ParsedModel> {
  const format = formatOf(options.name ?? '') || 'glb'
  const siblings = options.siblings ?? {}
  const manager = companionManager(siblings)
  let parsed: ParsedModel
  switch (format) {
    case 'glb':
    case 'gltf': {
      if (externalResources(bytes).length) {
        // The viewer receives one file's bytes from the backend, so sibling .bin/texture files cannot load (they
        // would resolve against the app page). Say so instead of failing with a fetch error.
        throw new Error('This .gltf references external files. Open the .glb, or a .gltf with embedded data.')
      }
      const gltf = await new GLTFLoader().parseAsync(bytes, '')
      parsed = { root: gltf.scene, clips: gltf.animations ?? [] }
      break
    }
    case 'fbx': {
      const group = new FBXLoader(manager).parse(bytes, '')
      parsed = { root: group, clips: group.animations ?? [] }
      break
    }
    case 'obj': {
      const text = new TextDecoder().decode(bytes)
      const loader = new OBJLoader(manager)
      const lib = mtlLibOf(text.slice(0, 65_536))
      const mtl = lib ? siblings[lib] : undefined
      if (mtl) {
        const materials = new MTLLoader(manager).parse(dataUrlText(mtl), '')
        materials.preload()
        loader.setMaterials(materials)
      }
      parsed = { root: loader.parse(text), clips: [] }
      break
    }
    case 'stl': {
      const geometry = new STLLoader().parse(bytes)
      const colored = (geometry as { hasColors?: boolean }).hasColors === true
      const material = new MeshStandardMaterial({ color: colored ? 0xffffff : 0xb9bec7, roughness: 0.6, metalness: 0.05, vertexColors: colored })
      parsed = { root: new Mesh(geometry, material), clips: [] }
      break
    }
    case 'usdz': {
      parsed = { root: new USDLoader(manager).parse(bytes), clips: [] }
      if (!countMeshes(parsed.root)) {
        // three.js reads USDZ with text (USDA) layers; its binary USDC reader is still a stub.
        throw new Error('This USDZ stores its mesh as binary USD, which the viewer cannot draw yet. Open the GLB instead.')
      }
      break
    }
    case '3mf': {
      parsed = { root: new ThreeMFLoader(manager).parse(bytes), clips: [] }
      break
    }
    default:
      throw new Error(`No 3D preview for .${format} files`)
  }
  if (!countMeshes(parsed.root)) {
    throw new Error(`This .${format} file has no mesh the viewer can draw`)
  }
  if (isZUp(format) || (format === 'usdz' && usdzIsZUp(bytes))) {
    // Printing formats are Z-up; stand the model up in three.js's Y-up world.
    const upright = new Group()
    parsed.root.rotation.x = -Math.PI / 2
    upright.add(parsed.root)
    parsed = { root: upright, clips: parsed.clips }
  }
  return parsed
}
