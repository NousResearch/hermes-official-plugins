/**
 * "View a model file…" against a REMOTE backend: the file the user picks is on this computer, which the
 * backend cannot open by path. Send its bytes instead; the backend stores them in the plugin's data dir
 * and answers with a path it can serve. Pure (no SDK) so node tests drive it with a fake `rest`.
 *
 * Newer Desktops carry a file as multipart (`ctx.rest(path, { upload })`). Older ones drop that option and
 * send an empty request (the backend answers "no file in the request"), and OAuth-gated remotes refuse
 * multipart outright; both fall back to a JSON body with the bytes base64-encoded, size-capped.
 */

/** Same cap as the backend's /file (the viewer could not show a larger model anyway). */
export const UPLOAD_MAX_BYTES = 128 * 1024 * 1024
/** A base64 JSON body is a third larger and is copied a few times on its way; keep the fallback smaller. */
export const JSON_UPLOAD_MAX_BYTES = 48 * 1024 * 1024

export interface UploadedModel {
  path: string
  name: string
  bytes: number
}

interface UploadRestOptions {
  method?: string
  body?: unknown
  upload?: { filename: string; contentType?: string; bytes: ArrayBuffer }
  timeoutMs?: number
}
type UploadRest = <T>(path: string, opts?: UploadRestOptions) => Promise<T>

const fmtMb = (n: number) => `${Math.round(n / 1048576)} MB`

export function bytesToBase64(bytes: ArrayBuffer): string {
  const view = new Uint8Array(bytes)
  let bin = ''
  for (let i = 0; i < view.length; i += 0x8000) {
    bin += String.fromCharCode(...view.subarray(i, i + 0x8000))
  }
  return btoa(bin)
}

/** The request reached the backend without the file (older Desktop) or the transport refused multipart. */
export function uploadUnsupported(err: unknown): boolean {
  const text = err instanceof Error ? err.message : String(err)
  return /no file in the request|uploads are not supported/i.test(text)
}

export async function uploadModel(rest: UploadRest, file: { name: string; bytes: ArrayBuffer; type?: string }): Promise<UploadedModel> {
  const size = file.bytes.byteLength
  if (size > UPLOAD_MAX_BYTES) {
    throw new Error(`${file.name} is ${fmtMb(size)}; the viewer opens models up to ${fmtMb(UPLOAD_MAX_BYTES)}`)
  }
  const timeoutMs = 300_000
  try {
    return await rest<UploadedModel>('upload', {
      method: 'POST',
      upload: { filename: file.name, contentType: file.type || 'application/octet-stream', bytes: file.bytes },
      timeoutMs
    })
  } catch (err) {
    if (!uploadUnsupported(err)) {
      throw err
    }
  }
  if (size > JSON_UPLOAD_MAX_BYTES) {
    throw new Error(`Update Hermes Desktop to send models over ${fmtMb(JSON_UPLOAD_MAX_BYTES)} to a remote Hermes`)
  }
  return rest<UploadedModel>('upload', { method: 'POST', body: { filename: file.name, data: bytesToBase64(file.bytes) }, timeoutMs })
}
