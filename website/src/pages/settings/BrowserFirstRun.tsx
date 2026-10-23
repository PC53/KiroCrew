/**
 * Settings -> Browser, first run (RFC browser-live-harness, section 8, design S2):
 * three steps, until all are done or the user skips. Step 1 installs the engine,
 * step 2 connects the user's own Chrome (or says that Kiro's Chromium is in use
 * where there is no Chrome), step 3 opens a chat to see the live card.
 *
 * Every fact a step shows comes from `firstRunProgress` over the gateway's status,
 * so the steps and the status card that replaces them agree.
 */
import { AlertTriangle, Check, Globe, Loader2, Play, SkipForward } from 'lucide-react'
import type { ReactNode } from 'react'

import type { BrowserStatusData } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { Badge, Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { firstRunProgress, type StepState } from './browserStatus'

/** The Chrome page where remote debugging is enabled. A URL the user types into
 *  Chrome, so it is never translated. */
const REMOTE_DEBUGGING_PAGE = 'chrome://inspect/#remote-debugging'

/** Human text for a rejected call: the `ApiError` message is what the error
 *  journal is keyed on, so `ErrorNotice` recovers the endpoint from it. */
export const failText = (e: unknown, fallback: string): string =>
  e instanceof Error && e.message ? e.message : fallback

function StepMarker({ state, index }: { state: StepState; index: number }) {
  if (state === 'done') {
    return (
      <span className="flex items-center justify-center w-6 h-6 rounded-full shrink-0 bg-ok text-white" aria-hidden>
        <Check size={13} />
      </span>
    )
  }
  const cls = state === 'active'
    ? 'bg-accent text-white'
    : 'bg-bg-elevated border border-border text-muted'
  return (
    <span className={`flex items-center justify-center w-6 h-6 rounded-full shrink-0 text-[12px] font-semibold ${cls}`} aria-hidden>
      {index}
    </span>
  )
}

function Step({ state, index, title, children, trailing }: {
  state: StepState
  index: number
  title: string
  children: ReactNode
  trailing?: ReactNode
}) {
  const stateLabel = state === 'done'
    ? i18nT('pages.settings.browserPanel.step_state_done')
    : state === 'active'
      ? i18nT('pages.settings.browserPanel.step_state_active')
      : i18nT('pages.settings.browserPanel.step_state_pending')
  return (
    <li className={`flex items-start gap-3 py-3 ${state === 'pending' ? 'opacity-70' : ''}`} aria-label={`${title}: ${stateLabel}`}>
      <StepMarker state={state} index={index} />
      <div className="flex-1 min-w-0 flex flex-col gap-1.5">
        <div className="text-[13px] font-semibold text-text">{title}</div>
        {children}
      </div>
      {trailing && <div className="shrink-0 flex items-start gap-2">{trailing}</div>}
    </li>
  )
}

function SubRow({ ok, waiting, children }: { ok: boolean; waiting?: boolean; children: ReactNode }) {
  return (
    <div className="flex items-center gap-2 text-[12px] text-text">
      {ok
        ? <Check size={13} className="lucide-inline shrink-0 text-ok-text" aria-hidden />
        : waiting
          ? <AlertTriangle size={13} className="lucide-inline shrink-0 text-warn-text" aria-hidden />
          : <span className="inline-block w-[13px] h-[13px] rounded-full border border-border shrink-0" aria-hidden />}
      <span className="min-w-0">{children}</span>
      {waiting && !ok && (
        <Badge variant="warn">{i18nT('pages.settings.browserPanel.waiting_for_you')}</Badge>
      )}
    </div>
  )
}

export interface BrowserFirstRunProps {
  status: BrowserStatusData | undefined
  installing: boolean
  installError: string | null
  onInstall: () => void
  switching: boolean
  switchError: string | null
  onSwitchToChrome: () => void
  onSkip: () => void
  /** Leave the steps for the status card without trying: for a reader who already knows. */
  onDone: () => void
  onTry: () => void
}

export function BrowserFirstRun({
  status, installing, installError, onInstall, switching, switchError, onSwitchToChrome, onSkip, onDone, onTry,
}: BrowserFirstRunProps) {
  const p = firstRunProgress(status)
  const job = status?.install_job
  const jobRunning = installing || job?.status === 'running'
  const jobFailed = !jobRunning && job?.status === 'failed'
  const engineLine = status?.installed
    ? i18nT('pages.settings.browserPanel.step_engine_done', {
      version: status.version ?? '',
      chromium: status.chromium?.version ?? '',
    })
    : i18nT('pages.settings.browserPanel.step_engine_todo')

  return (
    <div data-testid="browser-first-run">
      <div className="flex items-center gap-2 mb-1">
        <h3 className="text-[15px] font-semibold text-text m-0">{i18nT('pages.settings.browserPanel.first_run_title')}</h3>
        <Badge variant="aim">{i18nT('pages.settings.browserPanel.steps_done', { done: p.doneCount, total: 3 })}</Badge>
      </div>
      <ol className="list-none m-0 p-0 divide-y divide-border" aria-label={i18nT('pages.settings.browserPanel.first_run_title')}>
        <Step
          state={p.engine}
          index={1}
          title={i18nT('pages.settings.browserPanel.step_engine_title')}
          trailing={p.engine === 'done'
            ? <Badge variant="ok">{i18nT('pages.settings.browserPanel.step_state_done')}</Badge>
            : (
              <Btn primary onClick={onInstall} disabled={jobRunning} data-testid="browser-install">
                {jobRunning ? <Loader2 size={13} className="lucide-inline animate-spin" aria-hidden /> : null}
                {jobRunning
                  ? i18nT('pages.settings.browserPanel.installing')
                  : i18nT('pages.settings.browserPanel.install_engine')}
              </Btn>
            )}
        >
          <div className="text-[12px] text-muted">{engineLine}</div>
          {jobRunning && job?.stage && (
            <div className="text-[12px] text-muted" aria-live="polite">{job.stage}</div>
          )}
          {/* The gateway's own words for a failed job, never translated; the
              generic line only when it gave none. Hand-off on: nothing on this
              page is an unsaved draft. */}
          {(jobFailed || installError) && (
            <ErrorNotice
              variant="inline"
              message={installError ?? job?.error_detail ?? i18nT('pages.settings.browserPanel.install_failed')}
              askAgent
            />
          )}
        </Step>

        <Step
          state={p.connect}
          index={2}
          title={i18nT('pages.settings.browserPanel.step_connect_title')}
        >
          <div className="text-[12px] text-muted">
            {status?.browser?.kind === 'kiro_chromium' && p.connect === 'done'
              ? i18nT('pages.settings.browserPanel.step_connect_kiro_chromium')
              : i18nT('pages.settings.browserPanel.step_connect_desc')}
          </div>
          {p.connect !== 'pending' && status?.browser?.kind !== 'kiro_chromium' && (
            <>
              <SubRow ok={p.debuggingOn}>
                {i18nT('pages.settings.browserPanel.step_connect_open_tick', { url: REMOTE_DEBUGGING_PAGE })}
              </SubRow>
              <SubRow ok={p.allowed} waiting={status?.connection === 'waiting_allow'}>
                {i18nT('pages.settings.browserPanel.step_connect_allow')}
              </SubRow>
            </>
          )}
          {p.connect === 'active' && (
            <div className="flex flex-wrap items-center gap-2 pt-1">
              <Btn primary onClick={onSwitchToChrome} disabled={switching} data-testid="browser-switch-chrome">
                {switching
                  ? <Loader2 size={13} className="lucide-inline animate-spin" aria-hidden />
                  : <Globe size={13} className="lucide-inline" aria-hidden />}
                {i18nT('pages.settings.browserPanel.switch_to_chrome')}
              </Btn>
              <Btn onClick={onSkip} data-testid="browser-skip">
                <SkipForward size={13} className="lucide-inline" aria-hidden />
                {i18nT('pages.settings.browserPanel.skip_use_fallback')}
              </Btn>
            </div>
          )}
          {switchError && (
            <ErrorNotice variant="inline" message={switchError} askAgent />
          )}
        </Step>

        <Step
          state={p.try}
          index={3}
          title={i18nT('pages.settings.browserPanel.step_try_title')}
          trailing={(
            <>
              <Btn primary onClick={onTry} disabled={p.try !== 'active'} data-testid="browser-try">
                <Play size={13} className="lucide-inline" aria-hidden />
                {i18nT('pages.settings.browserPanel.try_it')}
              </Btn>
              {p.try === 'active' && (
                <Btn onClick={onDone} data-testid="browser-first-run-done">
                  {i18nT('pages.settings.browserPanel.first_run_done')}
                </Btn>
              )}
            </>
          )}
        >
          <div className="text-[12px] text-muted">{i18nT('pages.settings.browserPanel.step_try_desc')}</div>
        </Step>
      </ol>
    </div>
  )
}
