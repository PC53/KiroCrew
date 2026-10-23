/**
 * The live-browser store: one place that holds what the gateway relay says
 * about the agent's browser, per session, and the hooks the card reads it
 * through.
 *
 * Two inputs, both from the dashboard websocket (`useWebSocket`):
 *   - `browser_live` JSON events: the state machine (waiting_allow, live,
 *     in_control, done, login_needed, error), the page, the browser, the step
 *     strip and the stream's current rung and fps. `applyEvent` folds one in.
 *   - binary frames (`useBrowserLive.frame.ts`): `applyFrame` acks each one
 *     ONCE, keeps it in the turn's ring for Replay and hands it to whichever
 *     canvas is painting that turn, outside React's render cycle.
 *
 * Why a module store and not Redux: `TurnBlock` is store-free by design (the
 * app-sdk embeds it with no Provider), the frame path must not re-render a
 * React tree 60 times a second, and the frames themselves are bytes React has
 * no business holding. `useSyncExternalStore` gives the card a subscription
 * with none of that.
 *
 * Turn binding. The relay names turns by its own `turn_id`; the transcript
 * knows a turn by its rows. The card for a turn anchors on that turn's FIRST
 * `browser` tool row and binds to the session's current live record the first
 * time it mounts while the turn is running. The binding is remembered here, so
 * the virtualised transcript can unmount and remount the card without losing
 * which record is its own. A settled turn that was never bound (the page was
 * reloaded, or the card mounted after the turn ended) binds to the newest
 * unbound record only when that record's clock overlaps the anchor row's
 * timestamp; otherwise it shows nothing rather than another turn's browsing.
 */

import { useSyncExternalStore } from 'react'

import { api } from '../api/client'
import type { BrowserInputEvent } from './useBrowserLive.input'
import { FrameRing, decodeBrowserFrame, type DecodedBrowserFrame } from './useBrowserLive.frame'

export type BrowserLiveState = 'waiting_allow' | 'live' | 'in_control' | 'done' | 'login_needed' | 'error'
export type BrowserKind = 'user_chrome' | 'edge' | 'kiro_chromium'

export interface BrowserLiveStep { label: string; status: 'done' | 'active' | 'pending' }

/** The `browser_live` websocket event, as the wire contract spells it. */
export interface BrowserLiveEvent {
  session_key: string
  turn_id: string
  state: BrowserLiveState
  url?: string
  title?: string
  browser?: BrowserKind
  headless?: boolean
  step?: { index: number; label: string }
  steps?: BrowserLiveStep[]
  rung?: number
  fps?: number
  /** Server-authored detail for `error`; rendered verbatim, never translated. */
  detail?: string
}

/** One turn's browsing, as the card sees it. Replaced (not mutated) on every event. */
export interface BrowserLiveTurn {
  sessionKey: string
  turnId: string
  state: BrowserLiveState
  url: string
  title: string
  browser: BrowserKind | null
  headless: boolean
  step: { index: number; label: string } | null
  steps: BrowserLiveStep[]
  rung: number
  fps: number
  detail: string
  /** Wall-clock ms the first event arrived, and the ms of `done` / `error`. */
  startedAt: number
  endedAt: number | null
  /** True once a frame has landed, so the card can swap its placeholder for the canvas. */
  hasFrame: boolean
  /** The frames kept for Replay. Shared by every copy of this record. */
  ring: FrameRing
}

interface SessionRecord {
  current: BrowserLiveTurn | null
  turns: Map<string, BrowserLiveTurn>
  /** The highest seq acked, so a re-delivered frame is not acked twice. */
  ackedSeq: number
  /** Card instances currently on screen, by instance id. */
  visibleInstances: Set<string>
  /** The last visibility the gateway was told. */
  reportedVisible: boolean | null
}

type Listener = () => void
type FrameListener = (frame: DecodedBrowserFrame) => void

const STATES: ReadonlySet<string> = new Set(['waiting_allow', 'live', 'in_control', 'done', 'login_needed', 'error'])
const BROWSERS: ReadonlySet<string> = new Set(['user_chrome', 'edge', 'kiro_chromium'])

/** How far an anchor row's timestamp may sit from a record's clock and still claim it. */
const BIND_SLACK_MS = 120_000

function readSteps(raw: unknown): BrowserLiveStep[] {
  if (!Array.isArray(raw)) return []
  const out: BrowserLiveStep[] = []
  for (const s of raw) {
    if (!s || typeof s !== 'object') continue
    const { label, status } = s as Record<string, unknown>
    if (typeof label !== 'string') continue
    out.push({ label, status: status === 'done' || status === 'active' ? status : 'pending' })
  }
  return out
}

/** Check a wire event field by field; `null` for one the card must not act on. */
export function readBrowserLiveEvent(raw: unknown): BrowserLiveEvent | null {
  if (!raw || typeof raw !== 'object') return null
  const d = raw as Record<string, unknown>
  if (typeof d.session_key !== 'string' || !d.session_key) return null
  if (typeof d.turn_id !== 'string' || !d.turn_id) return null
  if (typeof d.state !== 'string' || !STATES.has(d.state)) return null
  const step = d.step && typeof d.step === 'object' && typeof (d.step as Record<string, unknown>).label === 'string'
    ? { index: Number((d.step as Record<string, unknown>).index) || 0, label: String((d.step as Record<string, unknown>).label) }
    : undefined
  return {
    session_key: d.session_key,
    turn_id: d.turn_id,
    state: d.state as BrowserLiveState,
    url: typeof d.url === 'string' ? d.url : undefined,
    title: typeof d.title === 'string' ? d.title : undefined,
    browser: typeof d.browser === 'string' && BROWSERS.has(d.browser) ? d.browser as BrowserKind : undefined,
    headless: typeof d.headless === 'boolean' ? d.headless : undefined,
    step,
    steps: d.steps === undefined ? undefined : readSteps(d.steps),
    rung: typeof d.rung === 'number' && Number.isFinite(d.rung) ? d.rung : undefined,
    fps: typeof d.fps === 'number' && Number.isFinite(d.fps) ? d.fps : undefined,
    detail: typeof d.detail === 'string' ? d.detail : undefined,
  }
}

const isEnded = (s: BrowserLiveState) => s === 'done' || s === 'error'

/** Map keys are `session + SEP + id`; a NUL cannot appear in either half. */
const SEP = '\u0000'
const joinKey = (a: string, b: string) => a + SEP + b

/** Swallow a write's rejection: the card's writes are best effort, and the
 *  journal already has the `ApiError`. */
const quiet = (p: Promise<unknown>) => { p.catch(() => {}) }

class BrowserLiveStore {
  private sessions = new Map<string, SessionRecord>()
  private anchors = new Map<string, string>()
  private listeners = new Set<Listener>()
  private frameListeners = new Map<string, Set<FrameListener>>()
  private documentListening = false
  /** Injected for tests; production uses the facade's methods. */
  private writes = {
    ack: (k: string, seq: number) => api.browserLiveAck(k, seq) as Promise<unknown>,
    visibility: (k: string, v: boolean) => api.browserLiveVisibility(k, v) as Promise<unknown>,
    claim: (k: string) => api.browserInputClaim(k) as Promise<unknown>,
    release: (k: string) => api.browserInputRelease(k) as Promise<unknown>,
    input: (k: string, e: BrowserInputEvent) => api.browserInputEvent(k, e) as Promise<unknown>,
    open: (k: string) => api.browserLiveOpenInBrowser(k) as Promise<unknown>,
  }

  private session(key: string): SessionRecord {
    let s = this.sessions.get(key)
    if (!s) {
      s = { current: null, turns: new Map(), ackedSeq: -1, visibleInstances: new Set(), reportedVisible: null }
      this.sessions.set(key, s)
    }
    return s
  }

  private notify(): void { for (const l of this.listeners) l() }

  subscribe = (l: Listener): (() => void) => {
    this.listeners.add(l)
    return () => { this.listeners.delete(l) }
  }

  // ---- inbound ------------------------------------------------------------

  /** Fold one `browser_live` event in. Unknown or malformed events are dropped. */
  applyEvent(raw: unknown, now: number = Date.now()): void {
    const ev = readBrowserLiveEvent(raw)
    if (!ev) return
    const s = this.session(ev.session_key)
    const prev = s.turns.get(ev.turn_id)
    // A new turn ends the one before it: the relay streams one turn at a time.
    if (!prev && s.current && !isEnded(s.current.state)) {
      const closed: BrowserLiveTurn = { ...s.current, state: 'done', endedAt: now }
      s.turns.set(closed.turnId, closed)
    }
    const next: BrowserLiveTurn = {
      sessionKey: ev.session_key,
      turnId: ev.turn_id,
      state: ev.state,
      url: ev.url ?? prev?.url ?? '',
      title: ev.title ?? prev?.title ?? '',
      browser: ev.browser ?? prev?.browser ?? null,
      headless: ev.headless ?? prev?.headless ?? false,
      step: ev.step ?? (ev.state === 'done' ? null : prev?.step ?? null),
      steps: ev.steps ?? prev?.steps ?? [],
      rung: ev.rung ?? prev?.rung ?? 0,
      fps: ev.fps ?? prev?.fps ?? 0,
      detail: ev.detail ?? (ev.state === 'error' ? prev?.detail ?? '' : ''),
      startedAt: prev?.startedAt ?? now,
      endedAt: isEnded(ev.state) ? (prev?.endedAt ?? now) : null,
      hasFrame: prev?.hasFrame ?? false,
      ring: prev?.ring ?? new FrameRing(),
    }
    s.turns.set(next.turnId, next)
    s.current = next
    this.notify()
  }

  /** Keep one decoded frame, ack it once, and paint it where a canvas listens. */
  applyFrame(frame: DecodedBrowserFrame): void {
    const key = frame.header.session_key
    const s = this.session(key)
    if (frame.header.seq > s.ackedSeq) {
      s.ackedSeq = frame.header.seq
      quiet(this.writes.ack(key, frame.header.seq))
    }
    const turn = s.current
    if (!turn || isEnded(turn.state)) return
    turn.ring.push(frame)
    if (!turn.hasFrame) {
      const withFrame = { ...turn, hasFrame: true }
      s.turns.set(turn.turnId, withFrame)
      s.current = withFrame
      this.notify()
    }
    const ls = this.frameListeners.get(joinKey(key, turn.turnId))
    if (ls) for (const l of ls) l(frame)
  }

  /** One binary websocket message: a frame, or nothing. A frame that fails the
   *  header checks is dropped whole (see useBrowserLive.frame). */
  applyBinary(buf: ArrayBuffer): void {
    const frame = decodeBrowserFrame(buf)
    if (frame) this.applyFrame(frame)
  }

  /** Paint callback for one turn. The last frame already held is delivered at once. */
  subscribeFrames(sessionKey: string, turnId: string, cb: FrameListener): () => void {
    const k = joinKey(sessionKey, turnId)
    let set = this.frameListeners.get(k)
    if (!set) { set = new Set(); this.frameListeners.set(k, set) }
    set.add(cb)
    const last = this.sessions.get(sessionKey)?.turns.get(turnId)?.ring.last
    if (last) cb(last)
    return () => {
      set?.delete(cb)
      if (set && set.size === 0) this.frameListeners.delete(k)
    }
  }

  // ---- reads --------------------------------------------------------------

  current(sessionKey: string | null): BrowserLiveTurn | null {
    return sessionKey ? this.sessions.get(sessionKey)?.current ?? null : null
  }

  inControl(sessionKey: string | null): boolean {
    return this.current(sessionKey)?.state === 'in_control'
  }

  /**
   * The record a turn card shows, by the rule in the module comment.
   * `anchorTs` is the anchor row's server timestamp in ms, when it has one.
   */
  resolveTurn(sessionKey: string, anchorKey: string, running: boolean, anchorTs: number | null): BrowserLiveTurn | null {
    const s = this.sessions.get(sessionKey)
    if (!s) return null
    const ak = joinKey(sessionKey, anchorKey)
    const bound = this.anchors.get(ak)
    if (bound) return s.turns.get(bound) ?? null
    const cur = s.current
    if (!cur) return null
    if (running) {
      if (isEnded(cur.state) && this.isBound(sessionKey, cur.turnId)) return null
      this.anchors.set(ak, cur.turnId)
      return cur
    }
    // Settled turn, never bound: claim the newest unbound FINISHED record only
    // when the clocks agree, so a reload cannot hand an old turn a newer turn's
    // browsing, and a settled turn never shows a turn that is still live.
    if (!isEnded(cur.state) || this.isBound(sessionKey, cur.turnId)) return null
    if (anchorTs !== null) {
      const end = cur.endedAt ?? Date.now()
      if (anchorTs < cur.startedAt - BIND_SLACK_MS || anchorTs > end + BIND_SLACK_MS) return null
    }
    this.anchors.set(ak, cur.turnId)
    return cur
  }

  private isBound(sessionKey: string, turnId: string): boolean {
    const prefix = sessionKey + SEP
    for (const [k, v] of this.anchors) if (v === turnId && k.startsWith(prefix)) return true
    return false
  }

  // ---- outbound -----------------------------------------------------------

  /** One card instance reports whether it is on screen; the gateway hears the OR. */
  setVisible(sessionKey: string, instanceId: string, visible: boolean): void {
    const s = this.session(sessionKey)
    if (visible) s.visibleInstances.add(instanceId)
    else s.visibleInstances.delete(instanceId)
    this.listenToDocument()
    this.reportVisibility(sessionKey)
  }

  private effectiveVisible(s: SessionRecord): boolean {
    const docVisible = typeof document === 'undefined' || document.visibilityState !== 'hidden'
    return docVisible && s.visibleInstances.size > 0
  }

  private reportVisibility(sessionKey: string): void {
    const s = this.session(sessionKey)
    const v = this.effectiveVisible(s)
    if (s.reportedVisible === v) return
    s.reportedVisible = v
    quiet(this.writes.visibility(sessionKey, v))
  }

  private listenToDocument(): void {
    if (this.documentListening || typeof document === 'undefined') return
    this.documentListening = true
    document.addEventListener('visibilitychange', () => {
      for (const k of this.sessions.keys()) this.reportVisibility(k)
    })
  }

  /** Take over. The state flips at once and reverts if the gateway refuses. */
  async claim(sessionKey: string): Promise<boolean> {
    const s = this.session(sessionKey)
    const before = s.current
    if (!before || isEnded(before.state)) return false
    this.setState(sessionKey, 'in_control')
    try {
      await this.writes.claim(sessionKey)
      return true
    } catch {
      if (s.current?.turnId === before.turnId) this.setState(sessionKey, before.state)
      return false
    }
  }

  /** Hand back. Optimistic the same way; the gateway's own event is the truth. */
  async release(sessionKey: string): Promise<void> {
    const s = this.session(sessionKey)
    if (s.current?.state !== 'in_control') return
    this.setState(sessionKey, 'live')
    try { await this.writes.release(sessionKey) } catch { /* the next event corrects the state */ }
  }

  sendInput(sessionKey: string, event: BrowserInputEvent): Promise<unknown> {
    return this.writes.input(sessionKey, event).catch(() => undefined)
  }

  openInBrowser(sessionKey: string): void {
    quiet(this.writes.open(sessionKey))
  }

  private setState(sessionKey: string, state: BrowserLiveState): void {
    const s = this.session(sessionKey)
    if (!s.current) return
    const next = { ...s.current, state }
    s.turns.set(next.turnId, next)
    s.current = next
    this.notify()
  }

  // ---- tests --------------------------------------------------------------

  /** Replace the writes (tests). Returns the previous set so a test can restore it. */
  __setWritesForTests(w: Partial<BrowserLiveStore['writes']>): BrowserLiveStore['writes'] {
    const prev = this.writes
    this.writes = { ...prev, ...w }
    return prev
  }

  __resetForTests(): void {
    this.sessions.clear()
    this.anchors.clear()
    this.frameListeners.clear()
    this.notify()
  }
}

export const browserLiveStore = new BrowserLiveStore()

// ---- hooks ------------------------------------------------------------------

/** The record the card for a turn shows, or `null` for a turn with none. */
export function useBrowserLiveTurn(sessionKey: string | null, anchorKey: string | null, running: boolean, anchorTs: number | null): BrowserLiveTurn | null {
  return useSyncExternalStore(
    browserLiveStore.subscribe,
    () => (sessionKey && anchorKey ? browserLiveStore.resolveTurn(sessionKey, anchorKey, running, anchorTs) : null),
    () => null,
  )
}

/** True while the person holds the session's browser (the footer reads this). */
export function useBrowserLiveInControl(sessionKey: string | null): boolean {
  return useSyncExternalStore(
    browserLiveStore.subscribe,
    () => browserLiveStore.inControl(sessionKey),
    () => false,
  )
}
