# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The observability verbs, end to end through the real subprocess.

Nothing here mocks the child. The whole point of this surface is the composition
between the argv the server renders and what the CLI's own parser makes of it,
so a test that stubbed the subprocess would assert on the one half that cannot
be wrong. Each test spawns the real ``li``, with ``LIONAGI_HOME`` pointed at a
temporary directory so what it reads is a store this test made.
"""

from __future__ import annotations

import asyncio
import json
import subprocess

import pytest

from lionagi.mcp import config, dispatch, verbs

# Every verb in the tranche that answers with no arguments, so one parametrized
# call covers the whole set the same way a caller would reach it.
NO_ARGUMENT_VERBS = (
    "monitor",
    "stats.runs",
    "invoke.list",
    "dispatch.ls",
    "state.ls",
    "state.stats",
    "team.list",
)

# Where each verb's payload puts the thing it read, so the availability wrapper
# can be asserted on without a second list of names to keep in step.
AVAILABILITY_KEY = {
    "monitor": "entities",
    "stats.runs": "groups",
    "invoke.list": "invocations",
    "dispatch.ls": "dispatches",
    "state.ls": "sessions",
    "state.stats": "row_counts",
    "team.list": "teams",
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A LIONAGI_HOME the child inherits, so no test reads a real store."""
    monkeypatch.setenv("LIONAGI_HOME", str(tmp_path))
    return tmp_path


def call(**kwargs):
    return asyncio.run(dispatch.request(**kwargs))


def run_op(op: str, args: dict | None = None) -> dict:
    answer = call(ops=[{"op": op, "args": args or {}}])
    return answer["ops"][0]


def li(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*config.li_command(), *argv], capture_output=True, text=True, check=False, timeout=120
    )


def seed_invocation(skill: str = "observability-test") -> str:
    """One invocation row, created the way anything else creates one."""
    done = li("invoke", "start", "--skill", skill)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


# ── every verb answers the contract, through the real command ────────────────


@pytest.mark.parametrize("verb", NO_ARGUMENT_VERBS)
def test_a_verb_answers_with_the_versioned_envelope(home, verb):
    op = run_op(verb)
    assert op["ok"] is True, op
    assert op["result"]["contract_version"] == 1
    assert isinstance(op["result"]["data"], dict)


@pytest.mark.parametrize("verb", NO_ARGUMENT_VERBS)
def test_every_read_a_verb_makes_carries_whether_it_was_established(home, verb):
    data = run_op(verb)["result"]["data"]
    wrapper = data[AVAILABILITY_KEY[verb]]
    assert set(wrapper) == {"available", "value", "reason_code", "detail"}


@pytest.mark.parametrize(
    "verb", ["monitor", "stats.runs", "invoke.list", "dispatch.ls", "state.ls", "state.stats"]
)
def test_a_store_that_does_not_exist_is_not_reported_as_zero_rows(home, verb):
    # The distinction the whole availability wrapper exists for: a caller that
    # has never run anything must not be told there are zero sessions in a
    # database that was never created.
    wrapper = run_op(verb)["result"]["data"][AVAILABILITY_KEY[verb]]
    assert wrapper["available"] is False
    assert wrapper["reason_code"] == "not_found"
    assert str(home) in wrapper["detail"]


def test_no_team_directory_at_all_is_a_definitive_zero(home):
    # The opposite case, and why it is not the one above: a team file is only
    # ever written by `li team create`, so nothing having been written is the
    # complete answer rather than a read that failed.
    teams = run_op("team.list")["result"]["data"]["teams"]
    assert teams["available"] is True
    assert teams["value"] == []


# ── the rows a seeded store actually holds ───────────────────────────────────


def test_a_listing_returns_the_row_the_cli_just_created(home):
    invocation_id = seed_invocation()
    invocations = run_op("invoke.list")["result"]["data"]["invocations"]
    assert invocations["available"] is True
    assert [row["id"] for row in invocations["value"]] == [invocation_id]
    row = invocations["value"][0]
    assert row["skill"] == "observability-test"
    assert row["status"] == "running"
    assert isinstance(row["started_at"], float)


def test_the_running_row_is_what_monitor_reports_in_flight(home):
    invocation_id = seed_invocation()
    data = run_op("monitor")["result"]["data"]
    entities = data["entities"]["value"]
    assert [e["id"] for e in entities if e["kind"] == "invocation"] == [invocation_id]
    # The caller is told the moment the observation was taken, so it can compute
    # an elapsed time itself rather than be handed one against an unseen clock.
    assert data["observed_at"] >= entities[0]["started_at"]


def test_a_filter_reaches_the_child_parser_and_narrows_the_result(home):
    seed_invocation("kept")
    seed_invocation("dropped")
    kept = run_op("invoke.list", {"skill": "kept"})["result"]["data"]["invocations"]["value"]
    assert [row["skill"] for row in kept] == ["kept"]


def test_a_store_that_exists_reports_its_size_and_journal_mode(home):
    seed_invocation()
    data = run_op("state.stats")["result"]["data"]
    assert data["database"]["exists"] is True
    assert data["database"]["size_bytes"] > 0
    assert data["row_counts"]["available"] is True
    assert set(data["row_counts"]["value"]) >= {"sessions", "branches", "messages"}
    assert data["journal_mode"]["available"] is True


def test_a_status_is_a_pair_and_never_a_placeholder_string(home):
    seed_invocation()
    spread = run_op("state.stats")["result"]["data"]["sessions_by_status"]["value"]
    assert all(set(entry) == {"status", "count"} for entry in spread)


# ── refusals arrive inside the envelope ──────────────────────────────────────


def test_an_id_with_no_row_is_a_refusal_and_not_a_crash(home):
    op = run_op("dispatch.show", {"id": "no-such-dispatch"})
    assert op["ok"] is False
    assert op["error"]["kind"] == "not_found"
    assert "no-such-dispatch" in op["error"]["message"]


def test_a_plugin_nobody_installed_is_a_refusal_naming_it(home):
    op = run_op("plugin.info", {"name": "no-such-plugin"})
    assert op["ok"] is False
    assert op["error"]["kind"] == "not_found"


def test_a_flag_that_only_shapes_the_printout_is_refused_with_its_reason(home):
    # The trap the doctor verb documents: `--json` is real to the parser and
    # meaningless past it, so admitting it would be accepted here and refused
    # by the machine dispatcher one process later.
    op = run_op("stats.runs", {"json": True})
    assert op["ok"] is False
    assert "shapes the human printout" in op["error"]["message"]


def test_the_detail_view_is_refused_rather_than_changing_the_payload_shape(home):
    op = run_op("monitor", {"id": "whatever"})
    assert op["ok"] is False
    assert "detail view" in op["error"]["message"]


@pytest.mark.parametrize("verb", ["monitor", "state.ls", "invoke.list"])
def test_a_parameter_nobody_declared_is_refused_by_name(home, verb):
    op = run_op(verb, {"noSuchParameter": 1})
    assert op["ok"] is False
    assert "noSuchParameter" in op["error"]["message"]


def test_a_window_that_cannot_be_parsed_is_refused_inside_the_envelope(home):
    op = run_op("stats.runs", {"since": "yesterday"})
    assert op["ok"] is False
    assert op["error"]["kind"] == "invalid_input"


# ── what the surface deliberately does not reach ─────────────────────────────


def test_the_plugin_listing_is_named_and_refused_with_why(home):
    entry = next(e for e in call(help=True)["verbs"] if e["verb"] == "plugin.list")
    assert entry["available"] is False
    assert "trust" in entry["reason"]
    op = run_op("plugin.list")
    assert op["ok"] is False
    assert op["error"]["kind"] == "unavailable"


def test_the_listing_that_prunes_trust_never_runs_from_this_surface(home):
    # Reached the way it would be if someone registered it, straight at the CLI,
    # to prove the refusal is in the command and not only in the registry.
    done = li("plugin", "list", "--machine")
    envelope = json.loads(done.stdout)
    assert envelope["ok"] is False
    assert envelope["error"]["kind"] == "unavailable"
    assert not (home / "settings.yaml").exists()


@pytest.mark.parametrize(
    ("command", "sub"),
    [("state", "prune"), ("dispatch", "purge"), ("team", "send"), ("invoke", "start")],
)
def test_a_subcommand_that_writes_says_so_instead_of_running(home, command, sub):
    done = li(command, sub, "--machine")
    envelope = json.loads(done.stdout)
    assert envelope["ok"] is False
    assert envelope["error"]["kind"] == "unavailable"


def test_a_subcommand_nobody_has_is_bad_input_not_an_absent_seam(home):
    envelope = json.loads(li("state", "not-a-subcommand", "--machine").stdout)
    assert envelope["error"]["kind"] == "invalid_input"


# ── one envelope on stdout, whatever the command wanted to print ─────────────


@pytest.mark.parametrize(
    "argv",
    [
        ("monitor", "--machine"),
        ("state", "stats", "--machine"),
        ("team", "list", "--machine"),
        ("stats", "runs", "--machine"),
    ],
)
def test_stdout_carries_exactly_one_json_object(home, argv):
    done = li(*argv)
    assert done.returncode == 0, done.stderr
    envelope = json.loads(done.stdout)
    assert done.stdout.strip().count("\n") == 0
    assert envelope["contract_version"] == 1
    assert envelope["ok"] is True


def test_the_alias_reaches_the_same_result_as_the_command(home):
    assert json.loads(li("mon", "--machine").stdout)["ok"] is True


# ── the registry says what it can do ─────────────────────────────────────────


@pytest.mark.parametrize("verb", [*NO_ARGUMENT_VERBS, "dispatch.show", "plugin.info"])
def test_the_verb_admits_only_parameters_its_machine_path_honours(verb):
    registered = verbs.VERBS[verb]
    schema = dispatch.verb_schema(registered)
    assert registered.admits is not None, f"{verb} admits every projected parameter"
    assert set(schema["properties"]) <= set(registered.admits)


# ── a store that exists but will not open ────────────────────────────────────


@pytest.fixture
def unreadable_store(home):
    """A real state.db this process cannot open, restored on the way out."""
    seed_invocation()
    store = home / "state.db"
    assert store.exists(), "the seed did not create a store"
    original = store.stat().st_mode
    store.chmod(0o000)
    try:
        try:
            store.open("rb").close()
        except OSError:
            pass
        else:
            pytest.skip("this process can read a mode-000 file, so there is nothing to test")
        yield store
    finally:
        store.chmod(original)


@pytest.mark.parametrize(
    "verb", ["monitor", "stats.runs", "invoke.list", "dispatch.ls", "state.ls", "state.stats"]
)
def test_a_store_that_will_not_open_is_not_an_implementation_crash(unreadable_store, verb):
    """Existing-but-unopenable is a third answer, and it is the store's, not ours.

    `internal` is this contract's word for our own bug, and a consumer is
    entitled to treat it as one — to stop, to report the tool as broken. A
    permission or a lock is none of those things: it is a routine condition of
    reading someone else's file, the caller can act on it, and it says nothing
    about whether the rows exist. Reporting it as `internal` also puts a driver
    exception string into a versioned contract, where the next release of that
    driver quietly changes it.
    """
    op = run_op(verb)
    assert op["ok"] is True, op
    wrapper = op["result"]["data"][AVAILABILITY_KEY[verb]]
    assert wrapper["available"] is False, wrapper
    assert wrapper["reason_code"] == "unreadable", wrapper


def test_a_detail_read_refuses_rather_than_claiming_the_record_is_missing(unreadable_store):
    """`not_found` would be a claim about the record. We never reached it.

    A detail read has no availability wrapper to put this in, so it has to
    refuse — but the refusal a caller acts on differently is `unavailable`. Told
    `not_found`, a caller stops looking for a dispatch that may be sitting in
    the store it could not open.
    """
    op = run_op("dispatch.show", {"id": "whatever"})
    assert op["ok"] is False, op
    assert op["error"]["kind"] == "unavailable", op["error"]
