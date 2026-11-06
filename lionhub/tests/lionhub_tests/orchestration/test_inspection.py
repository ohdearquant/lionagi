# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Shared read-only lifecycle lookup and import-boundary contracts."""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

import pytest

from lionagi.state.db import StateDB
from lionhub.orchestration.inspection import (
    resolve_agent_target,
    resolve_any_target,
    resolve_play_target,
)
from lionhub.orchestration.job import AmbiguousIdError
from tests._id_resolution_test_support import (
    FIRST,
    SECOND,
    SHARED,
    _seed_invocation,
    _seed_session,
)
from tests._id_resolution_test_support import (
    db_path as db_path,  # noqa: F401
)
from tests._status_test_support import (
    _make_branch,
    _make_invocation,
    _make_play,
    _make_session,
    _make_show,
    _seed_session_with_id,
)
from tests._status_test_support import (
    temp_db_path as temp_db_path,  # noqa: F401
)
from tests.cli._import_guard import (
    command_module_imports,
    resolved_imports,
    source_with_line,
)


@pytest.mark.parametrize(
    "resolver",
    [
        "resolve_any_target",
        "resolve_agent_target",
        "resolve_play_target",
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
    from lionhub.orchestration import inspection as _inspection

    fn = getattr(_inspection, resolver)
    async with StateDB(db_path) as db:
        await _seed_session(db, FIRST)
        await _seed_invocation(db, SECOND)

        with pytest.raises(AmbiguousIdError):
            await (fn(db, SHARED) if resolver == "resolve_any_target" else fn(db, SHARED, None))


async def test_the_status_resolvers_still_take_an_exact_id(db_path: Path):
    """Aggregating prefixes must not disturb the answer an exact id gives."""
    from lionhub.orchestration import inspection as _inspection

    async with StateDB(db_path) as db:
        await _seed_session(db, FIRST)
        await _seed_invocation(db, SECOND)

        assert (await _inspection.resolve_any_target(db, FIRST))[0] == "session"
        assert (await _inspection.resolve_any_target(db, SECOND))[0] == "invocation"
        assert (await _inspection.resolve_agent_target(db, SECOND, None))[0] == "invocation"


async def test_run_id_resolves_to_its_session(db_path: Path):
    from lionhub.orchestration import inspection as _inspection

    run_id = "20260801T070707-idres"
    async with StateDB(db_path) as db:
        await _seed_session_with_run_id(db, FIRST, run_id)

        hit = await _inspection.resolve_any_target(db, run_id)

    assert hit == ("session", await _reread(db_path, FIRST))


async def test_run_id_fallback_ignores_unrelated_sessions_and_invocations(db_path: Path):
    """Other rows in the database — a plain session, a plain invocation —
    must not deflect or block the run_id lookup for an id that is neither."""
    from lionhub.orchestration import inspection as _inspection

    run_id = "20260801T110606-idres"
    async with StateDB(db_path) as db:
        await _seed_session(db, FIRST)
        await _seed_invocation(db, SECOND)
        await _seed_session_with_run_id(db, "cccccccc-0000-4000-8000-000000000003", run_id)

        hit = await _inspection.resolve_any_target(db, run_id)

    assert hit is not None
    assert hit[0] == "session"
    assert hit[1]["id"] == "cccccccc-0000-4000-8000-000000000003"


async def test_an_id_matching_nothing_at_all_fails_cleanly(db_path: Path):
    """No session, invocation, play, branch, or run — the resolver returns
    None rather than raising or guessing."""
    from lionhub.orchestration import inspection as _inspection

    async with StateDB(db_path) as db:
        await _seed_session_with_run_id(db, FIRST, "20260801T080808-idres")

        assert await _inspection.resolve_any_target(db, "20260801T999999-nomatch") is None


async def test_an_ambiguous_run_id_prefix_is_refused_not_resolved(db_path: Path):
    """Two distinct run ids sharing a prefix must raise, mirroring the
    guarantee every other id kind in this module already gets. A resolver
    that silently picked one of them would be exactly the regression this
    module exists to catch."""
    from lionhub.orchestration import inspection as _inspection

    async with StateDB(db_path) as db:
        await _seed_session_with_run_id(db, FIRST, "20260801T090909-runA")
        await _seed_session_with_run_id(db, SECOND, "20260801T090909-runB")

        with pytest.raises(AmbiguousIdError):
            await _inspection.resolve_any_target(db, "20260801T090909-run")


async def test_run_id_fallback_prefers_the_most_recently_updated_session(db_path: Path):
    """`run_id` carries no uniqueness constraint — `get_sessions_for_run`
    already documents that one run can persist more than one session; the
    fallback must not just take whichever row a plain query happens to
    return first."""
    from lionhub.orchestration import inspection as _inspection

    run_id = "20260801T101010-idres"
    async with StateDB(db_path) as db:
        await _seed_session_with_run_id(db, FIRST, run_id)
        await db.update_session(FIRST, status="timed_out")
        await asyncio.sleep(0.01)
        await _seed_session_with_run_id(db, SECOND, run_id)

        hit = await _inspection.resolve_any_target(db, run_id)

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
    from lionhub.orchestration import inspection as _inspection

    shared_prefix = FIRST[:8]
    colliding_run_id = f"{shared_prefix}-run-for-a-different-session"
    other_session = "dddddddd-0000-4000-8000-000000000004"

    async with StateDB(db_path) as db:
        await _seed_session(db, FIRST)
        await _seed_session_with_run_id(db, other_session, colliding_run_id)

        with pytest.raises(AmbiguousIdError) as raised:
            await _inspection.resolve_any_target(db, shared_prefix)

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
    from lionhub.orchestration import inspection as _inspection

    prefix = "20260802"
    run_id = f"{prefix}T123456-abcdef"
    colliding_session = f"{prefix}-0000-4000-8000-00000000000a"
    run_owner = "ffffffff-0000-4000-8000-00000000000b"

    async with StateDB(db_path) as db:
        await _seed_session(db, colliding_session)
        await _seed_session_with_run_id(db, run_owner, run_id)

        with pytest.raises(AmbiguousIdError):
            await _inspection.resolve_any_target(db, prefix)

        # The full run id carries the `T`, so it stays decisive.
        hit = await _inspection.resolve_any_target(db, run_id)

    assert hit is not None
    assert hit[1]["id"] == run_owner


async def test_a_full_length_run_id_is_not_shadowed_by_any_primary_key(db_path: Path):
    """The other half of the same contract: a full-length run id is long enough
    that no primary key matches it, so the precedence above cannot shadow it.
    Seeded alongside a session whose id shares the run id's leading characters,
    which is the only way a primary key could plausibly intercept it.
    """
    from lionhub.orchestration import inspection as _inspection

    run_id = "20260801T121212-fullrun"
    decoy_session = f"{run_id[:8]}-0000-4000-8000-000000000005"
    owning_session = "eeeeeeee-0000-4000-8000-000000000006"

    async with StateDB(db_path) as db:
        await _seed_session(db, decoy_session)
        await _seed_session_with_run_id(db, owning_session, run_id)

        hit = await _inspection.resolve_any_target(db, run_id)

    assert hit is not None
    assert hit[0] == "session"
    assert hit[1]["id"] == owning_session


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


async def _reread(db_path: Path, session_id: str) -> dict:
    async with StateDB(db_path) as db:
        return await db.get_session(session_id)


@pytest.mark.asyncio
async def test_resolve_agent_target_by_id(temp_db_path: Path):
    async with StateDB() as db:
        sid = await _make_session(db)
        result = await resolve_agent_target(db, sid, None)
    assert result is not None
    entity_type, row = result
    assert entity_type == "session"
    assert row["id"] == sid


@pytest.mark.asyncio
async def test_resolve_agent_target_prefix_match(temp_db_path: Path):
    async with StateDB() as db:
        sid = await _make_session(db)
        result = await resolve_agent_target(db, sid[:6], None)
    assert result is not None
    assert result[1]["id"] == sid


@pytest.mark.asyncio
async def test_resolve_agent_target_falls_back_to_invocation(temp_db_path: Path):
    async with StateDB() as db:
        inv_id = await _make_invocation(db)
        result = await resolve_agent_target(db, inv_id, None)
    assert result is not None
    assert result[0] == "invocation"


@pytest.mark.asyncio
async def test_resolve_agent_target_explicit_id_ignores_kind_scoping(temp_db_path: Path):
    """An explicit id is honoured regardless of invocation_kind — only the
    no-id 'latest' default is kind-scoped."""
    async with StateDB() as db:
        sid = await _make_session(db, invocation_kind="play")
        result = await resolve_agent_target(db, sid, None)
    assert result is not None
    assert result[1]["id"] == sid


@pytest.mark.asyncio
async def test_resolve_play_target_falls_back_to_play_table(temp_db_path: Path):
    async with StateDB() as db:
        show_id = await _make_show(db)
        play_id = await _make_play(db, show_id)
        result = await resolve_play_target(db, play_id, None)
    assert result is not None
    assert result[0] == "play"


@pytest.mark.asyncio
async def test_resolve_any_target_no_kind_scoping(temp_db_path: Path):
    async with StateDB() as db:
        sid = await _make_session(db, invocation_kind="play")
        result = await resolve_any_target(db, sid)
    assert result is not None
    assert result[1]["id"] == sid


@pytest.mark.asyncio
async def test_resolve_unknown_id_returns_none(temp_db_path: Path):
    async with StateDB() as db:
        result = await resolve_agent_target(db, "0" * 40, None)
    assert result is None


@pytest.mark.asyncio
async def test_resolve_agent_target_falls_back_to_branch_id(temp_db_path: Path):
    """The id `li agent`'s post-run hint prints (`-r <branch_id>`) is a
    branches.id, not a sessions.id — `li agent status <branch_id>` must
    still resolve, to the branch's owning session."""
    async with StateDB() as db:
        sid = await _make_session(db)
        branch_id = await _make_branch(db, sid)
        result = await resolve_agent_target(db, branch_id, None)
    assert result is not None
    entity_type, row = result
    assert entity_type == "session"
    assert row["id"] == sid


@pytest.mark.asyncio
async def test_resolve_agent_target_branch_id_prefix_match(temp_db_path: Path):
    async with StateDB() as db:
        sid = await _make_session(db)
        branch_id = await _make_branch(db, sid)
        result = await resolve_agent_target(db, branch_id[:8], None)
    assert result is not None
    assert result[1]["id"] == sid


@pytest.mark.asyncio
async def test_resolve_any_target_falls_back_to_branch_id(temp_db_path: Path):
    async with StateDB() as db:
        sid = await _make_session(db)
        branch_id = await _make_branch(db, sid)
        result = await resolve_any_target(db, branch_id)
    assert result is not None
    assert result[0] == "session"
    assert result[1]["id"] == sid


@pytest.mark.asyncio
async def test_resolve_agent_target_unknown_branch_shaped_id_returns_none(temp_db_path: Path):
    """A well-formed-but-unknown id must still miss cleanly through the new
    branch_id fallback, not raise or false-positive match."""
    async with StateDB() as db:
        sid = await _make_session(db)
        await _make_branch(db, sid)
        result = await resolve_agent_target(db, str(uuid.uuid4()), None)
    assert result is None


async def test_fetch_by_id_rejects_ambiguous_prefix(temp_db_path: Path) -> None:
    from lionhub.orchestration.inspection import fetch_by_id
    from lionhub.orchestration.job import AmbiguousIdError

    async with StateDB() as db:
        first = await _seed_session_with_id(db, "abcde000-0000-0000-0000-000000000001")
        second = await _seed_session_with_id(db, "abcde111-0000-0000-0000-000000000002")

        with pytest.raises(AmbiguousIdError) as excinfo:
            await fetch_by_id(db, "sessions", "abcde")

    assert set(excinfo.value.candidates) == {first, second}


def test_inspection_imports_no_command_module() -> None:
    """`inspection` is the shared layer under the commands, so it must not
    import any of them."""
    from lionhub.orchestration import inspection as _inspection

    assert (
        command_module_imports(resolved_imports(_inspection.__file__, "lionhub.orchestration"))
        == set()
    )


@pytest.mark.parametrize(
    "probe",
    (
        "from lionagi.cli.status import _x",
        "from lionagi.cli import status",
        "import lionagi.cli.status",
        "from lionagi.cli.orchestrate import _x",
        "from lionagi.cli import orchestrate",
    ),
)
def test_inspection_guard_rejects_a_command_import(
    probe: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The layering guard fails when `inspection` imports a command module by path or by name."""
    from lionhub.orchestration import inspection as _inspection

    patched = source_with_line(_inspection.__file__, probe, tmp_path)
    monkeypatch.setattr(_inspection, "__file__", str(patched))
    with pytest.raises(AssertionError):
        test_inspection_imports_no_command_module()
