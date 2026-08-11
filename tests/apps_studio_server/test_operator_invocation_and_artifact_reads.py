# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Focused tests for the invocation-detail and artifact Operator read tools."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")

pytestmark = pytest.mark.asyncio

PLANTED_SECRET = "sk-live-9f8e7d6c5b4a3210"
PLANTED_PATH = "/Users/example-user/private/workspace/notes.txt"
PLANTED_STORE_URL = "postgresql+asyncpg://reader:hunter2@internal-db.example/data"


def _assert_planted_values_are_scrubbed(raw_source: object, tool_result: object) -> None:
    """Confirm every planted value is absent from the result, with a positive
    control proving each assertion would fail if the value leaked."""
    raw_text = json.dumps(raw_source)
    result_text = json.dumps(tool_result)

    # Positive control: the values really are present in the unredacted input,
    # so the absence checks below are not vacuously true.
    assert PLANTED_SECRET in raw_text
    assert PLANTED_PATH in raw_text
    assert PLANTED_STORE_URL in raw_text

    assert PLANTED_SECRET not in result_text
    assert PLANTED_PATH not in result_text
    assert PLANTED_STORE_URL not in result_text


# ---------------------------------------------------------------------------
# get_invocation
# ---------------------------------------------------------------------------


async def test_get_invocation_returns_projected_fields_for_known_id(monkeypatch):
    from lionagi.studio.operator.application_mcp import get_invocation
    from lionagi.studio.services import invocations

    source = {
        "id": "inv-happy",
        "skill": "tester",
        "prompt": "summarize the run",
        "sessions": [{"id": "s-1", "name": "child session"}],
        "artifacts": [{"id": "a-1", "kind": "result", "name": "result", "content": {"ok": True}}],
    }

    async def fake_get_invocation(_invocation_id, **_kwargs):
        return source

    monkeypatch.setattr(invocations, "get_invocation", fake_get_invocation)
    result = await get_invocation({"invocation_id": "inv-happy"})

    assert result["known"] is True
    assert result["source"] == "store"
    assert result["skill"] == "tester"
    assert result["sessions_truncated"] is False
    assert result["artifacts_truncated"] is False
    assert len(result["sessions"]) == 1
    assert len(result["artifacts"]) == 1


async def test_get_invocation_caps_oversized_artifact_content_and_flags_truncation(monkeypatch):
    from lionagi.studio.operator.application_mcp import get_invocation
    from lionagi.studio.services import invocations

    oversized_body = "y" * 2_000_050
    source = {
        "id": "inv-oversized",
        "sessions": [],
        "artifacts": [
            {"id": "a-1", "kind": "result", "name": "result", "content": {"body": oversized_body}}
        ],
    }

    async def fake_get_invocation(_invocation_id, **_kwargs):
        return source

    monkeypatch.setattr(invocations, "get_invocation", fake_get_invocation)
    result = await get_invocation({"invocation_id": "inv-oversized"})

    artifact = result["artifacts"][0]
    assert artifact["content_truncated"] is True
    assert len(json.dumps(artifact["content"]).encode()) <= 2_000_000


async def test_get_invocation_redacts_secret_url_and_path_from_all_content(monkeypatch):
    from lionagi.studio.operator.application_mcp import get_invocation
    from lionagi.studio.services import invocations

    source = {
        "id": "inv-secret",
        "prompt": f"Authorization: Bearer {PLANTED_SECRET} see {PLANTED_PATH} at {PLANTED_STORE_URL}",
        "sessions": [{"id": "s-1", "name": f"child of {PLANTED_PATH}"}],
        "artifacts": [
            {
                "id": "a-1",
                "kind": "result",
                "name": "result",
                "content": {
                    "token": PLANTED_SECRET,
                    "path": PLANTED_PATH,
                    "url": PLANTED_STORE_URL,
                },
            }
        ],
    }

    async def fake_get_invocation(_invocation_id, **_kwargs):
        return source

    monkeypatch.setattr(invocations, "get_invocation", fake_get_invocation)
    result = await get_invocation({"invocation_id": "inv-secret"})

    _assert_planted_values_are_scrubbed(source, result)


async def test_get_invocation_reports_unknown_for_missing_invocation_id(monkeypatch):
    from lionagi.studio.operator.application_mcp import get_invocation
    from lionagi.studio.services import invocations

    async def fake_get_invocation(_invocation_id, **_kwargs):
        return None

    monkeypatch.setattr(invocations, "get_invocation", fake_get_invocation)
    result = await get_invocation({"invocation_id": "does-not-exist"})

    assert result == {"known": False}


# ---------------------------------------------------------------------------
# list_artifacts
# ---------------------------------------------------------------------------


async def test_list_artifacts_returns_metadata_for_known_owner(monkeypatch):
    import lionagi.studio.operator.application_mcp as app_mcp

    source = [
        {"id": "a-1", "kind": "result", "name": "first"},
        {"id": "a-2", "kind": "result", "name": "second"},
    ]

    async def fake_rows(*_args, **_kwargs):
        return source

    monkeypatch.setattr(app_mcp, "_artifact_rows", fake_rows)
    result = await app_mcp.list_artifacts({"session_id": "session-happy", "limit": 50})

    assert result["source"] == "store"
    assert result["truncated"] is False
    assert [row["id"] for row in result["artifacts"]] == ["a-1", "a-2"]


async def test_list_artifacts_caps_row_count_and_flags_truncation(monkeypatch):
    import lionagi.studio.operator.application_mcp as app_mcp

    source = [{"id": f"a-{i}", "kind": "result", "name": f"item {i}"} for i in range(5)]

    async def fake_rows(*_args, **_kwargs):
        return source

    monkeypatch.setattr(app_mcp, "_artifact_rows", fake_rows)
    result = await app_mcp.list_artifacts({"invocation_id": "inv-many", "limit": 2})

    assert result["truncated"] is True
    assert len(result["artifacts"]) == 2


async def test_list_artifacts_redacts_secret_url_and_path_from_metadata(monkeypatch):
    import lionagi.studio.operator.application_mcp as app_mcp

    source = [
        {
            "id": "a-1",
            "kind": "result",
            "name": f"token={PLANTED_SECRET} path={PLANTED_PATH} url={PLANTED_STORE_URL}",
            "content": {"token": PLANTED_SECRET},
            "file_path": PLANTED_PATH,
        }
    ]

    async def fake_rows(*_args, **_kwargs):
        return source

    monkeypatch.setattr(app_mcp, "_artifact_rows", fake_rows)
    result = await app_mcp.list_artifacts({"session_id": "session-secret", "limit": 10})

    assert "content" not in result["artifacts"][0]
    assert "file_path" not in result["artifacts"][0]
    _assert_planted_values_are_scrubbed(source, result)


async def test_list_artifacts_returns_empty_for_unknown_owner_id(monkeypatch):
    import lionagi.studio.operator.application_mcp as app_mcp

    async def fake_rows(*_args, **_kwargs):
        return []

    monkeypatch.setattr(app_mcp, "_artifact_rows", fake_rows)
    result = await app_mcp.list_artifacts({"session_id": "no-such-session", "limit": 10})

    assert result["artifacts"] == []
    assert result["truncated"] is False


# ---------------------------------------------------------------------------
# get_artifact
# ---------------------------------------------------------------------------


async def test_get_artifact_returns_full_projection_for_known_id(monkeypatch):
    from lionagi.studio.operator.application_mcp import get_artifact
    from lionagi.studio.services import invocations

    source = {"id": "a-happy", "kind": "result", "name": "result", "content": {"ok": True}}

    async def fake_get_artifact(_artifact_id, **_kwargs):
        return source

    monkeypatch.setattr(invocations, "get_artifact", fake_get_artifact)
    result = await get_artifact({"artifact_id": "a-happy"})

    assert result["known"] is True
    assert result["source"] == "store"
    assert result["content_truncated"] is False
    assert result["content"] == {"ok": True}


async def test_get_artifact_caps_oversized_content_and_flags_truncation(monkeypatch):
    from lionagi.studio.operator.application_mcp import get_artifact
    from lionagi.studio.services import invocations

    oversized_body = "z" * 2_000_050
    source = {
        "id": "a-oversized",
        "kind": "result",
        "name": "result",
        "content": {"body": oversized_body},
    }

    async def fake_get_artifact(_artifact_id, **_kwargs):
        return source

    monkeypatch.setattr(invocations, "get_artifact", fake_get_artifact)
    result = await get_artifact({"artifact_id": "a-oversized"})

    assert result["content_truncated"] is True
    assert len(json.dumps(result["content"]).encode()) <= 2_000_000


async def test_get_artifact_redacts_secret_url_and_path_from_content(monkeypatch):
    from lionagi.studio.operator.application_mcp import get_artifact
    from lionagi.studio.services import invocations

    source = {
        "id": "a-secret",
        "kind": "result",
        "name": f"token={PLANTED_SECRET} path={PLANTED_PATH} url={PLANTED_STORE_URL}",
        "file_path": PLANTED_PATH,
        "content": {"token": PLANTED_SECRET, "path": PLANTED_PATH, "url": PLANTED_STORE_URL},
    }

    async def fake_get_artifact(_artifact_id, **_kwargs):
        return source

    monkeypatch.setattr(invocations, "get_artifact", fake_get_artifact)
    result = await get_artifact({"artifact_id": "a-secret"})

    assert "file_path" not in result
    _assert_planted_values_are_scrubbed(source, result)


async def test_get_artifact_reports_unknown_for_missing_artifact_id(monkeypatch):
    from lionagi.studio.operator.application_mcp import get_artifact
    from lionagi.studio.services import invocations

    async def fake_get_artifact(_artifact_id, **_kwargs):
        return None

    monkeypatch.setattr(invocations, "get_artifact", fake_get_artifact)
    result = await get_artifact({"artifact_id": "does-not-exist"})

    assert result == {"known": False}


# ---------------------------------------------------------------------------
# connection mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool_name", "service_name", "arguments"),
    [
        ("get_invocation", "get_invocation", {"invocation_id": "inv-1"}),
        ("get_artifact", "get_artifact", {"artifact_id": "a-1"}),
    ],
)
async def test_the_read_tools_ask_for_a_read_only_open_where_the_store_offers_one(
    monkeypatch, tool_name, service_name, arguments
):
    """These tools only read, so they must not take the ordinary open.

    The ordinary open applies schema on the way in, which acquires a write lock
    and can issue one-time migration statements — work a read has no business
    doing. Both directions are asserted: read-only is requested when the store
    can give it, and not requested when it cannot, since asking unconditionally
    fails at open on a server-backed store rather than degrading.
    """
    import lionagi.state.db as state_db
    from lionagi.studio.operator import application_mcp
    from lionagi.studio.services import invocations

    seen: list[bool] = []

    async def recording_service(_id, **kwargs):
        seen.append(kwargs.get("readonly"))
        return None

    monkeypatch.setattr(invocations, service_name, recording_service)
    tool = getattr(application_mcp, tool_name)

    monkeypatch.setattr(state_db, "read_only_open_supported", lambda: True)
    await tool(arguments)

    monkeypatch.setattr(state_db, "read_only_open_supported", lambda: False)
    await tool(arguments)

    assert seen == [True, False]


# ---------------------------------------------------------------------------
# redaction: the key-marker layer
# ---------------------------------------------------------------------------

# Deliberately shapeless: no prefix, no separator, no key=value or bearer form,
# so none of redact.py's pattern layers can claim it. Whatever redacts this can
# only have done so by looking at the KEY.
UNSHAPED_SECRET = "hunter2hunter2hunter2"


@pytest.mark.parametrize(
    "key",
    [
        "secret",
        "token",
        "password",
        "passwd",
        "api_key",
        "apikey",
        "credential",
        "authorization",
        "access_key",
        "private_key",
        "client_secret",
        "auth",
        "authentication",
        "bearer",
        "REFRESH_TOKEN",
        "db_password",
    ],
)
async def test_a_secret_with_no_recognisable_shape_is_redacted_by_its_key(key):
    """Every secret-marking key redacts its value even when the value itself
    looks like nothing.

    The shaped-secret tests cannot fail if this layer is removed — the pattern
    layer catches their planted values anyway — so without this arm the key
    markers are unpinned and dropping one is silent. Each marker gets its own
    case so removing any single one reddens rather than being covered by a
    sibling.
    """
    from lionagi.studio.operator.application_mcp import _safe_content
    from lionagi.studio.operator.redact import scrub_text

    # Premise, asserted rather than assumed: the pattern layer really does not
    # recognise this value. If a future pattern starts catching it, this fails
    # here and says so, instead of passing for a reason that is not the one
    # under test.
    assert scrub_text(UNSHAPED_SECRET) == UNSHAPED_SECRET

    projected = _safe_content({key: UNSHAPED_SECRET})

    assert projected == {key: "[redacted]"}


@pytest.mark.parametrize(
    "key",
    [
        # hyphenated, the HTTP header spelling
        "X-API-Key",
        "api-key",
        "access-key",
        "private-key",
        "client-secret",
        "x-auth-token",
        "API-KEY",
        # dotted, the config-file spelling
        "api.key",
        "access.key",
        "x.api.key",
        # spaced and otherwise punctuated
        "api key",
        "API KEY",
        "private key",
        "api:key",
        "api/key",
        # camelCase, which already folded to apikey and must keep doing so
        "apiKey",
    ],
)
async def test_a_field_name_redacts_whatever_separator_it_is_spelled_with(key):
    """Separators do not change which field a name refers to.

    Credentials reach us under HTTP header spellings such as X-API-Key and
    config spellings such as api.key, while our own records write api_key.
    Markers containing an underscore only ever matched the underscored form,
    so the other spellings walked past the key layer, and past the pattern
    layer too whenever the value had no shape.

    Every separator gets its own case rather than one representative, because
    the first version of this fix folded hyphens alone and left the dotted and
    spaced spellings leaking exactly as before.
    """
    from lionagi.studio.operator.application_mcp import _safe_content
    from lionagi.studio.operator.redact import scrub_text

    # Same premise as the underscored arm: the pattern layer must not be what
    # catches this, or the test would pass for the wrong reason.
    assert scrub_text(UNSHAPED_SECRET) == UNSHAPED_SECRET

    projected = _safe_content({key: UNSHAPED_SECRET})

    assert projected == {key: "[redacted]"}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("input_tokens", 1234),
        ("output_tokens", 5678),
        ("total_tokens", 6912),
        ("cached_tokens", 40),
        ("reasoning_tokens", 11),
        ("token_count", 7),
    ],
)
async def test_a_numeric_counter_survives_a_secret_marker_in_its_name(key, value):
    """Usage counters keep their values even though their names say "token".

    A number cannot carry a credential, so redacting one on the strength of its
    field name destroys the reported figure and protects nothing. These are the
    counters the cost and usage displays read, and a redacted counter is not a
    safe counter, it is a missing one.
    """
    from lionagi.studio.operator.application_mcp import _safe_content

    projected = _safe_content({key: value})

    assert projected == {key: value}


async def test_a_textual_secret_under_a_counter_shaped_name_is_still_redacted():
    """The numeric exemption is about the value, not about the name.

    Without this, "numbers are exempt" could be read as "anything whose name
    looks like a counter is exempt", which would reopen the leak the marker
    list exists to close.
    """
    from lionagi.studio.operator.application_mcp import _safe_content

    projected = _safe_content({"token_count": UNSHAPED_SECRET})

    assert projected == {"token_count": "[redacted]"}


async def test_token_shaped_mapping_keys_are_scrubbed_on_both_read_paths():
    """A credential can appear as a JSON key, not only as its value.

    The session/artifact projector already scrubs mapping keys as free text;
    tool arguments and manifests must apply the same rule or the latter path
    leaks the token verbatim.
    """
    from lionagi.studio.operator import application_mcp, redact

    secret_key = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    expected = {"[redacted]": "visible value"}

    assert redact.scrub_text(secret_key) == "[redacted]"
    assert application_mcp._safe_content({secret_key: "visible value"}) == expected
    assert redact.redact_arguments({secret_key: "visible value"}) == expected


def _spellings(name: str) -> list[str]:
    """The separator and case variants one field name arrives in.

    The run-together and camelCase forms are the load-bearing ones. Every other
    variant here keeps a separator, so the fold that precedes the comparison
    collapses all of them back onto the underscored name and they match
    whatever the rule's vocabulary happens to be. Only dropping the separator
    changes the folded string, so a list without these two asserts agreement
    over exactly the spellings that cannot disagree.
    """
    parts = name.split("_")
    variants = [
        name,
        name.replace("_", "-"),
        name.replace("_", "."),
        name.replace("_", " "),
        name.upper(),
        name.title(),
        "".join(parts),
        parts[0] + "".join(part.capitalize() for part in parts[1:]),
    ]
    return list(dict.fromkeys(variants))


def _every_name_the_secret_rule_knows() -> list[str]:
    """Read the corpus out of the rule instead of writing it down again.

    A hand-picked list can only hold names its author already believed both
    layers agreed about. That is not a hypothetical: two layers disagreed about
    `auth` while a test whose docstring claimed to assert their agreement was
    green, because the name was in neither list the author had chosen. Deriving
    the corpus means a name added to the vocabulary is covered by the same
    commit that adds it.
    """
    from lionagi.studio.operator import redact

    return sorted(set(redact._SECRET_KEY_MARKERS) | redact._EXACT_SECRET_FIELD_NAMES)


# Names a caller legitimately reads that carry a secret marker as a substring.
# `authors` is live on our own records, so withholding it deletes data rather
# than protecting anything. This is the arm that fails a fix which closes a gap
# by widening the substring markers instead of naming exact spellings.
_NAMES_A_CALLER_NEEDS_TO_READ = [
    "author",
    "authors",
    "author_name",
    "authored_by",
    "co_authors",
    "oauth_client_id",
    "unauthorized",
    "authority",
    "authorized_keys_count",
    "display_name",
    "created_at",
]


@pytest.mark.parametrize(
    "spelling",
    [spelling for name in _every_name_the_secret_rule_knows() for spelling in _spellings(name)],
)
async def test_every_name_the_secret_rule_knows_reads_the_same_on_both_paths(spelling):
    """Two callers ask whether a field name names a secret, on different paths.

    A name one of them treats as a secret and the other does not means the same
    credential is withheld on the session and artifact reads and served on the
    tool-argument and manifest reads. Pinning each rule on its own cannot see
    that — both stay green while they disagree — so this asserts the two answers
    together, over every name the rule is written in and every spelling of each.

    The two now share one predicate, which makes the agreement structural rather
    than coincidental. The assertion is still worth keeping: it is what reddens
    if either caller grows its own copy of the rule again, which is how they
    came apart the first time.
    """
    from lionagi.studio.operator import application_mcp, redact

    assert redact._is_secret_key(spelling) is True
    assert application_mcp._secret_field(spelling) is True


@pytest.mark.parametrize("name", _NAMES_A_CALLER_NEEDS_TO_READ)
async def test_a_name_that_merely_contains_a_marker_is_still_served(name):
    """Withholding too much is a defect too, and a quieter one.

    `auth` names a credential; `author` does not, and neither do `authors` or
    `oauth_client_id`. A rule that closes the `auth` gap by searching for it as
    a substring passes every test about credentials and silently empties fields
    a caller reads, which is why the exact-match names are compared for equality
    against the folded name rather than searched for inside it.
    """
    from lionagi.studio.operator import application_mcp, redact

    assert redact._is_secret_key(name) is False
    assert application_mcp._secret_field(name) is False


@pytest.mark.parametrize(
    "key",
    [
        "url",
        "uri",
        "dsn",
        "store_url",
        "store-url",
        "store.url",
        "storeUrl",
        "database_url",
        "database-url",
        "databaseUrl",
        "db_url",
        "db-url",
        "dbUrl",
        "connection_url",
        "connection-url",
        "connectionUrl",
    ],
)
async def test_a_location_field_is_withheld_even_when_its_value_has_no_url_shape(key):
    """Store locations are withheld by field name as well as by value shape.

    Every other test that plants a store location also gives it a scheme, so
    the pattern layer catches it and the name layer is never what makes the
    assertion pass. Deleting the name check outright left the whole suite
    green. A location that reaches us as bare text — a host, a path fragment,
    an operator's note — has only this layer between it and the caller.
    """
    from lionagi.studio.operator.application_mcp import _safe_content

    # Premise: this value survives under a name that matches nothing, so any
    # redaction below is the work of the key and not of a pattern.
    assert _safe_content({"note": UNSHAPED_SECRET}) == {"note": UNSHAPED_SECRET}

    projected = _safe_content({key: UNSHAPED_SECRET})

    assert projected == {key: "[redacted]"}
