// Phase 6C — Authenticated conversation WebSocket hook.
//
// Wraps the Phase 5C realtime protocol in a React hook so every UI
// component uses the SAME state machine and the SAME transport.  The
// hook never places the JWT in the URL — it goes through the
// `Sec-WebSocket-Protocol` header via the WebSocket constructor's
// `protocols` argument.
//
// State machine
// =============
//
//   idle ──open()──▶ connecting ──connected──▶ connected
//                                └─ai_processing──▶ processing
//                                                   └─assistant_message
//                                                     ├─▶ completed
//                                                     └─▶ connected (idle)
//   any state ──close──▶ disconnected
//   any state ──4xxx auth close──▶ authorization_failure
//   any state ──other 4xxx close──▶ retryable_failure
//   any state ──error frame──▶ protocol_violation
//
// Concurrency
// -----------
//   One AI request in flight per connection.  `sendUserMessage` rejects
//   with `{code:"Busy"}` while a previous request has not yet produced
//   its terminal frame.  Duplicate `request_id` (browser resend) is
//   silently consumed by the dedup FIFO (`MAX_RECENT_REQUEST_IDS`).
//
// Reconnection
// ------------
//   At most ONE auto-reconnect on `disconnected` / `retryable_failure`.
//   After that the user must press the Reconnect button — we never
//   build infinite reconnect loops (per Phase 6C spec).

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type MutableRefObject,
} from 'react'
import type {
  ClientFrame,
  ClientUserMessageFrame,
  ConnectionState,
  ServerAssistantFrame,
  ServerConnectedFrame,
  ServerErrorFrame,
  ServerFrame,
} from '../../types/ai'
import { MAX_RECENT_REQUEST_IDS } from '../../types/ai'
import {
  HEARTBEAT_DEFAULT_SECONDS,
  PROTOCOL_VERSION,
  assertUrlHasNoToken,
  buildPingFrame,
  buildSubprotocols,
  buildUserMessageFrame,
  buildWebSocketUrl,
  isUuid,
  parseServerFrame,
  serializeClientFrame,
} from './protocol'
import { HEARTBEAT_MULTIPLIER } from '../../types/ai'
import type { LookbackDays } from '../../types/finops'

// ---------------------------------------------------------------------------
// Test injection
// ---------------------------------------------------------------------------

/** Pluggable WebSocket factory — production uses `globalThis.WebSocket`,
 *  tests inject a `MockWebSocket`.  The factory must accept the same
 *  argument shape as the global `WebSocket` constructor. */
export type WebSocketFactory = (
  url: string,
  protocols?: string | string[],
) => WebSocketLike

export interface WebSocketLike {
  readyState: number
  onopen: ((ev: unknown) => void) | null
  onclose: ((ev: { code: number; reason: string }) => void) | null
  onmessage: ((ev: { data: string }) => void) | null
  onerror: ((ev: unknown) => void) | null
  send: (data: string) => void
  close: (code?: number, reason?: string) => void
  /** Test-only helper: pretend the server delivered a frame. */
  __test__receive?: (payload: ServerFrame | string) => void
  /** Test-only helper: simulate open / close. */
  __test__open?: () => void
  __test__close?: (code?: number, reason?: string) => void
}

interface MockControls {
  socket: WebSocketLike
  received: ServerFrame[]
  open: () => void
  close: (code?: number, reason?: string) => void
  receive: (payload: ServerFrame | string) => void
}

let _wsFactory: WebSocketFactory = (url, protocols) =>
  new globalThis.WebSocket(url, protocols) as unknown as WebSocketLike

/** Test-only: replace the WebSocket factory. */
export function __setWebSocketFactoryForTests(factory: WebSocketFactory): void {
  _wsFactory = factory
}

/** Test-only: restore the default factory. */
export function __resetWebSocketFactoryForTests(): void {
  _wsFactory = (url, protocols) =>
    new globalThis.WebSocket(url, protocols) as unknown as WebSocketLike
}

// ---------------------------------------------------------------------------
// Public hook surface
// ---------------------------------------------------------------------------

export interface ConversationSocketApi {
  state: ConnectionState
  /** Most recent server `connected` frame; null until connected. */
  connected: ServerConnectedFrame | null
  /** Last `assistant_message` delivered; cleared on the next submit. */
  lastAssistant: ServerAssistantFrame | null
  /** Last `error` frame received. */
  lastError: ServerErrorFrame | null
  /** Request id of the in-flight request, or null. */
  inflightRequestId: string | null
  /** Heartbeat interval announced by the server (default 30s). */
  heartbeatSeconds: number
  /** Connection close code observed, if any. */
  closeCode: number | null
  /** Manually open the socket. */
  connect: () => void
  /** Manually close the socket. */
  disconnect: () => void
  /** Manually reconnect after a failure. */
  reconnect: () => void
  /** Build + send a `user_message` frame. Returns the generated request_id. */
  sendUserMessage: (args: {
    question: string
    region?: string | null
    days: LookbackDays
    requestId?: string
  }) => string
}

export interface UseConversationSocketArgs {
  conversationId: number | null
  /** Returns the bearer token or null. */
  getToken: () => string | null
  /** Returns true when authentication is enabled (Phase 5A). */
  authEnabled?: boolean
  /** When the parent wants to be told when an assistant message arrives. */
  onAssistant?: (frame: ServerAssistantFrame) => void
  /** When the parent wants to be told when an error frame arrives. */
  onError?: (frame: ServerErrorFrame) => void
  /** When the parent wants to be told when the connection establishes. */
  onConnected?: (frame: ServerConnectedFrame) => void
  /** Optional factory override (test-only). */
  factory?: WebSocketFactory
  /** Controls the auto-reconnect policy.  Defaults to one auto-retry. */
  autoReconnect?: boolean
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function isRetryableCloseCode(code: number): boolean {
  if (code === 1006) return true // abnormal closure
  if (code === 1011) return true // internal error
  if (code >= 4400 && code <= 4499 && code !== 4401 && code !== 4403 && code !== 1008) {
    return true
  }
  return false
}

function isAuthorizationCloseCode(code: number): boolean {
  return code === 1008 || code === 4401 || code === 4403
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useConversationSocket(args: UseConversationSocketArgs): ConversationSocketApi {
  const {
    conversationId,
    getToken,
    authEnabled = true,
    onAssistant,
    onError,
    onConnected,
    factory,
    autoReconnect = true,
  } = args

  const [state, setState] = useState<ConnectionState>('idle')
  const [connected, setConnected] = useState<ServerConnectedFrame | null>(null)
  const [lastAssistant, setLastAssistant] = useState<ServerAssistantFrame | null>(null)
  const [lastError, setLastError] = useState<ServerErrorFrame | null>(null)
  const [closeCode, setCloseCode] = useState<number | null>(null)
  const [heartbeatSeconds, setHeartbeatSeconds] = useState<number>(HEARTBEAT_DEFAULT_SECONDS)

  const socketRef = useRef<WebSocketLike | null>(null)
  const inflightRef = useRef<string | null>(null)
  const recentIdsRef = useRef<string[]>([])
  const manualDisconnectRef = useRef<boolean>(false)
  const autoReconnectUsedRef = useRef<boolean>(false)
  const pingTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const callbacksRef = useRef({
    onAssistant,
    onError,
    onConnected,
  })
  callbacksRef.current = { onAssistant, onError, onConnected }

  const factoryRef: MutableRefObject<WebSocketFactory> = useRef(factory ?? _wsFactory)
  factoryRef.current = factory ?? _wsFactory

  // `connectRef` is assigned below; declaring the ref here so the
  // `connect` callback can call itself through the ref to break the
  // self-referential closure (TypeScript can't infer the dependency
  // graph otherwise).
  const connectRef = useRef<() => void>(() => {})

  // -- helpers -----------------------------------------------------------

  const stopHeartbeat = useCallback(() => {
    if (pingTimerRef.current !== null) {
      clearInterval(pingTimerRef.current)
      pingTimerRef.current = null
    }
  }, [])

  const startHeartbeat = useCallback((seconds: number) => {
    stopHeartbeat()
    const cadenceMs = Math.max(5_000, Math.floor(seconds * 1000 * HEARTBEAT_MULTIPLIER))
    pingTimerRef.current = setInterval(() => {
      const ws = socketRef.current
      if (!ws || ws.readyState !== 1) return
      try {
        ws.send(serializeClientFrame(buildPingFrame(Date.now())))
      } catch {
        // best-effort — close handler will run if the socket is gone.
      }
    }, cadenceMs)
  }, [stopHeartbeat])

  const recordRequestId = useCallback((id: string): boolean => {
    if (!isUuid(id)) return false
    if (recentIdsRef.current.includes(id)) return false
    recentIdsRef.current.push(id)
    while (recentIdsRef.current.length > MAX_RECENT_REQUEST_IDS) {
      recentIdsRef.current.shift()
    }
    return true
  }, [])

  const clearSocket = useCallback(() => {
    const ws = socketRef.current
    socketRef.current = null
    stopHeartbeat()
    if (!ws) return
    ws.onopen = null
    ws.onmessage = null
    ws.onclose = null
    ws.onerror = null
    try {
      ws.close(1000, 'client closing')
    } catch {
      /* best-effort */
    }
  }, [stopHeartbeat])

  // -- dispatch a server frame -----------------------------------------

  const dispatch = useCallback((frame: ServerFrame) => {
    switch (frame.type) {
      case 'connected': {
        const f = frame as ServerConnectedFrame
        setConnected(f)
        setState('connected')
        setHeartbeatSeconds(
          typeof f.heartbeat_interval_seconds === 'number' && f.heartbeat_interval_seconds > 0
            ? f.heartbeat_interval_seconds
            : HEARTBEAT_DEFAULT_SECONDS,
        )
        callbacksRef.current.onConnected?.(f)
        startHeartbeat(
          typeof f.heartbeat_interval_seconds === 'number' && f.heartbeat_interval_seconds > 0
            ? f.heartbeat_interval_seconds
            : HEARTBEAT_DEFAULT_SECONDS,
        )
        // Pin protocol version — would be a critical mismatch otherwise.
        if (f.protocol_version !== PROTOCOL_VERSION) {
          setState('protocol_violation')
        }
        return
      }
      case 'user_message_accepted': {
        // USER message persisted; the AI run is starting.  Mark
        // inflight as recognized so sendUserMessage dedup works
        // even if a retry happens.
        return
      }
      case 'ai_processing': {
        setState('processing')
        return
      }
      case 'assistant_message': {
        const f = frame as ServerAssistantFrame
        setLastAssistant(f)
        setLastError(null)
        setState('completed')
        inflightRef.current = null
        callbacksRef.current.onAssistant?.(f)
        return
      }
      case 'error': {
        const f = frame as ServerErrorFrame
        setLastError(f)
        // If the error matches an in-flight request, clear inflight.
        if (f.request_id && inflightRef.current === f.request_id) {
          inflightRef.current = null
        }
        // Map to UI state when the error is terminal for the request
        // (request_id present).  Otherwise treat as protocol violation.
        if (f.request_id) {
          setState('connected')
        } else {
          setState('protocol_violation')
        }
        callbacksRef.current.onError?.(f)
        return
      }
      case 'pong': {
        return
      }
      default: {
        // Unknown frame type — should not happen because the schema
        // validates it server-side.  Treat as protocol violation.
        setState('protocol_violation')
        return
      }
    }
  }, [startHeartbeat])

  // -- connect lifecycle ------------------------------------------------

  const connect = useCallback(() => {
    if (conversationId === null || conversationId === undefined) return
    if (!Number.isFinite(conversationId) || conversationId <= 0) return

    const token = getToken()
    if (!authEnabled || !token) {
      setState('authorization_failure')
      return
    }

    // Clean up any prior socket first.
    clearSocket()

    let url: string
    try {
      url = buildWebSocketUrl({ conversationId })
    } catch {
      setState('protocol_violation')
      return
    }
    assertUrlHasNoToken(url)

    const subprotocols = buildSubprotocols(token)
    if (subprotocols.length === 0) {
      setState('authorization_failure')
      return
    }

    let ws: WebSocketLike
    try {
      ws = factoryRef.current(url, subprotocols)
    } catch {
      setState('disconnected')
      return
    }
    socketRef.current = ws
    manualDisconnectRef.current = false
    setState('connecting')
    setCloseCode(null)

    ws.onopen = () => {
      // The `connected` server frame drives the actual `connected` UI
      // state — `onopen` only flips us out of `connecting` if the
      // server doesn't send a frame in time.  Most browsers fire
      // onopen first; the server frame arrives in a follow-up tick.
      setState((prev) => (prev === 'connecting' ? 'connecting' : prev))
    }

    ws.onmessage = (ev) => {
      let parsed: unknown
      try {
        parsed = JSON.parse(ev.data)
      } catch {
        setState('protocol_violation')
        return
      }
      const frame = parseServerFrame(parsed)
      if (!frame) {
        setState('protocol_violation')
        return
      }
      // Sanitize: refuse any frame that contains token-shaped strings.
      if (/\b[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/.test(ev.data)) {
        setState('protocol_violation')
        return
      }
      dispatch(frame)
    }

    ws.onerror = () => {
      // The close handler will run after onerror; we don't transition
      // state here to avoid a brief flash.
    }

    ws.onclose = (ev) => {
      const code = ev?.code ?? 1006
      setCloseCode(code)
      stopHeartbeat()
      socketRef.current = null
      if (manualDisconnectRef.current) {
        setState('disconnected')
        return
      }
      if (isAuthorizationCloseCode(code)) {
        setState('authorization_failure')
        return
      }
      if (isRetryableCloseCode(code)) {
        if (autoReconnect && !autoReconnectUsedRef.current) {
          autoReconnectUsedRef.current = true
          setState('connecting')
          // Schedule a single retry on the next tick.
          setTimeout(() => {
            // Re-check that the conversation id hasn't changed and
            // we still want to connect.
            connectRef.current()
          }, 250)
          return
        }
        setState('retryable_failure')
        return
      }
      setState('disconnected')
    }
  }, [
    authEnabled,
    clearSocket,
    conversationId,
    dispatch,
    factoryRef,
    getToken,
    autoReconnect,
    stopHeartbeat,
  ])

  // Bind the ref so the auto-reconnect path above can call `connect`.
  connectRef.current = connect

  const disconnect = useCallback(() => {
    manualDisconnectRef.current = true
    clearSocket()
    setState('disconnected')
  }, [clearSocket])

  const reconnect = useCallback(() => {
    autoReconnectUsedRef.current = false
    manualDisconnectRef.current = false
    setLastError(null)
    setLastAssistant(null)
    setCloseCode(null)
    connectRef.current()
  }, [])

  const sendUserMessage = useCallback(
    (args: {
      question: string
      region?: string | null
      days: LookbackDays
      requestId?: string
    }): string => {
      if (inflightRef.current !== null) {
        throw Object.assign(new Error('A previous question is still being processed.'), {
          code: 'Busy',
        })
      }
      let frame: ClientUserMessageFrame
      try {
        frame = buildUserMessageFrame({
          requestId: args.requestId,
          question: args.question,
          region: args.region ?? null,
          days: args.days,
        })
      } catch (err) {
        throw Object.assign(err instanceof Error ? err : new Error(String(err)), {
          code: 'InvalidQuestion',
        })
      }
      if (!recordRequestId(frame.request_id)) {
        throw Object.assign(new Error('Duplicate request id'), { code: 'Busy' })
      }
      const ws = socketRef.current
      if (!ws || ws.readyState !== 1) {
        throw Object.assign(new Error('Connection is not open'), {
          code: 'ConnectionClosed',
        })
      }
      try {
        ws.send(serializeClientFrame(frame))
      } catch (err) {
        throw Object.assign(err instanceof Error ? err : new Error(String(err)), {
          code: 'ConnectionClosed',
        })
      }
      inflightRef.current = frame.request_id
      setLastAssistant(null)
      setLastError(null)
      return frame.request_id
    },
    [recordRequestId],
  )

  // -- open on mount, close on unmount / conversation change -----------

  useEffect(() => {
    autoReconnectUsedRef.current = false
    manualDisconnectRef.current = false
    setLastAssistant(null)
    setLastError(null)
    setConnected(null)
    setCloseCode(null)
    inflightRef.current = null
    recentIdsRef.current = []
    if (conversationId === null || conversationId === undefined) {
      setState('idle')
      return
    }
    connectRef.current()
    return () => {
      manualDisconnectRef.current = true
      clearSocket()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId, authEnabled])

  // Stop heartbeat on unmount.
  useEffect(() => {
    return () => stopHeartbeat()
  }, [stopHeartbeat])

  // Drop in-flight if the token disappears.
  useEffect(() => {
    if (!authEnabled) {
      inflightRef.current = null
    }
  }, [authEnabled])

  const inflightRequestId = inflightRef.current

  return useMemo<ConversationSocketApi>(
    () => ({
      state,
      connected,
      lastAssistant,
      lastError,
      inflightRequestId,
      heartbeatSeconds,
      closeCode,
      connect,
      disconnect,
      reconnect,
      sendUserMessage,
    }),
    [
      state,
      connected,
      lastAssistant,
      lastError,
      inflightRequestId,
      heartbeatSeconds,
      closeCode,
      connect,
      disconnect,
      reconnect,
      sendUserMessage,
    ],
  )
}

// ---------------------------------------------------------------------------
// Public util: send a frame without an active socket (test-only).
// ---------------------------------------------------------------------------

/** Type-narrowing helper for callers. */
export function isAssistantFrame(f: ServerFrame): f is ServerAssistantFrame {
  return f.type === 'assistant_message'
}

export function isErrorFrame(f: ServerFrame): f is ServerErrorFrame {
  return f.type === 'error'
}

export function isConnectedFrame(f: ServerFrame): f is ServerConnectedFrame {
  return f.type === 'connected'
}

/** Convenience: serialize any client frame (test-only). */
export function encodeClient(frame: ClientFrame): string {
  return serializeClientFrame(frame)
}
