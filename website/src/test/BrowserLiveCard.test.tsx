import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import i18next from 'i18next'
import '../i18n/all'

import BrowserLiveCard, { browserLiveAnchor, isBrowserToolMessage } from '../pages/chat/BrowserLiveCard'
import TurnBlock from '../pages/chat/TurnBlock'
import ChatFooter from '../pages/chat/ChatFooter'
import type { DisplayItem, TurnItem } from '../pages/chat/types'
import { browserLiveStore } from '../hooks/useBrowserLive'
import type { DecodedBrowserFrame } from '../hooks/useBrowserLive.frame'

/**
 * BrowserLiveCard, driven through the store exactly as the websocket drives it.
 *
 * The card is one element through every state; the tests assert the face each
 * state shows, the writes each control posts, the hand-back key, and that
 * TurnBlock mounts the card once per turn after the turn's last browser row
 * (and not at all for a host that cannot name the session).
 */

const S = 'slot:abc'
const ev = (over: Record<string, unknown> = {}) => ({ session_key: S, turn_id: 't1', state: 'live', url: 'https://jspaint.app/', title: 'untitled - Paint', browser: 'user_chrome', headless: false, steps: [{ label: 'open jspaint', status: 'done' }, { label: 'draw roof', status: 'active' }, { label: 'screenshot', status: 'pending' }], step: { index: 1, label: 'click · polygon tool' }, ...over })
const frame = (seq: number): DecodedBrowserFrame => ({
  header: { type: 'browser_frame', session_key: S, seq, w: 640, h: 400, mask: [] },
  jpeg: new Uint8Array([1, 2, 3]),
  receivedAt: seq,
})

type Writes = NonNullable<Parameters<typeof browserLiveStore.__setWritesForTests>[0]>
let writes: Required<Writes>
let restore: ReturnType<typeof browserLiveStore.__setWritesForTests>

/** The observer the card uses to report visibility; tests fire it by hand. */
let observed: { cb: IntersectionObserverCallback; el: Element }[] = []
class FakeIO {
  constructor(private cb: IntersectionObserverCallback) {}
  observe(el: Element) { observed.push({ cb: this.cb, el }) }
  disconnect() { observed = observed.filter(o => o.cb !== this.cb) }
  unobserve() {}
  takeRecords() { return [] }
  root = null
  rootMargin = ''
  thresholds = []
}
function intersect(visible: boolean) {
  for (const o of observed) o.cb([{ isIntersecting: visible, target: o.el } as IntersectionObserverEntry], o as unknown as IntersectionObserver)
}

beforeEach(() => {
  browserLiveStore.__resetForTests()
  writes = {
    ack: vi.fn(async () => ({ ok: true })),
    visibility: vi.fn(async () => ({ ok: true })),
    claim: vi.fn(async () => ({ ok: true })),
    release: vi.fn(async () => ({ ok: true })),
    input: vi.fn(async () => ({ ok: true })),
    open: vi.fn(async () => ({ ok: true })),
  }
  restore = browserLiveStore.__setWritesForTests(writes)
  observed = []
  vi.stubGlobal('IntersectionObserver', FakeIO)
  // happy-dom has no 2D canvas; the painter is exercised by its own tests.
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
})
afterEach(() => {
  browserLiveStore.__setWritesForTests(restore)
  browserLiveStore.__resetForTests()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const card = (running = true) => <BrowserLiveCard sessionKey={S} anchorKey="call:1" anchorTs={null} running={running} />

describe('BrowserLiveCard faces', () => {
  it('renders nothing for a turn the relay has not spoken about', () => {
    const { container } = render(card())
    expect(container.querySelector('[data-testid="browser-live-card"]')).toBeNull()
  })

  it('waiting_allow: names the browser, the one step, and offers the switch', () => {
    act(() => browserLiveStore.applyEvent(ev({ state: 'waiting_allow' })))
    render(card())
    expect(screen.getByTestId('browser-live-card')).toHaveAttribute('data-state', 'waiting_allow')
    expect(screen.getByText(i18next.t('pages.chat.browserLiveCard.waiting_for_allow'))).toBeInTheDocument()
    expect(screen.getByText(i18next.t('pages.chat.browserLiveCard.wants_browser', { browser: i18next.t('pages.chat.browserLiveCard.browser_user_chrome') }))).toBeInTheDocument()
    fireEvent.click(screen.getByText(i18next.t('pages.chat.browserLiveCard.switch_to', { browser: 'Chrome' })))
    expect(writes.open).toHaveBeenCalledWith(S)
  })

  it('live: address, browser chip, step strip, current action, Take over', () => {
    act(() => browserLiveStore.applyEvent(ev()))
    render(card())
    // The header row holds Take over and one overflow menu trigger: two controls.
    expect(screen.getByTestId('browser-live-live').querySelector('.border-b')!.querySelectorAll('button')).toHaveLength(2)
    expect(screen.getByLabelText(i18next.t('pages.chat.browserLiveCard.more_actions'))).toBeInTheDocument()
    expect(screen.getByTestId('browser-live-card')).toHaveAttribute('data-state', 'live')
    expect(screen.getByText('jspaint.app')).toBeInTheDocument()
    expect(screen.getByText('· untitled - Paint')).toBeInTheDocument()
    expect(screen.getByText(i18next.t('pages.chat.browserLiveCard.browser_user_chrome'))).toBeInTheDocument()
    expect(screen.getByText('click · polygon tool')).toBeInTheDocument()
    const steps = screen.getByRole('list', { name: i18next.t('pages.chat.browserLiveCard.steps') })
    expect(steps.querySelectorAll('li')).toHaveLength(3)
    expect(steps.querySelector('[aria-current="step"]')).toHaveTextContent('draw roof')
    // No frame yet: the placeholder, not a canvas.
    expect(screen.getByText(i18next.t('pages.chat.browserLiveCard.waiting_for_first_frame'))).toBeInTheDocument()
    expect(screen.queryByTestId('browser-live-canvas')).toBeNull()
  })

  it('the first frame swaps the placeholder for the canvas sized to the frame', () => {
    act(() => browserLiveStore.applyEvent(ev()))
    render(card())
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas') as HTMLCanvasElement
    expect(canvas).toBeInTheDocument()
    expect(canvas.getAttribute('data-interactive')).toBeNull()
    expect(writes.ack).toHaveBeenCalledWith(S, 1)
  })

  it('Take over claims; the card grows, says who is in control, and Esc hands back', async () => {
    act(() => browserLiveStore.applyEvent(ev()))
    render(card())
    act(() => browserLiveStore.applyFrame(frame(1)))
    fireEvent.click(screen.getByTestId('browser-live-take-over'))
    expect(writes.claim).toHaveBeenCalledWith(S)
    const root = screen.getByTestId('browser-live-card')
    expect(root).toHaveAttribute('data-state', 'in_control')
    expect(root).toHaveAttribute('data-grown', 'true')
    expect(screen.getByText(i18next.t('pages.chat.browserLiveCard.you_are_in_control'))).toBeInTheDocument()
    // The step strip and the action bubble leave while the person drives.
    expect(screen.queryByRole('list')).toBeNull()
    const canvas = screen.getByTestId('browser-live-canvas')
    expect(canvas).toHaveAttribute('data-interactive', 'true')
    // Typing goes to the browser, not to the transcript.
    fireEvent.keyDown(canvas, { key: 'a', code: 'KeyA' })
    expect(writes.input).toHaveBeenCalledWith(S, expect.objectContaining({ type: 'key', action: 'down', key: 'a', text: 'a' }))
    fireEvent.keyDown(canvas, { key: 'Escape', code: 'Escape' })
    expect(writes.release).toHaveBeenCalledWith(S)
    expect(writes.input).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('browser-live-card')).toHaveAttribute('data-state', 'live')
  })

  it('the Hand back button releases too', () => {
    act(() => browserLiveStore.applyEvent(ev({ state: 'in_control' })))
    render(card())
    fireEvent.click(screen.getByText(i18next.t('pages.chat.browserLiveCard.hand_back')))
    expect(writes.release).toHaveBeenCalledWith(S)
  })

  it('login_needed: the live card with a sign-in banner whose button claims', () => {
    act(() => browserLiveStore.applyEvent(ev({ state: 'login_needed' })))
    render(card())
    expect(screen.getByText(i18next.t('pages.chat.browserLiveCard.take_over_to_sign_in'))).toBeInTheDocument()
    const buttons = screen.getAllByText(i18next.t('pages.chat.browserLiveCard.take_over'))
    fireEvent.click(buttons[buttons.length - 1])
    expect(writes.claim).toHaveBeenCalledWith(S)
  })

  it('done: one line with the host, the step count, the browser, Replay and Last frame', () => {
    act(() => {
      browserLiveStore.applyEvent(ev())
      browserLiveStore.applyFrame(frame(1))
      browserLiveStore.applyFrame(frame(2))
      browserLiveStore.applyEvent(ev({ state: 'done' }))
    })
    render(card(false))
    expect(screen.getByTestId('browser-live-card')).toHaveAttribute('data-state', 'done')
    expect(screen.getByText(i18next.t('pages.chat.browserLiveCard.browsed_host', { host: 'jspaint.app' }))).toBeInTheDocument()
    const summary = screen.getByText(/tab kept open/)
    expect(summary).toHaveTextContent(i18next.t('pages.chat.browserLiveCard.steps_count', { count: 3 }))
    expect(summary).toHaveTextContent(i18next.t('pages.chat.browserLiveCard.browser_user_chrome'))
    expect(screen.getByText(i18next.t('pages.chat.browserLiveCard.replay_steps', { count: 3 }))).toBeInTheDocument()
    expect(screen.getByText(i18next.t('pages.chat.browserLiveCard.open_in', { browser: 'Chrome' }))).toBeInTheDocument()
    // Two actions in the row, never three (max-two-buttons-per-row).
    expect(screen.getByTestId('browser-live-done').querySelectorAll('button').length).toBe(2)
    // No takeover affordance on a finished turn.
    expect(screen.queryByTestId('browser-live-take-over')).toBeNull()
  })

  it('done on a headless Kiro browser offers no window to open', () => {
    act(() => browserLiveStore.applyEvent(ev({ state: 'done', browser: 'kiro_chromium', headless: true })))
    render(card(false))
    expect(screen.queryByText(/Open in/)).toBeNull()
    expect(screen.getByText(/headless/)).toBeInTheDocument()
  })

  it('error: the done line carries the server detail through ErrorNotice', () => {
    act(() => browserLiveStore.applyEvent(ev({ state: 'error', detail: 'Chrome closed the port' })))
    render(card(false))
    expect(screen.getByText('Chrome closed the port')).toBeInTheDocument()
  })

  it('reports visibility from the observer and clears it on unmount', () => {
    act(() => browserLiveStore.applyEvent(ev()))
    const { unmount } = render(card())
    expect(observed).toHaveLength(1)
    act(() => intersect(true))
    expect(writes.visibility.mock.calls).toEqual([[S, true]])
    act(() => intersect(false))
    expect(writes.visibility.mock.calls).toEqual([[S, true], [S, false]])
    act(() => intersect(true))
    unmount()
    expect(writes.visibility.mock.calls.at(-1)).toEqual([S, false])
  })

  it('sanitises the page title and the step labels', () => {
    act(() => browserLiveStore.applyEvent(ev({ title: '<img src=x onerror=alert(1)>Paint', step: { index: 0, label: 'click <b>here</b>' } })))
    render(card())
    expect(document.querySelector('img[onerror]')).toBeNull()
    expect(document.querySelector('b')).toBeNull()
  })
})

describe('anchors and the tool-row test', () => {
  it('recognises the browser tool by its trusted name first, then by its pill text', () => {
    expect(isBrowserToolMessage({ role: 'tool', content: '🔧 browser', cls: '', meta: { tool_name: 'browser' } })).toBe(true)
    expect(isBrowserToolMessage({ role: 'tool', content: '🔧 browser', cls: '' })).toBe(true)
    expect(isBrowserToolMessage({ role: 'tool', content: '🔧 @browser', cls: '' })).toBe(true)
    expect(isBrowserToolMessage({ role: 'tool', content: '🔧 browser_harness_x', cls: '' })).toBe(false)
    expect(isBrowserToolMessage({ role: 'tool', content: '🔧 browser', cls: '', meta: { tool_name: 'shell' } })).toBe(false)
    expect(isBrowserToolMessage({ role: 'assistant', content: '🔧 browser', cls: '' })).toBe(false)
  })

  it('anchors on the call id, else the timestamp, and reads seconds as ms', () => {
    expect(browserLiveAnchor({ role: 'tool', content: '', cls: '', ts: '1761000000.5', meta: { tool_call_id: 'tc1' } })).toEqual({ key: 'call:tc1', ts: 1761000000500 })
    expect(browserLiveAnchor({ role: 'tool', content: '', cls: '', ts: '2026-10-22T10:00:00Z' })).toEqual({ key: 'ts:2026-10-22T10:00:00Z', ts: Date.parse('2026-10-22T10:00:00Z') })
    expect(browserLiveAnchor({ role: 'tool', content: '', cls: '' })).toEqual({ key: 'ts:', ts: null })
  })
})

describe('TurnBlock mounts the card', () => {
  const row = (id: string, tool: string): TurnItem => ({ kind: 'single', msg: { role: 'tool', content: `🔧 ${tool}`, cls: '', ts: String(Math.floor(Date.now() / 1000) + Number(id)), meta: { tool_call_id: `tc-${id}`, tool_name: tool } }, idx: Number(id) }) // flake-ok: the settled-turn binding compares the row clock with the record's client clock (±120 s)
  const text: TurnItem = { kind: 'single', msg: { role: 'assistant', content: 'The house is drawn and the screenshot sits in the card above, in red and blue.', cls: '', ts: '1761000099' }, idx: 99 }
  const turn = (items: TurnItem[], complete = false): Extract<DisplayItem, { kind: 'turn' }> => ({ kind: 'turn', items, complete })
  const renderItem = (it: TurnItem) => <div data-testid={`row-${it.kind === 'single' ? (it.msg.meta?.tool_call_id as string) ?? it.msg.role : 'group'}`} />

  it('once per turn, after the last browser row, when the host names the session', () => {
    act(() => browserLiveStore.applyEvent(ev()))
    const { container } = render(<TurnBlock turn={turn([row('1', 'read'), row('2', 'browser'), row('3', 'browser'), row('4', 'shell')])} renderItem={renderItem} browserLiveSessionKey={S} />)
    const cards = screen.getAllByTestId('browser-live-card')
    expect(cards).toHaveLength(1)
    const order = Array.from(container.querySelectorAll('[data-testid^="row-"], [data-testid="browser-live-card"]')).map(e => e.getAttribute('data-testid'))
    expect(order).toEqual(['row-tc-1', 'row-tc-2', 'row-tc-3', 'browser-live-card', 'row-tc-4'])
  })

  it('not at all without a session key, nor for a turn with no browser call', () => {
    act(() => browserLiveStore.applyEvent(ev()))
    render(<TurnBlock turn={turn([row('2', 'browser')])} renderItem={renderItem} />)
    expect(screen.queryByTestId('browser-live-card')).toBeNull()
    render(<TurnBlock turn={turn([row('1', 'read')])} renderItem={renderItem} browserLiveSessionKey={S} />)
    expect(screen.queryByTestId('browser-live-card')).toBeNull()
  })

  it('a settled turn keeps its card as the done line, outside the fold', () => {
    act(() => {
      browserLiveStore.applyEvent(ev())
      browserLiveStore.applyEvent(ev({ state: 'done' }))
    })
    render(<TurnBlock turn={turn([row('2', 'browser'), text], true)} renderItem={renderItem} browserLiveSessionKey={S} collapseAll />)
    const c = screen.getByTestId('browser-live-card')
    expect(c).toHaveAttribute('data-state', 'done')
    expect(c.closest('[data-collapsed="true"]')).toBeNull()
  })
})

describe('ChatFooter while the person holds the browser', () => {
  it('reads "waiting for you to hand the browser back" instead of Thinking', () => {
    act(() => browserLiveStore.applyEvent(ev({ state: 'in_control' })))
    render(<ChatFooter running stopping={false} state="running" lastRole="tool" browserSessionKey={S} />)
    expect(screen.getByRole('status')).toHaveAccessibleName(i18next.t('pages.chat.browserLiveCard.waiting_for_hand_back'))
    act(() => browserLiveStore.applyEvent(ev({ state: 'live' })))
    expect(screen.getByRole('status')).toHaveAccessibleName(i18next.t('pages.chat.chatFooter.thinking'))
  })
})
