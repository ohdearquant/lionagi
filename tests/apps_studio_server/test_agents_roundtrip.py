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
    import lionagi.studio.services.agents as agents_mod

    root = tmp_path / "agents"
    root.mkdir()
    monkeypatch.setattr(agents_mod, "_AGENTS_ROOT", root)
    return root


# ---------------------------------------------------------------------------
# Test 1: yolo/fast_mode surface in get_agent() response
# ---------------------------------------------------------------------------


def test_get_agent_surfaces_yolo_and_fast_mode(tmp_path, monkeypatch):
    from lionagi.studio.services.agents import get_agent

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
# Test 1b: lion_system surfaces in get_agent() response
# ---------------------------------------------------------------------------


def test_get_agent_surfaces_lion_system(tmp_path, monkeypatch):
    """An agent .md with lion_system: false round-trips via get_agent().

    lion_system was added to _KNOWN_FRONTMATTER_KEYS and get_agent() but was
    not covered by the existing round-trip tests — this is the regression the
    PR description targets (contract drift between agents.py and cli/_agents.py).
    """
    from lionagi.studio.services.agents import get_agent

    root = _make_agents_root(tmp_path, monkeypatch)
    md = root / "sysagent.md"
    _write_agent_md(
        md,
        """\
        ---
        provider: claude
        model: claude-sonnet-4-6
        yolo: false
        fast_mode: false
        lion_system: false
        ---
        System prompt here.
        """,
    )

    result = get_agent("sysagent")

    assert result is not None
    assert result.get("lion_system") is False
    # Ensure yolo and fast_mode still surface alongside lion_system
    assert result.get("yolo") is False
    assert result.get("fast_mode") is False


# ---------------------------------------------------------------------------
# Tests 1c/1d/1e: absent bool fields emit CLI defaults
# ---------------------------------------------------------------------------


def test_get_agent_lion_system_defaults_true(tmp_path, monkeypatch):
    """get_agent() on a profile WITHOUT lion_system: key returns lion_system: True.

    The CLI treats absent lion_system as True (lionagi/cli/_agents.py:180,
    lionagi/agent/config.py:53). Studio must emit the same default so callers
    see consistent behaviour regardless of whether the key is present.
    """
    from lionagi.studio.services.agents import get_agent

    root = _make_agents_root(tmp_path, monkeypatch)
    md = root / "no_lionsys.md"
    _write_agent_md(
        md,
        """\
        ---
        provider: claude
        model: claude-sonnet-4-6
        ---
        Body without lion_system key.
        """,
    )

    result = get_agent("no_lionsys")

    assert result is not None
    assert result.get("lion_system") is True, (
        "lion_system absent from frontmatter must default to True (CLI parity)"
    )


# ---------------------------------------------------------------------------
# Test 2: update_agent() writes yolo field to disk and get_agent() reads it back
# ---------------------------------------------------------------------------


def test_update_agent_writes_yolo_field(tmp_path, monkeypatch):
    from lionagi.studio.services.agents import get_agent, update_agent

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
# Test 2b: update_agent() writes lion_system field to disk
# ---------------------------------------------------------------------------


def test_update_agent_writes_lion_system_field(tmp_path, monkeypatch):
    from lionagi.studio.services.agents import get_agent, update_agent

    root = _make_agents_root(tmp_path, monkeypatch)
    md = root / "sysagent2.md"
    _write_agent_md(
        md,
        """\
        ---
        provider: claude
        model: claude-sonnet-4-6
        lion_system: false
        ---
        System prompt here.
        """,
    )

    updated = update_agent("sysagent2", {"lion_system": True})

    assert updated is not None
    assert updated.get("lion_system") is True

    # Confirm disk state via independent get_agent() call
    fresh = get_agent("sysagent2")
    assert fresh is not None
    assert fresh.get("lion_system") is True


# ---------------------------------------------------------------------------
# Test 3: reasoning_effort → effort migration
# ---------------------------------------------------------------------------


def test_get_agent_migrates_reasoning_effort_to_effort(tmp_path, monkeypatch):
    from lionagi.studio.services.agents import get_agent

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
    from lionagi.studio.services.agents import get_agent, update_agent

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
