# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Focused tests for the run_detail Operator read tool."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import pydantic
import pytest

import lionagi.state.db as state_db_mod

aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")
fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")

from lionagi.state.db import StateDB  # noqa: E402

pytestmark = pytest.mark.asyncio

# Exact key sets for the two result shapes run_detail can return.
_KNOWN_FALSE_KEYS = {"known", "source"}
_KNOWN_TRUE_KEYS = {
    "known",
    "source",
    "truncated",
    "runId",
    "id",
    "name",
    "playbookName",
    "agentName",
    "invocationKind",
    "showPlayName",
    "sourceKind",
    "invocationId",
    "model",
    "provider",
    "effort",
    "agentHash",
    "status",
    "startedAt",
    "endedAt",
    "endedAtApproximate",
    "createdAt",
    "updatedAt",
    "lastMessageAt",
    "effectiveHealth",
    "branchCount",
    "messageCount",
    "project",
    "projectSource",
    "statusReasonCode",
    "statusReasonSummary",
    "totalCostUsd",
    "inputTokens",
    "outputTokens",
    "stateRoot",
    "artifactRoot",
    "workerName",
    "task",
    "stepCount",
    "finishedAt",
    "error",
    "cwd",
    "manifest",
    "messageLimit",
    "messageCursor",
    "messageNextCursor",
}

_SERVER_URL = "postgresql+asyncpg://user:pw@127.0.0.1:1/lionagi_state"


async def seed_session(
    db_path: Path,
    *,
    session_id: str,
    status: str = "completed",
    name: str | None = None,
    playbook_name: str | None = None,
    agent_name: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    effort: str | None = None,
    started_at: float | None = None,
    ended_at: float | None = None,
    created_at: float | None = None,
    updated_at: float | None = None,
    project: str | None = None,
    project_source: str | None = None,
    total_cost_usd: float | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    prog_id = f"{session_id}-prog"
    fields: dict[str, Any] = {
        "id": session_id,
        "progression_id": prog_id,
        "name": name or f"run-{session_id}",
        "playbook_name": playbook_name,
        "agent_name": agent_name,
        "status": status,
        "model": model,
        "provider": provider,
        "effort": effort,
        "started_at": started_at,
        "ended_at": ended_at,
        "updated_at": updated_at if updated_at is not None else time.time(),
        "project": project,
        "project_source": project_source,
        "invocation_kind": "agent",
        "source_kind": "live",
    }
    # `created_at` is NOT NULL in the schema and create_session() only
    # supplies its `now` default when the key is absent entirely — a
    # present key with value None still fails the constraint.
    if created_at is not None:
        fields["created_at"] = created_at
    async with StateDB(db_path) as db:
        await db.create_progression(prog_id)
        await db.create_session(fields)
        # total_cost_usd/input_tokens/output_tokens are not accepted by
        # create_session's fixed column list — set post-creation instead.
        cost_fields: dict[str, Any] = {}
        if total_cost_usd is not None:
            cost_fields["total_cost_usd"] = total_cost_usd
        if input_tokens is not None:
            cost_fields["input_tokens"] = input_tokens
        if output_tokens is not None:
            cost_fields["output_tokens"] = output_tokens
        if cost_fields:
            await db.update_session(session_id, **cost_fields)


async def _set_status_reason(db_path: Path, session_id: str, code: str, summary: str) -> None:
    """Seed status_reason_code/summary directly; update_status()'s lifecycle
    policy machinery requires a real prior-status transition to be seeded
    through, which is unnecessary ceremony for a read-path test fixture."""
    import aiosqlite as _aiosqlite

    async with _aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "UPDATE sessions SET status_reason_code = ?, status_reason_summary = ? WHERE id = ?",
            (code, summary, session_id),
        )
        await conn.commit()


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: Any) -> Path:
    path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", path)
    return path


def _set_url(monkeypatch: Any, url: str | None) -> None:
    """Settings are frozen, so redirect the whole object the module reads."""
    monkeypatch.setattr(
        state_db_mod,
        "settings",
        state_db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": url}),
    )


# ── Cap 1 — RunDetailInput.run_id length bound (rejection, not truncation) ──


async def test_run_detail_rejects_run_id_over_length_cap(db_path, monkeypatch):
    from lionagi.studio.operator import run_detail as run_detail_mod

    called = False

    async def _spy(*args: Any, **kwargs: Any) -> None:
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(run_detail_mod, "get_run", _spy)

    with pytest.raises(pydantic.ValidationError):
        await run_detail_mod.run_detail({"run_id": "x" * 201})

    assert called is False


# ── Cap 2 — status_reason_summary over PER_ITEM_TEXT_CAP (8,000 chars) ──────


async def test_run_detail_status_reason_summary_over_text_cap_is_truncated_and_redacted(db_path):
    from lionagi.studio.operator.redact import PER_ITEM_TEXT_CAP
    from lionagi.studio.operator.run_detail import run_detail

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="failed")

    secret = "ghp_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8"  # noqa: S105 - planted, never real
    sentinel = "SENTINEL-NON-SECRET-MARKER"
    padding = "x" * (PER_ITEM_TEXT_CAP + 500)
    raw_summary = f"{sentinel} leaked credential {secret} then {padding}"
    await _set_status_reason(db_path, sid, "run_failed", raw_summary)

    # Positive control, step 1: the planted secret is present in the raw
    # serialization before any redaction runs.
    assert secret in json.dumps({"status_reason_summary": raw_summary})
    assert len(raw_summary) > PER_ITEM_TEXT_CAP

    # The cap runs on the SCRUBBED string, not the raw one, so the raw length
    # above does not establish that this fixture drives past the cap. State the
    # precondition in the frame the cap actually reads.
    from lionagi.studio.operator.redact import scrub_text

    assert len(scrub_text(raw_summary)) > PER_ITEM_TEXT_CAP

    result = await run_detail({"run_id": sid})

    assert set(result) == _KNOWN_TRUE_KEYS
    assert result["known"] is True
    # Assert the OUTPUT length directly. Without this the whole test survives
    # deleting the truncation slice: every other assertion here is about
    # redaction or about the flag, and the flag is derived rather than
    # independently observed, so it cannot stand in as the cap's witness.
    assert len(result["statusReasonSummary"]) == PER_ITEM_TEXT_CAP
    assert result["truncated"] is True
    # Positive control, step 2: the planted secret is absent from the result.
    assert secret not in json.dumps(result)
    # Positive control, step 3: a non-secret sentinel survives the cap, so a
    # blank-output bug (redacting/truncating everything) cannot pass.
    assert sentinel in result["statusReasonSummary"]

    # Companion negative case: a short summary trips neither this field's
    # truncation nor the handler's aggregate `truncated` flag — proves this
    # row drives past only Cap 2, not some other clamp.
    sid2 = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid2, status="failed")
    await _set_status_reason(db_path, sid2, "run_failed", "short reason, no cap involved")
    result2 = await run_detail({"run_id": sid2})
    assert result2["truncated"] is False
    assert result2["statusReasonSummary"] == "short reason, no cap involved"


async def test_run_detail_summary_flag_follows_the_capped_string_not_the_raw_one(db_path):
    """The rule-separating arm for the truncation flag.

    The over-cap row above can't protect this: there the raw string and the
    scrubbed string are BOTH over the cap, so a flag derived from either one
    reports True and passes under either rule -- no guard against the flag
    drifting back to the raw length.

    This fixture makes the two rules disagree: the summary is a run of
    absolute paths, so scrubbing collapses each to its leaf and pulls the
    length back under the cap (raw is over, scrubbed is well under).
    Nothing is cut, so the honest answer is False -- a raw-derived flag
    would answer True, telling the reader a complete string was clipped.
    """
    from lionagi.studio.operator.redact import PER_ITEM_TEXT_CAP, scrub_text
    from lionagi.studio.operator.run_detail import run_detail

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="failed")

    paths = " ".join(
        f"/Users/someone/khive-work/worktrees/lane-alpha/state/branches/branch-{i:04d}.json"
        for i in range(120)
    )
    raw_summary = f"run failed while reading {paths}"
    await _set_status_reason(db_path, sid, "run_failed", raw_summary)

    # The fixture separates the two rules: this is the assertion that makes the
    # test meaningful, so state it rather than trusting the construction.
    assert len(raw_summary) > PER_ITEM_TEXT_CAP > len(scrub_text(raw_summary))

    result = await run_detail({"run_id": sid})

    assert result["truncated"] is False
    # Nothing was cut, and the proof is the whole scrubbed string coming back:
    # both ends present, at exactly the scrubbed length.
    assert result["statusReasonSummary"] == scrub_text(raw_summary)
    assert result["statusReasonSummary"].startswith("run failed while reading branch-0000.json")
    assert result["statusReasonSummary"].endswith("branch-0119.json")
    assert "/Users/someone" not in result["statusReasonSummary"]


# ── Cap 3 — project over public_project's 160-char clamp (silent) ───────────


async def test_run_detail_long_relative_project_is_clamped_and_says_nothing(db_path):
    """The third cap on this surface, and the one worth naming: a relative
    ``project`` longer than 160 characters is clipped with no signal at all.

    ``truncated`` deliberately does NOT cover it. That flag says "a payload
    you asked for was cut", and ``project`` is a display label rather than a
    payload -- ``public_project`` already rewrites absolute paths to their leaf
    on every run, so a flag that fired on this would fire on ordinary records
    and stop meaning anything. The clamp being silent is a real property of the
    surface either way, so it is asserted here rather than left to be
    rediscovered.
    """
    from lionagi.studio.operator.run_detail import run_detail

    sid = str(uuid.uuid4())
    long_project = "nested/" + "p" * 400
    await seed_session(db_path, session_id=sid, project=long_project, project_source="explicit")

    assert len(long_project) > 160  # the fixture does drive past this cap

    result = await run_detail({"run_id": sid})

    assert result["project"] == long_project[:160]
    assert len(result["project"]) == 160
    assert result["truncated"] is False  # silent, by the reasoning above

    # Companion: a short relative project passes through whole, so this row
    # drives past only Cap 3.
    sid2 = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid2, project="lionagi", project_source="explicit")
    result2 = await run_detail({"run_id": sid2})
    assert result2["project"] == "lionagi"
    assert result2["truncated"] is False


# ── Cap 4 — manifest over ARTIFACT_BYTE_CAP, and projection safety ──────────
#
# These two drive ``_project`` directly rather than going through the DB, and
# the reason is the point of the tests. ``get_run``'s StateDB path constant-
# fills ``manifest``/``task``/``error``/``cwd`` (``{}``/``""``/``None``/
# ``None``) and pre-publicises both roots, so a DB-seeded test of these fields
# asserts on placeholders: it would pass identically with the redaction and the
# byte cap deleted, which is a green test that witnesses nothing. Driving the
# projection directly states plainly that the subject is the projection's
# contract for the values its field names promise, not the values one carrier
# happens to supply today.


async def test_project_manifest_over_byte_cap_is_replaced_and_flagged():
    from lionagi.studio.operator.redact import ARTIFACT_BYTE_CAP
    from lionagi.studio.operator.run_detail import _project

    # Each value exceeds the per-item text cap, so this also pins the order the
    # two caps compose in: per-item first, aggregate byte cap on the result.
    manifest = {f"field{i}": "y" * 9_000 for i in range(400)}
    assert len(json.dumps(manifest)) > ARTIFACT_BYTE_CAP  # fixture drives past it

    fields, truncated = _project({"manifest": manifest})

    assert truncated is True
    assert fields["manifest"] == {
        "truncated": True,
        "reason": "exceeds the artifact byte cap",
    }

    # Companion negative: a manifest under the cap survives whole and trips
    # nothing, so the row above drives past only Cap 4.
    small = {"kind": "agent", "steps": [1, 2, 3]}
    assert len(json.dumps(small)) < ARTIFACT_BYTE_CAP
    fields2, truncated2 = _project({"manifest": small})
    assert truncated2 is False
    assert fields2["manifest"] == small


async def test_project_scrubs_absolute_paths_out_of_the_detail_only_fields():
    from lionagi.studio.operator.run_detail import _project

    leaky = {
        "cwd": "/Users/someone/khive-work/worktrees/lane-x",
        "state_root": "/Users/someone/.lionagi/runs/20260809T101010-abc/state",
        "artifact_root": "/Users/someone/.lionagi/runs/20260809T101010-abc/artifacts",
        "task": "read /Users/someone/private/notes.md and summarise it",
        "error": 'Traceback: File "/Users/someone/private/app.py", line 3',
        "manifest": {"cwd": "/Users/someone/private/wt", "prompt": "cat /Users/someone/id_rsa"},
    }
    # Control: the fixture is genuinely leaky before the projection runs, so a
    # pass below cannot come from having supplied nothing to leak.
    assert "/Users/someone" in json.dumps(leaky)

    fields, _ = _project(leaky)

    assert "/Users/someone" not in json.dumps(fields)
    # And not blanked either -- the leaf survives, so a scrub-everything bug
    # cannot pass this.
    assert fields["cwd"] == "lane-x"
    assert fields["task"] == "read notes.md and summarise it"


# ── Mandatory arm: happy path ────────────────────────────────────────────────


async def test_run_detail_happy_path(db_path):
    from lionagi.studio.operator.run_detail import run_detail

    sid = str(uuid.uuid4())
    await seed_session(
        db_path,
        session_id=sid,
        status="completed",
        name="nightly-triage",
        playbook_name="triage-playbook",
        agent_name="triage-agent",
        model="gpt-5",
        provider="openai",
        effort="high",
        started_at=1000.0,
        ended_at=1050.0,
        created_at=900.0,
        updated_at=1050.0,
        project="acme-research",
        project_source="cli",
        total_cost_usd=1.23,
        input_tokens=100,
        output_tokens=50,
    )

    result = await run_detail({"run_id": sid})

    assert set(result) == _KNOWN_TRUE_KEYS
    assert result["known"] is True
    assert result["source"] == "store"
    assert result["truncated"] is False
    assert result["runId"] == sid
    assert result["id"] == sid
    assert result["status"] == "completed"
    # A terminal run's vacuous "healthy" is dropped from the projection:
    # health is a liveness concept, and "healthy" beside a terminal status
    # reads as a claim about the run's outcome. Only a pathological verdict
    # (leftover locks) would still be projected here.
    assert result["effectiveHealth"] is None
    assert result["project"] == "acme-research"
    assert result["totalCostUsd"] == 1.23
    assert result["task"] == ""
    assert result["error"] is None
    assert result["cwd"] is None
    assert result["manifest"] == {}


# ── Mandatory arm: readable-absent (store opened fine, no such run) ─────────


async def test_run_detail_readable_absent(db_path):
    from lionagi.studio.operator.run_detail import run_detail

    async with StateDB(db_path):
        pass  # forces schema creation without inserting any row

    result = await run_detail({"run_id": str(uuid.uuid4())})

    assert set(result) == _KNOWN_FALSE_KEYS
    assert result == {"known": False, "source": "store"}


# ── Mandatory arm: unavailable store (preflight refuses before the carrier) ─


async def test_run_detail_unavailable_when_store_known_absent(tmp_path, monkeypatch):
    from lionagi.studio.operator import run_detail as run_detail_mod

    missing = tmp_path / "no-such-home" / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", missing)

    called = False

    async def _spy(*args: Any, **kwargs: Any) -> None:
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(run_detail_mod, "get_run", _spy)

    result = await run_detail_mod.run_detail({"run_id": str(uuid.uuid4())})

    assert set(result) == _KNOWN_FALSE_KEYS
    assert result == {"known": False, "source": "unavailable"}
    assert called is False, "preflight refusal must not consult the carrier"


async def test_run_detail_unavailable_when_read_only_open_unsupported(db_path, monkeypatch):
    from lionagi.studio.operator import run_detail as run_detail_mod

    _set_url(monkeypatch, _SERVER_URL)

    called = False

    async def _spy(*args: Any, **kwargs: Any) -> None:
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(run_detail_mod, "get_run", _spy)

    result = await run_detail_mod.run_detail({"run_id": str(uuid.uuid4())})

    assert set(result) == _KNOWN_FALSE_KEYS
    assert result == {"known": False, "source": "unavailable"}
    assert called is False, "preflight refusal must not consult the carrier"


# ── Mandatory arm: actual open failure (preflight passes, carrier throws) ───


async def test_run_detail_actual_open_failure_collapses_to_unavailable(db_path, monkeypatch):
    from lionagi.studio.operator import run_detail as run_detail_mod

    async with StateDB(db_path):
        pass  # store exists and is addressable, so preflight legitimately passes

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("simulated disk read failure")

    monkeypatch.setattr(run_detail_mod, "get_run", _boom)

    result = await run_detail_mod.run_detail({"run_id": str(uuid.uuid4())})

    assert set(result) == _KNOWN_FALSE_KEYS
    assert result == {"known": False, "source": "unavailable"}


async def test_run_detail_store_not_addressable_error_collapses_to_unavailable(
    db_path, monkeypatch
):
    """The carrier can also fail with the package's own not-addressable
    signal (`require_file_store()` raising past `get_run` uncaught) rather
    than a bare OSError — both are caught by the same except clause."""
    from lionagi.studio.operator import run_detail as run_detail_mod
    from lionagi.studio.services._db import StoreNotAddressableError

    async with StateDB(db_path):
        pass

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise StoreNotAddressableError("postgresql")

    monkeypatch.setattr(run_detail_mod, "get_run", _boom)

    result = await run_detail_mod.run_detail({"run_id": str(uuid.uuid4())})

    assert set(result) == _KNOWN_FALSE_KEYS
    assert result == {"known": False, "source": "unavailable"}


# ── Mandatory arm: preflight/carrier race ────────────────────────────────────


async def test_run_detail_race_between_preflight_and_carrier_reports_unavailable(
    db_path, monkeypatch
):
    """The store passes preflight, the carrier legitimately returns None (no
    row), but the store has become unavailable by the time the handler
    re-checks — this must answer `unavailable`, not `store` (a vanished store
    must not be reported the same way as an empty one)."""
    from lionagi.studio.operator import run_detail as run_detail_mod

    async with StateDB(db_path):
        pass  # store exists, so the real carrier call legitimately returns None

    calls = {"n": 0}

    def _flaky_known_absent() -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # available at preflight, absent on the post-None recheck

    monkeypatch.setattr(run_detail_mod, "state_db_known_absent", _flaky_known_absent)
    monkeypatch.setattr(run_detail_mod, "read_only_open_supported", lambda: True)

    result = await run_detail_mod.run_detail({"run_id": str(uuid.uuid4())})

    assert set(result) == _KNOWN_FALSE_KEYS
    assert result == {"known": False, "source": "unavailable"}
    assert calls["n"] == 2, "must recheck availability exactly once after the None"


async def test_run_detail_reports_whether_the_end_was_measured_or_reconstructed(db_path):
    """A reconstructed end has to arrive labelled. The reader has no second
    source for the provenance, so a projection that drops the flag turns a
    guess into a measurement for everyone downstream of it."""
    import sqlite3

    measured_id = str(uuid.uuid4())
    reconstructed_id = str(uuid.uuid4())
    for sid in (measured_id, reconstructed_id):
        await seed_session(
            db_path, session_id=sid, status="completed", started_at=1000.0, ended_at=1050.0
        )

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE sessions SET ended_at_is_approximate = 1 WHERE id = ?", (reconstructed_id,)
        )
        conn.commit()
    finally:
        conn.close()

    from lionagi.studio.operator.run_detail import run_detail

    reconstructed = await run_detail({"run_id": reconstructed_id})
    # Control: the same shape with a measured end reports the other value, so
    # this is a test of the flag rather than of a constant.
    measured = await run_detail({"run_id": measured_id})

    assert reconstructed["endedAtApproximate"] is True
    assert measured["endedAtApproximate"] is False


async def test_every_projection_that_emits_an_end_also_emits_its_provenance():
    """Guards the class rather than the three known sites.

    The flag and the end are one fact. Each was added to its projection by
    hand, and the failure that keeps happening is a projection that carries
    the end and silently drops the label. This reads the source rather than
    the output so that a projection added later is covered without anyone
    remembering to come back here.
    """
    import ast

    roots = [
        Path(__file__).resolve().parents[2] / "lionagi" / "studio",
        Path(__file__).resolve().parents[2] / "lionagi" / "cli",
    ]
    files = [p for root in roots for p in root.rglob("*.py")]
    assert files, "no source files found -- the walk is broken, not the code"

    missing: list[str] = []
    checked = 0
    for path in files:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            if "endedAt" not in keys:
                continue
            checked += 1
            if "endedAtApproximate" not in keys:
                missing.append(f"{path.name}:{node.lineno}")

    # A walk that found no projections would pass this test while proving
    # nothing, so the population is asserted before the result is read.
    assert checked >= 3, f"expected at least 3 endedAt projections, found {checked}"
    assert not missing, "projections emitting endedAt without endedAtApproximate: " + ", ".join(
        missing
    )
