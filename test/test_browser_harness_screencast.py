"""The live stream: wire shape, the ladder on a fake clock, backpressure, watchdog, mask."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from browser_live_helpers import (
    CHROME_SESSION,
    SESSION,
    FakeRelay,
    frame_event,
    make_jpeg,
    make_stream,
)

from kiro_crew.browser_harness import screencast as sc

# ── wire shape ─────────────────────────────────────────────────────────────


def test_frame_header_round_trips_through_the_binary_message() -> None:
    header = sc.frame_header("chat-1", 7, 1280, 768, [[10, 20, 30, 40]])
    blob = sc.encode_frame(header, b"\xff\xd8jpeg")
    assert int.from_bytes(blob[:4], "big") == len(blob) - 4 - len(b"\xff\xd8jpeg")
    decoded, jpeg = sc.decode_frame(blob)
    assert decoded == {
        "type": "browser_frame",
        "session_key": "chat-1",
        "seq": 7,
        "w": 1280,
        "h": 768,
        "mask": [[10, 20, 30, 40]],
    }
    assert jpeg == b"\xff\xd8jpeg"


def test_header_length_prefix_is_four_byte_big_endian() -> None:
    blob = sc.encode_frame({"a": 1}, b"")
    assert blob[:4] == b"\x00\x00\x00\x07"
    assert blob[4:] == b'{"a":1}'


@pytest.mark.parametrize(
    "blob",
    [b"", b"\x00\x00", b"\x00\x00\x00\x09{}", b"\xff\xff\xff\xff{}", b"\x00\x00\x00\x02[]"],
)
def test_decode_refuses_short_oversized_or_non_object_headers(blob: bytes) -> None:
    with pytest.raises(ValueError):
        sc.decode_frame(blob)


def test_jpeg_size_reads_the_first_sof_segment() -> None:
    assert sc.jpeg_size(make_jpeg(640, 384)) == (640, 384)
    assert sc.jpeg_size(b"not a jpeg") is None
    assert sc.jpeg_size(b"\xff\xd8\xff\xd9") is None


def test_the_ladder_is_the_rfc_table() -> None:
    assert [(r.fps, r.quality, r.width) for r in sc.LADDER] == [
        (60, 60, 1280),
        (45, 55, 1280),
        (30, 50, 1280),
        (30, 45, 960),
        (20, 40, 960),
        (15, 40, 800),
        (10, 35, 640),
        (5, 30, 640),
    ]
    assert sc.LADDER[sc.TAKEOVER_RUNG].fps == 15


def test_live_config_reads_browser_live_with_defaults() -> None:
    assert sc.LiveConfig.from_config(SimpleNamespace()) == sc.LiveConfig()
    cfg = SimpleNamespace(
        browser=SimpleNamespace(
            live=SimpleNamespace(enabled=False, quality="low", max_fps=30, mask_passwords=False)
        )
    )
    live = sc.LiveConfig.from_config(cfg)
    assert live == sc.LiveConfig(enabled=False, quality="low", max_fps=30, mask_passwords=False)
    # max_fps 30 alone would start at rung 2; quality "low" pins the top to rung 5.
    assert live.best_rung() == 5
    assert sc.LiveConfig(max_fps=30).best_rung() == 2
    assert sc.LiveConfig(max_fps=15).best_rung() == 5
    bad = SimpleNamespace(browser=SimpleNamespace(live=SimpleNamespace(max_fps=7, quality="x")))
    assert sc.LiveConfig.from_config(bad) == sc.LiveConfig()


# ── the controller, pure ───────────────────────────────────────────────────


def _feed(ctrl: sc.AdaptiveController, frames: int, nbytes: int, delivery: float) -> None:
    for _ in range(frames):
        ctrl.observe(nbytes, delivery)


def test_controller_holds_the_top_rung_on_a_fast_link() -> None:
    ctrl = sc.AdaptiveController()
    _feed(ctrl, 60, 40_000, 0.002)
    stats = ctrl.tick()
    assert stats.rung == 0 and stats.fps == 60 and stats.late_share == 0


def test_controller_steps_down_when_more_than_a_fifth_arrive_late() -> None:
    ctrl = sc.AdaptiveController()
    period = sc.LADDER[0].period
    # 15 of 60 late (25%) on a link that is otherwise fast: one rung down.
    _feed(ctrl, 45, 40_000, 0.002)
    _feed(ctrl, 15, 40_000, period * 2)
    assert ctrl.tick().rung == 1
    # 10 of 60 late (17%): calm, holds.
    _feed(ctrl, 50, 40_000, 0.002)
    _feed(ctrl, 10, 40_000, period * 3)
    assert ctrl.tick().rung == 1


def test_controller_jumps_as_far_down_as_the_measured_link_needs() -> None:
    ctrl = sc.AdaptiveController()
    # 40 kB frames taking 0.2 s each: a 200 kB/s link. Rung 0 needs 2.4 MB/s.
    _feed(ctrl, 5, 40_000, 0.2)
    stats = ctrl.tick()
    assert stats.rung > 1
    need = 40_000 * (sc.LADDER[stats.rung].quality / 60) * (sc.LADDER[stats.rung].width / 1280) ** 2
    assert need * sc.LADDER[stats.rung].fps <= 200_000 * sc.DOWN_TARGET_UTILISATION


def test_controller_steps_up_after_two_calm_windows_with_headroom() -> None:
    ctrl = sc.AdaptiveController()
    ctrl.rung = 4
    # Frames at rung 4 (q40, 960) of 15 kB delivered in 1 ms: a very fast link.
    _feed(ctrl, 20, 15_000, 0.001)
    assert ctrl.tick().rung == 4  # calm 1
    _feed(ctrl, 20, 15_000, 0.001)
    assert ctrl.tick().rung == 0  # calm 2: straight to the best rung the link covers


def test_controller_climbs_only_to_the_rung_the_link_covers() -> None:
    ctrl = sc.AdaptiveController()
    ctrl.rung = 5
    # 10 kB frames taking 10 ms: a 1 MB/s link. Rung 0 would need about 6 MB/s.
    _feed(ctrl, 15, 10_000, 0.01)
    ctrl.tick()
    _feed(ctrl, 15, 10_000, 0.01)
    after = ctrl.tick().rung
    assert 0 < after < 5


def test_controller_bounds_clamp_the_current_rung() -> None:
    ctrl = sc.AdaptiveController()
    assert ctrl.set_bounds(floor=sc.TAKEOVER_RUNG) == sc.TAKEOVER_RUNG
    _feed(ctrl, 15, 10_000, 0.001)
    ctrl.tick()
    _feed(ctrl, 15, 10_000, 0.001)
    assert ctrl.tick().rung == sc.TAKEOVER_RUNG  # never climbs past the floor
    assert ctrl.set_bounds(floor=0) == sc.TAKEOVER_RUNG
    assert ctrl.set_bounds(ceiling=2) == 2


# ── the stream against a fake relay and clock ──────────────────────────────


@pytest.mark.asyncio
async def test_start_attaches_a_relay_session_and_starts_at_the_top_rung() -> None:
    stream, relay, _clock, _sink = make_stream()
    await stream.start()
    assert relay.attached == ["target-1"] and stream.session_id == SESSION
    assert relay.calls[0] == ("Page.enable", {}, SESSION)
    method, params, sid = relay.calls[1]
    assert method == "Page.startScreencast" and sid == SESSION
    assert params == {
        "format": "jpeg",
        "quality": 60,
        "maxWidth": 1280,
        "maxHeight": 768,
        "everyNthFrame": 1,
    }
    await stream.stop()
    assert relay.methods("Page.stopScreencast") and relay.detached == [SESSION]


@pytest.mark.asyncio
async def test_a_frame_reaches_the_sink_then_chrome_is_acked_on_the_grid() -> None:
    stream, relay, clock, sink = make_stream(config=sc.LiveConfig(mask_passwords=False))
    await stream.start()
    jpeg = make_jpeg(1280, 768)
    for _ in range(3):
        await relay.on_event(frame_event(jpeg))
    assert [h["seq"] for h, _ in sink.frames] == [1, 2, 3]
    header, blob = sink.frames[0]
    assert header == {
        "type": "browser_frame",
        "session_key": "chat-1",
        "seq": 1,
        "w": 1280,
        "h": 768,
        "mask": [],
    }
    assert sc.decode_frame(blob)[1] == jpeg
    acks = relay.methods("Page.screencastFrameAck")
    assert [p for _, p, _ in acks] == [{"sessionId": CHROME_SESSION}] * 3
    # Target-time pacing on a fixed grid: the first ack is immediate (its slot is
    # ``now - period``), the second lands on ``now`` and the third waits one period.
    assert clock.sleeps == [pytest.approx(sc.LADDER[0].period)]
    assert stream.metrics.frames == 3 and stream.metrics.bytes == 3 * len(jpeg)


@pytest.mark.asyncio
async def test_backpressure_a_card_that_never_acks_makes_frames_late_and_steps_down() -> None:
    stream, relay, clock, sink = make_stream(
        ack_on_arrival=False, config=sc.LiveConfig(mask_passwords=False)
    )
    await stream.start()
    before = clock.now
    await relay.on_event(frame_event(make_jpeg(1280, 768)))
    # The frame waited the whole card-ack bound, then Chrome was acked anyway.
    assert clock.now - before >= sc.CARD_ACK_TIMEOUT_S
    assert len(relay.methods("Page.screencastFrameAck")) == 1
    starts_before = len(relay.methods("Page.startScreencast"))
    stats = await stream.tick()
    assert stats.late_share == 1.0 and stats.rung >= 1
    # Rung 0 -> 1 changes quality, so the screencast restarted on the same session.
    assert stats.restarted is True
    assert len(relay.methods("Page.stopScreencast")) == 1
    assert len(relay.methods("Page.startScreencast")) == starts_before + 1
    assert relay.methods("Page.startScreencast")[-1][1]["quality"] == sc.LADDER[stats.rung].quality


@pytest.mark.asyncio
async def test_a_late_card_ack_is_still_accepted_once_the_frame_moved_on() -> None:
    stream, relay, _clock, _sink = make_stream(
        ack_on_arrival=False, config=sc.LiveConfig(mask_passwords=False)
    )
    await stream.start()
    await relay.on_event(frame_event(make_jpeg(640, 384)))
    assert stream.ack(1) is False  # nothing waits on it any more
    assert stream.ack(99) is False


@pytest.mark.asyncio
async def test_only_a_quality_or_size_change_restarts_the_screencast() -> None:
    stream, relay, _clock, _sink = make_stream(config=sc.LiveConfig(mask_passwords=False))
    await stream.start()
    starts = len(relay.methods("Page.startScreencast"))
    # Same (quality, width): pacing only, Chrome is not touched.
    assert await stream._apply_rung(3, 3) is False
    assert len(relay.methods("Page.startScreencast")) == starts
    # q60 -> q55 at the same width: a restart on the same session.
    stream.controller.rung = 1
    assert await stream._apply_rung(0, 1) is True
    assert len(relay.methods("Page.stopScreencast")) == 1
    last = relay.methods("Page.startScreencast")[-1]
    assert last[2] == SESSION and last[1]["quality"] == 55 and last[1]["maxWidth"] == 1280
    # 1280 -> 960 is a size change: another restart.
    stream.controller.rung = 3
    assert await stream._apply_rung(2, 3) is True
    assert relay.methods("Page.startScreencast")[-1][1]["maxWidth"] == 960


@pytest.mark.asyncio
async def test_off_screen_stops_the_screencast_and_visible_starts_it_again() -> None:
    stream, relay, _clock, sink = make_stream(config=sc.LiveConfig(mask_passwords=False))
    await stream.start()
    await stream.set_visible(False)
    assert len(relay.methods("Page.stopScreencast")) == 1 and stream.casting is False
    # A rung change while hidden does not touch Chrome.
    assert await stream._apply_rung(0, 3) is False
    await stream.set_visible(True)
    assert len(relay.methods("Page.startScreencast")) == 2 and stream.casting is True
    await stream.set_visible(True)  # idempotent
    assert len(relay.methods("Page.startScreencast")) == 2


@pytest.mark.asyncio
async def test_watchdog_restarts_after_input_with_no_frame_for_three_seconds() -> None:
    stream, relay, clock, _sink = make_stream(config=sc.LiveConfig(mask_passwords=False))
    await stream.start()
    stream.note_input()
    clock.now += sc.WATCHDOG_S - 0.5
    assert (await stream.tick()).restarted is False
    clock.now += 0.5
    stats = await stream.tick()
    assert stats.restarted is True and stream.metrics.restarts == 1
    # Fires once per input: the next tick with no new input does nothing.
    clock.now += 10
    assert (await stream.tick()).restarted is False


@pytest.mark.asyncio
async def test_a_frame_after_input_clears_the_watchdog() -> None:
    stream, relay, clock, _sink = make_stream(config=sc.LiveConfig(mask_passwords=False))
    await stream.start()
    stream.note_input()
    clock.now += 1
    await relay.on_event(frame_event(make_jpeg(640, 384)))
    clock.now += sc.WATCHDOG_S
    assert (await stream.tick()).restarted is False


@pytest.mark.asyncio
async def test_password_boxes_are_asked_for_scaled_and_rate_limited() -> None:
    stream, relay, clock, sink = make_stream()
    # Two password fields in CSS px of a 1280-wide viewport; the frame is 640 wide.
    relay.answers["Runtime.evaluate"] = {
        "result": {"value": [[100, 50, 200, 30], [0, 0, 0, 0], "junk"]}
    }
    await stream.start()
    await relay.on_event(frame_event(make_jpeg(640, 384), device_w=1280))
    header, _ = sink.frames[0]
    assert header["mask"] == [[50, 25, 101, 16]]
    method, params, sid = relay.methods("Runtime.evaluate")[0]
    assert sid == SESSION and params["returnByValue"] is True
    assert "input[type=password]" in params["expression"]
    # Within the interval the cached boxes are reused: no second evaluate.
    clock.now += sc.MASK_MIN_INTERVAL_S / 2
    await relay.on_event(frame_event(make_jpeg(640, 384), device_w=1280))
    assert len(relay.methods("Runtime.evaluate")) == 1
    assert sink.frames[1][0]["mask"] == [[50, 25, 101, 16]]
    clock.now += sc.MASK_MIN_INTERVAL_S
    await relay.on_event(frame_event(make_jpeg(640, 384), device_w=1280))
    assert len(relay.methods("Runtime.evaluate")) == 2


@pytest.mark.asyncio
async def test_a_failed_mask_query_keeps_the_last_boxes_rather_than_unmasking() -> None:
    stream, relay, clock, sink = make_stream()
    relay.answers["Runtime.evaluate"] = {"result": {"value": [[10, 10, 20, 20]]}}
    await stream.start()
    await relay.on_event(frame_event(make_jpeg(1280, 768)))
    relay.fail.add("Runtime.evaluate")
    clock.now += 1
    await relay.on_event(frame_event(make_jpeg(1280, 768)))
    assert sink.frames[1][0]["mask"] == [[10, 10, 21, 21]]


@pytest.mark.asyncio
async def test_mask_off_in_config_never_asks_the_page() -> None:
    stream, relay, _clock, sink = make_stream(config=sc.LiveConfig(mask_passwords=False))
    await stream.start()
    await relay.on_event(frame_event(make_jpeg(1280, 768)))
    assert not relay.methods("Runtime.evaluate") and sink.frames[0][0]["mask"] == []


@pytest.mark.asyncio
async def test_takeover_caps_the_stream_at_fifteen_fps_and_releases_after() -> None:
    stream, relay, _clock, _sink = make_stream(config=sc.LiveConfig(mask_passwords=False))
    await stream.start()
    await stream.set_takeover(True)
    assert stream.rung == sc.TAKEOVER_RUNG
    assert relay.methods("Page.startScreencast")[-1][1]["maxWidth"] == 800
    await stream.set_takeover(False)
    assert stream.controller.floor == 0 and stream.rung == sc.TAKEOVER_RUNG  # climbs by measurement


@pytest.mark.asyncio
async def test_frames_are_ignored_after_stop_and_bad_payloads_are_dropped() -> None:
    stream, relay, _clock, sink = make_stream(config=sc.LiveConfig(mask_passwords=False))
    await stream.start()
    await relay.on_event({"method": "Page.screencastFrame", "params": {"data": "%%%"}})
    await relay.on_event({"method": "Page.loadEventFired", "params": {}})
    assert sink.frames == []
    await stream.stop()
    await relay.on_event(frame_event(make_jpeg(640, 384)))
    assert sink.frames == [] and not relay.methods("Page.screencastFrameAck")


@pytest.mark.asyncio
async def test_on_tick_reports_each_window_to_the_hub() -> None:
    seen: list[sc.TickStats] = []

    async def on_tick(stats: sc.TickStats) -> None:
        seen.append(stats)

    relay = FakeRelay()
    stream = sc.LiveStream(
        relay,
        "t",
        SimpleNamespace(frame=None),  # never reached: no frames pushed
        clock=lambda: 0.0,
        sleep=_no_sleep,
        auto_control=False,
        on_tick=on_tick,
    )
    await stream.start()
    await stream.tick()
    assert len(seen) == 1 and seen[0].frames == 0 and seen[0].rung == 0


async def _no_sleep(_s: float) -> None:
    return None
