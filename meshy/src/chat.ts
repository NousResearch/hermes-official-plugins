/**
 * Where a card or gallery button sends its chat message. Pure (the host is passed in) so node tests can drive it.
 *
 * - A transcript card sends to the chat whose transcript shows it: Desktop passes that chat's id to directive
 *   renders as `sessionId` (null while the chat has no session yet). The id is captured when the card renders,
 *   so a button that does async work first still lands in the card's chat even if the user switched chats
 *   meanwhile. Hosts that predate the field pass nothing (undefined) and keep the old call, `submit(null, …)`:
 *   the composer the user is typing in.
 * - The gallery is not inside any chat, so it sends to the chat that made the selected model (the session id
 *   recorded for "Made from"), opening that chat first when no composer for it is on screen. With no recorded chat,
 *   or a host without `openSession`, it keeps the old call.
 *
 * Both resolve false when nothing took the text; the caller shows the warning toast.
 */

export interface ComposerLike {
  submit?: (sessionId: null | string, text: string) => boolean
  insertText?: (sessionId: null | string, text: string, opts?: { mode?: 'block' | 'inline' | 'prefix' }) => Promise<boolean>
}

export interface ChatHost {
  composer?: ComposerLike
  openSession?: (storedSessionId: string) => Promise<void>
}

export interface ChatSender {
  /** Send `text` as a user message. */
  submit: (text: string) => Promise<boolean>
  /** Put `text` in the composer as a new paragraph, unsent. */
  insert: (text: string) => Promise<boolean>
}

const submitTo = (host: ChatHost, id: null | string, text: string): boolean =>
  typeof host.composer?.submit === 'function' && Boolean(host.composer.submit(id, text))

const insertTo = async (host: ChatHost, id: null | string, text: string): Promise<boolean> =>
  typeof host.composer?.insertText === 'function' && Boolean(await host.composer.insertText(id, text, { mode: 'block' }))

/** Sender for a transcript card; `sessionId` is the directive render prop (undefined on hosts without it). */
export function cardChat(host: ChatHost, sessionId: null | string | undefined): ChatSender {
  const id = sessionId ?? null
  return {
    submit: text => Promise.resolve(submitTo(host, id, text)),
    insert: text => insertTo(host, id, text)
  }
}

/** After `openSession` resolves the chat's composer can take a moment to mount and claim its address. */
export const OPEN_RETRY_MS = 150
export const OPEN_RETRIES = 20

const sleep = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms))

/** Sender for the gallery: `resolveSession` names the chat recorded for the selected model (null = none). */
export function recordedChat(
  host: ChatHost,
  resolveSession: () => Promise<null | string | undefined>,
  wait: (ms: number) => Promise<void> = sleep
): ChatSender {
  const deliver = async (verb: keyof ComposerLike, send: (id: null | string) => Promise<boolean>): Promise<boolean> => {
    if (typeof host.composer?.[verb] !== 'function') {
      return false // never open a chat for a verb this host cannot run
    }
    let id: null | string | undefined = null
    try {
      id = await resolveSession()
    } catch {
      id = null
    }
    if (!id || typeof host.openSession !== 'function') {
      return send(null)
    }
    if (await send(id)) {
      return true
    }
    try {
      await host.openSession(id)
    } catch {
      return false
    }
    for (let i = 0; i < OPEN_RETRIES; i++) {
      if (await send(id)) {
        return true
      }
      await wait(OPEN_RETRY_MS)
    }
    return false
  }
  return {
    submit: text => deliver('submit', async id => submitTo(host, id, text)),
    insert: text => deliver('insertText', id => insertTo(host, id, text))
  }
}
