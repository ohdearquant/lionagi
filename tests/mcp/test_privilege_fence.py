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

from lionagi.mcp import dispatch, jobs, projection, verbs

# Named by the operation an agent could otherwise perform on itself, not by a
# spelling: a rename that keeps the capability must still fail this.
FENCED_PATHS = ("state migrate", "plugin trust", "hooks trust")

# The previous surface's names for the same capabilities. A synonym is resolved
# before dispatch, so a fenced capability must not be reachable by an old name
# either.
FENCED_LEGACY_NAMES = ("plugin_trust", "hooks_trust", "state_migrate")


def call(**kwargs):
    return asyncio.run(dispatch.request(**kwargs))


def test_the_fence_list_is_the_one_the_registry_states():
    assert set(verbs.FENCED_PATHS) == set(FENCED_PATHS)


def test_no_registered_verb_resolves_to_a_fenced_command_path():
    for verb in verbs.VERBS.values():
        if verb.cli_path is None:
            continue
        for fenced in FENCED_PATHS:
            assert not verb.cli_path.startswith(fenced), f"{verb.name} -> {verb.cli_path}"


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
    # positional argument of the command that was already chosen.
    seen: dict = {}

    def fake_submit(kind, flags, **kwargs):
        seen.update(kind=kind, flags=list(flags))
        return {"run_id": "rid"}

    monkeypatch.setattr(jobs, "submit", fake_submit)
    call(ops=[{"op": "agent.submit", "args": {"query": ["plugin", "trust", "evil"]}}])
    assert seen["kind"] == "agent"
    assert jobs._KIND_ARGV["agent"] == ["agent"]
    assert not any(flag.startswith("--") for flag in seen["flags"])
