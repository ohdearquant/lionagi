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
