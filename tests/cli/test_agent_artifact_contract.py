# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0029 teardown integration tests for artifact contract verification in agent.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lionagi import Branch
from lionagi.cli._persist import setup_agent_persist as _setup_live_persist
from lionagi.cli._persist import teardown_agent_persist as _teardown_live_persist
from lionagi.state.db import StateDB


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


# ── test_teardown_no_contract_unchanged ──────────────────────────────────────


async def test_teardown_no_contract_unchanged(temp_db_path: Path):
    """Session without artifact_contract → teardown exits with status as resolved by exit code."""
    branch = Branch(name="b")
    ctx = await _setup_live_persist(branch, agent_name="a1")
    assert ctx is not None
    await _teardown_live_persist(ctx, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    # No contract means verification is skipped; status remains completed.
    assert s["status"] == "completed"
    assert s["artifact_contract_json"] is None
    assert s["artifact_verification_json"] is None


# ── test_teardown_contract_passed_stays_completed ────────────────────────────


async def test_teardown_contract_passed_stays_completed(temp_db_path: Path, tmp_path: Path):
    """Required artifact present → verification passed, session stays completed."""
    artifacts_dir = tmp_path / "arts"
    artifacts_dir.mkdir()
    (artifacts_dir / "report.md").write_text("review content")

    branch = Branch(name="b")
    contract = {"expected": [{"id": "report", "path": "report.md", "required": True}]}
    ctx = await _setup_live_persist(
        branch,
        agent_name="reviewer",
        artifacts_path=str(artifacts_dir),
        artifact_contract=contract,
    )
    assert ctx is not None
    await _teardown_live_persist(ctx, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "completed"
    v = s["artifact_verification_json"]
    assert isinstance(v, dict)
    assert v["status"] == "passed"


# ── test_teardown_contract_failed_overrides_completed ────────────────────────


async def test_teardown_contract_failed_overrides_completed(temp_db_path: Path, tmp_path: Path):
    """Required artifact missing on clean exit → status overridden to failed with FAILED_MISSING_ARTIFACT."""
    artifacts_dir = tmp_path / "arts"
    artifacts_dir.mkdir()
    # report.md intentionally not written

    branch = Branch(name="b")
    contract = {"expected": [{"id": "report", "path": "report.md", "required": True}]}
    ctx = await _setup_live_persist(
        branch,
        agent_name="reviewer",
        artifacts_path=str(artifacts_dir),
        artifact_contract=contract,
    )
    assert ctx is not None
    await _teardown_live_persist(ctx, status="completed")

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "failed"
    assert s["status_reason_code"] == "run.failed.missing_artifact"

    # Evidence refs reference the missing artifact id.
    evidence_raw = s["status_evidence_refs"]
    evidence = json.loads(evidence_raw) if isinstance(evidence_raw, str) else evidence_raw
    assert isinstance(evidence, list)
    assert any(e.get("id") == "report" for e in evidence)

    v = s["artifact_verification_json"]
    assert isinstance(v, dict)
    assert v["status"] == "failed"


# ── test_teardown_already_failed_keeps_original_reason ───────────────────────


async def test_teardown_already_failed_keeps_original_reason(temp_db_path: Path, tmp_path: Path):
    """Missing artifact on an already-failed run preserves original reason, not FAILED_MISSING_ARTIFACT."""
    artifacts_dir = tmp_path / "arts"
    artifacts_dir.mkdir()
    # report.md intentionally not written

    branch = Branch(name="b")
    contract = {"expected": [{"id": "report", "path": "report.md", "required": True}]}
    ctx = await _setup_live_persist(
        branch,
        agent_name="reviewer",
        artifacts_path=str(artifacts_dir),
        artifact_contract=contract,
    )
    assert ctx is not None
    exc = RuntimeError("agent crashed")
    await _teardown_live_persist(ctx, status="failed", exception=exc)

    async with StateDB() as db:
        s = await db.get_session(ctx["session_id"])
    assert s is not None
    assert s["status"] == "failed"
    # Original crash reason preserved — artifact code must NOT override it.
    assert s["status_reason_code"] == "run.failed.exception"

    # Verification still ran and stored.
    v = s["artifact_verification_json"]
    assert isinstance(v, dict)
    assert v["status"] == "failed"
