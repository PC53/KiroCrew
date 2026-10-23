/** A question this tab queues while another tab closes the side conversation.
 *  The gateway drops the queue with the conversation, and when the close
 *  frame beats the question's own HTTP receipt, this tab holds no card of it to
 *  hand back: its only card came from the scrubbed queue frame. The receipt hands
 *  the text back instead. */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import reducer, { sideClose, sseSideQueue } from '../store/chatSlice'
import { renderWithProviders, createTestStore } from './helpers'
import dashboardReducer from '../store/dashboardSlice'

vi.mock('../api/client', () => ({
  api: {
    sideState: vi.fn().mockResolvedValue({ open: false }),
    sideOpen: vi.fn().mockResolvedValue({ ok: true, open: true, messages: 2, last_run_id: 'r1', created_at: '2026-05-20T00:00:00Z' }),
    sideTurn: vi.fn(),
  },
  SEARCH_MIN_CHARS: 2,
}))

import SideChat from '../pages/chat/SideChat'
import { api } from '../api/client'
import { clearSideChatDrafts, readSideChatDraft } from '../chat-core/composer/sideChatDrafts'

type SideTurnBody = Awaited<ReturnType<typeof api.sideTurn>>

const SLOT = 'closed-elsewhere-slot'
const initial = reducer(undefined, { type: '@@INIT' })

/** A side mid-turn, so a submit can only steer or queue. */
function busyStore() {
  return createTestStore({
    dashboard: { ...dashboardReducer(undefined, { type: '@@INIT' }), connected: true },
    chat: {
      ...initial,
      activeSlot: SLOT,
      slotSideRestored: { [SLOT]: true },
      slotSide: {
        [SLOT]: {
          messages: [
            { role: 'user' as const, content: 'q1', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
            { role: 'assistant' as const, content: 'partial', ts: '2026-05-20T00:00:01Z', run_id: 'r1' },
          ],
          lastRunId: 'r1',
          pending: false,
          streaming: true,
          openedAtTurnCount: 0,
          createdAt: '2026-05-20T00:00:00Z',
        },
      },
    },
  })
}

describe('SideChat: the conversation closes in another tab while a question is on its way', () => {
  beforeEach(() => {
    localStorage.clear()
    clearSideChatDrafts()
    vi.mocked(api.sideTurn).mockReset()
  })

  it('hands a queued question back when the close lands before its receipt', async () => {
    let answer: (body: SideTurnBody) => void = () => {}
    vi.mocked(api.sideTurn).mockImplementationOnce(() => new Promise<SideTurnBody>(resolve => { answer = resolve }))
    const store = busyStore()
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Ask a side question'), 'later please')
    await user.click(screen.getByTestId('busy-send-caret'))
    await user.click(screen.getByTestId('busy-send-mode-queue'))
    await user.click(screen.getByTestId('busy-send-button'))
    await waitFor(() => expect(api.sideTurn).toHaveBeenCalledWith(SLOT, 'later please', undefined))

    // The gateway queued it and pushed its scrubbed card, then another tab closed
    // the conversation, and both frames landed before this question's receipt.
    act(() => {
      store.dispatch(sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q-1', content: 'later please' }))
      store.dispatch(sideClose(SLOT))
    })
    await act(async () => { answer({ ok: true, queued: true, queue_id: 'q-1', still_queued: true, depth: 1 }) })

    await waitFor(() => expect(readSideChatDraft(SLOT)).toContain('later please'))
    expect(store.getState().chat.slotSide[SLOT]).toBeUndefined()
  })
})
