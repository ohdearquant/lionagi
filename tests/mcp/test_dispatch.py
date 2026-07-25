# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The dispatch surface: what a caller can discover, and what it is refused.

With one advertised tool, a caller learns the surface by calling it. That makes
the catalog, the per-verb schema and the shape of a rejection part of the
contract rather than convenience — so they are asserted here, including the
properties that only matter to a caller who got something wrong.
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from lionagi.mcp import dispatch, jobs, verbs


def call(**kwargs):
    return asyncio.run(dispatch.request(**kwargs))


def spawn_op(op: str, args: dict) -> dict:
    """A spawn op carrying the fingerprint its verb requires.

    Fetched the way a caller has to fetch it, so these tests exercise the
    round-trip rather than reaching past it.
    """
    return {"op": op, "args": args, "schema_fingerprint": call(help=op)["schema_fingerprint"]}


@pytest.fixture
def submitted(monkeypatch):
    """Capture what a spawn verb hands the job engine; nothing is spawned."""
    seen: dict = {}

    def fake_submit(kind, flags, **kwargs):
        seen.clear()
        seen.update(kind=kind, flags=list(flags), **kwargs)
        return {"run_id": "rid", "status": "running", "terminal": False, "outcome": None}

    monkeypatch.setattr(jobs, "submit", fake_submit)
    return seen


# ── the catalog ──────────────────────────────────────────────────────────────


def test_catalog_carries_a_signature_not_a_bare_name():
    # A list of names forces a second call before any first call. The point of
    # the signature is that the common invocation can be written from it.
    entries = {e["verb"]: e for e in call(help=True)["verbs"]}
    assert entries["job.status"]["required"] == ["run_id"]
    assert entries["job.wait"]["required"] == ["run_ids"]
    assert entries["play.submit"]["required"] == ["playbook"]
    assert all(e["summary"] for e in entries.values())


def test_catalog_names_an_unavailable_verb_with_its_reason():
    # Hiding it would make "not built" and "cannot be built yet" the same answer.
    entries = {e["verb"]: e for e in call(help=True)["verbs"]}
    absent = entries["schedule.list"]
    assert absent["available"] is False
    assert "machine result" in absent["reason"]


def test_catalog_never_advertises_a_previous_surface_name():
    listed = {e["verb"] for e in call(help=True)["verbs"]}
    assert listed.isdisjoint(verbs.SYNONYMS), sorted(listed & set(verbs.SYNONYMS))


def test_help_for_a_verb_returns_the_projected_schema():
    schema = call(help="agent.submit")["schema"]
    assert schema["additionalProperties"] is False
    # Descriptions come from the CLI's own help text, so they cannot go stale.
    assert schema["properties"]["timeout"]["type"] == "integer"
    assert schema["properties"]["yolo"]["type"] == "boolean"
    assert schema["properties"]["agent"]["x-flag"] == "--agent"


def test_help_for_an_unavailable_verb_says_why_instead_of_failing():
    answer = call(help="monitor")
    assert answer["available"] is False and answer["reason"]


def test_help_for_a_name_nobody_registered_is_an_error():
    with pytest.raises(ValueError, match="no such verb"):
        call(help="agent.summon")


# ── per-op envelope ──────────────────────────────────────────────────────────


def test_a_failing_op_does_not_fail_the_call_or_the_ops_beside_it():
    answer = call(ops=[{"op": "server.info"}, {"op": "job.status", "args": {}}])
    assert answer["status"] == "partial"
    assert answer["ops"][0]["ok"] is True
    assert answer["ops"][1]["ok"] is False
    assert answer["ops"][1]["op"] == "job.status"


def test_every_op_is_answered_in_the_order_it_was_given():
    answer = call(ops=[{"op": "job.list", "args": {"limit": 1}}, {"op": "server.info"}])
    assert [o["op"] for o in answer["ops"]] == ["job.list", "server.info"]
    assert answer["status"] == "success"


def test_ops_over_the_documented_maximum_is_an_error_not_a_truncation():
    over = [{"op": "server.info"}] * (verbs.MAX_OPS + 1)
    with pytest.raises(ValueError, match=f"over the maximum of {verbs.MAX_OPS}"):
        call(ops=over)


# ── closed validation, and the schema that comes back with the refusal ───────
#
# These request the submit fixture even though none of them should reach it: if
# validation ever stops refusing, the op runs, and a test that spawns a real
# background agent is a far worse failure than a red assertion.


def test_a_misspelled_parameter_is_refused_by_name(submitted):
    answer = call(ops=[spawn_op("agent.submit", {"tiemout": 30})])
    error = answer["ops"][0]["error"]
    assert error["kind"] == "invalid_input"
    assert "tiemout" in error["message"]


def test_a_rejected_op_carries_the_schema_it_was_judged_against(submitted):
    # This is what makes the first mistake cost one round-trip: the caller is
    # told the shape in the same reply that refuses them.
    answer = call(ops=[spawn_op("agent.submit", {"nope": 1})])
    schema = answer["ops"][0]["error"]["schema"]
    assert schema["title"] == "agent.submit"
    assert "timeout" in schema["properties"]


def test_a_wrong_type_is_refused_naming_what_was_expected():
    answer = call(ops=[{"op": "job.output", "args": {"run_id": "r", "tail_chars": "lots"}}])
    assert "expects integer" in answer["ops"][0]["error"]["message"]


@pytest.mark.parametrize(
    "value",
    [5, 1, 0, [1], {"a": 1}, False, None],
    ids=["int", "one", "zero", "list", "object", "false", "null"],
)
def test_a_flag_that_is_legal_bare_still_only_takes_what_it_declares(submitted, value):
    # A flag with an optional value projects as two alternatives, a string or a
    # literal true. Two alternatives is not "anything": admitting a value neither
    # branch describes would make the advertised schema and the admitted set two
    # different contracts, and the value reaches argv either way.
    answer = call(ops=[spawn_op("flow.submit", {"query": ["m", "do it"], "with_synthesis": value})])
    assert answer["ops"][0]["ok"] is False, value
    assert "expects string or the literal true" in answer["ops"][0]["error"]["message"]


@pytest.mark.parametrize("value", ["gpt-5", True], ids=["string", "bare"])
def test_a_flag_that_is_legal_bare_takes_both_forms_it_declares(submitted, value):
    answer = call(ops=[spawn_op("flow.submit", {"query": ["m", "do it"], "with_synthesis": value})])
    assert answer["ops"][0]["ok"] is True, value
    expected = "--with-synthesis" if value is True else f"--with-synthesis={value}"
    assert expected in submitted["flags"]


def test_a_flag_a_detached_run_cannot_honour_is_refused_with_its_reason(submitted):
    # Accepting it and dropping it would leave the caller believing it applied.
    answer = call(ops=[spawn_op("agent.submit", {"verbose": True})])
    assert "job.output" in answer["ops"][0]["error"]["message"]


def test_a_missing_required_parameter_names_itself(submitted):
    answer = call(ops=[spawn_op("play.submit", {})])
    assert "missing required parameter 'playbook'" in answer["ops"][0]["error"]["message"]


# ── previous-surface names ───────────────────────────────────────────────────


@pytest.mark.parametrize(("old", "new"), sorted(verbs.SYNONYMS.items()))
def test_a_previous_surface_name_resolves_to_its_namespaced_verb(old, new):
    assert verbs.resolve(old) == new
    assert new in verbs.VERBS


def test_a_synonym_dispatches_and_reports_the_namespaced_name():
    answer = call(ops=[{"op": "server_info"}])
    assert answer["ops"][0]["ok"] is True
    assert answer["ops"][0]["op"] == "server.info"


def test_the_synonym_sunset_lives_in_one_named_constant():
    assert verbs.SYNONYM_REMOVAL_DATE == "2026-09-30"
    assert call(help=True)["synonyms_removed_after"] == verbs.SYNONYM_REMOVAL_DATE


# ── argv rendering ───────────────────────────────────────────────────────────


def test_a_spawn_verb_renders_the_tokens_the_cli_parser_declares(submitted):
    answer = call(
        ops=[
            spawn_op(
                "agent.submit",
                {
                    "query": ["claude/opus"],
                    "prompt": "hello",
                    "agent": "implementer",
                    "yolo": True,
                    "timeout": 900,
                    "image": ["/a.png", "/b.png"],
                    "label": "probe",
                    "notify_seat": "seat",
                },
            )
        ]
    )
    assert answer["ops"][0]["ok"] is True
    assert submitted["kind"] == "agent"
    # The model spec is the trailing positional, as on the command line.
    assert submitted["flags"][-1] == "claude/opus"
    # A flag and its value are one token, so the value cannot be read as an
    # option by the parser or by anything scanning argv ahead of it.
    assert submitted["flags"][0] == "--agent=implementer"
    assert "--yolo" in submitted["flags"]
    assert sum(f.startswith("--image=") for f in submitted["flags"]) == 2
    # The server owns the prompt and the notify wiring; neither reaches argv.
    assert submitted["prompt"] == "hello"
    assert "--prompt" not in submitted["flags"]
    assert "--notify" not in submitted["flags"]
    assert submitted["label"] == "probe"
    assert submitted["notify_target"] == "seat"


def test_a_boolean_only_reaches_argv_when_it_differs_from_the_parser_default(submitted):
    call(ops=[spawn_op("agent.submit", {"query": ["m"], "yolo": False})])
    assert "--yolo" not in submitted["flags"]


def test_each_spawn_verb_reaches_its_own_run_kind(submitted):
    for verb, kind in (
        ("agent.submit", "agent"),
        ("flow.submit", "flow"),
        ("fanout.submit", "fanout"),
    ):
        call(ops=[spawn_op(verb, {"query": ["m", "do it"]})])
        assert submitted["kind"] == kind


# ── playbooks resolve in two stages ──────────────────────────────────────────


def test_base_help_says_a_playbook_declares_further_arguments():
    schema = call(help="play.submit")["schema"]
    assert "x-playbook-arguments" in schema


def test_naming_a_playbook_that_does_not_exist_is_an_error():
    with pytest.raises(ValueError):
        call(help={"verb": "play.submit", "playbook": "no-such-playbook-anywhere"})


def test_a_verb_with_no_playbook_stage_refuses_one():
    with pytest.raises(ValueError, match="takes no playbook"):
        call(help={"verb": "agent.submit", "playbook": "anything"})


# ── the long tail runs as a subprocess and returns a versioned envelope ──────


def test_a_machine_verb_returns_the_contract_envelope_it_was_given():
    answer = call(ops=[{"op": "handshake"}])
    result = answer["ops"][0]["result"]
    assert result["contract_version"] >= 1
    assert result["data"]["implementation"] == "lionagi"


def test_a_machine_verb_that_writes_no_result_is_an_explicit_error(monkeypatch):
    # Absent output must never read as an empty success: a caller that treats it
    # as one concludes the command answered and found nothing.
    monkeypatch.setattr(dispatch.config, "li_command", lambda: ["true"])
    answer = call(ops=[{"op": "handshake"}])
    assert answer["ops"][0]["ok"] is False
    assert "no machine result" in answer["ops"][0]["error"]["message"]


def test_a_machine_verb_that_writes_something_other_than_json_is_an_error(monkeypatch):
    monkeypatch.setattr(dispatch.config, "li_command", lambda: ["echo", "not json at all"])
    answer = call(ops=[{"op": "handshake"}])
    assert answer["ops"][0]["ok"] is False
    assert "one JSON value" in answer["ops"][0]["error"]["message"]


def test_a_machine_command_that_cannot_be_launched_is_an_error(monkeypatch):
    monkeypatch.setattr(
        dispatch.config, "li_command", lambda: ["/nonexistent/li-that-is-not-installed"]
    )
    answer = call(ops=[{"op": "handshake"}])
    assert answer["ops"][0]["ok"] is False
    assert "could not launch" in answer["ops"][0]["error"]["message"]


def test_a_refusal_from_the_machine_command_keeps_its_kind(monkeypatch):
    envelope = json.dumps(
        {
            "ok": False,
            "contract_version": 1,
            "data": None,
            "error": {"kind": "not_found", "message": "nothing here", "detail": None},
        }
    )
    # A stub that ignores the trailing command path the dispatcher appends and
    # writes only the envelope, which is the channel contract being tested.
    monkeypatch.setattr(
        dispatch.config, "li_command", lambda: [sys.executable, "-c", f"print({envelope!r})"]
    )
    answer = call(ops=[{"op": "handshake"}])
    assert answer["ops"][0]["error"]["kind"] == "not_found"


# ── response conventions ─────────────────────────────────────────────────────


def test_every_result_is_json_serializable_machine_data():
    answer = call(ops=[{"op": "server.info"}, {"op": "job.list", "args": {"limit": 1}}])
    json.dumps(answer)  # raises if anything humanized or exotic crept in


def test_server_info_reports_one_advertised_tool():
    info = call(ops=[{"op": "server.info"}])["ops"][0]["result"]
    assert info["tool_count"] == 1
    assert info["verb_count"] == len(verbs.VERBS)
    assert info["absent_verb_count"] == len(verbs.ABSENT)


# ── the spawn fingerprint ────────────────────────────────────────────────────
#
# Collapsing the surface to one tool makes discovery a call; it does not make
# discovery happen. These pin what the requirement does and does not establish.


def test_help_for_a_spawn_verb_returns_a_fingerprint(submitted):
    answer = call(help="agent.submit")
    assert answer["schema_fingerprint"]
    assert answer["schema_fingerprint"] == call(help="agent.submit")["schema_fingerprint"]


@pytest.mark.parametrize("verb", ["job.status", "job.wait", "job.kill", "server.info"])
def test_a_verb_that_is_not_a_spawn_neither_offers_nor_demands_one(verb):
    # The kill path is the deliberate exemption: a discovery round-trip in front
    # of stopping a runaway run is friction at the moment it is most expensive.
    assert "schema_fingerprint" not in call(help=verb)
    answer = call(ops=[{"op": verb, "args": {"run_id": "nope"} if verb != "server.info" else {}}])
    error = answer["ops"][0].get("error") or {}
    assert error.get("kind") != "stale_schema"


def test_a_spawn_op_without_a_fingerprint_is_refused_with_the_call_that_fixes_it(submitted):
    answer = call(ops=[{"op": "agent.submit", "args": {"query": ["m"]}}])
    error = answer["ops"][0]["error"]
    assert error["kind"] == "stale_schema"
    assert error["detail"]["help"] == "agent.submit"
    assert error["detail"]["schema_fingerprint"] == call(help="agent.submit")["schema_fingerprint"]
    assert submitted == {}


def test_a_stale_fingerprint_is_refused_and_says_it_is_the_schema_that_moved(submitted):
    answer = call(
        ops=[
            {
                "op": "agent.submit",
                "args": {"query": ["m"]},
                "schema_fingerprint": "0000000000000000",
            }
        ]
    )
    error = answer["ops"][0]["error"]
    assert error["kind"] == "stale_schema"
    assert "changed since that schema was read" in error["message"]
    assert submitted == {}


def test_the_fingerprint_follows_the_schema_it_describes():
    # A fingerprint that did not move when the parameters moved would let a caller
    # validate against one shape and run another, which is the only thing this
    # mechanism actually guarantees.
    schema = call(help="agent.submit")["schema"]
    moved = json.loads(json.dumps(schema))
    moved["properties"]["a_parameter_that_did_not_exist"] = {"type": "string"}
    assert dispatch.schema_fingerprint(moved) != dispatch.schema_fingerprint(schema)


def test_the_fingerprint_is_not_a_claim_that_anyone_read_the_schema(submitted):
    # Written down as a test because the ADR states the limit and a reader of the
    # code should meet it here too: the value is transferable, so a caller who
    # inherited it from a prompt template passes exactly like one who fetched it.
    inherited = call(help="flow.submit")["schema_fingerprint"]
    answer = call(
        ops=[
            {
                "op": "flow.submit",
                "args": {"query": ["m", "do it"]},
                "schema_fingerprint": inherited,
            }
        ]
    )
    assert answer["ops"][0]["ok"] is True
