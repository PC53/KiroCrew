"""The ``browser`` section: which browser the agent drives, and the live view in chat.

Settings -> Browser writes every key here through ``PATCH /api/config/kirocrew``;
``browser_harness`` (the supervisor and the screencast) reads them with these
defaults. ``dashboard.browser_local_origins``, the Preview tab's allowlist, stays
on the ``dashboard`` section because the Preview tab is a dashboard surface the
agent never drives.

An owner of the :mod:`kiro_crew.config.sections` facade; it never imports the facade.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from kiro_crew.config.fields import _meta, _safe_bool
from kiro_crew.config.sections_defaults import SectionReader

logger = logging.getLogger("kiro_crew.config.loader")


#: Which browser the agent drives. ``auto`` is the discovery order the RFC fixes
#: (the user's Chrome, then Edge on Windows, then Kiro's headless Chromium when
#: there is no display or the Chrome policy forbids remote debugging). The three
#: hand picks each pin one rung of that ladder; ``headless`` is Kiro's Chromium
#: with no window even on a desktop.
BROWSER_CHOICE_AUTO = "auto"
BROWSER_CHOICES = (BROWSER_CHOICE_AUTO, "user_chrome", "kiro_chromium", "headless")

#: Stream quality of the live view. ``auto`` lets the adaptive ladder in
#: ``browser_harness/screencast.py`` pick a rung from the measured link; a fixed
#: value pins the JPEG quality and scale and leaves only the frame rate adaptive.
LIVE_QUALITY_AUTO = "auto"
LIVE_QUALITIES = (LIVE_QUALITY_AUTO, "high", "medium", "low")

#: Frame-rate ceilings the card can be asked for. 60 is the ladder's top rung; the
#: machine's own limit is lower on a slow link, and the ladder never exceeds this.
LIVE_MAX_FPS_CHOICES = (15, 30, 60)
LIVE_MAX_FPS_DEFAULT = 60

# Values already warned about in this process, keyed on ``repr`` because they
# arrive from ``config.json`` and may be unhashable. The gateway loads config
# repeatedly, so an unusable stored value is per-install information, not
# per-load. Exposed for tests to reset.
_WARNED_BROWSER_VALUES: set[str] = set()


def _warn_once(key: str, value: object, resolved: object, choices: tuple) -> None:
    seen = f"{key}={value!r}"
    if seen in _WARNED_BROWSER_VALUES:
        return
    _WARNED_BROWSER_VALUES.add(seen)
    logger.warning(
        "Unusable %s %r; using %r instead. Choices: %s. Run 'kirocrew config set %s "
        "<value>' to choose one, or 'kirocrew config defaults --adopt %s' to drop the "
        "stored value and this notice.",
        key,
        value,
        resolved,
        ", ".join(str(c) for c in choices),
        key,
        key,
    )


def browser_choice_resolution(value: object) -> str:
    """The browser ``browser.choice`` of *value* selects. Pure; never logs.

    A JSON ``null`` is the absent key and takes the default. Any value outside
    :data:`BROWSER_CHOICES` degrades to ``auto``: that is the only value that can
    never leave the agent without a browser, since the discovery order falls
    through to Kiro's own Chromium.
    """
    if value in BROWSER_CHOICES:
        return str(value)
    return BROWSER_CHOICE_AUTO


def live_quality_resolution(value: object) -> str:
    """The quality ``browser.live.quality`` of *value* selects. Pure; never logs."""
    if value in LIVE_QUALITIES:
        return str(value)
    return LIVE_QUALITY_AUTO


def live_max_fps_resolution(value: object) -> int:
    """The ceiling ``browser.live.max_fps`` of *value* selects. Pure; never logs.

    A digit string is an accepted spelling (``"30"``), so a value typed at the
    CLI reads back as the int. A bool is NOT a frame rate even though it is an
    int in Python; it degrades like any other junk, to the top rung, which is
    what the ladder would have reached anyway.
    """
    if isinstance(value, bool):
        return LIVE_MAX_FPS_DEFAULT
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, int) and value in LIVE_MAX_FPS_CHOICES:
        return value
    return LIVE_MAX_FPS_DEFAULT


def _validated_choice(value: object) -> str:
    resolved = browser_choice_resolution(value)
    if value is not None and value not in BROWSER_CHOICES:
        _warn_once("browser.choice", value, resolved, BROWSER_CHOICES)
    return resolved


def _validated_quality(value: object) -> str:
    resolved = live_quality_resolution(value)
    if value is not None and value not in LIVE_QUALITIES:
        _warn_once("browser.live.quality", value, resolved, LIVE_QUALITIES)
    return resolved


def _validated_max_fps(value: object) -> int:
    resolved = live_max_fps_resolution(value)
    spelled = int(value.strip()) if isinstance(value, str) and value.strip().isdigit() else value
    if value is not None and not (
        isinstance(spelled, int) and not isinstance(spelled, bool) and spelled == resolved
    ):
        _warn_once("browser.live.max_fps", value, resolved, LIVE_MAX_FPS_CHOICES)
    return resolved


@dataclass
class BrowserLiveConfig:
    """``browser.live``: the live view of the agent's browser in chat."""

    enabled: bool = field(
        default=True,
        metadata=_meta(
            "Show live view in chat",
            "When the agent uses the browser, the turn shows a live card of the page "
            "in the chat. Click the picture to take over. Off hides the card; the "
            "agent still browses.",
        ),
    )
    quality: str = field(
        default=LIVE_QUALITY_AUTO,
        metadata=_meta(
            "Quality",
            "`auto` adapts frame rate and picture quality to your link, lowering the "
            "picture first and the frame rate second when the link is slow. A fixed "
            "value pins the picture quality and leaves only the frame rate adaptive.",
            enum=list(LIVE_QUALITIES),
        ),
    )
    max_fps: int = field(
        default=LIVE_MAX_FPS_DEFAULT,
        metadata=_meta(
            "Max frame rate",
            "Frames per second the live view never exceeds: 15, 30 or 60. 60 is the "
            "most this machine streams. No frames are sent while the card is off "
            "screen.",
            enum=list(LIVE_MAX_FPS_CHOICES),
        ),
    )
    mask_passwords: bool = field(
        default=True,
        metadata=_meta(
            "Mask password fields",
            "Cover password boxes in the live picture. Passwords are only ever typed "
            "by you, during a takeover; they never pass through the agent and, with "
            "this on, never appear in a frame.",
        ),
    )

    @classmethod
    def from_raw(cls, section: object) -> "BrowserLiveConfig":
        """Build from a raw ``browser.live`` dict; a non-dict builds the defaults."""
        if not isinstance(section, dict):
            return cls()
        reader = SectionReader(cls, section)
        return cls(
            enabled=reader.read("enabled", _safe_bool),
            quality=_validated_quality(reader.get("quality")),
            max_fps=_validated_max_fps(reader.get("max_fps")),
            mask_passwords=reader.read("mask_passwords", _safe_bool),
        )


@dataclass
class BrowserConfig:
    """``browser``: the browser the agent drives, and the live view in chat.

    Every field is hot-applied: the supervisor reads ``choice`` when it next
    launches or attaches a browser, the screencast reads ``live`` when it next
    starts a stream, and Settings reads ``first_run_dismissed`` on every render.
    """

    choice: str = field(
        default=BROWSER_CHOICE_AUTO,
        metadata=_meta(
            "Browser",
            "Which browser the agent drives. `auto` picks your Chrome (with your "
            "logins, once you allow remote debugging in it), then Edge on Windows, "
            "then Kiro's own Chromium without a window on a machine with no display. "
            "`user_chrome` and `kiro_chromium` pin one of those; `headless` runs "
            "Kiro's Chromium without a window even on a desktop.",
            enum=list(BROWSER_CHOICES),
        ),
    )
    live: BrowserLiveConfig = field(
        default_factory=BrowserLiveConfig,
        metadata=_meta("Live view", "The live view of the agent's browser in chat."),
    )
    first_run_dismissed: bool = field(
        default=False,
        metadata=_meta(
            "First-run steps dismissed",
            "Set once the three setup steps on Settings -> Browser are completed or "
            "skipped, after which the page shows the status card instead. Written by "
            "the dashboard; clear it to see the steps again.",
        ),
    )

    @classmethod
    def from_raw(cls, section: object) -> "BrowserConfig":
        """Build from a raw ``browser`` dict -- the ONE parse site.

        Every value is NORMALIZED rather than validated-and-rejected, like
        :meth:`DecisionsConfig.from_raw`: a hand-edited ``config.json`` must not
        stop the gateway booting, and every key here has a value that cannot make
        things worse (``auto`` finds a browser; the ladder's top rung is what it
        would reach anyway). Accepts whatever ``json.loads`` produced, ``None`` and
        a non-dict included.
        """
        if not isinstance(section, dict):
            return cls()
        reader = SectionReader(cls, section)
        return cls(
            choice=_validated_choice(reader.get("choice")),
            live=BrowserLiveConfig.from_raw(section.get("live")),
            first_run_dismissed=reader.read("first_run_dismissed", _safe_bool),
        )
