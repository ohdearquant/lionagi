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
from pathlib import Path

import pytest

from lionagi._spec_limits import MAX_SPEC_PROMPT_CHARS
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
    """Capture what a spawn verb hands the job engine; nothing is spawned.

    The profile resolver is stubbed to accept any name: these tests are about
    dispatch mechanics (positionals, cwd, prompts), not about which profile
    files happen to exist on the machine running them — that resolution is
    covered on its own in test_roster.py and test_admission_*.py.
    """
    import lionagi.cli._providers as providers

    monkeypatch.setattr(providers, "load_agent_profile", lambda name: None)
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


def test_agent_submit_schema_discloses_the_prompt_admission_cap():
    schema = call(help="agent.submit")
    prompt = schema["schema"]["properties"]["prompt"]
    prompt_file = schema["schema"]["properties"]["prompt_file"]

    assert prompt["maxLength"] == MAX_SPEC_PROMPT_CHARS
    assert str(MAX_SPEC_PROMPT_CHARS) in prompt_file["description"]


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


def test_a_relative_cwd_is_recorded_as_the_directory_it_was_checked_against(
    tmp_path, monkeypatch, submitted
):
    """A caller-relative cwd is stored absolute.

    The directory check already answers about the submitting process's own
    directory, so a relative path that passes it and is then stored unresolved is
    validated against one directory and later read against another.
    """
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(tmp_path)

    answer = call(
        ops=[
            spawn_op(
                "agent.submit",
                {"prompt": "hi", "agent": "implementer", "cwd": project.name},
            )
        ]
    )

    assert answer["ops"][0]["ok"] is True
    assert Path(submitted["cwd"]).is_absolute()
    assert Path(submitted["cwd"]) == project.resolve()


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
    # No fingerprint is sent, deliberately: for a verb that requires a playbook and
    # was given none, the missing playbook is the error the caller can act on, and
    # the fingerprint gate must not preempt it with a complaint about a value help
    # declines to hand out.
    answer = call(ops=[{"op": "play.submit", "args": {}}])
    assert "missing required parameter 'playbook'" in answer["ops"][0]["error"]["message"]
    assert submitted == {}


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


@pytest.fixture
def a_playbook(tmp_path, monkeypatch):
    """A playbook declaring one argument of its own, resolvable from cwd.

    Project-local rather than in the user's real playbook directory: a run dying
    between write and cleanup would otherwise leave a fixture behind where a real
    playbook of the same name could collide with it.
    """
    directory = tmp_path / ".lionagi" / "playbooks"
    directory.mkdir(parents=True)
    (directory / "remedy-fixture.playbook.yaml").write_text(
        json.dumps(
            {
                "name": "remedy-fixture",
                "description": "fixture for the fingerprint remedy",
                "prompt": "act on {subject}",
                "args": {"subject": {"type": "str", "default": ".", "help": "what to act on"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return "remedy-fixture"


@pytest.fixture
def invalid_playbook(tmp_path, monkeypatch):
    directory = tmp_path / ".lionagi" / "playbooks"
    directory.mkdir(parents=True)
    (directory / "invalid-spec.playbook.yaml").write_text(
        json.dumps(
            {
                "name": "invalid-spec",
                "model": "claude-code/opus-4-7",
                "prompt": "do the work",
                "max-ops": 64,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return "invalid-spec"


def test_play_submit_refuses_invalid_spec_before_job_creation(invalid_playbook, submitted):
    qualified = call(help={"verb": "play.submit", "playbook": invalid_playbook})
    answer = call(
        ops=[
            {
                "op": "play.submit",
                "args": {"playbook": invalid_playbook},
                "schema_fingerprint": qualified["schema_fingerprint"],
            }
        ]
    )

    op = answer["ops"][0]
    assert op["ok"] is False
    assert op["error"]["kind"] == "invalid_input"
    assert "max_ops" in op["error"]["message"]
    assert "[0, 50]" in op["error"]["message"]
    assert "64" in op["error"]["message"]
    assert "run_id" not in op
    assert submitted == {}


def test_a_refused_playbook_call_is_told_to_ask_help_for_that_playbook(a_playbook, submitted):
    """The remedy has to name the playbook, or a caller who re-reads it loops.

    A playbook's own arguments are part of the schema, so the fingerprint the
    refusal quotes is qualified by the playbook. A `help` pointer naming the verb
    alone sends a caller who re-fetches to the argument-free schema, whose
    fingerprint this same call then refuses — the remedy would return the caller
    to the error it is answering.
    """
    answer = call(ops=[{"op": "play.submit", "args": {"playbook": a_playbook}}])
    error = answer["ops"][0]["error"]
    assert error["kind"] == "stale_schema"
    assert error["detail"]["help"] == {"verb": "play.submit", "playbook": a_playbook}
    assert a_playbook in error["message"]
    # Following the remedy exactly must produce a call this verb accepts, which is
    # the property the pointer exists for.
    qualified = call(help={"verb": "play.submit", "playbook": a_playbook})
    assert error["detail"]["schema_fingerprint"] == qualified["schema_fingerprint"]
    assert error["detail"]["schema_fingerprint"] != call(help="play.submit").get(
        "schema_fingerprint"
    )
    assert submitted == {}


def test_a_stale_playbook_fingerprint_is_told_the_same_qualified_source(a_playbook, submitted):
    # The base fingerprint is the wrong-schema value a caller most plausibly
    # arrives with, so it is the one worth pinning on the stale branch.
    base = call(help="play.submit").get("schema_fingerprint") or "0000000000000000"
    answer = call(
        ops=[
            {
                "op": "play.submit",
                "args": {"playbook": a_playbook},
                "schema_fingerprint": base,
            }
        ]
    )
    error = answer["ops"][0]["error"]
    assert error["kind"] == "stale_schema"
    assert error["detail"]["help"] == {"verb": "play.submit", "playbook": a_playbook}
    assert submitted == {}


def test_targeted_help_withholds_a_fingerprint_no_successful_call_can_carry():
    """`play.submit` requires a playbook, so its argument-free fingerprint is dead.

    Every *successful* `play.submit` op names a playbook and resolves a
    schema that is not the argument-free one -- that fingerprint is accepted
    only by a call that omits the playbook, which then fails validation, so
    its sole effect is to buy a round-trip on the way to a different error.
    The catalog withheld it while targeted help returned it, so one contract
    had two answers depending on which way you asked. Withholding it is not
    silence: the answer names the parameter the fingerprint varies with, so
    the caller knows to ask again with a playbook rather than retry with a
    stale string.
    """
    answer = call(help="play.submit")
    assert "schema_fingerprint" not in answer
    assert answer["schema_fingerprint_varies_with"] == ["playbook"]
    # The catalog says the same thing about the same verb.
    entry = {e["verb"]: e for e in call(help=True)["verbs"]}["play.submit"]
    assert "schema_fingerprint" not in entry
    assert entry["schema_fingerprint_varies_with"] == ["playbook"]


def test_targeted_help_quotes_the_fingerprint_once_the_playbook_is_named(a_playbook):
    # The suppression above must be about the missing argument, not about the verb:
    # naming the playbook resolves it, so the value is real and is quoted.
    answer = call(help={"verb": "play.submit", "playbook": a_playbook})
    assert answer["schema_fingerprint"]
    assert answer["schema_fingerprint"] != schema_fingerprint_of_base_play_submit()


def schema_fingerprint_of_base_play_submit() -> str:
    return dispatch.schema_fingerprint(dispatch.verb_schema(verbs.VERBS["play.submit"]))


def test_an_optional_playbook_still_quotes_its_argument_free_fingerprint():
    # `flow.submit` takes a playbook without requiring one, so the argument-free
    # schema is a real call and its fingerprint is usable. Pinned so the
    # suppression above cannot widen into every playbook-aware verb.
    answer = call(help="flow.submit")
    assert answer["schema_fingerprint"]
    assert answer["schema_fingerprint_varies_with"] == ["playbook"]


def test_a_playbook_aware_call_naming_none_is_pointed_at_the_base_schema(submitted):
    # `flow.submit` takes a playbook but does not require one, so the
    # argument-free schema is a real call and its own fingerprint is the right
    # answer here. Pinned so the qualified case above cannot be satisfied by
    # always naming a playbook.
    answer = call(ops=[{"op": "flow.submit", "args": {"query": ["m", "do it"]}}])
    error = answer["ops"][0]["error"]
    assert error["kind"] == "stale_schema"
    assert error["detail"]["help"] == {"verb": "flow.submit"}
    assert "playbook" not in error["detail"]["help"]
    assert submitted == {}


def test_no_refusal_points_at_a_help_call_that_returns_no_fingerprint(submitted):
    """Every fingerprint refusal's pointer must lead somewhere that answers it.

    A refusal that says "ask help=X" while X withholds the fingerprint is a dead
    end, and it is the failure the withholding above would introduce if the gate
    still fired for a verb whose required playbook is missing. So the two are
    checked against each other rather than separately: follow every reachable
    pointer and require a fingerprint at the other end.
    """
    subjects = [
        {"op": "agent.submit", "args": {"query": ["m"]}},
        {"op": "flow.submit", "args": {"query": ["m", "do it"]}},
        {"op": "fanout.submit", "args": {"query": ["m", "do it"]}},
        {"op": "play.submit", "args": {}},
    ]
    seen = 0
    for op in subjects:
        error = call(ops=[op])["ops"][0].get("error") or {}
        if error.get("kind") != "stale_schema":
            continue
        seen += 1
        pointer = error["detail"]["help"]
        answer = call(help=pointer)
        assert "schema_fingerprint" in answer, (
            f"{op['op']} is refused and told to ask help={pointer!r}, which returns no "
            "fingerprint — the remedy leads nowhere"
        )
        assert answer["schema_fingerprint"] == error["detail"]["schema_fingerprint"]
    assert seen, "no op was refused for its fingerprint — this check read nothing"
    assert submitted == {}


# ── the long tail runs as a subprocess and returns a versioned envelope ──────


def test_a_machine_verb_returns_the_contract_envelope_it_was_given():
    answer = call(ops=[{"op": "handshake"}])
    result = answer["ops"][0]["result"]
    assert result["contract_version"] >= 1
    assert result["data"]["implementation"] == "lionagi"


def test_team_send_and_receive_round_trip_through_the_dispatcher(tmp_path, monkeypatch):
    # `team.send`/`team.receive` used to be `AbsentVerb`s with no machine seam;
    # this is the round trip that seam now has to carry, through the same
    # `request()` entry point every op goes through — not the CLI directly.
    monkeypatch.setenv("LIONAGI_HOME", str(tmp_path))
    created = call(ops=[{"op": "team.create", "args": {"name": "Squad", "members": "a,b"}}])
    assert created["ops"][0]["ok"] is True
    team_id = created["ops"][0]["result"]["data"]["id"]

    sent = call(ops=[{"op": "team.send", "args": {"content": "hi", "team": team_id, "to": "all"}}])
    assert sent["ops"][0]["ok"] is True
    assert sent["ops"][0]["result"]["data"]["team_id"] == team_id

    received = call(ops=[{"op": "team.receive", "args": {"team": team_id, "member": "a"}}])
    assert received["ops"][0]["ok"] is True
    assert [m["content"] for m in received["ops"][0]["result"]["data"]["messages"]] == ["hi"]


def test_team_send_to_an_unknown_team_is_a_not_found_error_via_the_dispatcher(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LIONAGI_HOME", str(tmp_path))
    answer = call(
        ops=[{"op": "team.send", "args": {"content": "hi", "team": "no-such-team", "to": "all"}}]
    )
    assert answer["ops"][0]["ok"] is False
    assert answer["ops"][0]["error"]["kind"] == "not_found"


def test_team_create_with_bad_input_is_refused_before_ever_spawning_the_cli(tmp_path, monkeypatch):
    # Closed argument validation catches this before `_run_machine` spawns a
    # subprocess at all — the schema admits only `name`/`members`.
    monkeypatch.setenv("LIONAGI_HOME", str(tmp_path))
    answer = call(ops=[{"op": "team.create", "args": {"name": "Squad"}}])
    assert answer["ops"][0]["ok"] is False
    assert answer["ops"][0]["error"]["kind"] == "invalid_input"
    assert "missing required parameter 'members'" in answer["ops"][0]["error"]["message"]


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


def test_agent_submit_rejects_inline_prompt_over_shared_limit_before_spawn(submitted):
    answer = call(
        ops=[
            spawn_op(
                "agent.submit",
                {"query": ["codex"], "prompt": "x" * (MAX_SPEC_PROMPT_CHARS + 1)},
            )
        ]
    )["ops"][0]

    assert answer["ok"] is False
    assert answer["error"]["kind"] == "invalid_input"
    assert str(MAX_SPEC_PROMPT_CHARS) in answer["error"]["message"]
    assert submitted == {}


def test_agent_submit_rejects_prompt_file_over_shared_limit_before_spawn(submitted, tmp_path):
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("x" * (MAX_SPEC_PROMPT_CHARS + 1))

    answer = call(
        ops=[
            spawn_op(
                "agent.submit",
                {"query": ["codex"], "prompt_file": str(prompt_file)},
            )
        ]
    )["ops"][0]

    assert answer["ok"] is False
    assert answer["error"]["kind"] == "invalid_input"
    assert str(MAX_SPEC_PROMPT_CHARS) in answer["error"]["message"]
    assert submitted == {}


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
