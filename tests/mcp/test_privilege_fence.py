# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Operations that grant privilege stay off this surface.

Every caller here is an agent. Trusting a plugin lets a bundle run code in the
process, trusting a hook does the same for a hook bundle, and migrating the store
rewrites what the rest of these verbs report on. Exposing any of them would let
the thing being granted a right be the thing that grants it. These stay
human-at-a-terminal operations.

The fence is an allowlist, so the interesting assertions are about the routes
around it rather than about the three names: a verb that is not registered, a
command path that is not a verb's path, and the absence of any parameter that
carries opaque argv.
"""

from __future__ import annotations

import asyncio

import pytest

from lionagi.cli import machine
from lionagi.mcp import dispatch, jobs, projection, verbs

# Named by the operation an agent could otherwise perform on itself, not by a
# spelling: a rename that keeps the capability must still fail this.
FENCED_PATHS = ("state migrate", "plugin trust", "hooks trust")

# The previous surface's names for the same capabilities. A synonym is resolved
# before dispatch, so a fenced capability must not be reachable by an old name
# either.
FENCED_LEGACY_NAMES = ("plugin_trust", "hooks_trust", "state_migrate")

# Every command path this surface can execute, written out here rather than read
# off the registry, so the two have to agree. Matching against a list of forbidden
# spellings only catches a capability that keeps its name: `plugin authorize`
# would grant exactly what `plugin trust` grants and pass such a check. What is
# checkable without knowing tomorrow's names is the size of the reachable set, so
# adding any verb fails this until someone writes the new path down.
REVIEWED_PATHS = frozenset(
    {
        "agent",
        "doctor",
        "handshake",
        "orchestrate fanout",
        "orchestrate flow",
        "runs",
        "dispatch ls",
        "dispatch show",
        # The queue's writes. Reviewed together because the caller that reads the
        # queue from this surface is the one that has to resolve what it finds, and
        # each acts on a single row the caller named: `ack` needs the row's own ack
        # token, `retry` moves one row back to pending, `purge` deletes one row by id
        # and refuses a criteria sweep.
        "dispatch ack",
        "dispatch retry",
        "dispatch purge",
        "invoke list",
        # Read-only: reports what the lifecycle store already recorded about one
        # run id. It writes nothing and names no path of its own.
        "lifecycle",
        "monitor",
        "plugin info",
        "schedule create",
        "schedule delete",
        "schedule disable",
        "schedule enable",
        "schedule export",
        "schedule get",
        "schedule limits",
        "schedule list",
        "schedule runs",
        "schedule status",
        "schedule trigger",
        "schedule validate",
        "state ls",
        "state stats",
        "stats runs",
        "team list",
        # Team coordination read/write. Reviewed together: all four act only on
        # the team store under the server's own home, take a closed argument
        # set matching the CLI flags, and exist so filesystem-sandboxed workers
        # can coordinate through the unsandboxed server instead of being
        # refused at open() on the team file.
        "team create",
        "team show",
        "team send",
        "team receive",
    }
)


def call(**kwargs):
    return asyncio.run(dispatch.request(**kwargs))


def spawn_op(op: str, args: dict) -> dict:
    """A spawn op carrying the fingerprint its verb requires.

    Fetched the way a caller has to fetch it, so these tests exercise the
    round-trip rather than reaching past it.
    """
    return {"op": op, "args": args, "schema_fingerprint": call(help=op)["schema_fingerprint"]}


def test_the_fence_list_is_the_one_the_registry_states():
    assert set(verbs.FENCED_PATHS) == set(FENCED_PATHS)


def test_no_registered_verb_resolves_to_a_fenced_command_path():
    for verb in verbs.VERBS.values():
        if verb.cli_path is None:
            continue
        for fenced in FENCED_PATHS:
            assert not verb.cli_path.startswith(fenced), f"{verb.name} -> {verb.cli_path}"


def test_the_reachable_command_paths_are_the_reviewed_ones():
    """The whole reachable set, both directions.

    A verb added under a name nobody thought to forbid is the way this fence
    fails, and no list of forbidden spellings can be written in advance. Reading
    the reachable set and diffing it against one a person wrote down catches the
    addition itself, whatever it is called.
    """
    reachable = {verb.cli_path for verb in verbs.VERBS.values() if verb.cli_path is not None}
    assert reachable - REVIEWED_PATHS == set(), "a command path became reachable unreviewed"
    assert REVIEWED_PATHS - reachable == set(), "a reviewed path is gone; update the list"


def test_no_catalog_entry_names_a_fenced_operation():
    listed = {entry["verb"] for entry in call(help=True)["verbs"]}
    for fenced in FENCED_PATHS:
        assert fenced.replace(" ", ".") not in listed


@pytest.mark.parametrize("name", FENCED_LEGACY_NAMES)
def test_a_previous_surface_name_for_a_fenced_operation_resolves_to_nothing(name):
    assert name not in verbs.SYNONYMS
    answer = call(ops=[{"op": name}])
    assert answer["ops"][0]["ok"] is False


@pytest.mark.parametrize("path", FENCED_PATHS)
def test_asking_for_a_fenced_path_as_a_verb_is_refused(path):
    for spelling in (path, path.replace(" ", "."), path.replace(" ", "_")):
        answer = call(ops=[{"op": spelling}])
        assert answer["ops"][0]["ok"] is False, spelling


def test_the_projector_can_read_more_than_the_surface_allows():
    # Reachability is not authorization, and that gap is the point: what a schema
    # can be generated for must stay strictly wider than what can be run, so
    # adding a CLI command never silently widens this surface.
    readable = set(projection.available_paths())
    assert {"plugin trust", "hooks trust"} <= readable
    runnable = {v.cli_path for v in verbs.VERBS.values() if v.cli_path}
    assert readable - runnable


def test_no_verb_accepts_opaque_argv():
    # The fence rests on there being no route from a parameter value to a new
    # command boundary. The surest form of that is no parameter carrying argv at
    # all, which is what this asserts — if one is ever added, it needs a
    # fail-closed check and this test should be replaced by one that exercises it.
    for verb in verbs.VERBS.values():
        try:
            schema = dispatch.verb_schema(verb)
        except projection.SchemaProjectionError:  # pragma: no cover - none today
            continue
        assert "extra_args" not in schema["properties"], verb.name


def test_a_spawn_verb_cannot_be_argued_into_a_different_command(monkeypatch):
    # Every spawn verb's command boundary comes from its job kind, not from any
    # caller-supplied value, so a value that looks like a subcommand lands as a
    # positional pair of the command that was already chosen.
    seen: dict = {}

    def fake_submit(kind, flags, **kwargs):
        seen.update(kind=kind, flags=list(flags))
        return {"run_id": "rid"}

    monkeypatch.setattr(jobs, "submit", fake_submit)
    call(ops=[spawn_op("agent.submit", {"query": ["plugin", "trust"]})])
    assert seen["kind"] == "agent"
    assert jobs._KIND_ARGV["agent"] == ["agent"]
    assert seen["flags"] == ["--", "plugin", "trust"]


@pytest.mark.parametrize(
    "value",
    ["--machine", "--cwd", "-h", "--", "--help"],
    ids=["machine", "cwd", "short", "sentinel", "help"],
)
def test_a_positional_that_looks_like_a_switch_stays_a_positional(monkeypatch, value):
    # The parser is not the only thing that reads this argv: the entry point scans
    # every pre-sentinel token for `--machine` before it knows which command was
    # asked for. A caller-supplied value landing in option position is therefore
    # able to change which code runs, not merely which flags it sees.
    seen: dict = {}

    def fake_submit(kind, flags, **kwargs):
        seen.update(kind=kind, flags=list(flags))
        return {"run_id": "rid"}

    monkeypatch.setattr(jobs, "submit", fake_submit)
    # The prompt travels separately, which is also what leaves the sole
    # positional free to be the model this command requires.
    answer = call(ops=[spawn_op("agent.submit", {"query": [value], "prompt": "do it"})])
    assert answer["ops"][0]["ok"] is True
    # Asserted on the rendering rather than on what a parser makes of it: where
    # the sentinel sits is the same on every Python, and it is what decides
    # whether the token can be read as an option at all.
    assert seen["flags"] == ["--", value]
    assert not machine.has_machine_flag(seen["flags"])


def test_a_flag_value_that_looks_like_a_switch_stays_a_value(monkeypatch):
    seen: dict = {}

    def fake_submit(kind, flags, **kwargs):
        seen.update(kind=kind, flags=list(flags))
        return {"run_id": "rid"}

    monkeypatch.setattr(jobs, "submit", fake_submit)
    # A profile name rather than a working directory: a cwd is checked against
    # the filesystem before anything is rendered, so it can no longer carry an
    # arbitrary string and would test the binding on a value that cannot arrive.
    # This one is free text all the way to argv, which is where the binding has
    # to hold.
    call(ops=[spawn_op("agent.submit", {"query": ["hi"], "agent": "--machine"})])
    assert "--agent=--machine" in seen["flags"]
    assert "--machine" not in seen["flags"]
    assert not machine.has_machine_flag(seen["flags"])


def test_a_working_directory_that_looks_like_a_switch_never_reaches_argv(monkeypatch):
    """The same fence, one step earlier. A cwd has to name a directory that is
    there, and a switch is not one, so the value is refused before argv exists
    rather than bound safely inside it."""
    spawned: list = []

    monkeypatch.setattr(jobs, "submit", lambda *a, **kw: spawned.append(kw) or {"run_id": "rid"})
    answer = call(ops=[spawn_op("agent.submit", {"query": ["hi"], "cwd": "--machine"})])

    assert answer["ops"][0]["ok"] is False
    assert answer["ops"][0]["error"]["kind"] == "invalid_input"
    assert spawned == []
