import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

/* ── api client mock ───────────────────────────────────────────────────────
 * The panel reads and writes only through these methods, so mocking them keeps
 * every case network-free. The config read answers the `browser` section and the
 * Preview origins; every write is the generic single-path PATCH. */
vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    api: {
      getBrowserStatus: vi.fn(),
      installBrowserHarness: vi.fn(),
      testBrowser: vi.fn(),
      disconnectBrowser: vi.fn(),
      kirocrewConfig: vi.fn(),
      patchConfig: vi.fn(),
    },
  }
})

/* The hand-off to a chat navigates away; here it is only observed. */
const sendErrorToChat = vi.fn(() => true)
vi.mock('../../utils/errorReport', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../utils/errorReport')>()),
  sendErrorToChat: (...args: unknown[]) => sendErrorToChat(...(args as [])),
}))

import { api, type BrowserStatusData } from '../../api/client'
import { i18next } from '../../i18n/all'
import { BrowserPanel } from './BrowserPanel'
import { firstRunProgress } from './browserStatus'

function status(overrides: Partial<BrowserStatusData> = {}): BrowserStatusData {
  return {
    installed: true,
    version: '0.1.13',
    browser: { kind: 'user_chrome', headless: false },
    connection: 'connected',
    detail: null,
    chromium: { downloaded: true, version: '156' },
    install_job: null,
    platform: 'darwin',
    ...overrides,
  }
}

type Cfg = {
  browser?: Record<string, unknown>
  dashboard?: { browser_local_origins?: string[] }
}

function config(overrides: Cfg = {}): Cfg {
  return {
    browser: { choice: 'auto', first_run_dismissed: false, live: { enabled: true, quality: 'auto', max_fps: 60, mask_passwords: true } },
    dashboard: { browser_local_origins: ['localhost:5173'] },
    ...overrides,
  }
}

const mocked = api as unknown as {
  getBrowserStatus: ReturnType<typeof vi.fn>
  installBrowserHarness: ReturnType<typeof vi.fn>
  testBrowser: ReturnType<typeof vi.fn>
  disconnectBrowser: ReturnType<typeof vi.fn>
  kirocrewConfig: ReturnType<typeof vi.fn>
  patchConfig: ReturnType<typeof vi.fn>
}

beforeEach(async () => {
  await i18next.changeLanguage('en')
  mocked.getBrowserStatus.mockReset().mockResolvedValue(status())
  mocked.installBrowserHarness.mockReset().mockResolvedValue(status({ install_job: { status: 'running', stage: 'venv' } }))
  mocked.testBrowser.mockReset().mockResolvedValue({ ok: true, connection: 'connected', detail: null })
  mocked.disconnectBrowser.mockReset().mockResolvedValue(status({ connection: 'disconnected', browser: null }))
  mocked.kirocrewConfig.mockReset().mockResolvedValue(config())
  mocked.patchConfig.mockReset().mockResolvedValue({ ok: true })
  sendErrorToChat.mockClear()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('firstRunProgress', () => {
  it('starts at step 1 with nothing installed', () => {
    const p = firstRunProgress(status({ installed: false, connection: 'disconnected', browser: null }))
    expect(p).toMatchObject({ engine: 'active', connect: 'pending', try: 'pending', doneCount: 0 })
  })

  it('moves to step 2 once the engine is installed, and infers remote debugging from the Allow prompt', () => {
    const p = firstRunProgress(status({ connection: 'waiting_allow', browser: null }))
    expect(p).toMatchObject({ engine: 'done', connect: 'active', try: 'pending', doneCount: 1 })
    expect(p.debuggingOn).toBe(true)
    expect(p.allowed).toBe(false)
  })

  it('treats a connected user browser as step 2 done and step 3 next', () => {
    const p = firstRunProgress(status())
    expect(p).toMatchObject({ engine: 'done', connect: 'done', try: 'active', doneCount: 2, debuggingOn: true, allowed: true })
  })

  it("treats Kiro's own Chromium as step 2 done too: there is nothing to allow", () => {
    const p = firstRunProgress(status({ browser: { kind: 'kiro_chromium', headless: true } }))
    expect(p.connect).toBe('done')
    expect(p.allowed).toBe(false)
  })

  it('knows nothing before the status answers', () => {
    expect(firstRunProgress(undefined)).toMatchObject({ engine: 'active', connect: 'pending', doneCount: 0 })
  })
})

describe('BrowserPanel - first run (S2)', () => {
  it('shows the three steps with the count of done ones, and the primary card is one element', async () => {
    mocked.getBrowserStatus.mockResolvedValue(status({ connection: 'disconnected', browser: null }))
    renderWithProviders(<BrowserPanel />)
    const card = await screen.findByTestId('browser-primary-card')
    expect(within(card).getByTestId('browser-first-run')).toBeTruthy()
    expect(within(card).getByText('1 of 3 done')).toBeTruthy()
    expect(within(card).getByTestId('browser-switch-chrome')).toBeTruthy()
    expect(within(card).getByTestId('browser-skip')).toBeTruthy()
    expect(screen.queryByTestId('browser-status-card')).toBeNull()
  })

  it('installs the engine from step 1 and shows the job stage', async () => {
    mocked.getBrowserStatus.mockResolvedValue(status({ installed: false, version: null, connection: 'disconnected', browser: null, chromium: null }))
    renderWithProviders(<BrowserPanel />)
    const install = await screen.findByTestId('browser-install')
    // After the POST the gateway reports the running job on every status read.
    mocked.getBrowserStatus.mockResolvedValue(status({ installed: false, version: null, connection: 'disconnected', browser: null, chromium: null, install_job: { status: 'running', stage: 'venv' } }))
    fireEvent.click(install)
    await waitFor(() => expect(mocked.installBrowserHarness).toHaveBeenCalledTimes(1))
    // The POST's answer carries the running job, which the panel paints at once.
    await screen.findByText('venv')
  })

  it('marks the Allow sub-row as waiting and keeps the step open until Chrome answers', async () => {
    mocked.getBrowserStatus.mockResolvedValue(status({ connection: 'waiting_allow', browser: null }))
    renderWithProviders(<BrowserPanel />)
    await screen.findByText('Waiting for you')
    expect(screen.getByTestId('browser-first-run')).toBeTruthy()
  })

  it('Switch to Chrome asks the gateway to attach, and a prior fallback pick is moved back to Chrome', async () => {
    mocked.getBrowserStatus.mockResolvedValue(status({ connection: 'disconnected', browser: null }))
    mocked.kirocrewConfig.mockResolvedValue(config({ browser: { choice: 'kiro_chromium', first_run_dismissed: false } }))
    renderWithProviders(<BrowserPanel />)
    fireEvent.click(await screen.findByTestId('browser-switch-chrome'))
    await waitFor(() => expect(mocked.testBrowser).toHaveBeenCalledTimes(1))
    expect(mocked.patchConfig).toHaveBeenCalledWith('browser.choice', 'user_chrome')
  })

  it('Skip pins the fallback browser and dismisses the steps for good', async () => {
    mocked.getBrowserStatus.mockResolvedValue(status({ connection: 'disconnected', browser: null }))
    renderWithProviders(<BrowserPanel />)
    fireEvent.click(await screen.findByTestId('browser-skip'))
    await waitFor(() => {
      expect(mocked.patchConfig).toHaveBeenCalledWith('browser.choice', 'kiro_chromium')
      expect(mocked.patchConfig).toHaveBeenCalledWith('browser.first_run_dismissed', true)
    })
  })

  it('step 3 stays open once Chrome is connected; Try it stages the prompt and dismisses the steps', async () => {
    renderWithProviders(<BrowserPanel />)
    const tryBtn = await screen.findByTestId('browser-try')
    expect(tryBtn).not.toBeDisabled()
    expect(screen.getByText('2 of 3 done')).toBeTruthy()
    fireEvent.click(tryBtn)
    expect(sendErrorToChat).toHaveBeenCalledTimes(1)
    expect(String(sendErrorToChat.mock.calls[0][0])).toContain('example.com')
    await waitFor(() => expect(mocked.patchConfig).toHaveBeenCalledWith('browser.first_run_dismissed', true))
  })

  it('Done leaves the steps for the status card without a hand-off, in the SAME card element', async () => {
    renderWithProviders(<BrowserPanel />)
    const before = await screen.findByTestId('browser-primary-card')
    mocked.kirocrewConfig.mockResolvedValue(config({ browser: { first_run_dismissed: true } }))
    fireEvent.click(screen.getByTestId('browser-first-run-done'))
    expect(sendErrorToChat).not.toHaveBeenCalled()
    await waitFor(() => expect(mocked.patchConfig).toHaveBeenCalledWith('browser.first_run_dismissed', true))
    await screen.findByTestId('browser-status-card')
    const after = screen.getByTestId('browser-primary-card')
    expect(after.getAttribute('data-testid')).toBe(before.getAttribute('data-testid'))
    expect(screen.queryByTestId('browser-first-run')).toBeNull()
  })

  it('a config that already dismissed the steps opens on the status card', async () => {
    mocked.kirocrewConfig.mockResolvedValue(config({ browser: { first_run_dismissed: true } }))
    renderWithProviders(<BrowserPanel />)
    const card = await screen.findByTestId('browser-status-card')
    expect(card.getAttribute('data-connection')).toBe('connected')
    expect(screen.queryByTestId('browser-first-run')).toBeNull()
  })
})

describe('BrowserPanel - status card (S1)', () => {
  beforeEach(() => {
    mocked.kirocrewConfig.mockResolvedValue(config({ browser: { first_run_dismissed: true } }))
  })

  it('shows the browser in use, its connection, and lights its chip in the fallback order', async () => {
    renderWithProviders(<BrowserPanel />)
    const card = await screen.findByTestId('browser-status-card')
    expect(within(card).getAllByText('Your Chrome').length).toBeGreaterThan(0)
    expect(within(card).getByTestId('browser-connection-badge').textContent).toBe('Connected')
    expect(within(card).getByTestId('browser-chip-user_chrome').getAttribute('aria-current')).toBe('true')
    expect(within(card).getByTestId('browser-chip-kiro_chromium').getAttribute('aria-current')).toBeNull()
    // Not Windows: the Edge rung is shown but dimmed.
    expect(within(card).getByTestId('browser-chip-edge').className).toContain('opacity-60')
  })

  it('turns yellow while Chrome waits for Allow, and says so', async () => {
    mocked.getBrowserStatus.mockResolvedValue(status({ connection: 'waiting_allow' }))
    renderWithProviders(<BrowserPanel />)
    const card = await screen.findByTestId('browser-status-card')
    expect(card.getAttribute('data-connection')).toBe('waiting_allow')
    expect(card.className).toContain('border-warn')
    expect(within(card).getByTestId('browser-connection-badge').textContent).toBe('Waiting for Allow in Chrome')
    expect(within(card).getByText(/Click Allow there; this never times out/)).toBeTruthy()
  })

  it('shows the gateway detail verbatim on an error, and the Edge chip undimmed on Windows', async () => {
    mocked.getBrowserStatus.mockResolvedValue(status({ connection: 'error', detail: 'CDP handshake refused (ECONNRESET)', platform: 'win32' }))
    renderWithProviders(<BrowserPanel />)
    const card = await screen.findByTestId('browser-status-card')
    expect(within(card).getByTestId('browser-status-detail').textContent).toBe('CDP handshake refused (ECONNRESET)')
    expect(within(card).getByTestId('browser-chip-edge').className).not.toContain('opacity-60')
  })

  it('Test runs the connection test and reports its answer; Disconnect detaches', async () => {
    renderWithProviders(<BrowserPanel />)
    const card = await screen.findByTestId('browser-status-card')
    fireEvent.click(within(card).getByTestId('browser-test'))
    await waitFor(() => expect(mocked.testBrowser).toHaveBeenCalledTimes(1))
    await screen.findByTestId('browser-test-result')
    mocked.getBrowserStatus.mockResolvedValue(status({ connection: 'disconnected', browser: null }))
    fireEvent.click(within(card).getByTestId('browser-disconnect'))
    await waitFor(() => expect(mocked.disconnectBrowser).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByTestId('browser-status-card').getAttribute('data-connection')).toBe('disconnected'))
    expect(screen.getByTestId('browser-disconnect')).toBeDisabled()
  })

  it('renders the Engine rows with versions and the locked Privacy row', async () => {
    renderWithProviders(<BrowserPanel />)
    await screen.findByTestId('browser-status-card')
    expect(screen.getByText('0.1.13')).toBeTruthy()
    expect(screen.getByText('156')).toBeTruthy()
    expect(screen.getByText('No usage data, no cloud browser')).toBeTruthy()
    expect(screen.getByText('Locked')).toBeTruthy()
  })
})

describe('BrowserPanel - live view, origins editor, advanced', () => {
  beforeEach(() => {
    mocked.kirocrewConfig.mockResolvedValue(config({ browser: { first_run_dismissed: true } }))
  })

  it('every live-view and advanced control writes ONE config path', async () => {
    renderWithProviders(<BrowserPanel />)
    await screen.findByTestId('browser-status-card')
    const liveToggle = screen.getByRole('switch', { name: 'Show the live view in chat' })
    fireEvent.click(liveToggle)
    await waitFor(() => expect(mocked.patchConfig).toHaveBeenCalledWith('browser.live.enabled', false))
    const mask = screen.getByRole('switch', { name: 'Mask password fields in the picture' })
    fireEvent.click(mask)
    await waitFor(() => expect(mocked.patchConfig).toHaveBeenCalledWith('browser.live.mask_passwords', false))
    // Every control carries its configKey, which is what SettingRef lands on.
    for (const key of ['browser.live.enabled', 'browser.live.quality', 'browser.live.max_fps', 'browser.live.mask_passwords', 'browser.choice', 'dashboard.browser_local_origins']) {
      expect(document.querySelector(`[data-setting-key="${key}"]`), key).not.toBeNull()
    }
  })

  it('lists the origins as chips, adds a normalized entry, refuses a bad one, and removes', async () => {
    renderWithProviders(<BrowserPanel />)
    await screen.findByTestId('browser-status-card')
    expect(screen.getByText('localhost:5173')).toBeTruthy()
    const input = screen.getByPlaceholderText('localhost:5173') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'http://127.0.0.1:3000/' } })
    fireEvent.click(screen.getByTestId('browser-origin-add'))
    await waitFor(() => expect(mocked.patchConfig).toHaveBeenCalledWith('dashboard.browser_local_origins', ['localhost:5173', '127.0.0.1:3000']))
    expect(input.value).toBe('')

    fireEvent.change(input, { target: { value: 'example.com:80' } })
    fireEvent.click(screen.getByTestId('browser-origin-add'))
    expect(screen.getByTestId('browser-origin-invalid')).toBeTruthy()
    expect(input.getAttribute('aria-invalid')).toBe('true')
    expect(mocked.patchConfig).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Remove localhost:5173' }))
    await waitFor(() => expect(mocked.patchConfig).toHaveBeenCalledWith('dashboard.browser_local_origins', []))
  })

  it('a duplicate add writes nothing', async () => {
    renderWithProviders(<BrowserPanel />)
    await screen.findByTestId('browser-status-card')
    const input = screen.getByPlaceholderText('localhost:5173')
    fireEvent.change(input, { target: { value: 'LOCALHOST:5173' } })
    fireEvent.click(screen.getByTestId('browser-origin-add'))
    expect(mocked.patchConfig).not.toHaveBeenCalled()
  })
})

describe('BrowserPanel - failures', () => {
  it('a failed first status read says so with a hand-off', async () => {
    mocked.getBrowserStatus.mockRejectedValue(new Error('gateway down'))
    renderWithProviders(<BrowserPanel />)
    await screen.findByText('gateway down')
    expect(screen.queryByTestId('browser-primary-card')).toBeNull()
  })

  it('a config read failure disables the config controls but keeps the status', async () => {
    mocked.kirocrewConfig.mockRejectedValue(new Error('config unreadable'))
    renderWithProviders(<BrowserPanel />)
    await screen.findByText('config unreadable')
    expect(screen.getByRole('switch', { name: 'Show the live view in chat' }).getAttribute('aria-disabled')).toBe('true')
  })

  it('re-renders its copy on a language switch without remounting', async () => {
    mocked.kirocrewConfig.mockResolvedValue(config({ browser: { first_run_dismissed: true } }))
    const { rerender } = renderWithProviders(<BrowserPanel />)
    const card = await screen.findByTestId('browser-status-card')
    await act(async () => { await i18next.changeLanguage('zh-CN') })
    rerender(<BrowserPanel />)
    expect(screen.getAllByText('你的 Chrome').length).toBeGreaterThan(0)
    // Same element: the panel re-rendered rather than remounting.
    expect(screen.getByTestId('browser-status-card')).toBe(card)
  })
})
