import type { RefObject } from 'react'
import { motion, useReducedMotion } from 'framer-motion'
import {
  Check, ExternalLink, Globe, Hand, Lock, LogIn, Maximize2, Minimize2,
  Monitor, MoreHorizontal, MousePointer2, Pause, Play, ShieldCheck,
} from 'lucide-react'

import { Btn } from '../../components/ui'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../../components/ui/dropdown-menu'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import { fmtDuration } from '../../i18n/format'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import type { BrowserKind, BrowserLiveTurn } from '../../hooks/useBrowserLive'
import BrowserLiveCardCanvas, { type BrowserLiveCanvasHandle } from './BrowserLiveCardCanvas'

/**
 * The three faces of BrowserLiveCard. Each is the INSIDE of the card's one
 * motion element (the card owns the element and the state); these only draw.
 * Row geometry belongs to the card; these fill it.
 */

/** A page URL as the host a person reads, or the raw text when it is not a URL. */
export function hostOf(url: string): string {
  if (!url) return ''
  try {
    const u = new URL(url)
    return u.hostname || url
  } catch {
    return url
  }
}

function isHttps(url: string): boolean {
  try { return new URL(url).protocol === 'https:' } catch { return false }
}

/** "Your Chrome", "Edge", "Kiro's browser (headless)": which browser the frame shows. */
export function browserLabel(kind: BrowserKind | null, headless: boolean): string {
  switch (kind) {
    case 'user_chrome': return i18nT('pages.chat.browserLiveCard.browser_user_chrome')
    case 'edge': return i18nT('pages.chat.browserLiveCard.browser_edge')
    case 'kiro_chromium': return headless ? i18nT('pages.chat.browserLiveCard.browser_kiro_headless') : i18nT('pages.chat.browserLiveCard.browser_kiro')
    default: return i18nT('pages.chat.browserLiveCard.browser_unknown')
  }
}

/** The window "Open in …" focuses; a headless browser has none. */
function openTarget(turn: BrowserLiveTurn): string | null {
  if (turn.browser === 'user_chrome') return i18nT('pages.chat.browserLiveCard.chrome')
  if (turn.browser === 'edge') return i18nT('pages.chat.browserLiveCard.edge')
  if (turn.browser === 'kiro_chromium' && !turn.headless) return i18nT('pages.chat.browserLiveCard.kiro_browser_short')
  return null
}

function title(turn: BrowserLiveTurn): string {
  return sanitizeLlmOutput(turn.title).slice(0, 120)
}

// ---- Waiting for Allow -------------------------------------------------------

export function BrowserLiveAllowView({ turn, onSwitchToBrowser, onUseKiroBrowser }: {
  turn: BrowserLiveTurn
  onSwitchToBrowser: () => void
  onUseKiroBrowser: () => void
}) {
  const browser = browserLabel(turn.browser, turn.headless)
  const target = openTarget(turn)
  return (
    <div className="p-3" data-testid="browser-live-allow">
      <div className="flex items-center gap-2 rounded-lg border border-border bg-bg px-3 py-1.5 text-[13px]">
        <Globe size={14} aria-hidden="true" className="text-muted shrink-0" />
        <span className="font-medium text-text-strong truncate">{hostOf(turn.url)}</span>
        <span className="text-muted truncate">· {i18nT('pages.chat.browserLiveCard.connecting_to', { browser })}</span>
      </div>
      <div className="mt-4 mb-1 flex items-start gap-4 px-1">
        <div className="shrink-0 grid place-items-center size-16 rounded-xl bg-accent-subtle text-accent">
          <ShieldCheck size={30} aria-hidden="true" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-[15px] font-semibold text-text-strong">{i18nT('pages.chat.browserLiveCard.wants_browser', { browser })}</div>
          <p className="mt-1 text-[13px] leading-5 text-muted">
            {i18nT('pages.chat.browserLiveCard.allow_explainer')} {i18nT('pages.chat.browserLiveCard.allow_asked_once')}
          </p>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            {target && (
              <Btn primary onClick={onSwitchToBrowser}>
                <ExternalLink size={14} aria-hidden="true" />
                {i18nT('pages.chat.browserLiveCard.switch_to', { browser: target })}
              </Btn>
            )}
            <Btn onClick={onUseKiroBrowser}>{i18nT('pages.chat.browserLiveCard.use_kiro_browser')}</Btn>
          </div>
        </div>
        <div className="shrink-0 self-center flex items-center gap-1.5 text-[12px] text-muted" role="status">
          <span aria-hidden="true" className="inline-block size-2 rounded-full bg-warn" />
          {i18nT('pages.chat.browserLiveCard.waiting_for_allow')}
        </div>
      </div>
    </div>
  )
}

// ---- Live and In control -----------------------------------------------------

export function BrowserLiveLiveView({ turn, grown, onScreen, canvasRef, onClaim, onRelease, onOpenInBrowser, onToggleExpand }: {
  turn: BrowserLiveTurn
  grown: boolean
  onScreen: boolean
  canvasRef: RefObject<BrowserLiveCanvasHandle>
  onClaim: () => void
  onRelease: () => void
  onOpenInBrowser: () => void
  onToggleExpand: () => void
}) {
  const reduce = useReducedMotion()
  const inControl = turn.state === 'in_control'
  const loginNeeded = turn.state === 'login_needed'
  const target = openTarget(turn)
  const pageTitle = title(turn)
  const step = turn.step ? sanitizeLlmOutput(turn.step.label).slice(0, 120) : ''
  const frameLabel = i18nT('pages.chat.browserLiveCard.frame_label', { host: hostOf(turn.url) })

  // Narrow-first: one column at every width. The header's address pill is the
  // only flexible item (min-w-0 + truncate), the browser chip leaves below `sm`,
  // the Take over label collapses to its icon below `sm`, and the step strip
  // wraps. The frame keeps its 16:10 box at any width.
  return (
    <div data-testid="browser-live-live">
      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border min-h-[44px]">
        {inControl ? (
          <>
            <Hand size={16} aria-hidden="true" className="text-accent shrink-0" />
            <span className="text-[13px] font-semibold text-text-strong whitespace-nowrap">{i18nT('pages.chat.browserLiveCard.you_are_in_control')}</span>
            <span className="text-[12px] text-muted truncate">{i18nT('pages.chat.browserLiveCard.kiro_paused')}</span>
            <span className="flex-1" />
            {target && (
              <Btn onClick={onOpenInBrowser} className="whitespace-nowrap shrink-0">
                <ExternalLink size={14} aria-hidden="true" />
                {i18nT('pages.chat.browserLiveCard.open_in', { browser: target })}
              </Btn>
            )}
            <Btn primary onClick={onRelease} className="whitespace-nowrap shrink-0">
              {i18nT('pages.chat.browserLiveCard.hand_back')}
              <kbd className="ml-0.5 rounded border border-accent-fg/40 px-1 py-px font-sans text-[10px] leading-4">Esc</kbd>
            </Btn>
          </>
        ) : (
          <>
            <span className="flex items-center gap-1.5 text-[12px] font-semibold text-ok whitespace-nowrap" role="status">
              <motion.span
                aria-hidden="true"
                className="inline-block size-2 rounded-full bg-ok"
                animate={reduce ? undefined : { opacity: [1, 0.35, 1] }}
                transition={reduce ? undefined : { duration: 1.6, repeat: Infinity, ease: 'easeInOut' }}
              />
              {i18nT('pages.chat.browserLiveCard.live')}
            </span>
            <div className="flex min-w-0 flex-1 items-center gap-2 rounded-md border border-border bg-bg px-2.5 py-1 text-[13px]">
              {isHttps(turn.url)
                ? <Lock size={13} aria-hidden="true" className="text-muted shrink-0" />
                : <Globe size={13} aria-hidden="true" className="text-muted shrink-0" />}
              <span className="font-medium text-text-strong truncate">{hostOf(turn.url)}</span>
              {pageTitle && <span className="text-muted truncate">· {pageTitle}</span>}
              <span className="flex-1" />
              <span className="hidden sm:flex items-center gap-1 text-[12px] text-muted whitespace-nowrap">
                <Monitor size={13} aria-hidden="true" />
                {browserLabel(turn.browser, turn.headless)}
              </span>
            </div>
            <Btn primary onClick={onClaim} data-testid="browser-live-take-over">
              <Hand size={14} aria-hidden="true" />
              <span className="hidden sm:inline">{i18nT('pages.chat.browserLiveCard.take_over')}</span>
              <span className="sr-only sm:hidden">{i18nT('pages.chat.browserLiveCard.take_over')}</span>
            </Btn>
            {/* Two controls per row: Take over, then everything else behind one menu. */}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Btn className="!px-1.5" aria-label={i18nT('pages.chat.browserLiveCard.more_actions')} title={i18nT('pages.chat.browserLiveCard.more_actions')}>
                  <MoreHorizontal size={14} aria-hidden="true" />
                </Btn>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="min-w-[190px]">
                {target && (
                  <DropdownMenuItem onSelect={onOpenInBrowser}>
                    <ExternalLink size={14} aria-hidden="true" />
                    {i18nT('pages.chat.browserLiveCard.open_in', { browser: target })}
                  </DropdownMenuItem>
                )}
                <DropdownMenuItem onSelect={onToggleExpand}>
                  {grown ? <Minimize2 size={14} aria-hidden="true" /> : <Maximize2 size={14} aria-hidden="true" />}
                  {grown ? i18nT('pages.chat.browserLiveCard.shrink') : i18nT('pages.chat.browserLiveCard.expand')}
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </>
        )}
      </div>

      {loginNeeded && !inControl && (
        <div className="flex items-center gap-2 px-3 py-1.5 bg-accent-subtle text-[13px] text-text border-b border-border" role="status">
          <LogIn size={14} aria-hidden="true" className="text-accent shrink-0" />
          <span className="flex-1">{i18nT('pages.chat.browserLiveCard.take_over_to_sign_in')}</span>
          <Btn onClick={onClaim}>{i18nT('pages.chat.browserLiveCard.take_over')}</Btn>
        </div>
      )}

      {/* Frame */}
      <div className="group relative bg-card" style={{ aspectRatio: '16 / 10' }}>
        {turn.hasFrame ? (
          <BrowserLiveCardCanvas
            ref={canvasRef}
            sessionKey={turn.sessionKey}
            turnId={turn.turnId}
            visible={onScreen}
            interactive={inControl}
            onHandBack={onRelease}
            onClaim={inControl ? undefined : onClaim}
            aria-label={inControl ? i18nT('pages.chat.browserLiveCard.frame_label_in_control', { host: hostOf(turn.url) }) : frameLabel}
          />
        ) : (
          <div className="absolute inset-0 grid place-items-center text-[13px] text-muted" role="status">
            {i18nT('pages.chat.browserLiveCard.waiting_for_first_frame')}
          </div>
        )}
        {!inControl && turn.hasFrame && (
          // The hover cue. Pointer-events off: the click lands on the canvas.
          <div
            aria-hidden="true"
            className="pointer-events-none absolute inset-0 grid place-items-center bg-bg/40 opacity-0 transition-opacity duration-150 motion-reduce:transition-none group-hover:opacity-100"
          >
            <span className="flex items-center gap-1.5 rounded-full border border-border-strong bg-card/95 px-3 py-1.5 text-[13px] font-medium text-text-strong shadow-md">
              <Hand size={14} aria-hidden="true" />
              {i18nT('pages.chat.browserLiveCard.click_to_take_over')}
            </span>
          </div>
        )}
        {!inControl && step && (
          <div className="pointer-events-none absolute left-3 bottom-3 flex max-w-[80%] items-center gap-1.5 rounded-full bg-bg-elevated/95 border border-border px-3 py-1 text-[12px] text-text shadow-md" role="status">
            <MousePointer2 size={12} aria-hidden="true" className="text-accent shrink-0" />
            <span className="truncate">{step}</span>
          </div>
        )}
      </div>

      {/* Step strip */}
      {!inControl && turn.steps.length > 0 && (
        <ol className="flex flex-wrap items-center gap-1.5 px-3 py-2 border-t border-border" aria-label={i18nT('pages.chat.browserLiveCard.steps')}>
          {turn.steps.map((s, i) => (
            <li
              key={`${i}-${s.label}`}
              aria-current={s.status === 'active' ? 'step' : undefined}
              className={`flex items-center gap-1 rounded-full border px-2 py-0.5 text-[12px] leading-4 ${
                s.status === 'done' ? 'border-border text-text'
                : s.status === 'active' ? 'border-accent text-accent bg-accent-subtle'
                : 'border-border text-muted'
              }`}
            >
              {s.status === 'done' && <Check size={11} aria-hidden="true" className="text-ok" />}
              {s.status === 'active' && <span aria-hidden="true" className="inline-block size-1.5 rounded-full bg-accent" />}
              <span className="truncate max-w-[180px]">{sanitizeLlmOutput(s.label).slice(0, 60)}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}

// ---- Done ----------------------------------------------------------------------

function durationLabel(turn: BrowserLiveTurn): string {
  const ms = Math.max(0, (turn.endedAt ?? Date.now()) - turn.startedAt)
  const s = Math.round(ms / 1000)
  if (s < 60) return fmtDuration([[s, 'second']])
  return fmtDuration([[Math.floor(s / 60), 'minute'], [s % 60, 'second']], { dropZero: true })
}

export function BrowserLiveDoneRow({ turn, onScreen, canvasRef, replaying, onReplay, onOpenInBrowser }: {
  turn: BrowserLiveTurn
  onScreen: boolean
  canvasRef: RefObject<BrowserLiveCanvasHandle>
  replaying: boolean
  onReplay: () => void
  onOpenInBrowser: () => void
}) {
  const count = turn.steps.length
  const target = openTarget(turn)
  const host = hostOf(turn.url)
  const parts = [
    i18nT('pages.chat.browserLiveCard.steps_count', { count }),
    durationLabel(turn),
    browserLabel(turn.browser, turn.headless),
  ]
  if (turn.browser === 'user_chrome' || turn.browser === 'edge') parts.push(i18nT('pages.chat.browserLiveCard.tab_kept_open'))

  // Narrow-first: the thumbnail is 112px wide on a phone and 200px from `sm`;
  // the text column truncates; the two buttons wrap under it when they must.
  return (
    <div className="flex items-stretch gap-3 p-2" data-testid="browser-live-done">
      <div className="relative shrink-0 w-[112px] sm:w-[200px] overflow-hidden rounded-md border border-border bg-bg" style={{ aspectRatio: '16 / 10' }}>
        {turn.hasFrame ? (
          <BrowserLiveCardCanvas
            ref={canvasRef}
            sessionKey={turn.sessionKey}
            turnId={turn.turnId}
            visible={onScreen}
            interactive={false}
            aria-label={i18nT('pages.chat.browserLiveCard.last_frame_label', { host })}
          />
        ) : (
          <div className="absolute inset-0 grid place-items-center text-muted"><Globe size={22} aria-hidden="true" /></div>
        )}
      </div>
      <div className="min-w-0 flex-1 flex flex-col justify-center gap-1 py-1">
        <div className="flex items-center gap-2 text-[14px] font-semibold text-text-strong">
          <Globe size={15} aria-hidden="true" className="text-muted shrink-0" />
          <span className="truncate">{host ? i18nT('pages.chat.browserLiveCard.browsed_host', { host }) : i18nT('pages.chat.browserLiveCard.browsed')}</span>
        </div>
        <div className="text-[12px] text-muted truncate">{parts.join(' · ')}</div>
        {turn.state === 'error' && (
          <ErrorNotice variant="inline" message={turn.detail || i18nT('pages.chat.browserLiveCard.error_generic')} askAgent />
        )}
        <div className="mt-1 flex flex-wrap items-center gap-2">
          {turn.hasFrame && (
            <Btn onClick={onReplay} aria-pressed={replaying}>
              {replaying ? <Pause size={13} aria-hidden="true" /> : <Play size={13} aria-hidden="true" />}
              {replaying ? i18nT('pages.chat.browserLiveCard.stop_replay') : i18nT('pages.chat.browserLiveCard.replay_steps', { count })}
            </Btn>
          )}
          {target && (
            <Btn onClick={onOpenInBrowser} className="border-transparent">
              <ExternalLink size={13} aria-hidden="true" />
              {i18nT('pages.chat.browserLiveCard.open_in', { browser: target })}
            </Btn>
          )}
        </div>
      </div>
    </div>
  )
}
