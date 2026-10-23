/**
 * The live-browser card's writes (RFC: live browser in chat, "Wire contract").
 *
 * Every route is owner-only on the gateway: it accepts the dashboard owner's
 * cookie or token and refuses the internal secret with 403, so an agent cannot
 * claim input for itself or ack frames it never saw. The card sends bodies in
 * FRAME pixels; nothing here converts.
 */

import type { ClientTransport } from './transport'
import type { BrowserInputEvent } from '../../hooks/useBrowserLive.input'

export type { BrowserInputEvent }

export interface BrowserLiveOk { ok: boolean }

export function createBrowserLiveEndpoints({ post, j }: ClientTransport) {
  return {
    browserLive: {
      /** The card is (or is not) on screen: an off-screen card stops the screencast. */
      browserLiveVisibility: (sessionKey: string, visible: boolean) =>
        post('/api/browser/live/visibility', { session_key: sessionKey, visible }).then(j) as Promise<BrowserLiveOk>,
      /** Frame `seq` reached the card; the relay acks Chrome only after this. */
      browserLiveAck: (sessionKey: string, seq: number) =>
        post('/api/browser/live/ack', { session_key: sessionKey, seq }).then(j) as Promise<BrowserLiveOk>,
      /** Hand mouse and keyboard to the person; agent ops return `user_in_control` meanwhile. */
      browserInputClaim: (sessionKey: string) =>
        post('/api/browser/input/claim', { session_key: sessionKey }).then(j) as Promise<BrowserLiveOk>,
      browserInputRelease: (sessionKey: string) =>
        post('/api/browser/input/release', { session_key: sessionKey }).then(j) as Promise<BrowserLiveOk>,
      /** One mouse, wheel or key event, in frame pixels. Never logged on either side. */
      browserInputEvent: (sessionKey: string, event: BrowserInputEvent) =>
        post('/api/browser/input/event', { session_key: sessionKey, event }).then(j) as Promise<BrowserLiveOk>,
      /** Focus the real browser window, for what the frame cannot carry (autofill, file pickers). */
      browserLiveOpenInBrowser: (sessionKey: string) =>
        post('/api/browser/live/open-in-browser', { session_key: sessionKey }).then(j) as Promise<BrowserLiveOk>,
    },
  }
}
