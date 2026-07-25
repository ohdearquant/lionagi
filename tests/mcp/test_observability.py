# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the observability/control MCP tools.

The CLI helpers each tool sits on are stubbed, so these assert two things: the
typed parameters reach the underlying call unchanged, and the value handed back
has the shape the tool's docstring promises.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastmcp", reason="requires the 'mcp' extra")

from fastmcp import FastMCP  # noqa: E402 — must follow the extra guard

from lionagi.mcp.observability import register_observability_tools  # noqa: E402


@pytest.fixture
def tools():
    return register_observability_tools(FastMCP("test-observability"))


@pytest.fixture
def db_present(monkeypatch):
    monkeypatch.setattr("lionagi.mcp.observability._state_db_exists", lambda: True)


def _record(monkeypatch, target, result):
    """Replace an async helper with a recorder returning *result*."""
    seen: dict = {}

    async def fake(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return result

    monkeypatch.setattr(target, fake)
    return seen


def test_every_tool_is_registered_on_the_server(tools):
    assert set(tools) == {
        "monitor_running",
        "monitor_entity",
        "monitor_wait_for_runs",
        "kill_entity",
        "kill_stale_entities",
        "stats_runs",
        "doctor",
        "dispatch_list",
        "dispatch_show",
        "dispatch_ack",
        "dispatch_retry",
        "dispatch_purge",
        "state_list_sessions",
        "state_stats",
        "state_checkpoint",
        "state_vacuum",
        "state_prune",
        "state_doctor",
        "state_import_runs",
        "state_import_teams",
    }


def test_no_tool_takes_an_extra_args_escape_hatch(tools):
    import inspect

    for name, fn in tools.items():
        params = inspect.signature(fn).parameters
        assert "extra_args" not in params, name


# ── monitor ───────────────────────────────────────────────────────────────────


def test_monitor_running_passes_window_type_and_project(tools, monkeypatch, db_present):
    seen = _record(
        monkeypatch,
        "lionagi.mcp.observability._snapshot",
        {"entities": [{"id": "abc", "type": "play"}], "count": 1},
    )
    monkeypatch.setattr("lionagi.cli.monitor._since_timestamp", lambda w: 1234.0)

    out = tools["monitor_running"](since="2h", entity_type="play", project="lionagi")

    assert seen["kwargs"] == {"since": 1234.0, "entity_type": "play", "project": "lionagi"}
    assert out == {"entities": [{"id": "abc", "type": "play"}], "count": 1}


def test_monitor_running_defaults_to_no_window(tools, monkeypatch, db_present):
    seen = _record(monkeypatch, "lionagi.mcp.observability._snapshot", {"entities": [], "count": 0})

    tools["monitor_running"]()

    assert seen["kwargs"] == {"since": None, "entity_type": None, "project": None}


def test_monitor_running_rejects_an_unknown_entity_type(tools, db_present):
    with pytest.raises(ValueError, match="entity_type must be one of"):
        tools["monitor_running"](entity_type="branch")


def test_monitor_running_is_empty_without_a_state_db(tools, monkeypatch):
    monkeypatch.setattr("lionagi.mcp.observability._state_db_exists", lambda: False)

    assert tools["monitor_running"]() == {"entities": [], "count": 0}


def test_monitor_entity_returns_row_branches_and_plays(tools, monkeypatch, db_present):
    detail = {
        "found": True,
        "entity_type": "session",
        "entity": {"id": "s1", "status": "running"},
        "branches": [{"name": "reviewer", "status": "completed"}],
        "plays": [],
    }
    seen = _record(monkeypatch, "lionagi.mcp.observability._entity_detail", detail)

    out = tools["monitor_entity"]("s1")

    assert seen["args"] == ("s1",)
    assert out == detail


def test_monitor_wait_rejects_an_unbounded_wait(tools):
    with pytest.raises(ValueError, match="max_wait must be positive"):
        tools["monitor_wait_for_runs"](["r1"], max_wait=0)


def test_monitor_wait_rejects_an_empty_id_list(tools):
    with pytest.raises(ValueError, match="at least one"):
        tools["monitor_wait_for_runs"]([])


def test_monitor_wait_passes_poll_settings_through(tools, monkeypatch, db_present):
    result = {
        "finished": True,
        "timed_out": False,
        "all_succeeded": True,
        "runs": [{"id": "r1", "status": "completed"}],
        "sessions": [],
        "unresolved": [],
        "still_pending": [],
    }
    seen = _record(monkeypatch, "lionagi.mcp.observability._wait_for_runs", result)

    out = tools["monitor_wait_for_runs"](
        ["r1", "r2"], interval=1.5, chain=False, follow=True, max_wait=30.0
    )

    assert seen["args"] == (["r1", "r2"],)
    assert seen["kwargs"] == {
        "interval": 1.5,
        "chain": False,
        "follow": True,
        "max_wait": 30.0,
    }
    assert out == result


# ── kill ──────────────────────────────────────────────────────────────────────


def test_kill_entity_passes_reason_recursion_and_grace(tools, monkeypatch):
    killed = {
        "ok": True,
        "entity_type": "session",
        "entity_id": "s1",
        "killed": [{"entity_type": "session", "entity_id": "s1", "pid": 42, "signal": "SIGTERM"}],
        "blocked": [],
    }
    seen = _record(monkeypatch, "lionagi.mcp.observability._kill_entity", killed)

    out = tools["kill_entity"]("s1", reason="stuck", recursive=True, grace_seconds=2.5)

    assert seen["args"] == ("s1",)
    assert seen["kwargs"] == {"reason": "stuck", "recursive": True, "grace_seconds": 2.5}
    assert out == killed


def test_kill_stale_reports_counters_and_echoes_dry_run(tools, monkeypatch):
    seen = _record(
        monkeypatch,
        "lionagi.cli.kill.sweep_stale",
        {
            "cancelled": 3,
            "skipped_recent": 1,
            "skipped_live_pid": 2,
            "skipped_unverifiable_pid": 0,
        },
    )

    out = tools["kill_stale_entities"](threshold_seconds=60, reason="sweep", dry_run=True)

    assert seen["kwargs"] == {
        "threshold_seconds": 60,
        "user_reason": "sweep",
        "grace_seconds": 5.0,
        "dry_run": True,
    }
    assert out == {
        "cancelled": 3,
        "skipped_recent": 1,
        "skipped_live_pid": 2,
        "skipped_unverifiable_pid": 0,
        "dry_run": True,
    }


def test_sweep_stale_is_the_seam_the_cli_summary_reports(capsys):
    """`li kill --all-stale` prints exactly the counters the tool returns."""
    import asyncio

    from lionagi.cli import kill as kill_mod

    async def fake_sweep(**kwargs):
        return {
            "cancelled": 2,
            "skipped_recent": 1,
            "skipped_live_pid": 0,
            "skipped_unverifiable_pid": 4,
        }

    original = kill_mod.sweep_stale
    kill_mod.sweep_stale = fake_sweep
    try:
        rc = asyncio.run(kill_mod._do_kill_all_stale(threshold_seconds=3600))
    finally:
        kill_mod.sweep_stale = original

    out = capsys.readouterr().out
    assert rc == 0
    assert "cancelled 2 stale entities" in out
    assert "skipped_recent=1" in out
    assert "skipped_unverifiable_pid=4" in out


# ── stats / doctor ────────────────────────────────────────────────────────────


def test_stats_runs_validates_and_forwards_group_keys(tools, monkeypatch):
    seen = _record(
        monkeypatch,
        "lionagi.cli.stats._run_stats_runs",
        [
            {
                "project": "lionagi",
                "model": "opus",
                "run_count": 5,
                "completed": 4,
                "failed": 1,
                "first_at": 1.0,
                "last_at": 2.0,
            }
        ],
    )

    out = tools["stats_runs"](since="1d", group_by=["project", "model"])

    assert seen["kwargs"]["group_by"] == ["project", "model"]
    assert isinstance(seen["kwargs"]["since"], float)
    assert out["since"] == "1d"
    assert out["group_by"] == ["project", "model"]
    assert out["rows"][0]["run_count"] == 5
    assert out["rows"][0]["first_at"] == "1970-01-01T00:00:01+00:00"


def test_stats_runs_defaults_to_project_and_kind(tools, monkeypatch):
    seen = _record(monkeypatch, "lionagi.cli.stats._run_stats_runs", [])

    out = tools["stats_runs"]()

    assert seen["kwargs"]["group_by"] == ["project", "kind"]
    assert out["rows"] == []


def test_stats_runs_rejects_a_bad_group_key(tools):
    with pytest.raises(ValueError, match="Unknown --group-by key"):
        tools["stats_runs"](group_by=["nonsense"])


def test_stats_runs_rejects_a_non_positive_window(tools):
    with pytest.raises(ValueError, match="must be positive"):
        tools["stats_runs"](since="0d")


def test_doctor_splits_failures_from_warnings(tools, monkeypatch):
    monkeypatch.setattr(
        "lionagi.cli.doctor.collect_checks",
        lambda: {
            "python": {"status": "ok", "detail": "fine"},
            "studio_daemon": {"status": "warn", "detail": "unreachable"},
            "dep:psutil": {"status": "fail", "detail": "ImportError"},
        },
    )

    out = tools["doctor"]()

    assert out["ok"] is False
    assert out["failed"] == ["dep:psutil"]
    assert out["warned"] == ["studio_daemon"]
    assert out["checks"]["python"]["detail"] == "fine"


def test_doctor_runs_against_the_real_environment(tools):
    out = tools["doctor"]()

    assert set(out) == {"ok", "failed", "warned", "checks"}
    assert out["checks"]["import:lionagi.cli.main"]["status"] == "ok"
    assert out["ok"] == (out["failed"] == [])


# ── dispatch ──────────────────────────────────────────────────────────────────


def test_dispatch_list_forwards_status_and_limit(tools, monkeypatch):
    seen = _record(
        monkeypatch,
        "lionagi.mcp.observability._dispatch_list",
        {"rows": [{"id": "d1", "status": "pending"}], "count": 1},
    )

    out = tools["dispatch_list"](status="pending", limit=5)

    assert seen["kwargs"] == {"status": "pending", "limit": 5}
    assert out["count"] == 1


def test_dispatch_show_reports_not_found_as_data(tools, monkeypatch):
    _record(
        monkeypatch,
        "lionagi.mcp.observability._dispatch_show",
        {"found": False, "dispatch": None},
    )

    assert tools["dispatch_show"]("nope") == {"found": False, "dispatch": None}


def test_dispatch_ack_and_retry_report_whether_the_cas_applied(tools, monkeypatch):
    _record(
        monkeypatch,
        "lionagi.mcp.observability._dispatch_ack",
        {"applied": False, "dispatch_id": "d1"},
    )
    _record(
        monkeypatch,
        "lionagi.mcp.observability._dispatch_retry",
        {"applied": True, "dispatch_id": "d1"},
    )

    assert tools["dispatch_ack"]("d1", "tok")["applied"] is False
    assert tools["dispatch_retry"]("d1")["applied"] is True


def test_dispatch_purge_refuses_an_unscoped_bulk_delete(tools):
    with pytest.raises(ValueError, match="whole outbox"):
        tools["dispatch_purge"]()


def test_dispatch_purge_routes_an_id_to_the_single_row_path(tools, monkeypatch):
    seen = _record(
        monkeypatch,
        "lionagi.mcp.observability._dispatch_purge_one",
        {"found": True, "deleted": 1, "dry_run": False},
    )
    bulk = _record(monkeypatch, "lionagi.mcp.observability._dispatch_purge_bulk", {})

    out = tools["dispatch_purge"](dispatch_id="d1")

    assert seen["args"] == ("d1",)
    assert seen["kwargs"] == {"dry_run": False}
    assert bulk == {}
    assert out["deleted"] == 1


def test_dispatch_purge_routes_criteria_to_the_bulk_path(tools, monkeypatch):
    seen = _record(
        monkeypatch,
        "lionagi.mcp.observability._dispatch_purge_bulk",
        {"deleted": 4, "total": 4, "dry_run": True, "by_status": {"expired": 4}},
    )

    out = tools["dispatch_purge"](status="expired", before=99.0, dry_run=True)

    assert seen["kwargs"] == {"status": "expired", "before": 99.0, "dry_run": True}
    assert out["by_status"] == {"expired": 4}


# ── state ─────────────────────────────────────────────────────────────────────


def test_state_list_sessions_forwards_limit_and_status(tools, monkeypatch, db_present):
    seen = _record(
        monkeypatch,
        "lionagi.mcp.observability._list_sessions",
        {"sessions": [{"id": "s1", "branch_count": 2}], "count": 1},
    )

    out = tools["state_list_sessions"](limit=10, status="failed")

    assert seen["kwargs"] == {"limit": 10, "status": "failed"}
    assert out["sessions"][0]["branch_count"] == 2


def test_state_checkpoint_rejects_an_unknown_mode(tools):
    with pytest.raises(ValueError, match="mode must be one of"):
        tools["state_checkpoint"](mode="SOMETIMES")


def test_state_checkpoint_forwards_the_mode(tools, monkeypatch):
    seen = _record(monkeypatch, "lionagi.cli.state._checkpoint", "busy=0, log_pages=0")

    out = tools["state_checkpoint"](mode="PASSIVE")

    assert seen["args"] == ("PASSIVE",)
    assert out == {"mode": "PASSIVE", "result": "busy=0, log_pages=0"}


def test_state_prune_forwards_retention_and_echoes_dry_run(tools, monkeypatch):
    seen = _record(
        monkeypatch,
        "lionagi.cli.state._prune",
        {"sessions": 7, "branches": 12, "messages": 0},
    )

    out = tools["state_prune"](keep_days=3, keep_n=5, dry_run=True)

    assert seen["kwargs"] == {"keep_days": 3, "keep_n": 5, "dry_run": True}
    assert out == {"sessions": 7, "branches": 12, "messages": 0, "dry_run": True}


def test_state_doctor_forwards_threshold_and_target_status(tools, monkeypatch):
    seen = _record(
        monkeypatch,
        "lionagi.cli.state._doctor",
        {"running": 4, "swept": 2, "skipped": 2},
    )

    out = tools["state_doctor"](stale_hours=6, new_status="failed", dry_run=False)

    assert seen["kwargs"] == {"stale_hours": 6, "dry_run": False, "new_status": "failed"}
    assert out == {
        "running": 4,
        "swept": 2,
        "skipped": 2,
        "new_status": "failed",
        "dry_run": False,
    }


def test_state_doctor_rejects_a_status_outside_the_vocabulary(tools):
    with pytest.raises(ValueError, match="new_status must be"):
        tools["state_doctor"](new_status="cancelled")


def test_state_import_tools_return_their_counters(tools, monkeypatch):
    _record(
        monkeypatch,
        "lionagi.cli.state._import_runs",
        {"sessions": 3, "branches": 4, "messages": 9, "skipped": 1, "errors": 0},
    )
    _record(
        monkeypatch,
        "lionagi.cli.state._import_teams",
        {"teams": 1, "messages": 2, "skipped_teams": 0, "errors": 0},
    )

    assert tools["state_import_runs"]()["sessions"] == 3
    assert tools["state_import_teams"]()["teams"] == 1


def test_state_vacuum_reports_sizes_around_the_rebuild(tools, monkeypatch, tmp_path):
    db = tmp_path / "state.db"
    db.write_bytes(b"x" * 100)
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db)
    _record(monkeypatch, "lionagi.cli.state._vacuum", None)

    out = tools["state_vacuum"]()

    assert out == {"ok": True, "bytes_before": 100, "bytes_after": 100}
