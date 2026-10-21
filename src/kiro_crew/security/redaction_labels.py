"""Context rules: a secret found by the field it sits in or the key ID beside it.

Two rules live here, both ranked after the fixed patterns of
:mod:`kiro_crew.security.redaction_patterns`:

* **Labelled values.** A value after a secret field name (``password =``,
  ``"SecretAccessKey":``, ``Authorization: Bearer``) is a Secret. Only the VALUE
  is replaced; the label stays, so structured output still parses and a reader
  sees which field went. Labels match case-insensitively and accept any of the
  usual word separators (see ``credential_patterns``).
* **Key-ID pairing.** An AWS access key ID is an Identifier and stays visible,
  but an unlabelled 40-character secret-shaped value next to one is the secret
  half of the pair, so that value is a Secret.

Each function returns spans against its input; the redactor merges them with the
other passes' spans.

:mod:`kiro_crew.security.redaction` is the import path callers use.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from typing import NamedTuple

from kiro_crew import credential_values
from kiro_crew.credential_patterns import (
    AUTHORIZATION_LABEL,
    AUTHORIZATION_VALUE,
    AWS_KEY_ID,
    AWS_KEY_ID_REDACTION,
    AWS_LABEL_WORDS,
    AWS_SECRET_LABEL,
    AWS_SESSION_LABEL,
    BARE_SIGNATURE_LABEL,
    GENERIC_LABEL_WORDS,
    GENERIC_SECRET_LABEL,
    LABEL_SEPARATOR,
    LABEL_VALUE_OR_QUOTED,
    RULE_TIERS,
    SIGNING_LABEL,
    TIER_SECRET,
)

from .redaction_normalise import normalised_views
from .secret_catalog import CATALOG_ARRIVING_REACH, catalog_keyword_start, is_identifier_value

#: Every rule id this module writes into a span.
RULE_IDS = (
    "authorization_header",
    "aws_secret_access_key",
    "aws_session_token",
    "secret_label",
    "aws_key_pair",
    "request_signature",
)


#: The rules whose finding blocks a request: Secret tier, except a generic
#: labelled value (it redacts output; its label is too weak to refuse on).
BLOCKING_RULE_IDS = frozenset(
    rule for rule in RULE_IDS if RULE_TIERS[rule] == TIER_SECRET and rule != "secret_label"
)


class LabelSpan(NamedTuple):
    """One value to redact: its span, the rule that found it and the label kept."""

    start: int
    end: int
    rule: str
    label: str


#: A secret label, its separator and its value. The label has no left boundary:
#: a label glued to the text before it (``AWS_SECRET_ACCESS_KEY=``) is still one.
_LABELLED_RE = re.compile(
    rf"(?P<label>(?:(?P<aws_secret>{AWS_SECRET_LABEL})|(?P<aws_session>{AWS_SESSION_LABEL})"
    rf"|{GENERIC_SECRET_LABEL}){LABEL_SEPARATOR})(?P<value>{LABEL_VALUE_OR_QUOTED})"
)

#: The AWS labels with no separator at all (``aws configure set
#: aws_secret_access_key <key>``, ``--aws-secret-access-key <key>``, ``secret
#: access key is <key>``), with the value in brackets (``key: <value>``) or in
#: the next cell of a Markdown table (``| AWS_SECRET_ACCESS_KEY | <key> |``). The
#: label alone is weak evidence here, so the value must have the secret's own
#: shape: 40 base64 characters for the key, 100 or more for a session token.
_AWS_SHAPED_RE = re.compile(
    rf"(?P<label>(?:{AWS_SECRET_LABEL}|(?P<session>{AWS_SESSION_LABEL}))"
    r"""["'>`*]{0,3}\s{0,8}(?:(?i:is|was)\s|[:=|])?\s{0,8}["'<{(\[`*]{0,3})"""
    r"(?P<value>(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40}(?![A-Za-z0-9+/=])"
    r"|(?<![A-Za-z0-9+/=])[A-Za-z0-9+/=]{100,})"
)

#: A field whose NAME is a secret label and whose value sits in a sibling field:
#: ``{"name": "AWS_SECRET_ACCESS_KEY", "value": "..."}`` (container and task
#: environments) and its YAML form.
_NAME_VALUE_RE = re.compile(
    r"""(?i:name)["']?\s{0,8}[:=]\s{0,8}["']?"""
    rf"(?P<name>[A-Za-z0-9_.-]{{0,40}}(?:{AWS_SECRET_LABEL}|{AWS_SESSION_LABEL}|{GENERIC_SECRET_LABEL}))"
    r"""["']?\s{0,8},?\s{0,16}(?:-\s{1,8})?"""
    r"""(?P<label>["']?(?i:value)["']?\s{0,8}[:=]\s{0,8}["']?)"""
    rf"(?P<value>{LABEL_VALUE_OR_QUOTED})"
)

#: The labels whose value is a credential whatever its shape: the AWS secret
#: access key and session token, as plain patterns for a scan that reads only
#: ``get_credential_patterns()``.
FIXED_LABEL_RE = re.compile(
    rf"(?:{AWS_SECRET_LABEL}|{AWS_SESSION_LABEL}){LABEL_SEPARATOR}(?![$%{{<*])"
    r"""[^\s"'`,;&{}()<>\[\]\\]{8,}"""
)

_AUTHORIZATION_RE = re.compile(rf"(?P<label>{AUTHORIZATION_LABEL})(?P<value>{AUTHORIZATION_VALUE})")

#: A signing value behind its label, in any serialisation (``"X-Amz-Signature":
#: "..."``, ``X-Amz-Security-Token: ...``), and a bare ``Signature`` field.
_SIGNING_RE = re.compile(
    rf"(?P<label>(?:(?P<named>{SIGNING_LABEL})|{BARE_SIGNATURE_LABEL})"
    r"""["']?\s{0,8}[:=]\s{0,8}["']?)"""
    rf"(?P<value>{LABEL_VALUE_OR_QUOTED})"
)

#: Words that, before ``_token``, name a token that is not a credential: a page
#: cursor, a form's anti-forgery nonce, a count.
_NOT_A_SECRET_TOKEN = frozenset(
    {"next", "page", "pagination", "continuation", "cursor", "csrf", "xsrf", "sync", "resume"}
    | {"max", "num", "total", "count", "the", "a", "end", "start", "stop", "sort", "search"}
)


def _names_no_secret(label: str) -> bool:
    """A ``*_token`` label whose word before it names a cursor or a nonce."""
    words = [w for w in re.split(r"[^a-z0-9]+", label.lower()) if w]
    # The text before a label can end mid-word; only the last two words count.
    return len(words) >= 2 and words[-1] == "token" and words[-2] in _NOT_A_SECRET_TOKEN


def _in_arn(text: str, pos: int) -> bool:
    """A label word right after a ``:`` of an ARN (``arn:...:secret:name``) is a
    resource type; after ``$env:`` or any other colon it is still a label."""
    if pos == 0 or text[pos - 1] != ":":
        return False
    token = text[max(0, pos - 256) : pos].split()
    return bool(token) and "arn:" in token[-1].lower()


#: The longest quoted value read through to its closing quote.
QUOTED_MAX = credential_values.QUOTED_MAX


def _labelled_value(text: str, m: re.Match[str]) -> tuple[str, int, str]:
    """``(value, end, quote)``: a quoted literal runs to its closing quote
    (:func:`credential_values.value_extent`), never shorter than the unquoted run."""
    value, end, start = m["value"], m.end("value"), m.start("value")
    quote = m["label"][-1] if m["label"][-1] in "\"'" else ""
    if quote:
        close = credential_values.value_extent(text, start, quote)
        if close > end:
            value, end = text[start:close], close
    return value, end, quote


def _is_caption(value: str, label: str, quote: str) -> bool:
    """Whether an UNQUOTED *value* is only its label's own word again, in any
    case, with at most a trailing colon (``<label for="password">Password:</label>``):
    a caption that names the field. A quoted value is a literal (``"Password!"``)."""
    if quote:
        return False
    word = value[:-1] if value.endswith(":") else value
    return word.isalpha() and word.lower() == "".join(c for c in label if c.isalpha()).lower()


def _value_is_secret(
    text: str, value: str, end: int, quote: str, label: str, *, aws: bool = False
) -> bool:
    """:func:`credential_values.labelled_value_is_secret` for a value ending at
    *end* behind *label*; a quoted value behind a weak label keeps the length and
    word tests."""
    if _is_caption(value, label, quote):
        return False
    next_char = text[end] if end < len(text) else ""
    weak = not aws and credential_values.is_weak_label(label)
    return credential_values.labelled_value_is_secret(
        value, quoted=bool(quote), next_char=next_char, aws=aws, weak=weak
    )


def fixed_label_patterns() -> list[re.Pattern[str]]:
    """The AWS label and ``Authorization`` patterns, for scans that only search."""
    return [FIXED_LABEL_RE, _AUTHORIZATION_RE]


_AWS_SECRET_NAME_RE = re.compile(rf"{AWS_SECRET_LABEL}\Z")
_AWS_SESSION_NAME_RE = re.compile(rf"{AWS_SESSION_LABEL}\Z")


def _name_rule(name: str) -> str:
    """The rule a ``name``/``value`` pair takes from its name."""
    if _AWS_SECRET_NAME_RE.search(name):
        return "aws_secret_access_key"
    if _AWS_SESSION_NAME_RE.search(name):
        return "aws_session_token"
    return "secret_label"


def label_spans(text: str) -> list[LabelSpan]:
    """Every labelled secret value in *text*, sorted by start and disjoint."""
    found: list[LabelSpan] = []
    for m in _SIGNING_RE.finditer(text):
        value, end, quote = _labelled_value(text, m)
        if not value or credential_values.is_tag(value):
            continue
        # A named signing value goes whatever its shape; a bare ``Signature``
        # is judged like any labelled value (``<p>Signature: Jane</p>`` stays).
        if m["named"] is None and not _value_is_secret(text, value, end, quote, m["label"]):
            continue
        rule = "aws_session_token" if "token" in m["label"].lower() else "request_signature"
        found.append(LabelSpan(m.start("value"), end, rule, m["label"]))
    for m in _AUTHORIZATION_RE.finditer(text):
        found.append(
            LabelSpan(m.start("value"), m.end("value"), "authorization_header", m["label"])
        )
    for m in _LABELLED_RE.finditer(text):
        value, end, quote = _labelled_value(text, m)
        aws = m["aws_secret"] is not None or m["aws_session"] is not None
        if not _value_is_secret(text, value, end, quote, m["label"], aws=aws):
            continue
        if aws:
            rule = "aws_secret_access_key" if m["aws_secret"] is not None else "aws_session_token"
        elif (
            # Inside an ARN (``...:secret:name``) the word is a resource type.
            not _in_arn(text, m.start())
            and not _names_no_secret(text[max(0, m.start() - 40) : m.end("label")])
        ):
            rule = "secret_label"
        else:
            continue
        found.append(LabelSpan(m.start("value"), end, rule, m["label"]))
    for m in _AWS_SHAPED_RE.finditer(text):
        if _secret_shaped(m["value"]) or (m["session"] is not None and len(m["value"]) >= 100):
            rule = "aws_session_token" if m["session"] is not None else "aws_secret_access_key"
            found.append(LabelSpan(m.start("value"), m.end("value"), rule, m["label"]))
    for m in _NAME_VALUE_RE.finditer(text):
        value, end, quote = _labelled_value(text, m)
        # The sibling name carries the label: an AWS name keeps its AWS rule
        # (and its blocking tier), as the same label written inline would.
        rule = _name_rule(m["name"])
        aws = rule != "secret_label"
        if _value_is_secret(text, value, end, quote, m["name"], aws=aws):
            found.append(LabelSpan(m.start("value"), end, rule, m["label"]))
    # A label that sits inside an earlier match's value is part of that value,
    # not a label: judge overlap from where each label starts.
    found.sort(key=lambda span: span.start - len(span.label))
    disjoint: list[LabelSpan] = []
    for span in found:
        if disjoint and span.start - len(span.label) < disjoint[-1].end:
            continue
        disjoint.append(span)
    return disjoint


#: A keyword every context rule's match starts at: a label, a key ID prefix or
#: a URI scheme.
_CONTEXT_HINT_RE = re.compile(
    rf"{AWS_SECRET_LABEL}|{AWS_SESSION_LABEL}|{GENERIC_SECRET_LABEL}|{SIGNING_LABEL}"
    r"|(?i:signature|authorization|name)|AKIA|ASIA|ABIA|ACCA|://"
)


def has_context_hint(text: str) -> bool:
    """Whether *text* holds a keyword some context rule's match starts at."""
    return _CONTEXT_HINT_RE.search(text) is not None


# ── URI passwords with a slash ──
# The fixed URI-userinfo pattern stops a password at ``/`` so it never runs past
# the authority into the path. A password may still carry an unencoded ``/`` (a
# cloud secret key used as a database password does), so this rule reads a
# password that holds one, up to the first ``@`` that is followed by a host.
#: A value that opens with digits and then ``/`` may be a port and a path
#: (``localhost:5173/@vite/client``, the ``@`` in the path), so it is read as a
#: password only when what follows its ``@`` is plainly a host: a dotted name or
#: a name with a port (``app:2024/Pw@db.internal:5432``).
_URI_VALUE = r"[^\s/@]{0,256}/[^\s@]{0,256}?"
_URI_PLAIN_HOST = r"[A-Za-z0-9-]+(?:(?:\.[A-Za-z0-9-]+)+(?::[0-9]{1,5})?|:[0-9]{1,5})"
_URI_SLASH_PASSWORD_RE = re.compile(
    r"(?<![A-Za-z0-9+.-])(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|rediss?|amqps?|https?|ftps?)"
    rf"://[^\s:/@]{{0,128}}:(?P<value>(?![0-9]{{1,5}}/){_URI_VALUE}"
    rf"|(?=[0-9]{{1,5}}/){_URI_VALUE}(?=@{_URI_PLAIN_HOST}(?:[/?#\s\"'>]|\Z)))@"
    r"(?=[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*(?::[0-9]{1,5})?(?:[/?#\s\"'>]|\Z))"
)


def uri_password_spans(text: str) -> list[LabelSpan]:
    """Passwords holding a ``/`` in ``scheme://user:password@host`` URIs."""
    if "://" not in text:
        return []
    return [
        LabelSpan(m.start("value"), m.end("value"), "url_userinfo", "")
        for m in _URI_SLASH_PASSWORD_RE.finditer(text)
    ]


# ── Key-ID pairing ──
#: Any AWS key ID a secret can be paired with. Only an upper-case letter or a
#: digit before it is part of a longer token; a lower-case word glued in front
#: (``key<ID>``) still leaves it a key ID.
_KEY_ID_RE = re.compile(rf"(?<![A-Z0-9])(?:{AWS_KEY_ID}|{AWS_KEY_ID_REDACTION})(?![A-Za-z0-9])")

#: A run of the base64 alphabet a secret access key can sit in.
_PAIR_RUN_RE = re.compile(r"[A-Za-z0-9+/=]{40,}")

#: How far from a key ID its secret may sit: the width of a CSV row, a JSON
#: object or an ``export`` pair of lines, in characters, on either side.
_PAIR_REACH = 256

#: The length of a secret access key.
_SECRET_LEN = 40


def _secret_shaped(value: str) -> bool:
    """A 40-character value that can be a secret key: mixed case or symbols, not hex."""
    if is_identifier_value(value):
        return False
    # The secret key's alphabet is base64 without padding; a digit is common
    # but not required.
    if "=" in value:
        return False
    return any(c.islower() for c in value) and any(c.isupper() for c in value)


def _secret_key_piece(run: str) -> tuple[int, int] | None:
    """Where a base64 run holds a 40-character secret-shaped key, as offsets in
    *run*: the whole run, or a 40-character piece between its edges and ``/``,
    ``+``, ``=``. ``None`` when it holds none."""
    # A key may itself start or end with ``/`` or ``+``: judge the run as written
    # before reading the separators off its edges.
    if len(run) == _SECRET_LEN and _secret_shaped(run):
        return 0, _SECRET_LEN
    # A key holds no ``=`` (40 base64 characters of 30 bytes, unpadded), so only
    # ``=`` is read off the run's edges; a ``/`` or ``+`` there may be the key's.
    lead = len(run) - len(run.lstrip("="))
    core = run.strip("=")
    if len(core) == _SECRET_LEN:
        return (lead, lead + _SECRET_LEN) if _secret_shaped(core) else None
    cuts = [0, *(i + 1 for i, c in enumerate(core) if c in "/+="), len(core)]
    edges = set(cuts) | {c - 1 for c in cuts if c > 0}
    # A piece that starts on a ``/`` or ``+`` is tried after the one starting
    # just past it: the separator is more often the path's than the key's.
    for a in sorted(cuts, key=lambda cut: (core[cut : cut + 1] in ("/", "+"), cut)):
        piece = core[a : a + _SECRET_LEN]
        if len(piece) == _SECRET_LEN and a + _SECRET_LEN in edges and _secret_shaped(piece):
            return _with_own_edge(run, lead + a, lead + a + _SECRET_LEN)
    for b in cuts:
        a = b - _SECRET_LEN
        if a >= 0 and (a in edges or a + 1 in edges) and _secret_shaped(core[a:b]):
            return _with_own_edge(run, lead + a, lead + b)
    return None


def _with_own_edge(run: str, a: int, b: int) -> tuple[int, int]:
    """A 40-character piece ending one short of a cut also takes that last
    ``/`` or ``+`` when the run ends there: a key may end with one, and the run
    stripped it before the piece was read (``x=<key ending in +>``)."""
    if b == len(run) - 1 and run[b] in "/+":
        b += 1
    return a, b


def _holds_secret_key(run: str) -> bool:
    """Whether a base64 run holds a secret-shaped key (:func:`_secret_key_piece`)."""
    return _secret_key_piece(run) is not None


def paired_secret_spans(text: str, units: list[tuple[int, int]] | None = None) -> list[LabelSpan]:
    """Secret keys within reach of an AWS access key ID, in either order and
    behind any delimiter.

    The key IDs are blanked out of a working copy, so a run that touches one ends
    there; every base64 run within :data:`_PAIR_REACH` of a key ID that holds a
    secret-shaped 40-character key has that key redacted. The key ID itself is never
    in a span: it is an Identifier. *units*, when given, receives each pair as
    one span, key ID and secret.
    """
    if not any(prefix in text for prefix in _KEY_ID_PREFIXES):
        return []
    keys = [m.span() for m in _KEY_ID_RE.finditer(text)]
    if not keys:
        return []
    work = list(text)
    for start, end in keys:
        work[start:end] = " " * (end - start)
    blanked = "".join(work)
    key_starts = [start for start, _ in keys]
    found: list[LabelSpan] = []
    for m in _PAIR_RUN_RE.finditer(blanked):
        # The nearest key ID on either side, found by bisection.
        i = bisect_left(key_starts, m.end())
        near = [keys[j] for j in (i - 1, i) if 0 <= j < len(keys)]
        near = [k for k in near if m.start() < k[1] + _PAIR_REACH and k[0] - _PAIR_REACH < m.end()]
        piece = _secret_key_piece(m.group()) if near else None
        if piece is None:
            continue
        # Only the key itself goes: the label or path text glued to it stays.
        start, end = m.start() + piece[0], m.start() + piece[1]
        found.append(LabelSpan(start, end, "aws_key_pair", ""))
        if units is not None:
            key = near[0]
            units.append((min(key[0], start), max(key[1], end)))
    return found


# ── Stream holdback ──
# The stream redactor emits text in pieces, and a context rule needs its whole
# match in one batch call. Every context rule declares where its match starts
# (its trigger) and how far it can reach; the stream holds from the earliest
# trigger whose match may still be arriving, on the raw text and on its
# normalised view. One table, so a new rule adds a row, not a predicate.

#: How far a key ID's pairing reaches: a 40-character key within the reach.
_PAIR_HOLD_REACH = _PAIR_REACH + _SECRET_LEN + 1

#: How far back a quoted value's label may sit: the label and separator, the
#: longest quoted value the batch rule reads, its closing quote and what follows.
QUOTED_HOLD_REACH = 96 + QUOTED_MAX + 64

#: How far a ``name`` field reaches to its sibling ``value``.
_NAME_HOLD_REACH = 160

#: How far back a vendor-catalog value still arriving may start its keyword.
CATALOG_HOLD_REACH = CATALOG_ARRIVING_REACH

#: The longest tail the hold reads.
_HOLD_TAIL = max(_PAIR_HOLD_REACH, QUOTED_HOLD_REACH)

_KEY_ID_PREFIXES = ("AKIA", "ASIA", "ABIA", "ACCA")

#: Every label the batch rules read, at the start of its label and separator:
#: a separator is ``=``, ``:``, ``=>`` and the like, ``is``/``was``, or blanks.
_LABEL_TRIGGER_RE = re.compile(
    rf"(?:{AWS_SECRET_LABEL}|{AWS_SESSION_LABEL}|{GENERIC_SECRET_LABEL}|{SIGNING_LABEL}"
    rf"|{BARE_SIGNATURE_LABEL}|(?i:(?:proxy-)?authorization))"
    r"""(?P<sep>["'*_\\>`]{0,6}\s{0,8}(?:(?:=>|:=|[:=>|\uff1a\uff1d]|(?i:is|was)\s)\s{0,8})?"""
    r"""(?:(?i:bearer|basic|token)\s{1,8})?[*_\\<{(\[]{0,3}(?P<quote>["']?))"""
)


#: A multi-word label still arriving at the buffer end (``api ke``, ``secret
#: acc``), built from the same word table as the labels themselves.
def _prefixes(word: str, tail: str = "") -> str:
    """A regex for any prefix of *word*, empty included; *tail* may follow it whole."""
    pattern = tail
    for char in reversed(word):
        pattern = f"(?:{re.escape(char)}{pattern})?"
    return pattern


def _partial_label(words: tuple[str, ...]) -> str:
    """A regex for a multi-word label cut anywhere after its first word."""
    tail = ""
    for word in reversed(words[1:]):
        tail = f"(?:[ _.-]?{_prefixes(word, tail)})?"
    return re.escape(words[0]) + tail


_PARTIAL_LABEL_RE = re.compile(
    "(?<![A-Za-z])(?i:"
    + "|".join(
        _partial_label(words)
        for words in (*AWS_LABEL_WORDS, *GENERIC_LABEL_WORDS)
        if len(words) > 1
    )
    + r")\Z"
)
_NAME_TRIGGER_RE = re.compile(r"""(?i:name)["']?\s{0,8}[:=]""")
_UNQUOTED_RUN_RE = re.compile(r"""[^\s"'`,;&{}()<>\[\]\\]*""")
#: A base64 block wrapped over lines, as PEM, MIME and ``base64`` write it.
_B64_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
_B64_RUN_RE = re.compile(r"[A-Za-z0-9+/=]*")
_LINE_BREAK_RE = re.compile(r"\r?\n|\\n")


def _wrapped_block_start(buf: str, lo: int) -> int | None:
    """Where a base64 block wrapped over lines (by newlines or literal ``\\n``)
    starts, when it reaches the end of *buf*; one pass over the tail's lines."""
    spans: list[tuple[int, int]] = []
    pos = lo
    for brk in _LINE_BREAK_RE.finditer(buf, lo):
        spans.append((pos, brk.start()))
        pos = brk.end()
    spans.append((pos, len(buf)))
    last_start, last_end = spans[-1]
    if len(spans) < 2 or _B64_RUN_RE.fullmatch(buf, last_start, last_end) is None:
        return None
    start = None
    for line_start, line_end in reversed(spans[:-1]):
        if line_end - line_start >= 16 and _B64_RUN_RE.fullmatch(buf, line_start, line_end):
            start = line_start
            continue
        # The block's first line may share its line with prose before it.
        run = line_end
        while run > line_start and buf[run - 1] in _B64_CHARS:
            run -= 1
        if line_end - run >= 16:
            start = run
        break
    return start


def _label_still_arriving(buf: str, m: re.Match[str]) -> bool:
    """Whether label match *m*'s value may still be arriving: no value yet, or a
    value that runs to the buffer end (a quoted one, to its unclosed quote)."""
    start = m.end()
    if start >= len(buf):
        return True
    if m["quote"]:
        end = credential_values.value_extent(buf, start, m["quote"])
        if end >= len(buf):
            return True
        if end - start >= credential_values.QUOTED_READ_MAX and buf[end] not in (
            m["quote"],
            "\n",
        ):
            # Still open past the bound the batch rule reads a value to: the
            # caller's reach (:func:`quoted_hold_reach`) decides how far to hold.
            return True
        # Just closed (or past the bound) with only a short non-blank run after
        # it: the stream's own cut would otherwise land inside the value.
        rest = buf[end + (1 if buf[end] in (m["quote"], "\n") else 0) :]
        return len(rest) <= 64 and not any(c.isspace() for c in rest)
    run = _UNQUOTED_RUN_RE.match(buf, start)
    return run is not None and run.end() >= len(buf)


def stream_hold_start(buf: str) -> int | None:
    """:func:`_hold_start` on the buffer and on its normalised view, the earlier.

    The batch pass also reads labels through escapes and terminal codes
    (``{\\"password\\": ...}``), so the hold looks through them too and maps the
    view's position back to the buffer.
    """
    hold = _hold_start(buf)
    lo = max(0, len(buf) - 2 * _HOLD_TAIL)
    for view in normalised_views(buf[lo:]):
        seen = _hold_start(view.text)
        if seen is not None and seen < len(view.text):
            start = lo + view.starts[seen]
            hold = start if hold is None else min(hold, start)
    return hold


def open_quoted_start(buf: str) -> int | None:
    """Where a labelled quoted value still open at the buffer's end starts its
    label, looking back :data:`QUOTED_HOLD_REACH`; ``None`` when there is none.

    The stream holds from here until the closing quote, past its usual 512
    floor up to that reach, so the value is redacted beside its label.
    """
    lo = max(0, len(buf) - QUOTED_VALUE_HOLD_REACH)
    found = _open_quoted_start(buf, lo)
    # Through escapes and terminal codes too (``{\\"SessionToken\\": \\"...``):
    # the batch pass reads a label in the normalised view, so the hold does.
    for view in normalised_views(buf[lo:]):
        seen = _open_quoted_start(view.text, 0)
        if seen is not None and seen < len(view.text):
            start = lo + view.starts[seen]
            found = start if found is None else min(found, start)
    return found


#: How far a labelled quoted value is held while its quote is open: the label
#: and separator, the longest quoted value every reader removes whole
#: (``QUOTED_READ_MAX``: a session token, a long password), its closing quote and
#: what follows. Bounded, and only while a quote is open after a secret label.
QUOTED_VALUE_HOLD_REACH = 96 + credential_values.QUOTED_READ_MAX + 64


def quoted_hold_reach(buf: str, start: int) -> int:
    """How far a quoted value whose label starts at *start* is held."""
    return QUOTED_VALUE_HOLD_REACH


def _open_quoted_start(buf: str, lo: int) -> int | None:
    for m in _LABEL_TRIGGER_RE.finditer(buf, lo):
        if len(buf) - m.start() > quoted_hold_reach(buf, m.start()):
            continue  # past its own reach: released, as the batch rule bounds it
        if m["quote"] and _label_still_arriving(buf, m):
            return m.start()
    # A ``name``/``value`` pair: the label is the sibling ``name`` field, so the
    # hold starts there and lasts until the quoted value closes.
    for m in _NAME_VALUE_ARRIVING_RE.finditer(buf, lo):
        if m["quote"] and _label_still_arriving(buf, m):
            return m.start()
    return None


def overrun_quoted_value(buf: str, reach: int) -> tuple[int, str] | None:
    """``(value_start, quote)`` of a labelled quoted value still open at the
    buffer end once the label and value span *reach* characters (the stream's
    long-token bound), on the text or a normalised view, with ``quote`` given as
    ``\\"`` when the view decoded an escaped quote; ``None`` when there is none.
    The stream drops such a value through its closing quote instead of releasing it."""
    found = _overrun_quoted_value(buf, reach)
    for view in normalised_views(buf):
        seen = _overrun_quoted_value(view.text, reach)
        if seen is not None and seen[0] < len(view.text):
            start = view.starts[seen[0]]
            quote = seen[1]
            if found is None or start < found[0]:
                found = (start, quote)
    if found is not None:
        start, quote = found
        encoded = buf[max(0, start - 3) : start].upper()
        if encoded in ("%22", "%27"):
            # Opened by a percent-encoded quote: the drop starts at it and ends
            # through the same encoded quote, as the batch rule's span does.
            return start - 3, encoded
        depth = len(buf[: start - 1]) - len(buf[: start - 1].rstrip("\\"))
        if buf[start - 1 : start] == quote and depth:
            # Opened by an escaped quote (``\\"``, ``\\\\\\"`` one JSON level
            # deeper): it ends at the same escaped quote in the source.
            found = (start, "\\" * depth + quote)
    return found


def _overrun_quoted_value(buf: str, reach: int) -> tuple[int, str] | None:
    for regex in (_LABEL_TRIGGER_RE, _NAME_VALUE_ARRIVING_RE):
        for m in regex.finditer(buf):
            if not m["quote"] or len(buf) - m.start() < reach:
                continue
            # Closed by the buffer's last character counts too: nothing after it
            # tells the long-token cap the value ended, and it would drop the label.
            if credential_values.value_extent(buf, m.end(), m["quote"]) >= len(buf) - 1:
                return m.end(), m["quote"]
    return None


def label_ends_near(buf: str, cut: int) -> bool:
    """Whether a label (with its separator) ends within a few characters before
    *cut*, on the text or through escapes: a cut there may part it from its value."""
    lo = max(0, cut - 160)
    window = buf[lo : cut + 1]
    for m in _LABEL_TRIGGER_RE.finditer(window):
        if len(window) - 1 - m.end() <= 8:
            return True
    return "\x1b" in window or "\\" in window[-16:]


def open_view_value(buf: str, tail: int) -> tuple[int, int] | None:
    """``(label_start, value_start)`` of an unquoted labelled value the batch pass
    reads only in a normalised view of *buf*'s last *tail* characters
    (``password%3D<value>``) and that still runs to the buffer end, as source
    offsets; ``None`` when there is none. The stream holds such a value with its
    label, as it holds a quoted one."""
    lo = max(0, len(buf) - tail)
    found: tuple[int, int] | None = None
    for view in normalised_views(buf[lo:]):
        for span in label_spans(view.text):
            if span.end < len(view.text) or view.text[span.start - 1 : span.start] in "\"'":
                continue
            label_start = max(0, span.start - len(span.label))
            source_label, _ = view.source_span(label_start, span.start)
            source_value, _ = view.source_span(span.start, span.end)
            if found is None or lo + source_label < found[0]:
                found = (lo + source_label, lo + source_value)
    return found


def view_label_spans(buf: str, tail: int | None = None) -> list[tuple[int, int]]:
    """Labelled values the batch pass finds only in a normalised view of *buf*'s
    last *tail* characters (the hold's tail by default), from the LABEL's start
    to the value's end, as source offsets: a stream cut inside one parts the
    label from its value."""
    lo = max(0, len(buf) - (_HOLD_TAIL if tail is None else tail))
    spans: list[tuple[int, int]] = []
    for view in normalised_views(buf[lo:]):
        for span in label_spans(view.text):
            start = max(0, span.start - len(span.label))
            source_start, source_end = view.source_span(start, span.end)
            spans.append((lo + source_start, lo + source_end))
    return spans


#: A ``name``/``value`` pair (:data:`_NAME_VALUE_RE`) up to where its value
#: opens, with the value's opening quote as group ``quote``.
_NAME_VALUE_ARRIVING_RE = re.compile(
    r"""(?i:name)["']?\s{0,8}[:=]\s{0,8}["']?"""
    rf"[A-Za-z0-9_.-]{{0,40}}(?:{AWS_SECRET_LABEL}|{AWS_SESSION_LABEL}|{GENERIC_SECRET_LABEL})"
    r"""["']?\s{0,8},?\s{0,16}(?:-\s{1,8})?["']?(?i:value)["']?\s{0,8}[:=]\s{0,8}"""
    r"""(?P<quote>["'])"""
)


def _hold_start(buf: str) -> int | None:
    """Where the stream must hold from so every context rule sees its whole match.

    The earliest trigger whose reach has not fully arrived: a label whose value
    may still be arriving (:func:`_label_still_arriving`, at most
    :data:`QUOTED_HOLD_REACH`), a ``name`` field, a key ID or a base64 run that
    holds a key (either may be the first of a pair), a wrapped base64 block
    reaching the end, and a vendor-catalog keyword. ``None`` when nothing needs
    holding. Reads only the buffer's tail, so it costs the same however long
    the buffer is.
    """
    lo = max(0, len(buf) - _HOLD_TAIL)
    starts: list[int] = []
    for m in _LABEL_TRIGGER_RE.finditer(buf, max(0, len(buf) - QUOTED_HOLD_REACH)):
        if _label_still_arriving(buf, m):
            starts.append(m.start())
            break
    partial = _PARTIAL_LABEL_RE.search(buf, max(0, len(buf) - 24))
    if partial is not None:
        starts.append(partial.start())
    for m in _NAME_TRIGGER_RE.finditer(buf, max(0, len(buf) - _NAME_HOLD_REACH)):
        starts.append(m.start())
        break
    if any(prefix in buf[lo:] for prefix in _KEY_ID_PREFIXES):
        for key in _KEY_ID_RE.finditer(buf, lo):
            if len(buf) - key.end() < _PAIR_HOLD_REACH:
                starts.append(key.start())
                break
    for run in _PAIR_RUN_RE.finditer(buf, lo):
        if len(buf) - run.end() < _PAIR_HOLD_REACH and _holds_secret_key(run.group()):
            starts.append(run.start())
            break
    wrapped = _wrapped_block_start(buf, lo)
    if wrapped is not None:
        starts.append(wrapped)
    keyword = long_token_start(buf)
    if keyword is not None:
        starts.append(keyword)
    return min(starts) if starts else None


#: An AWS STS session token whose run reaches the buffer end: still arriving.
_STS_ARRIVING_RE = re.compile(
    r"(?<![A-Za-z0-9+/])(?:IQoJb3JpZ2lu|FwoGZXIvYXdz)[A-Za-z0-9+/]{0,4096}={0,2}\Z"
)


def long_token_start(buf: str) -> int | None:
    """Where a token that may run past the stream's 512 floor is still arriving:
    a vendor-catalog keyword's value (:func:`catalog_keyword_start`) or an STS
    session token. The stream holds from here as far as
    :data:`CATALOG_HOLD_REACH`, so the token reaches one batch call whole."""
    found = catalog_keyword_start(buf)
    sts = _STS_ARRIVING_RE.search(buf, max(0, len(buf) - CATALOG_HOLD_REACH))
    if sts is not None and (found is None or sts.start() < found):
        found = sts.start()
    return found
