import { describe, it, expect } from 'vitest'

import {
  drawnRect, keyToEvent, mapPointToFrame, modifiersOf, pointerToEvent, wheelToEvent, isHandBackKey,
  MOD_ALT, MOD_CTRL, MOD_META, MOD_SHIFT,
} from '../hooks/useBrowserLive.input'

const noMods = { altKey: false, ctrlKey: false, metaKey: false, shiftKey: false }
const frame = { w: 1280, h: 800 }

describe('mapPointToFrame', () => {
  it('scales a point in a box that matches the frame aspect', () => {
    const box = { left: 100, top: 50, width: 640, height: 400 }
    expect(mapPointToFrame(100, 50, box, frame)).toEqual({ x: 0, y: 0 })
    expect(mapPointToFrame(420, 250, box, frame)).toEqual({ x: 640, y: 400 })
    expect(mapPointToFrame(740, 450, box, frame)).toEqual({ x: 1279, y: 799 })
  })

  it('letterboxes a box wider than the frame and refuses points in the bars', () => {
    const box = { left: 0, top: 0, width: 1000, height: 400 }
    // 1280x800 fits as 640x400, centred at left 180.
    expect(drawnRect(box, frame)).toEqual({ left: 180, top: 0, width: 640, height: 400 })
    expect(mapPointToFrame(100, 200, box, frame)).toBeNull()
    expect(mapPointToFrame(180, 200, box, frame)).toEqual({ x: 0, y: 400 })
    expect(mapPointToFrame(500, 200, box, frame)).toEqual({ x: 640, y: 400 })
  })

  it('letterboxes a box taller than the frame', () => {
    const box = { left: 0, top: 0, width: 640, height: 1000 }
    expect(drawnRect(box, frame)).toEqual({ left: 0, top: 300, width: 640, height: 400 })
    expect(mapPointToFrame(320, 100, box, frame)).toBeNull()
    expect(mapPointToFrame(320, 500, box, frame)).toEqual({ x: 640, y: 400 })
  })

  it('returns null for an empty box', () => {
    expect(mapPointToFrame(5, 5, { left: 0, top: 0, width: 0, height: 0 }, frame)).toBeNull()
  })
})

describe('modifiersOf', () => {
  it('builds the CDP bitmask', () => {
    expect(modifiersOf(noMods)).toBe(0)
    expect(modifiersOf({ ...noMods, altKey: true })).toBe(MOD_ALT)
    expect(modifiersOf({ ...noMods, ctrlKey: true, shiftKey: true })).toBe(MOD_CTRL | MOD_SHIFT)
    expect(modifiersOf({ altKey: true, ctrlKey: true, metaKey: true, shiftKey: true })).toBe(MOD_ALT | MOD_CTRL | MOD_META | MOD_SHIFT)
  })
})

describe('pointerToEvent', () => {
  const box = { left: 0, top: 0, width: 640, height: 400 }
  it('maps a left down with a click count', () => {
    expect(pointerToEvent('down', { ...noMods, clientX: 320, clientY: 200, button: 0 }, box, frame, 2))
      .toEqual({ type: 'mouse', action: 'down', x: 640, y: 400, button: 'left', clicks: 2, modifiers: 0 })
  })
  it('names the right and middle buttons', () => {
    expect(pointerToEvent('up', { ...noMods, clientX: 0, clientY: 0, button: 2 }, box, frame)!.button).toBe('right')
    expect(pointerToEvent('up', { ...noMods, clientX: 0, clientY: 0, button: 1 }, box, frame)!.button).toBe('middle')
  })
  it('a move reports the held button from the buttons bitmask and no clicks', () => {
    expect(pointerToEvent('move', { ...noMods, clientX: 10, clientY: 10, button: 0, buttons: 1 }, box, frame))
      .toMatchObject({ action: 'move', button: 'left', clicks: 0 })
    expect(pointerToEvent('move', { ...noMods, clientX: 10, clientY: 10, button: 0, buttons: 0 }, box, frame)!.button).toBe('none')
  })
  it('clamps the click count to the range the browser understands', () => {
    expect(pointerToEvent('down', { ...noMods, clientX: 1, clientY: 1, button: 0 }, box, frame, 0)!.clicks).toBe(1)
    expect(pointerToEvent('down', { ...noMods, clientX: 1, clientY: 1, button: 0 }, box, frame, 9)!.clicks).toBe(3)
  })
  it('drops a pointer in the letterbox', () => {
    expect(pointerToEvent('down', { ...noMods, clientX: 10, clientY: 10, button: 0 }, { left: 0, top: 0, width: 1000, height: 400 }, frame)).toBeNull()
  })
})

describe('wheelToEvent', () => {
  const box = { left: 0, top: 0, width: 640, height: 400 }
  it('passes pixel deltas through, rounded', () => {
    expect(wheelToEvent({ ...noMods, clientX: 320, clientY: 200, deltaX: 0, deltaY: 53.4, deltaMode: 0 }, box, frame))
      .toEqual({ type: 'wheel', x: 640, y: 400, dx: 0, dy: 53, modifiers: 0 })
  })
  it('scales line and page deltas to pixels', () => {
    expect(wheelToEvent({ ...noMods, clientX: 0, clientY: 0, deltaX: 0, deltaY: 3, deltaMode: 1 }, box, frame)!.dy).toBe(48)
    expect(wheelToEvent({ ...noMods, clientX: 0, clientY: 0, deltaX: 0, deltaY: 1, deltaMode: 2 }, box, frame)!.dy).toBe(800)
  })
})

describe('keyToEvent', () => {
  it('carries text on a printable key down only', () => {
    expect(keyToEvent('down', { ...noMods, key: 'a', code: 'KeyA' })).toEqual({ type: 'key', action: 'down', key: 'a', code: 'KeyA', modifiers: 0, text: 'a' })
    expect(keyToEvent('up', { ...noMods, key: 'a', code: 'KeyA' })).not.toHaveProperty('text')
    expect(keyToEvent('down', { ...noMods, key: 'Enter', code: 'Enter' })).not.toHaveProperty('text')
  })
  it('drops the text under Ctrl or Meta, which are shortcuts, not typing', () => {
    expect(keyToEvent('down', { ...noMods, ctrlKey: true, key: 'a', code: 'KeyA' })).not.toHaveProperty('text')
    expect(keyToEvent('down', { ...noMods, metaKey: true, key: 'v', code: 'KeyV' })).not.toHaveProperty('text')
    expect(keyToEvent('down', { ...noMods, shiftKey: true, key: 'A', code: 'KeyA' }).text).toBe('A')
  })
  it('Esc is the hand-back key and nothing else is', () => {
    expect(isHandBackKey('Escape')).toBe(true)
    expect(isHandBackKey('Enter')).toBe(false)
  })
})
