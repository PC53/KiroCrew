"""Settings -> Browser's four routes: status, install, test, disconnect.

``GET  /api/browser/status``      the supervisor's status plus the install state
``POST /api/browser/install``     start the engine install job (venv + Chromium)
``POST /api/browser/test``        connect (bounded wait) and count the agent's tabs
``POST /api/browser/disconnect``  stop browser, daemon and relay

All four are OWNER-only and REFUSE a caller authenticated by the internal secret.
The agent reaches the browser through the ``browser`` MCP tool and its governance
check; an agent that could install an interpreter the gateway executes, or connect
to the user's Chrome by itself, would route around that check. A human in an
authenticated dashboard is the consent these routes rest on. The read is owner-only
too: the status names which browser profile is in use and whether the user's Chrome
is accepting connections, which is reconnaissance for nothing an agent should do.

The install is a JOB: the dashboard cannot tell which install is running from its own
mutation state (a refreshed page never saw the request), so the gateway keeps one
:class:`InstallJob` per install, published before any work starts, and every status
read reports it. In-memory on the event loop: a gateway restart starts with no job,
which is what keeps a stale "running" from surviving one. The installer runs in a
worker thread and reports stages through a callback marshalled back with
``call_soon_threadsafe``; each update names its job id so a late callback from a
finished job cannot move a newer one.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aiohttp import web

from kiro_crew.browser_harness import install as harness_install
from kiro_crew.browser_harness.supervisor import CONNECTED, HarnessSupervisor, get_supervisor
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

# ── SEL operation names: one prefix so the audit trail filters with one glob ──
OP_STATUS = "browser_harness.status"
OP_INSTALL = "browser_harness.install"
OP_TEST = "browser_harness.test"
OP_DISCONNECT = "browser_harness.disconnect"

#: How long ``POST /api/browser/test`` waits for a connection before answering with
#: whatever state it reached. The Allow sheet on the user's Chrome outlasts this on
#: purpose; the page polls ``GET /api/browser/status`` while it shows ``waiting_allow``.
TEST_WAIT_SECS = 10.0

STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"

#: Wall clock for the job timestamps; a module attribute so tests can pin it.
clock = time.time


@dataclass
class InstallJob:
    """One engine install: what it targets, where it is, how it ended."""

    id: str
    components: tuple[str, ...]
    status: str
    stage: str
    started_at: float
    updated_at: float
    finished_at: float | None = None
    error: str | None = None

    @classmethod
    def start(cls, components: tuple[str, ...]) -> InstallJob:
        now = clock()
        return cls(
            uuid.uuid4().hex, components, STATUS_RUNNING, harness_install.STAGE_PREPARING, now, now
        )

    @property
    def running(self) -> bool:
        return self.status == STATUS_RUNNING

    def apply_stage(self, job_id: str, stage: str) -> bool:
        if job_id != self.id or not self.running or stage not in harness_install.STAGES:
            return False
        self.stage = stage
        self.updated_at = clock()
        return True

    def finish(self, job_id: str, status: str, error: str | None = None) -> bool:
        if job_id != self.id or not self.running:
            return False
        now = clock()
        self.status, self.error, self.finished_at, self.updated_at = status, error, now, now
        return True

    def snapshot(self) -> dict[str, Any]:
        end = self.finished_at if self.finished_at is not None else clock()
        return {
            "id": self.id,
            "components": list(self.components),
            "status": self.status,
            "stage": self.stage,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at) if self.finished_at is not None else None,
            "elapsed_s": round(max(0.0, end - self.started_at), 1),
            "error": self.error,
        }


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def _sel():
    """Late-binding ``sel()``: this module is imported BY ``handlers/__init__``."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811 — circular import

    return _pkg.sel()


def _supervisor(request: web.Request) -> HarnessSupervisor:
    """The gateway's supervisor; ``app["browser_harness_supervisor"]`` lets a test inject one."""
    injected = request.app.get("browser_harness_supervisor")
    if isinstance(injected, HarnessSupervisor):
        return injected
    return get_supervisor(config_dir())


def _deny_non_owner(request: web.Request, operation: str) -> web.Response | None:
    """Owner cookie or token only; app tokens, non-owners, anonymous and the internal secret are refused."""
    from kiro_crew.dashboard.handlers._shared import _owner_denial_response
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    if request.get("internal_auth"):
        _audit(
            request,
            operation,
            "denied",
            "internal-secret callers may not control the browser engine",
        )
        return web.json_response(
            {"error": "forbidden for internal callers", "code": "internal_caller"}, status=403
        )
    if is_owner_dashboard_request(request):
        _audit(request, operation, "allowed")
        return None
    _audit(request, operation, "denied", "browser engine routes require the dashboard owner")
    return _owner_denial_response(request, "dashboard user required", "dashboard_user_required")


def _audit(request: web.Request, operation: str, outcome: str, error: str = "") -> None:
    app_name = request.get("app", "")
    caller = f"app:{app_name}" if app_name else str(request.get("user") or "anonymous")
    try:
        _sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome=outcome,
            source="browser_api",
            resources=request.path,
            error=error,
        )
    except Exception:  # noqa: BLE001 - an audit failure never breaks the request
        logger.warning("SEL logging failed for %s", operation, exc_info=True)


def _job(state: Any) -> InstallJob | None:
    job = getattr(state, "_browser_harness_install_job", None)
    return job if isinstance(job, InstallJob) else None


def _install_active(state: Any) -> bool:
    job = _job(state)
    task = getattr(state, "_browser_harness_install_task", None)
    return bool((job is not None and job.running) or (task is not None and not task.done()))


def _status_payload(request: web.Request) -> dict[str, Any]:
    """``supervisor.status()`` plus what Settings -> Browser reads on top of it.

    The Settings wire types (``website/src/api/client/browserHarness.ts``) read
    ``chromium.downloaded``, ``install_job.error_detail`` and ``platform``; the
    supervisor's own spellings (``chromium.present``, ``install_job.error``) stay
    beside them so nothing that already reads them moves.
    """
    state = request.app["state"]
    payload = _supervisor(request).status()
    job = _job(state)
    chromium = payload.get("chromium")
    if isinstance(chromium, dict):
        chromium.setdefault("downloaded", bool(chromium.get("present")))
    snapshot = job.snapshot() if job is not None else None
    if snapshot is not None:
        snapshot["error_detail"] = snapshot.get("error")
    payload["installing"] = _install_active(state)
    payload["install_job"] = snapshot
    payload["platform"] = sys.platform
    return payload


async def api_browser_harness_status(request: web.Request) -> web.Response:
    """GET /api/browser/status -- install state, browser in use, connection, job."""
    denied = _deny_non_owner(request, OP_STATUS)
    if denied is not None:
        return denied
    return web.json_response(await asyncio.to_thread(_status_payload, request))


async def api_browser_harness_install(request: web.Request) -> web.Response:
    """POST /api/browser/install -- start the engine install; 409 while one runs.

    Body (optional): ``{"components": ["harness", "chromium"]}``; default both.
    """
    denied = _deny_non_owner(request, OP_INSTALL)
    if denied is not None:
        return denied
    state = request.app["state"]
    components = await _requested_components(request)
    if components is None:
        return web.json_response(
            {"error": "unknown component", "code": "bad_component"}, status=400
        )
    if _install_active(state):
        job = _job(state)
        return web.json_response(
            {
                "error": "an install is already running",
                "code": "install_already_running",
                "install_job": job.snapshot() if job is not None else None,
            },
            status=409,
        )
    _start_install_job(state, components)
    return web.json_response(await asyncio.to_thread(_status_payload, request), status=202)


async def api_browser_harness_test(request: web.Request) -> web.Response:
    """POST /api/browser/test -- connect with a bounded wait, report the state reached."""
    denied = _deny_non_owner(request, OP_TEST)
    if denied is not None:
        return denied
    report = await _supervisor(request).test(TEST_WAIT_SECS)
    report["ok"] = report.get("connection") == CONNECTED
    return web.json_response(report)


async def api_browser_harness_disconnect(request: web.Request) -> web.Response:
    """POST /api/browser/disconnect -- stop browser, daemon and relay."""
    denied = _deny_non_owner(request, OP_DISCONNECT)
    if denied is not None:
        return denied
    await _supervisor(request).stop()
    return web.json_response(await asyncio.to_thread(_status_payload, request))


async def _requested_components(request: web.Request) -> tuple[str, ...] | None:
    if not request.can_read_body:
        return harness_install.COMPONENTS
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body means the default
        return harness_install.COMPONENTS
    raw = body.get("components") if isinstance(body, dict) else None
    if raw is None:
        return harness_install.COMPONENTS
    if not isinstance(raw, list) or not raw:
        return None
    components = tuple(str(c) for c in raw)
    if any(c not in harness_install.COMPONENTS for c in components):
        return None
    return components


def _start_install_job(state: Any, components: tuple[str, ...]) -> None:
    loop = asyncio.get_running_loop()
    job = InstallJob.start(components)
    state._browser_harness_install_job = job

    def _on_stage(stage: str, _detail: str) -> None:
        loop.call_soon_threadsafe(job.apply_stage, job.id, stage)

    async def _run() -> None:
        try:
            await harness_install.install(config_dir(), _on_stage, components=components)
        except asyncio.CancelledError:
            job.finish(job.id, STATUS_INTERRUPTED, "interrupted: the gateway stopped this install")
            raise
        except harness_install.InstallError as exc:
            job.finish(job.id, STATUS_FAILED, f"{exc.stage}: {exc.detail}")
            return
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            job.finish(job.id, STATUS_FAILED, str(exc)[-harness_install.ERROR_DETAIL_CAP :])
            return
        job.finish(job.id, STATUS_SUCCEEDED)

    state._browser_harness_install_task = asyncio.create_task(_run())


async def stop_browser_harness_install(state: Any) -> None:
    """Cancel a running install at gateway shutdown. Idempotent, never raises."""
    task = getattr(state, "_browser_harness_install_task", None)
    if isinstance(task, asyncio.Task) and not task.done():
        task.cancel()
        await asyncio.wait({task})
        if not task.cancelled():
            task.exception()


__all__ = [
    "InstallJob",
    "TEST_WAIT_SECS",
    "api_browser_harness_disconnect",
    "api_browser_harness_install",
    "api_browser_harness_status",
    "api_browser_harness_test",
    "stop_browser_harness_install",
]
