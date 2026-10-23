/**
 * Settings -> Browser -> Live view in chat, and -> Advanced: the four
 * `browser.live.*` controls and the `browser.choice` picker. Every control
 * carries its `configKey`, so a `<SettingRef>` chip can land on it.
 */
import { SettingsCard, SettingsSection, SettingsSelect, SettingsToggle } from '../../components/settings'
import { i18nT } from '../../i18n/t'
import { BROWSER_CHOICES, LIVE_MAX_FPS, LIVE_QUALITIES, type BrowserSettingsView } from './useBrowserSettings'

/** Catalog KEY per quality, flat literals for the key-reference gate. */
const QUALITY_LABEL_KEY: Record<(typeof LIVE_QUALITIES)[number], string> = {
  auto: 'pages.settings.browserPanel.quality_auto',
  high: 'pages.settings.browserPanel.quality_high',
  medium: 'pages.settings.browserPanel.quality_medium',
  low: 'pages.settings.browserPanel.quality_low',
}

const CHOICE_LABEL_KEY: Record<(typeof BROWSER_CHOICES)[number], string> = {
  auto: 'pages.settings.browserPanel.choice_auto',
  user_chrome: 'pages.settings.browserPanel.choice_user_chrome',
  kiro_chromium: 'pages.settings.browserPanel.choice_kiro_chromium',
  headless: 'pages.settings.browserPanel.choice_headless',
}

export function BrowserLiveSection({ view, disabled, setConfig }: {
  view: BrowserSettingsView
  disabled: boolean
  setConfig: (path: string, value: unknown) => void
}) {
  const fpsOptions = LIVE_MAX_FPS.map(String)
  return (
    <SettingsSection title={i18nT('pages.settings.browserPanel.live_title')}>
      <SettingsCard>
        <SettingsToggle
          label={i18nT('pages.settings.browserPanel.live_enabled_label')}
          description={i18nT('pages.settings.browserPanel.live_enabled_desc')}
          configKey="browser.live.enabled"
          checked={view.live.enabled}
          onChange={v => setConfig('browser.live.enabled', v)}
          disabled={disabled}
        />
        <SettingsSelect
          label={i18nT('pages.settings.browserPanel.quality_label')}
          description={i18nT('pages.settings.browserPanel.quality_desc')}
          configKey="browser.live.quality"
          value={view.live.quality}
          options={[...LIVE_QUALITIES]}
          optionLabels={LIVE_QUALITIES.map(q => i18nT(QUALITY_LABEL_KEY[q]))}
          onChange={v => setConfig('browser.live.quality', v)}
          disabled={disabled}
        />
        <SettingsSelect
          label={i18nT('pages.settings.browserPanel.max_fps_label')}
          description={i18nT('pages.settings.browserPanel.max_fps_desc')}
          configKey="browser.live.max_fps"
          value={String(view.live.maxFps)}
          options={fpsOptions}
          optionLabels={LIVE_MAX_FPS.map(n => i18nT('pages.settings.browserPanel.fps_value', { fps: n }))}
          onChange={v => setConfig('browser.live.max_fps', Number(v))}
          disabled={disabled}
        />
        <SettingsToggle
          label={i18nT('pages.settings.browserPanel.mask_passwords_label')}
          description={i18nT('pages.settings.browserPanel.mask_passwords_desc')}
          configKey="browser.live.mask_passwords"
          checked={view.live.maskPasswords}
          onChange={v => setConfig('browser.live.mask_passwords', v)}
          disabled={disabled}
        />
      </SettingsCard>
    </SettingsSection>
  )
}

export function BrowserAdvancedSection({ view, disabled, setConfig }: {
  view: BrowserSettingsView
  disabled: boolean
  setConfig: (path: string, value: unknown) => void
}) {
  return (
    <SettingsSection title={i18nT('pages.settings.browserPanel.advanced_title')}>
      <SettingsCard>
        <SettingsSelect
          label={i18nT('pages.settings.browserPanel.choice_label')}
          description={i18nT('pages.settings.browserPanel.choice_desc')}
          configKey="browser.choice"
          value={view.choice}
          options={[...BROWSER_CHOICES]}
          optionLabels={BROWSER_CHOICES.map(c => i18nT(CHOICE_LABEL_KEY[c]))}
          onChange={v => setConfig('browser.choice', v)}
          disabled={disabled}
        />
      </SettingsCard>
    </SettingsSection>
  )
}
