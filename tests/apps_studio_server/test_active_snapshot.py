from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

from lionagi.state.db import StateDB  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _seed_snapshot_rows(db_path: Path) -> None:
    now = time.time()
    async with StateDB(db_path) as db:
        for index in range(5):
            invocation_id = f"active-inv-{index}"
            await db.create_invocation(
                {
                    "id": invocation_id,
                    "skill": f"skill-{index}",
                    "status": "running",
                    "started_at": now - 100 + index,
                }
            )
            progression_id = str(uuid.uuid4())
            await db.create_progression(progression_id)
            await db.create_session(
                {
                    "id": f"active-run-{index}",
                    "progression_id": progression_id,
                    "name": f"active {index}",
                    "status": "running",
                    "started_at": now - 100 + index,
                    "last_message_at": now,
                    "invocation_id": invocation_id,
                    "project": "org/alpha" if index < 3 else "org/beta",
                }
            )

        for index in range(3):
            invocation_id = f"terminal-inv-{index}"
            await db.create_invocation(
                {
                    "id": invocation_id,
                    "skill": f"finished-skill-{index}",
                    "status": "completed",
                    "started_at": now - 300 - index,
                    "ended_at": now - index,
                }
            )
            progression_id = str(uuid.uuid4())
            await db.create_progression(progression_id)
            await db.create_session(
                {
                    "id": f"terminal-run-{index}",
                    "progression_id": progression_id,
                    "name": f"terminal {index}",
                    "status": "completed",
                    "started_at": now - 300 - index,
                    "ended_at": now - index,
                    "project": "org/alpha",
                }
            )


def _client(tmp_path, monkeypatch) -> TestClient:
    import lionagi.state.db as state_db_mod

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    _run(_seed_snapshot_rows(db_path))

    from lionagi.studio.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_active_snapshot_is_bounded_and_discloses_exact_omissions(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "lionagi.studio.services.runs._session_liveness", lambda *_args, **_kwargs: True
    )

    response = client.get(
        "/api/active-snapshot",
        params={"run_limit": 2, "invocation_limit": 3, "recent_limit": 2},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["snapshot_version"]
    assert isinstance(payload["snapshot_at"], float)
    assert payload["active_run_total"] == 5
    assert payload["active_run_omitted"] == 3
    assert len(payload["active_runs"]) == 2
    assert {row["status"] for row in payload["active_runs"]} == {"running"}
    assert payload["active_invocation_total"] == 5
    assert payload["active_invocation_omitted"] == 2
    assert len(payload["active_invocations"]) == 3
    assert {row["status"] for row in payload["active_invocations"]} == {"running"}
    assert len(payload["recent_runs"]) == 2
    assert payload["recent_run_has_more"] is True
    assert len(payload["recent_invocations"]) == 2
    assert payload["recent_invocation_has_more"] is True
    assert payload["complete"] is False


def test_active_snapshot_scopes_invocations_and_totals_with_runs(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "lionagi.studio.services.runs._session_liveness", lambda *_args, **_kwargs: True
    )

    response = client.get(
        "/api/active-snapshot",
        params={"project": "org/alpha", "run_limit": 10, "invocation_limit": 10},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["active_run_total"] == 3
    assert payload["active_invocation_total"] == 3
    assert payload["active_run_omitted"] == 0
    assert payload["active_invocation_omitted"] == 0
    assert {row["project"] for row in payload["active_runs"]} == {"org/alpha"}
    assert {row["id"] for row in payload["active_invocations"]} == {
        "active-inv-0",
        "active-inv-1",
        "active-inv-2",
    }
    assert payload["complete"] is True


def test_active_snapshot_rejects_unbounded_limits(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.get("/api/active-snapshot", params={"run_limit": 501})

    assert response.status_code == 422


async def _seed_kind_rows(db_path: Path) -> None:
    """One running run per stored orchestration kind, plus a legacy row with none.

    The stored vocabulary spells the show-driven root "show-play"; "show" is the
    facet's spelling and is not a value any row carries.
    """
    now = time.time()
    kinds = ["play", "flow", "fanout", "show-play", "agent", None]
    async with StateDB(db_path) as db:
        for index, kind in enumerate(kinds):
            invocation_id = f"kind-inv-{index}"
            await db.create_invocation(
                {
                    "id": invocation_id,
                    "skill": f"skill-{index}",
                    "status": "running",
                    "started_at": now - 100 + index,
                }
            )
            progression_id = str(uuid.uuid4())
            await db.create_progression(progression_id)
            row = {
                "id": f"kind-run-{index}",
                "progression_id": progression_id,
                "name": f"{kind or 'legacy'} run",
                "status": "running",
                "started_at": now - 100 + index,
                "last_message_at": now,
                "invocation_id": invocation_id,
            }
            if kind is not None:
                row["invocation_kind"] = kind
            await db.create_session(row)


def _kind_client(tmp_path, monkeypatch) -> TestClient:
    import lionagi.state.db as state_db_mod

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    _run(_seed_kind_rows(db_path))
    monkeypatch.setattr(
        "lionagi.studio.services.runs._session_liveness", lambda *_args, **_kwargs: True
    )

    from lionagi.studio.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765")


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("play", {"play run"}),
        ("flow", {"flow run"}),
        ("fanout", {"fanout run"}),
        # The facet spells one thing; the writers have spelled it two ways.
        ("show", {"show-play run"}),
        # A row predating the column carries NULL and reads as a plain agent
        # run everywhere else, so the agent facet has to admit it here too.
        ("agent", {"agent run", "legacy run"}),
    ],
)
def test_active_snapshot_kind_facet_selects_the_same_rows_the_runs_listing_would(
    tmp_path, monkeypatch, kind: str, expected: set[str]
):
    client = _kind_client(tmp_path, monkeypatch)

    response = client.get("/api/active-snapshot", params={"kind": kind})

    assert response.status_code == 200
    payload = response.json()
    assert {row["name"] for row in payload["active_runs"]} == expected
    assert payload["active_run_total"] == len(expected), (
        "the total is what makes a bounded snapshot's omissions readable, so it "
        "has to count the facet's rows rather than every running row"
    )


def test_active_snapshot_kind_facet_leaves_invocation_grouping_alone(tmp_path, monkeypatch):
    """The facet selects runs only, matching the listing this replaced.

    Stated as a test because the client relies on it: a kind-scoped view is the
    one case where a childless group can be an artifact of the filter, and the
    reducer stops trusting server coherence exactly there.
    """
    client = _kind_client(tmp_path, monkeypatch)

    response = client.get("/api/active-snapshot", params={"kind": "play"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["active_run_total"] == 1
    assert payload["active_invocation_total"] == 6


def test_active_snapshot_refuses_an_unknown_kind(tmp_path, monkeypatch):
    """Refused rather than silently empty, and by the runs listing's own check."""
    client = _kind_client(tmp_path, monkeypatch)

    response = client.get("/api/active-snapshot", params={"kind": "aegnt"})

    assert response.status_code == 422
    assert "aegnt" in response.text


async def _seed_engine_mirror_rows(db_path: Path) -> None:
    """A canonical run and the mirrored transcript attributed to it.

    The runs listing collapses this pair into the canonical row. Both halves are
    running children of one invocation, which is the shape that lets a missing
    exclusion show up as an inflated total rather than an extra list entry only.
    """
    now = time.time()
    async with StateDB(db_path) as db:
        await db.create_invocation(
            {
                "id": "mirror-inv",
                "skill": "mirrored",
                "status": "running",
                "started_at": now - 60,
            }
        )
        for session_id, node_metadata in (
            ("canonical-run", None),
            ("mirror-run", {"engine_parent_run_id": "canonical-run"}),
        ):
            progression_id = str(uuid.uuid4())
            await db.create_progression(progression_id)
            row = {
                "id": session_id,
                "progression_id": progression_id,
                "name": session_id,
                "status": "running",
                "started_at": now - 60,
                "last_message_at": now,
                "invocation_id": "mirror-inv",
                "project": "org/alpha",
            }
            if node_metadata is not None:
                row["node_metadata"] = node_metadata
            await db.create_session(row)


def test_active_snapshot_collapses_an_engine_mirror_into_its_canonical_run(tmp_path, monkeypatch):
    """One logical run must count once.

    The snapshot's totals are the feature: Fleet and Mission read them as the
    exact active count, so admitting the mirror does not merely add a row, it
    reports work that is not separately happening.
    """
    import lionagi.state.db as state_db_mod

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    _run(_seed_engine_mirror_rows(db_path))
    monkeypatch.setattr(
        "lionagi.studio.services.runs._session_liveness", lambda *_args, **_kwargs: True
    )

    from lionagi.studio.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")

    response = client.get("/api/active-snapshot", params={"run_limit": 10})
    assert response.status_code == 200
    payload = response.json()
    assert {row["id"] for row in payload["active_runs"]} == {"canonical-run"}
    assert payload["active_run_total"] == 1

    # The scoped path builds its invocation membership from a separate child
    # subquery, so it needs its own arm rather than inheriting the one above.
    scoped = client.get("/api/active-snapshot", params={"project": "org/alpha", "run_limit": 10})
    assert scoped.status_code == 200
    scoped_payload = scoped.json()
    assert {row["id"] for row in scoped_payload["active_runs"]} == {"canonical-run"}
    assert scoped_payload["active_run_total"] == 1
    assert [row["id"] for row in scoped_payload["active_invocations"]] == ["mirror-inv"]


async def _seed_legacy_null_status_row(db_path: Path) -> None:
    now = time.time()
    async with StateDB(db_path) as db:
        progression_id = str(uuid.uuid4())
        await db.create_progression(progression_id)
        await db.create_session(
            {
                "id": "legacy-run",
                "progression_id": progression_id,
                "name": "legacy",
                "started_at": now - 500,
                "ended_at": now - 400,
            }
        )


def test_active_snapshot_normalizes_a_legacy_null_status_row(tmp_path, monkeypatch):
    """A row predating the status column reads as completed everywhere else.

    Serving it as null puts the value straight into the client's status
    handling, which lowercases it, so the crash lands on whoever polls next
    rather than on the row's own view.
    """
    import lionagi.state.db as state_db_mod

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    _run(_seed_legacy_null_status_row(db_path))

    from lionagi.studio.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")

    response = client.get("/api/active-snapshot", params={"recent_limit": 10})

    assert response.status_code == 200
    payload = response.json()
    assert [row["id"] for row in payload["recent_runs"]] == ["legacy-run"]
    assert payload["recent_runs"][0]["status"] == "completed"


async def _seed_fanout_children(db_path: Path, *, wide: int, narrow: int) -> None:
    now = time.time()
    async with StateDB(db_path) as db:
        for invocation_id, count in (("wide-inv", wide), ("narrow-inv", narrow)):
            await db.create_invocation(
                {
                    "id": invocation_id,
                    "skill": invocation_id,
                    "status": "running",
                    "started_at": now - 60,
                }
            )
            for index in range(count):
                progression_id = str(uuid.uuid4())
                await db.create_progression(progression_id)
                await db.create_session(
                    {
                        "id": f"{invocation_id}-child-{index}",
                        "progression_id": progression_id,
                        "name": f"{invocation_id} child {index}",
                        "status": "running",
                        "created_at": now - 60 + index,
                        "started_at": now - 60 + index,
                        "last_message_at": now,
                        "invocation_id": invocation_id,
                    }
                )


def test_active_snapshot_caps_children_per_invocation_and_stops_claiming_health(
    tmp_path, monkeypatch
):
    """The row limits bound invocations; this bounds each invocation's children.

    The health verdict is worst-of, so a capped read can only be optimistic:
    reporting healthy off a partial sample is the one answer the sample cannot
    support, and the narrow invocation is the control that the cap did not just
    silence everything.
    """
    import lionagi.state.db as state_db_mod
    from lionagi.studio.services import active_snapshot as snap_mod

    monkeypatch.setattr(snap_mod, "MAX_INVOCATION_CHILDREN", 2)
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    _run(_seed_fanout_children(db_path, wide=5, narrow=1))

    seen: dict[int, int] = {}

    async def _fake_health(sessions, *, now):
        seen[len(sessions)] = seen.get(len(sessions), 0) + 1
        return "healthy", now

    monkeypatch.setattr("lionagi.studio.services.invocations._invocation_health", _fake_health)
    monkeypatch.setattr(
        "lionagi.studio.services.runs._session_liveness", lambda *_args, **_kwargs: True
    )

    from lionagi.studio.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")

    response = client.get("/api/active-snapshot", params={"run_limit": 10})

    assert response.status_code == 200
    payload = response.json()
    health = {row["id"]: row["health"] for row in payload["active_invocations"]}
    # Five running children, two read.
    assert sorted(seen) == [1, 2], (
        "the wide invocation must reach the health classifier with the cap's "
        f"worth of children, not its whole fanout; saw sizes {sorted(seen)}"
    )
    assert health["wide-inv"] == "unknown"
    assert health["narrow-inv"] == "healthy"

    partial = {
        row["id"]: row["health_from_partial_children"] for row in payload["active_invocations"]
    }
    assert partial["wide-inv"] is True
    assert partial["narrow-inv"] is False, (
        "the uncapped invocation must not be marked partial, or the flag says "
        "nothing and every consumer learns to ignore it"
    )


def test_a_non_healthy_verdict_survives_truncation_and_is_marked_as_a_floor(tmp_path, monkeypatch):
    """Truncation makes the verdict a lower bound, and the two kinds of verdict
    do not survive that equally.

    "healthy" asserts that nothing worse exists anywhere, which is precisely what
    the unread children could refute, so it is downgraded. "orphaned" asserts that
    some child IS orphaned, which reading the rest could only worsen — discarding
    it would hide a known-bad invocation from the very view built to surface them.
    It stays, carrying the flag that says it may understate.

    This fixture is the one that separates the two rules: a blanket
    "truncated means unknown" passes the healthy case above and fails here.
    """
    import lionagi.state.db as state_db_mod
    from lionagi.studio.services import active_snapshot as snap_mod

    monkeypatch.setattr(snap_mod, "MAX_INVOCATION_CHILDREN", 2)
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    _run(_seed_fanout_children(db_path, wide=5, narrow=1))

    async def _fake_health(sessions, *, now):
        # The capped sample of the wide invocation already contains a bad child.
        return ("orphaned" if len(sessions) > 1 else "healthy"), now

    monkeypatch.setattr("lionagi.studio.services.invocations._invocation_health", _fake_health)
    monkeypatch.setattr(
        "lionagi.studio.services.runs._session_liveness", lambda *_args, **_kwargs: True
    )

    from lionagi.studio.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    payload = client.get("/api/active-snapshot", params={"run_limit": 10}).json()

    rows = {row["id"]: row for row in payload["active_invocations"]}
    assert rows["wide-inv"]["health"] == "orphaned", (
        "a proven-bad child must not be erased by the cap that hid its siblings"
    )
    assert rows["wide-inv"]["health_from_partial_children"] is True, (
        "and the consumer must be told the verdict is a floor, since an unread "
        "child could be worse still"
    )
