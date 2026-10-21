"""Recursive-force ``rm`` seatbelt behind the two ``local-destructive-rm-rf-*`` rows.

Refuses the common respellings of ``rm -rf /`` and ``rm -rf ~`` the rows miss, at the
top level and in every nested-shell payload the shared walk finds. A seatbelt, not a
parser: other shell shapes stay with the rows and the OS sandbox.
"""

from __future__ import annotations

import re

from ..trust_patterns import ENV_ASSIGNMENT_RE
from . import shell_normalizer as _shell
from .denied_rules import BUILTIN_DENIED_RULES

_RM_FLOOR_RULE_IDS = {
    "root": "local-destructive-rm-rf-root",
    "home": "local-destructive-rm-rf-home",
}
_RM_FLOOR_BY_KIND: dict[str, tuple[str, str]] = {
    kind: (rule.id, rule.pattern)
    for kind, rule_id in _RM_FLOOR_RULE_IDS.items()
    for rule in BUILTIN_DENIED_RULES
    if rule.id == rule_id
}
_RM_FLOOR_NOTE = "Matched on the rm flags and target, not the pattern text: recursive and force against this directory itself."
_RM_FLOOR_UNREAD_NOTE = "Refused because the rm check could not read this command. Simplify it."
_CAN_FIRE_RE = re.compile(r"r[^\w\s]*m")  # a literal rm, quote splices allowed
_HOME_WORDS = frozenset({"~", "$HOME", "${HOME}"})
_CUT_CHARS = frozenset(";&|\n`()")


def _can_fire(command: str) -> bool:
    return bool(_CAN_FIRE_RE.search(command))


def _target_kind(word: str) -> "str | None":
    """``root`` / ``home`` when *word* is that dir itself, with an optional ``/`` or ``/*``."""
    base = word[:-2] if word.endswith("/*") else word
    base = base.rstrip("/") if base.strip("/") else ""
    if word.startswith("/") and base == "":
        return "root"
    return "home" if base in _HOME_WORDS else None


def _segments(command: str) -> "list[str]":
    """*command* cut at separators the shared quote machine marks live; stops at a heredoc."""
    out: list[str] = []
    seg: list[str] = []
    pos, state = 0, 0
    while pos < len(command):
        text, resume, resume_state = command[pos:], len(command), 0
        for step in _shell._iter_shell_chars(text, state):
            if step.active and text.startswith("<<", step.offset):
                break  # heredoc bodies are data; the rows and the sandbox own them
            if step.active and step.char == "#" and _shell._opens_comment(text, step.offset):
                newline = text.find("\n", step.offset)  # a comment's quotes open nothing
                resume = len(command) if newline < 0 else pos + newline
                break
            if step.state == 2 and text.startswith("$(", step.offset):
                # bash reads "$(…)" from an unquoted start, then the double quote resumes
                body = text[step.offset + 2 :]
                close, proven = _shell._matching_close_paren(body, 0)
                out.extend(_segments(body[: close - 1 if proven else close]))
                seg.append("\0")
                resume, resume_state = pos + step.offset + 2 + close, 2
                break
            if step.active and step.char in _CUT_CHARS:
                out.append("".join(seg))
                seg = []
            elif step.char in "$~" and (
                len(step.text) == 2 or step.state == 1 or (step.char == "~" and step.state)
            ):
                seg.append("\0")  # an escaped, single-quoted $ or any quoted ~ is literal
            else:
                seg.append(step.text)
        pos, state = resume, resume_state
    return [*out, "".join(seg)]


def _rm_wipe_targets(command: str) -> "set[str]":
    """The targets (``root`` / ``home``) a recursive-force ``rm`` in *command* wipes."""
    kinds: set[str] = set()
    for segment in _segments(command):
        words = _shell._shell_tokens(segment)
        while words and ENV_ASSIGNMENT_RE.match(words[0]):
            words = words[1:]
        if not words or _shell._program_basename(words[0]) != "rm":
            continue
        recursive = force = done = False
        operands = []
        for arg in words[1:]:
            if done or not arg.startswith("-") or arg == "-":
                operands.append(arg)
            elif arg == "--":
                done = True
            elif arg.startswith("--"):
                recursive = recursive or arg == "--recursive"
                force = force or arg == "--force"
            else:
                recursive = recursive or "r" in arg or "R" in arg
                force = force or "f" in arg
        if recursive and force:
            kinds |= {kind for arg in operands if (kind := _target_kind(arg))}
    return kinds


def _after_shell_verb(tokens: "list[str]", programs: "list[str]", index: int) -> bool:
    """True when *tokens[index]* is the argument of ``eval`` / ``source`` / ``.``."""
    before = index - 1
    while before >= 0 and tokens[before] == "--":
        before -= 1
    if before < 0 or programs[before] != tokens[before]:
        return False  # a redirect target named ``eval`` is a file, not the verb
    verb = tokens[before]
    return verb in _shell._NESTED_SHELL_VERBS or _shell._program_basename(verb) in (
        _shell._NESTED_SHELL_VERBS
    )


def _command_lines(text: str) -> "list[str]":
    """*text* cut at each live newline; a ``#`` comment's quotes open nothing."""
    lines: list[str] = []
    pos = 0
    while pos <= len(text):
        rest, cut, end = text[pos:], len(text), len(text)
        for step in _shell._iter_shell_chars(rest):
            if step.active and step.char == "#" and _shell._opens_comment(rest, step.offset):
                newline = rest.find("\n", step.offset)  # the line ends before the comment
                cut, end = pos + step.offset, len(text) if newline < 0 else pos + newline
                break
            if step.active and step.char == "\n":
                cut = end = pos + step.offset
                break
        lines.append(text[pos:cut])
        pos = end + 1
    return lines


def _rm_wipe_targets_nested(command: str) -> "set[str]":
    """:func:`_rm_wipe_targets` over *command* and each nested-shell payload in it.

    Payloads come from the shared ``_nested_shell_payloads`` and the shared data-consumer
    exemption (``echo bash -c '...'`` prints), and each is read by this floor's own segment
    scan. A payload is always shorter than its parent, so the walk ends. ``eval`` and
    ``source`` payloads are not read: bash re-joins words under each ``eval``, which this
    seatbelt does not model, so those stay with the rows and the OS sandbox.
    """
    kinds: set[str] = set()
    pending, seen = [command], {command}
    while pending:
        source = pending.pop()
        kinds |= _rm_wipe_targets(source)
        # The tokenizer reads an unquoted newline as a space; bash ends the command there.
        # Cut comments before folding: bash continues no line from inside a comment.
        for line in _command_lines(source):
            tokens = _shell._shell_tokens(_shell._fold_line_continuations(line))
            payloads = _shell._nested_shell_payloads(tokens, allow_join=False)
            if not payloads:
                continue
            programs = _shell._argv_programs(tokens)
            disqualified = _shell._data_consumer_command_disqualified(tokens)
            positions: dict[str, list[int]] = {}
            for index, token in enumerate(tokens):
                positions.setdefault(token, []).append(index)
            text = "\0".join(tokens)
            for payload in dict.fromkeys(payloads):
                spots = positions.get(payload, [])
                sole = text.count(payload) == len(spots)  # no glued carrier repeats it
                if spots and sole and all(_after_shell_verb(tokens, programs, i) for i in spots):
                    continue  # an ``eval`` / ``source`` argument
                # A payload cut out of a glued word (``bash<<<'x'``, ``-c'x'``) may equal an
                # inert ``echo`` argument too; it is exempt only if it occurs nowhere else.
                if (
                    spots
                    and sole
                    and all(
                        _shell._data_consumer_exempt(
                            i, payload, programs, tokens, command_disqualified=disqualified
                        )
                        for i in spots
                    )
                ):
                    continue
                # The tokenizer keeps ``\\$`` inside double quotes, where bash drops the
                # backslash; reading it as ``$`` fails closed (a single-quoted ``\\$HOME``
                # is then refused too).
                payload = payload.replace("\\$", "$")
                if len(payload) < len(source) and payload not in seen:
                    seen.add(payload)
                    pending.append(payload)
    return kinds
