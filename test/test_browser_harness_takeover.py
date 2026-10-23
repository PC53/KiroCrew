"""Takeover: the input lock and the card-event-to-CDP mapping."""

from __future__ import annotations

import pytest
from browser_live_helpers import SESSION, FakeRelay

from kiro_crew.browser_harness import takeover as tk


def test_lock_refuses_the_agent_while_the_person_holds_it() -> None:
    lock = tk.InputLock()
    assert lock.held() is False
    assert lock.claim(tk.USER) is True
    assert lock.held() is True and lock.owner == tk.USER
    assert lock.claim(tk.AGENT) is False
    assert lock.claim(tk.USER) is True  # re-claim by the holder is idempotent
    lock.release()
    assert lock.held() is False and lock.claim(tk.AGENT) is True


def test_lock_refuses_an_empty_owner() -> None:
    assert tk.InputLock().claim("") is False


@pytest.mark.parametrize(
    "raw",
    [
        "not an object",
        {"type": "touch", "x": 1, "y": 1},
        {"type": "mouse", "x": -1, "y": 0},
        {"type": "mouse", "x": tk.MAX_COORD + 1, "y": 0},
        {"type": "mouse", "x": True, "y": 0},
        {"type": "mouse", "x": 1, "y": 1, "button": "pinky"},
        {"type": "mouse", "x": 1, "y": 1, "action": "click"},  # a press needs a button
        {"type": "mouse", "x": 1, "y": 1, "button": "left", "clicks": 9},
        {"type": "mouse", "x": 1, "y": 1, "button": "left", "action": "jiggle"},
        {"type": "wheel", "x": 1, "y": 1, "dx": tk.MAX_DELTA + 1},
        {"type": "key"},
        {"type": "key", "key": "a" * (tk.MAX_KEY_CHARS + 1)},
        {"type": "key", "text": "a" * (tk.MAX_TEXT_CHARS + 1)},
        {"type": "key", "key": "a", "action": "hold"},
        {"type": "key", "key": "a", "modifiers": 16},
    ],
)
def test_malformed_events_are_refused_before_anything_is_sent(raw: object) -> None:
    with pytest.raises(tk.InputEventError) as info:
        tk.parse_input_event(raw)
    assert info.value.code == "invalid_input_event"


def test_a_click_is_a_press_and_a_release() -> None:
    ev = tk.parse_input_event({"type": "mouse", "x": 10, "y": 20, "button": "left"})
    assert tk.cdp_calls(ev) == [
        (
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "button": "left",
                "clickCount": 1,
                "x": 10,
                "y": 20,
                "modifiers": 0,
            },
        ),
        (
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "button": "left",
                "clickCount": 1,
                "x": 10,
                "y": 20,
                "modifiers": 0,
            },
        ),
    ]


def test_a_double_click_counts_up_and_a_bare_move_is_a_move() -> None:
    ev = tk.parse_input_event({"type": "mouse", "x": 1, "y": 2, "button": "left", "clicks": 2})
    counts = [p["clickCount"] for _, p in tk.cdp_calls(ev)]
    assert counts == [1, 1, 2, 2]
    move = tk.parse_input_event({"type": "mouse", "x": 3, "y": 4})
    assert tk.cdp_calls(move) == [
        ("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 3, "y": 4, "modifiers": 0})
    ]


def test_down_and_up_map_separately_for_drags() -> None:
    down = tk.parse_input_event(
        {"type": "mouse", "x": 1, "y": 1, "button": "left", "action": "down"}
    )
    up = tk.parse_input_event({"type": "mouse", "x": 9, "y": 9, "button": "left", "action": "up"})
    assert [p["type"] for _, p in tk.cdp_calls(down)] == ["mousePressed"]
    assert [p["type"] for _, p in tk.cdp_calls(up)] == ["mouseReleased"]


def test_a_wheel_event_carries_its_deltas() -> None:
    ev = tk.parse_input_event(
        {"type": "wheel", "x": 5, "y": 6, "dx": -3, "dy": 120, "modifiers": 2}
    )
    assert tk.cdp_calls(ev) == [
        (
            "Input.dispatchMouseEvent",
            {"type": "mouseWheel", "x": 5, "y": 6, "deltaX": -3, "deltaY": 120, "modifiers": 2},
        )
    ]


def test_a_key_press_is_key_down_with_text_then_key_up() -> None:
    ev = tk.parse_input_event({"type": "key", "key": "a", "code": "KeyA", "text": "a"})
    assert tk.cdp_calls(ev) == [
        (
            "Input.dispatchKeyEvent",
            {
                "modifiers": 0,
                "key": "a",
                "code": "KeyA",
                "type": "keyDown",
                "text": "a",
                "unmodifiedText": "a",
            },
        ),
        ("Input.dispatchKeyEvent", {"type": "keyUp", "modifiers": 0, "key": "a", "code": "KeyA"}),
    ]
    raw = tk.parse_input_event({"type": "key", "key": "Escape", "action": "down"})
    assert tk.cdp_calls(raw) == [
        ("Input.dispatchKeyEvent", {"modifiers": 0, "key": "Escape", "type": "rawKeyDown"})
    ]


@pytest.mark.asyncio
async def test_dispatch_sends_on_the_relay_session_and_refuses_before_sending() -> None:
    relay = FakeRelay()
    sent = await tk.dispatch_input(
        relay, SESSION, {"type": "mouse", "x": 1, "y": 2, "button": "right"}
    )
    assert sent == 2
    assert [(m, s) for m, _, s in relay.calls] == [("Input.dispatchMouseEvent", SESSION)] * 2
    with pytest.raises(tk.InputEventError):
        await tk.dispatch_input(relay, SESSION, {"type": "key"})
    assert len(relay.calls) == 2
