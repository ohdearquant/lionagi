# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The catalog has to be enough to write the call it describes.

``help=true`` promises its answer is enough to make the common call. That
promise only holds if the entry carries everything the call is gated on, so
these tests build ops from the catalog reply alone and check that nothing
refuses them on a gate the catalog was supposed to have satisfied.

The spawn verbs are the gated ones, and running one starts a background
agent, so the ops here are steered into a refusal that lies *past* every
gate (an unreadable prompt file): a call rejected for its prompt is a call
whose fingerprint was accepted, which is the fact under test -- nothing is
actually spawned to establish it.
"""

from __future__ import annotations

import json

import pytest

from lionagi.mcp import dispatch

from .stdio_client import StdioMCPClient

# A path that cannot exist, so the op is refused while reading it rather than
# spawned. Absolute because the server refuses a relative prompt_file by rule.
UNREADABLE_PROMPT_FILE = "/nonexistent/lionagi-catalog-sufficiency/prompt.txt"


def _entry(catalog: dict, verb: str) -> dict:
    return next(e for e in catalog["verbs"] if e["verb"] == verb)


def _gated_verbs() -> list[str]:
    return [name for name, verb in dispatch.VERBS.items() if verb.executor == "spawn"]


@pytest.fixture(scope="module")
def catalog() -> dict:
    return dispatch.catalog()


def test_every_fingerprint_gated_verb_says_so_in_the_catalog(catalog: dict) -> None:
    """A caller reading one entry can tell whether that verb needs a fingerprint.

    Without this the gate is discoverable only by tripping it, which costs the
    round-trip the catalog exists to save.
    """
    for name in _gated_verbs():
        entry = _entry(catalog, name)
        assert "schema_fingerprint" in entry or "schema_fingerprint_varies_with" in entry, (
            f"{name} is fingerprint-gated and its catalog entry says nothing about it"
        )


def test_a_quoted_fingerprint_is_the_one_the_gate_wants(catalog: dict) -> None:
    """The value in the entry is the value the dispatcher compares against.

    Checked against the dispatcher's own computation rather than a pinned
    string, so the test tracks the schema instead of dating with it.
    """
    quoted = {
        name: _entry(catalog, name)["schema_fingerprint"]
        for name in _gated_verbs()
        if "schema_fingerprint" in _entry(catalog, name)
    }
    assert quoted, "no verb quotes a fingerprint, so the promise cannot hold for any of them"
    for name, fingerprint in quoted.items():
        wanted = dispatch.schema_fingerprint(dispatch.verb_schema(dispatch.VERBS[name]))
        assert fingerprint == wanted


def test_a_verb_whose_schema_depends_on_an_argument_quotes_no_fingerprint(catalog: dict) -> None:
    """Silence beats a string that is guaranteed to be refused.

    ``play.submit`` requires a playbook and is projected again once one is named,
    so the argument-free schema it would otherwise be fingerprinted from
    describes a call that cannot be made.
    """
    for name in _gated_verbs():
        verb = dispatch.VERBS[name]
        entry = _entry(catalog, name)
        varies = entry.get("schema_fingerprint_varies_with", [])
        if any(parameter in verb.requires for parameter in varies):
            assert "schema_fingerprint" not in entry
            assert varies


def test_a_positional_the_parser_will_not_enforce_is_still_reported(catalog: dict) -> None:
    """``required: []`` alone reads as "a call with no arguments is valid".

    Every spawn verb's prompt arrives through a positional argparse declares with
    ``nargs="*"``, which the parser accepts empty and the command cannot run
    without.
    """
    for name in _gated_verbs():
        entry = _entry(catalog, name)
        assert entry.get("required_unenforced"), (
            f"{name} reports no unenforced requirement, though its prompt positional is one"
        )
        assert not set(entry["required_unenforced"]) & set(entry.get("required", []))


def test_the_unenforced_parameters_are_real_parameters(catalog: dict) -> None:
    """A name reported here has to be one the caller may actually pass."""
    for entry in catalog["verbs"]:
        if not entry.get("available"):
            continue
        named = entry.get("required_unenforced")
        if not named:
            continue
        schema = dispatch.verb_schema(dispatch.VERBS[entry["verb"]])
        for parameter in named:
            assert parameter in schema["properties"]


@pytest.mark.parametrize("verb", ["agent.submit", "flow.submit", "fanout.submit"])
def test_a_call_built_only_from_the_catalog_clears_the_gate(verb: str, tmp_path) -> None:
    """The whole point, driven over the wire the way a client drives it.

    Ask for the catalog, read one entry, send the op it describes. Before the
    entry carried a fingerprint the first attempt came back ``stale_schema``,
    which is the gate refusing a caller who did exactly what the tool told them
    to do. Now the refusal is about the prompt file this test deliberately made
    unreadable, which is a refusal from past the gate.
    """
    with StdioMCPClient(cwd=str(tmp_path)) as client:
        client.initialize()
        catalog = client.request(help=True)
        entry = _entry(catalog, verb)

        op_fields = {}
        if "schema_fingerprint" in entry:
            op_fields["schema_fingerprint"] = entry["schema_fingerprint"]
        result = client.op(verb, {"prompt_file": UNREADABLE_PROMPT_FILE}, **op_fields)

    assert result["ok"] is False, "the unreadable prompt file should have refused this op"
    kind = result["error"]["kind"]
    assert kind != "stale_schema", (
        f"{verb} was refused at the fingerprint gate by a call built from the catalog: "
        f"{json.dumps(result['error'])}"
    )
    assert kind == "invalid_input"
    assert "prompt_file" in result["error"]["message"]


def test_a_catalog_built_call_to_an_ordinary_read_verb_succeeds(tmp_path) -> None:
    """The same construction on a verb with no gate and no side effects.

    Separates "the fingerprint path now works" from "the transport works at all",
    so a green above cannot be read as either one on its own.
    """
    with StdioMCPClient(cwd=str(tmp_path)) as client:
        client.initialize()
        catalog = client.request(help=True)
        entry = _entry(catalog, "job.list")
        assert entry["required"] == []
        assert "schema_fingerprint" not in entry
        result = client.op("job.list", {})

    assert result["ok"] is True, json.dumps(result)


async def test_the_advertised_tool_description_still_carries_the_call_gates():
    """The tool's docstring is published metadata, so trimming it changes what
    every client is told.

    The rest of this module checks that the CATALOG is enough to write the call.
    This checks the step before it: that the description a client reads at all
    still says a catalog exists, that fingerprint-gated verbs need the
    ``schema_fingerprint`` it returns, and that help and ops go in separate
    calls. A caller that is never told about the fingerprint cannot be rescued
    by a catalog it has no reason to fetch.

    Asserted against the REGISTERED tool rather than the function's ``__doc__``,
    because what a client receives is whatever the decorator published, and those
    are only the same thing for as long as nobody passes an explicit description.
    """
    from lionagi.mcp.server import mcp

    tool = await mcp.get_tool("request")
    described = tool.description or ""

    # Must be present: without these a caller cannot form a gated op.
    for required in ("schema_fingerprint", "second round-trip", "help=true"):
        assert required in described, (
            f"the advertised description no longer mentions {required!r}; "
            "clients are told less than the server requires"
        )

    # Must NOT be present: guards the assertion above against passing on a
    # description that merely got longer. If this ever legitimately appears,
    # the arm above is the one to re-derive, not this one.
    assert "TODO" not in described
