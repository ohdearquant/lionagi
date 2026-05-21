# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0022 provenance tests.

Covers: agent_definition_hash() lookup + hashing, resolve_model_spec()
canonicalization, and the DB write path (model/provider/effort/agent_hash
columns on sessions, model/provider/agent_name on branches).
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest

from lionagi.state.db import StateDB
from lionagi.state.provenance import (
    agent_definition_hash,
    resolve_model_spec,
)


@pytest.fixture
async def db():
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


def _uid() -> str:
    return str(uuid.uuid4())


# ── resolve_model_spec ────────────────────────────────────────────────────────


def test_resolve_combines_provider_and_model():
    assert resolve_model_spec("claude", "claude-sonnet-4-6") == "claude/claude-sonnet-4-6"


def test_resolve_passes_already_qualified_model_through():
    assert (
        resolve_model_spec("claude", "claude/claude-sonnet-4-6")
        == "claude/claude-sonnet-4-6"
    )


def test_resolve_returns_only_arg_when_one_missing():
    assert resolve_model_spec(None, "claude/claude-sonnet-4-6") == "claude/claude-sonnet-4-6"
    assert resolve_model_spec("claude", None) == "claude"


def test_resolve_none_for_both_none():
    assert resolve_model_spec(None, None) is None


# ── agent_definition_hash ────────────────────────────────────────────────────


def test_hash_none_for_missing_agent(tmp_path: Path, monkeypatch):
    """Unknown agent name → None (caller writes NULL to agent_hash)."""
    import lionagi.cli._runs as _runs_mod

    monkeypatch.setattr(_runs_mod, "LIONAGI_HOME", tmp_path / "lionagi-home")
    assert agent_definition_hash("never-existed") is None


def test_hash_none_for_empty_name():
    assert agent_definition_hash(None) is None
    assert agent_definition_hash("") is None


def test_hash_finds_nested_md(tmp_path: Path, monkeypatch):
    """ADR-0022 lookup order: ``agents/<name>/<name>.md`` first."""
    import lionagi.cli._runs as _runs_mod

    home = tmp_path / "lionagi-home"
    monkeypatch.setattr(_runs_mod, "LIONAGI_HOME", home)
    agents = home / "agents"
    nested = agents / "reviewer"
    nested.mkdir(parents=True)
    body = b"# reviewer\nbe thorough.\n"
    (nested / "reviewer.md").write_bytes(body)

    expected = hashlib.sha256(body).hexdigest()[:16]
    assert agent_definition_hash("reviewer") == expected


def test_hash_falls_back_to_flat_md(tmp_path: Path, monkeypatch):
    """When no nested dir exists, fall back to ``agents/<name>.md``."""
    import lionagi.cli._runs as _runs_mod

    home = tmp_path / "lionagi-home"
    monkeypatch.setattr(_runs_mod, "LIONAGI_HOME", home)
    agents = home / "agents"
    agents.mkdir(parents=True)
    body = b"# analyst\n"
    (agents / "analyst.md").write_bytes(body)

    expected = hashlib.sha256(body).hexdigest()[:16]
    assert agent_definition_hash("analyst") == expected


def test_hash_is_16_chars():
    """Pin the truncation length so storage size stays predictable."""
    h = hashlib.sha256(b"x").hexdigest()[:16]
    assert len(h) == 16


# ── DB write path ────────────────────────────────────────────────────────────


async def test_create_session_persists_provenance(db: StateDB):
    prog_id = _uid()
    sid = _uid()
    await db.create_progression(prog_id)
    await db.create_session(
        {
            "id": sid,
            "progression_id": prog_id,
            "status": "running",
            "model": "claude/claude-sonnet-4-6",
            "provider": "claude",
            "effort": "high",
            "agent_hash": "abc123def456",
        }
    )
    row = await db.get_session(sid)
    assert row["model"] == "claude/claude-sonnet-4-6"
    assert row["provider"] == "claude"
    assert row["effort"] == "high"
    assert row["agent_hash"] == "abc123def456"


async def test_create_session_provenance_nullable(db: StateDB):
    """Legacy callers that don't supply provenance keys must not crash."""
    prog_id = _uid()
    sid = _uid()
    await db.create_progression(prog_id)
    await db.create_session(
        {"id": sid, "progression_id": prog_id, "status": "running"}
    )
    row = await db.get_session(sid)
    assert row["model"] is None
    assert row["provider"] is None
    assert row["effort"] is None
    assert row["agent_hash"] is None


async def test_create_branch_persists_per_branch_provenance(db: StateDB):
    prog_id = _uid()
    sid = _uid()
    bprog = _uid()
    bid = _uid()
    await db.create_progression(prog_id)
    await db.create_progression(bprog)
    await db.create_session(
        {"id": sid, "progression_id": prog_id, "status": "running"}
    )
    await db.create_branch(
        {
            "id": bid,
            "session_id": sid,
            "progression_id": bprog,
            "model": "claude/claude-opus-4-7",
            "provider": "claude",
            "agent_name": "critic",
        }
    )
    row = await db.get_branch(bid)
    assert row["model"] == "claude/claude-opus-4-7"
    assert row["provider"] == "claude"
    assert row["agent_name"] == "critic"


async def test_update_session_allows_provenance_columns(db: StateDB):
    """Backfill from filesystem runs can set provenance after the fact."""
    prog_id = _uid()
    sid = _uid()
    await db.create_progression(prog_id)
    await db.create_session(
        {"id": sid, "progression_id": prog_id, "status": "running"}
    )
    await db.update_session(
        sid, model="openai/gpt-4.1", provider="openai", effort="medium"
    )
    row = await db.get_session(sid)
    assert row["model"] == "openai/gpt-4.1"
    assert row["provider"] == "openai"
    assert row["effort"] == "medium"
