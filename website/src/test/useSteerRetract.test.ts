import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import type { ChatMessage } from '../types'

const retractSteer = vi.fn()
vi.mock('../api/client', () => ({ api: { retractSteer: (...args: unknown[]) => retractSteer(...args) } }))
const revealComposer = vi.fn()
vi.mock('../pages/chat/composerFocus', () => ({ revealComposer: () => revealComposer() }))

const TAKE_BACKS_KEY = 'mc-steer-take-backs'
const storedTakeBacks = () => JSON.parse(localStorage.getItem(TAKE_BACKS_KEY) ?? '{}') as Record<string, unknown>

import {
  __resetSteerTakeBacksForTests,
  useSteerRetract,
  type SteerRetractOptions,
} from '../hooks/useSteerRetract'
import { ApiError } from '../api/apiError'

const row = (meta: Record<string, unknown>, content = 'read [attached_file 1] /tmp/notes.md'): ChatMessage => ({
  role: 'user',
  content,
  cls: 'msg msg-u',
  ts: '2026-10-23T05:00:00Z',
  meta: { steer: true, mid: 'm-1', steerRetract: true, files: ['/tmp/notes.md'], ...meta },
})

function setup(initial: ChatMessage[], slotRunning = true, slot = 'test') {
  const restoreDraft = vi.fn()
  const hook = renderHook(
    (props: Partial<SteerRetractOptions>) => useSteerRetract({
      slot, messages: initial, slotRunning, restoreDraft, ...props,
    }),
    { initialProps: {} },
  )
  return { hook, restoreDraft }
}

beforeEach(() => {
  localStorage.clear()
  __resetSteerTakeBacksForTests()
  revealComposer.mockReset()
  retractSteer.mockReset()
  retractSteer.mockResolvedValue({ ok: true, pending: true })
})

describe('useSteerRetract', () => {
  it('asks the gateway once and marks the row pending', () => {
    retractSteer.mockImplementationOnce(() => {
      expect(storedTakeBacks()).toHaveProperty('test')
      return Promise.resolve({ ok: true, pending: true })
    })
    const { hook } = setup([row({ steerState: 'written' })])

    act(() => { hook.result.current.onSteerAction('m-1', 'edit') })
    act(() => { hook.result.current.onSteerAction('m-1', 'edit') })

    expect(retractSteer).toHaveBeenCalledTimes(1)
    expect(retractSteer).toHaveBeenCalledWith('test', 'm-1', 'edit')
    expect(hook.result.current.pending).toEqual({ 'm-1': 'edit' })
  })

  it('restores an edited steer after module state is lost and removes its stored recovery', async () => {
    retractSteer.mockImplementationOnce(() => new Promise(() => {}))
    const first = setup([row({ steerState: 'written' })])
    act(() => { first.hook.result.current.onSteerAction('m-1', 'edit') })
    expect(storedTakeBacks()).toHaveProperty('test')
    first.hook.unmount()

    __resetSteerTakeBacksForTests()
    const second = setup([row({ steerState: 'retracted', steerRetractAction: 'edit' })])

    await waitFor(() => expect(second.restoreDraft).toHaveBeenCalledTimes(1), { timeout: 500 })
    expect(second.restoreDraft.mock.calls[0][0]).toBe('read @notes.md')
    expect(second.hook.result.current.pending).toEqual({})
    expect(storedTakeBacks()).not.toHaveProperty('test')
  })

  it.each([
    ['corrupt', '{', null],
    ['expired', JSON.stringify({
      test: {
        slot: 'test', mid: 'm-1', action: 'edit', content: 'lost text',
        observedRunning: true, requestFailed: false,
      },
    }), JSON.stringify({ test: 0 })],
  ])('ignores and removes a %s stored recovery record', (_kind, stored, timestamps) => {
    localStorage.setItem(TAKE_BACKS_KEY, stored)
    if (timestamps !== null) localStorage.setItem(`${TAKE_BACKS_KEY}-ts`, timestamps)
    __resetSteerTakeBacksForTests()

    const { hook, restoreDraft } = setup([row({ steerState: 'retracted', steerRetractAction: 'edit' })])

    expect(hook.result.current.pending).toEqual({})
    expect(restoreDraft).not.toHaveBeenCalled()
    expect(localStorage.getItem(TAKE_BACKS_KEY)).toBeNull()
  })

  it.each(['unsend', 'queue'] as const)('removes stored recovery after a taken-back %s settles', (action) => {
    const { hook } = setup([row({ steerState: 'written' })])
    act(() => { hook.result.current.onSteerAction('m-1', action) })
    expect(storedTakeBacks()).toHaveProperty('test')

    hook.rerender({ messages: [row({ steerState: 'retracted', steerRetractAction: action })] })

    expect(hook.result.current.pending).toEqual({})
    expect(storedTakeBacks()).not.toHaveProperty('test')
  })

  it('puts an edited steer back in the composer once its row says it was taken back', () => {
    const { hook, restoreDraft } = setup([row({ steerState: 'written' })])
    act(() => { hook.result.current.onSteerAction('m-1', 'edit') })
    expect(restoreDraft).not.toHaveBeenCalled()

    hook.rerender({ messages: [row({ steerState: 'retracted', steerRetractAction: 'edit' })] })

    expect(restoreDraft).toHaveBeenCalledTimes(1)
    const [text, files] = restoreDraft.mock.calls[0]
    // The marker comes back as a mention, spelled by the shortest suffix no other
    // staged file shares, the way a cancelled queue card's does.
    expect(text).toBe('read @notes.md')
    expect(files).toEqual(['/tmp/notes.md'])
    expect(revealComposer).toHaveBeenCalledTimes(1)
    expect(hook.result.current.pending).toEqual({})
  })

  it.each(['unsend', 'queue'] as const)('leaves the composer alone for a taken-back %s', (action) => {
    const { hook, restoreDraft } = setup([row({ steerState: 'written' })])
    act(() => { hook.result.current.onSteerAction('m-1', action) })

    hook.rerender({ messages: [row({ steerState: 'retracted', steerRetractAction: action })] })

    expect(restoreDraft).not.toHaveBeenCalled()
    expect(revealComposer).not.toHaveBeenCalled()
    expect(hook.result.current.pending).toEqual({})
  })

  it('takes nothing back when the turn read the steer first', () => {
    const { hook, restoreDraft } = setup([row({ steerState: 'written' })])
    act(() => { hook.result.current.onSteerAction('m-1', 'edit') })

    hook.rerender({ messages: [row({ steerState: 'consumed' })] })

    expect(restoreDraft).not.toHaveBeenCalled()
    expect(hook.result.current.pending).toEqual({})
    expect(hook.result.current.refusals).toEqual({})
  })

  it('reports an agent that refused to take it back', () => {
    const { hook, restoreDraft } = setup([row({ steerState: 'written' })])
    act(() => { hook.result.current.onSteerAction('m-1', 'unsend') })

    hook.rerender({ messages: [row({ steerState: 'written', steerRetract: false })] })

    expect(restoreDraft).not.toHaveBeenCalled()
    expect(hook.result.current.refusals).toEqual({ 'm-1': 'unsupported' })
  })

  it.each(['edit', 'queue'] as const)('hands a %s back to the composer when a hard stop discarded the steer', (action) => {
    const { hook, restoreDraft } = setup([row({ steerState: 'written' })])
    act(() => { hook.result.current.onSteerAction('m-1', action) })

    hook.rerender({ slotRunning: false })

    expect(restoreDraft).toHaveBeenCalledTimes(1)
    expect(hook.result.current.pending).toEqual({})
  })

  it('drops an unsend a hard stop discarded without touching the composer', () => {
    const { hook, restoreDraft } = setup([row({ steerState: 'written' })])
    act(() => { hook.result.current.onSteerAction('m-1', 'unsend') })

    hook.rerender({ slotRunning: false })

    expect(restoreDraft).not.toHaveBeenCalled()
    expect(hook.result.current.pending).toEqual({})
  })

  it('says why the gateway refused, and stays quiet when the steer is simply gone', async () => {
    retractSteer.mockRejectedValueOnce(new ApiError(409, 'busy', JSON.stringify({ code: 'steer_retract_busy' })))
    const { hook } = setup([row({ steerState: 'written' })])

    act(() => { hook.result.current.onSteerAction('m-1', 'unsend') })
    await waitFor(
      () => expect(hook.result.current.refusals).toEqual({ 'm-1': 'busy' }),
      { timeout: 500 },
    )
    expect(hook.result.current.pending).toEqual({})
    expect(hook.result.current.requestFailures).toEqual({})

    retractSteer.mockRejectedValueOnce(new ApiError(409, 'gone', JSON.stringify({ code: 'steer_not_pending' })))
    act(() => { hook.result.current.onSteerAction('m-1', 'unsend') })
    await waitFor(() => expect(hook.result.current.pending).toEqual({}), { timeout: 500 })
    expect(hook.result.current.refusals).toEqual({})
    expect(hook.result.current.requestFailures).toEqual({})
  })

  it.each([
    ['a network error', new TypeError('connection lost')],
    ['an HTTP 503', new ApiError(503, 'unavailable')],
  ])('keeps reconciliation pending after %s and exposes the request failure until settlement', async (_name, error) => {
    retractSteer.mockRejectedValueOnce(error)
    const { hook } = setup([row({ steerState: 'written' })])

    act(() => { hook.result.current.onSteerAction('m-1', 'unsend') })

    await waitFor(
      () => expect(hook.result.current.requestFailures).toEqual({ 'm-1': true }),
      { timeout: 500 },
    )
    expect(hook.result.current.pending).toEqual({ 'm-1': 'unsend' })
    expect(hook.result.current.refusals).toEqual({})

    hook.rerender({ messages: [row({ steerState: 'consumed' })] })

    expect(hook.result.current.pending).toEqual({})
    expect(hook.result.current.requestFailures).toEqual({})
  })

  it.each([
    {
      name: 'a transport failure',
      action: 'edit' as const,
      request: () => Promise.reject(new TypeError('connection lost')),
      messages: [row({ steerState: 'retracted', steerRetractAction: 'edit' })],
      slotRunning: true,
    },
    {
      name: 'a server failure',
      action: 'edit' as const,
      request: () => Promise.reject(new ApiError(503, 'unavailable')),
      messages: [row({ steerState: 'retracted', steerRetractAction: 'edit' })],
      slotRunning: true,
    },
    {
      name: 'no answer before a hard stop',
      action: 'queue' as const,
      request: () => new Promise(() => {}),
      messages: [row({ steerState: 'written' })],
      slotRunning: false,
    },
  ])('keeps recovery through $name and a remount', async ({ action, request, messages, slotRunning }) => {
    retractSteer.mockImplementationOnce(request)
    const first = setup([row({ steerState: 'written' })])
    act(() => { first.hook.result.current.onSteerAction('m-1', action) })

    await waitFor(
      () => expect(first.hook.result.current.pending).toEqual({ 'm-1': action }),
      { timeout: 500 },
    )
    first.hook.unmount()

    const second = setup(messages, slotRunning)
    await waitFor(() => expect(second.restoreDraft).toHaveBeenCalledTimes(1), { timeout: 500 })
    expect(second.hook.result.current.pending).toEqual({})
  })

  it('keeps a stored take-back until the reloaded transcript arrives', async () => {
    retractSteer.mockImplementationOnce(() => new Promise(() => {}))
    const first = setup([row({ steerState: 'written' })], true)
    act(() => { first.hook.result.current.onSteerAction('m-1', 'edit') })
    first.hook.unmount()
    __resetSteerTakeBacksForTests()

    const reloaded = setup([], false)

    expect(reloaded.restoreDraft).not.toHaveBeenCalled()
    expect(reloaded.hook.result.current.pending).toEqual({ 'm-1': 'edit' })
    expect(storedTakeBacks()).toHaveProperty('test')

    reloaded.hook.rerender({
      messages: [row({ steerState: 'retracted', steerRetractAction: 'edit' })],
      slotRunning: false,
    })

    await waitFor(() => expect(reloaded.restoreDraft).toHaveBeenCalledTimes(1), { timeout: 500 })
    expect(reloaded.hook.result.current.pending).toEqual({})
    expect(storedTakeBacks()).not.toHaveProperty('test')
  })

  it('waits to observe the slot running before an idle remount can mean a hard stop', async () => {
    retractSteer.mockImplementationOnce(() => new Promise(() => {}))
    const first = setup([row({ steerState: 'written' })], false, 'slot-a')
    act(() => { first.hook.result.current.onSteerAction('m-1', 'edit') })
    first.hook.unmount()
    __resetSteerTakeBacksForTests()

    const otherSlot = setup(
      [row({ steerState: 'retracted', steerRetractAction: 'edit' })],
      true,
      'slot-b',
    )
    expect(otherSlot.restoreDraft).not.toHaveBeenCalled()
    expect(otherSlot.hook.result.current.pending).toEqual({})

    const ownSlot = setup([row({ steerState: 'written' })], false, 'slot-a')
    expect(ownSlot.restoreDraft).not.toHaveBeenCalled()
    expect(ownSlot.hook.result.current.pending).toEqual({ 'm-1': 'edit' })

    ownSlot.hook.rerender({ slotRunning: true })
    expect(ownSlot.restoreDraft).not.toHaveBeenCalled()

    ownSlot.hook.rerender({
      slotRunning: true,
      messages: [row({ steerState: 'retracted', steerRetractAction: 'edit' })],
    })
    await waitFor(() => expect(ownSlot.restoreDraft).toHaveBeenCalledTimes(1), { timeout: 500 })
    expect(otherSlot.restoreDraft).not.toHaveBeenCalled()
    expect(ownSlot.hook.result.current.pending).toEqual({})
  })

  it('keeps a stored edit through unrelated provisional rows until its retracted row loads', () => {
    retractSteer.mockImplementation(() => new Promise(() => {}))
    const { hook, restoreDraft } = setup([row({ steerState: 'written' })])
    act(() => { hook.result.current.onSteerAction('m-1', 'edit') })

    const unrelated = row({ mid: 'm-2', steerState: 'written' }, 'another steer')
    hook.rerender({
      messages: [unrelated],
      transcriptAuthoritative: false,
    } as Partial<SteerRetractOptions>)

    expect(restoreDraft).not.toHaveBeenCalled()
    expect(hook.result.current.pending).toEqual({ 'm-1': 'edit' })
    expect(storedTakeBacks()).toHaveProperty('test')

    hook.rerender({
      messages: [row({ steerState: 'retracted', steerRetractAction: 'edit' })],
      transcriptAuthoritative: true,
    } as Partial<SteerRetractOptions>)

    expect(restoreDraft).toHaveBeenCalledTimes(1)
    expect(hook.result.current.pending).toEqual({})
    expect(storedTakeBacks()).not.toHaveProperty('test')
  })

  it('does not restore a written edit when a provisional transcript says the turn ended', () => {
    retractSteer.mockImplementation(() => new Promise(() => {}))
    const { hook, restoreDraft } = setup([row({ steerState: 'written' })])
    act(() => { hook.result.current.onSteerAction('m-1', 'edit') })

    hook.rerender({
      messages: [row({ steerState: 'written' })],
      slotRunning: false,
      transcriptAuthoritative: false,
    } as Partial<SteerRetractOptions>)

    expect(restoreDraft).not.toHaveBeenCalled()
    expect(hook.result.current.pending).toEqual({ 'm-1': 'edit' })
    expect(storedTakeBacks()).toHaveProperty('test')
  })

  it('settles a missing row without disturbing another slot take-back', () => {
    retractSteer.mockImplementation(() => new Promise(() => {}))
    const slotB = setup([row({ steerState: 'written' })], true, 'slot-b')
    act(() => { slotB.hook.result.current.onSteerAction('m-1', 'queue') })

    const slotA = setup([row({ steerState: 'written' })], true, 'slot-a')
    act(() => { slotA.hook.result.current.onSteerAction('m-1', 'edit') })
    const other = row({ mid: 'm-2', steerState: 'written' }, 'another steer')
    slotA.hook.rerender({ messages: [other] })

    expect(slotA.restoreDraft).not.toHaveBeenCalled()
    expect(slotA.hook.result.current.pending).toEqual({})
    expect(slotB.hook.result.current.pending).toEqual({ 'm-1': 'queue' })
    expect(storedTakeBacks()).not.toHaveProperty('slot-a')
    expect(storedTakeBacks()).toHaveProperty('slot-b')

    act(() => { slotA.hook.result.current.onSteerAction('m-2', 'queue') })
    expect(retractSteer).toHaveBeenCalledTimes(3)
    expect(slotA.hook.result.current.pending).toEqual({ 'm-2': 'queue' })
    expect(slotB.hook.result.current.pending).toEqual({ 'm-1': 'queue' })
  })
})
