# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Rules the two redaction layers share.

``redact.py`` judges a credential twice: once by the name a value sits under
in a mapping, and once by the name written in front of it in free text. The
tests here are about the two agreeing. The read tools that call them have
their own tests alongside.
"""

from __future__ import annotations

import pytest

from lionagi.studio.operator import redact

NO_KNOWN_VALUES: frozenset[str] = frozenset()
SECRET = "s3cr3t-value-abc123def456"


def _scrub(text: str) -> str:
    # Pinned empty so the env this runs in cannot decide the outcome.
    return redact.scrub_text(text, known_values=NO_KNOWN_VALUES)


def test_every_name_the_field_layer_calls_a_credential_is_one_in_free_text_too():
    """The free-text half used to carry its own, shorter list of credential
    names. A spec that wrote ``Authorization=Token <value>`` as prose kept
    the value, while the same name used as a mapping key had it removed --
    and ``auth_token``, ``credential`` and ``MY_API_KEY`` behaved the same
    way. Deriving the cases from the marker list means a name added to one
    layer cannot go missing from the other.
    """
    names = sorted({*redact._SECRET_KEY_MARKERS, *redact._EXACT_SECRET_FIELD_NAMES})
    assert names, "no credential markers to check"

    for name in names:
        assert redact.is_secret_field_name(name), f"{name} is not a credential name"
        for text in (f"{name}={SECRET}", f"{name}: {SECRET}"):
            assert SECRET not in _scrub(text), f"leaked: {_scrub(text)!r}"


@pytest.mark.parametrize(
    "text",
    [
        "Authorization=Token " + SECRET,
        "Authorization: Token " + SECRET,
        "authorization=" + SECRET,
        "Proxy-Authorization: Basic " + SECRET,
        "MY_API_KEY=" + SECRET,
        "curl -H 'Authorization=Token " + SECRET + "'",
    ],
)
def test_a_credential_written_into_prose_is_removed(text):
    assert SECRET not in _scrub(text)


def test_an_auth_scheme_survives_the_credential_it_introduces():
    """The scheme names a mechanism, so a reader can still tell which kind
    of auth a spec asked for."""
    assert _scrub("Authorization: Bearer " + SECRET) == "Authorization: Bearer [redacted]"
    assert _scrub("Authorization=Token " + SECRET) == "Authorization=Token [redacted]"


def test_an_unrecognized_scheme_takes_the_credential_with_it():
    """An unknown scheme must not leave the credential standing behind the
    word in front of it."""
    assert SECRET not in _scrub("Authorization: Weirdscheme " + SECRET)


# A base64 credential is the realistic Basic-auth value and it carries "=" or
# "/", which the shape-based value rule excludes by construction. Using it
# here keeps these tests answerable only by the auth-pair rule: SECRET above
# is caught by shape alone, so it would pass them with that rule broken.
OPAQUE = "QWxhZGRpbjpvcGVuIHNlc2FtZQ=="


def test_the_opaque_credential_is_invisible_to_the_shape_rule():
    """Guards the tests below: if the shape rule ever starts catching this
    value they still pass, but stop being about the auth-pair rule."""
    assert not redact._looks_like_secret_value(OPAQUE)


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Basic\n" + OPAQUE,
        "Authorization=Token\n" + OPAQUE,
        "Proxy-Authorization: Bearer\n" + OPAQUE,
        "GET /v1 HTTP/1.1\r\nAuthorization: Basic\r\n " + OPAQUE + "\r\nAccept: */*",
    ],
)
def test_a_credential_folded_onto_the_next_line_still_goes(text):
    """A header folded across lines, or pasted into free text with the break
    kept, puts the credential on the line after its scheme. Matching only
    horizontal whitespace consumed the scheme and published the credential,
    which is worse than not matching at all."""
    assert OPAQUE not in _scrub(text)


def test_the_value_ends_at_a_blank_line():
    """One break is a folded header; a blank line starts new text, and the
    word after it must not be eaten as though it were a credential."""
    assert _scrub("Authorization: Basic\n\nRetry the request") == (
        "Authorization: [redacted]\n\nRetry the request"
    )


def test_an_unrecognized_scheme_does_not_reach_onto_the_next_line():
    """The unknown-scheme branch takes two bare tokens on trust. Across a
    break the second one belongs to whatever follows, not to the header."""
    assert _scrub("Authorization: Weirdscheme\nGET /next") == (
        "Authorization: [redacted]\nGET /next"
    )


@pytest.mark.parametrize(
    "text",
    [
        "max_tokens: 4096",
        "prompt_tokens=12, completion_tokens=8",
        "token_count: 7",
        "Note: this is fine",
        "see https://example.com/docs",
    ],
)
def test_a_count_is_not_a_credential(text):
    """The marker test matches by substring, so ``max_tokens`` and
    ``prompt_tokens`` reach the free-text rule. The field-name layer already
    lets those through -- ``redact_scalar`` only redacts strings, so
    ``{"max_tokens": 4096}`` survives it -- and the same reading written out
    as prose has to survive here.
    """
    assert _scrub(text) == text


def test_a_mapping_key_and_the_same_name_in_prose_agree():
    """The two layers reached by one call, on one payload."""
    payload = {
        "Authorization": f"Token {SECRET}",
        "notes": f"send Authorization=Token {SECRET}",
        "max_tokens": 4096,
    }
    out = redact.redact_arguments(payload)

    assert out["Authorization"] == "[redacted]"
    assert SECRET not in out["notes"]
    assert out["max_tokens"] == 4096
