/**
 * Pure derivations behind Settings -> Browser.
 *
 * Each fact the page shows (which first-run step is next, what the status badge
 * says, which fallback chip is lit, whether an origin entry is well-formed) is one
 * function of the gateway's `GET /api/browser/status` answer plus the config, so
 * each rule is testable without rendering and the two views (the three steps and
 * the status card) cannot disagree about the same fact.
 */
import type { BrowserConnection, BrowserKind, BrowserStatusData } from '../../api/client'

/** Where one first-run step stands. */
export type StepState = 'done' | 'active' | 'pending'

export interface FirstRunProgress {
  engine: StepState
  connect: StepState
  try: StepState
  /** Steps done, 0-3; the chip beside the heading reads it. */
  doneCount: number
  /**
   * The sub-rows of step 2, each a fact the status implies. `debuggingOn` is
   * inferred: Chrome only shows its Allow prompt once remote debugging is
   * enabled, so `waiting_allow` or a connected Chrome both prove it; nothing else
   * can.
   */
  debuggingOn: boolean
  allowed: boolean
}

/** Whether the attached browser is the user's own (Chrome or Edge), with their logins. */
export function isUserBrowser(kind: BrowserKind | null | undefined): boolean {
  return kind === 'user_chrome' || kind === 'edge'
}

/**
 * The three setup steps, from the gateway's answer.
 *
 * Step 2 is "done" in two cases that look different but mean the same thing for
 * the user: their own browser is connected, or the gateway picked Kiro's Chromium
 * because there is no Chrome to connect (a server, a headless host). In the second
 * case there is nothing left for them to allow.
 */
export function firstRunProgress(status: BrowserStatusData | undefined): FirstRunProgress {
  const installed = !!status?.installed
  const connected = status?.connection === 'connected'
  const kind = status?.browser?.kind ?? null
  const userBrowserConnected = connected && isUserBrowser(kind)
  const kiroChromiumConnected = connected && kind === 'kiro_chromium'
  const waiting = status?.connection === 'waiting_allow'

  const engine: StepState = installed ? 'done' : 'active'
  const connect: StepState = !installed
    ? 'pending'
    : userBrowserConnected || kiroChromiumConnected ? 'done' : 'active'
  const tryStep: StepState = engine === 'done' && connect === 'done' ? 'active' : 'pending'
  const doneCount = (engine === 'done' ? 1 : 0) + (connect === 'done' ? 1 : 0)
  return {
    engine,
    connect,
    try: tryStep,
    doneCount,
    debuggingOn: waiting || userBrowserConnected,
    allowed: userBrowserConnected,
  }
}

/** Badge tone per connection state. `waiting_allow` is the one yellow state. */
export const CONNECTION_VARIANT: Record<BrowserConnection, 'ok' | 'warn' | 'err' | 'muted'> = {
  connected: 'ok',
  waiting_allow: 'warn',
  disconnected: 'muted',
  error: 'err',
}

/**
 * Catalog KEY per connection state. A flat record of literal keys so the
 * key-reference gate resolves `i18nT(CONNECTION_LABEL_KEY[state])` statically;
 * module scope is fine because these are keys, not copy.
 */
export const CONNECTION_LABEL_KEY: Record<BrowserConnection, string> = {
  connected: 'pages.settings.browserPanel.connection_connected',
  waiting_allow: 'pages.settings.browserPanel.connection_waiting_allow',
  disconnected: 'pages.settings.browserPanel.connection_disconnected',
  error: 'pages.settings.browserPanel.connection_error',
}

/** Catalog KEY per browser kind, for the chips and the status card's title. */
export const BROWSER_KIND_LABEL_KEY: Record<BrowserKind, string> = {
  user_chrome: 'pages.settings.browserPanel.kind_user_chrome',
  edge: 'pages.settings.browserPanel.kind_edge',
  kiro_chromium: 'pages.settings.browserPanel.kind_kiro_chromium',
}

/** The discovery order the status card draws as chips, left to right. */
export const FALLBACK_ORDER: readonly BrowserKind[] = ['user_chrome', 'edge', 'kiro_chromium']

/** Whether the gateway host runs Windows, where the Edge rung is reachable at all. */
export function isWindowsHost(platform: string | undefined): boolean {
  return typeof platform === 'string' && platform.toLowerCase().startsWith('win')
}
