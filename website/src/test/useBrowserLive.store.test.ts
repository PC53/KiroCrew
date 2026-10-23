import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

import { browserLiveStore, readBrowserLiveEvent } from '../hooks/useBrowserLive'
import type { DecodedBrowserFrame } from '../hooks/useBrowserLive.frame'
import { encodeBrowserFrame } from '../hooks/useBrowserLive.frame'

/**
 * The live-browser store: the state machine a `browser_live` event drives, the
 * one-ack-per-frame rule, the turn binding a card survives a remount with, the
 * visibility OR, and the optimistic claim that reverts when the gateway says no.
 * Every write goes to injected stubs; nothing here touches the network.
 */

const S = 'slot:abc'
const ev = (over: Record<string, unknown> = {}) => ({ session_key: S, turn_id: 't1', state: 'live', url: 'https://jspaint.app/', title: 'untitled - Paint', browser: 'user_chrome', headless: false, steps: [{ label: 'open', status: 'done' }, { label: 'draw', status: 'active' }], step: { index: 1, label: 'draw' }, rung: 0, fps: 58, ...over })
const frame = (seq: number): DecodedBrowserFrame => ({
  header: { type: 'browser_frame', session_key: S, seq, w: 1280, h: 800, mask: [] },
  jpeg: new Uint8Array([1, 2, 3]),
  receivedAt: 1000 + seq,
})

let writes: ReturnType<typeof stubWrites>
function stubWrites(claimOk = true) {
  return {
    ack: vi.fn(async () => ({ ok: true })),
    visibility: vi.fn(async () => ({ ok: true })),
    claim: vi.fn(async () => { if (!claimOk) throw new Error('403'); return { ok: true } }),
    release: vi.fn(async () => ({ ok: true })),
    input: vi.fn(async () => ({ ok: true })),
    open: vi.fn(async () => ({ ok: true })),
  }
}
let restore: ReturnType<typeof browserLiveStore.__setWritesForTests>

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-10-22T10:00:00Z'))
  browserLiveStore.__resetForTests()
  writes = stubWrites()
  restore = browserLiveStore.__setWritesForTests(writes)
})
afterEach(() => {
  browserLiveStore.__setWritesForTests(restore)
  browserLiveStore.__resetForTests()
  vi.useRealTimers()
})

describe('readBrowserLiveEvent', () => {
  it('accepts a well-formed event and normalises optional fields', () => {
    const e = readBrowserLiveEvent(ev({ steps: [{ label: 'a', status: 'bogus' }, { nope: 1 }, { label: 'b', status: 'done' }] }))
    expect(e).toMatchObject({ session_key: S, turn_id: 't1', state: 'live', browser: 'user_chrome' })
    expect(e!.steps).toEqual([{ label: 'a', status: 'pending' }, { label: 'b', status: 'done' }])
  })
  it.each([
    ['no session key', ev({ session_key: '' })],
    ['no turn id', ev({ turn_id: undefined })],
    ['unknown state', ev({ state: 'flying' })],
    ['not an object', 'live'],
    ['null', null],
  ])('drops an event with %s', (_l, raw) => {
    expect(readBrowserLiveEvent(raw)).toBeNull()
  })
  it('ignores an unknown browser kind rather than trusting it', () => {
    expect(readBrowserLiveEvent(ev({ browser: 'safari' }))!.browser).toBeUndefined()
  })
})

describe('applyEvent: the state machine', () => {
  it('creates the record on the first event and stamps its start', () => {
    browserLiveStore.applyEvent(ev({ state: 'waiting_allow' }))
    const t = browserLiveStore.current(S)!
    expect(t.state).toBe('waiting_allow')
    expect(t.startedAt).toBe(Date.now())
    expect(t.endedAt).toBeNull()
    expect(t.hasFrame).toBe(false)
  })

  it('a later event for the same turn keeps what it does not restate', () => {
    browserLiveStore.applyEvent(ev())
    browserLiveStore.applyEvent({ session_key: S, turn_id: 't1', state: 'in_control' })
    const t = browserLiveStore.current(S)!
    expect(t.state).toBe('in_control')
    expect(t.url).toBe('https://jspaint.app/')
    expect(t.steps).toHaveLength(2)
    expect(t.browser).toBe('user_chrome')
  })

  it('done stamps the end once; a repeated done does not move it', () => {
    browserLiveStore.applyEvent(ev())
    vi.advanceTimersByTime(42_000)
    browserLiveStore.applyEvent(ev({ state: 'done', step: undefined }))
    const end = browserLiveStore.current(S)!.endedAt
    expect(end).toBe(Date.now())
    vi.advanceTimersByTime(5_000)
    browserLiveStore.applyEvent(ev({ state: 'done', step: undefined }))
    expect(browserLiveStore.current(S)!.endedAt).toBe(end)
    expect(browserLiveStore.current(S)!.step).toBeNull()
  })

  it('a new turn id closes the previous live turn as done', () => {
    browserLiveStore.applyEvent(ev())
    browserLiveStore.applyEvent(ev({ turn_id: 't2', state: 'waiting_allow' }))
    expect(browserLiveStore.current(S)!.turnId).toBe('t2')
    // A settled card never binds to a record that is still live.
    expect(browserLiveStore.resolveTurn(S, 'anchor-1', false, Date.now())).toBeNull()
  })

  it('an error event keeps the server detail verbatim', () => {
    browserLiveStore.applyEvent(ev({ state: 'error', detail: 'Chrome closed the debugging port' }))
    expect(browserLiveStore.current(S)).toMatchObject({ state: 'error', detail: 'Chrome closed the debugging port' })
    expect(browserLiveStore.current(S)!.endedAt).not.toBeNull()
  })

  it('a malformed event changes nothing', () => {
    browserLiveStore.applyEvent(ev())
    const listener = vi.fn()
    const off = browserLiveStore.subscribe(listener)
    browserLiveStore.applyEvent({ session_key: S, state: 'live' })
    expect(listener).not.toHaveBeenCalled()
    off()
  })
})

describe('applyFrame: ack once, keep, paint', () => {
  it('acks each new seq exactly once and flips hasFrame on the first frame', () => {
    browserLiveStore.applyEvent(ev())
    const listener = vi.fn()
    browserLiveStore.subscribe(listener)
    browserLiveStore.applyFrame(frame(1))
    browserLiveStore.applyFrame(frame(2))
    browserLiveStore.applyFrame(frame(2))
    expect(writes.ack.mock.calls).toEqual([[S, 1], [S, 2]])
    const t = browserLiveStore.current(S)!
    expect(t.hasFrame).toBe(true)
    expect(t.ring.length).toBe(3)
    // One notify for the first frame only: later frames never re-render React.
    expect(listener).toHaveBeenCalledTimes(1)
  })

  it('delivers frames to a subscribed painter, and the last one on subscribe', () => {
    browserLiveStore.applyEvent(ev())
    browserLiveStore.applyFrame(frame(1))
    const paint = vi.fn()
    const off = browserLiveStore.subscribeFrames(S, 't1', paint)
    expect(paint).toHaveBeenCalledWith(expect.objectContaining({ header: expect.objectContaining({ seq: 1 }) }))
    browserLiveStore.applyFrame(frame(2))
    expect(paint).toHaveBeenCalledTimes(2)
    off()
    browserLiveStore.applyFrame(frame(3))
    expect(paint).toHaveBeenCalledTimes(2)
  })

  it('still acks a frame for a session with no live turn, so the relay never stalls', () => {
    browserLiveStore.applyFrame(frame(9))
    expect(writes.ack).toHaveBeenCalledWith(S, 9)
    expect(browserLiveStore.current(S)).toBeNull()
  })

  it('a frame after done is acked but not kept', () => {
    browserLiveStore.applyEvent(ev({ state: 'done' }))
    browserLiveStore.applyFrame(frame(1))
    expect(writes.ack).toHaveBeenCalledWith(S, 1)
    expect(browserLiveStore.current(S)!.ring.length).toBe(0)
  })

  it('the websocket binary branch decodes and lands a frame here', () => {
    browserLiveStore.applyEvent(ev())
    browserLiveStore.applyBinary(encodeBrowserFrame({ session_key: S, seq: 5, w: 100, h: 50 }, new Uint8Array([1])))
    expect(writes.ack).toHaveBeenCalledWith(S, 5)
    expect(browserLiveStore.current(S)!.hasFrame).toBe(true)
    browserLiveStore.applyBinary(new Uint8Array([0, 0]).buffer)
    expect(writes.ack).toHaveBeenCalledTimes(1)
  })
})

describe('resolveTurn: binding a card to a record', () => {
  it('a running turn binds to the current record and keeps it across a remount', () => {
    browserLiveStore.applyEvent(ev())
    expect(browserLiveStore.resolveTurn(S, 'call:a', true, null)!.turnId).toBe('t1')
    browserLiveStore.applyEvent(ev({ turn_id: 't2' }))
    // The first card, remounted after its turn settled, still shows t1.
    expect(browserLiveStore.resolveTurn(S, 'call:a', false, null)!.turnId).toBe('t1')
    expect(browserLiveStore.resolveTurn(S, 'call:a', false, null)!.state).toBe('done')
    // A second running turn's card binds to t2.
    expect(browserLiveStore.resolveTurn(S, 'call:b', true, null)!.turnId).toBe('t2')
  })

  it('a settled, never-bound turn claims the newest unbound record only when the clocks overlap', () => {
    browserLiveStore.applyEvent(ev())
    vi.advanceTimersByTime(30_000)
    browserLiveStore.applyEvent(ev({ state: 'done' }))
    const started = browserLiveStore.current(S)!.startedAt
    // An anchor row from an hour earlier is some other turn's.
    expect(browserLiveStore.resolveTurn(S, 'call:old', false, started - 3_600_000)).toBeNull()
    // One from inside the window is this turn's.
    expect(browserLiveStore.resolveTurn(S, 'call:now', false, started + 1_000)!.turnId).toBe('t1')
    // And once bound, nobody else gets it.
    expect(browserLiveStore.resolveTurn(S, 'call:other', false, started + 2_000)).toBeNull()
  })

  it('returns null for a session the relay never spoke about', () => {
    expect(browserLiveStore.resolveTurn('slot:none', 'call:x', true, null)).toBeNull()
  })
})

describe('visibility: the OR of every card instance and the document', () => {
  it('reports true on the first visible instance and false when the last leaves', () => {
    browserLiveStore.setVisible(S, 'card-1', true)
    browserLiveStore.setVisible(S, 'card-2', true)
    browserLiveStore.setVisible(S, 'card-1', false)
    expect(writes.visibility.mock.calls).toEqual([[S, true]])
    browserLiveStore.setVisible(S, 'card-2', false)
    expect(writes.visibility.mock.calls).toEqual([[S, true], [S, false]])
  })

  it('a hidden document reads as not visible, and a return re-reports', () => {
    browserLiveStore.setVisible(S, 'card-1', true)
    const spy = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    document.dispatchEvent(new Event('visibilitychange'))
    expect(writes.visibility.mock.calls.at(-1)).toEqual([S, false])
    spy.mockReturnValue('visible')
    document.dispatchEvent(new Event('visibilitychange'))
    expect(writes.visibility.mock.calls.at(-1)).toEqual([S, true])
    spy.mockRestore()
  })
})

describe('claim and release', () => {
  it('claim flips to in_control at once and posts the claim', async () => {
    browserLiveStore.applyEvent(ev())
    const p = browserLiveStore.claim(S)
    expect(browserLiveStore.inControl(S)).toBe(true)
    await expect(p).resolves.toBe(true)
    expect(writes.claim).toHaveBeenCalledWith(S)
  })

  it('a refused claim reverts to the state before it', async () => {
    browserLiveStore.__setWritesForTests(stubWrites(false))
    browserLiveStore.applyEvent(ev({ state: 'login_needed' }))
    await expect(browserLiveStore.claim(S)).resolves.toBe(false)
    expect(browserLiveStore.current(S)!.state).toBe('login_needed')
  })

  it('claim does nothing on a finished turn', async () => {
    browserLiveStore.applyEvent(ev({ state: 'done' }))
    await expect(browserLiveStore.claim(S)).resolves.toBe(false)
    expect(writes.claim).not.toHaveBeenCalled()
  })

  it('release flips back to live and posts; a release while not in control is a no-op', async () => {
    browserLiveStore.applyEvent(ev())
    await browserLiveStore.release(S)
    expect(writes.release).not.toHaveBeenCalled()
    await browserLiveStore.claim(S)
    await browserLiveStore.release(S)
    expect(browserLiveStore.current(S)!.state).toBe('live')
    expect(writes.release).toHaveBeenCalledWith(S)
  })

  it('input and open-in-browser go straight to their writes', async () => {
    browserLiveStore.applyEvent(ev())
    await browserLiveStore.sendInput(S, { type: 'key', action: 'down', key: 'a', code: 'KeyA', text: 'a', modifiers: 0 })
    expect(writes.input).toHaveBeenCalledWith(S, expect.objectContaining({ type: 'key', key: 'a' }))
    browserLiveStore.openInBrowser(S)
    expect(writes.open).toHaveBeenCalledWith(S)
  })
})
