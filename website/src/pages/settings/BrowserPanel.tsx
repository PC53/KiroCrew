/**
 * Settings -> Browser (RFC browser-live-harness, section 8).
 *
 * Two shapes of the same page. Until the three setup steps are done or skipped
 * (`browser.first_run_dismissed`), the primary card is the step list (S2); after
 * that it is the status card (S1). They are ONE element: the card keeps its
 * `layoutId`, so the list grows into the status card instead of vanishing and
 * being replaced. Under reduced motion the layout animation is dropped and the
 * continuity stays.
 *
 * Below the primary card, in both shapes: Engine, Live view in chat, Local
 * preview, Advanced. The old Playwright CLI install, token and built-in-browser
 * toggle UI is gone with the stack it served.
 */
import { AnimatePresence, motion } from 'framer-motion'
import { useCallback, useState } from 'react'

import { SettingsCard, SettingsSection } from '../../components/settings'
import ErrorNotice from '../../components/ErrorNotice'
import { FormSkeleton } from '../../components/ui'
import { useReducedMotion } from '../../hooks/useReducedMotion'
import { i18nT } from '../../i18n/t'
import { sendErrorToChat } from '../../utils/errorReport'
import { BrowserEngineSection } from './BrowserEngineSection'
import { BrowserFirstRun, failText } from './BrowserFirstRun'
import { BrowserLiveSection, BrowserAdvancedSection } from './BrowserLiveSection'
import { BrowserLocalOrigins } from './BrowserLocalOrigins'
import { BrowserStatusCard } from './BrowserStatusCard'
import { useBrowserSettings } from './useBrowserSettings'

const PRIMARY_CARD_ID = 'settings-browser-primary-card'

export function BrowserPanel() {
  const s = useBrowserSettings()
  const reduceMotion = useReducedMotion()
  const { status, config, view, setConfig, patch, install, test, disconnect } = s
  // The step list stays until a click ends it (Skip, Try it, or Done): the last
  // step is a thing to try, not a state the gateway can report, so the page
  // never decides on its own that first run is over. The flag is written so a
  // reload lands on the status card too.
  const showFirstRun = !view.firstRunDismissed
  const [tried, setTried] = useState(false)

  const dismissFirstRun = useCallback(() => {
    if (!view.firstRunDismissed) setConfig('browser.first_run_dismissed', true)
  }, [view.firstRunDismissed, setConfig])

  const switchToChrome = useCallback(() => {
    // Pin the pick, then ask the gateway to attach: the attach is what makes
    // Chrome show its Allow prompt, and the status poll follows it from there.
    // `auto` already tries Chrome first, so only a pick that excludes it moves.
    if (view.choice !== 'user_chrome' && view.choice !== 'auto') setConfig('browser.choice', 'user_chrome')
    test.mutate()
  }, [view.choice, setConfig, test])

  const skipToFallback = useCallback(() => {
    setConfig('browser.choice', 'kiro_chromium')
    dismissFirstRun()
  }, [setConfig, dismissFirstRun])

  const tryIt = useCallback(() => {
    setTried(true)
    dismissFirstRun()
    // Stages the prompt in a chat composer and navigates there; nothing is sent
    // until the user presses Send.
    sendErrorToChat(i18nT('pages.settings.browserPanel.try_prompt'))
  }, [dismissFirstRun])

  if (status.isLoading || config.isLoading) {
    return (
      <SettingsSection title={i18nT('pages.settings.browserPanel.title')}>
        <SettingsCard>
          <FormSkeleton rows={['info', 'field']} />
        </SettingsCard>
      </SettingsSection>
    )
  }
  if (!status.data) {
    return (
      <SettingsSection title={i18nT('pages.settings.browserPanel.title')}>
        {/* Nothing to lose: no draft has mounted. */}
        <ErrorNotice
          message={failText(status.error, i18nT('pages.settings.browserPanel.cannot_load'))}
          askAgent
        />
      </SettingsSection>
    )
  }

  const installError = install.isError
    ? failText(install.error, i18nT('pages.settings.browserPanel.install_request_failed'))
    : null
  const testError = test.isError
    ? failText(test.error, i18nT('pages.settings.browserPanel.test_request_failed'))
    : null
  const disconnectError = disconnect.isError
    ? failText(disconnect.error, i18nT('pages.settings.browserPanel.disconnect_request_failed'))
    : null
  const saveError = patch.isError
    ? failText(patch.error, i18nT('pages.settings.browserPanel.save_failed'))
    : null
  const configUnavailable = config.isError

  return (
    <>
      <p className="text-[12px] text-muted mt-0 mb-3">{i18nT('pages.settings.browserPanel.subtitle')}</p>

      {/* One element, two forms. `layout` on the shared id animates the card's
          size between the step list and the status card; `mode="wait"` keeps the
          outgoing content from overlapping the incoming one. */}
      <SettingsSection title={showFirstRun
        ? i18nT('pages.settings.browserPanel.first_run_section')
        : i18nT('pages.settings.browserPanel.browser_in_use')}
      >
        <AnimatePresence mode="wait" initial={false}>
          <motion.div
            key={showFirstRun ? 'first-run' : 'status'}
            layoutId={PRIMARY_CARD_ID}
            layout={!reduceMotion}
            initial={reduceMotion ? false : { opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={reduceMotion ? undefined : { opacity: 0 }}
            transition={{ duration: reduceMotion ? 0 : 0.18 }}
            data-testid="browser-primary-card"
          >
            {showFirstRun ? (
              <SettingsCard>
                <BrowserFirstRun
                  status={status.data}
                  installing={install.isPending}
                  installError={installError}
                  onInstall={() => install.mutate()}
                  switching={test.isPending}
                  switchError={testError}
                  onSwitchToChrome={switchToChrome}
                  onSkip={skipToFallback}
                  onDone={dismissFirstRun}
                  onTry={tryIt}
                />
              </SettingsCard>
            ) : (
              <BrowserStatusCard
                status={status.data}
                testing={test.isPending}
                testResult={test.data ?? null}
                testError={testError}
                onTest={() => test.mutate()}
                disconnecting={disconnect.isPending}
                disconnectError={disconnectError}
                onDisconnect={() => disconnect.mutate()}
              />
            )}
          </motion.div>
        </AnimatePresence>
        {/* The status read failed after a first answer: what is shown may be stale. */}
        {status.isRefetchError && (
          <ErrorNotice
            variant="inline"
            className="mt-2"
            message={failText(status.error, i18nT('pages.settings.browserPanel.status_stale'))}
            askAgent
          />
        )}
        {tried && !showFirstRun && (
          <p className="text-[12px] text-muted mt-2 mb-0">{i18nT('pages.settings.browserPanel.try_sent')}</p>
        )}
      </SettingsSection>

      {!showFirstRun && (
        <BrowserEngineSection
          status={status.data}
          installing={install.isPending}
          installError={installError}
          onInstall={() => install.mutate()}
        />
      )}

      {configUnavailable && (
        // Hand-off on: no draft on this page yet (the origins field is below).
        <ErrorNotice
          variant="inline"
          className="mb-3"
          message={failText(config.error, i18nT('pages.settings.browserPanel.config_unavailable'))}
          askAgent
        />
      )}

      <BrowserLiveSection view={view} disabled={configUnavailable || patch.isPending} setConfig={setConfig} />

      {showFirstRun && (
        <SettingsSection title={i18nT('pages.settings.browserPanel.privacy_section')}>
          <SettingsCard>
            <BrowserPrivacyRow />
          </SettingsCard>
        </SettingsSection>
      )}

      <BrowserLocalOrigins
        origins={view.localOrigins}
        disabled={configUnavailable || patch.isPending}
        onChange={next => setConfig('dashboard.browser_local_origins', next)}
        saveError={saveError}
      />

      <BrowserAdvancedSection view={view} disabled={configUnavailable || patch.isPending} setConfig={setConfig} />
    </>
  )
}

/** The locked Privacy statement on its own, for the first-run shape where the
 *  Engine section (which carries it otherwise) is not yet shown. */
function BrowserPrivacyRow() {
  return (
    <div className="flex items-start gap-3 py-1" data-setting-label={i18nT('pages.settings.browserPanel.privacy_title')}>
      <div className="flex-1 min-w-0 flex flex-col gap-0.5">
        <span className="text-[13px] font-semibold text-text">{i18nT('pages.settings.browserPanel.privacy_title')}</span>
        <span className="text-[12px] text-muted">{i18nT('pages.settings.browserPanel.privacy_desc')}</span>
      </div>
      <span className="shrink-0 text-[12px] text-ok-text font-medium">{i18nT('pages.settings.browserPanel.locked')}</span>
    </div>
  )
}
