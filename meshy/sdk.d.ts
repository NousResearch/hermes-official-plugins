// Minimal ambient types for the parts of `@hermes/plugin-sdk` this plugin uses (typecheck only;
// the host supplies the real module at runtime). Shapes mirror apps/desktop/src/sdk + contrib/plugin.ts.
import type { ComponentProps, CSSProperties, ReactNode } from 'react'

export interface PluginRestOptions {
  method?: string
  body?: unknown
  /** Single-file multipart upload (newer Desktops; older ones drop it and send no body). */
  upload?: { filename: string; contentType?: string; bytes: ArrayBuffer }
  timeoutMs?: number
}
export interface PluginFileDialogOptions {
  defaultPath?: string
  filters?: Array<{ extensions: string[]; name: string }>
  title?: string
}
export interface PluginOs {
  openExternal: (url: string) => Promise<boolean>
  revealPath: (path: string) => Promise<boolean>
  pickOpenPath: (options?: PluginFileDialogOptions) => Promise<null | string>
  /** Newer Desktops only: feature-detect. */
  writeClipboard?: (text: string) => Promise<boolean>
}
export interface GatewayEvent {
  type: string
  payload?: unknown
  session_id?: string
  /** Renderer-side tags (apps/shared gateway-events.ts): the profile/connection in front when it arrived. */
  profile?: string
  connectionId?: string
}
export interface ReadableAtom<T> {
  get: () => T
  subscribe: (fn: (value: T) => void) => () => void
}
export interface PluginContribution {
  id: string
  area: string
  order?: number
  data?: unknown
  render?: () => ReactNode
}
export interface PluginContext {
  readonly source: string
  registerMany: (cs: PluginContribution[]) => () => void
  onDispose: (fn: () => void) => void
  onEvent: (type: string, listener: (event: GatewayEvent) => void) => () => void
  rest: <T>(path: string, opts?: PluginRestOptions) => Promise<T>
  os: PluginOs
}
export interface HermesPlugin {
  id: string
  name: string
  description?: string
  defaultEnabled?: boolean
  register: (ctx: PluginContext) => void
}
/** Props a TRANSCRIPT_DIRECTIVE_AREA `render` receives (apps/desktop/src/lib/transcript-directives.ts). */
export interface TranscriptDirectiveProps {
  attrs: Readonly<Record<string, string>>
  source: string
  streaming: boolean
  /** The chat whose transcript shows the directive (stored id, else runtime id; null before it has a session).
   *  Newer Desktops only: undefined on older ones. */
  sessionId?: null | string
}
export interface PaletteContribution {
  id: string
  label: string
  keywords?: string[]
  run: () => void
}

export const PALETTE_AREA: string
export const STATUSBAR_AREAS: { left: string; right: string }
export const TRANSCRIPT_DIRECTIVE_AREA: string
export const host: {
  /** Composer bus (apps/desktop/src/sdk/composer.ts): `null` = the composer the user is typing in. */
  composer?: {
    submit: (sessionId: null | string, text: string) => boolean
    insertText?: (sessionId: null | string, text: string, opts?: { mode?: 'block' | 'inline' | 'prefix' }) => Promise<boolean>
  }
  /** Read-only app state; ctx.rest goes to the backend of `profile` on `connectionId`. */
  state?: { profile?: ReadableAtom<string>; connectionId?: ReadableAtom<null | string> }
  /** Registered connections (newer Desktops): `kind` tells a backend on this computer from a remote one. */
  connections?: () => Promise<Array<{ id: string; kind?: string; label?: string }>>
  openSession?: (storedSessionId: string, options?: { intent?: 'in-place' | 'stack' | 'tab' | 'window' }) => Promise<void>
  navigate: (path: string) => void
  notify: (input: { kind: 'error' | 'info' | 'success' | 'warning'; message: string }) => string
  openWorkspace?: (
    id: string,
    options: {
      /** Where the tile docks (newer Desktops; older ones ignore it and use their default). */
      dock?: { before?: null | string; pane: string; pos: 'bottom' | 'center' | 'left' | 'right' | 'top' }
      minWidth?: string
      onClose?: () => void
      render: () => ReactNode
      title?: string
    }
  ) => () => void
}
export function Button(
  props: ComponentProps<'button'> & { size?: 'default' | 'icon' | 'sm'; variant?: 'default' | 'ghost' | 'outline' }
): ReactNode
export function Codicon(props: { name: string; size?: number; spinning?: boolean; style?: CSSProperties }): ReactNode
// Radix-backed menu primitives the Desktop SDK re-exports (components/ui/dropdown-menu).
export function DropdownMenu(props: { children?: ReactNode; open?: boolean; onOpenChange?: (open: boolean) => void }): ReactNode
export function DropdownMenuTrigger(props: { asChild?: boolean; children?: ReactNode }): ReactNode
export function DropdownMenuContent(props: { align?: 'center' | 'end' | 'start'; children?: ReactNode; style?: CSSProperties }): ReactNode
export function DropdownMenuItem(
  props: { children?: ReactNode; disabled?: boolean; onSelect?: (event: Event) => void; title?: string } & Record<`data-${string}`, string>
): ReactNode
export function DropdownMenuSeparator(): ReactNode
