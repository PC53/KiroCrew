"""Route registration for the live browser card (visibility, acks, takeover, open-in-browser).

Browser-called and cookie-authed, like the computer-use config pair in
``system.py``: these are the dashboard owner's own controls over the agent's
browser, so they are deliberately NOT on the strict-internal list and the
handlers refuse the internal secret outright (``handlers/browser_live.py``).
Registered after ``system`` because every path here is a literal under
``/api/browser/live/`` and ``/api/browser/input/``; nothing earlier in the
table patterns over those prefixes.
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.dashboard import handlers


async def _close_live_hub(_app: web.Application) -> None:
    from kiro_crew.browser_harness.live_hub import get_live_hub

    await get_live_hub().close()


def register(app: web.Application) -> None:
    """Register the live browser card routes on *app*, and wire the hub to its state.

    ``app["state"]`` is set before the route table is registered, so the hub's two
    owner-only senders (``deliver_ws_owners`` for ``browser_live`` events,
    ``deliver_ws_owners_bytes`` for frames) are bound here, and every stream is
    stopped on cleanup.
    """
    state = app.get("state")
    if state is not None:
        handlers.bind_live_hub(state)
        app.on_cleanup.append(_close_live_hub)
    app.router.add_post("/api/browser/live/visibility", handlers.api_browser_live_visibility)
    app.router.add_post("/api/browser/live/ack", handlers.api_browser_live_ack)
    app.router.add_post(
        "/api/browser/live/open-in-browser", handlers.api_browser_live_open_in_browser
    )
    app.router.add_post("/api/browser/input/claim", handlers.api_browser_input_claim)
    app.router.add_post("/api/browser/input/release", handlers.api_browser_input_release)
    app.router.add_post("/api/browser/input/event", handlers.api_browser_input_event)
