/**
 * Settings -> Browser -> Local preview: the `dashboard.browser_local_origins`
 * editor. Chips with a remove button, plus an add field that accepts
 * `localhost:5173` or a pasted `http://localhost:5173/`.
 *
 * The list is written as ONE value (the whole array) because that is the shape
 * the PATCH route stores; a malformed entry is refused here, before the round
 * trip, with the same rule the gateway applies (`normalizeLocalOrigin`).
 */
import { Laptop, Plus, X } from 'lucide-react'
import { useId, useState } from 'react'

import ErrorNotice from '../../components/ErrorNotice'
import { SettingsCard, SettingsField, SettingsSection } from '../../components/settings'
import { Btn, Input } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { normalizeLocalOrigin } from '../../utils/localPreviewOrigins'

/** Mirrors the gateway's `BROWSER_LOCAL_ORIGINS_MAX`. */
export const LOCAL_ORIGINS_MAX = 32

export function BrowserLocalOrigins({ origins, disabled, onChange, saveError }: {
  origins: string[]
  disabled: boolean
  onChange: (next: string[]) => void
  saveError: string | null
}) {
  const [draft, setDraft] = useState('')
  const [invalid, setInvalid] = useState(false)
  const inputId = useId()
  const invalidId = useId()
  const full = origins.length >= LOCAL_ORIGINS_MAX

  const add = () => {
    const origin = normalizeLocalOrigin(draft)
    if (!origin) { setInvalid(true); return }
    setInvalid(false)
    setDraft('')
    if (origins.includes(origin)) return
    onChange([...origins, origin])
  }
  const remove = (origin: string) => onChange(origins.filter(o => o !== origin))

  return (
    <SettingsSection title={i18nT('pages.settings.browserPanel.local_preview_title')}>
      <SettingsCard>
        <div className="flex items-start gap-3">
          <Laptop size={16} className="text-muted shrink-0 mt-2" aria-hidden />
          <div className="flex-1 min-w-0">
            <SettingsField
              label={i18nT('pages.settings.browserPanel.origins_label')}
              description={i18nT('pages.settings.browserPanel.origins_desc')}
              configKey="dashboard.browser_local_origins"
              controlId={inputId}
            >
              <ul className="list-none m-0 p-0 flex flex-wrap items-center gap-1.5" aria-label={i18nT('pages.settings.browserPanel.origins_label')}>
                {origins.map(o => (
                  <li key={o} className="inline-flex items-center gap-1 rounded-md border border-border bg-bg-elevated pl-2 pr-1 py-0.5 font-mono text-[12px] text-text">
                    <span>{o}</span>
                    <Btn
                      onClick={() => remove(o)}
                      disabled={disabled}
                      aria-label={i18nT('pages.settings.browserPanel.remove_origin', { origin: o })}
                      className="!px-1 !py-0.5"
                    >
                      <X size={12} className="lucide-inline" aria-hidden />
                    </Btn>
                  </li>
                ))}
                {origins.length === 0 && (
                  <li className="text-[12px] text-muted">{i18nT('pages.settings.browserPanel.origins_empty')}</li>
                )}
              </ul>
              <form
                className="flex items-center gap-2 pt-1"
                onSubmit={e => { e.preventDefault(); add() }}
              >
                <Input
                  id={inputId}
                  value={draft}
                  onChange={e => { setDraft(e.target.value); if (invalid) setInvalid(false) }}
                  placeholder="localhost:5173"
                  disabled={disabled || full}
                  spellCheck={false}
                  autoCorrect="off"
                  autoCapitalize="off"
                  aria-invalid={invalid || undefined}
                  aria-describedby={invalid ? invalidId : undefined}
                  className="font-mono text-[12px] max-w-[260px]"
                />
                <Btn type="submit" disabled={disabled || full || !draft.trim()} data-testid="browser-origin-add">
                  <Plus size={13} className="lucide-inline" aria-hidden />
                  {i18nT('pages.settings.browserPanel.add_origin')}
                </Btn>
              </form>
              {/* A validation hint, not an error: nothing was sent. Plain text,
                  no alert role. */}
              {invalid && (
                <p id={invalidId} className="text-[12px] text-warn-text m-0" data-testid="browser-origin-invalid">
                  {i18nT('pages.settings.browserPanel.origin_invalid')}
                </p>
              )}
              {full && (
                <p className="text-[12px] text-muted m-0">
                  {i18nT('pages.settings.browserPanel.origins_full', { count: LOCAL_ORIGINS_MAX })}
                </p>
              )}
            </SettingsField>
          </div>
        </div>
        {/* No hand-off: the add field beside this notice may hold an unsaved draft. */}
        {saveError && <ErrorNotice variant="inline" className="mt-2" message={saveError} />}
      </SettingsCard>
    </SettingsSection>
  )
}
