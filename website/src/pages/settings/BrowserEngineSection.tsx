/**
 * Settings -> Browser -> Engine: the Browser Harness version, Kiro's Chromium
 * with Redownload, and the locked Privacy row.
 *
 * The Privacy row is a statement, not a control: the harness's telemetry and the
 * Browser Use cloud are forced off by the environment fence at daemon start
 * (RFC section 1), and there is deliberately no key to turn either back on.
 */
import { Download, Loader2, Lock, Monitor, Package, ShieldCheck } from 'lucide-react'
import type { ReactNode } from 'react'

import type { BrowserStatusData } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsCard, SettingsSection } from '../../components/settings'
import { Badge, Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'

function Row({ icon, title, meta, desc, trailing }: {
  icon: ReactNode
  title: string
  meta?: string | null
  desc: string
  trailing: ReactNode
}) {
  return (
    <div className="flex items-start gap-3 py-2.5" data-setting-label={title}>
      <span className="text-muted shrink-0 mt-0.5" aria-hidden>{icon}</span>
      <div className="flex-1 min-w-0 flex flex-col gap-0.5">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-[13px] font-semibold text-text">{title}</span>
          {meta && <span className="font-mono text-[12px] text-muted">{meta}</span>}
        </div>
        <div className="text-[12px] text-muted">{desc}</div>
      </div>
      <div className="shrink-0 flex items-center gap-2">{trailing}</div>
    </div>
  )
}

export function BrowserEngineSection({ status, installing, installError, onInstall }: {
  status: BrowserStatusData
  installing: boolean
  installError: string | null
  onInstall: () => void
}) {
  const jobRunning = installing || status.install_job?.status === 'running'
  const chromium = status.chromium ?? null
  return (
    <SettingsSection title={i18nT('pages.settings.browserPanel.engine_title')}>
      <SettingsCard>
        <div className="divide-y divide-border">
          <Row
            icon={<Package size={16} />}
            title={i18nT('pages.settings.browserPanel.engine_harness')}
            meta={status.version}
            desc={i18nT('pages.settings.browserPanel.engine_harness_desc')}
            trailing={status.installed
              ? <Badge variant="ok">{i18nT('pages.settings.browserPanel.installed')}</Badge>
              : (
                <Btn primary onClick={onInstall} disabled={jobRunning}>
                  {jobRunning && <Loader2 size={13} className="lucide-inline animate-spin" aria-hidden />}
                  {jobRunning
                    ? i18nT('pages.settings.browserPanel.installing')
                    : i18nT('pages.settings.browserPanel.install_engine')}
                </Btn>
              )}
          />
          <Row
            icon={<Monitor size={16} />}
            title={i18nT('pages.settings.browserPanel.engine_kiro_chromium')}
            meta={chromium?.version ?? null}
            desc={i18nT('pages.settings.browserPanel.engine_kiro_chromium_desc')}
            trailing={(
              <>
                {chromium?.downloaded
                  ? <Badge variant="ok">{i18nT('pages.settings.browserPanel.downloaded')}</Badge>
                  : <Badge variant="muted">{i18nT('pages.settings.browserPanel.not_downloaded')}</Badge>}
                <Btn onClick={onInstall} disabled={jobRunning} aria-label={i18nT('pages.settings.browserPanel.redownload')}>
                  {jobRunning
                    ? <Loader2 size={13} className="lucide-inline animate-spin" aria-hidden />
                    : <Download size={13} className="lucide-inline" aria-hidden />}
                  {chromium?.downloaded
                    ? i18nT('pages.settings.browserPanel.redownload')
                    : i18nT('pages.settings.browserPanel.download')}
                </Btn>
              </>
            )}
          />
          <Row
            icon={<ShieldCheck size={16} />}
            title={i18nT('pages.settings.browserPanel.privacy_title')}
            desc={i18nT('pages.settings.browserPanel.privacy_desc')}
            trailing={(
              <Badge variant="ok">
                <Lock size={11} className="lucide-inline" aria-hidden /> {i18nT('pages.settings.browserPanel.locked')}
              </Badge>
            )}
          />
        </div>
        {/* Hand-off on: no draft on this card. */}
        {installError && <ErrorNotice variant="inline" className="mt-2" message={installError} askAgent />}
      </SettingsCard>
    </SettingsSection>
  )
}
