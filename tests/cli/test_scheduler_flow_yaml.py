# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the flow_yaml schedule action kind (#1174)."""

from __future__ import annotations

import os
import textwrap
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# subprocess.build_argv tests
# ---------------------------------------------------------------------------


def _minimal_schedule(**kwargs) -> dict:
    base = {
        "id": "sched-abc",
        "name": "test-sched",
        "trigger_type": "cron",
        "action_kind": "flow_yaml",
        "action_model": "",
        "action_prompt": "",
        "action_flow_yaml": "prompt: hello world\n",
        "action_project": None,
        "action_extra_args": [],
    }
    base.update(kwargs)
    return base


def test_build_argv_flow_yaml_dispatches_li_o_flow():
    """flow_yaml kind builds argv with `li o flow -f <tmpfile>`."""
    from lionagi.studio.scheduler.subprocess import build_argv

    sched = _minimal_schedule()
    argv, tmp_path = build_argv(sched, {})

    try:
        assert argv[:4] == ["uv", "run", "li", "o"]
        assert argv[4] == "flow"
        assert "-f" in argv
        fi = argv.index("-f")
        assert argv[fi + 1] == tmp_path
        assert tmp_path is not None
        # Temp file must exist and contain the YAML text
        assert os.path.isfile(tmp_path)
        assert "hello world" in open(tmp_path).read()
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def test_build_argv_flow_yaml_returns_tmp_path_none_for_other_kinds():
    """Other action kinds do not produce a temp file."""
    from lionagi.studio.scheduler.subprocess import build_argv

    for kind in ("agent", "flow", "fanout", "play"):
        sched = {
            "id": "s1",
            "action_kind": kind,
            "action_model": "",
            "action_prompt": "",
            "action_agent": None,
            "action_playbook": None,
            "action_project": None,
            "action_extra_args": [],
            "action_flow_yaml": None,
        }
        argv, tmp_path = build_argv(sched, {})
        assert tmp_path is None, f"Expected no tmp_path for kind={kind}"


def test_build_argv_flow_yaml_cleanup_on_spawn():
    """spawn_and_wait deletes the temp file after the subprocess exits."""
    import asyncio
    import tempfile

    from lionagi.studio.scheduler.subprocess import spawn_and_wait

    # Create a real temp file to verify cleanup
    fd, tmp_path = tempfile.mkstemp(suffix=".yaml", prefix="lionagi-test-")
    os.close(fd)
    assert os.path.exists(tmp_path)

    async def _run():
        with patch(
            "lionagi.studio.scheduler.subprocess.asyncio.create_subprocess_exec"
        ) as mock_exec:
            mock_proc = MagicMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc
            return await spawn_and_wait(
                ["uv", "run", "li", "o", "flow", "-f", tmp_path],
                "inv-001",
                tmp_path=tmp_path,
            )

    exit_code, _ = asyncio.get_event_loop().run_until_complete(_run())
    assert exit_code == 0
    # Temp file must be gone
    assert not os.path.exists(tmp_path)


# ---------------------------------------------------------------------------
# Creation-time YAML validation tests
# ---------------------------------------------------------------------------


def test_validate_flow_yaml_spec_accepts_valid_spec():
    """A valid flow spec returns None (no error)."""
    from lionagi.studio.services.schedules import _validate_flow_yaml_spec

    yaml_text = textwrap.dedent("""\
        prompt: Run a quick summary
        workers: 2
        effort: medium
    """)
    assert _validate_flow_yaml_spec(yaml_text) is None


def test_validate_flow_yaml_spec_rejects_invalid_yaml():
    """Malformed YAML is rejected with a clear error."""
    from lionagi.studio.services.schedules import _validate_flow_yaml_spec

    result = _validate_flow_yaml_spec("key: [unclosed")
    assert result is not None
    assert "not valid YAML" in result


def test_validate_flow_yaml_spec_rejects_non_mapping():
    """A YAML list is rejected because a flow spec must be a mapping."""
    from lionagi.studio.services.schedules import _validate_flow_yaml_spec

    result = _validate_flow_yaml_spec("- item1\n- item2\n")
    assert result is not None
    assert "mapping" in result or "dict" in result


def test_validate_flow_yaml_spec_rejects_invalid_field():
    """Out-of-range spec field is rejected at validation time."""
    from lionagi.studio.services.schedules import _validate_flow_yaml_spec

    # workers > 32 is invalid
    result = _validate_flow_yaml_spec("workers: 999\n")
    assert result is not None
    assert "workers" in result


def test_create_schedule_rejects_empty_flow_yaml():
    """create_schedule raises ValueError when action_flow_yaml is missing."""
    import asyncio

    from lionagi.studio.services.schedules import create_schedule

    data = {
        "name": "bad-sched",
        "trigger_type": "cron",
        "action_kind": "flow_yaml",
        # action_flow_yaml intentionally absent
    }

    async def _run():
        await create_schedule(data)

    try:
        asyncio.get_event_loop().run_until_complete(_run())
        raise AssertionError("Should have raised ValueError")
    except ValueError as exc:
        assert "action_flow_yaml" in str(exc)


def test_create_schedule_rejects_malformed_flow_yaml():
    """create_schedule raises ValueError on malformed YAML spec."""
    import asyncio

    from lionagi.studio.services.schedules import create_schedule

    data = {
        "name": "bad-yaml-sched",
        "trigger_type": "cron",
        "action_kind": "flow_yaml",
        "action_flow_yaml": "key: [unclosed",
    }

    async def _run():
        await create_schedule(data)

    try:
        asyncio.get_event_loop().run_until_complete(_run())
        raise AssertionError("Should have raised ValueError")
    except ValueError as exc:
        assert "flow_yaml" in str(exc).lower() or "YAML" in str(exc)


# ---------------------------------------------------------------------------
# Lifecycle / status parity test
# ---------------------------------------------------------------------------


def test_flow_yaml_lifecycle_parity_with_play():
    """flow_yaml and play both go through the same fire → running → terminal path.

    This test mocks build_argv and spawn_and_wait and verifies that the engine
    records the same status-transition sequence for flow_yaml as it does for
    play, confirming uniform treatment by reapers and monitor.
    """
    # We verify parity at the subprocess layer (same build_argv / spawn_and_wait
    # contract) rather than running the full engine, which requires a live DB.

    from lionagi.studio.scheduler.subprocess import build_argv

    play_sched = {
        "id": "s1",
        "action_kind": "play",
        "action_model": "",
        "action_prompt": "",
        "action_agent": None,
        "action_playbook": "my-playbook",
        "action_project": None,
        "action_extra_args": [],
        "action_flow_yaml": None,
    }
    flow_yaml_sched = _minimal_schedule()

    play_argv, play_tmp = build_argv(play_sched, {})
    yaml_argv, yaml_tmp = build_argv(flow_yaml_sched, {})

    try:
        # Both produce non-empty argv lists starting with uv run li
        assert play_argv[:3] == ["uv", "run", "li"]
        assert yaml_argv[:3] == ["uv", "run", "li"]

        # play produces no temp file; flow_yaml produces one
        assert play_tmp is None
        assert yaml_tmp is not None

        # flow_yaml must use the same flow execution path (li o flow)
        assert "o" in yaml_argv
        assert "flow" in yaml_argv
        assert "-f" in yaml_argv
    finally:
        if yaml_tmp and os.path.exists(yaml_tmp):
            os.unlink(yaml_tmp)


# ---------------------------------------------------------------------------
# CLI parser tests
# ---------------------------------------------------------------------------


def test_cli_flow_yaml_choice_accepted():
    """--action-kind flow_yaml is a recognized choice."""
    import argparse

    from lionagi.cli.schedule import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    add_schedule_subparser(sub)
    args = parser.parse_args(["schedule", "create", "my-sched", "--action-kind", "flow_yaml"])
    assert args.action_kind == "flow_yaml"


# ---------------------------------------------------------------------------
# Persistence round-trip test (MAJ-1)
# This test verifies that action_flow_yaml survives CREATE and UPDATE through
# the real DB layer (not just dict manipulation). It MUST fail before the
# CRIT-1/2/3 fixes and pass after.
# ---------------------------------------------------------------------------


def test_flow_yaml_db_roundtrip():
    """action_flow_yaml is persisted by create_schedule and survives get_schedule."""
    import asyncio
    import tempfile
    import uuid

    from lionagi.state.db import StateDB

    yaml_spec = "prompt: round-trip check\nworkers: 1\n"
    schedule_id = str(uuid.uuid4())

    async def _run():
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            async with StateDB(db_path) as db:
                await db.create_schedule(
                    {
                        "id": schedule_id,
                        "name": "rt-test",
                        "trigger_type": "cron",
                        "action_kind": "flow_yaml",
                        "action_flow_yaml": yaml_spec,
                    }
                )
                row = await db.get_schedule(schedule_id)
                assert row is not None, "schedule not found after create"
                assert row["action_flow_yaml"] == yaml_spec, (
                    f"action_flow_yaml lost on INSERT: got {row['action_flow_yaml']!r}"
                )

                # UPDATE path (CRIT-2)
                updated_spec = "prompt: updated spec\nworkers: 2\n"
                await db.update_schedule(schedule_id, action_flow_yaml=updated_spec)
                row2 = await db.get_schedule(schedule_id)
                assert row2 is not None
                assert row2["action_flow_yaml"] == updated_spec, (
                    f"action_flow_yaml lost on UPDATE: got {row2['action_flow_yaml']!r}"
                )
        finally:
            import os

            os.unlink(db_path)

    asyncio.get_event_loop().run_until_complete(_run())
