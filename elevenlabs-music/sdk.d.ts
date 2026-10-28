// Minimal ambient types for the parts of `@hermes/plugin-sdk` this plugin uses (typecheck only; the host
// supplies the real module at runtime). Shapes mirror apps/desktop/src/sdk + contrib/plugin.ts. Every member
// newer than the oldest supported Desktop is optional and feature-detected at the call site.
import type { ComponentProps, CSSProperties, ReactNode } from 'react'

export interface PluginRestOptions {
  method?: string
  body?: unknown
  timeoutMs?: number
}
export interface PluginOs {
  revealPath: (path: string) => Promise<boolean>
  writeClipboard?: (text: string) => Promise<boolean>
}
export interface GatewayEvent {
  type: string
  payload?: unknown
  session_id?: string
  profile?: string
  connectionId?: string
  /** Client-local: recovered/held during a reconnect, not fresh work. */
  replayed?: boolean
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
  register: (ctx: PluginContext) => void
}
export interface PaletteContribution {
  id: string
  label: string
  keywords?: string[]
  run: () => void
}
/** Props a transcript directive's render receives. `sessionId` arrived with the directive-owner seam;
 *  older hosts omit it (undefined). */
export interface TranscriptDirectiveProps {
  attrs: Readonly<Record<string, string>>
  source: string
  streaming: boolean
  sessionId?: null | string
}
export interface ConnectionRow {
  id: string
  kind?: string
  label?: string
  primary?: boolean
}

/** The file a tool-media renderer draws (`transcript.toolMedia`). */
export interface ToolMediaFile {
  path: string
  name: string
  kind: string
  mime: string
  resolveUrl: () => Promise<string>
}
/** Props a tool-media renderer receives: the file plus the tool call that produced it. */
export interface ToolMediaRendererProps {
  file: ToolMediaFile
  toolName: string
  toolCallId: string
  args: Readonly<Record<string, unknown>>
  result: unknown
  sessionId: null | string
  fallback: ReactNode
}

export const PALETTE_AREA: string
export const TRANSCRIPT_DIRECTIVE_AREA: string
/** Desktops with the tool-media renderer seam only; undefined on older ones (feature-detect it). */
export const TOOL_MEDIA_RENDERER_AREA: string | undefined
export const host: {
  composer?: {
    submit: (sessionId: null | string, text: string) => boolean
    insertText?: (sessionId: null | string, text: string, opts?: { mode?: 'block' | 'inline' | 'prefix' }) => Promise<boolean>
  }
  state?: { profile?: ReadableAtom<string>; connectionId?: ReadableAtom<null | string> }
  activeConnectionId?: () => null | string
  connections?: () => Promise<ConnectionRow[]>
  openSession?: (storedSessionId: string, options?: { intent?: 'in-place' | 'stack' | 'tab' | 'window' }) => Promise<void>
  navigate: (path: string) => void
  notify: (input: { kind: 'error' | 'info' | 'success' | 'warning'; message: string }) => string
  openWorkspace?: (
    id: string,
    options: { minWidth?: string; onClose?: () => void; render: () => ReactNode; title?: string }
  ) => () => void
}
export function Button(
  props: ComponentProps<'button'> & { size?: 'default' | 'icon' | 'sm'; variant?: 'default' | 'ghost' | 'outline' }
): ReactNode
export function Codicon(props: { name: string; size?: number; spinning?: boolean; style?: CSSProperties }): ReactNode
export function DropdownMenu(props: { children?: ReactNode; open?: boolean; onOpenChange?: (open: boolean) => void }): ReactNode
export function DropdownMenuTrigger(props: { asChild?: boolean; children?: ReactNode }): ReactNode
export function DropdownMenuContent(props: { align?: 'center' | 'end' | 'start'; children?: ReactNode; style?: CSSProperties }): ReactNode
export function DropdownMenuItem(
  props: { children?: ReactNode; disabled?: boolean; onSelect?: (event: Event) => void; title?: string } & Record<`data-${string}`, string>
): ReactNode
export function DropdownMenuSeparator(): ReactNode
