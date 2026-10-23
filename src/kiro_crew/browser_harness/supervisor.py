"""The gateway-owned lifecycle of browser, relay and Harness daemon.

One :class:`HarnessSupervisor` per gateway (:func:`get_supervisor`). :meth:`ensure`
brings the three up in order -- pick the browser (:mod:`.discover`), reach its CDP
endpoint (the user's Chrome through its ``DevToolsActivePort`` file; Kiro's Chromium by
launching it), start the relay (:mod:`.relay`) and connect it, then start Harness's
daemon in the fenced environment (:mod:`.env`) pointed at the relay -- and
:meth:`stop` takes them down in reverse. :meth:`status` is what Settings and the
live card read, and :meth:`call` is the ONE way a helper reaches the daemon.

Helpers run in a worker process the venv's interpreter executes
(:mod:`.helper_worker`), because Harness's helpers are synchronous functions that
talk to the daemon over its IPC: in the gateway's own process they would block the
loop, and importing them there would mean importing an unpinned copy. The worker is
given an allowlist and so is this module (:data:`HELPERS`); a name outside it is
refused before anything is sent.

Why there is no deadline on connecting to the user's Chrome: Chrome 144+ shows an
"Allow remote debugging?" sheet per connection, and the connection that raised the
sheet is the only thing keeping it on screen. A timeout would drop the sheet and the
retry would raise another. ``waiting_allow`` therefore lasts until the person answers,
Chrome closes, or the gateway stops. Approval took 215 s in one measured run.

Everything the daemon writes -- its socket, pid file, log, screenshots -- lives in
``<home>/browser-harness-ipc``, a ``HIDDEN`` crew-home leaf, and Kiro's Chromium
profile in ``<home>/browser-profile``, another. The sandbox never sees the socket
path, the Windows IPC token, or a cookie a takeover login left behind.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.browser_harness import HARNESS_VERSION, discover, install
from kiro_crew.browser_harness.env import daemon_env
from kiro_crew.browser_harness.relay import CdpRelay, RelayClosed
from kiro_crew.loop_lock import LoopBoundLock

logger = logging.getLogger(__name__)

#: Crew-home leaves this module owns. Spelled identically in ``sandbox_leaves`` and
#: ``security.paths_lists``; ``test_browser_harness_supervisor`` pins them equal.
IPC_LEAF = "browser-harness-ipc"
PROFILE_LEAF = "browser-profile"

#: Connection states ``status()`` reports.
CONNECTED = "connected"
WAITING_ALLOW = "waiting_allow"
DISCONNECTED = "disconnected"
ERROR = "error"

#: The helpers :meth:`HarnessSupervisor.call` will forward, with the keyword
#: arguments each accepts. Mirrors ``helper_worker._ALLOWLIST``; ``js`` exists only
#: for the gateway's own accessibility walker (``browser_harness/snapshot_walker.js``).
HELPERS: Mapping[str, tuple[str, ...]] = {
    "goto": ("url",),
    "click": ("x", "y", "button", "clicks"),
    "mouse_move": ("x", "y"),
    "drag": ("x", "y", "to_x", "to_y"),
    "type_text": ("text",),
    "press_key": ("key", "modifiers"),
    "scroll": ("x", "y", "dy", "dx"),
    "screenshot": ("path", "full", "max_dim"),
    "page_info": (),
    "wait_for_load": ("timeout",),
    "new_tab": ("url",),
    "switch_tab": ("target", "activate"),
    "list_tabs": ("include_chrome",),
    "close_tab": ("target",),
    "current_tab": (),
    "js": ("expression", "target_id"),
}

#: The daemon's instance name. Not ``default``: a named daemon creates its own tab
#: instead of attaching to the first page it finds, which on the user's Chrome would
#: be one of THEIR tabs.
DAEMON_NAME = "kirocrew"

#: Harness's runtime file stems with ``BH_RUNTIME_DIR`` set (``_ipc._runtime_stem``).
_SOCK_FILE = "bu.sock"
_PORT_FILE = "bu.port"

#: AF_UNIX ``sun_path`` is 104 bytes on macOS; a data home deeper than this moves the
#: runtime directory to the system temp dir.
_MAX_SOCKET_PATH = 100

#: Budgets. The daemon needs only a loopback websocket to the relay, so its start is
#: fast; the worker imports Pillow and friends once.
DAEMON_START_TIMEOUT_SECS = 60.0
WORKER_START_TIMEOUT_SECS = 60.0
#: A helper round trip. Screenshots and ``wait_for_load`` are the long ones.
CALL_TIMEOUT_SECS = 90.0
#: Kiro's Chromium writing ``DevToolsActivePort`` after launch.
CHROMIUM_START_TIMEOUT_SECS = 30.0
_POLL_SECS = 0.1
_IPC_TIMEOUT_SECS = 3.0
_STOP_GRACE_SECS = 5.0

#: Chromium flags. ``--remote-debugging-port=0`` picks a free port and writes it to
#: ``DevToolsActivePort``; the window size is the screencast's native frame.
_CHROMIUM_ARGS: tuple[str, ...] = (
    "--remote-debugging-port=0",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-sync",
    "--disable-background-networking",
    "--window-size=1280,800",
)
_HEADLESS_ARG = "--headless=new"
#: Only as root: Chromium refuses to start sandboxed as uid 0 (a container), and a
#: sandbox-less Chromium for anyone else would be a widening for no reason.
_NO_SANDBOX_ARG = "--no-sandbox"
_ROOT_UID = 0

#: Chrome writes this file in the user-data directory when its debugging port is on.
DEVTOOLS_PORT_FILE = "DevToolsActivePort"
_LOOPBACK = "127.0.0.1"


class HarnessNotInstalled(RuntimeError):
    """``ensure()`` was called before Settings -> Browser installed the engine."""


class BrowserUnavailable(RuntimeError):
    """No browser can be reached: the detail says what the person has to do."""


class HelperError(RuntimeError):
    """The helper raised in the worker; the message is Harness's own."""


@dataclass
class HarnessHandle:
    """What ``ensure()`` hands back: the live pieces, for the stream and tool lanes."""

    choice: discover.BrowserChoice
    relay: CdpRelay
    daemon: asyncio.subprocess.Process | None
    worker: asyncio.subprocess.Process | None
    browser: asyncio.subprocess.Process | None
    started_at: float = field(default_factory=time.monotonic)


class HarnessSupervisor:
    """Browser, relay and daemon for one gateway. See the module docstring."""

    def __init__(
        self,
        home: Path,
        *,
        config_reader: Callable[[], Mapping[str, Any]] | None = None,
        base_env: Mapping[str, str] | None = None,
    ) -> None:
        self.home = Path(home)
        self._config_reader = config_reader or _default_config_reader
        self._base_env = base_env
        self._handle: HarnessHandle | None = None
        self._connection = DISCONNECTED
        self._detail = ""
        self._choice: discover.BrowserChoice | None = None
        self._ensure_lock = LoopBoundLock()
        self._call_lock = LoopBoundLock()
        self._ensure_task: asyncio.Task[HarnessHandle] | None = None

    # ── paths ──

    @property
    def ipc_dir(self) -> Path:
        return self.home / IPC_LEAF

    @property
    def profile_dir(self) -> Path:
        return self.home / PROFILE_LEAF

    def runtime_dir(self) -> Path:
        """Where the daemon's socket lives: the hidden IPC leaf, or temp when too deep."""
        preferred = self.ipc_dir / "run"
        if len(str(preferred / _SOCK_FILE).encode()) <= _MAX_SOCKET_PATH:
            return preferred
        digest = hashlib.sha256(str(self.home).encode()).hexdigest()[:12]
        return Path(tempfile.gettempdir()) / f"kirocrew-bh-{digest}"

    @property
    def relay(self) -> CdpRelay | None:
        return self._handle.relay if self._handle is not None else None

    # ── status ──

    def status(self) -> dict[str, Any]:
        """The Settings/live-card shape: install, browser, connection, detail."""
        state = install.install_state(self.home)
        choice = self._choice
        if choice is None:
            choice = self._choose(state)
        return {
            "installed": state.installed,
            "version": state.version if state.installed else None,
            "wanted_version": HARNESS_VERSION,
            "browser": choice.as_dict() if choice is not None else None,
            "connection": self._connection,
            "detail": self._detail,
            "agent_tabs": len(self.relay.agent_targets()) if self.relay else 0,
            "chromium": state.chromium.as_dict(),
        }

    @property
    def connected(self) -> bool:
        handle = self._handle
        return handle is not None and not handle.relay.closed and _alive(handle.daemon)

    # ── lifecycle ──

    async def ensure(self) -> HarnessHandle:
        """Bring browser, relay and daemon up (or return the live handle).

        Serialised: a second caller waits for the first instead of launching a
        second browser. Raises :class:`HarnessNotInstalled` or
        :class:`BrowserUnavailable`; waits without deadline on the Allow sheet.
        """
        async with self._ensure_lock:
            if self.connected and self._handle is not None:
                return self._handle
            await self._teardown()
            return await self._start()

    def ensure_in_background(self) -> asyncio.Task[HarnessHandle]:
        """Start ``ensure()`` as a task the caller does not wait for (the HTTP routes)."""
        task = self._ensure_task
        if task is not None and not task.done():
            return task
        task = asyncio.create_task(self.ensure(), name="browser-harness-ensure")
        self._ensure_task = task
        task.add_done_callback(_log_ensure_outcome)
        return task

    async def stop(self) -> None:
        """Close the agent's tabs, stop daemon, worker, relay and Kiro's Chromium."""
        task = self._ensure_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - stopping anyway
                pass
        async with self._ensure_lock:
            await self._teardown()
        self._set(DISCONNECTED, "")

    async def test(self, wait_secs: float) -> dict[str, Any]:
        """``ensure()`` with a bounded wait, then the status; the Allow sheet outlasts it."""
        task = self.ensure_in_background()
        try:
            await asyncio.wait_for(asyncio.shield(task), wait_secs)
        except asyncio.TimeoutError:
            pass
        except Exception:  # noqa: BLE001 - the failure is in status()["detail"]
            pass
        report = self.status()
        if self.connected:
            try:
                tabs = await self.call("list_tabs", include_chrome=False)
                report["tabs"] = len(tabs) if isinstance(tabs, list) else None
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                report["tabs"] = None
                report["detail"] = str(exc)
        return report

    # ── helpers ──

    async def call(self, helper: str, **kwargs: Any) -> Any:
        """Run ONE allowlisted Harness helper through the worker; return its result."""
        accepted = HELPERS.get(helper)
        if accepted is None:
            raise HelperError(f"helper not allowed: {helper}")
        unknown = sorted(set(kwargs) - set(accepted))
        if unknown:
            raise HelperError(f"{helper}: unexpected arguments {unknown}")
        handle = await self.ensure()
        async with self._call_lock:
            if not _alive(handle.worker):
                handle.worker = await self._start_worker(handle)
            assert handle.worker is not None
            reply = await _worker_request(
                handle.worker, {"id": 1, "helper": helper, "kwargs": kwargs}, CALL_TIMEOUT_SECS
            )
        if not reply.get("ok"):
            raise HelperError(str(reply.get("error") or "helper failed"))
        return reply.get("result")

    # ── internals ──

    def _set(self, connection: str, detail: str) -> None:
        self._connection = connection
        self._detail = detail

    def _choose(self, state: install.InstallState) -> discover.BrowserChoice | None:
        try:
            cfg = self._config_reader()
        except Exception:  # noqa: BLE001 - a config read failure means defaults
            cfg = {}
        try:
            return discover.choose_browser(
                cfg,
                kiro_chromium=state.chromium.executable,
                kiro_profile=self.profile_dir,
            )
        except Exception:  # noqa: BLE001 - a probe failure is "unknown", not a crash
            logger.debug("browser discovery failed", exc_info=True)
            return None

    async def _start(self) -> HarnessHandle:
        state = install.install_state(self.home)
        if not state.installed or state.python is None:
            self._set(DISCONNECTED, "Browser Harness is not installed")
            raise HarnessNotInstalled(self._detail)
        choice = self._choose(state)
        if choice is None:
            self._set(ERROR, "could not pick a browser")
            raise BrowserUnavailable(self._detail)
        self._choice = choice
        browser_proc: asyncio.subprocess.Process | None = None
        relay = CdpRelay()
        try:
            if choice.kind == "kiro_chromium":
                browser_proc, ws_url = await self._launch_chromium(choice, state)
                self._set(DISCONNECTED, "connecting to Kiro's Chromium")
            else:
                ws_url = await self._user_browser_ws_url(choice)
                self._set(WAITING_ALLOW, "click Allow in the browser's remote debugging prompt")
            relay_url = await relay.start(ws_url)
            relay.on_upstream_closed = self._on_upstream_closed
            daemon = await self._start_daemon(state.python, relay_url)
            handle = HarnessHandle(choice, relay, daemon, None, browser_proc)
            handle.worker = await self._start_worker(handle)
        except asyncio.CancelledError:
            await relay.close()
            await _stop_process(browser_proc)
            self._set(DISCONNECTED, "")
            raise
        except Exception as exc:
            await relay.close()
            await _stop_process(browser_proc)
            self._set(ERROR, str(exc))
            raise
        self._handle = handle
        self._set(CONNECTED, "")
        return handle

    async def _teardown(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            await self._shutdown_stray_daemon()
            return
        if not handle.relay.closed:
            try:
                await handle.relay.close_agent_targets()
            except (RelayClosed, Exception):  # noqa: BLE001 - best effort at teardown
                pass
        await self._ipc_shutdown()
        await _stop_process(handle.worker)
        await _stop_process(handle.daemon)
        await handle.relay.close()
        await _stop_process(handle.browser)

    async def _on_upstream_closed(self) -> None:
        if self._connection == CONNECTED:
            self._set(DISCONNECTED, "the browser closed the connection")

    async def _user_browser_ws_url(self, choice: discover.BrowserChoice) -> str:
        profile = choice.user_data_dir
        name = "Chrome" if choice.kind == "user_chrome" else "Edge"
        if profile is None:
            raise BrowserUnavailable(f"{name} has no profile directory on this machine")
        url = _devtools_ws_url(profile)
        if url is None:
            self._set(DISCONNECTED, f"turn on {name}'s remote debugging")
            raise BrowserUnavailable(
                f"{name} is not accepting debugging connections: open "
                "chrome://inspect/#remote-debugging and turn on 'Allow remote debugging', "
                "then try again"
            )
        return url

    async def _launch_chromium(
        self, choice: discover.BrowserChoice, state: install.InstallState
    ) -> tuple[asyncio.subprocess.Process, str]:
        exe = state.chromium.executable
        if exe is None:
            raise BrowserUnavailable("Kiro's Chromium is not downloaded: install it in Settings")
        profile = self.profile_dir
        platform_compat.make_owner_only_dir(profile)
        port_file = profile / DEVTOOLS_PORT_FILE
        port_file.unlink(missing_ok=True)
        argv = [str(exe), *_CHROMIUM_ARGS, f"--user-data-dir={profile}"]
        if choice.headless:
            argv.append(_HEADLESS_ARG)
        if platform_compat.effective_uid() == _ROOT_UID:
            argv.append(_NO_SANDBOX_ARG)
        proc = await self._spawn(argv, env=dict(self._env()), pipes=False)
        deadline = time.monotonic() + CHROMIUM_START_TIMEOUT_SECS
        while time.monotonic() < deadline:
            url = _devtools_ws_url(profile)
            if url is not None:
                return proc, url
            if proc.returncode is not None:
                break
            await asyncio.sleep(_POLL_SECS)
        await _stop_process(proc)
        raise BrowserUnavailable("Kiro's Chromium did not open its debugging port")

    def _env(self) -> Mapping[str, str]:
        return self._base_env if self._base_env is not None else os.environ

    def _daemon_env(self, relay_url: str) -> dict[str, str]:
        ipc = self.ipc_dir
        runtime = self.runtime_dir()
        for path in (ipc, ipc / "tmp", ipc / "workspace", runtime):
            platform_compat.make_owner_only_dir(path)
        env = daemon_env(self._env(), relay_url)
        env.update(
            {
                "BU_NAME": DAEMON_NAME,
                "BH_HOME": str(ipc),
                "BH_CONFIG_DIR": str(ipc),
                "BH_RUNTIME_DIR": str(runtime),
                "BH_TMP_DIR": str(ipc / "tmp"),
                "BH_AGENT_WORKSPACE": str(ipc / "workspace"),
                "PYTHONIOENCODING": "utf-8",
            }
        )
        return env

    async def _start_daemon(self, python: Path, relay_url: str) -> asyncio.subprocess.Process:
        await self._shutdown_stray_daemon()
        env = self._daemon_env(relay_url)
        proc = await self._spawn(
            [str(python), "-m", "browser_harness.daemon"], env=env, pipes=False
        )
        deadline = time.monotonic() + DAEMON_START_TIMEOUT_SECS
        while time.monotonic() < deadline:
            if await self._ipc_ping():
                return proc
            if proc.returncode is not None:
                raise BrowserUnavailable(f"the Harness daemon exited with code {proc.returncode}")
            await asyncio.sleep(_POLL_SECS)
        await _stop_process(proc)
        raise BrowserUnavailable("the Harness daemon did not start in time")

    async def _start_worker(self, handle: HarnessHandle) -> asyncio.subprocess.Process:
        state = install.install_state(self.home)
        if state.python is None:
            raise HarnessNotInstalled("Browser Harness is not installed")
        env = self._daemon_env(handle.relay.url)
        worker_py = Path(__file__).with_name("helper_worker.py")
        proc = await self._spawn([str(state.python), "-I", str(worker_py)], env=env, pipes=True)
        reply = await _worker_request(proc, {"id": 0, "ping": True}, WORKER_START_TIMEOUT_SECS)
        if not reply.get("ok"):
            await _stop_process(proc)
            raise BrowserUnavailable(str(reply.get("error") or "the helper worker did not start"))
        return proc

    async def _spawn(
        self, argv: list[str], *, env: dict[str, str], pipes: bool
    ) -> asyncio.subprocess.Process:
        """The supervisor's one spawn: the venv's interpreter or Kiro's Chromium.

        Every argv token is a module constant or a path under the data home or
        the read-only harness prefix; nothing an agent says reaches it. Each child
        gets its own process group so :func:`_stop_process` can take the tree.
        """
        kwargs: dict[str, Any] = {"env": env, "stdin": subprocess.DEVNULL}
        if pipes:
            kwargs["stdin"] = subprocess.PIPE
            kwargs["stdout"] = subprocess.PIPE
            kwargs["stderr"] = subprocess.DEVNULL
        else:
            kwargs["stdout"] = subprocess.DEVNULL
            kwargs["stderr"] = subprocess.DEVNULL
        if platform_compat.IS_WINDOWS:
            kwargs["creationflags"] = (
                platform_compat.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined,unused-ignore]
            )
        else:
            kwargs["start_new_session"] = True
        return await asyncio.create_subprocess_exec(*argv, **kwargs)

    # ── Harness IPC (the daemon's own protocol, client side) ──

    async def _ipc_ping(self) -> bool:
        reply = await self._ipc_request({"meta": "ping"})
        return isinstance(reply, dict) and reply.get("pong") is True

    async def _ipc_shutdown(self) -> None:
        await self._ipc_request({"meta": "shutdown"})

    async def _shutdown_stray_daemon(self) -> None:
        """A daemon from a previous gateway life would make ours exit as 'already running'."""
        if await self._ipc_ping():
            await self._ipc_shutdown()
            deadline = time.monotonic() + _STOP_GRACE_SECS
            while time.monotonic() < deadline and await self._ipc_ping():
                await asyncio.sleep(_POLL_SECS)
        runtime = self.runtime_dir()
        for name in (_SOCK_FILE, _PORT_FILE):
            try:
                (runtime / name).unlink(missing_ok=True)
            except OSError:
                pass

    async def _ipc_request(self, body: dict[str, Any]) -> Any:
        runtime = self.runtime_dir()
        try:
            if platform_compat.IS_WINDOWS:
                port_text = await asyncio.to_thread(
                    (runtime / _PORT_FILE).read_text, encoding="utf-8"
                )
                port_info = json.loads(port_text)
                body = {**body, "token": port_info["token"]}
                opener = asyncio.open_connection(_LOOPBACK, int(port_info["port"]))
            else:
                opener = asyncio.open_unix_connection(str(runtime / _SOCK_FILE))
            reader, writer = await asyncio.wait_for(opener, _IPC_TIMEOUT_SECS)
        except (OSError, ValueError, KeyError, asyncio.TimeoutError):
            return None
        try:
            writer.write((json.dumps(body) + "\n").encode("utf-8"))
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), _IPC_TIMEOUT_SECS)
            return json.loads(line.decode("utf-8")) if line else None
        except (OSError, ValueError, asyncio.TimeoutError):
            return None
        finally:
            writer.close()


# ── module helpers ──


def _default_config_reader() -> Mapping[str, Any]:
    """The ``browser`` config section, tolerant of a build that has not shipped it."""
    from kiro_crew.config import KiroCrewConfig

    section = getattr(KiroCrewConfig.load(), "browser", None)
    if section is None:
        return {}
    if dataclasses.is_dataclass(section) and not isinstance(section, type):
        return dataclasses.asdict(section)
    if isinstance(section, Mapping):
        return dict(section)
    return {}


def _devtools_ws_url(profile: Path) -> str | None:
    """The websocket URL Chrome wrote to ``<profile>/DevToolsActivePort``, if live."""
    try:
        lines = (profile / DEVTOOLS_PORT_FILE).read_text(encoding="utf-8").splitlines()
        port = int(lines[0].strip())
        path = lines[1].strip()
    except (OSError, ValueError, IndexError):
        return None
    if not path.startswith("/"):
        return None
    try:
        socket.create_connection((_LOOPBACK, port), timeout=0.5).close()
    except OSError:
        return None
    return f"ws://{_LOOPBACK}:{port}{path}"


def _alive(proc: asyncio.subprocess.Process | None) -> bool:
    return proc is not None and proc.returncode is None


async def _stop_process(proc: asyncio.subprocess.Process | None) -> None:
    if proc is None or proc.returncode is not None:
        return
    if proc.stdin is not None:
        try:
            proc.stdin.close()
        except Exception:  # noqa: BLE001 - the pipe may already be gone
            pass
    try:
        await platform_compat.terminate_and_reap(proc, grace=_STOP_GRACE_SECS)
    except Exception:  # noqa: BLE001 - a child that will not die is logged, not raised
        logger.debug("browser harness child did not stop cleanly", exc_info=True)


async def _worker_request(
    proc: asyncio.subprocess.Process, body: dict[str, Any], timeout: float
) -> dict[str, Any]:
    assert proc.stdin is not None and proc.stdout is not None
    try:
        proc.stdin.write((json.dumps(body) + "\n").encode("utf-8"))
        await proc.stdin.drain()
        line = await asyncio.wait_for(proc.stdout.readline(), timeout)
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"helper timed out after {timeout:.0f}s"}
    except (OSError, ConnectionError, BrokenPipeError) as exc:
        return {"ok": False, "error": f"helper worker is gone: {exc}"}
    if not line:
        return {"ok": False, "error": "helper worker exited"}
    try:
        reply = json.loads(line.decode("utf-8"))
    except ValueError:
        return {"ok": False, "error": "helper worker answered garbage"}
    return reply if isinstance(reply, dict) else {"ok": False, "error": "bad reply"}


def _log_ensure_outcome(task: asyncio.Task[HarnessHandle]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.info("browser harness not connected: %s", exc)


_SUPERVISORS: dict[Path, HarnessSupervisor] = {}


def get_supervisor(home: Path) -> HarnessSupervisor:
    """The one supervisor for *home*; created on first use."""
    key = Path(home)
    sup = _SUPERVISORS.get(key)
    if sup is None:
        sup = HarnessSupervisor(key)
        _SUPERVISORS[key] = sup
    return sup


def _reset_supervisors_for_tests() -> None:
    _SUPERVISORS.clear()


__all__ = [
    "BrowserUnavailable",
    "CONNECTED",
    "DISCONNECTED",
    "ERROR",
    "HELPERS",
    "HarnessHandle",
    "HarnessNotInstalled",
    "HarnessSupervisor",
    "HelperError",
    "IPC_LEAF",
    "PROFILE_LEAF",
    "WAITING_ALLOW",
    "get_supervisor",
]
