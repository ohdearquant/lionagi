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
    absent = entries["schedule.apply"]
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
    answer = call(help="state.doctor")
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


# ── help and ops are separate calls ──────────────────────────────────────────
#
# The two answers have different shapes, so there is no reply that could carry
# both. Running the ops and dropping the catalog, or the reverse, would look
# like success to a caller whose other half never happened — so the pair is
# refused by name instead.


def test_help_alongside_ops_is_refused_and_names_both_parameters():
    with pytest.raises(ValueError) as refusal:
        call(help=True, ops=[{"op": "server.info"}])
    message = str(refusal.value)
    assert "help" in message and "ops" in message


def test_help_alongside_ops_refuses_before_any_op_runs(submitted):
    with pytest.raises(ValueError):
        call(
            help=True,
            ops=[spawn_op("agent.submit", {"prompt": "hi", "agent": "implementer"})],
        )
    assert submitted == {}


def test_help_alone_still_answers_with_the_catalog():
    answer = call(help=True)
    assert answer["verbs"]
    assert "ops" not in answer


def test_ops_alone_still_run():
    answer = call(ops=[{"op": "server.info"}])
    assert answer["status"] == "success"
    assert answer["ops"][0]["ok"] is True


def test_help_with_an_empty_ops_list_is_a_plain_help_call():
    assert call(help=True, ops=[]) == call(help=True)


@pytest.mark.parametrize("bad", [{}, {"op": "server.info"}, "server.info"])
def test_help_alongside_a_malformed_ops_reports_the_wrong_type(bad):
    """A malformed ops is judged on its shape, not on whether it is truthy.

    Deciding by truthiness split these three: the empty dict was falsey, so it
    was read as "no ops" and dropped in silence, which is the very thing this
    refusal exists to prevent. The other two were truthy and came back blamed on
    the help conflict rather than on being the wrong type.
    """
    with pytest.raises(ValueError, match="ops is a list"):
        call(help=True, ops=bad)


@pytest.mark.parametrize("bad", [{}, {"op": "server.info"}, "server.info"])
def test_a_malformed_ops_reports_the_same_way_with_and_without_help(bad):
    """Whether help was asked for cannot change what a wrong type is called."""
    with pytest.raises(ValueError, match="ops is a list") as with_help:
        call(help=True, ops=bad)
    with pytest.raises(ValueError, match="ops is a list") as without_help:
        call(ops=bad)
    assert str(with_help.value) == str(without_help.value)


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


def test_a_json_encoded_flag_reaches_the_parser_encoded():
    # The parser decodes this flag's single token from JSON, so the schema
    # advertises the decoded shape while the rendered token has to be the
    # encoding of it. Those are two halves of one contract held in two files:
    # a test of the projection alone, or of the parser alone, passes while the
    # round-trip is broken and every caller gets an unusable command line.
    schema = call(help="schedule.create")["schema"]
    assert schema["properties"]["action_command_args"]["x-json-encoded"] is True

    argv = dispatch.render_argv(
        schema, {"name": "n", "action_command_args": ["review-pr", "--repo", "{{r}}"]}
    )

    flag = next(t for t in argv if t.startswith("--action-command-args"))
    # One token, so the values cannot be read as further options.
    assert flag == f"--action-command-args={json.dumps(['review-pr', '--repo', '{{r}}'])}"
    assert json.loads(flag.split("=", 1)[1]) == ["review-pr", "--repo", "{{r}}"]


def test_a_flag_a_detached_run_cannot_honour_is_refused_with_its_reason(submitted):
    # Accepting it and dropping it would leave the caller believing it applied.
    answer = call(ops=[spawn_op("agent.submit", {"verbose": True})])
    assert "job.output" in answer["ops"][0]["error"]["message"]


def test_a_missing_required_parameter_names_itself(submitted):
    answer = call(ops=[spawn_op("play.submit", {})])
    assert "missing required parameter 'playbook'" in answer["ops"][0]["error"]["message"]


def test_a_refusal_on_a_synchronous_verb_does_not_blame_a_background_run():
    """The reason a parameter is declined has to match the verb it was passed to.

    Every refusal used to be on a spawn verb, where "nobody is attached to the
    terminal" explains all of them, so the message said so in general terms.
    `dispatch purge` is synchronous: a caller told its parameter was refused
    because the run is detached would go looking for a background run they never
    started.
    """
    answer = call(ops=[{"op": "dispatch.purge", "args": {"id": "d1", "status": "dead_letter"}}])
    message = answer["ops"][0]["error"]["message"]
    assert "'status' is not accepted here" in message
    assert "background run" not in message
    # And the reason travels with it, so the caller learns what to do instead.
    assert "purge one id" in message


def test_a_refusal_on_a_detached_verb_still_says_the_run_is_detached(submitted):
    answer = call(ops=[spawn_op("agent.submit", {"verbose": True})])
    assert "not accepted on a background run" in answer["ops"][0]["error"]["message"]


def test_the_queue_sweep_is_refused_by_name_rather_than_left_undeclared():
    """An unadmitted parameter and a refused one read very differently to a caller.

    Dropping `--status`/`--before` from `admits` alone would report them as
    unknown, and they are not unknown: they exist on the command, they are spelled
    correctly, and they are declined. A caller told "unknown parameter" looks for
    a typo instead of reading why.
    """
    schema = dispatch.verb_schema(verbs.VERBS["dispatch.purge"])
    assert sorted(schema["properties"]) == ["dry_run", "id"]
    assert sorted(schema["x-refused"]) == ["before", "status"]
    # The parser leaves `id` optional because omitting it is how a terminal asks
    # for a sweep. Here an absent id can never succeed, so the schema says so
    # rather than letting the caller make the call and find out.
    assert schema["required"] == ["id"]


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
    call(ops=[spawn_op("agent.submit", {"query": ["m"], "prompt": "do it", "yolo": False})])
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


def test_a_success_envelope_beside_a_non_zero_exit_is_not_a_success(monkeypatch):
    """Two channels contradicting each other is not an answer.

    A command that speaks this contract exits 0 whenever it emitted an envelope,
    so a success envelope from a child that exited non-zero says the child is not
    speaking it. Nothing here can tell which channel is right, and reporting the
    envelope means a caller reads a crash as a result.
    """
    envelope = json.dumps({"ok": True, "contract_version": 1, "data": {"x": 1}, "error": None})
    monkeypatch.setattr(
        dispatch.config,
        "li_command",
        lambda: [sys.executable, "-c", f"print({envelope!r}); raise SystemExit(7)"],
    )
    answer = call(ops=[{"op": "handshake"}])
    assert answer["ops"][0]["ok"] is False
    assert "exited 7" in answer["ops"][0]["error"]["message"]


def test_a_refusal_envelope_beside_a_non_zero_exit_keeps_its_own_error(monkeypatch):
    """The complement: there the channels agree, so the envelope says more."""
    envelope = json.dumps(
        {
            "ok": False,
            "contract_version": 1,
            "data": None,
            "error": {"kind": "not_found", "message": "nothing here", "detail": None},
        }
    )
    monkeypatch.setattr(
        dispatch.config,
        "li_command",
        lambda: [sys.executable, "-c", f"print({envelope!r}); raise SystemExit(3)"],
    )
    answer = call(ops=[{"op": "handshake"}])
    assert answer["ops"][0]["error"]["kind"] == "not_found"


@pytest.mark.parametrize("bad", [[], "", False, 0, 0.0, "args"], ids=repr)
def test_args_that_is_not_an_object_is_refused_even_when_it_is_falsey(bad):
    """A falsey non-object used to become `{}` before its type was ever checked.

    The type check below it was unreachable for exactly the values a caller is
    most likely to send by mistake, so the op ran with the caller's input
    silently discarded and reported success — which is the answer closed
    validation exists to make impossible.
    """
    answer = call(ops=[{"op": "job.list", "args": bad}])
    assert answer["ops"][0]["ok"] is False
    assert answer["ops"][0]["error"]["kind"] == "invalid_input"


@pytest.mark.parametrize("absent", [{"op": "job.list"}, {"op": "job.list", "args": None}])
def test_no_arguments_may_be_spelled_as_absent_or_null(absent):
    assert call(ops=[absent])["ops"][0]["ok"] is True


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


# ── a run that could not start ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("op", "args"),
    [
        ("agent.submit", {"prompt": "do it"}),
        ("agent.submit", {"query": ["do it"]}),
    ],
)
def test_a_submission_with_no_model_is_refused_instead_of_handed_a_handle(submitted, op, args):
    # Every spawning command refuses to start without a model, and it refuses
    # after its own startup — so a submission that reached the spawn came back
    # describing a started run, with a pid, while the run was already over. Such
    # a run never reaches the terminal hook, so it never becomes terminal and no
    # terminal notice is ever delivered: a caller waiting for one waits forever.
    # What the caller is told AT SUBMIT is the subject here, so the assertion is
    # on the submit result and not on the job record.
    answer = call(ops=[spawn_op(op, args)])["ops"][0]
    assert answer["ok"] is False
    assert answer["error"]["kind"] == "invalid_input"
    assert "no model" in answer["error"]["message"]
    # The refusal names the schema it judged against, as every other one does.
    assert answer["error"]["schema"]["title"] == op
    # Nothing was spawned, so there is no run_id for a caller to go on waiting on.
    assert submitted == {}
    assert "run_id" not in answer


@pytest.mark.parametrize(
    ("op", "args"),
    [
        ("agent.submit", {"query": ["a-model"], "prompt": "do it"}),
        ("agent.submit", {"agent": "a-profile", "prompt": "do it"}),
        ("flow.submit", {"query": ["a-model"], "prompt": "do it"}),
        ("flow.submit", {"query": ["a-model", "do it"]}),
        ("flow.submit", {"agent": "a-profile", "prompt": "do it"}),
        ("fanout.submit", {"agent": "a-profile", "prompt": "do it"}),
    ],
)
def test_a_submission_that_names_a_model_still_spawns(submitted, op, args):
    # The refusal above is conservative on purpose: it fires only where no source
    # of a model exists at all. This pins the other side of that line, so a
    # tightening that started refusing ordinary submissions is caught here.
    answer = call(ops=[spawn_op(op, args)])["ops"][0]
    assert answer["ok"] is True
    assert submitted["kind"]


def test_a_spec_file_may_be_the_thing_that_names_the_model(submitted):
    # A flow spec declares the orchestrator in content the server does not read.
    # Refusing it would reject valid submissions, so its presence hands the
    # question back to the command. A playbook is the same case and takes the
    # same branch, but naming one here would require it to exist on the machine.
    answer = call(ops=[spawn_op("flow.submit", {"file": "/tmp/spec.yaml"})])["ops"][0]
    assert answer["ok"] is True, answer


def _refusal(op: str) -> str:
    return call(ops=[spawn_op(op, {"prompt": "do it"})])["ops"][0]["error"]["message"]


def _sources(kind: str) -> str:
    """The remediation a refusal of this kind would quote.

    Read from the table rather than from a refusal for the orchestrating
    commands, which answer a submission naming nothing with the default
    orchestrator profile and so cannot reach this refusal at all. What the text
    says is still worth holding: it is what a caller of any future refusal reads.
    """
    return dispatch._MODEL_SOURCES[kind]


def test_the_remediation_names_only_sources_the_verb_it_was_sent_to_accepts(submitted):
    """A fix a caller cannot follow costs them the round-trip it was meant to save.

    `fanout` takes neither a spec file nor a playbook, so a message that offered
    either would be answered by a second refusal, this time from argument
    validation, on a call the caller made because the first refusal told them to.
    Both halves are asserted together: what each message names, and what the
    receiving schema actually admits.
    """
    fanout, flow = _sources("fanout"), _sources("flow")
    assert "'file'" not in fanout and "'playbook'" not in fanout
    assert "'file'" in flow and "'playbook'" in flow

    fanout_admits = set(call(help="fanout.submit")["schema"]["properties"])
    flow_admits = set(call(help="flow.submit")["schema"]["properties"])
    assert not {"file", "playbook"} & fanout_admits
    assert {"file", "playbook"} <= flow_admits
    # A profile is the one source all three share, so every message names it.
    assert "'agent'" in fanout and "'agent'" in flow and "'agent'" in _refusal("agent.submit")


def test_the_remediation_says_where_in_the_positionals_the_model_goes(submitted):
    """Where the model sits differs by command, so each message says its own answer.

    Every one of these commands reads a lone positional as the prompt, so a
    caller who passes the model on its own has passed a prompt — each message
    has to say where the prompt goes instead, or it describes a call still
    missing a model. The agent's prompt travels separately, so its message names
    the parameter that carries it as well as the second positional.
    """
    for kind in ("fanout", "flow"):
        message = _sources(kind)
        assert "with the prompt after it" in message, message
        assert "read as the prompt" in message, message
    agent = _refusal("agent.submit")
    assert "first value of 'query'" in agent, agent
    assert "the prompt in 'prompt' or as a second value" in agent, agent
    assert "read as the prompt" in agent, agent


def test_every_spawning_command_has_its_own_model_sources(submitted):
    """A new spawn kind must arrive with the remediation its refusal will quote.

    The sources are per command, so the registry and the table are two lists of
    the same commands kept in separate files. Nothing else holds them together:
    add a spawning verb and the refusal for it falls back to a message that
    names no argument at all, which is the least a caller can act on. This is
    the check that says so at authoring time instead.
    """
    registered = {v.job_kind for v in verbs.VERBS.values() if v.executor == "spawn"}
    assert registered, "no spawning verb is registered; this check would pass vacuously"
    assert registered <= set(dispatch._MODEL_SOURCES), sorted(
        registered - set(dispatch._MODEL_SOURCES)
    )
    # Each entry must also survive the refusal it is quoted in, so a stale entry
    # for a kind no longer registered is reported rather than left to rot.
    assert set(dispatch._MODEL_SOURCES) <= registered, sorted(
        set(dispatch._MODEL_SOURCES) - registered
    )


def test_a_command_whose_kind_the_table_does_not_name_is_still_refused_as_a_result(
    submitted, monkeypatch
):
    """An unlisted kind is a client input error, not a server fault.

    Indexing the sources table by kind makes a kind it does not name an
    exception out of dispatch, which reaches the caller as an internal failure
    and tells them their submission was fine. It was not: it carries no model
    and the run would die on start. So it is the ordinary refusal, and it still
    has to name a correction the caller can make: one assembled from arguments
    the command itself declares, so acting on it cannot land in a second
    refusal from argument validation.
    """
    probe = verbs.Verb(
        name="probe.submit",
        summary="A spawning verb whose kind the sources table does not name.",
        executor="spawn",
        cli_path="orchestrate fanout",
        job_kind="probe",
        server_params=verbs._SPAWN_SERVER_PARAMS,
    )
    monkeypatch.setattr(dispatch, "VERBS", {**verbs.VERBS, probe.name: probe})
    answer = call(ops=[spawn_op("probe.submit", {"prompt": "do it"})])["ops"][0]
    assert answer["ok"] is False, answer
    assert answer["error"]["kind"] == "invalid_input", answer
    message = answer["error"]["message"]
    assert "has no model and nothing to supply one" in message
    # The caller has to be able to write the corrected request from this. It
    # says the sources are not recorded for this command, and then names the
    # arguments that both satisfy the check and appear in this command's own
    # schema, so sending one of them cannot be refused as an unknown parameter.
    assert "no model sources recorded for the 'probe' command" in message, message
    declared = dispatch.verb_schema(probe)["properties"]
    assert {"query", "agent"} <= set(declared), sorted(declared)
    assert "first value of 'query'" in message, message
    assert "name a profile with 'agent'" in message, message
    # 'file' and 'playbook' are model sources the check accepts but this command
    # does not declare, so they are not offered.
    assert "file" not in declared and "playbook" not in declared, sorted(declared)
    assert "'file'" not in message and "'playbook'" not in message, message
    # Nothing was spawned: the point of refusing here is that no run is started.
    assert submitted == {}


def test_an_unlisted_kind_declaring_no_model_argument_says_so_instead_of_guessing(
    submitted, monkeypatch
):
    """With nothing to name, the refusal names the gap rather than an argument.

    The correction is only as good as the arguments it can be assembled from. A
    command declaring none of them leaves nothing true to say about where a
    model goes, and a reassuring sentence there would be the guess the
    per-command sources exist to avoid.
    """
    probe = verbs.Verb(
        name="opaque.submit",
        summary="A spawning verb declaring none of the arguments the check reads.",
        executor="spawn",
        own_schema={"type": "object", "properties": {}, "additionalProperties": False},
        job_kind="opaque",
    )
    monkeypatch.setattr(dispatch, "VERBS", {**verbs.VERBS, probe.name: probe})
    answer = call(ops=[spawn_op("opaque.submit", {})])["ops"][0]
    assert answer["ok"] is False, answer
    message = answer["error"]["message"]
    assert "declares no argument this check reads as one" in message, message
    assert "per-command model sources" in message, message
    assert submitted == {}


def test_job_list_carries_the_delivery_state_out_to_the_caller(monkeypatch, tmp_path):
    """The verb hands the listing back whole, delivery state included.

    A field the job engine adds and the verb layer then drops is a change that
    ships and does nothing, so the property is asserted at the surface a caller
    actually reads rather than one layer in.
    """
    from lionagi.mcp import config

    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    rid = jobs.new_run_id()
    jobs._write_job(
        {"run_id": rid, "status": "completed", "kind": "agent", "pid": None, "log": None}
    )
    jobs.record_notify_delivery(
        rid, {"attempted": True, "ok": False, "exit_code": 1, "error": None, "command": "notify"}
    )

    answer = call(ops=[{"op": "job.list", "args": {"limit": 5}}])

    listed = answer["ops"][0]["result"]["jobs"]
    assert [j["run_id"] for j in listed] == [rid]
    assert listed[0]["notify_delivery_state"] == "failed"
