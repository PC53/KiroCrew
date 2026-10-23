"""The live card's routes: owner only, internal secret refused, session ownership, one hub call each.

The handlers are mounted behind stand-in middlewares (the way the computer-use API
tests do): ``_as`` sets the identity the real ``token_auth`` middleware would. The
real owner predicate (``is_owner_dashboard_request``) and the real hub run; the
stream behind the hub is the ``FakeStream`` the hub tests use.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from browser_live_helpers import FakeRelay
from test_browser_live_hub import FakeStream, Wire

from kiro_crew.browser_harness import live_hub as lh
from kiro_crew.dashboard import routes as routes_pkg
from kiro_crew.dashboard.handlers import browser_live as mod
from kiro_crew.dashboard.routes import browser_live as route_slice

pytestmark = pytest.mark.asyncio

ROUTES = (
    "/api/browser/live/visibility",
    "/api/browser/live/ack",
    "/api/browser/live/open-in-browser",
    "/api/browser/input/claim",
    "/api/browser/input/release",
    "/api/browser/input/event",
)


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(mod, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None))
    lh.reset_live_hub()
    yield
    lh.reset_live_hub()


def _identity(kind: str):
    """``owner``: the dashboard owner's cookie. ``app``: an app token. ``internal``:
    the verified internal secret. ``anon``: nothing."""

    @web.middleware
    async def middleware(request: web.Request, handler):
        if kind == "owner":
            request["user"], request["app"] = "local-app", ""
        elif kind == "app":
            request["user"], request["app"] = "some-app", "some-app"
        elif kind == "internal":
            request["internal_auth"] = True
            request["user"], request["app"] = "local-app", ""
        return await handler(request)

    return middleware


def _app(kind: str = "owner") -> tuple[web.Application, Wire]:
    app = web.Application(middlewares=[_identity(kind)])
    wire = Wire()
    state = SimpleNamespace(
        owner_id="",
        _slots={"chat-1": object()},
        deliver_ws_owners=wire.publish,
        deliver_ws_owners_bytes=wire.send_frame,
    )
    app["state"] = state
    route_slice.register(app)
    return app, wire


async def _post(client: TestClient, path: str, body: Any) -> tuple[int, dict[str, Any]]:
    resp = await client.post(path, json=body)
    return resp.status, await resp.json()


async def _live(session_key: str = "chat-1") -> tuple[FakeStream, FakeRelay]:
    relay = FakeRelay()
    stream = await lh.get_live_hub().start_stream(
        session_key, relay, "tgt", browser="edge", headless=False, stream_factory=FakeStream
    )
    assert isinstance(stream, FakeStream)
    return stream, relay


@pytest.mark.parametrize("path", ROUTES)
async def test_the_internal_secret_is_refused_on_every_route(path: str) -> None:
    app, _wire = _app("internal")
    async with TestClient(TestServer(app)) as client:
        status, body = await _post(client, path, {"session_key": "chat-1"})
    assert status == 403 and body["code"] == "internal_secret_required"


@pytest.mark.parametrize("kind", ["app", "anon"])
@pytest.mark.parametrize("path", ROUTES)
async def test_a_non_owner_is_refused_on_every_route(kind: str, path: str) -> None:
    app, _wire = _app(kind)
    async with TestClient(TestServer(app)) as client:
        status, body = await _post(client, path, {"session_key": "chat-1"})
    assert status == 403 and body["code"] == "owner_only"


@pytest.mark.parametrize("key", ["chat-2", "", "../x", "a" * 129, 7, None])
async def test_a_session_this_dashboard_does_not_own_is_unknown(key: Any) -> None:
    app, _wire = _app()
    async with TestClient(TestServer(app)) as client:
        status, body = await _post(client, "/api/browser/live/ack", {"session_key": key, "seq": 1})
    assert status == 404 and body["code"] == "unknown_session"


async def test_the_dashboard_prefix_folds_onto_the_bare_slot_key() -> None:
    app, _wire = _app()
    await _live()
    async with TestClient(TestServer(app)) as client:
        status, body = await _post(
            client, "/api/browser/live/ack", {"session_key": "dashboard:chat-1", "seq": 3}
        )
    assert status == 200 and body == {"ok": True, "session_key": "chat-1", "acked": True}


async def test_a_non_json_body_is_refused() -> None:
    app, _wire = _app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/browser/input/claim", data=b"[]", headers={"Content-Type": "application/json"}
        )
        assert resp.status == 400
        resp = await client.post(
            "/api/browser/input/claim", data=b"x", headers={"Content-Type": "text/plain"}
        )
        assert resp.status == 415


async def test_visibility_reaches_the_stream_and_refuses_a_non_boolean() -> None:
    app, _wire = _app()
    stream, _relay = await _live()
    async with TestClient(TestServer(app)) as client:
        status, body = await _post(
            client, "/api/browser/live/visibility", {"session_key": "chat-1", "visible": False}
        )
        assert status == 200 and body["visible"] is False
        assert ("visible", False) in stream.calls
        status, body = await _post(
            client, "/api/browser/live/visibility", {"session_key": "chat-1", "visible": "no"}
        )
        assert status == 400 and body["code"] == "invalid_visible"


async def test_ack_validates_seq_and_reports_whether_a_frame_waited() -> None:
    app, _wire = _app()
    async with TestClient(TestServer(app)) as client:
        status, body = await _post(
            client, "/api/browser/live/ack", {"session_key": "chat-1", "seq": 1}
        )
        assert status == 200 and body["acked"] is False  # no stream yet
        for bad in (-1, True, "1", None):
            status, body = await _post(
                client, "/api/browser/live/ack", {"session_key": "chat-1", "seq": bad}
            )
            assert status == 400 and body["code"] == "invalid_seq"


async def test_takeover_claim_event_release_round_trip() -> None:
    app, wire = _app()
    stream, relay = await _live()
    hub = lh.get_live_hub()
    async with TestClient(TestServer(app)) as client:
        # An event before the claim is refused: the lock is not held.
        status, body = await _post(
            client,
            "/api/browser/input/event",
            {"session_key": "chat-1", "event": {"type": "mouse", "x": 1, "y": 1}},
        )
        assert status == 409 and body["code"] == "not_in_control"
        status, body = await _post(client, "/api/browser/input/claim", {"session_key": "chat-1"})
        assert status == 200 and body["in_control"] is True
        assert hub.in_control("chat-1") is True
        assert wire.events[-1][1]["state"] == "in_control"
        status, body = await _post(
            client,
            "/api/browser/input/event",
            {
                "session_key": "chat-1",
                "event": {"type": "key", "key": "a", "code": "KeyA", "text": "a"},
            },
        )
        assert status == 200 and body["sent"] == 2 and stream.inputs == 1
        assert [m for m, _, _ in relay.calls] == ["Input.dispatchKeyEvent"] * 2
        status, body = await _post(
            client,
            "/api/browser/input/event",
            {"session_key": "chat-1", "event": {"type": "mouse", "x": -5, "y": 1}},
        )
        assert status == 400 and body["code"] == "invalid_input_event"
        status, body = await _post(
            client, "/api/browser/input/event", {"session_key": "chat-1", "event": "click"}
        )
        assert status == 400 and body["code"] == "invalid_input_event"
        status, body = await _post(client, "/api/browser/input/release", {"session_key": "chat-1"})
        assert status == 200 and body["in_control"] is False
        assert hub.in_control("chat-1") is False
        assert wire.events[-1][1]["state"] == "live"


async def test_open_in_browser_reports_whether_the_window_was_focused() -> None:
    app, _wire = _app()
    async with TestClient(TestServer(app)) as client:
        status, body = await _post(
            client, "/api/browser/live/open-in-browser", {"session_key": "chat-1"}
        )
        assert status == 200 and body["focused"] is False
        _stream, relay = await _live()
        status, body = await _post(
            client, "/api/browser/live/open-in-browser", {"session_key": "chat-1"}
        )
        assert status == 200 and body["focused"] is True
        assert relay.calls[0][0] == "Target.activateTarget"


async def test_the_slice_binds_the_hub_to_the_owner_senders_and_registers_cleanup() -> None:
    app, wire = _app()
    hub = lh.get_live_hub()
    assert hub._publish == wire.publish and hub._send_frame == wire.send_frame
    assert route_slice._close_live_hub in app.on_cleanup
    paths = {r.resource.canonical for r in app.router.routes()}
    assert paths == set(ROUTES)
    assert all(r.method == "POST" for r in app.router.routes())


async def test_the_slice_is_in_the_route_table_after_system() -> None:
    names = routes_pkg.REGISTRAR_NAMES
    assert names.index("browser_live") == names.index("system") + 1


async def test_the_frame_sender_is_the_binary_owner_delivery() -> None:
    app, wire = _app()
    stream, _relay = await _live()
    await stream.push(b"\x00\x00\x00\x02{}\xff\xd8")
    assert wire.frames == [b"\x00\x00\x00\x02{}\xff\xd8"]
