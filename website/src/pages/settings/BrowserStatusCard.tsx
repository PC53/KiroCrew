/**
 * Settings -> Browser, the status card (design S1): the browser in use, its
 * connection state, the fallback order as chips, Test and Disconnect.
 *
 * The card turns yellow in exactly one state, `waiting_allow`: Chrome is showing
 * its prompt and the next move is the user's, in another window.
 */
import { ArrowRight, Check, Globe, Loader2, Monitor, Play, Unplug } from 'lucide-react'

import type { BrowserKind, BrowserStatusData, BrowserTestData } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { Badge, Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import {
  BROWSER_KIND_LABEL_KEY,
  CONNECTION_LABEL_KEY,
  CONNECTION_VARIANT,
  FALLBACK_ORDER,
  isUserBrowser,
  isWindowsHost,
} from './browserStatus'

function KindIcon({ kind, size = 14 }: { kind: BrowserKind; size?: number }) {
  return kind === 'kiro_chromium'
    ? <Monitor size={size} className="lucide-inline" aria-hidden />
    : <Globe size={size} className="lucide-inline" aria-hidden />
}

function FallbackChip({ kind, active, dimmed, note }: { kind: BrowserKind; active: boolean; dimmed: boolean; note?: string }) {
  const cls = active
    ? 'border-accent text-accent bg-accent-subtle'
    : dimmed
      ? 'border-border text-muted opacity-60'
      : 'border-border text-text'
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[12px] ${cls}`}
      aria-current={active ? 'true' : undefined}
      data-testid={`browser-chip-${kind}`}
    >
      <KindIcon kind={kind} size={13} />
      <span>{i18nT(BROWSER_KIND_LABEL_KEY[kind])}</span>
      {note && <span className="text-muted">· {note}</span>}
      {active && <Check size={12} className="lucide-inline text-ok-text" aria-hidden />}
    </span>
  )
}

export interface BrowserStatusCardProps {
  status: BrowserStatusData
  testing: boolean
  testResult: BrowserTestData | null
  testError: string | null
  onTest: () => void
  disconnecting: boolean
  disconnectError: string | null
  onDisconnect: () => void
}

export function BrowserStatusCard({
  status, testing, testResult, testError, onTest, disconnecting, disconnectError, onDisconnect,
}: BrowserStatusCardProps) {
  const kind = status.browser?.kind ?? null
  const connection = status.connection
  const waiting = connection === 'waiting_allow'
  const windows = isWindowsHost(status.platform)
  const title = kind
    ? i18nT(BROWSER_KIND_LABEL_KEY[kind])
    : i18nT('pages.settings.browserPanel.no_browser_yet')
  const desc = kind === null
    ? i18nT('pages.settings.browserPanel.no_browser_desc')
    : isUserBrowser(kind)
      ? i18nT('pages.settings.browserPanel.user_browser_desc')
      : status.browser?.headless
        ? i18nT('pages.settings.browserPanel.kiro_chromium_headless_desc')
        : i18nT('pages.settings.browserPanel.kiro_chromium_desc')

  return (
    <div
      className={`rounded-xl border p-4 flex flex-col gap-3 ${waiting ? 'border-warn bg-warn-subtle' : 'border-border bg-bg-elevated'}`}
      data-testid="browser-status-card"
      data-connection={connection}
    >
      <div className="flex items-start gap-3">
        <span className={`flex items-center justify-center w-11 h-11 rounded-xl shrink-0 ${waiting ? 'bg-warn text-white' : 'bg-accent-subtle text-accent'}`} aria-hidden>
          {kind ? <KindIcon kind={kind} size={20} /> : <Globe size={20} />}
        </span>
        <div className="flex-1 min-w-0 flex flex-col gap-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-[14px] font-semibold text-text">{title}</span>
            <Badge variant={CONNECTION_VARIANT[connection]} data-testid="browser-connection-badge">
              {i18nT(CONNECTION_LABEL_KEY[connection])}
            </Badge>
          </div>
          <div className="text-[12px] text-muted">{desc}</div>
          {/* Server-authored detail (an error cause, a hint), verbatim and never translated. */}
          {status.detail && connection !== 'connected' && (
            <div className="text-[12px] text-text" data-testid="browser-status-detail">{status.detail}</div>
          )}
          {waiting && (
            <div className="text-[12px] text-warn-text">{i18nT('pages.settings.browserPanel.waiting_allow_hint')}</div>
          )}
        </div>
        <div className="shrink-0 flex flex-col items-end gap-1.5">
          <Btn onClick={onTest} disabled={testing || !status.installed} data-testid="browser-test">
            {testing
              ? <Loader2 size={13} className="lucide-inline animate-spin" aria-hidden />
              : <Play size={13} className="lucide-inline" aria-hidden />}
            {i18nT('pages.settings.browserPanel.test_connection')}
          </Btn>
          <Btn onClick={onDisconnect} disabled={disconnecting || connection === 'disconnected'} data-testid="browser-disconnect">
            <Unplug size={13} className="lucide-inline" aria-hidden />
            {i18nT('pages.settings.browserPanel.disconnect')}
          </Btn>
        </div>
      </div>

      {/* The discovery order, left to right; the lit chip is the browser in use. */}
      <div className="flex items-center gap-1.5 flex-wrap" aria-label={i18nT('pages.settings.browserPanel.fallback_order')}>
        {FALLBACK_ORDER.map((k, i) => (
          <span key={k} className="inline-flex items-center gap-1.5">
            {i > 0 && <ArrowRight size={12} className="text-muted shrink-0" aria-hidden />}
            <FallbackChip
              kind={k}
              active={kind === k}
              dimmed={k === 'edge' && !windows}
              note={k === 'edge' ? i18nT('pages.settings.browserPanel.windows_only') : undefined}
            />
          </span>
        ))}
      </div>

      {testResult && !testError && (
        <div className="text-[12px]" data-testid="browser-test-result" aria-live="polite">
          {testResult.ok
            ? <span className="text-ok-text">{i18nT('pages.settings.browserPanel.test_ok')}</span>
            : <span className="text-text">{testResult.detail || i18nT('pages.settings.browserPanel.test_failed')}</span>}
        </div>
      )}
      {/* Hand-off on: nothing on this card is an unsaved draft. */}
      {testError && <ErrorNotice variant="inline" message={testError} askAgent />}
      {disconnectError && <ErrorNotice variant="inline" message={disconnectError} askAgent />}
    </div>
  )
}
