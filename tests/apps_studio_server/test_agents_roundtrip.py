# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for #1010 — Studio agent frontmatter field completeness.

Covers:
1. yolo/fast_mode round-trip through get_agent().
2. update_agent() writes yolo field to disk.
3. reasoning_effort→effort migration in get_agent().
4. model without provider prefix round-trips through update_agent().
"""
from __future__ import annotations

import textwrap

import pytest

# Studio extra may not be installed in all environments.
pytest.importorskip("fastapi", reason="studio extra not installed")
pytest.importorskip("yaml", reason="PyYAML not installed")


def _write_agent_md(path, content: str) -> None:
    path.write_text(textwrap.dedent(content))


def _make_agents_root(tmp_path, monkeypatch):
    """Point _AGENTS_ROOT at a temp directory."""
    import apps.studio.server.services.agents as agents_mod

    root = tmp_path / "agents"
    root.mkdir()
    monkeypatch.setattr(agents_mod, "_AGENTS_ROOT", root)
    return root


# ---------------------------------------------------------------------------
# Test 1: yolo/fast_mode surface in get_agent() response
# ---------------------------------------------------------------------------


def test_get_agent_surfaces_yolo_and_fast_mode(tmp_path, monkeypatch):
    """An agent .md with yolo: true and fast_mode: false round-trips via get_agent()."""
    from apps.studio.server.services.agents import get_agent

    root = _make_agents_root(tmp_path, monkeypatch)
    md = root / "myagent.md"
    _write_agent_md(
        md,
        """\
        ---
        provider: claude
        model: claude-sonnet-4-6
        yolo: true
        fast_mode: false
        ---
        System prompt here.
        """,
    )

    result = get_agent("myagent")

    assert result is not None
    assert result.get("yolo") is True
    assert result.get("fast_mode") is False


# ---------------------------------------------------------------------------
# Test 2: update_agent() writes yolo field to disk and get_agent() reads it back
# ---------------------------------------------------------------------------


def test_update_agent_writes_yolo_field(tmp_path, monkeypatch):
    """update_agent(name, {'yolo': False}) persists yolo: false and get_agent() returns it."""
    from apps.studio.server.services.agents import get_agent, update_agent

    root = _make_agents_root(tmp_path, monkeypatch)
    md = root / "myagent.md"
    _write_agent_md(
        md,
        """\
        ---
        provider: claude
        model: claude-sonnet-4-6
        yolo: true
        ---
        System prompt here.
        """,
    )

    updated = update_agent("myagent", {"yolo": False})

    assert updated is not None
    assert updated.get("yolo") is False

    # Confirm disk state via independent get_agent() call
    fresh = get_agent("myagent")
    assert fresh is not None
    assert fresh.get("yolo") is False


# ---------------------------------------------------------------------------
# Test 3: reasoning_effort → effort migration
# ---------------------------------------------------------------------------


def test_get_agent_migrates_reasoning_effort_to_effort(tmp_path, monkeypatch):
    """A file with reasoning_effort: high is returned with effort: 'high', no reasoning_effort key."""
    from apps.studio.server.services.agents import get_agent

    root = _make_agents_root(tmp_path, monkeypatch)
    md = root / "legacy.md"
    _write_agent_md(
        md,
        """\
        ---
        provider: claude
        model: claude-sonnet-4-6
        reasoning_effort: high
        ---
        Legacy agent.
        """,
    )

    result = get_agent("legacy")

    assert result is not None
    assert result.get("effort") == "high"
    assert "reasoning_effort" not in result


# ---------------------------------------------------------------------------
# Test 4: model without provider prefix round-trips through update_agent()
# ---------------------------------------------------------------------------


def test_update_agent_canonicalises_model_with_provider(tmp_path, monkeypatch):
    """model: claude-sonnet-4-6 (no prefix) + provider: claude → model: 'claude/claude-sonnet-4-6'."""
    from apps.studio.server.services.agents import get_agent, update_agent

    root = _make_agents_root(tmp_path, monkeypatch)
    md = root / "noprefix.md"
    _write_agent_md(
        md,
        """\
        ---
        provider: claude
        model: claude-sonnet-4-6
        ---
        Agent body.
        """,
    )

    updated = update_agent("noprefix", {"provider": "claude", "model": "claude-sonnet-4-6"})

    assert updated is not None
    assert updated.get("model") == "claude/claude-sonnet-4-6"
