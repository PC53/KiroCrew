"""``POST /api/browser/op``: the ``browser`` tool's forwarding leg.

A MACHINE-only strict route reached over the AF_UNIX internal-API socket, where
``request.remote`` is empty. It gates on ``request["internal_auth"] is True``
ALONE (mirroring ``api_computer_use_invoke``); an added loopback re-assert would
403 every unix-socket op. Pinned here: the auth gate, body validation, the
``not_installed`` answer, the result pass-through, and how a timeout or a raised
helper is turned into a code the shim can render. And the live card's glue: the
first op of a turn opens the hub's turn, every op is a step, and after an op the
stream follows the daemon's current tab.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.browser_harness import live_hub as hub_mod
from kiro_crew.browser_harness import ops as harness_ops
from kiro_crew.dashboard.handlers import browser_ops as mod


@pytest.fixture(autouse=True)
def _quiet_sel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "_sel", lambda: MagicMock())


@pytest.fixture(autouse=True)
def _fresh_hub() -> Any:
    hub_mod.reset_live_hub()
    yield
    hub_mod.reset_live_hub()


def _request(body: dict[str, Any] | None, *, internal: bool = True) -> Any:
    req = make_mocked_request("POST", "/api/browser/op")
    if internal:
        req["internal_auth"] = True
    if body is not None:
        req._read_bytes = json.dumps(body).encode()
    return req


class _Supervisor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.relay = MagicMock()
        self.relay.agent_targets.return_value = ["T1"]
        self.tab = {"targetId": "T1", "url": "https://u", "title": "t"}

    def status(self) -> dict[str, Any]:
        return {"browser": {"kind": "kiro_chromium", "headless": True}, "connection": "connected"}

    async def call(self, helper: str, **kwargs: Any) -> Any:
        self.calls.append((helper, kwargs))
        if helper == "js":
            return {"url": "u", "title": "t", "nodes": []}
        if helper == "current_tab":
            return self.tab
        return {}


def _install(monkeypatch: pytest.MonkeyPatch, supervisor: Any) -> None:
    async def _sup(hub: Any, key: str) -> Any:
        return supervisor

    monkeypatch.setattr(mod, "_supervisor", _sup)


def _no_stream(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record ``start_stream`` calls instead of attaching a screencast to a MagicMock relay."""
    seen: list[tuple[str, str]] = []

    async def _start(self: Any, key: str, relay: Any, target: str, **kw: Any) -> None:
        seen.append((key, target))
        sess = self._session(key)
        sess.url, sess.title = kw.get("url", ""), kw.get("title", "")

    monkeypatch.setattr(hub_mod.LiveHub, "start_stream", _start)
    return seen


@pytest.mark.asyncio
async def test_missing_internal_auth_is_rejected() -> None:
    resp = await mod.api_browser_op(
        _request({"op": "snapshot", "session_key": "k"}, internal=False)
    )
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "loopback_only"


@pytest.mark.asyncio
async def test_non_loopback_remote_passes_when_internal_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # make_mocked_request's remote is not loopback; only internal_auth decides.
    _install(monkeypatch, _Supervisor())
    _no_stream(monkeypatch)
    resp = await mod.api_browser_op(_request({"op": "snapshot", "session_key": "k"}))
    assert resp.status == 200
    assert json.loads(resp.body)["ok"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body, code",
    [
        (None, "invalid_json"),
        ({"op": "evaluate", "session_key": "k"}, "bad_op"),
        ({"op": "snapshot", "session_key": "k", "args": []}, "args_must_be_object"),
        ({"op": "snapshot"}, "session_key_required"),
    ],
)
async def test_body_validation(body: dict[str, Any] | None, code: str) -> None:
    req = _request(body)
    if body is None:
        req._read_bytes = b"not json"
    resp = await mod.api_browser_op(req)
    assert resp.status == 400
    assert json.loads(resp.body)["code"] == code


@pytest.mark.asyncio
async def test_not_installed_is_a_refusal_the_shim_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, None)
    resp = await mod.api_browser_op(_request({"op": "snapshot", "session_key": "k"}))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["ok"] is False and body["code"] == "not_installed"
    assert "Settings -> Browser" in body["error"]


@pytest.mark.asyncio
async def test_result_passes_through_and_the_context_is_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sup = _Supervisor()
    _install(monkeypatch, sup)
    streams = _no_stream(monkeypatch)
    resp = await mod.api_browser_op(_request({"op": "snapshot", "session_key": "dashboard:chat-1"}))
    body = json.loads(resp.body)
    assert body == {"ok": True, "result": {"ok": True, "url": "u", "title": "t", "snapshot": ""}}
    assert sup.calls[0][0] == "js"
    # The live card's glue: the hub session is keyed by the SLOT key (the
    # ``dashboard:`` transport prefix folded off), the op is a step on an open
    # turn, and the stream now follows the daemon's current tab.
    sess = hub_mod.get_live_hub().get("chat-1")
    assert sess is not None and sess.turn_id and sess.state != hub_mod.STATE_DONE
    assert [st.label for st in sess.steps] == ["Read the page"]
    assert streams == [("chat-1", "T1")]
    assert sess.url == "https://u" and sess.title == "t"
    assert sup.calls[-1][0] == "current_tab"


@pytest.mark.asyncio
async def test_a_refusal_from_the_op_is_returned_flat(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _Supervisor())
    _no_stream(monkeypatch)
    resp = await mod.api_browser_op(
        _request({"op": "click", "session_key": "k", "args": {"ref": "nope"}})
    )
    body = json.loads(resp.body)
    assert body["ok"] is False and body["code"] == "bad_ref"


@pytest.mark.asyncio
async def test_a_raised_helper_becomes_browser_error_with_one_bounded_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Boom(_Supervisor):
        async def call(self, helper: str, **kwargs: Any) -> Any:
            raise RuntimeError("daemon gone\nTraceback (most recent call last) ..." + "x" * 500)

    _install(monkeypatch, _Boom())
    resp = await mod.api_browser_op(_request({"op": "snapshot", "session_key": "k"}))
    body = json.loads(resp.body)
    assert body["ok"] is False and body["code"] == "browser_error"
    assert body["error"] == "daemon gone"


@pytest.mark.asyncio
async def test_a_supervisor_that_fails_to_start_is_browser_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _sup(hub: Any, key: str) -> Any:
        raise OSError("chrome refused the connection")

    monkeypatch.setattr(mod, "_supervisor", _sup)
    resp = await mod.api_browser_op(_request({"op": "snapshot", "session_key": "k"}))
    body = json.loads(resp.body)
    assert body == {"ok": False, "code": "browser_error", "error": "chrome refused the connection"}


@pytest.mark.asyncio
async def test_an_op_past_its_budget_is_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _Supervisor())

    async def _hang(op: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        await asyncio.sleep(3600)  # flake-ok: cancelled by the wait_for budget below
        return {"ok": True}

    monkeypatch.setattr(harness_ops, "run_op", _hang)
    monkeypatch.setattr(mod, "OP_TIMEOUT_S", 0.01)
    resp = await mod.api_browser_op(_request({"op": "snapshot", "session_key": "k"}))
    body = json.loads(resp.body)
    assert body["ok"] is False and body["code"] == "timeout"


def test_build_context_binds_the_live_hub_and_relay() -> None:
    hub = hub_mod.LiveHub()
    hub.turn_started("held", "t1")
    asyncio.run(hub.claim("held"))
    sup = _Supervisor()
    ctx = mod.build_context("held", sup, hub)
    assert ctx.in_control() is True
    ctx.step("Open x")
    assert [st.label for st in hub.get("held").steps] == ["Open x"]
    assert ctx.agent_targets() == ["T1"]
    assert ctx.helper == sup.call
    assert mod.build_context("free", sup, hub).in_control() is False


def test_live_session_key_is_the_slot_key_the_card_uses() -> None:
    assert mod.live_session_key("dashboard:chat-7") == "chat-7"
    assert mod.live_session_key("chat-7") == "chat-7"


def test_the_first_op_opens_a_turn_and_later_ops_join_it() -> None:
    hub = hub_mod.LiveHub()
    mod._open_turn(hub, "k")
    first = hub.get("k").turn_id
    assert first
    mod._open_turn(hub, "k")
    assert hub.get("k").turn_id == first, "the same turn until the chat runner ends it"
    hub.turn_ended("k")
    mod._open_turn(hub, "k")
    assert hub.get("k").turn_id != first, "a new turn after turn_ended"


@pytest.mark.asyncio
async def test_follow_current_tab_restarts_only_when_the_target_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = hub_mod.LiveHub()
    sup = _Supervisor()
    streams = _no_stream(monkeypatch)
    await mod.follow_current_tab(hub, "k", sup)
    assert streams == [("k", "T1")]
    # Same target again: only the address pill moves (page_changed), no restart.
    hub.get("k").stream = MagicMock(target_id="T1")
    sup.tab = {"targetId": "T1", "url": "https://u/2", "title": "t2"}
    await mod.follow_current_tab(hub, "k", sup)
    assert streams == [("k", "T1")]
    assert hub.get("k").url == "https://u/2"
    # The agent switched tabs: the stream is restarted on the new target.
    sup.tab = {"targetId": "T2", "url": "https://v", "title": "v"}
    await mod.follow_current_tab(hub, "k", sup)
    assert streams == [("k", "T1"), ("k", "T2")]


@pytest.mark.asyncio
async def test_follow_current_tab_swallows_a_failing_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Boom(_Supervisor):
        async def call(self, helper: str, **kwargs: Any) -> Any:
            raise RuntimeError("daemon gone")

    hub = hub_mod.LiveHub()
    streams = _no_stream(monkeypatch)
    await mod.follow_current_tab(hub, "k", _Boom())
    assert streams == []


@pytest.mark.asyncio
async def test_no_tab_follow_while_the_person_holds_the_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sup = _Supervisor()
    _install(monkeypatch, sup)
    streams = _no_stream(monkeypatch)
    hub = hub_mod.get_live_hub()
    hub.turn_started("k", "t1")
    await hub.claim("k")
    resp = await mod.api_browser_op(_request({"op": "snapshot", "session_key": "k"}))
    body = json.loads(resp.body)
    assert body["ok"] is False and body["code"] == "user_in_control"
    assert streams == [] and sup.calls == []


@pytest.mark.asyncio
async def test_waiting_allow_reaches_the_card_while_ensure_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_supervisor`` tells the hub ``waiting_allow`` as soon as the supervisor says so."""
    import types

    class _Sup:
        def __init__(self) -> None:
            self.polls = 0
            self.done = asyncio.Event()

        def ensure_in_background(self) -> Any:
            async def _wait() -> Any:
                await self.done.wait()
                return self

            return asyncio.ensure_future(_wait())

        def status(self) -> dict[str, Any]:
            self.polls += 1
            if self.polls >= 2:
                self.done.set()
            return {
                "connection": "waiting_allow",
                "browser": {"kind": "user_chrome", "headless": False},
                "detail": "click Allow",
            }

    sup = _Sup()
    fake_mod = types.SimpleNamespace(WAITING_ALLOW="waiting_allow", get_supervisor=lambda home: sup)
    monkeypatch.setattr(harness_ops, "browsing_available", lambda home: True)
    monkeypatch.setattr(mod.importlib, "import_module", lambda name: fake_mod)
    monkeypatch.setattr(mod, "_ENSURE_POLL_S", 0.001)
    hub = hub_mod.LiveHub()
    got = await mod._supervisor(hub, "k")
    assert got is sup
    sess = hub.get("k")
    assert sess is not None and sess.state == hub_mod.STATE_WAITING_ALLOW
    assert sess.browser == "user_chrome" and sess.headless is False
