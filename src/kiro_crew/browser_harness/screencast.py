"""The live stream: one ``Page.startScreencast`` on a relay-owned session, paced
by the card and adapted to the link.

Shape, from the measured relay spike (RFC "Live browser in chat", section 3):

* **Source.** ``Page.startScreencast`` (JPEG) on a flat session the relay owns, so
  no frame ever reaches Harness's shared event buffer.
* **Pacing.** Chrome is acked only after the card has acked the frame's ``seq``
  (or :data:`CARD_ACK_TIMEOUT_S` passed), on a fixed time grid, so transit time
  counts toward the frame interval instead of adding to it.
* **Ladder.** Once a second :class:`AdaptiveController` picks one of the eight
  :data:`LADDER` rungs: down at once when more than 20% of frames arrive late or
  the link cannot carry the rung, as far as the measured rate needs; up after two
  calm seconds, straight to the best rung the link covers with 30% headroom. An
  fps change is pacing only; a quality or width change restarts the screencast
  on the same session.
* **Off screen, off.** :meth:`LiveStream.set_visible` stops the screencast while
  the card is hidden and starts it again when it shows.
* **Watchdog.** Input sent with no frame for :data:`WATCHDOG_S` restarts the
  screencast (a Windows headless run stalled one tab at 0-2 fps in the spike).
* **Password mask.** Before a frame is sent the page is asked, at most every
  :data:`MASK_MIN_INTERVAL_S`, for the boxes of visible password fields; the card
  paints over them. Off when ``browser.live.mask_passwords`` is false.

Every clock read and every sleep goes through the two callables the constructor
takes, so the ladder, the pacing and the watchdog run against a fake clock in
tests with no real waiting (Determinism contract D2/D3). Nothing here imports the
dashboard: the frame leaves through the injected :class:`FrameSink`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import struct
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: ``type`` of the JSON header that precedes every binary frame on the owner websocket.
FRAME_HEADER_TYPE = "browser_frame"

#: Big-endian header-length prefix: ``>I``, four bytes.
_HEADER_LEN = struct.Struct(">I")

#: Longest JSON header the decoder accepts; a real one is under 200 bytes.
MAX_HEADER_BYTES = 64 * 1024


@dataclass(frozen=True)
class Rung:
    """One step of the ladder: frames per second, JPEG quality, frame width."""

    fps: int
    quality: int
    width: int

    @property
    def period(self) -> float:
        return 1.0 / self.fps

    @property
    def height(self) -> int:
        """The matching ``maxHeight``: a 5:3 box, as the spike measured."""
        return int(self.width * 0.6)


#: The eight rungs from the RFC, best first. Index 0 is the 60 fps target.
LADDER: tuple[Rung, ...] = (
    Rung(60, 60, 1280),
    Rung(45, 55, 1280),
    Rung(30, 50, 1280),
    Rung(30, 45, 960),
    Rung(20, 40, 960),
    Rung(15, 40, 800),
    Rung(10, 35, 640),
    Rung(5, 30, 640),
)

#: The lowest rung whose fps is at most 15: the stream's rate during takeover.
TAKEOVER_RUNG = next(i for i, r in enumerate(LADDER) if r.fps <= 15)

#: Seconds without a frame after an input before the screencast is restarted.
WATCHDOG_S = 3.0
#: Longest wait for the card's ack before the frame counts as late and Chrome is
#: acked anyway, so a card that stopped acking slows the stream instead of
#: freezing it.
CARD_ACK_TIMEOUT_S = 2.0
#: Shortest interval between two password-box queries to the page.
MASK_MIN_INTERVAL_S = 0.25
#: A frame is late when it reaches the card after this many periods.
LATE_PERIODS = 1.5
#: Share of late frames in one window that steps the ladder down.
LATE_SHARE_DOWN = 0.2
#: Calm windows (no step-down trigger) before the ladder may step up.
CALM_WINDOWS_UP = 2
#: Headroom the measured link must have over a higher rung's need to climb to it.
UP_HEADROOM = 1.3
#: A rung is kept only while its need stays under this share of the link rate.
DOWN_UTILISATION = 0.9
#: A step-down lands on the first rung whose estimate fits this share of the link.
DOWN_TARGET_UTILISATION = 0.8
#: Seconds between two controller ticks.
TICK_S = 1.0

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class LiveConfig:
    """The ``browser.live.*`` settings the stream reads, with the RFC defaults.

    Built from a loaded config through :meth:`from_config`; every read is a
    ``getattr`` with a default, so the stream works before the settings section
    exists and after a key is removed.
    """

    enabled: bool = True
    quality: str = "auto"
    max_fps: int = 60
    mask_passwords: bool = True

    @classmethod
    def from_config(cls, cfg: Any) -> "LiveConfig":
        browser = getattr(cfg, "browser", None)
        live = getattr(browser, "live", None)
        if live is None:
            return cls()
        max_fps = getattr(live, "max_fps", 60)
        quality = str(getattr(live, "quality", "auto") or "auto")
        return cls(
            enabled=bool(getattr(live, "enabled", True)),
            quality=quality if quality in _QUALITY_FLOORS else "auto",
            max_fps=int(max_fps) if max_fps in (15, 30, 60) else 60,
            mask_passwords=bool(getattr(live, "mask_passwords", True)),
        )

    def best_rung(self) -> int:
        """The highest rung this config admits: the ``max_fps`` and ``quality`` floors."""
        rung = 0
        while rung < len(LADDER) - 1 and LADDER[rung].fps > self.max_fps:
            rung += 1
        return max(rung, _QUALITY_FLOORS[self.quality])


#: ``browser.live.quality`` as the rung a fixed quality pins the ladder's top to.
_QUALITY_FLOORS: dict[str, int] = {"auto": 0, "high": 0, "medium": 3, "low": 5}


def encode_frame(header: Mapping[str, Any], jpeg: bytes) -> bytes:
    """The owner-websocket binary message: 4-byte header length, JSON header, JPEG."""
    head = json.dumps(dict(header), separators=(",", ":")).encode("utf-8")
    return _HEADER_LEN.pack(len(head)) + head + jpeg


def decode_frame(blob: bytes) -> tuple[dict[str, Any], bytes]:
    """Split a binary frame message back into its header and JPEG bytes.

    Raises ``ValueError`` on a short, oversized or non-object header: the card's
    decoder and the tests share this one definition of the wire shape.
    """
    if len(blob) < _HEADER_LEN.size:
        raise ValueError("frame shorter than its length prefix")
    (length,) = _HEADER_LEN.unpack_from(blob, 0)
    if length > MAX_HEADER_BYTES or len(blob) < _HEADER_LEN.size + length:
        raise ValueError("frame header length out of range")
    start = _HEADER_LEN.size
    header = json.loads(blob[start : start + length].decode("utf-8"))
    if not isinstance(header, dict):
        raise ValueError("frame header is not a JSON object")
    return header, blob[start + length :]


def frame_header(
    session_key: str, seq: int, width: int, height: int, mask: list[list[int]]
) -> dict[str, Any]:
    """The header the card reads, with exactly the Wire-contract fields."""
    return {
        "type": FRAME_HEADER_TYPE,
        "session_key": session_key,
        "seq": seq,
        "w": width,
        "h": height,
        "mask": mask,
    }


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Width and height from a JPEG's first frame header, or ``None``.

    Walks the marker segments to the first SOF marker (``FFC0``..``FFCF`` minus the
    DHT/JPG/DAC markers) and reads its height and width. No decoder, no PIL.
    """
    if len(data) < 4 or data[0] != 0xFF or data[1] != 0xD8:
        return None
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if pos + 4 > len(data):
            return None
        (length,) = struct.unpack_from(">H", data, pos + 2)
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if pos + 9 > len(data):
                return None
            height, width = struct.unpack_from(">HH", data, pos + 5)
            return width, height
        pos += 2 + length
    return None


@dataclass
class _Window:
    """One controller window's measurements, reset every tick."""

    frames: int = 0
    bytes: int = 0
    late: int = 0
    delivery_s: float = 0.0


@dataclass
class TickStats:
    """What one controller tick measured and decided; the hub reads it for the card."""

    rung: int
    fps: float
    frames: int
    late_share: float
    kb_per_frame: float
    restarted: bool = False


class AdaptiveController:
    """The pure ladder decision: measurements in, rung out. No I/O, no clock."""

    def __init__(self, *, floor: int = 0, ceiling: int = len(LADDER) - 1) -> None:
        self.rung = max(0, min(floor, ceiling))
        self.floor = floor
        self.ceiling = ceiling
        self.calm = 0
        self.window = _Window()

    def observe(self, nbytes: int, delivery_s: float) -> None:
        """Record one frame: its size and the time from send to the card's ack."""
        w = self.window
        w.frames += 1
        w.bytes += nbytes
        w.delivery_s += delivery_s
        if delivery_s > LATE_PERIODS * LADDER[self.rung].period:
            w.late += 1

    def set_bounds(self, *, floor: int | None = None, ceiling: int | None = None) -> int:
        """Narrow or widen the rungs in play; the current rung is clamped into them."""
        if floor is not None:
            self.floor = max(0, min(floor, len(LADDER) - 1))
        if ceiling is not None:
            self.ceiling = max(0, min(ceiling, len(LADDER) - 1))
        if self.floor > self.ceiling:
            self.floor = self.ceiling
        self.rung = max(self.floor, min(self.rung, self.ceiling))
        return self.rung

    def tick(self) -> TickStats:
        """Close the window and pick the next rung. Returns what it measured."""
        w, self.window = self.window, _Window()
        cur = LADDER[self.rung]
        n = max(w.frames, 1)
        per_frame = w.bytes / n
        # Link rate: bytes the card took divided by the time it took to take them.
        # A window with no measurable delivery time is a link faster than we can see.
        link_rate = w.bytes / w.delivery_s if w.delivery_s > 1e-3 and w.bytes else float("inf")
        late_share = w.late / n if w.frames else 0.0
        stats = TickStats(
            rung=self.rung,
            fps=float(w.frames) / TICK_S,
            frames=w.frames,
            late_share=round(late_share, 3),
            kb_per_frame=round(per_frame / 1024, 1),
        )

        def _need(r: Rung) -> float:
            return per_frame * (r.quality / cur.quality) * (r.width / cur.width) ** 2 * r.fps

        if w.frames and (late_share > LATE_SHARE_DOWN or _need(cur) > link_rate * DOWN_UTILISATION):
            self.calm = 0
            target = self.rung + 1
            while target < self.ceiling:
                if _need(LADDER[target]) <= link_rate * DOWN_TARGET_UTILISATION:
                    break
                target += 1
            self.rung = min(target, self.ceiling)
        else:
            self.calm += 1
            if self.calm >= CALM_WINDOWS_UP and self.rung > self.floor:
                best = self.rung
                for r in range(self.rung - 1, self.floor - 1, -1):
                    if _need(LADDER[r]) * UP_HEADROOM <= link_rate:
                        best = r
                    else:
                        break
                if best != self.rung:
                    self.calm = 0
                    self.rung = best
        stats.rung = self.rung
        return stats


class FrameSink(Protocol):
    """Where a finished frame goes: the hub, which sends it to owner websockets."""

    async def frame(self, header: Mapping[str, Any], blob: bytes) -> None: ...


class Relay(Protocol):
    """The slice of ``CdpRelay`` this module uses (CONTRACT ``relay.py``)."""

    async def send(
        self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None
    ) -> dict[str, Any]: ...

    async def attach(
        self, target_id: str, on_event: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> str: ...

    async def detach(self, session_id: str) -> None: ...


#: Boxes of visible password fields, in CSS pixels of the viewport. Read-only,
#: no page state touched; ``returnByValue`` keeps the reply a plain array.
_MASK_JS = (
    "(()=>{const r=[];for(const e of document.querySelectorAll('input[type=password]'))"
    "{const b=e.getBoundingClientRect();if(b.width>0&&b.height>0)"
    "r.push([b.left,b.top,b.width,b.height]);}return r;})()"
)


@dataclass
class _Metrics:
    frames: int = 0
    bytes: int = 0
    seq: int = 0
    last_frame_at: float = field(default=-1.0)
    restarts: int = 0


class LiveStream:
    """One screencast on one agent target, delivered to one card.

    ``relay``, ``target_id`` and ``sink`` are the contract; ``session_key`` names
    the card. ``clock`` and ``sleep`` default to the real monotonic clock and
    ``asyncio.sleep`` and are the only time the class reads. With
    ``auto_control=False`` the once-a-second tick is the caller's to drive
    (tests, or a hub that ticks every stream from one timer).
    """

    def __init__(
        self,
        relay: Relay,
        target_id: str,
        sink: FrameSink,
        *,
        session_key: str = "",
        config: LiveConfig | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        auto_control: bool = True,
        on_tick: Callable[[TickStats], Awaitable[None]] | None = None,
    ) -> None:
        self.relay = relay
        self.target_id = target_id
        self.sink = sink
        self.session_key = session_key
        self.config = config or LiveConfig()
        self._clock = clock
        self._sleep = sleep
        self._auto_control = auto_control
        self._on_tick = on_tick
        self.controller = AdaptiveController(floor=self.config.best_rung())
        self.session_id = ""
        self.metrics = _Metrics()
        self.running = False
        self.visible = True
        self.casting = False
        self._last_ack_due = -1.0
        self._frame_lock = asyncio.Lock()
        self._acks: dict[int, asyncio.Event] = {}
        self._input_at = -1.0
        self._input_pending = False
        self._mask_at = -1.0
        self._mask: list[list[int]] = []
        self._control_task: asyncio.Task[None] | None = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    @property
    def rung(self) -> int:
        return self.controller.rung

    async def start(self) -> None:
        """Attach the relay-owned session and start the screencast at the top rung."""
        if self.running:
            return
        self.session_id = await self.relay.attach(self.target_id, self._on_event)
        self.running = True
        await self.relay.send("Page.enable", {}, self.session_id)
        if self.visible:
            await self._start_cast()
        if self._auto_control:
            self._control_task = asyncio.ensure_future(self._control_loop())

    async def stop(self) -> None:
        """Stop the screencast, detach the session, release every waiter."""
        if not self.running:
            return
        self.running = False
        task, self._control_task = self._control_task, None
        if task is not None:
            task.cancel()
        try:
            if self.casting:
                await self._stop_cast()
        finally:
            for ev in self._acks.values():
                ev.set()
            self._acks.clear()
            try:
                await self.relay.detach(self.session_id)
            except Exception:
                logger.debug("live stream detach failed", exc_info=True)

    async def _start_cast(self) -> None:
        r = LADDER[self.controller.rung]
        await self.relay.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": r.quality,
                "maxWidth": r.width,
                "maxHeight": r.height,
                "everyNthFrame": 1,
            },
            self.session_id,
        )
        self.casting = True

    async def _stop_cast(self) -> None:
        self.casting = False
        await self.relay.send("Page.stopScreencast", {}, self.session_id)

    async def restart(self) -> None:
        """Stop and start the screencast on the same session (quality/size change, watchdog)."""
        if not self.running or not self.visible:
            return
        self.metrics.restarts += 1
        if self.casting:
            await self._stop_cast()
        await self._start_cast()

    # ── card-side signals ──────────────────────────────────────────────────

    async def set_visible(self, visible: bool) -> None:
        """Off screen, off: stop the screencast while the card is hidden."""
        visible = bool(visible)
        if visible == self.visible:
            return
        self.visible = visible
        if not self.running:
            return
        if visible:
            await self._start_cast()
        elif self.casting:
            await self._stop_cast()

    def ack(self, seq: int) -> bool:
        """The card received frame ``seq``. Returns whether a frame was waiting on it."""
        ev = self._acks.get(seq)
        if ev is None:
            return False
        ev.set()
        return True

    def note_input(self) -> None:
        """An input was dispatched to the page; the watchdog starts counting."""
        self._input_at = self._clock()
        self._input_pending = True

    async def set_takeover(self, active: bool) -> None:
        """Cap the stream at the takeover rung while the person holds input."""
        before = self.controller.rung
        if active:
            after = self.controller.set_bounds(floor=max(TAKEOVER_RUNG, self.config.best_rung()))
        else:
            after = self.controller.set_bounds(floor=self.config.best_rung())
        if after != before:
            await self._apply_rung(before, after)

    # ── frames ─────────────────────────────────────────────────────────────

    async def _on_event(self, msg: dict[str, Any]) -> None:
        if msg.get("method") != "Page.screencastFrame" or not self.running:
            return
        params = msg.get("params") or {}
        async with self._frame_lock:
            if not self.running:
                return
            try:
                await self._frame(params)
            except Exception:
                logger.debug("live frame dropped", exc_info=True)

    async def _frame(self, params: dict[str, Any]) -> None:
        data = params.get("data")
        if not isinstance(data, str):
            return
        try:
            jpeg = base64.b64decode(data, validate=True)
        except (ValueError, TypeError):
            return
        now = self._clock()
        self.metrics.last_frame_at = now
        self._input_pending = False
        meta = params.get("metadata") or {}
        size = jpeg_size(jpeg)
        if size is None:
            size = (int(meta.get("deviceWidth") or 0), int(meta.get("deviceHeight") or 0))
        width, height = size
        mask = await self._mask_rects(meta, width, height)
        self.metrics.seq += 1
        seq = self.metrics.seq
        header = frame_header(self.session_key, seq, width, height, mask)
        waiter = asyncio.Event()
        self._acks[seq] = waiter
        t0 = self._clock()
        try:
            await self.sink.frame(header, encode_frame(header, jpeg))
            await self._wait_card_ack(waiter)
        finally:
            self._acks.pop(seq, None)
        if not self.running:
            return
        took = self._clock() - t0
        self.metrics.frames += 1
        self.metrics.bytes += len(jpeg)
        self.controller.observe(len(jpeg), took)
        await self._paced_ack(params.get("sessionId"))

    async def _wait_card_ack(self, waiter: asyncio.Event) -> bool:
        """Wait for the card's ack, bounded by :data:`CARD_ACK_TIMEOUT_S` on the injected sleep."""
        if waiter.is_set():
            return True
        wait = asyncio.ensure_future(waiter.wait())
        timeout = asyncio.ensure_future(self._sleep(CARD_ACK_TIMEOUT_S))
        try:
            await asyncio.wait({wait, timeout}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (wait, timeout):
                if not t.done():
                    t.cancel()
        return waiter.is_set()

    async def _paced_ack(self, chrome_session: Any) -> None:
        """Ack Chrome at the next slot of a fixed grid: target-time pacing."""
        period = LADDER[self.controller.rung].period
        now = self._clock()
        due = max(self._last_ack_due + period, now - period)
        if due > now:
            await self._sleep(due - now)
        self._last_ack_due = due
        if not self.running or not self.casting:
            return
        try:
            await self.relay.send(
                "Page.screencastFrameAck", {"sessionId": chrome_session}, self.session_id
            )
        except Exception:
            # A late ack after a restart or stop is Chrome telling us the frame is
            # gone; the next frame carries its own session id.
            logger.debug("late screencastFrameAck", exc_info=True)

    async def _mask_rects(
        self, meta: Mapping[str, Any], width: int, height: int
    ) -> list[list[int]]:
        """Password boxes in frame pixels, re-read at most every :data:`MASK_MIN_INTERVAL_S`."""
        if not self.config.mask_passwords:
            return []
        now = self._clock()
        if self._mask_at >= 0 and now - self._mask_at < MASK_MIN_INTERVAL_S:
            return list(self._mask)
        self._mask_at = now
        try:
            reply = await self.relay.send(
                "Runtime.evaluate",
                {"expression": _MASK_JS, "returnByValue": True},
                self.session_id,
            )
        except Exception:
            # Keep the last boxes rather than unmask on a transient failure.
            return list(self._mask)
        boxes = ((reply.get("result") or {}).get("value")) if isinstance(reply, dict) else None
        device_w = float(meta.get("deviceWidth") or 0) or float(width or 0)
        scale = (width / device_w) if device_w and width else 1.0
        rects: list[list[int]] = []
        if isinstance(boxes, list):
            for box in boxes[:32]:
                if not (isinstance(box, list) and len(box) == 4):
                    continue
                try:
                    x, y, w, h = (float(v) for v in box)
                except (TypeError, ValueError):
                    continue
                if w <= 0 or h <= 0:
                    continue
                rects.append(
                    [
                        max(0, int(x * scale)),
                        max(0, int(y * scale)),
                        max(1, int(w * scale + 1)),
                        max(1, int(h * scale + 1)),
                    ]
                )
        self._mask = rects
        return list(rects)

    # ── control ────────────────────────────────────────────────────────────

    async def _control_loop(self) -> None:
        while self.running:
            await self._sleep(TICK_S)
            if not self.running:
                return
            try:
                await self.tick()
            except Exception:
                logger.debug("live stream tick failed", exc_info=True)

    async def tick(self) -> TickStats:
        """One controller window: pick the rung, run the watchdog, report."""
        before = self.controller.rung
        stats = self.controller.tick()
        if stats.rung != before:
            stats.restarted = await self._apply_rung(before, stats.rung)
        if await self._watchdog():
            stats.restarted = True
        if self._on_tick is not None:
            await self._on_tick(stats)
        return stats

    async def _apply_rung(self, before: int, after: int) -> bool:
        """Apply a rung change: fps is pacing only; quality or width restarts the cast."""
        old, new = LADDER[before], LADDER[after]
        if (old.quality, old.width) == (new.quality, new.width):
            return False
        if self.running and self.visible:
            await self.restart()
            return True
        return False

    async def _watchdog(self) -> bool:
        """Input sent and no frame for :data:`WATCHDOG_S`: restart the screencast once."""
        if not self._input_pending or not self.running or not self.visible:
            return False
        if self._clock() - self._input_at < WATCHDOG_S:
            return False
        self._input_pending = False
        await self.restart()
        return True


__all__ = [
    "AdaptiveController",
    "CARD_ACK_TIMEOUT_S",
    "FRAME_HEADER_TYPE",
    "FrameSink",
    "LADDER",
    "LiveConfig",
    "LiveStream",
    "MASK_MIN_INTERVAL_S",
    "Relay",
    "Rung",
    "TAKEOVER_RUNG",
    "TickStats",
    "WATCHDOG_S",
    "decode_frame",
    "encode_frame",
    "frame_header",
    "jpeg_size",
]
