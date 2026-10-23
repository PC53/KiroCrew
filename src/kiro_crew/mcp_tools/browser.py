"""The ``browser`` tool: run one operation in the agent's browser.

``schemas()`` is the ADVERTISEMENT half (name, model-facing description, the
JSON Schema a call is validated against) and ``HANDLERS`` maps the name to the
function that runs it. Both halves live here so the contract and the behavior
read together, and ``test_mcp_tool_registry`` fails if one arrives without the
other.

The browser itself is the Browser Harness the gateway supervises
(``browser_harness/``), reached over the gateway's own CDP relay. This tool is
its agent-facing SUBMIT side only: ``kirocrew-core`` is a separate stdio shim, so
the handler cannot reach the supervisor in-process. It POSTs one op to the
gateway route ``/api/browser/op`` over loopback with the internal-secret
handshake, the same shape ``mcp_computer`` uses, and the gateway runs it through
``browser_harness.ops``. The tool holds NO state: the ref map lives in the page,
the tab ledger in the relay, the input lock in the gateway.

Two gates run HERE, before anything is posted, and they fail in opposite
directions on purpose:

* **Governance** (``capabilities.browse``): a denial refuses OUTRIGHT. There is
  no other browser to fall back to, and there must not be one, or the control
  would not control anything.
* **The navigate gate**: only a public http(s) URL, or a loopback ``host:port``
  the OPERATOR listed in ``dashboard.browser_local_origins``, is ever opened.
  Everything else (loopback, private, link-local, the cloud metadata address, a
  parser-differential spelling) is refused, because those are where the user's
  own control planes live, this dashboard included. The same gate covers the
  URL a new tab opens with.

Availability is the gateway's to report, not this process's to probe: a host
with no Harness answers ``not_installed`` and the tool says so. The tool is
always advertised because kiro-cli caches ``tools/list`` for a session's life,
so an install made mid-session is picked up on the next call.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import unquote_to_bytes, urlsplit

from kiro_crew import mcp_core
from kiro_crew.browser_harness.ops import OPS, SLOW_OPS
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.security import canonicalize_ip, redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

# The gateway route the handler submits to. Loopback + ``X-Internal-Secret``; the
# handler answers HTTP 200 ``{ok: true, result}`` / 200 ``{ok: false, code, error}``.
COMMAND_PATH = "/api/browser/op"
COMMAND_TIMEOUT_SECS = 90.0
SLOW_COMMAND_TIMEOUT_SECS = 90.0
FAST_COMMAND_TIMEOUT_SECS = 30.0

# Re-exported for the registry tests and the prompt gate: the ops the gateway serves.
BROWSER_OPS: tuple[str, ...] = OPS

_LOOPBACK_HOST_NAMES = {"localhost", "ip6-localhost", "ip6-loopback"}

# Code points a browser refuses in a host once it is percent-decoded.
_FORBIDDEN_DECODED_HOST_CHARS = frozenset("%/@?#[]:\\<>^| ")

_RESULT_CAP = 20_000

_NOT_INSTALLED_TEXT = (
    "Error: browsing is not set up on this host. Ask the user to install it from "
    "Settings -> Browser (one click), then call the tool again."
)

_URL_GATE_TEXT = (
    "Error: the browser tool only opens public http(s) URLs, plus loopback origins "
    "the user listed under Settings -> Browser -> Local preview origins "
    "(dashboard.browser_local_origins, exact host:port such as localhost:5173). "
    "A localhost, loopback, private or link-local address is refused otherwise. Do "
    "not edit the setting yourself; if the user wants it opened, ask them to add "
    "the origin there (or run `kirocrew config set dashboard.browser_local_origins "
    "'[\"localhost:5173\"]'`, which replaces the whole list), or to open it in "
    "their own browser."
)


def _percent_decoded_host(host: str) -> str | None:
    """Decode ``host`` once, as the browser's URL parser does.

    ``urlsplit`` keeps ``%31%32%37.0.0.1`` encoded, but the browser decodes it
    to ``127.0.0.1``. Returns None when the browser would reject the decoded
    host (invalid UTF-8, or a char a host may not carry).
    """
    try:
        decoded = unquote_to_bytes(host).decode("utf-8")
    except UnicodeDecodeError:
        return None
    if any(
        ch in _FORBIDDEN_DECODED_HOST_CHARS or ord(ch) < 0x21 or ord(ch) == 0x7F for ch in decoded
    ):
        return None
    return decoded


def _navigate_target_is_public(url: str) -> bool:
    """True iff ``url`` is an ordinary public http(s) page.

    The browser accepts ANY http(s) URL, so without this a ``navigate`` op could
    drive a loopback control plane (this dashboard included) or the
    ``169.254.169.254`` metadata endpoint. Kept LOCAL (not imported from the
    dashboard) so the ``kirocrew-core`` shim stays free of dashboard deps.

    ``is_global`` subsumes loopback/link-local/private/CGNAT/reserved in one
    predicate, applied to the address and any embedded IPv4 (``ipv4_mapped`` /
    ``sixtofour``). The host is first run through ``security.canonicalize_ip`` so
    an alternate IPv4 encoding the browser would still normalize -- decimal
    (``2852039166``), hex (``0x...``), octal, short/mixed, IPv6-mapped -- is
    resolved to dotted-quad and caught rather than mistaken for a DNS name. DNS
    names are NOT resolved (a blocking call + rebinding TOCTOU on the hot path);
    the residual public-name->private-IP risk is accepted and documented in the
    prompt.

    A percent-encoded host (``%31%32%37.0.0.1``, ``loc%61lhost``) is decoded
    first, as the browser does, so every check below sees the browser's host.
    """
    raw = url.strip()
    # Parser-differential SSRF: reject backslash (Chromium treats it as '/'),
    # and any control/whitespace char (tab/CR/LF and the rest), which browsers
    # strip or normalize but urlsplit does not -- e.g.
    # ``http://169.254.169.254\@example.com/`` parses to host ``example.com``
    # here yet Chromium navigates to the metadata endpoint. A legitimate URL
    # needs none of these (percent-encode instead), so refuse rather than parse.
    if "\\" in raw or " " in raw or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return False
    parts = urlsplit(raw)
    if parts.scheme.lower() not in ("http", "https"):
        return False
    host = parts.hostname or ""
    if "%" in host and ":" not in host:  # bracketed IPv6 keeps its own path
        decoded = _percent_decoded_host(host)
        if decoded is None:
            return False
        host = decoded
    host = host.lower().rstrip(".")
    if not host or host in _LOOPBACK_HOST_NAMES or host.endswith(".localhost"):
        return False
    # Reject non-ASCII hosts: a fullwidth-digit / IDN form the browser may
    # normalize toward a private IP would otherwise slip through the DNS branch
    # below. Legitimate IDN sites are reached by their ASCII punycode (xn--...).
    if any(ord(ch) > 0x7F for ch in host):
        return False
    try:
        addr: Any = ipaddress.ip_address(canonicalize_ip(host))
    except ValueError:
        return True  # a DNS name (not an IP in any encoding); public browsing unaffected
    candidates = [addr]
    for embedding in ("ipv4_mapped", "sixtofour"):
        embedded = getattr(addr, embedding, None)
        if embedded is not None:
            candidates.append(embedded)
    return all(c.is_global for c in candidates)


def _allowed_local_origins() -> frozenset[str]:
    """The operator's ``dashboard.browser_local_origins``, read fresh each call.

    Already canonical ``host:port`` entries (``config.sections`` drops every
    other shape). Fail-CLOSED to empty: a config read error must not widen the
    navigate gate.
    """
    try:
        raw = getattr(KiroCrewConfig.load().dashboard, "browser_local_origins", [])
    except Exception:
        return frozenset()
    return frozenset(e for e in raw if isinstance(e, str)) if isinstance(raw, list) else frozenset()


def _navigate_target_is_allowed_local(url: str, allowed: frozenset[str]) -> bool:
    """True iff ``url`` is a loopback origin the operator listed, exactly.

    The exception to :func:`_navigate_target_is_public`, never a relaxation of
    it: the same parser-differential refusals run first, then the URL's own
    ``host:port`` (default port filled in from the scheme) must equal an entry
    byte for byte. Only the three spellings ``config.sections`` admits can ever
    match, so an alternate IPv4 encoding (``0x7f000001``, ``2130706433``) or a
    ``*.localhost`` name stays refused even when ``localhost:<port>`` is listed.
    Userinfo is refused outright so ``http://localhost:5173@host/`` cannot be
    read two ways. The gateway's own port is refused even when listed: it is
    the control plane this gate exists to keep the browser away from.
    """
    if not allowed:
        return False
    raw = url.strip()
    if "\\" in raw or " " in raw or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return False
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or "@" in parts.netloc:
        return False
    host = (parts.hostname or "").lower()
    if host == "::1":
        host = "[::1]"
    try:
        port = parts.port
    except ValueError:
        return False
    if port is None:
        port = 443 if scheme == "https" else 80
    if f"{host}:{port}" not in allowed:
        return False
    try:
        return port != mcp_core._api_port()
    except Exception:
        return False  # gateway port unknowable -> cannot prove this is not it


def _url_allowed(url: Any) -> bool:
    """The navigate gate: public, or an operator-listed loopback origin."""
    if not isinstance(url, str):
        return False
    return _navigate_target_is_public(url) or _navigate_target_is_allowed_local(
        url, _allowed_local_origins()
    )


def _gated_url(op: str, op_args: dict[str, Any]) -> Any:
    """The URL an op would open, or None when it opens none.

    ``navigate`` always opens one; ``tabs`` opens one only on ``action: new``
    with a URL other than ``about:blank``.
    """
    if op == "navigate":
        return op_args.get("url", "")
    if op == "tabs" and op_args.get("action") == "new":
        url = op_args.get("url", "about:blank")
        return None if url == "about:blank" else url
    return None


def _args_are_scalar(op_args: dict[str, Any]) -> bool:
    """Reject non-scalar op arguments (e.g. an object-valued ``type.text``).

    Every browser op takes string/number/bool values (a url, a ref, text, a
    key) or a list of those; none takes a nested object. An object would reach
    the page and render as ``[object Object]``, so refuse it here rather than
    inserting garbage into the page.
    """
    for value in op_args.values():
        if value is None or isinstance(value, (str, int, float, bool)):
            continue
        if isinstance(value, list) and all(
            item is None or isinstance(item, (str, int, float, bool)) for item in value
        ):
            continue
        return False
    return True


def schemas() -> list[dict[str, Any]]:
    """Descriptor for the browser tool. Always advertised (see the module docstring)."""
    return [
        {
            "name": "browser",
            "description": (
                "Drive the agent's browser: run ONE operation on the page and the "
                "user watches it live in the chat. `op` is the action and `args` its "
                "parameters. navigate {url}; snapshot (the page as an outline with "
                "element refs; call it before click/type and again after the page "
                "changes); click {ref | x,y, button?, clicks?: 1|2}; type {ref?, text, "
                "submit?} (a password field is refused: the user signs in by taking "
                "over); press_key {key, modifiers?}; hover {ref | x,y}; drag {ref|x,y "
                "-> to_ref|to_x,to_y}; scroll {ref|x,y?, dx?, dy?}; select_option "
                "{ref, values}; wait_for {text | textGone | time (s) | load}; "
                "back; console {limit?}; screenshot (writes a PNG, returns its "
                "absolute path: show it with ![..](path)); tabs {action: list|switch"
                "|new, target_id?, url?} over the tabs you opened. Only public "
                "http(s) URLs open, plus loopback origins the user listed in "
                "Settings -> Browser. While the user has taken over, every op "
                "returns user_in_control: wait for them to hand the browser back."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "op": {
                        "type": "string",
                        "enum": list(BROWSER_OPS),
                        "description": "The browser operation to perform.",
                    },
                    "args": {
                        "type": "object",
                        "description": (
                            "Operation parameters (e.g. url, ref, text, key). "
                            "Optional; defaults to an empty object."
                        ),
                    },
                },
                "required": ["op"],
            },
        }
    ]


def _post_command(
    session_key: str, op: str, args: dict[str, Any], session_header: str, timeout_s: float
) -> tuple[int | None, dict]:
    """POST one op to the gateway, surfacing the HTTP status.

    Returns ``(status, body)`` -- ``status`` is ``None`` for a transport failure
    (no gateway listening, a timeout). ``session_key`` goes in the BODY, where
    the gateway keys the live card and the input lock by it. ``session_header``
    is the ``dashboard:``-namespaced key for the ``X-Session-Key`` header, sent
    together with the per-session token (``_session_token_header``) only when
    the caller resolved one: the header and token are a pair the peer check
    compares, and a fabricated header would be refused where a missing one is
    simply absent.
    """
    body = json.dumps({"session_key": session_key, "op": op, "args": args}).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Internal-Secret": mcp_core._internal_secret(),
    }
    if session_header:
        headers["X-Session-Key"] = session_header
        headers.update(mcp_core._session_token_header())
    req = urllib.request.Request(
        f"{mcp_core._api_base()}{COMMAND_PATH}", data=body, headers=headers, method="POST"
    )
    try:
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- URL is the loopback gateway (_api_base()) + a fixed internal path; never agent-controlled  # noqa: E501
        with mcp_core._api_urlopen(req, timeout=timeout_s) as resp:
            return int(resp.status), json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            return int(exc.code), json.loads(exc.read())
        except Exception:
            return int(exc.code), {}
    except (urllib.error.URLError, TimeoutError, socket.timeout):
        return None, {}
    except Exception:
        return None, {}


def _redacted(text: str) -> str:
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def _result_text(op: str, result: Any) -> str:
    """Concise success text, with untrusted page content redacted."""
    if not isinstance(result, dict):
        return f"Browser {op}: ok"
    if op == "screenshot":
        path = str(result.get("path", ""))
        return f"Browser screenshot saved: {path}\nShow it with ![screenshot]({path})"
    if op == "snapshot":
        head = f"Browser snapshot of {result.get('url', '')} -- {result.get('title', '')}".rstrip(
            " -"
        )
        outline = str(result.get("snapshot", ""))
        partial = f"\n(partial: {result['partial']})" if result.get("partial") else ""
        return _redacted(f"{head}{partial}\n{outline}")[:_RESULT_CAP]
    fields = {k: v for k, v in result.items() if k != "ok" and v is not None}
    if not fields:
        return f"Browser {op}: ok"
    return _redacted(f"Browser {op}: {json.dumps(fields, default=str)}")[:_RESULT_CAP]


def _error_text(code: str, detail: str) -> str:
    if code == "not_installed":
        return _NOT_INSTALLED_TEXT
    if code == "user_in_control":
        return (
            "Error: user_in_control -- the user has taken over the browser. Do not "
            "retry in a loop; tell them you are waiting, and continue once they hand "
            "it back (Esc or 'Hand back')."
        )
    detail = _redacted(detail or "the browser op failed")[:2000]
    return f"Error ({code}): {detail}" if code else f"Error: {detail}"


def browser(name: str, args: dict[str, Any]) -> str:
    # Governance gate (capabilities.browse). A policy that denies web browsing
    # stops it here, outright: there is no other browser to fall back to.
    session_key = mcp_core._resolve_session_key()
    gov_denied = mcp_core._vet_browse_governance(session_key)
    if gov_denied:
        return f"Error: {gov_denied}"
    op = args.get("op")
    if not isinstance(op, str) or op not in BROWSER_OPS:
        return f"Error: op must be one of: {', '.join(BROWSER_OPS)}"
    op_args = args.get("args") or {}
    if not isinstance(op_args, dict):
        return "Error: args must be a JSON object"
    if not _args_are_scalar(op_args):
        return "Error: args values must be strings, numbers, or booleans (or lists of those)"

    # The navigate gate, applied to every URL an op would open (see _gated_url).
    gated = _gated_url(op, op_args)
    if gated is not None and not _url_allowed(gated):
        return _URL_GATE_TEXT

    # The caller's session key, resolved LENIENTLY like every other X-Session-Key
    # tool (artifacts, sessions, skills): on a default install the strict
    # resolver returns "" for the user's own main session too. The key names the
    # chat slot whose live card shows this op and whose input lock can refuse
    # it. A session with no key (a CLI run with no dashboard) still browses, with
    # no card to show it on; it gets a process-local key and no header.
    session_header = ""
    if session_key:
        header_err = mcp_core._session_key_header_error(session_key)
        if header_err:
            return f"Error: {header_err}"
        session_header = session_key
    body_key = session_key or f"anonymous:{os.getpid()}"

    timeout_s = SLOW_COMMAND_TIMEOUT_SECS if op in SLOW_OPS else FAST_COMMAND_TIMEOUT_SECS
    logger.debug("browser/tool: op=%s session=%s -> POST %s", op, body_key, COMMAND_PATH)
    status, payload = _post_command(body_key, op, op_args, session_header, timeout_s)
    if status == 200 and isinstance(payload, dict) and payload.get("ok") is True:
        return _result_text(op, payload.get("result"))
    if status == 200 and isinstance(payload, dict) and payload.get("ok") is False:
        return _error_text(str(payload.get("code") or ""), str(payload.get("error") or ""))
    if status is None:
        return "Error: the gateway did not answer the browser op (is Kiro Crew running?)"
    code = payload.get("code") if isinstance(payload, dict) else None
    err = payload.get("error") if isinstance(payload, dict) else None
    return f"Error: browser op failed (HTTP {status}{f', {code}' if code else ''}): {err or 'no detail'}"


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "browser": browser,
}
