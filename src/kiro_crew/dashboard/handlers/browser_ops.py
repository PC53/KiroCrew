"""``POST /api/browser/op`` -- run one ``browser`` MCP tool op on the Harness.

The ``browser`` tool lives in the ``kirocrew-core`` stdio shim, a separate
process, so it cannot call ``HarnessSupervisor.call`` in-process: it posts the
op here and this handler runs :func:`kiro_crew.browser_harness.ops.run_op`
against the gateway's one supervisor. MACHINE-only: the route is in
``_STRICT_INTERNAL_API_PATHS`` and the handler re-asserts
``request["internal_auth"] is True`` (set only on a verified
``X-Internal-Secret``), mirroring ``api_computer_use_invoke``. It does NOT also
require a loopback ``request.remote``: the shim reaches the gateway over the
AF_UNIX internal-API socket, where ``remote`` is empty.

Body: ``{"session_key": str, "op": str, "args": object}``. ``session_key`` is the
caller's chat slot, which the live card and the input lock are keyed by.

Responses (always HTTP 200 once authorised, so the shim reads one shape):

* ``{"ok": true, "result": {...}}`` -- the op ran;
* ``{"ok": false, "code": str, "error": str}`` -- the op answered a refusal
  (``user_in_control``, ``password_field``, ``ref_stale``, ...), or this
  handler's own: ``not_installed`` (no Harness on this host), ``browser_error``
  (the daemon or browser failed), ``timeout`` (the op ran past its budget).

The URL gate (public http(s) only, plus the operator's listed loopback origins)
and the ``capabilities.browse`` governance check run in the shim BEFORE the
post; this route trusts its one caller for those and enforces what only the
gateway can see: who holds the browser, which tabs the agent owns.

The live card (``browser_harness.live_hub``) is driven from here too, because
this is the one place that knows both the session and the browser: the FIRST
op of a turn opens the hub's turn (a fresh ``turn_id``; the chat runner closes
it from ``_end_turn_tail``), every op is a step, and after each op the card
follows the daemon's current tab -- a stream is started on it, or restarted
when the agent moved to another tab, else only the address pill is updated.
While the owner's Chrome shows its Allow sheet the card reads ``waiting_allow``.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import uuid
from typing import Any

from aiohttp import web

from kiro_crew.browser_harness import ops as harness_ops
from kiro_crew.browser_harness.live_hub import (
    STATE_DONE,
    STATE_WAITING_ALLOW,
    LiveHub,
    get_live_hub,
)
from kiro_crew.config.paths import config_dir
from kiro_crew.dashboard.state import _normalize_slot_key

logger = logging.getLogger(__name__)

OP_TIMEOUT_S = 20.0
SLOW_OP_TIMEOUT_S = 75.0
#: How long an op waits for the browser to come up (Kiro's Chromium launch, or the
#: owner's Allow click) before answering ``browser_error``.
ENSURE_TIMEOUT_S = 60.0
#: How often the wait above re-reads the supervisor's connection for the card.
_ENSURE_POLL_S = 0.25


def _sel():
    """Late-binding ``sel()``: this module is imported BY ``handlers/__init__``."""
    from kiro_crew.dashboard import handlers

    return handlers.sel()


def _audit(request: web.Request, outcome: str, error: str = "") -> None:
    try:
        _sel().log_api_access(
            caller=str(request.get("user") or request.remote or ""),
            operation="browser_op",
            outcome=outcome,
            source="browser",
            resources=request.path,
            error=error or None,
        )
    except Exception:  # noqa: BLE001 - auditing never fails the request
        logger.debug("browser op audit failed", exc_info=True)


def _live_handlers() -> Any:
    """Late-binding import of the card's handler module (imported BY ``handlers/__init__``)."""
    from kiro_crew.dashboard.handlers import browser_live

    return browser_live


def live_session_key(session_key: str) -> str:
    """The hub key for a caller's session: the dashboard slot key the card is keyed by.

    The shim resolves its session as ``dashboard:<slot>`` or the bare slot; the card
    posts ``activeSlot``. Folding both through the slot directory's normaliser is
    what makes the frame, the event and the input lock meet on one key.
    """
    return _normalize_slot_key(session_key) or session_key


def build_context(
    session_key: str, supervisor: Any, hub: LiveHub | None = None
) -> harness_ops.OpContext:
    """Bind the gateway's supervisor, relay and live hub to one caller's session."""
    hub = hub if hub is not None else get_live_hub()

    def _step(label: str) -> Any:
        return hub.step(session_key, label)

    def _in_control() -> Any:
        return hub.in_control(session_key)

    def _agent_targets() -> list[str]:
        relay = getattr(supervisor, "relay", None)
        return list(relay.agent_targets()) if relay is not None else []

    return harness_ops.OpContext(
        helper=supervisor.call,
        step=_step,
        in_control=_in_control,
        agent_targets=_agent_targets,
        screenshot_dir=harness_ops.screenshot_dir(config_dir()),
        sleep=asyncio.sleep,
    )


async def _supervisor(hub: LiveHub, key: str) -> Any:
    """The gateway's supervisor with the daemon running, or None when not installed.

    The wait for ``ensure()`` is bounded and watched: while the supervisor reports
    ``waiting_allow`` the card is told so, which is how the person learns that
    Chrome is asking for a click.
    """
    home = config_dir()
    if not harness_ops.browsing_available(home):
        return None
    sup = importlib.import_module("kiro_crew.browser_harness.supervisor")
    instance = sup.get_supervisor(home)
    task = instance.ensure_in_background()
    deadline = asyncio.get_running_loop().time() + ENSURE_TIMEOUT_S
    told_allow = False
    while True:
        done, _pending = await asyncio.wait({task}, timeout=_ENSURE_POLL_S)
        if done:
            task.result()  # raises what ensure() raised
            return instance
        status = instance.status()
        if status.get("connection") == sup.WAITING_ALLOW and not told_allow:
            told_allow = True
            browser = status.get("browser") or {}
            hub.set_state(
                key,
                STATE_WAITING_ALLOW,
                browser=browser.get("kind"),
                headless=browser.get("headless"),
            )
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(str(status.get("detail") or "the browser did not come up in time"))


def _open_turn(hub: LiveHub, key: str) -> None:
    """The first browser op of a turn opens the card's turn; later ops join it."""
    sess = hub.get(key)
    if sess is None or not sess.turn_id or sess.state == STATE_DONE:
        hub.turn_started(key, uuid.uuid4().hex)


async def follow_current_tab(hub: LiveHub, key: str, supervisor: Any) -> None:
    """Point the session's stream at the daemon's current tab; update the address pill.

    One cheap IPC call (``current_tab``) per op. A new target (first op, ``tabs
    new``/``switch``) starts or restarts the screencast on it; the same target only
    refreshes url and title. Failures are logged, never surfaced: the op itself
    already answered.
    """
    relay = getattr(supervisor, "relay", None)
    if relay is None:
        return
    try:
        tab = await supervisor.call("current_tab")
    except Exception:  # noqa: BLE001 - the card is best-effort beside the op
        logger.debug("browser live: current_tab failed", exc_info=True)
        return
    if not isinstance(tab, dict):
        return
    target = str(tab.get("targetId") or tab.get("target_id") or "")
    if not target:
        return
    url = str(tab.get("url") or "")
    title = str(tab.get("title") or "")
    sess = hub.get(key)
    stream = sess.stream if sess is not None else None
    if stream is not None and getattr(stream, "target_id", None) == target:
        hub.page_changed(key, url=url, title=title)
        return
    browser = (supervisor.status().get("browser") or {}) if hasattr(supervisor, "status") else {}
    _live_handlers().refresh_live_config(hub)
    try:
        await hub.start_stream(
            key,
            relay,
            target,
            browser=str(browser.get("kind") or "kiro_chromium"),
            headless=bool(browser.get("headless", True)),
            url=url,
            title=title,
        )
    except Exception:  # noqa: BLE001 - a screencast that will not start leaves the op intact
        logger.debug("browser live: start_stream failed", exc_info=True)


async def api_browser_op(request: web.Request) -> web.Response:
    """POST /api/browser/op -- see the module docstring."""
    if request.get("internal_auth") is not True:
        _audit(request, "denied", "internal-secret authentication required")
        return web.json_response({"error": "loopback only", "code": "loopback_only"}, status=403)
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    op = body.get("op")
    args = body.get("args")
    session_key = body.get("session_key")
    if not isinstance(op, str) or op not in harness_ops.OPS:
        return web.json_response({"error": "unknown op", "code": "bad_op"}, status=400)
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return web.json_response(
            {"error": "args must be an object", "code": "args_must_be_object"}, status=400
        )
    if not isinstance(session_key, str) or not session_key:
        return web.json_response(
            {"error": "session_key required", "code": "session_key_required"}, status=400
        )
    hub = get_live_hub()
    key = live_session_key(session_key)
    _open_turn(hub, key)
    try:
        supervisor = await _supervisor(hub, key)
    except Exception as exc:  # noqa: BLE001 - the browser plane failed to come up
        logger.warning("browser op: supervisor unavailable: %s", exc)
        return web.json_response({"ok": False, "code": "browser_error", "error": _safe(exc)})
    if supervisor is None:
        return web.json_response(
            {
                "ok": False,
                "code": "not_installed",
                "error": "browsing is not set up on this host; install it from Settings -> Browser",
            }
        )
    ctx = build_context(key, supervisor, hub)
    budget = SLOW_OP_TIMEOUT_S if op in harness_ops.SLOW_OPS else OP_TIMEOUT_S
    try:
        result = await asyncio.wait_for(harness_ops.run_op(op, args, ctx), timeout=budget)
    except asyncio.TimeoutError:
        _audit(request, "error", "timeout")
        return web.json_response(
            {"ok": False, "code": "timeout", "error": f"{op} ran past {budget:g}s"}
        )
    except Exception as exc:  # noqa: BLE001 - a helper or the daemon raised
        logger.warning("browser op %s failed: %s", op, exc)
        _audit(request, "error", type(exc).__name__)
        return web.json_response({"ok": False, "code": "browser_error", "error": _safe(exc)})
    _audit(request, "allowed")
    if not hub.in_control(key):
        await follow_current_tab(hub, key, supervisor)
    if result.get("ok"):
        return web.json_response({"ok": True, "result": result})
    return web.json_response(result)


def _safe(exc: BaseException) -> str:
    """One line of the failure, bounded, so a daemon traceback never floods the model."""
    text = str(exc).strip().splitlines()
    return (text[0] if text else type(exc).__name__)[:300]
