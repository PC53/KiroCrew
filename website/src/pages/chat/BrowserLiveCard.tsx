import { memo, useCallback, useEffect, useId, useRef, useState } from 'react'
import { motion, useReducedMotion } from 'framer-motion'
import { useInRouterContext, useNavigate } from 'react-router-dom'

import type { ChatMessage } from '../../types'
import { browserLiveStore, useBrowserLiveTurn, type BrowserLiveTurn } from '../../hooks/useBrowserLive'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import type { BrowserLiveCanvasHandle } from './BrowserLiveCardCanvas'
import { BrowserLiveAllowView, BrowserLiveDoneRow, BrowserLiveLiveView, hostOf } from './BrowserLiveCardViews'

/**
 * BrowserLiveCard: the agent's browser, in the chat, while it browses.
 *
 * One card per turn, attached to that turn's FIRST `browser` tool call
 * (TurnBlock mounts it after the turn's last browser row, so it sits where the
 * eye already is: between the work and the live "Thinking" line). The card is
 * ONE element through every state. Waiting for Allow, live, hovered, in
 * control, done: the same `motion.div` with one `layoutId` changes size and
 * contents, never swaps for another component, so a takeover reads as "the
 * card grew" and a finished turn as "the card folded to a line". Reduced motion
 * drops the animation and keeps the continuity.
 *
 * State comes from `useBrowserLive` (the `browser_live` websocket event and the
 * binary frame stream); this component only decides what to show for it.
 * Frames are painted by `BrowserLiveCardCanvas` outside React. Visibility is
 * reported from here: an IntersectionObserver on the root plus the document's
 * own visibility, so an off-screen or background card stops the screencast.
 *
 * Trust: `title` and every step label are agent- or page-authored text and go
 * through `sanitizeLlmOutput` in the views; the URL shows as its host. Nothing
 * typed during takeover passes through this component at all.
 */

/** The `browser` tool row: the trusted name when the gateway stamped one, else the pill text. */
export function isBrowserToolMessage(msg: ChatMessage): boolean {
  if (msg.role !== 'tool') return false
  const trusted = msg.meta?.tool_name
  if (typeof trusted === 'string' && trusted) return trusted === 'browser'
  return /^🔧\s*@?browser(\s|$)/.test(msg.content)
}

/** What a turn's card anchors on: the first browser row's call id, else its timestamp. */
export function browserLiveAnchor(msg: ChatMessage): { key: string; ts: number | null } {
  const id = msg.meta?.tool_call_id
  const key = typeof id === 'string' && id ? `call:${id}` : `ts:${msg.ts ?? ''}`
  const n = msg.ts ? Number(msg.ts) : NaN
  // Server timestamps are seconds (a float string); an ISO string parses too.
  const ts = Number.isFinite(n) ? n * 1000 : msg.ts ? Date.parse(msg.ts) : NaN
  return { key, ts: Number.isFinite(ts) ? ts : null }
}

export interface BrowserLiveCardProps {
  sessionKey: string
  anchorKey: string
  anchorTs: number | null
  /** The turn is still running, so a card with no binding takes the session's current record. */
  running: boolean
  /** Navigate to a dashboard path (Settings -> Browser). */
  goTo: (to: string) => void
}

type View = 'allow' | 'live' | 'done'

function viewFor(turn: BrowserLiveTurn): View {
  switch (turn.state) {
    case 'waiting_allow': return 'allow'
    case 'done':
    case 'error': return 'done'
    default: return 'live'
  }
}

function BrowserLiveCard({ sessionKey, anchorKey, anchorTs, running, goTo }: BrowserLiveCardProps) {
  useLanguageGeneration()
  const turn = useBrowserLiveTurn(sessionKey, anchorKey, running, anchorTs)
  const reduce = useReducedMotion()
  const instanceId = useId()
  const rootRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<BrowserLiveCanvasHandle>(null)
  const [onScreen, setOnScreen] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const [replaying, setReplaying] = useState(false)

  // On-screen tracking. An observer where the host has one; a host without
  // (an old shim) counts the card as visible so the stream is never stuck off.
  useEffect(() => {
    const el = rootRef.current
    if (!el || !turn) return
    if (typeof IntersectionObserver !== 'function') {
      setOnScreen(true)
      browserLiveStore.setVisible(sessionKey, instanceId, true)
      return () => browserLiveStore.setVisible(sessionKey, instanceId, false)
    }
    const io = new IntersectionObserver((entries) => {
      const v = entries.some(e => e.isIntersecting)
      setOnScreen(v)
      browserLiveStore.setVisible(sessionKey, instanceId, v)
    }, { threshold: 0.05 })
    io.observe(el)
    return () => {
      io.disconnect()
      browserLiveStore.setVisible(sessionKey, instanceId, false)
    }
  }, [sessionKey, instanceId, !!turn])

  const inControl = turn?.state === 'in_control'
  // Focus the frame the moment control arrives, so keys go to the page at once.
  useEffect(() => { if (inControl) canvasRef.current?.focus() }, [inControl])

  const claim = useCallback(() => { void browserLiveStore.claim(sessionKey) }, [sessionKey])
  const release = useCallback(() => { void browserLiveStore.release(sessionKey) }, [sessionKey])
  const openInBrowser = useCallback(() => browserLiveStore.openInBrowser(sessionKey), [sessionKey])

  const replay = useCallback(() => {
    const c = canvasRef.current
    if (!c || !turn) return
    if (replaying) { c.stop(); setReplaying(false); return }
    setReplaying(true)
    void c.replay(turn.ring.all()).then(() => setReplaying(false))
  }, [turn, replaying])

  if (!turn) return null
  const view = viewFor(turn)
  const grown = inControl || (expanded && view === 'live')
  const transition = reduce ? { duration: 0 } : { type: 'spring' as const, stiffness: 420, damping: 38 }

  return (
    <div className="px-4 mx-auto w-full py-1" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <motion.div
        ref={rootRef}
        layout
        layoutId={`browser-live-${sessionKey}-${turn.turnId}`}
        transition={transition}
        data-testid="browser-live-card"
        data-state={turn.state}
        data-grown={grown ? 'true' : undefined}
        className={`relative overflow-hidden rounded-xl border bg-card text-text ${
          inControl
            ? 'border-accent ring-2 ring-accent/40 shadow-[0_0_28px_var(--accent-glow)]'
            : 'border-border shadow-sm'
        }`}
        style={grown ? { marginInline: 'calc(-1 * clamp(0px, (100vw - var(--mc-content-width, 900px) - 160px) / 2, 56px))' } : undefined}
      >
        {view === 'allow' && (
          <BrowserLiveAllowView
            turn={turn}
            onSwitchToBrowser={openInBrowser}
            onUseKiroBrowser={() => goTo('/settings/browser')}
          />
        )}
        {view === 'live' && (
          <BrowserLiveLiveView
            turn={turn}
            grown={grown}
            onScreen={onScreen}
            canvasRef={canvasRef}
            onClaim={claim}
            onRelease={release}
            onOpenInBrowser={openInBrowser}
            onToggleExpand={() => setExpanded(v => !v)}
          />
        )}
        {view === 'done' && (
          <BrowserLiveDoneRow
            turn={turn}
            onScreen={onScreen}
            canvasRef={canvasRef}
            replaying={replaying}
            onReplay={replay}
            onOpenInBrowser={openInBrowser}
          />
        )}
      </motion.div>
    </div>
  )
}

/** The Allow card's "use Kiro's own browser" goes to Settings -> Browser. The
 *  router is optional here (the app-sdk embed has none), so the navigation is
 *  resolved once, above, and handed down as a plain function. */
function BrowserLiveCardWithRouter(props: Omit<BrowserLiveCardProps, 'goTo'>) {
  const navigate = useNavigate()
  const goTo = useCallback((to: string) => navigate(to), [navigate])
  return <BrowserLiveCard {...props} goTo={goTo} />
}

const assignLocation = (to: string) => { window.location.assign(to) }

function BrowserLiveCardRoot(props: Omit<BrowserLiveCardProps, 'goTo'>) {
  return useInRouterContext() ? <BrowserLiveCardWithRouter {...props} /> : <BrowserLiveCard {...props} goTo={assignLocation} />
}

export { hostOf }
export default memo(BrowserLiveCardRoot)
