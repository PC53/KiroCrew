"""Whether a value behind a secret label is a secret: the one shared predicate.

The batch redactor (:mod:`kiro_crew.security.redaction_labels`) and the log
filter (:mod:`kiro_crew.log_redaction`) both read a labelled value here, so the
two cannot drift apart. A value is a secret unless the WHOLE value is on a
short exemption list; nothing here guesses from a value's first characters or
from the code around it. Imports only the standard library and
:mod:`kiro_crew.credential_patterns`, so the log filter stays an import leaf.
"""

from __future__ import annotations

import re

from kiro_crew.credential_patterns import (
    AWS_KEY_ID_BODY,
    AWS_KEY_ID_PREFIXES,
    AWS_KEY_ID_REDACTION_ONLY_PREFIXES,
    REFERENCE_VALUE,
)

#: The shortest value a label rule redacts. Shorter values are flags, units and
#: words (``password: no``), not secrets.
MIN_VALUE_LEN = 8

#: How much of a quoted value the stream's window around a label covers: the
#: reach the view passes and the stream's tail are sized by.
QUOTED_MAX = 512

#: The longest quoted value read through to its closing quote, in every reader:
#: a long one (a session token, a password with commas) is removed whole.
QUOTED_READ_MAX = 4096

#: A one-case run of letters this long is a passphrase, not a word.
WORD_LEN_MAX = 15

_REFERENCE_RE = re.compile(REFERENCE_VALUE)

#: A whole redaction tag (``[REDACTED: credential]``): a fixed point, not a value.
#: Only the tags the redactors themselves write: any other text inside the
#: brackets is the writer's own, and may be a secret dressed as a tag.
_TAG_RE = re.compile(
    r"\[REDACTED(?:"
    r": (?:credential|encoded credential|policy request header"
    r"|unscannable JSON payload|suspicious URL)"
    r"| PRIVATE KEY|-(?:ACCESS|REFRESH|SSO))?\]"
)

#: A secret-store reference (``secret://name``, ``vault://path``, ``env:NAME``):
#: it names where a secret lives, and is not one.
_STORE_REFERENCE_RE = re.compile(r"(?i:[a-z][a-z0-9+.-]{1,15}://|(?:env|ssm|op|keychain):)")

#: A value that can only NAME something: an AWS key ID, a 12-digit account ID,
#: an ARN. Identifier tier, so it is never redacted behind a label.
NAMING_VALUE = (
    rf"(?:{AWS_KEY_ID_PREFIXES}|{AWS_KEY_ID_REDACTION_ONLY_PREFIXES}){AWS_KEY_ID_BODY}"
    r"|[0-9]{12}|arn:[a-z0-9-]+:\S*"
)
_NAMING_RE = re.compile(NAMING_VALUE, re.ASCII)


def value_extent(text: str, start: int, quote: str) -> int:
    """End of a value opened by *quote* at *start*: its unescaped closing quote.

    The extent depends on the quote state only. A value still open at the line
    end or the text end ends there, so it is removed whole up to that point
    rather than cut at its first delimiter. Past :data:`QUOTED_READ_MAX`
    characters the rest is read in one pass to the closing quote or the line
    end: a long value is never cut short and its tail released in the clear.
    """
    pos = start
    limit = min(len(text), start + QUOTED_READ_MAX)
    while pos < limit:
        char = text[pos]
        if char == "\\":
            pos += 2
            continue
        if char == quote or char == "\n":
            return pos
        pos += 1
    if pos >= len(text):
        return len(text)
    rest = _QUOTED_REST_RE[quote].match(text, pos)
    return rest.end() if rest is not None else pos


#: The rest of a quoted value past :data:`QUOTED_READ_MAX`: escapes and any byte
#: but its quote or a line break.
_QUOTED_REST_RE = {q: re.compile(rf"(?:[^{q}\\\n]|\\.)*") for q in ('"', "'", "`")}


def is_tag(value: str) -> bool:
    """Whether *value* is a whole redaction tag."""
    return _TAG_RE.fullmatch(value) is not None


def without_tags(text: str, start: int, end: int) -> str:
    """``text[start:end]`` with every whole redaction tag that starts in it
    removed: what is left is the writer's own text, a forged tag included."""
    out: list[str] = []
    pos = start
    for tag in _TAG_RE.finditer(text, start, len(text)):
        if tag.start() >= end:
            break
        out.append(text[pos : tag.start()])
        pos = max(pos, tag.end())
    if pos < end:
        out.append(text[pos:end])
    return "".join(out)


def is_naming_value(value: str) -> bool:
    """True when the WHOLE value is a key ID, an account ID or an ARN: names only."""
    return _NAMING_RE.fullmatch(value) is not None


def _is_dotted_name(value: str) -> bool:
    """A chain of two or more names of letters and ``_`` (``settings.aws_key``)."""
    parts = value.split(".")
    return len(parts) > 1 and all(p and p.isascii() and p.replace("_", "").isalpha() for p in parts)


#: Labels whose word also names something that is not a secret: ``pwd`` (a
#: working directory, a shell command), a glued ``_pass`` / ``_token`` and a bare
#: ``signature``. A quoted value behind one keeps the length and word
#: exemptions (``{"bash": {"pwd": "deny"}}``); behind a strong label it does not.
_WEAK_LABEL_RE = re.compile(r"(?i:pwd|[_.-]pass|[_.-]token|signature)[\"'*_\s:=>\uff1a\uff1d\\]*\Z")


#: The AWS labels that end like a weak one (``aws_session_token``): never weak.
_AWS_TAIL_RE = re.compile(r"(?i:(?:session|security)[ _.-]?token)[\"'*_\s:=>\uff1a\uff1d\\]*\Z")


def is_weak_label(label: str) -> bool:
    """Whether *label* (with its separator) is a weak label (:data:`_WEAK_LABEL_RE`);
    an AWS label is never weak."""
    return _WEAK_LABEL_RE.search(label) is not None and _AWS_TAIL_RE.search(label) is None


#: The shortest digit run behind a secret label that is a PIN.
MIN_PIN_LEN = 4

_UNICODE_ESCAPES_RE = re.compile(r"(?:\\u[0-9A-Fa-f]{4})+")

#: A code name that carries a secret label's word (``new_password``,
#: ``password_hash``, ``userApiKey``): lower-case first, letters and ``_`` only,
#: with a ``_`` or a capital inside, so it reads as an identifier.
_LABEL_WORD_NAME_RE = re.compile(r"[a-z_][A-Za-z_]*")
_LABEL_WORDS_RE = re.compile(r"(?i:passw(?:or)?d|passphrase|secret|token|api_?key|pwd)")


def _is_label_word_name(value: str) -> bool:
    """Whether *value*, written unquoted, is a variable named after the secret
    it holds (``password = new_password``): a name for the value, not the value."""
    return (
        _LABEL_WORD_NAME_RE.fullmatch(value) is not None
        and ("_" in value or not value.islower())
        and _LABEL_WORDS_RE.search(value) is not None
    )


def _is_word(value: str) -> bool:
    """A single-case word of letters at most :data:`WORD_LEN_MAX` long."""
    return value.isalpha() and (value.islower() or value.isupper()) and len(value) <= WORD_LEN_MAX


def is_exempt_value(value: str, *, aws: bool = False, quoted: bool = False) -> bool:
    """Whether the WHOLE *value* names or masks a secret instead of holding one.

    The list: shorter than :data:`MIN_VALUE_LEN` (not a PIN of
    :data:`MIN_PIN_LEN` or more digits); a code name carrying the label's word
    (``new_password``, ``userApiKey``); a reference or a mask
    (``$VAR``, ``${VAR}``, ``%VAR%``, ``{{ x }}``, ``<your key>``, ``****``); a
    secret-store reference; a redaction tag; a single-case word of letters no
    longer than :data:`WORD_LEN_MAX` (``password: required``); a dotted chain of
    letter names (``settings.aws_key``); a path (``/srv/app``, ``~/x``, ``./x``).
    Every test is on the whole value. A *quoted* value is a literal, so only a
    reference, a store reference or a tag exempts it, whatever its length.
    """
    if not value or is_tag(value):
        return True
    if _REFERENCE_RE.fullmatch(value) is not None or _STORE_REFERENCE_RE.match(value):
        return True
    if quoted:
        # A quoted value behind a secret label is a literal the writer chose to
        # quote: no length, word, dotted-name or path test applies to it.
        return False
    if len(value) < MIN_VALUE_LEN:
        # A run of four or more digits is a PIN, not a flag or a unit.
        return not (len(value) >= MIN_PIN_LEN and value.isdigit())
    if _is_label_word_name(value):
        return True
    if aws:
        # Behind an AWS secret label the value is the secret whatever it looks
        # like: no word or path exemption.
        return _is_dotted_name(value)
    if _is_word(value):
        return True
    return _is_dotted_name(value) or value.startswith(("/", "~/", "./", "../"))


def labelled_value_is_secret(
    value: str, *, quoted: bool, next_char: str, aws: bool = False, weak: bool = False
) -> bool:
    """Whether a value behind a secret label is a secret.

    It is, unless the whole value is exempt (:func:`is_exempt_value`), it can
    only name something (:func:`is_naming_value`), or, written unquoted, it is
    called or subscripted (*next_char* is ``(`` or ``[``: ``load_key(path)``,
    ``os.environ[...]``). Behind an AWS secret label (*aws*) a value shaped
    like a key ID is still the secret the label names, so it is not vetoed.
    A run of digits is never a name behind a secret label: it can be a PIN or
    a numeric password, and an account ID keeps its own label (``account``).
    """
    if quoted and weak and _UNICODE_ESCAPES_RE.fullmatch(value):
        # A literal written only as ``\\uXXXX`` escapes (``'\\u7eff\\u706f'``) is
        # judged by the characters it spells.
        value = value.encode("ascii").decode("unicode_escape")
    pin = len(value) >= MIN_PIN_LEN and value.isdigit()
    if quoted and weak and not pin and (len(value) < MIN_VALUE_LEN or _is_word(value)):
        # A quoted value behind a weak label keeps ONLY the length and word
        # tests (``{"pwd": "deny"}``); no path or dotted-name test applies.
        return False
    if is_exempt_value(value, aws=aws, quoted=quoted):
        return False
    if not aws and not value.isdigit() and is_naming_value(value):
        return False
    return quoted or next_char not in ("(", "[")
