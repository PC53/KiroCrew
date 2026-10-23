"""Installing Browser Harness: a pinned venv under the data home, from a hash lock.

``<home>/browser-harness`` is a virtual environment the gateway creates with its own
interpreter and fills with ``pip install --require-hashes -r requirements.lock``. The
lock ships in this package (``setup.cfg`` package data) and pins every wheel of
``browser-harness==0.1.13`` and its dependencies by sha256, so what runs under the
gateway is exactly what was audited: a mirror or a hijacked index cannot substitute a
file without failing the hash check.

The prefix is a ``READONLY`` crew-home leaf (``sandbox_leaves``): a sandboxed process
can neither replace the interpreter the gateway later executes nor read the Chromium
profile beside it. The installer runs in the gateway, outside the sandbox, so it can
still lay down a new version.

Kiro's Chromium (:mod:`.chromium`) is downloaded into the same prefix by the same
install job so one Settings button covers the whole engine.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew.browser_harness import HARNESS_VERSION, chromium
from kiro_crew.platform_compat import IS_WINDOWS
from kiro_crew.subprocess_utf8 import UTF8_TEXT

logger = logging.getLogger(__name__)

#: The crew-home leaf. Spelled identically in ``sandbox_leaves`` and
#: ``security.paths_lists``; ``test_browser_harness_install`` pins the three equal.
PREFIX_LEAF = "browser-harness"

#: The shipped lock file, beside this module.
LOCK_FILE = Path(__file__).with_name("requirements.lock")

#: Written last, so a prefix without it is an install that did not finish.
MARKER_FILE = "kirocrew-harness.json"

#: Harness 0.1.13 declares ``Requires-Python >= 3.11``.
MIN_PYTHON: tuple[int, int] = (3, 11)

#: Progress stages, in order. The Settings page shows the current one.
STAGE_PREPARING = "preparing"
STAGE_CREATING_VENV = "creating_venv"
STAGE_INSTALLING_HARNESS = "installing_harness"
STAGE_FINISHING = "finishing"
STAGES: tuple[str, ...] = (
    STAGE_PREPARING,
    STAGE_CREATING_VENV,
    STAGE_INSTALLING_HARNESS,
    chromium.STAGE_DOWNLOADING,
    chromium.STAGE_EXTRACTING,
    STAGE_FINISHING,
)

#: Which parts an install request may ask for.
COMPONENT_HARNESS = "harness"
COMPONENT_CHROMIUM = "chromium"
COMPONENTS: tuple[str, ...] = (COMPONENT_HARNESS, COMPONENT_CHROMIUM)

#: Budgets for the two subprocess steps. ``venv`` is seconds; a cold pip install of
#: a dozen wheels over a slow link is minutes.
_VENV_TIMEOUT_SECS = 120.0
_PIP_TIMEOUT_SECS = 15 * 60.0

#: How much of a failed step's output the state carries. The Settings page shows
#: the tail, which for pip is where the reason is.
ERROR_DETAIL_CAP = 2000

#: pip flags: hashes are mandatory, nothing is prompted, no version nag on stderr.
_PIP_ARGS: tuple[str, ...] = (
    "-m",
    "pip",
    "install",
    "--require-hashes",
    "--no-input",
    "--disable-pip-version-check",
    "-r",
)

ProgressCallback = Callable[[str, str], None]


class InstallError(RuntimeError):
    """A step failed; ``detail`` is the bounded, operator-readable reason."""

    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail[-ERROR_DETAIL_CAP:]


@dataclass(frozen=True)
class InstallState:
    """What is installed under the prefix, from the filesystem alone."""

    prefix: Path
    installed: bool
    version: str | None
    python: Path | None
    chromium: chromium.ChromiumState
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "installed": self.installed,
            "version": self.version,
            "wanted_version": HARNESS_VERSION,
            "prefix": str(self.prefix),
            "chromium": self.chromium.as_dict(),
            "error": self.error,
        }


def harness_prefix(home: Path) -> Path:
    """The venv root: ``<home>/browser-harness``."""
    return Path(home) / PREFIX_LEAF


def venv_python(prefix: Path) -> Path:
    """The interpreter inside *prefix*, by the venv layout of this OS."""
    if IS_WINDOWS:
        return prefix / "Scripts" / "python.exe"
    return prefix / "bin" / "python"


def lock_digest(lock: Path = LOCK_FILE) -> str:
    return hashlib.sha256(lock.read_bytes()).hexdigest()


def install_state(home: Path) -> InstallState:
    """Read the install from disk. Never launches anything, never raises."""
    prefix = harness_prefix(home)
    python = venv_python(prefix)
    marker = prefix / MARKER_FILE
    version: str | None = None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("version"), str):
            version = data["version"]
    except (OSError, ValueError):
        version = None
    try:
        have_python = python.is_file()
    except OSError:
        have_python = False
    installed = have_python and version == HARNESS_VERSION
    return InstallState(
        prefix=prefix,
        installed=installed,
        version=version,
        python=python if have_python else None,
        chromium=chromium.chromium_state(prefix),
    )


async def install(
    home: Path,
    progress: ProgressCallback | None = None,
    *,
    components: tuple[str, ...] = COMPONENTS,
) -> InstallState:
    """Create or refresh the venv and download Chromium. Raises :class:`InstallError`.

    Runs its blocking steps in a worker thread; *progress* is called from that
    thread with ``(stage, detail)`` and must marshal onto the loop itself.
    """
    return await asyncio.to_thread(install_sync, home, progress, components=components)


def install_sync(
    home: Path,
    progress: ProgressCallback | None = None,
    *,
    components: tuple[str, ...] = COMPONENTS,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> InstallState:
    """The blocking body of :func:`install`. *runner* replaces ``subprocess.run`` in tests."""
    notify = progress or (lambda _stage, _detail: None)
    prefix = harness_prefix(home)
    notify(STAGE_PREPARING, str(prefix))
    if COMPONENT_HARNESS in components:
        _install_harness(prefix, notify, runner or _run)
    if COMPONENT_CHROMIUM in components:
        try:
            chromium.download_chromium(prefix, notify)
        except chromium.ChromiumDownloadError as exc:
            raise InstallError(chromium.STAGE_DOWNLOADING, str(exc)) from exc
    notify(STAGE_FINISHING, str(prefix))
    return install_state(home)


def _install_harness(
    prefix: Path,
    notify: ProgressCallback,
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    if sys.version_info < MIN_PYTHON:
        raise InstallError(
            STAGE_PREPARING,
            f"Browser Harness needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+; "
            f"the gateway runs {sys.version_info[0]}.{sys.version_info[1]}",
        )
    if not LOCK_FILE.is_file():
        raise InstallError(STAGE_PREPARING, f"lock file missing: {LOCK_FILE}")
    prefix.parent.mkdir(parents=True, exist_ok=True)
    state = install_state(prefix.parent)
    python = venv_python(prefix)
    if not state.python or state.version != HARNESS_VERSION:
        notify(STAGE_CREATING_VENV, str(prefix))
        # ``--clear`` empties an older venv's site-packages; the chromium directory
        # beside them is not part of the venv layout and survives.
        _step(
            run,
            [sys.executable, "-m", "venv", "--clear", str(prefix)],
            _VENV_TIMEOUT_SECS,
            STAGE_CREATING_VENV,
        )
    notify(STAGE_INSTALLING_HARNESS, HARNESS_VERSION)
    _step(
        run, [str(python), *_PIP_ARGS, str(LOCK_FILE)], _PIP_TIMEOUT_SECS, STAGE_INSTALLING_HARNESS
    )
    marker = {"version": HARNESS_VERSION, "lock_sha256": lock_digest(), "python": str(python)}
    (prefix / MARKER_FILE).write_text(json.dumps(marker, indent=2), encoding="utf-8")


def _step(
    run: Callable[..., subprocess.CompletedProcess[str]],
    argv: list[str],
    timeout: float,
    stage: str,
) -> None:
    try:
        result = run(argv, timeout)
    except subprocess.TimeoutExpired as exc:
        raise InstallError(stage, f"timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        raise InstallError(stage, str(exc)) from exc
    if result.returncode != 0:
        output = (result.stderr or "") + (result.stdout or "")
        raise InstallError(stage, output.strip() or f"exit {result.returncode}")


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """The installer's one spawn: a fixed argv of our own interpreter and the venv's.

    Every token is a constant of this module or a path derived from the data home;
    the only trigger is the owner pressing Install in Settings.
    """
    return subprocess.run(
        argv,
        capture_output=True,
        timeout=timeout,
        check=False,
        env=_child_env(),
        **UTF8_TEXT,
    )


def _child_env() -> dict[str, str]:
    return {**os.environ, "PYTHONIOENCODING": "utf-8", "PIP_NO_INPUT": "1"}
