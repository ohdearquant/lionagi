# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The verb surface as a client reaches it: over stdio, after a handshake.

These are deliberately not skipped when the transport will not start. A server
that cannot be spoken to is the failure this module exists to catch, and a skip
would report that absence as a pass.
"""

from __future__ import annotations

import pytest

from .stdio_client import StdioMCPClient

pytestmark = pytest.mark.timeout(180)


@pytest.fixture(scope="module")
def mcp() -> StdioMCPClient:
    """One handshaken server for the module; terminated however the tests end."""
    client = StdioMCPClient()
    try:
        client.initialize()
    except BaseException:
        client.close()
        raise
    try:
        yield client
    finally:
        client.close()


def test_handshake_advertises_the_request_tool(mcp: StdioMCPClient) -> None:
    names = [tool["name"] for tool in mcp.list_tools()]
    assert "request" in names, names


def test_a_read_verb_answers_over_the_wire(mcp: StdioMCPClient) -> None:
    result = mcp.op("server.info")
    assert result["ok"] is True, result


def test_help_returns_the_catalog(mcp: StdioMCPClient) -> None:
    catalog = mcp.request(help=True)
    assert catalog["available_count"] > 0
    assert any(entry["verb"] == "schedule.create" for entry in catalog["verbs"])


# ── the JSON-encoded flag a caller could not satisfy ─────────────────────────


def test_schedule_create_advertises_the_shape_it_accepts(mcp: StdioMCPClient) -> None:
    """The advertised type is the one the parser decodes, not ``string``.

    A plain string was refused as invalid JSON and a list as the wrong type,
    so the advertised ``string`` named a type no value satisfied.
    """
    schema = mcp.request(help="schedule.create")["schema"]
    spec = schema["properties"]["action_command_args"]
    assert spec["type"] == "array"
    assert spec["items"] == {"type": "string"}
    assert spec["x-json-encoded"] is True


def test_schedule_create_accepts_the_list_it_advertises(mcp: StdioMCPClient) -> None:
    """A value valid per the advertised schema gets past argument validation.

    The call is expected to fail further in — the command allowlist, or no
    scheduler to write to — but it has to get past argument validation, and it
    must not fail naming this parameter.
    """
    result = mcp.op(
        "schedule.create",
        {
            "name": "transport-harness-probe",
            "interval": 3600,
            "trigger_type": "interval",
            "action_kind": "command",
            "action_command": "echo",
            "action_command_args": ["hello", "{{run_id}}"],
        },
    )
    if not result["ok"]:
        message = result["error"]["message"]
        assert "action_command_args" not in message, message
        # The two ways the old contract refused a schema-obeying caller.
        assert "expects string" not in message, message
        assert "must be valid JSON" not in message, message


def test_schedule_create_refuses_a_bare_string(mcp: StdioMCPClient) -> None:
    """The type it advertises is also the type it enforces."""
    result = mcp.op(
        "schedule.create",
        {"name": "transport-harness-probe", "action_command_args": "hello"},
    )
    assert result["ok"] is False
    assert result["error"]["kind"] == "invalid_input"
    assert "action_command_args" in result["error"]["message"]


# ── the fingerprint gate says where the fingerprint goes ─────────────────────


def test_missing_fingerprint_error_states_the_op_level_shape(mcp: StdioMCPClient) -> None:
    result = mcp.op("agent.submit", {"prompt": "unused"})
    assert result["ok"] is False
    message = result["error"]["message"]
    assert "'args'" in message and "schema_fingerprint" in message
    # The literal op shape, so a caller cannot read "with the op" as "in args".
    assert "{'op': 'agent.submit', 'args': {...}, 'schema_fingerprint':" in message


def test_fingerprint_inside_args_is_refused_as_a_wrong_place(mcp: StdioMCPClient) -> None:
    """Putting it in ``args`` used to repeat the same refusal verbatim.

    Identical text for "you did not send it" and "you sent it in the wrong
    place" reads as an idempotent failure, so a caller retries the shape it
    already sent instead of moving the field.
    """
    fingerprint = mcp.request(help="agent.submit")["schema_fingerprint"]
    result = mcp.op("agent.submit", {"prompt": "unused", "schema_fingerprint": fingerprint})
    assert result["ok"] is False
    message = result["error"]["message"]
    assert "sibling of 'args'" in message or "unknown parameter" in message
