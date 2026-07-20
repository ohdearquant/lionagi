# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for `li schedule create <kind> <name> ...` typed quick-create:
per-kind compilation into a ScheduleMember, resolution via the exact
resolve_member() path a ScheduleSet member uses, trigger validation
surfaces, and additive compatibility with the legacy flat create form."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")
pytest.importorskip("croniter", reason="studio extra not installed")

from lionagi.state.db import StateDB


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


@pytest.fixture
def agent_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A minimal .lionagi/agents/reviewer.md discoverable via cwd, and cwd
    itself carrying no project markers (git/config.toml) so quick-create
    project auto-detection resolves to None -- see test_schedule_declaration.py
    for the identical rationale on pinning _find_lionagi_dirs directly."""
    import lionagi.cli._providers as providers_mod

    agents_dir = tmp_path / ".lionagi" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "reviewer.md").write_text("---\nmodel: anthropic/claude-sonnet-5\n---\nBody.\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(providers_mod, "_find_lionagi_dirs", lambda: [agents_dir.parent])
    return tmp_path


def _run(kind: str, argv: list[str]) -> int:
    from lionagi.studio.cli import run_schedule_quick_create

    return run_schedule_quick_create(kind, argv)


async def _get(name: str) -> dict:
    async with StateDB() as db:
        row = await db.get_schedule_by_name(name)
    assert row is not None, f"schedule {name!r} was not created"
    return row


async def _get_or_none(name: str) -> dict | None:
    async with StateDB() as db:
        return await db.get_schedule_by_name(name)


# ---------------------------------------------------------------------------
# Per-kind happy paths
# ---------------------------------------------------------------------------


def test_quick_create_agent_at_trigger(temp_db_path, agent_profile, capsys):
    rc = _run(
        "agent",
        [
            "release-check",
            "--profile",
            "reviewer",
            "--prompt",
            "Check the release candidate",
            "--at",
            "2026-07-15T09:00:00-04:00",
        ],
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Created:" in out
    row = asyncio.run(_get("release-check"))
    assert row["managed_by"] == "cli"
    assert row["owner_key"] is None
    assert row["spec_version"] == "lionagi.io/v1alpha1"
    assert row["action_kind"] == "agent"
    assert row["action_agent"] == "reviewer"
    assert row["action_model"] == "anthropic/claude-sonnet-5"
    assert row["action_prompt"] == "Check the release candidate"
    assert row["trigger_type"] == "at"
    assert row["max_runs"] == 1  # forced by an 'at' trigger
    assert row["next_fire_at"] is not None
    assert row["action_cwd"] == str(agent_profile)
    assert row["resolved_digest"]
    assert row["resolved_target"]["target"]["kind"] == "agent"


def test_quick_create_agent_prompt_file(temp_db_path, agent_profile):
    prompt_file = agent_profile / "prompt.txt"
    prompt_file.write_text("Summarize overnight activity")
    rc = _run(
        "agent",
        [
            "from-file",
            "--profile",
            "reviewer",
            "--prompt-file",
            str(prompt_file),
            "--every",
            "1h",
        ],
    )
    assert rc == 0
    row = asyncio.run(_get("from-file"))
    assert row["action_prompt"] == "Summarize overnight activity"


def test_quick_create_flow_cron_trigger(temp_db_path, agent_profile):
    flow_file = agent_profile / "flow.yaml"
    flow_file.write_text(
        "agents:\n  - id: a1\n    model: anthropic/claude-sonnet-5\n    prompt: hi\n"
    )
    rc = _run(
        "flow",
        [
            "nightly-review",
            "--cron",
            "0 2 * * *",
            "--timezone",
            "America/New_York",
            "--file",
            str(flow_file),
        ],
    )
    assert rc == 0
    row = asyncio.run(_get("nightly-review"))
    assert row["action_kind"] == "flow_yaml"
    assert row["trigger_type"] == "cron"
    assert row["cron_expr"] == "0 2 * * *"
    assert row["resolved_timezone"] == "America/New_York"
    assert "agents:" in row["action_flow_yaml"]


def test_quick_create_playbook_every_trigger(temp_db_path, agent_profile):
    rc = _run(
        "playbook",
        [
            "health-audit",
            "--every",
            "6h",
            "--playbook",
            "health-audit",
            "--arg",
            "project=lionagi",
        ],
    )
    assert rc == 0
    row = asyncio.run(_get("health-audit"))
    assert row["action_kind"] == "play"
    assert row["action_playbook"] == "health-audit"
    assert row["trigger_type"] == "interval"
    assert row["interval_sec"] == 6 * 3600
    assert row["resolved_target"]["target"]["args"] == {"project": "lionagi"}


def test_quick_create_command_argv_form(temp_db_path, agent_profile, monkeypatch):
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    rc = _run(
        "command",
        ["refresh", "--every", "15m", "--", "refresh-index", "full"],
    )
    assert rc == 0
    row = asyncio.run(_get("refresh"))
    assert row["action_kind"] == "command"
    assert row["action_command"] == "refresh-index"
    assert row["action_command_args"] == ["full"]


# ---------------------------------------------------------------------------
# --once sugar
# ---------------------------------------------------------------------------


def test_quick_create_once_is_max_runs_one(temp_db_path, agent_profile):
    rc = _run(
        "agent",
        ["once-job", "--profile", "reviewer", "--prompt", "hi", "--every", "1h", "--once"],
    )
    assert rc == 0
    row = asyncio.run(_get("once-job"))
    assert row["max_runs"] == 1


def test_quick_create_once_and_max_runs_mutually_exclusive(temp_db_path, agent_profile, capsys):
    rc = _run(
        "agent",
        [
            "conflict",
            "--profile",
            "reviewer",
            "--prompt",
            "hi",
            "--every",
            "1h",
            "--once",
            "--max-runs",
            "5",
        ],
    )
    assert rc == 1
    assert "mutually exclusive" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Trigger validation failure surfaces -- each rejects with zero DB writes
# ---------------------------------------------------------------------------


def test_quick_create_bad_cron_expression_rejected(temp_db_path, agent_profile, capsys):
    rc = _run(
        "agent",
        [
            "bad-cron",
            "--profile",
            "reviewer",
            "--prompt",
            "hi",
            "--cron",
            "not a cron",
            "--timezone",
            "UTC",
        ],
    )
    assert rc == 1
    assert "cron" in capsys.readouterr().err.lower()
    assert asyncio.run(_get_or_none("bad-cron")) is None


def test_quick_create_cron_missing_timezone_rejected(temp_db_path, agent_profile, capsys):
    rc = _run(
        "agent",
        ["missing-tz", "--profile", "reviewer", "--prompt", "hi", "--cron", "0 2 * * *"],
    )
    assert rc == 1
    assert "--timezone" in capsys.readouterr().err


def test_quick_create_space_separated_at_rejected(temp_db_path, agent_profile, capsys):
    rc = _run(
        "agent",
        [
            "space-at",
            "--profile",
            "reviewer",
            "--prompt",
            "hi",
            "--at",
            "2026-07-15 09:00:00-04:00",
        ],
    )
    assert rc == 1
    assert "RFC 3339" in capsys.readouterr().err


def test_quick_create_zero_duration_every_rejected(temp_db_path, agent_profile, capsys):
    rc = _run(
        "agent",
        ["zero-every", "--profile", "reviewer", "--prompt", "hi", "--every", "0m"],
    )
    assert rc == 1
    assert "positive" in capsys.readouterr().err


def test_quick_create_malformed_github_filter_rejected(temp_db_path, agent_profile, capsys):
    rc = _run(
        "agent",
        [
            "bad-gh",
            "--profile",
            "reviewer",
            "--prompt",
            "hi",
            "--github",
            "owner/repo",
            "--github-filter",
            '{"bogus": true}',
        ],
    )
    assert rc == 1
    assert "unknown key" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Command target: never a shell string
# ---------------------------------------------------------------------------


def test_quick_create_command_requires_trailing_separator(temp_db_path, agent_profile, capsys):
    rc = _run("command", ["no-sep", "--every", "15m", "refresh-index", "full"])
    assert rc == 1
    assert "--" in capsys.readouterr().err


def test_quick_create_command_rejects_single_shell_string_token(
    temp_db_path, agent_profile, monkeypatch, capsys
):
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    rc = _run(
        "command",
        ["shell-like", "--every", "15m", "--", "refresh-index --incremental"],
    )
    assert rc == 1
    assert asyncio.run(_get_or_none("shell-like")) is None


# ---------------------------------------------------------------------------
# Name qualification: <project>/<name> when a project is detected
# ---------------------------------------------------------------------------


def test_quick_create_qualifies_name_with_detected_project(temp_db_path, agent_profile):
    config_dir = agent_profile / ".lionagi"
    (config_dir / "config.toml").write_text('[project]\nname = "demo"\n')
    rc = _run(
        "agent",
        ["nightly", "--profile", "reviewer", "--prompt", "hi", "--every", "1h"],
    )
    assert rc == 0
    row = asyncio.run(_get("demo/nightly"))
    assert row["action_project"] == "demo"


# ---------------------------------------------------------------------------
# Duplicate-name collisions (self and cross-owner-from-apply)
# ---------------------------------------------------------------------------


def test_quick_create_duplicate_name_rejected(temp_db_path, agent_profile, capsys):
    rc = _run("agent", ["dup", "--profile", "reviewer", "--prompt", "hi", "--every", "1h"])
    assert rc == 0
    rc2 = _run("agent", ["dup", "--profile", "reviewer", "--prompt", "hi", "--every", "1h"])
    assert rc2 == 1
    assert "already exists" in capsys.readouterr().err


def test_apply_collides_with_quick_created_schedule(temp_db_path, agent_profile):
    """A later ScheduleSet apply targeting the same qualified name as a
    CLI quick-create row must still hit the ownership-collision guard."""
    from lionagi.studio.services.schedule_declaration import (
        ScheduleSetError,
        apply_schedule_set,
        parse_schedule_set,
    )

    (agent_profile / ".lionagi" / "config.toml").write_text('[project]\nname = "demo"\n')

    rc = _run("agent", ["nightly", "--profile", "reviewer", "--prompt", "hi", "--every", "1h"])
    assert rc == 0
    assert asyncio.run(_get_or_none("demo/nightly")) is not None

    manifest = f"""
apiVersion: lionagi.io/v1alpha1
kind: ScheduleSet
metadata:
  name: automation
  project: demo
schedules:
  nightly:
    trigger:
      cron:
        expression: "0 2 * * *"
        timezone: America/New_York
    target:
      kind: agent
      profile: reviewer
      prompt: "check things"
    execution:
      cwd: {agent_profile}
"""
    doc = parse_schedule_set(manifest)

    async def _apply():
        async with StateDB() as db:
            await apply_schedule_set(db, doc, agent_profile)

    with pytest.raises(ScheduleSetError):
        asyncio.run(_apply())


# ---------------------------------------------------------------------------
# Additive compatibility: the legacy flat `create` form is untouched
# ---------------------------------------------------------------------------


def test_legacy_create_dispatch_unaffected_by_quick_create_kinds(monkeypatch):
    """`li schedule create NAME ...` (NAME not a reserved kind token) must
    still be routed to the legacy HTTP-backed _cmd_create, not quick-create."""
    import lionagi.studio.cli as schedule_cli
    from lionagi.cli.main import main

    captured = {}

    def fake_api(path, method="GET", body=None):
        captured.update(path=path, method=method, body=body)
        return {"id": "legacy-id", "name": body.get("name") if body else None}

    monkeypatch.setattr(schedule_cli, "_api", fake_api)
    rc = main(["schedule", "create", "my-legacy-sched", "--cron", "0 * * * *", "--prompt", "ping"])
    assert rc == 0
    assert captured["method"] == "POST"
    assert captured["body"]["name"] == "my-legacy-sched"


def test_quick_create_kind_tokens_reserved_from_legacy_positional(monkeypatch):
    """A quick-create kind token as the first arg after 'create' is always
    routed to typed quick-create, even without further matching flags --
    this is the documented additive-compatibility tradeoff."""
    import lionagi.studio.cli as schedule_cli

    called = {}
    monkeypatch.setattr(
        schedule_cli,
        "run_schedule_quick_create",
        lambda kind, argv: called.update(kind=kind, argv=argv) or 0,
    )
    from lionagi.cli.main import main

    rc = main(
        ["schedule", "create", "agent", "foo", "--profile", "x", "--prompt", "y", "--every", "1h"]
    )
    assert rc == 0
    assert called["kind"] == "agent"
    assert called["argv"] == ["foo", "--profile", "x", "--prompt", "y", "--every", "1h"]
