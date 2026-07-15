# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for `li schedule export`: legacy-row conversion (happy path per
action kind, on_success/on_fail BLOCKED, unsupported action_kind BLOCKED,
malformed trigger BLOCKED, round-trip through validate+apply to a fresh DB),
default declaration/cli re-export (authored_spec round-trip incl. a quoted
notify "on" key, deterministic ordering), and the CLI surface (--output vs
stdout, never writes the database)."""

from __future__ import annotations

import types
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")
pytest.importorskip("croniter", reason="studio extra not installed")

import yaml

from lionagi.state.db import StateDB
from lionagi.studio.services.schedule_declaration import apply_schedule_set, parse_schedule_set
from lionagi.studio.services.schedule_export import (
    build_managed_export_document,
    convert_legacy_rows,
    dump_schedule_set_yaml,
    format_report,
)

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


@pytest.fixture
def agent_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A minimal .lionagi/agents/reviewer.md discoverable via cwd; see
    test_schedule_declaration.py for the identical fixture rationale."""
    import lionagi.cli._providers as providers_mod

    agents_dir = tmp_path / ".lionagi" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "reviewer.md").write_text("---\nmodel: anthropic/claude-sonnet-5\n---\nBody.\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(providers_mod, "_find_lionagi_dirs", lambda: [agents_dir.parent])
    return tmp_path


async def _create_legacy_row(row: dict) -> None:
    async with StateDB() as db:
        await db.create_schedule(row)


def _legacy_row(schedule_id: str, name: str, *, cwd: Path, **overrides) -> dict:
    row = {
        "id": schedule_id,
        "name": name,
        "trigger_type": "cron",
        "cron_expr": "0 2 * * *",
        "action_kind": "agent",
        "action_agent": "reviewer",
        "action_prompt": "check things",
        "action_cwd": str(cwd),
        "enabled": 1,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# Legacy conversion — happy path per action kind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_agent_row_converts_ready(temp_db_path, agent_profile):
    async with StateDB() as db:
        await db.create_schedule(_legacy_row("a1", "demo/nightly", cwd=agent_profile))
        rows = await db.list_schedules()
    doc, lines = convert_legacy_rows(
        rows, flows_dir=agent_profile / "flows", manifest_dir=agent_profile
    )
    assert [line.status for line in lines] == ["READY"]
    assert "demo/nightly" in doc.schedules
    member = doc.schedules["demo/nightly"]
    assert member.target.kind == "agent"
    assert member.target.profile == "reviewer"
    assert member.target.prompt == "check things"


@pytest.mark.asyncio
async def test_legacy_command_row_converts_ready(temp_db_path, agent_profile, monkeypatch):
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_row(
                "c1",
                "demo/refresh",
                cwd=agent_profile,
                trigger_type="interval",
                cron_expr=None,
                interval_sec=900,
                action_kind="command",
                action_agent=None,
                action_prompt=None,
                action_command="refresh-index",
                action_command_args=["incremental"],
            )
        )
        rows = await db.list_schedules()
    doc, lines = convert_legacy_rows(
        rows, flows_dir=agent_profile / "flows", manifest_dir=agent_profile
    )
    assert [line.status for line in lines] == ["READY"]
    member = doc.schedules["demo/refresh"]
    assert member.target.kind == "command"
    assert member.target.executable == "refresh-index"
    assert member.target.args == ["incremental"]
    assert member.trigger.every == "900s"


@pytest.mark.asyncio
async def test_legacy_playbook_row_converts_ready(temp_db_path, agent_profile):
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_row(
                "p1",
                "demo/audit",
                cwd=agent_profile,
                action_kind="play",
                action_agent=None,
                action_prompt=None,
                action_playbook="health-audit",
            )
        )
        rows = await db.list_schedules()
    doc, lines = convert_legacy_rows(
        rows, flows_dir=agent_profile / "flows", manifest_dir=agent_profile
    )
    assert [line.status for line in lines] == ["READY"]
    member = doc.schedules["demo/audit"]
    assert member.target.kind == "playbook"
    assert member.target.name == "health-audit"


@pytest.mark.asyncio
async def test_legacy_flow_yaml_row_converts_ready(temp_db_path, agent_profile):
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_row(
                "f1",
                "demo/nightly-flow",
                cwd=agent_profile,
                trigger_type="interval",
                cron_expr=None,
                interval_sec=3600,
                action_kind="flow_yaml",
                action_agent=None,
                action_prompt=None,
                action_flow_yaml="workers: 2\n",
            )
        )
        rows = await db.list_schedules()
    flows_dir = agent_profile / "flows"
    doc, lines = convert_legacy_rows(rows, flows_dir=flows_dir, manifest_dir=agent_profile)
    assert [line.status for line in lines] == ["READY"]
    member = doc.schedules["demo/nightly-flow"]
    assert member.target.kind == "flow"
    written = Path(member.target.file)
    assert written.is_file()
    assert written.read_text() == "workers: 2\n"


# ---------------------------------------------------------------------------
# Legacy conversion — BLOCKED cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_row_with_on_success_is_blocked_and_omitted(temp_db_path, agent_profile):
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_row(
                "b1",
                "demo/chained",
                cwd=agent_profile,
                on_success={"prompt": "notify done"},
            )
        )
        rows = await db.list_schedules()
    doc, lines = convert_legacy_rows(
        rows, flows_dir=agent_profile / "flows", manifest_dir=agent_profile
    )
    assert len(lines) == 1
    assert lines[0].status == "BLOCKED"
    assert "dependency conversion required" in lines[0].message
    assert "demo/chained" not in doc.schedules


@pytest.mark.asyncio
async def test_legacy_row_with_on_fail_is_blocked_and_omitted(temp_db_path, agent_profile):
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_row("b2", "demo/chained-fail", cwd=agent_profile, on_fail={"prompt": "alert"})
        )
        rows = await db.list_schedules()
    doc, lines = convert_legacy_rows(
        rows, flows_dir=agent_profile / "flows", manifest_dir=agent_profile
    )
    assert lines[0].status == "BLOCKED"
    assert "demo/chained-fail" not in doc.schedules


@pytest.mark.asyncio
async def test_legacy_unsupported_action_kind_is_blocked_and_omitted(temp_db_path, agent_profile):
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_row(
                "u1",
                "demo/unsupported",
                cwd=agent_profile,
                action_kind="fanout",
                action_agent=None,
                action_prompt=None,
            )
        )
        rows = await db.list_schedules()
    doc, lines = convert_legacy_rows(
        rows, flows_dir=agent_profile / "flows", manifest_dir=agent_profile
    )
    assert lines[0].status == "BLOCKED"
    assert "no v1 target equivalent" in lines[0].message
    assert doc.schedules == {}


@pytest.mark.asyncio
async def test_legacy_malformed_trigger_is_blocked_and_omitted(temp_db_path, agent_profile):
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_row("m1", "demo/badcron", cwd=agent_profile, cron_expr="not a cron expr")
        )
        rows = await db.list_schedules()
    doc, lines = convert_legacy_rows(
        rows, flows_dir=agent_profile / "flows", manifest_dir=agent_profile
    )
    assert lines[0].status == "BLOCKED"
    assert "demo/badcron" not in doc.schedules


# ---------------------------------------------------------------------------
# Round-trip: export legacy -> validate -> apply to a fresh DB -> compare
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_export_round_trips_to_a_fresh_db(tmp_path, monkeypatch, agent_profile):
    source_db = tmp_path / "source.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", source_db)
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_row("orig1", "demo/nightly", cwd=agent_profile, action_project="demo")
        )
        rows = await db.list_schedules()

    flows_dir = tmp_path / "export.flows"
    doc, lines = convert_legacy_rows(rows, flows_dir=flows_dir, manifest_dir=agent_profile)
    assert [line.status for line in lines] == ["READY"]

    output_path = tmp_path / "schedules.yaml"
    output_path.write_text(dump_schedule_set_yaml(doc))

    # `li schedule validate` re-parses + statically resolves without writing.
    reparsed = parse_schedule_set(output_path.read_text(), source=str(output_path))

    fresh_db = tmp_path / "fresh.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", fresh_db)
    async with StateDB() as db:
        result = await apply_schedule_set(db, reparsed, output_path.parent)
        assert result.created == 1
        applied = await db.get_schedule_by_name("demo/nightly")
    assert applied is not None
    resolved_target = applied["resolved_target"]["target"]
    assert resolved_target["kind"] == "agent"
    assert resolved_target["profile"] == "reviewer"
    assert resolved_target["prompt"] == "check things"
    assert resolved_target["model"] == "anthropic/claude-sonnet-5"


# ---------------------------------------------------------------------------
# Default export — authored_spec round-trip incl. quoted notify "on"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_export_round_trips_notify_with_quoted_on_key(temp_db_path, agent_profile):
    authored_spec = {
        "description": None,
        "enabled": True,
        "trigger": {
            "cron": {"expression": "0 2 * * *", "timezone": "UTC"},
            "every": None,
            "at": None,
            "github": None,
        },
        "target": {"kind": "agent", "profile": "reviewer", "prompt": "check things", "model": None},
        "execution": {"cwd": str(agent_profile), "project": None},
        "policies": {
            "missedFire": "skip",
            "overlap": "skip",
            "maxRuns": None,
            "budget": None,
            "rateLimit": None,
        },
        "notify": {"on": ["completed", "failed"], "command": "notify-done"},
    }
    async with StateDB() as db:
        await db.create_schedule(
            {
                "id": "d1",
                "name": "demo/nightly",
                "trigger_type": "cron",
                "cron_expr": "0 2 * * *",
                "action_kind": "agent",
                "action_agent": "reviewer",
                "action_prompt": "check things",
                "action_project": "demo",
                "managed_by": "declaration",
                "authored_spec": authored_spec,
                "notify_on": ["completed", "failed"],
                "notify_command": "notify-done",
            }
        )
        rows = await db.list_schedules()

    doc, lines = build_managed_export_document(rows)
    assert [line.status for line in lines] == ["READY"]
    # The row's own project ("demo") matches the document's chosen project,
    # so it is keyed by its local name -- re-applying reconstructs
    # "demo/nightly" exactly (see _member_key).
    member = doc.schedules["nightly"]
    assert member.notify.on == ["completed", "failed"]

    yaml_text = dump_schedule_set_yaml(doc)
    assert '"on":' in yaml_text

    # Round-trips as the string "on", not the boolean True a bare key would
    # parse to under YAML 1.1.
    raw = yaml.safe_load(yaml_text)
    notify_block = raw["schedules"]["nightly"]["notify"]
    assert "on" in notify_block
    assert True not in notify_block
    assert notify_block["on"] == ["completed", "failed"]

    # And the document as a whole is still a valid ScheduleSet.
    reparsed = parse_schedule_set(yaml_text)
    assert reparsed.schedules["nightly"].notify.on == ["completed", "failed"]


@pytest.mark.asyncio
async def test_default_export_is_deterministically_name_sorted(temp_db_path, agent_profile):
    async with StateDB() as db:
        for schedule_id, name in (("z1", "demo/zzz-last"), ("a1", "demo/aaa-first")):
            await db.create_schedule(
                {
                    "id": schedule_id,
                    "name": name,
                    "trigger_type": "cron",
                    "cron_expr": "0 2 * * *",
                    "action_kind": "agent",
                    "action_agent": "reviewer",
                    "action_prompt": "check things",
                    "managed_by": "cli",
                    "authored_spec": {
                        "description": None,
                        "enabled": True,
                        "trigger": {"cron": {"expression": "0 2 * * *", "timezone": "UTC"}},
                        "target": {
                            "kind": "agent",
                            "profile": "reviewer",
                            "prompt": "check things",
                        },
                    },
                }
            )
        rows = await db.list_schedules()

    doc, lines = build_managed_export_document(rows)
    assert list(doc.schedules.keys()) == ["demo/aaa-first", "demo/zzz-last"]
    report_lines = [line.qualified_name for line in lines]
    assert report_lines == ["demo/aaa-first", "demo/zzz-last"]


# ---------------------------------------------------------------------------
# CLI surface — --output vs stdout, never writes the database
# ---------------------------------------------------------------------------


def _export_args(**overrides) -> types.SimpleNamespace:
    base = {"legacy": False, "output": None, "report": None}
    base.update(overrides)
    return types.SimpleNamespace(**base)


def test_cli_export_writes_output_file_and_report_file(temp_db_path, agent_profile, tmp_path):
    # _cmd_export runs its own event loop (asyncio.run) internally, so this
    # test must stay a plain sync function, not @pytest.mark.asyncio.
    import asyncio

    from lionagi.studio.cli import _cmd_export

    asyncio.run(_create_legacy_row(_legacy_row("cli1", "demo/nightly", cwd=agent_profile)))

    output_path = tmp_path / "out" / "schedules.yaml"
    report_path = tmp_path / "out" / "report.txt"
    rc = _cmd_export(_export_args(legacy=True, output=str(output_path), report=str(report_path)))
    assert rc == 0
    assert output_path.is_file()
    doc = yaml.safe_load(output_path.read_text())
    assert doc["kind"] == "ScheduleSet"
    assert "demo/nightly" in doc["schedules"]
    report_text = report_path.read_text()
    assert "READY" in report_text
    assert "1 ready, 0 blocked" in report_text


def test_cli_export_prints_to_stdout_and_stderr_by_default(temp_db_path, agent_profile, capsys):
    import asyncio

    from lionagi.studio.cli import _cmd_export

    asyncio.run(_create_legacy_row(_legacy_row("cli2", "demo/nightly", cwd=agent_profile)))

    rc = _cmd_export(_export_args(legacy=True))
    assert rc == 0
    captured = capsys.readouterr()
    assert "kind: ScheduleSet" in captured.out
    assert "READY" in captured.err


def test_export_never_writes_the_database(temp_db_path, agent_profile, tmp_path):
    import asyncio

    from lionagi.studio.cli import _cmd_export

    async def _setup():
        async with StateDB() as db:
            await db.create_schedule(_legacy_row("cli3", "demo/nightly", cwd=agent_profile))
            return await db.list_schedules()

    before = asyncio.run(_setup())

    output_path = tmp_path / "schedules.yaml"
    rc = _cmd_export(_export_args(legacy=True, output=str(output_path)))
    assert rc == 0

    async def _reread():
        async with StateDB() as db:
            return await db.list_schedules()

    after = asyncio.run(_reread())

    assert len(before) == len(after) == 1
    assert before[0]["updated_at"] == after[0]["updated_at"]
    assert before[0]["enabled"] == after[0]["enabled"]
