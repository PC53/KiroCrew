"""The live hub: per-session state behind the chat's browser card.

One :class:`LiveHub` per gateway holds a :class:`LiveSession` per session key: the
turn it belongs to, the step strip, the card's state, the stream and the input
lock. The ``browser`` tool reports through :meth:`LiveHub.turn_started`,
:meth:`LiveHub.step` and :meth:`LiveHub.turn_ended` and asks
:meth:`LiveHub.in_control` before every op; the card's routes
(``dashboard/handlers/browser_live.py``) call the rest.

Two things leave the hub, both to OWNER websockets only, through the two senders
:meth:`LiveHub.bind` installs: the ``browser_live`` JSON event
(:func:`live_event`) on every state change, and the binary frame the stream
built. The hub imports nothing from the dashboard; the dashboard binds itself in.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.browser_harness.screencast import (
    LiveConfig,
    LiveStream,
    Relay,
    TickStats,
)
from kiro_crew.browser_harness.takeover import USER, InputLock, dispatch_input

logger = logging.getLogger(__name__)

#: Owner-websocket event name for a card state change.
LIVE_EVENT = "browser_live"

#: The card states, in the Wire contract's words.
STATE_WAITING_ALLOW = "waiting_allow"
STATE_LIVE = "live"
STATE_IN_CONTROL = "in_control"
STATE_DONE = "done"
STATE_LOGIN_NEEDED = "login_needed"
STATE_ERROR = "error"
STATES = frozenset(
    {
        STATE_WAITING_ALLOW,
        STATE_LIVE,
        STATE_IN_CONTROL,
        STATE_DONE,
        STATE_LOGIN_NEEDED,
        STATE_ERROR,
    }
)

#: Longest step label kept; the tool's labels are a few words.
MAX_STEP_LABEL = 120
#: Steps kept per turn; older ones fold into the count.
MAX_STEPS = 200

Publish = Callable[[str, Mapping[str, Any]], Awaitable[Any]]
SendFrame = Callable[[bytes], Awaitable[Any]]


@dataclass
class Step:
    label: str
    status: str = "active"  # done | active | pending


@dataclass
class LiveSession:
    """Everything the card shows for one session, plus the stream feeding it."""

    session_key: str
    turn_id: str = ""
    state: str = ""
    url: str = ""
    title: str = ""
    browser: str = ""
    headless: bool = False
    steps: list[Step] = field(default_factory=list)
    lock: InputLock = field(default_factory=InputLock)
    stream: LiveStream | None = None
    visible: bool = True
    rung: int = 0
    fps: float = 0.0
    started_at: float = 0.0
    ended_at: float = 0.0

    @property
    def relay(self) -> Relay | None:
        return self.stream.relay if self.stream is not None else None


def live_event(sess: LiveSession) -> dict[str, Any]:
    """The ``browser_live`` payload: exactly the Wire-contract fields."""
    active = next((i for i, s in enumerate(sess.steps) if s.status == "active"), None)
    step = (
        {"index": active, "label": sess.steps[active].label}
        if active is not None
        else {"index": len(sess.steps) - 1, "label": sess.steps[-1].label} if sess.steps else None
    )
    return {
        "session_key": sess.session_key,
        "turn_id": sess.turn_id,
        "state": sess.state,
        "url": sess.url,
        "title": sess.title,
        "browser": sess.browser,
        "headless": bool(sess.headless),
        "step": step,
        "steps": [{"label": s.label, "status": s.status} for s in sess.steps],
        "rung": int(sess.rung),
        "fps": float(sess.fps),
    }


class _StreamSink:
    """The stream's :class:`FrameSink`, bound to one session of the hub."""

    def __init__(self, hub: "LiveHub", sess: LiveSession) -> None:
        self._hub = hub
        self._sess = sess

    async def frame(self, header: Mapping[str, Any], blob: bytes) -> None:
        await self._hub._send_frame_blob(self._sess, blob)


class LiveHub:
    """Per-gateway registry of live sessions. Construct one; bind the dashboard in."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._sessions: dict[str, LiveSession] = {}
        self._publish: Publish | None = None
        self._send_frame: SendFrame | None = None
        self._config = LiveConfig()
        self._clock = clock
        self._tasks: set[asyncio.Task[Any]] = set()

    # ── wiring ─────────────────────────────────────────────────────────────

    def bind(self, publish: Publish | None, send_frame: SendFrame | None) -> None:
        """Install the owner-only senders (JSON events, binary frames)."""
        self._publish = publish
        self._send_frame = send_frame

    def configure(self, cfg: Any) -> None:
        """Read ``browser.live.*`` from a loaded config; applies to streams started after."""
        self._config = LiveConfig.from_config(cfg)

    @property
    def config(self) -> LiveConfig:
        return self._config

    def get(self, session_key: str) -> LiveSession | None:
        return self._sessions.get(session_key)

    def _session(self, session_key: str) -> LiveSession:
        sess = self._sessions.get(session_key)
        if sess is None:
            sess = LiveSession(session_key=session_key)
            self._sessions[session_key] = sess
        return sess

    # ── the tool's side ────────────────────────────────────────────────────

    def turn_started(self, session_key: str, turn_id: str) -> None:
        """A turn began browsing: reset the strip; the card attaches to this turn."""
        sess = self._session(session_key)
        sess.turn_id = str(turn_id)
        sess.steps = []
        sess.started_at = self._clock()
        sess.ended_at = 0.0
        if sess.state in ("", STATE_DONE):
            sess.state = ""
        self._emit(sess)

    def step(self, session_key: str, label: str) -> None:
        """The agent is on a new step: the previous active one is done."""
        sess = self._session(session_key)
        text = str(label or "")[:MAX_STEP_LABEL]
        for s in sess.steps:
            if s.status == "active":
                s.status = "done"
        sess.steps.append(Step(label=text))
        if len(sess.steps) > MAX_STEPS:
            del sess.steps[: len(sess.steps) - MAX_STEPS]
        self._emit(sess)

    def turn_ended(self, session_key: str) -> None:
        """The turn is over: the stream stops, the lock opens, the card folds to one line."""
        sess = self._sessions.get(session_key)
        if sess is None:
            return
        for s in sess.steps:
            if s.status == "active":
                s.status = "done"
        sess.lock.release()
        sess.ended_at = self._clock()
        sess.state = STATE_DONE
        stream, sess.stream = sess.stream, None
        if stream is not None:
            self._spawn(stream.stop())
        self._emit(sess)

    def in_control(self, session_key: str) -> bool:
        """Whether the person holds the input lock; an agent op answers ``user_in_control``."""
        sess = self._sessions.get(session_key)
        return sess is not None and sess.lock.held()

    # ── the supervisor's side ──────────────────────────────────────────────

    def set_state(
        self,
        session_key: str,
        state: str,
        *,
        url: str | None = None,
        title: str | None = None,
        browser: str | None = None,
        headless: bool | None = None,
    ) -> None:
        """A state the supervisor knows (waiting_allow, login_needed, error) or a page change."""
        if state not in STATES:
            raise ValueError(f"unknown live state {state!r}")
        sess = self._session(session_key)
        if state == STATE_IN_CONTROL and not sess.lock.held():
            state = STATE_LIVE
        sess.state = state
        self._update_page(sess, url=url, title=title, browser=browser, headless=headless)
        self._emit(sess)

    def page_changed(self, session_key: str, *, url: str = "", title: str = "") -> None:
        sess = self._session(session_key)
        self._update_page(sess, url=url, title=title)
        self._emit(sess)

    @staticmethod
    def _update_page(
        sess: LiveSession,
        *,
        url: str | None = None,
        title: str | None = None,
        browser: str | None = None,
        headless: bool | None = None,
    ) -> None:
        if url is not None:
            sess.url = str(url)[:2048]
        if title is not None:
            sess.title = str(title)[:300]
        if browser is not None:
            sess.browser = str(browser)
        if headless is not None:
            sess.headless = bool(headless)

    async def start_stream(
        self,
        session_key: str,
        relay: Relay,
        target_id: str,
        *,
        browser: str,
        headless: bool,
        url: str = "",
        title: str = "",
        stream_factory: Callable[..., LiveStream] | None = None,
    ) -> LiveStream | None:
        """Start the screencast of the agent's target for this session's card.

        Returns ``None`` when the live view is off in config. Replaces a stream that
        is already running (the agent moved to another tab).
        """
        sess = self._session(session_key)
        self._update_page(sess, url=url, title=title, browser=browser, headless=headless)
        if not self._config.enabled:
            sess.state = STATE_LIVE if not sess.lock.held() else STATE_IN_CONTROL
            self._emit(sess)
            return None
        old, sess.stream = sess.stream, None
        if old is not None:
            await old.stop()
        factory = stream_factory or LiveStream
        stream = factory(
            relay,
            target_id,
            _StreamSink(self, sess),
            session_key=session_key,
            config=self._config,
            on_tick=self._tick_for(sess),
        )
        stream.visible = sess.visible
        sess.stream = stream
        await stream.start()
        if sess.lock.held():
            await stream.set_takeover(True)
        sess.rung = stream.rung
        sess.state = STATE_IN_CONTROL if sess.lock.held() else STATE_LIVE
        self._emit(sess)
        return stream

    async def stop_stream(self, session_key: str) -> None:
        sess = self._sessions.get(session_key)
        if sess is None or sess.stream is None:
            return
        stream, sess.stream = sess.stream, None
        await stream.stop()

    def _tick_for(self, sess: LiveSession) -> Callable[[TickStats], Awaitable[None]]:
        async def _on_tick(stats: TickStats) -> None:
            changed = stats.rung != sess.rung or round(stats.fps) != round(sess.fps)
            sess.rung, sess.fps = stats.rung, stats.fps
            if changed:
                await self._emit_now(sess)

        return _on_tick

    # ── the card's side ────────────────────────────────────────────────────

    async def set_visible(self, session_key: str, visible: bool) -> None:
        sess = self._session(session_key)
        sess.visible = bool(visible)
        if sess.stream is not None:
            await sess.stream.set_visible(sess.visible)

    def ack(self, session_key: str, seq: int) -> bool:
        sess = self._sessions.get(session_key)
        if sess is None or sess.stream is None:
            return False
        return sess.stream.ack(int(seq))

    async def claim(self, session_key: str) -> bool:
        """The person takes input. ``False`` only when someone else holds it."""
        sess = self._session(session_key)
        if not sess.lock.claim(USER):
            return False
        if sess.stream is not None:
            await sess.stream.set_takeover(True)
        sess.state = STATE_IN_CONTROL
        await self._emit_now(sess)
        return True

    async def release(self, session_key: str) -> None:
        sess = self._sessions.get(session_key)
        if sess is None:
            return
        was_held = sess.lock.held()
        sess.lock.release()
        if sess.stream is not None:
            await sess.stream.set_takeover(False)
        if sess.state == STATE_IN_CONTROL:
            sess.state = STATE_LIVE
        if was_held:
            await self._emit_now(sess)

    async def input_event(self, session_key: str, event: dict[str, Any]) -> int:
        """Dispatch one card event. Raises ``InputEventError`` or ``LookupError``."""
        sess = self._sessions.get(session_key)
        if sess is None or not sess.lock.held() or sess.lock.owner != USER:
            raise LookupError("not_in_control")
        stream = sess.stream
        if stream is None or not stream.session_id:
            raise LookupError("no_stream")
        sent = await dispatch_input(stream.relay, stream.session_id, event)
        stream.note_input()
        return sent

    async def open_in_browser(self, session_key: str) -> bool:
        """Focus the real window the stream shows (``Page.bringToFront`` on the target)."""
        sess = self._sessions.get(session_key)
        if sess is None or sess.stream is None or not sess.stream.session_id:
            return False
        stream = sess.stream
        try:
            await stream.relay.send("Target.activateTarget", {"targetId": stream.target_id})
            await stream.relay.send("Page.bringToFront", {}, stream.session_id)
        except Exception:
            logger.debug("open-in-browser failed", exc_info=True)
            return False
        return True

    # ── delivery ───────────────────────────────────────────────────────────

    async def _send_frame_blob(self, sess: LiveSession, blob: bytes) -> None:
        if self._send_frame is None:
            return
        try:
            await self._send_frame(blob)
        except Exception:
            logger.debug("live frame send failed", exc_info=True)

    async def _emit_now(self, sess: LiveSession) -> None:
        if self._publish is None or not sess.state:
            return
        try:
            await self._publish(LIVE_EVENT, live_event(sess))
        except Exception:
            logger.debug("browser_live publish failed", exc_info=True)

    def _emit(self, sess: LiveSession) -> None:
        """Publish from a sync caller: scheduled on the running loop, dropped without one."""
        if self._publish is None or not sess.state:
            return
        self._spawn(self._emit_now(sess))

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return
        task: asyncio.Task[Any] = loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        """Stop every stream (gateway shutdown)."""
        for sess in list(self._sessions.values()):
            stream, sess.stream = sess.stream, None
            if stream is not None:
                try:
                    await stream.stop()
                except Exception:
                    logger.debug("live stream stop failed", exc_info=True)
        for task in list(self._tasks):
            task.cancel()


_HUB: LiveHub | None = None


def get_live_hub() -> LiveHub:
    """The gateway's one hub, created on first use."""
    global _HUB
    if _HUB is None:
        _HUB = LiveHub()
    return _HUB


def reset_live_hub() -> None:
    """Drop the singleton (tests)."""
    global _HUB
    _HUB = None


__all__ = [
    "LIVE_EVENT",
    "LiveHub",
    "LiveSession",
    "STATES",
    "STATE_DONE",
    "STATE_ERROR",
    "STATE_IN_CONTROL",
    "STATE_LIVE",
    "STATE_LOGIN_NEEDED",
    "STATE_WAITING_ALLOW",
    "Step",
    "get_live_hub",
    "live_event",
    "reset_live_hub",
]
