"""Tests for the shared shell reader that every argv-structural floor reads.

The self-protection subcommand floor, the git-publish floor and the ssh self-target
floor all read a command through one layer in ``shell_normalizer``: the operand
reader, the per-token program attribution (``_argv_programs``), the data-consumer
exemption and the frame walk. These tests pin how that layer reads a redirection
glued to an operand word, a launcher in front of a data consumer, ``git grep``, and
a quoted-delimiter heredoc body, through the public gate (``is_denied``) and the
reader functions themselves.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401

from kiro_crew import security

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_denied_commands_security family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


def _refused(cmd: str) -> bool:
    return security.is_denied(cmd) is not None


class TestRedirectGluedToAnOperandWord:
    """``restart>/tmp/log`` is the argv word ``restart`` plus a redirection."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "kirocrew restart>/tmp/log",
            "kirocrew restart>>/tmp/log",
            "kirocrew restart&>/tmp/log",
            "kirocrew restart</dev/null",
            "kirocrew restart>/tmp/log; echo ok",
            "kirocrew update>/tmp/log",
            "kirocrew file-delivery>/tmp/log approve",
            "python -m kiro_crew restart>/tmp/log",
        ],
    )
    def test_glued_redirect_keeps_the_subcommand_word(self, cmd):
        assert _refused(cmd), cmd

    def test_operand_reader_keeps_the_word_in_front_of_the_redirect(self):
        tokens = security._self_tokens("kirocrew restart>/tmp/log")
        assert security._self_cli_operands(tokens, 0) == ["restart"]

    @pytest.mark.parametrize(
        ("token", "word"),
        [
            ("restart>/tmp/log", "restart"),
            ("restart&>/tmp/log", "restart"),
            ("restart</dev/null", "restart"),
            ("2>/tmp/log", ""),
            ("{fd}>/tmp/log", ""),
            (">/tmp/log", ""),
            (">", ""),
            ("restart", ""),
            ("$x>/tmp/log", ""),
            ("{a,b}>/tmp/log", ""),
            ("x<(true)", ""),
        ],
    )
    def test_redirect_word_prefix(self, token, word):
        assert security._redirect_word_prefix(token) == word

    @pytest.mark.parametrize(
        "cmd",
        [
            "kirocrew $x>/tmp/log restart",
            "kirocrew {a,b}>/tmp/log restart",
            "kirocrew x<(true) restart",
        ],
    )
    def test_a_non_literal_word_before_a_redirect_is_not_read_as_an_operand(self, cmd):
        assert _refused(cmd), cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "kirocrew status>/tmp/log",
            "kirocrew 2>/tmp/log status",
            "kirocrew restart2>/tmp/log",
        ],
    )
    def test_a_descriptor_or_another_subcommand_stays_allowed(self, cmd):
        assert not _refused(cmd), cmd


class TestLauncherInFrontOfADataConsumer:
    """A launcher that runs a data consumer hands the consumer data arguments."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "git grep -n kirocrew restart",
            "time grep kirocrew restart",
            "time -p grep kirocrew restart",
            "sudo grep kirocrew restart",
            "env X=1 grep kirocrew restart",
            "nice -n 5 grep kirocrew restart",
            "timeout 5 grep kirocrew restart",
            "nohup time grep kirocrew restart",
            "command grep kirocrew restart",
            # A redirection carrying ``&`` (``2>&1``) is not a command boundary,
            # so the search stays a data consumer.
            "sudo grep kirocrew restart 2>&1",
            "time grep kirocrew restart >&2",
            # ``--pre`` is a preprocessor option only for rg/ag/ack; as a grep
            # search pattern it is ordinary data, so the search stays allowed.
            "grep -r -e --pre -e kirocrew -e restart src/",
            "grep --pre kirocrew restart src/",
        ],
    )
    def test_consumer_arguments_are_data(self, cmd):
        assert not _refused(cmd), cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "sudo kirocrew restart",
            "time kirocrew restart",
            # A substitution in program position after a launcher is not a
            # literal program, so the launcher's attribution is kept.
            "env $(echo pkill) -f kirocrew",
            "sudo `echo pkill` -f kirocrew",
            "sudo env kirocrew restart",
            "sudo -- kirocrew restart",
            "sudo -u grep kirocrew restart",
            "env -S grep kirocrew restart",
            "nice -n grep kirocrew restart",
            "timeout grep kirocrew restart",
            "timeout -s 9 5 grep kirocrew restart",
            "command -v kirocrew restart",
            "sudo grep kirocrew restart | sh",
            "sudo sed 's/x/y/e' kirocrew restart",
            "time grep x; kirocrew restart",
            "git -c x=y grep kirocrew restart",
            "git grep -O kirocrew restart",
            "git grep -no kirocrew restart",
            "git grep x | sh kirocrew restart",
            # ``rg``/``ag``/``ack`` run a preprocessor named by ``--pre``, so the
            # argument after it is executable, launched or not.
            "rg --pre kirocrew restart",
            "env rg --pre kirocrew -ex restart",
            "printf x > r; env rg --pre kirocrew -ex restart",
            # A glued control ``&``/``&&`` chains a SECOND command the launcher
            # does not run; it must not inherit the data-consumer label.
            "sudo grep x& kirocrew restart",
            "nice grep x&& kirocrew restart",
            "env grep x&& kirocrew restart",
            "time grep x& kirocrew restart",
            # A mid-token control ``&`` is a separator too (``x&command``).
            "env grep x&kirocrew restart",
            "sudo grep foo&kirocrew restart",
            # ``&<`` is not a redirection (bash has no such operator): it
            # backgrounds the search, then ``<f`` applies to the executed command.
            "sudo grep x &<f kirocrew restart",
            "git grep x&<f kirocrew restart",
            "kirocrew restart&</dev/null",
        ],
    )
    def test_an_unmodelled_launcher_form_is_still_read_as_running(self, cmd):
        # ``git grep -O`` runs a pager program; words reach the reader lowercased,
        # so any ``o`` in a short-option bundle keeps ``git grep`` unlabelled.
        assert _refused(cmd), cmd

    def test_argv_programs_labels_the_launched_consumer(self):
        tokens = ["sudo", "grep", "kirocrew", "restart", ";", "kirocrew", "restart"]
        programs = security._argv_programs(tokens)
        assert programs[:4] == ["grep"] * 4
        assert programs[5:] == ["kirocrew", "kirocrew"]

    @pytest.mark.parametrize(
        "cmd",
        [
            "sudo grep -nE 'chmod|/etc/' src/",
            "time grep -n chmod /etc/passwd",
            "git grep -n 'chmod 777 /etc/' src",
        ],
    )
    def test_a_permission_verb_mention_under_a_launcher_is_allowed(self, cmd):
        assert not _refused(cmd), cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "sudo chmod 777 /etc/passwd",
            "time chmod 777 /etc/passwd",
            "nice -n 5 chmod 777 /etc/passwd",
            "env X=1 chmod 777 /etc/passwd",
            "command chmod 777 /etc/passwd",
            "timeout 5 chmod 777 /etc/passwd",
            # A launched second-operand writer names its sink after the verb.
            "sudo uniq chmod /etc/passwd",
            "time xxd chmod /etc/shadow",
            "nice -n 5 uniq chmod /etc/passwd",
            "sudo xxd chmod /usr/local/bin/git",
        ],
    )
    def test_a_permission_verb_run_by_a_launcher_is_refused(self, cmd):
        assert _refused(cmd), cmd
