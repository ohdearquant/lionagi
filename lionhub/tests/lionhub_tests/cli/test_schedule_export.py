# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Schedule export command adapter behavior."""

from __future__ import annotations

import types

import pytest
import yaml

from ..orchestration.schedule import _test_support_schedule_export as _support
from ..orchestration.schedule._test_support_schedule_export import _legacy_row

# Fixtures shared with the support module, bound here so they apply to this module only.
agent_profile = _support.agent_profile
temp_db_path = _support.temp_db_path

from lionagi.state.db import StateDB
from lionhub.orchestration.schedule.declaration import build_plan, parse_schedule_set

ADOPTION_COLUMNS = {
    "authored_spec",
    "managed_by",
    "owner_key",
    "resolved_digest",
    "resolved_target",
    "resolved_timezone",
    "spec_version",
    "updated_at",
}


async def _create_legacy_row(row: dict) -> None:
    async with StateDB() as db:
        await db.create_schedule(row)


def _export_args(**overrides) -> types.SimpleNamespace:
    base = {"legacy": False, "output": None, "report": None}
    base.update(overrides)
    return types.SimpleNamespace(**base)


def test_cli_export_writes_output_file_and_report_file(temp_db_path, agent_profile, tmp_path):
    # _cmd_export runs its own event loop (asyncio.run) internally, so this
    # test must stay a plain sync function, not @pytest.mark.asyncio.
    import asyncio

    from lionhub.cli.schedule import _cmd_export

    asyncio.run(_create_legacy_row(_legacy_row("cli1", "demo/nightly", cwd=agent_profile)))

    output_path = tmp_path / "out" / "schedules.yaml"
    report_path = tmp_path / "out" / "report.txt"
    rc = _cmd_export(_export_args(legacy=True, output=str(output_path), report=str(report_path)))
    assert rc == 0
    assert output_path.is_file()
    doc = yaml.safe_load(output_path.read_text())
    assert doc["kind"] == "ScheduleSet"
    assert "nightly" in doc["schedules"]
    report_text = report_path.read_text()
    assert "READY" in report_text
    assert "1 ready, 0 blocked" in report_text


def test_cli_export_prints_to_stdout_and_stderr_by_default(temp_db_path, agent_profile, capsys):
    import asyncio

    from lionhub.cli.schedule import _cmd_export

    asyncio.run(_create_legacy_row(_legacy_row("cli2", "demo/nightly", cwd=agent_profile)))

    rc = _cmd_export(_export_args(legacy=True))
    assert rc == 0
    captured = capsys.readouterr()
    assert "kind: ScheduleSet" in captured.out
    assert "READY" in captured.err


def test_export_never_writes_the_database(temp_db_path, agent_profile, tmp_path):
    import asyncio

    from lionhub.cli.schedule import _cmd_export

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


def test_cli_export_returns_partial_exit_code_when_a_row_is_blocked(
    temp_db_path, agent_profile, tmp_path
):
    import asyncio

    from lionhub.cli.schedule import EXIT_EXPORT_PARTIAL, _cmd_export

    asyncio.run(_create_legacy_row(_legacy_row("cli-ready", "demo/nightly", cwd=agent_profile)))
    asyncio.run(
        _create_legacy_row(
            _legacy_row(
                "cli-blocked",
                "demo/chained",
                cwd=agent_profile,
                on_success={"prompt": "notify done"},
            )
        )
    )

    output_path = tmp_path / "out" / "schedules.yaml"
    report_path = tmp_path / "out" / "report.txt"
    rc = _cmd_export(_export_args(legacy=True, output=str(output_path), report=str(report_path)))
    assert rc == EXIT_EXPORT_PARTIAL
    assert rc != 0
    # The document and report are still emitted exactly as on a clean export.
    assert output_path.is_file()
    doc = yaml.safe_load(output_path.read_text())
    assert "nightly" in doc["schedules"]
    report_text = report_path.read_text()
    assert "READY" in report_text
    assert "BLOCKED" in report_text
    assert "1 ready, 1 blocked" in report_text


def test_cli_export_rejects_sibling_filename_collisions(temp_db_path, agent_profile, tmp_path):
    """Two projects whose sanitized filename tokens collide must fail loudly
    before any sibling file is written, never silently overwrite one."""
    import asyncio

    from lionhub.cli.schedule import _cmd_export

    asyncio.run(
        _create_legacy_row(
            _legacy_row("sc1", "foo/bar/a", cwd=agent_profile, action_project="foo/bar")
        )
    )
    asyncio.run(
        _create_legacy_row(
            _legacy_row("sc2", "foo:bar/b", cwd=agent_profile, action_project="foo:bar")
        )
    )

    output_path = tmp_path / "out" / "schedules.yaml"
    rc = _cmd_export(_export_args(legacy=True, output=str(output_path)))
    assert rc == 1
    assert not any((tmp_path / "out").glob("*.yaml")) if (tmp_path / "out").exists() else True


def test_cli_export_distinct_sibling_tokens_write_all_files(temp_db_path, agent_profile, tmp_path):
    import asyncio

    from lionhub.cli.schedule import _cmd_export

    asyncio.run(
        _create_legacy_row(_legacy_row("sd1", "alpha/a", cwd=agent_profile, action_project="alpha"))
    )
    asyncio.run(
        _create_legacy_row(_legacy_row("sd2", "beta/b", cwd=agent_profile, action_project="beta"))
    )

    output_path = tmp_path / "out" / "schedules.yaml"
    rc = _cmd_export(_export_args(legacy=True, output=str(output_path)))
    assert rc == 0
    names = sorted(p.name for p in (tmp_path / "out").glob("*.yaml"))
    assert names == ["schedules.alpha.yaml", "schedules.beta.yaml"]


def test_legacy_export_reapplies_unchanged_as_its_own_set(temp_db_path, agent_profile, monkeypatch):
    """A row created before schedule sets existed, exported with --legacy, must
    re-apply to itself without changing: adopted as the set its document names,
    it keeps every column it ran with and gains only the adoption record, then
    applies again as that set's own row with no difference and no change."""
    import asyncio

    from lionhub.cli.schedule import _cmd_apply_set, _cmd_export

    # The zone the row was recorded firing in; adoption confirms it only when
    # the scheduler runs in that zone.
    monkeypatch.setattr("lionhub.orchestration.schedule.timing.SCHEDULER_TZ", "America/New_York")

    async def _seed():
        async with StateDB() as db:
            await db.create_schedule(
                _legacy_row(
                    "rt1",
                    "demo/nightly",
                    cwd=agent_profile,
                    action_project=None,
                    # Not the profile's own model, so a dropped model would show.
                    action_model="openai/gpt-5",
                )
            )

    asyncio.run(_seed())

    output_path = agent_profile / "export" / "schedules.yaml"
    assert _cmd_export(_export_args(legacy=True, output=str(output_path))) == 0
    doc = parse_schedule_set(output_path.read_text(), source=str(output_path))
    assert (doc.metadata.project, doc.metadata.name) == ("demo", "legacy-export")

    async def _rows():
        async with StateDB() as db:
            return {row["id"]: row for row in await db.list_schedules()}

    async def _plan(adopt: bool):
        async with StateDB() as db:
            return (await build_plan(db, doc, output_path.parent, adopt=adopt))[0]

    before = asyncio.run(_rows())
    plan = asyncio.run(_plan(adopt=True))
    assert [(entry.qualified_name, entry.action) for entry in plan] == [
        ("demo/nightly", "ADOPT")
    ], [entry.detail for entry in plan]
    assert (
        _cmd_apply_set(
            types.SimpleNamespace(file=str(output_path), dry_run=False, adopt=True, as_json=False)
        )
        == 0
    )

    after = asyncio.run(_rows())
    assert set(after) == set(before)
    was, stored = before["rt1"], after["rt1"]
    assert stored["name"] == "demo/nightly"
    assert stored["action_model"] == "openai/gpt-5"
    assert stored["action_project"] is None
    # Adoption writes the ownership and the declaration it adopted, and nothing else.
    assert stored["owner_key"] == "demo/legacy-export"
    assert stored["managed_by"] == "declaration"
    assert stored["spec_version"] == doc.apiVersion
    assert stored["resolved_digest"]
    assert stored["authored_spec"]
    assert stored["resolved_target"]
    assert stored["resolved_timezone"] == was["effective_timezone"]
    changed = {key for key in set(was) | set(stored) if was.get(key) != stored.get(key)}
    assert changed == ADOPTION_COLUMNS, sorted(changed)

    plan = asyncio.run(_plan(adopt=False))
    assert [(entry.qualified_name, entry.action) for entry in plan] == [
        ("demo/nightly", "UNCHANGED")
    ], [entry.detail for entry in plan]
    assert (
        _cmd_apply_set(
            types.SimpleNamespace(file=str(output_path), dry_run=False, adopt=False, as_json=False)
        )
        == 0
    )
    assert asyncio.run(_rows()) == after
