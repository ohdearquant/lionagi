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

from lionagi.adapters._base import _redact_url
from lionagi.studio.operator import redact
from lionagi.studio.operator.application_mcp import _safe_content

NO_KNOWN_VALUES: frozenset[str] = frozenset()
SECRET = "s3cr3t-value-abc123def456"


# A value the shape heuristic and the known env values cannot catch, so each
# test below can only be answered by the name the value sits under.
SIGNING_KEY_VALUE = "opaque/value=="
SIGNING_KEY_NAMES = [
    "LIONAGI_STUDIO_EVIDENCE_HMAC_KEY",
    "hmac-key",
    "hmacKey",
    "SIGNING_KEY",
    "signing-key",
    "signingKey",
    "my_signing_key_id",
    "mySigningKeyValue",
    "myHMACKeyBase64",
    "signingKeyValue",
    "hmacKeyBase64",
    "SIGNINGKEY",
    "SIGNING_KEY_ID",
    "signing_keys",
    "hmac_keys",
    "signing_key2",
    "SigningKey2",
    "mysigningkey",
    "webhooksigningkey",
    "MYSIGNINGKEY",
    "signing_keyword",
    "hmac_keyboard",
    "signingkeyvalue",
    "deSigningKey",
    "designingkey",
    "designing_key",
    "redesigning_key",
    "RedesigningKey",
    "codesigningkey",
    "codesigning_key",
    "code_signing_key",
    "nodesigningkey",
    "nodesigning_key",
    "node_signing_key",
]


def test_the_signing_key_value_is_invisible_to_the_shape_rule():
    """Guards the signing-key tests below: if the shape rule ever starts
    catching this value they still pass, but stop being about the name."""
    assert not redact._looks_like_secret_value(SIGNING_KEY_VALUE)


@pytest.mark.parametrize("name", SIGNING_KEY_NAMES)
def test_signing_key_names_are_classified_as_credentials(name):
    assert redact.is_secret_field_name(name)


@pytest.mark.parametrize("name", SIGNING_KEY_NAMES)
def test_signing_key_names_are_withheld_in_mappings(name):
    assert redact.redact_scalar(name, SIGNING_KEY_VALUE) == "[redacted]"


@pytest.mark.parametrize("name", SIGNING_KEY_NAMES)
def test_signing_key_names_are_withheld_in_free_text(name):
    assert (
        redact.scrub_text(f"{name}={SIGNING_KEY_VALUE}", known_values=NO_KNOWN_VALUES)
        == f"{name}=[redacted]"
    )


@pytest.mark.parametrize("name", SIGNING_KEY_NAMES)
def test_signing_key_names_are_withheld_in_adapter_urls(name):
    assert _redact_url(f"https://example.com/?{name}=opaque-value&safe=visible") == (
        f"https://example.com/?{name}=***&safe=visible"
    )


@pytest.mark.parametrize("name", SIGNING_KEY_NAMES)
def test_signing_key_names_in_the_environment_become_known_secrets(name, monkeypatch):
    monkeypatch.setenv(name, SIGNING_KEY_VALUE)
    assert SIGNING_KEY_VALUE in redact.known_secret_values()


@pytest.mark.parametrize("name", SIGNING_KEY_NAMES)
def test_signing_key_names_are_withheld_in_tool_content(name):
    assert _safe_content({name: {"value": SIGNING_KEY_VALUE}}) == {name: "[redacted]"}


@pytest.mark.parametrize(
    "name",
    [
        "sort_key",
        "cache_key",
        "primary_key",
        "key",
    ],
)
def test_unrelated_key_names_remain_visible(name):
    assert not redact.is_secret_field_name(name)
    assert redact.redact_scalar(name, "visible") == "visible"
    assert redact.scrub_text(f"{name}=visible", known_values=NO_KNOWN_VALUES) == f"{name}=visible"
    assert _safe_content({name: {"value": "visible"}}) == {name: {"value": "visible"}}
    url = f"https://example.com/?{name}=visible"
    # Adapter URLs independently reserve the bare query parameter "key".
    assert _redact_url(url) == ("https://example.com/?key=***" if name == "key" else url)


def test_known_signing_key_value_is_scrubbed_when_echoed(monkeypatch):
    value = "opaque/value=="
    monkeypatch.setenv("LIONAGI_STUDIO_EVIDENCE_HMAC_KEY", value)
    assert redact.scrub_text(f"echo {value}") == "echo [redacted]"


@pytest.mark.parametrize(
    ("name", "folded"),
    [("accessKey", "accesskey"), ("X-API.Key", "x_api_key"), ("SigningKey2", "signingkey2")],
)
def test_fold_field_name_keeps_its_existing_case_and_separator_contract(name, folded):
    assert redact.fold_field_name(name) == folded


@pytest.mark.parametrize("name", ["privateMode", "authorName", "clientService", "apiaryKey"])
def test_camel_splitting_does_not_expand_the_older_marker_rules(name):
    assert not redact.is_secret_field_name(name)


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


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Weirdscheme\n " + OPAQUE,
        "Authorization: Weirdscheme\n\t" + OPAQUE,
        "Authorization=Weirdscheme\n    " + OPAQUE,
        "Proxy-Authorization: Weirdscheme\r\n " + OPAQUE,
    ],
)
def test_an_unrecognized_schemes_folded_credential_still_goes(text):
    """A scheme this module does not know is exactly the case where the value
    after it cannot be judged, and folding it onto the next line left it
    standing one line below the marker announcing what it was. A recognized
    scheme is its own evidence that a credential follows; without one, the
    break itself has to look like a fold, and a fold begins with a space or a
    tab."""
    assert OPAQUE not in _scrub(text)


def test_an_unrecognized_scheme_does_not_reach_onto_an_unindented_line():
    """The boundary of the rule above, and the reason it asks for indentation.

    The unknown-scheme branch takes its second token on trust. A next line
    that starts hard against the margin is ordinary text rather than a folded
    header, so taking its first word would redact prose on every occurrence
    while catching a credential on almost none.
    """
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
