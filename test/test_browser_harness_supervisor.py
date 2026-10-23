"""The supervisor (``browser_harness/supervisor.py``) without a browser: status shape,
the helper allowlist, the IPC leaves, the DevTools port probe and the worker protocol.
"""

from __future__ import annotations

import socket
import sys
import types
from pathlib import Path

import pytest

from kiro_crew import sandbox_leaves, sandbox_precreate
from kiro_crew.browser_harness import HARNESS_VERSION, helper_worker, supervisor
from kiro_crew.browser_harness.supervisor import HarnessSupervisor
from kiro_crew.security import paths_lists


def test_the_hidden_leaves_are_spelled_the_same_in_every_fence() -> None:
    for leaf in (supervisor.IPC_LEAF, supervisor.PROFILE_LEAF):
        assert leaf in sandbox_leaves._CREW_HIDDEN_LEAVES, leaf
        assert leaf in sandbox_precreate._CREW_PRECREATE_HIDDEN_DIR_LEAVES, leaf
        assert leaf in paths_lists._CREW_SECRET_LEAVES, leaf
    assert supervisor.IPC_LEAF == "browser-harness-ipc"
    assert supervisor.PROFILE_LEAF == "browser-profile"


def test_the_supervisor_allowlist_matches_the_worker_allowlist() -> None:
    worker = {name: accepted for name, (_fn, accepted) in helper_worker._ALLOWLIST.items()}
    assert dict(supervisor.HELPERS) == worker
    for forbidden in ("http_get", "browser_js", "browser_cdp", "run", "upload_file", "fill_input"):
        assert forbidden not in supervisor.HELPERS


def test_status_on_an_empty_home_has_the_contract_shape(tmp_path: Path) -> None:
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {"choice": "headless"})
    status = sup.status()
    assert set(status) >= {"installed", "version", "browser", "connection", "detail"}
    assert status["installed"] is False and status["version"] is None
    assert status["wanted_version"] == HARNESS_VERSION
    assert status["browser"] == {"kind": "kiro_chromium", "headless": True}
    assert status["connection"] == supervisor.DISCONNECTED
    assert status["agent_tabs"] == 0
    assert sup.relay is None and sup.connected is False


@pytest.mark.asyncio
async def test_ensure_refuses_before_install_and_says_so_in_status(tmp_path: Path) -> None:
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {})
    with pytest.raises(supervisor.HarnessNotInstalled):
        await sup.ensure()
    assert sup.status()["connection"] == supervisor.DISCONNECTED
    assert "not installed" in sup.status()["detail"]


@pytest.mark.asyncio
async def test_call_refuses_an_unlisted_helper_or_argument_before_connecting(
    tmp_path: Path,
) -> None:
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {})
    with pytest.raises(supervisor.HelperError):
        await sup.call("http_get", url="https://example.invalid")
    with pytest.raises(supervisor.HelperError):
        await sup.call("goto", url="https://example.invalid", referrer="x")
    with pytest.raises(supervisor.HarnessNotInstalled):
        await sup.call("goto", url="https://example.invalid")


@pytest.mark.asyncio
async def test_test_reports_the_state_reached_within_its_wait(tmp_path: Path) -> None:
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {})
    report = await sup.test(wait_secs=1.0)
    assert report["connection"] == supervisor.DISCONNECTED
    assert report["installed"] is False


@pytest.mark.asyncio
async def test_stop_on_a_fresh_supervisor_is_a_no_op(tmp_path: Path) -> None:
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {})
    await sup.stop()
    assert sup.status()["connection"] == supervisor.DISCONNECTED


def test_the_runtime_dir_moves_to_temp_when_the_socket_path_would_be_too_long(
    tmp_path: Path,
) -> None:
    short = HarnessSupervisor(Path("/tmp/k"))
    assert short.runtime_dir() == Path("/tmp/k") / supervisor.IPC_LEAF / "run"
    deep = HarnessSupervisor(tmp_path / ("x" * 120))
    assert deep.runtime_dir().name.startswith("kirocrew-bh-")
    assert len(str(deep.runtime_dir() / "bu.sock").encode()) <= supervisor._MAX_SOCKET_PATH + 40


def test_the_devtools_port_file_is_trusted_only_when_something_listens(tmp_path: Path) -> None:
    assert supervisor._devtools_ws_url(tmp_path) is None
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        (tmp_path / supervisor.DEVTOOLS_PORT_FILE).write_text(
            f"{port}\n/devtools/browser/abc\n", encoding="utf-8"
        )
        assert (
            supervisor._devtools_ws_url(tmp_path) == f"ws://127.0.0.1:{port}/devtools/browser/abc"
        )
        (tmp_path / supervisor.DEVTOOLS_PORT_FILE).write_text(
            f"{port}\nnot-a-path\n", encoding="utf-8"
        )
        assert supervisor._devtools_ws_url(tmp_path) is None
    finally:
        listener.close()
    (tmp_path / supervisor.DEVTOOLS_PORT_FILE).write_text(
        f"{port}\n/devtools/browser/abc\n", encoding="utf-8"
    )
    assert supervisor._devtools_ws_url(tmp_path) is None


def test_get_supervisor_is_one_per_home(tmp_path: Path) -> None:
    supervisor._reset_supervisors_for_tests()
    try:
        a = supervisor.get_supervisor(tmp_path)
        assert supervisor.get_supervisor(tmp_path) is a
        assert supervisor.get_supervisor(tmp_path / "other") is not a
    finally:
        supervisor._reset_supervisors_for_tests()


def test_the_daemon_env_pins_name_and_hidden_roots(tmp_path: Path) -> None:
    sup = HarnessSupervisor(tmp_path, base_env={"PATH": "/usr/bin", "BU_CDP_WS": "ws://evil"})
    env = sup._daemon_env("http://127.0.0.1:9999")
    assert env["BU_NAME"] == supervisor.DAEMON_NAME != "default"
    assert env["BU_CDP_URL"] == "http://127.0.0.1:9999"
    assert "BU_CDP_WS" not in env
    assert Path(env["BH_HOME"]) == tmp_path / supervisor.IPC_LEAF
    assert Path(env["BH_RUNTIME_DIR"]) == sup.runtime_dir()
    assert Path(env["BH_AGENT_WORKSPACE"]).is_dir()
    assert env["BH_TELEMETRY"] == "false"


# ── the worker's protocol, with a fake helpers module ──


def _fake_helpers() -> types.ModuleType:
    mod = types.ModuleType("helpers")
    mod.goto_url = lambda url: {"frameId": "f", "url": url}  # type: ignore[attr-defined]
    mod.page_info = lambda: {"url": "about:blank"}  # type: ignore[attr-defined]

    def boom(**_kw):
        raise RuntimeError("Session with given id not found")

    mod.click_at_xy = boom  # type: ignore[attr-defined]
    mod.http_get = lambda url: "never"  # type: ignore[attr-defined]
    return mod


def test_the_worker_runs_only_allowlisted_helpers_with_allowlisted_arguments() -> None:
    helpers = _fake_helpers()
    ok = helper_worker._handle(helpers, {"id": 1, "helper": "goto", "kwargs": {"url": "https://a"}})
    assert ok == {"id": 1, "ok": True, "result": {"frameId": "f", "url": "https://a"}}
    assert helper_worker._handle(helpers, {"id": 2, "ping": True})["result"] == {"pong": True}
    denied = helper_worker._handle(helpers, {"id": 3, "helper": "http_get", "kwargs": {"url": "x"}})
    assert denied["ok"] is False and "not allowed" in denied["error"]
    extra = helper_worker._handle(
        helpers, {"id": 4, "helper": "goto", "kwargs": {"url": "x", "evil": 1}}
    )
    assert extra["ok"] is False and "unexpected arguments" in extra["error"]
    failed = helper_worker._handle(
        helpers, {"id": 5, "helper": "click", "kwargs": {"x": 1, "y": 2}}
    )
    assert failed["ok"] is False and "Session with given id" in failed["error"]
    assert "Traceback" not in failed["error"]


@pytest.mark.asyncio
async def test_the_worker_script_reports_a_missing_harness_instead_of_hanging() -> None:
    """Run the real script under the gateway's interpreter, which has no ``browser_harness``."""
    import asyncio
    import json

    script = Path(helper_worker.__file__)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(script),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        reply = await supervisor._worker_request(proc, {"id": 0, "ping": True}, 30.0)
    finally:
        await supervisor._stop_process(proc)
    assert reply["ok"] is False
    assert "import failed" in reply["error"]
    assert json.dumps(reply)
