"""A two-segment link token is redacted the same wherever it starts.

The two-segment alternative in the credential pattern has a left boundary that
keeps `eyJ` from matching inside a longer word (`keyJson.get`) or right after a
`.`. After a `.` it starts a match only when the token also carries a `-` or a
payload shaped as base64url of ASCII JSON, which every minted token has and no
identifier chain does, so a token there is redacted as at the start of a text
while a dotted member chain of the same lengths is kept.
"""

from __future__ import annotations

import pytest

from kiro_crew.security import redact_credentials

#: A neutral token of the two-segment shape: `eyJ` plus a 100-char payload in the
#: base64url alphabet, a `.`, and a 43-char signature. Built from repeated filler,
#: so it decodes to nothing meaningful.
_TOKEN = "eyJ" + "a-b_" * 25 + "." + "c" * 21 + "-" + "d" * 21
_PLACEHOLDER = "[REDACTED: credential]"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (_TOKEN, _PLACEHOLDER),
        ("prefix " + _TOKEN, "prefix " + _PLACEHOLDER),
        ("prefix." + _TOKEN, "prefix." + _PLACEHOLDER),
        ("see ." + _TOKEN, "see ." + _PLACEHOLDER),
    ],
    ids=["start", "after-space", "after-word-dot", "after-lone-dot"],
)
def test_link_token_is_redacted_wherever_it_starts(text, expected):
    result, warnings = redact_credentials(text)

    assert result == expected
    assert len(warnings) == 1


def test_link_token_after_a_sentence_dot_is_redacted():
    """A token that follows the end of a sentence with no space is removed whole."""
    result, _ = redact_credentials(f"Open the link.{_TOKEN} to continue")

    assert result == f"Open the link.{_PLACEHOLDER} to continue"


@pytest.mark.parametrize(
    "text",
    [
        "obj.eyJsonReader.readValueFromInputStream(x)",
        "config.eyJsonSerializerConfigurationFactoryBuilderRegistryProviderDelegating"
        "InterceptorFactoryAdapterHandler.deserializeFromStringValueUsing"
        "ConfiguredObjectMapperInstance(x)",
        "cache.eyJ2IjoxfQ.json",
    ],
    ids=["short-member", "long-member", "dotted-filename"],
)
def test_dotted_identifiers_after_a_dot_are_kept(text):
    """Short dotted members after a `.` are kept by the segment lengths."""
    result, warnings = redact_credentials(text)

    assert result == text
    assert warnings == []


#: A member named `eyJ` plus a long identifier, then a 43-char member: the
#: two-segment shape at full length, built only from identifier characters.
_LONG_MEMBER = (
    "eyJsonSerializerConfigurationFactoryBuilderRegistryProviderDelegating"
    "InterceptorFactoryAdapterHandlerImpl"
)
_MEMBER_43 = "deserializeFromStringValueUsingMapperConfig"


@pytest.mark.parametrize("before", ["config.", "self.cache.", "."])
@pytest.mark.parametrize("after", ["(x)", "", ".close()", " = None"])
def test_a_full_length_dotted_member_chain_is_kept(before, after):
    """A dotted chain whose members have the token's lengths is ordinary code."""
    assert len(_LONG_MEMBER) - 3 >= 96 and len(_MEMBER_43) == 43
    text = f"{before}{_LONG_MEMBER}.{_MEMBER_43}{after}"

    result, warnings = redact_credentials(text)

    assert result == text
    assert warnings == []


def test_minted_link_tokens_after_a_dot_are_redacted():
    """Every token the generator mints is removed after a `.`, whatever it holds.

    A payload is base64url of ASCII JSON; the minted set covers signatures with
    and without `-`, so neither property the pattern reads is assumed.
    """
    from kiro_crew.dashboard.token_auth import generate_token

    tokens = [generate_token(f"user-{n}", 300, register_nonce=False) for n in range(200)]
    assert any("-" not in t for t in tokens) and any("-" in t for t in tokens)
    for token in tokens:
        result, _ = redact_credentials(f"obj.{token}(x)")
        assert result == f"obj.{_PLACEHOLDER}(x)", token


@pytest.mark.timeout(60)
@pytest.mark.parametrize("filler", ["a", "o", "a-", "aaao"])
def test_long_runs_after_a_dot_scan_in_linear_time(filler):
    """A long run after `.eyJ` that never completes a token is scanned once.

    Sized so a scan that re-walks the run from each position cannot finish
    inside the timeout.
    """
    text = (".eyJ" + filler * (400_000 // len(filler)) + ".x ") * 2

    result, warnings = redact_credentials(text)

    assert result == text
    assert warnings == []
