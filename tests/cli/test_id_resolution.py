# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Short ids are guesses, and every CLI resolver has to treat them as one.

A full id is a primary key and settles the question. A prefix does not: it can
fit several records, and there is no rule that makes one of them the right
answer. Each of these resolvers feeds a command that acts — resuming a branch,
killing a process, replaying a run — so picking a match is picking a target the
caller never named. These tests hold all four to refusing instead.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

import pytest

from lionagi.cli._util import AmbiguousIdError, fetch_unique_row, resolve_entity
from lionagi.state.db import StateDB

# Two ids of each kind that agree on their first six characters.
SHARED = "abc123"
FIRST = f"{SHARED}00-0000-4000-8000-000000000001"
SECOND = f"{SHARED}00-0000-4000-8000-000000000002"


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", path)
    return path


async def _seed_session(db: StateDB, session_id: str) -> None:
    prog_id = str(uuid.uuid4())
    await db.create_progression(prog_id)
    await db.create_session(
        {
            "id": session_id,
            "progression_id": prog_id,
            "status": "running",
            "started_at": time.time(),
        }
    )


async def _seed_invocation(db: StateDB, invocation_id: str) -> None:
    await db.create_invocation(
        {
            "id": invocation_id,
            "skill": "test",
            "started_at": time.time(),
            "status": "running",
        }
    )


# ── across kinds ──────────────────────────────────────────────────────────────


async def test_a_prefix_that_fits_two_kinds_is_refused(db_path: Path):
    """Search order says where to look first, not who wins a tie.

    A prefix that fits a session and an invocation equally well has no correct
    winner, and taking the one whose table is searched earlier answers a
    question about lookup order as if it were a question about intent.
    """
    async with StateDB(db_path) as db:
        await _seed_session(db, FIRST)
        await _seed_invocation(db, SECOND)

        with pytest.raises(AmbiguousIdError) as caught:
            await resolve_entity(db, SHARED)

    message = str(caught.value)
    assert "session" in message and "invocation" in message
    assert FIRST in message and SECOND in message


async def test_a_full_id_resolves_even_when_its_prefix_is_ambiguous(db_path: Path):
    """Refusing a prefix must not spread to the id it is a prefix of."""
    async with StateDB(db_path) as db:
        await _seed_session(db, FIRST)
        await _seed_invocation(db, SECOND)

        table, entity_type, row = await resolve_entity(db, FIRST)

    assert (table, entity_type) == ("sessions", "session")
    assert row["id"] == FIRST


async def test_an_unambiguous_prefix_still_resolves(db_path: Path):
    """The refusal is about collisions, not about prefixes."""
    async with StateDB(db_path) as db:
        await _seed_session(db, FIRST)

        table, entity_type, row = await resolve_entity(db, SHARED)

    assert (table, entity_type) == ("sessions", "session")
    assert row["id"] == FIRST


# ── inside one kind ───────────────────────────────────────────────────────────


async def test_a_prefix_that_fits_two_rows_of_one_kind_is_refused(db_path: Path):
    async with StateDB(db_path) as db:
        await _seed_session(db, FIRST)
        await _seed_session(db, SECOND)

        with pytest.raises(AmbiguousIdError) as caught:
            await fetch_unique_row(db, "sessions", SHARED)

    assert "session" in str(caught.value)


# ── case ──────────────────────────────────────────────────────────────────────


async def test_an_upper_cased_prefix_does_not_match_a_lower_cased_id(db_path: Path):
    """LIKE compares ASCII case-insensitively on the default backend.

    Left at that, a prefix would match ids it is not actually a prefix of,
    while the exact comparison beside it would not — the same input resolving
    one way as a whole id and another way as a prefix.
    """
    async with StateDB(db_path) as db:
        await _seed_session(db, FIRST)

        assert await fetch_unique_row(db, "sessions", SHARED.upper()) is None
        assert await resolve_entity(db, SHARED.upper()) is None


# ── branch files ──────────────────────────────────────────────────────────────


@pytest.fixture
def runs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "runs"
    root.mkdir()
    monkeypatch.setattr("lionagi.cli._runs.RUNS_ROOT", root)
    monkeypatch.setattr("lionagi.cli._runs._LEGACY_AGENTS_ROOT", tmp_path / "absent")
    return root


def _write_branch(runs_root: Path, run_id: str, branch_id: str) -> Path:
    branches = runs_root / run_id / "branches"
    branches.mkdir(parents=True, exist_ok=True)
    path = branches / f"{branch_id}.json"
    path.write_text(json.dumps({"id": branch_id}))
    return path


def test_find_branch_refuses_a_prefix_that_fits_two_branches(runs_root: Path):
    """Resuming acts, so the wrong branch is a new leg on someone else's work."""
    from lionagi.cli._runs import find_branch

    _write_branch(runs_root, "run-a", FIRST)
    _write_branch(runs_root, "run-b", SECOND)

    with pytest.raises(AmbiguousIdError) as caught:
        find_branch(SHARED)

    assert FIRST in str(caught.value) and SECOND in str(caught.value)


def test_find_branch_takes_an_exact_id_from_an_older_run(runs_root: Path):
    """An exact id is a complete answer and must win wherever it lives.

    Directories are walked newest first, which is a reasonable place to start
    looking and a bad reason to prefer one complete answer over another.
    """
    from lionagi.cli._runs import find_branch

    older = _write_branch(runs_root, "run-old", FIRST)
    _write_branch(runs_root, "run-new", SECOND)
    newer_dir = runs_root / "run-new"
    older_dir = runs_root / "run-old"
    now = time.time()
    import os

    os.utime(older_dir, (now - 600, now - 600))
    os.utime(newer_dir, (now, now))

    run_id, path = find_branch(FIRST)

    assert (run_id, path) == ("run-old", older)


# ── run directories ───────────────────────────────────────────────────────────


def test_run_dir_lookup_refuses_a_prefix_that_fits_two_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Taking the newest match answers "the newest run starting with this".

    That is not the question the caller asked, and the commands built on this
    resolver replay and inspect whatever comes back.
    """
    from lionagi.cli.orchestrate import _checkpoint

    root = tmp_path / "runs"
    (root / f"{SHARED}-aaaaaa").mkdir(parents=True)
    (root / f"{SHARED}-bbbbbb").mkdir(parents=True)
    monkeypatch.setattr(_checkpoint, "RUNS_ROOT", root)

    with pytest.raises(AmbiguousIdError) as caught:
        _checkpoint._find_run_dir_by_id(SHARED)

    assert f"{SHARED}-aaaaaa" in str(caught.value)


def test_run_dir_lookup_still_resolves_an_exact_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from lionagi.cli.orchestrate import _checkpoint

    root = tmp_path / "runs"
    exact = root / f"{SHARED}-aaaaaa"
    exact.mkdir(parents=True)
    (root / f"{SHARED}-aaaaaa-extended").mkdir(parents=True)
    monkeypatch.setattr(_checkpoint, "RUNS_ROOT", root)

    run_dir = _checkpoint._find_run_dir_by_id(f"{SHARED}-aaaaaa")

    assert run_dir is not None
    assert run_dir.state_root == exact


# ── team files ────────────────────────────────────────────────────────────────


@pytest.fixture
def teams_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "teams"
    root.mkdir()
    monkeypatch.setattr("lionagi.cli.team.TEAMS_DIR", root)
    return root


def _write_team(teams_dir: Path, team_id: str, name: str) -> Path:
    path = teams_dir / f"{team_id}.json"
    path.write_text(json.dumps({"id": team_id, "name": name, "members": [], "messages": []}))
    return path


def test_team_lookup_refuses_a_prefix_that_fits_two_teams(teams_dir: Path):
    """Otherwise the team a message lands in depends on directory order."""
    from lionagi.cli.team import _team_file

    _write_team(teams_dir, FIRST, "one")
    _write_team(teams_dir, SECOND, "two")

    with pytest.raises(AmbiguousIdError) as caught:
        _team_file(SHARED)

    assert FIRST in str(caught.value) and SECOND in str(caught.value)


def test_team_lookup_settles_on_an_id_or_a_name(teams_dir: Path):
    """Both are complete answers, so neither is held up by a colliding prefix."""
    from lionagi.cli.team import _team_file

    first = _write_team(teams_dir, FIRST, "one")
    second = _write_team(teams_dir, SECOND, "two")

    assert _team_file(FIRST) == first
    assert _team_file("two") == second


# ── the resolvers the CLI surfaces actually call ──────────────────────────────


@pytest.mark.parametrize(
    "resolver",
    [
        "_resolve_any_target",
        "_resolve_agent_target",
        "_resolve_play_target",
    ],
)
async def test_the_status_resolvers_refuse_a_prefix_that_fits_two_kinds(db_path: Path, resolver):
    """The resolvers behind the commands, not only the shared helper.

    Each of these used to try one table after another and keep the first hit,
    so a prefix that fits a session and an invocation resolved to the session
    because sessions are looked at first. `li o ctl pause` and the checkpoint
    replay act on what comes back, so the pick is a control queued against a
    flow the caller never identified.
    """
    from lionagi.cli import status

    fn = getattr(status, resolver)
    async with StateDB(db_path) as db:
        await _seed_session(db, FIRST)
        await _seed_invocation(db, SECOND)

        with pytest.raises(AmbiguousIdError):
            await (fn(db, SHARED) if resolver == "_resolve_any_target" else fn(db, SHARED, None))


async def test_the_monitor_detail_resolver_refuses_a_prefix_that_fits_two_kinds(db_path: Path):
    from lionagi.cli.monitor import _find_entity

    async with StateDB(db_path) as db:
        await _seed_session(db, FIRST)
        await _seed_invocation(db, SECOND)

        with pytest.raises(AmbiguousIdError):
            await _find_entity(db, SHARED)


async def test_the_status_resolvers_still_take_an_exact_id(db_path: Path):
    """Aggregating prefixes must not disturb the answer an exact id gives."""
    from lionagi.cli import status

    async with StateDB(db_path) as db:
        await _seed_session(db, FIRST)
        await _seed_invocation(db, SECOND)

        assert (await status._resolve_any_target(db, FIRST))[0] == "session"
        assert (await status._resolve_any_target(db, SECOND))[0] == "invocation"
        assert (await status._resolve_agent_target(db, SECOND, None))[0] == "invocation"


async def test_the_monitor_detail_resolver_still_takes_an_exact_id(db_path: Path):
    """The same guarantee where monitor gets it, which is by delegation.

    Refusing an ambiguous prefix is only correct while an exact id still
    answers. Monitor gets both from `resolve_entity` rather than stating them
    itself, so the assertion is here to hold that delegation in place.
    """
    from lionagi.cli.monitor import _find_entity

    async with StateDB(db_path) as db:
        await _seed_session(db, FIRST)
        await _seed_invocation(db, SECOND)

        assert (await _find_entity(db, FIRST))[0] == "session"
        assert (await _find_entity(db, SECOND))[0] == "invocation"


# ── run id: a separate id space, not part of the generic resolver ─────────────
#
# Run ids (cli/_runs.py, one directory per run) are not primary keys
# `_util.resolve_entity` searches — they are mirrored onto sessions only
# through the nullable `sessions.run_id` column. `_resolve_any_target` (which
# backs both `li o ctl status` and `li o ctl msg`) falls back to it the same
# way it already falls back to branch_id, after the generic sweep comes up
# empty.


async def _seed_session_with_run_id(db: StateDB, session_id: str, run_id: str) -> None:
    prog_id = str(uuid.uuid4())
    await db.create_progression(prog_id)
    await db.create_session(
        {
            "id": session_id,
            "progression_id": prog_id,
            "status": "running",
            "started_at": time.time(),
            "run_id": run_id,
        }
    )


async def test_run_id_resolves_to_its_session(db_path: Path):
    from lionagi.cli import status

    run_id = "20260801T070707-idres"
    async with StateDB(db_path) as db:
        await _seed_session_with_run_id(db, FIRST, run_id)

        hit = await status._resolve_any_target(db, run_id)

    assert hit == ("session", await _reread(db_path, FIRST))


async def _reread(db_path: Path, session_id: str) -> dict:
    async with StateDB(db_path) as db:
        return await db.get_session(session_id)


async def test_run_id_fallback_ignores_unrelated_sessions_and_invocations(db_path: Path):
    """Other rows in the database — a plain session, a plain invocation —
    must not deflect or block the run_id lookup for an id that is neither."""
    from lionagi.cli import status

    run_id = "20260801T110606-idres"
    async with StateDB(db_path) as db:
        await _seed_session(db, FIRST)
        await _seed_invocation(db, SECOND)
        await _seed_session_with_run_id(db, "cccccccc-0000-4000-8000-000000000003", run_id)

        hit = await status._resolve_any_target(db, run_id)

    assert hit is not None
    assert hit[0] == "session"
    assert hit[1]["id"] == "cccccccc-0000-4000-8000-000000000003"


async def test_an_id_matching_nothing_at_all_fails_cleanly(db_path: Path):
    """No session, invocation, play, branch, or run — the resolver returns
    None rather than raising or guessing."""
    from lionagi.cli import status

    async with StateDB(db_path) as db:
        await _seed_session_with_run_id(db, FIRST, "20260801T080808-idres")

        assert await status._resolve_any_target(db, "20260801T999999-nomatch") is None


async def test_an_ambiguous_run_id_prefix_is_refused_not_resolved(db_path: Path):
    """Two distinct run ids sharing a prefix must raise, mirroring the
    guarantee every other id kind in this module already gets. A resolver
    that silently picked one of them would be exactly the regression this
    module exists to catch."""
    from lionagi.cli import status

    async with StateDB(db_path) as db:
        await _seed_session_with_run_id(db, FIRST, "20260801T090909-runA")
        await _seed_session_with_run_id(db, SECOND, "20260801T090909-runB")

        with pytest.raises(AmbiguousIdError):
            await status._resolve_any_target(db, "20260801T090909-run")


async def test_run_id_fallback_prefers_the_most_recently_updated_session(db_path: Path):
    """`run_id` carries no uniqueness constraint — `get_sessions_for_run`
    already documents that one run can persist more than one session; the
    fallback must not just take whichever row a plain query happens to
    return first."""
    from lionagi.cli import status

    run_id = "20260801T101010-idres"
    async with StateDB(db_path) as db:
        await _seed_session_with_run_id(db, FIRST, run_id)
        await db.update_session(FIRST, status="timed_out")
        await asyncio.sleep(0.01)
        await _seed_session_with_run_id(db, SECOND, run_id)

        hit = await status._resolve_any_target(db, run_id)

    assert hit[0] == "session"
    assert hit[1]["id"] == SECOND


async def test_a_prefix_matching_both_a_primary_key_and_a_run_id_is_refused(db_path: Path):
    """A prefix that fits an entity primary key AND a run id is ambiguous, and
    consulting the run-id space last does not make it less so.

    This resolver searches sessions, invocations and plays together for one
    reason: the commands built on it act, so letting search order pick between
    two candidates queues a control against something the caller never named.
    That reason does not stop at the entity kinds. Ordering the run-id space
    after them decides the same question by the same means, so the cross-space
    case is refused and the operator is told what to disambiguate.
    """
    from lionagi.cli import status

    shared_prefix = FIRST[:8]
    colliding_run_id = f"{shared_prefix}-run-for-a-different-session"
    other_session = "dddddddd-0000-4000-8000-000000000004"

    async with StateDB(db_path) as db:
        await _seed_session(db, FIRST)
        await _seed_session_with_run_id(db, other_session, colliding_run_id)

        with pytest.raises(AmbiguousIdError) as raised:
            await status._resolve_any_target(db, shared_prefix)

    # Both spaces are named, so the refusal is actionable rather than a bare no.
    candidates = " ".join(raised.value.candidates)
    assert FIRST in candidates
    assert colliding_run_id in candidates


async def test_a_date_length_run_prefix_that_also_fits_a_session_id_is_refused(db_path: Path):
    """The realistic shape of that collision, with the values an operator types.

    A run id opens with a date, and every digit of a date is valid hex, so a
    date-length prefix can prefix a session UUID as well. Nothing longer can
    collide: position nine of a run id is a `T`, which no UUID contains. The
    short prefix is therefore the only case worth pinning, and it is the one a
    synthetic prefix does not exercise.
    """
    from lionagi.cli import status

    prefix = "20260802"
    run_id = f"{prefix}T123456-abcdef"
    colliding_session = f"{prefix}-0000-4000-8000-00000000000a"
    run_owner = "ffffffff-0000-4000-8000-00000000000b"

    async with StateDB(db_path) as db:
        await _seed_session(db, colliding_session)
        await _seed_session_with_run_id(db, run_owner, run_id)

        with pytest.raises(AmbiguousIdError):
            await status._resolve_any_target(db, prefix)

        # The full run id carries the `T`, so it stays decisive.
        hit = await status._resolve_any_target(db, run_id)

    assert hit is not None
    assert hit[1]["id"] == run_owner


async def test_a_full_length_run_id_is_not_shadowed_by_any_primary_key(db_path: Path):
    """The other half of the same contract: a full-length run id is long enough
    that no primary key matches it, so the precedence above cannot shadow it.
    Seeded alongside a session whose id shares the run id's leading characters,
    which is the only way a primary key could plausibly intercept it.
    """
    from lionagi.cli import status

    run_id = "20260801T121212-fullrun"
    decoy_session = f"{run_id[:8]}-0000-4000-8000-000000000005"
    owning_session = "eeeeeeee-0000-4000-8000-000000000006"

    async with StateDB(db_path) as db:
        await _seed_session(db, decoy_session)
        await _seed_session_with_run_id(db, owning_session, run_id)

        hit = await status._resolve_any_target(db, run_id)

    assert hit is not None
    assert hit[0] == "session"
    assert hit[1]["id"] == owning_session
