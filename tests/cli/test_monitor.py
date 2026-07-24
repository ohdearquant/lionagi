# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for `li monitor` — real-time entity observation CLI."""

from __future__ import annotations

import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from lionagi.cli.monitor import (
    _NON_TTY_MAX_COL_WIDTH,
    _cached_detect_project,
    _colour_status,
    _elapsed,
    _find_entity,
    _format_coordination_line,
    _format_table,
    _gather_table_rows,
    _invocation_to_row,
    _parse_json_field,
    _pid_alive,
    _play_to_row,
    _render_branch_lines,
    _run_detail,
    _run_table,
    _session_to_row,
    _show_project_matches,
    _show_to_row,
    _since_timestamp,
    _stdout_is_tty,
    _trunc,
)
from lionagi.state.db import StateDB


class _FakeStdout:
    """Minimal stand-in for sys.stdout exposing only isatty() — enough to
    drive call-time TTY detection without a real terminal/pipe."""

    def __init__(self, is_tty: bool) -> None:
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test temp DB; patch DEFAULT_DB_PATH so StateDB() opens it."""
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr("lionagi.cli.monitor._run_table", _run_table)  # identity; force DB path
    return db_path


@pytest.fixture(autouse=True)
def _clear_project_cache() -> None:
    """_show_project_matches caches detect_project() results by repo path
    for the monitor process lifetime — clear it around every test so a
    monkeypatched detect_project in one test can't leak a cached result
    into another test that reuses the same repo path."""
    _cached_detect_project.cache_clear()
    yield
    _cached_detect_project.cache_clear()


async def _make_session(
    db: StateDB,
    *,
    status: str = "running",
    project: str | None = None,
    invocation_kind: str | None = "agent",
    model: str | None = "claude-3-5-sonnet",
    effort: str | None = "medium",
    provider: str | None = "anthropic",
    invocation_id: str | None = None,
) -> str:
    sid = uuid.uuid4().hex[:12]
    pid = uuid.uuid4().hex
    await db.create_progression(pid)
    await db.create_session(
        {
            "id": sid,
            "progression_id": pid,
            "status": status,
            "invocation_kind": invocation_kind,
            "project": project,
            "model": model,
            "effort": effort,
            "provider": provider,
            "started_at": time.time(),
            "invocation_id": invocation_id,
        }
    )
    return sid


async def _make_invocation(
    db: StateDB,
    *,
    status: str = "running",
    skill: str = "show",
    node_metadata: dict | None = None,
) -> str:
    inv_id = uuid.uuid4().hex[:12]
    await db.create_invocation(
        {
            "id": inv_id,
            "skill": skill,
            "started_at": time.time(),
            "status": status,
            "node_metadata": node_metadata,
        }
    )
    return inv_id


async def _make_show(
    db: StateDB,
    *,
    status: str = "active",
    topic: str = "test-topic",
    repo: str | None = None,
) -> str:
    show_id = uuid.uuid4().hex[:12]
    await db.create_show(
        {
            "id": show_id,
            "topic": topic,
            "status": status,
            "repo": repo,
            "show_dir": "/tmp/show",
        }
    )
    return show_id


async def _make_play(
    db: StateDB,
    show_id: str,
    *,
    status: str = "running",
    name: str = "play-1",
    session_id: str | None = None,
) -> str:
    play_id = uuid.uuid4().hex[:12]
    await db.create_play(
        {
            "id": play_id,
            "show_id": show_id,
            "name": name,
            "status": status,
            "session_id": session_id,
            "started_at": time.time(),
        }
    )
    return play_id


# ── Unit: formatting helpers ──────────────────────────────────────────────────


def test_elapsed_none_start():
    assert _elapsed(None) == "-"


def test_elapsed_seconds():
    assert _elapsed(100.0, ended_at=145.0) == "45s"


def test_elapsed_minutes():
    assert _elapsed(100.0, ended_at=190.0) == "1m30s"


def test_elapsed_hours():
    assert _elapsed(100.0, ended_at=7300.0) == "2h00m"


def test_trunc_short():
    assert _trunc("hello", 10) == "hello"


def test_trunc_long():
    result = _trunc("hello world", 8)
    assert len(result) == 8
    assert result.endswith("…")


def test_since_timestamp_hours(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("lionagi.cli.monitor.time.time", lambda: 100_000.0)
    assert _since_timestamp("1h") == 96_400.0


def test_since_timestamp_minutes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("lionagi.cli.monitor.time.time", lambda: 100_000.0)
    assert _since_timestamp("30m") == 98_200.0


def test_since_timestamp_days(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("lionagi.cli.monitor.time.time", lambda: 200_000.0)
    assert _since_timestamp("2d") == 27_200.0


def test_since_timestamp_invalid():
    # "3x" passes int parsing but 'x' is not a known unit
    with pytest.raises(ValueError):
        _since_timestamp("3x")


def test_since_timestamp_bad_unit():
    with pytest.raises(ValueError, match="Unknown time unit"):
        _since_timestamp("5z")


def test_colour_status_running():
    result = _colour_status("running")
    # Should contain the word "running"
    assert "running" in result


def test_colour_status_unknown():
    result = _colour_status("some_unknown_status")
    # Unknown statuses are returned as-is
    assert result == "some_unknown_status"


def test_pid_alive_none():
    assert _pid_alive(None) is None


def test_pid_alive_own_process():
    import os

    assert _pid_alive(os.getpid()) is True


def test_pid_alive_nonexistent():
    # PID 0 is reserved on POSIX; sending a signal to it has special semantics.
    # Use a very high PID unlikely to exist instead.
    result = _pid_alive(9_999_999)
    assert result is False or result is None  # platform-dependent


# ── Unit: table formatting ────────────────────────────────────────────────────


def test_format_table_empty():
    output = _format_table([])
    assert "no running" in output.lower() or output.strip() == ""


def test_format_table_one_row():
    rows = [
        {
            "id": "abc123",
            "type": "session",
            "project": "myproject",
            "status": "running",
            "phase": "agent",
            "elapsed": "5m30s",
            "agents": "1",
        }
    ]
    output = _format_table(rows)
    assert "abc123" in output
    assert "session" in output
    assert "myproject" in output
    assert "running" in output


def test_format_table_header():
    rows = [
        {
            "id": "x",
            "type": "y",
            "project": "z",
            "status": "running",
            "phase": "-",
            "elapsed": "-",
            "agents": "-",
        }
    ]
    output = _format_table(rows)
    assert "ID" in output
    assert "TYPE" in output
    assert "STATUS" in output
    assert "ELAPSED" in output


def test_format_table_non_tty_never_truncates_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """Piped (non-TTY) output must never truncate identifying columns —
    a long project name used to come back as 'ohdearquant/l…', silently
    breaking `li monitor | grep <full-name>`. TTY-ness is patched on the
    real sys.stdout (not the cached module global) to prove detection
    happens at render time."""
    monkeypatch.setattr(sys, "stdout", _FakeStdout(False))
    long_project = "ohdearquant/lionagi-super-long-repo-name"
    rows = [
        {
            "id": "abc123",
            "type": "session",
            "project": long_project,
            "status": "running",
            "phase": "agent",
            "elapsed": "5m",
            "agents": "1",
        }
    ]
    output = _format_table(rows)
    assert long_project in output


def test_format_table_tty_still_truncates_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """A TTY dashboard keeps the compact fixed-width behavior — only piped
    output widens/never-truncates."""
    monkeypatch.setattr(sys, "stdout", _FakeStdout(True))
    long_project = "ohdearquant/lionagi-super-long-repo-name"
    rows = [
        {
            "id": "abc123",
            "type": "session",
            "project": long_project,
            "status": "running",
            "phase": "agent",
            "elapsed": "5m",
            "agents": "1",
        }
    ]
    output = _format_table(rows)
    assert long_project not in output
    assert "…" in output


def test_format_table_tty_detected_at_call_time_not_import_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same process/import must render both ways depending on stdout
    *at call time* — a module imported while a TTY was attached, then
    later invoked against redirected/piped stdout (or vice versa), must
    not get stuck on whatever stdout looked like at import."""
    long_project = "ohdearquant/lionagi-super-long-repo-name"
    rows = [
        {
            "id": "abc123",
            "type": "session",
            "project": long_project,
            "status": "running",
            "phase": "agent",
            "elapsed": "5m",
            "agents": "1",
        }
    ]

    monkeypatch.setattr(sys, "stdout", _FakeStdout(True))
    tty_output = _format_table(rows)
    assert long_project not in tty_output

    monkeypatch.setattr(sys, "stdout", _FakeStdout(False))
    piped_output = _format_table(rows)
    assert long_project in piped_output


def test_stdout_is_tty_reflects_current_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdout", _FakeStdout(True))
    assert _stdout_is_tty() is True
    monkeypatch.setattr(sys, "stdout", _FakeStdout(False))
    assert _stdout_is_tty() is False


def test_format_table_non_tty_caps_pathological_width(monkeypatch: pytest.MonkeyPatch) -> None:
    """One pathologically long value must not blow up padding for every
    row — layout width is capped at _NON_TTY_MAX_COL_WIDTH; the value
    itself still prints in full (never clipped), just without alignment
    padding past the cap."""
    monkeypatch.setattr(sys, "stdout", _FakeStdout(False))
    huge_project = "x" * 10_000
    rows = [
        {
            "id": "abc123",
            "type": "session",
            "project": huge_project,
            "status": "running",
            "phase": "agent",
            "elapsed": "5m",
            "agents": "1",
        },
        {
            "id": "def456",
            "type": "session",
            "project": "short-project",
            "status": "running",
            "phase": "agent",
            "elapsed": "3m",
            "agents": "1",
        },
    ]
    output = _format_table(rows)
    lines = output.splitlines()

    # The full pathological value is still present — grep never misses it.
    assert huge_project in output

    # But layout (header separator, and every OTHER row's padding) must not
    # scale with the 10k-char value: bounded by the column-width ceiling,
    # not by the longest value seen.
    separator = lines[1]
    assert len(separator) < 10 * _NON_TTY_MAX_COL_WIDTH

    short_line = next(line for line in lines if "short-project" in line)
    assert len(short_line) < 10 * _NON_TTY_MAX_COL_WIDTH


# ── Unit: row builders ────────────────────────────────────────────────────────


def test_session_to_row():
    sess = {
        "id": "abc123def456",
        "invocation_kind": "agent",
        "project": "lionagi",
        "status": "running",
        "agent_name": "coder",
        "model": "claude-3-5-sonnet",
        "started_at": time.time() - 100,
    }
    row = _session_to_row(sess)
    assert row["type"] == "agent"
    assert row["project"] == "lionagi"
    assert row["status"] == "running"
    assert row["phase"] == "coder"


def test_session_to_row_no_optional():
    sess = {
        "id": "abc123def456",
        "status": "running",
    }
    row = _session_to_row(sess)
    assert row["project"] == "-"
    assert row["phase"] == "-"


def test_session_to_row_current_phase_wins():
    """A live flow phase overrides the static orchestrator/playbook name."""
    sess = {
        "id": "abc123def456",
        "invocation_kind": "play",
        "status": "running",
        "agent_name": "orchestrator",
        "playbook_name": "feature",
        "current_phase": "executing",
    }
    assert _session_to_row(sess)["phase"] == "executing"

    # Before a flow leaves planning, current_phase is NULL → fall back.
    sess["current_phase"] = None
    assert _session_to_row(sess)["phase"] == "orchestrator"


def test_parse_json_field_passes_through_dict():
    assert _parse_json_field({"a": 1}) == {"a": 1}


def test_parse_json_field_decodes_string():
    assert _parse_json_field('{"a": 1}') == {"a": 1}


def test_parse_json_field_rejects_non_object():
    assert _parse_json_field("[1, 2]") is None
    assert _parse_json_field("not json") is None
    assert _parse_json_field(None) is None
    assert _parse_json_field(42) is None


def test_format_coordination_line_none_when_all_zero():
    telemetry = {
        "signals": {"emitted": {}, "received": 0, "acted_on": 0},
        "files_overlap": {"count": 0, "top": []},
    }
    assert _format_coordination_line(telemetry) is None


def test_format_coordination_line_renders_nonzero_counts():
    telemetry = {
        "signals": {"emitted": {"ScheduleRunSucceeded": 1}, "received": 2, "acted_on": 1},
        "files_overlap": {"count": 3, "top": [{"path": "/a.py", "workers": 2}]},
    }
    line = _format_coordination_line(telemetry)
    assert line == "emitted=1 received=2 acted_on=1 files_overlap=3"


def test_format_coordination_line_none_when_missing_keys():
    assert _format_coordination_line({}) is None


def test_format_coordination_line_malformed_signals_list_does_not_raise():
    """`signals` persisted as a list (not a dict) must be treated as absent
    rather than raising AttributeError on `.get()`."""
    assert _format_coordination_line({"signals": [1]}) is None


def test_format_coordination_line_malformed_emitted_and_overlap_do_not_raise():
    telemetry = {
        "signals": {"emitted": [1, 2], "received": "not-a-number", "acted_on": None},
        "files_overlap": [{"path": "/a.py"}],
    }
    assert _format_coordination_line(telemetry) is None


def test_format_coordination_line_malformed_nested_counts_ignored_alongside_valid_ones():
    """A malformed `files_overlap` shape must not suppress a legitimate
    nonzero signal count elsewhere in the same telemetry dict."""
    telemetry = {
        "signals": {"emitted": {"ScheduleRunSucceeded": 1}, "received": 1, "acted_on": 1},
        "files_overlap": "not-a-dict",
    }
    line = _format_coordination_line(telemetry)
    assert line == "emitted=1 received=1 acted_on=1 files_overlap=0"


def test_invocation_to_row():
    inv = {
        "id": "inv001abc",
        "status": "running",
        "skill": "show",
        "session_count": 3,
        "started_at": time.time() - 300,
    }
    row = _invocation_to_row(inv)
    assert row["type"] == "invocation"
    assert row["agents"] == "3"
    assert row["phase"] == "show"


def test_show_to_row():
    show = {
        "id": "show001abc",
        "status": "active",
        "topic": "my-feature",
        "repo": "octocat/hello",
    }
    row = _show_to_row(show)
    assert row["type"] == "show"
    assert row["project"] == "octocat/hello"
    assert "my-feature" in row["phase"]


def test_play_to_row():
    play = {
        "id": "play001abc",
        "status": "running",
        "name": "backend-impl",
        "started_at": time.time() - 60,
    }
    row = _play_to_row(play)
    assert row["type"] == "play"
    assert row["phase"] == "backend-impl"
    assert row["project"] == "-"


def test_play_to_row_renders_session_project():
    """_query_running_plays attaches the linked session's project as
    session_project; _play_to_row must render it instead of hardcoding '-'."""
    play = {
        "id": "play001abc",
        "status": "running",
        "name": "backend-impl",
        "session_project": "lionagi",
    }
    row = _play_to_row(play)
    assert row["project"] == "lionagi"


def test_play_to_row_orphan_no_session_project_falls_back():
    """A play with no linked session (or a dangling session_id) has no
    session_project — must render '-', not crash."""
    play = {
        "id": "play002abc",
        "status": "running",
        "name": "orphan-play",
        "session_project": None,
    }
    row = _play_to_row(play)
    assert row["project"] == "-"


# ── Integration: DB-backed list_running ───────────────────────────────────────


@pytest.mark.asyncio
async def test_gather_table_rows_empty(temp_db_path: Path) -> None:
    async with StateDB() as db:
        rows = await _gather_table_rows(db, since=None, entity_type=None, project=None)
    assert rows == []


@pytest.mark.asyncio
async def test_gather_table_rows_sessions(temp_db_path: Path) -> None:
    async with StateDB() as db:
        sid1 = await _make_session(db, status="running", project="proj-a")
        sid2 = await _make_session(db, status="completed", project="proj-a")  # should be excluded
        rows = await _gather_table_rows(db, since=None, entity_type=None, project=None)

    session_ids = [r["id"] for r in rows]
    assert sid1[:16] in session_ids
    # completed session must NOT appear
    assert not any(sid2[:16] in rid for rid in session_ids)


@pytest.mark.asyncio
async def test_gather_table_rows_project_filter(temp_db_path: Path) -> None:
    async with StateDB() as db:
        s_a = await _make_session(db, project="proj-a")
        s_b = await _make_session(db, project="proj-b")
        rows_a = await _gather_table_rows(db, since=None, entity_type=None, project="proj-a")
        rows_b = await _gather_table_rows(db, since=None, entity_type=None, project="proj-b")

    ids_a = [r["id"] for r in rows_a]
    ids_b = [r["id"] for r in rows_b]
    assert s_a[:16] in ids_a
    assert s_b[:16] not in ids_a
    assert s_b[:16] in ids_b
    assert s_a[:16] not in ids_b


@pytest.mark.asyncio
async def test_gather_table_rows_project_filter_applies_to_plays(temp_db_path: Path) -> None:
    """--project must scope plays too, not just sessions — the plays table
    has no project column, so a play's project is inherited from its
    linked session."""
    async with StateDB() as db:
        show_id = await _make_show(db)
        sid_a = await _make_session(db, project="proj-a")
        sid_b = await _make_session(db, project="proj-b")
        play_a = await _make_play(db, show_id, name="play-a", session_id=sid_a)
        play_b = await _make_play(db, show_id, name="play-b", session_id=sid_b)

        rows_a = await _gather_table_rows(db, since=None, entity_type="play", project="proj-a")
        rows_b = await _gather_table_rows(db, since=None, entity_type="play", project="proj-b")

    ids_a = [r["id"] for r in rows_a]
    ids_b = [r["id"] for r in rows_b]
    assert play_a[:16] in ids_a
    assert play_b[:16] not in ids_a
    assert play_b[:16] in ids_b
    assert play_a[:16] not in ids_b


@pytest.mark.asyncio
async def test_gather_table_rows_project_filter_applies_to_shows(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--project must scope shows too. `repo` is a filesystem path, not a
    project slug, so the filter must go through detect_project() rather
    than compare `repo` to `--project` as strings — this pins the
    path -> slug translation, not string equality."""
    async with StateDB() as db:
        show_a = await _make_show(db, topic="show-a", repo="/Users/lion/projects/proj-a")
        show_b = await _make_show(db, topic="show-b", repo="/Users/lion/projects/proj-b")

        def fake_detect_project(path: Path) -> tuple[str | None, str | None]:
            name = Path(path).name
            if name in ("proj-a", "proj-b"):
                return (name, "git_remote")
            return (None, None)

        monkeypatch.setattr("lionagi.cli.monitor.detect_project", fake_detect_project)

        rows_a = await _gather_table_rows(db, since=None, entity_type="show", project="proj-a")
        rows_b = await _gather_table_rows(db, since=None, entity_type="show", project="proj-b")

    ids_a = [r["id"] for r in rows_a]
    ids_b = [r["id"] for r in rows_b]
    assert show_a[:16] in ids_a
    assert show_b[:16] not in ids_a
    assert show_b[:16] in ids_b
    assert show_a[:16] not in ids_b


@pytest.mark.asyncio
async def test_gather_table_rows_show_annotated_repo_matches_project(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real _show.md repo lines sometimes carry a trailing remote
    annotation after the path — the matcher must strip it and resolve the
    bare path rather than fail derivation on a nonexistent path."""
    async with StateDB() as db:
        show_id = await _make_show(
            db,
            topic="annotated",
            repo="/Users/lion/projects/proj-a  (org/proj-a, PRIVATE)",
        )

        seen: list[str] = []

        def fake_detect_project(path: Path) -> tuple[str | None, str | None]:
            seen.append(str(path))
            if Path(path).name == "proj-a":
                return ("proj-a", "git_remote")
            return (None, None)

        monkeypatch.setattr("lionagi.cli.monitor.detect_project", fake_detect_project)

        rows = await _gather_table_rows(db, since=None, entity_type="show", project="proj-a")

    assert show_id[:16] in [r["id"] for r in rows]
    assert seen == ["/Users/lion/projects/proj-a"]


def test_show_project_matches_strips_tab_separated_annotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tab (not a literal space) before the annotation must still be
    stripped — the old `.split(" (")` missed this variant."""
    seen: list[str] = []

    def fake_detect_project(path: Path) -> tuple[str | None, str | None]:
        seen.append(str(path))
        return ("proj-a", "git_remote") if Path(path).name == "proj-a" else (None, None)

    monkeypatch.setattr("lionagi.cli.monitor.detect_project", fake_detect_project)

    show = {"repo": "/Users/lion/projects/proj-a\t(org/proj-a, PRIVATE)"}
    assert _show_project_matches(show, "proj-a") is True
    assert seen == ["/Users/lion/projects/proj-a"]


def test_show_project_matches_strips_no_space_annotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No whitespace at all before the annotation must still be stripped —
    the old `.split(" (")` required a leading space to trigger."""
    seen: list[str] = []

    def fake_detect_project(path: Path) -> tuple[str | None, str | None]:
        seen.append(str(path))
        return ("proj-a", "git_remote") if Path(path).name == "proj-a" else (None, None)

    monkeypatch.setattr("lionagi.cli.monitor.detect_project", fake_detect_project)

    show = {"repo": "/Users/lion/projects/proj-a(org/proj-a, PRIVATE)"}
    assert _show_project_matches(show, "proj-a") is True
    assert seen == ["/Users/lion/projects/proj-a"]


def test_show_project_matches_preserves_mid_path_parens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directory name that itself contains " (something)" mid-path (not
    at the end of the string) must NOT be truncated — the strip is anchored
    to a trailing annotation only."""
    seen: list[str] = []

    def fake_detect_project(path: Path) -> tuple[str | None, str | None]:
        seen.append(str(path))
        return ("proj-a", "git_remote") if str(path).endswith("proj-a") else (None, None)

    monkeypatch.setattr("lionagi.cli.monitor.detect_project", fake_detect_project)

    show = {"repo": "/Users/lion/projects/foo (bar)/proj-a"}
    assert _show_project_matches(show, "proj-a") is True
    assert seen == ["/Users/lion/projects/foo (bar)/proj-a"]


def test_show_project_matches_strips_normal_remote_annotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical " (remote, ...)" suffix form still strips correctly
    under the new regex."""
    seen: list[str] = []

    def fake_detect_project(path: Path) -> tuple[str | None, str | None]:
        seen.append(str(path))
        return ("proj-a", "git_remote") if Path(path).name == "proj-a" else (None, None)

    monkeypatch.setattr("lionagi.cli.monitor.detect_project", fake_detect_project)

    show = {"repo": "/Users/lion/projects/proj-a (org/proj-a, PRIVATE)"}
    assert _show_project_matches(show, "proj-a") is True
    assert seen == ["/Users/lion/projects/proj-a"]


@pytest.mark.asyncio
async def test_gather_table_rows_show_missing_repo_excluded_under_project(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A show with no repo path has nothing to derive a project from — it
    must be excluded once --project is applied, but still render in the
    unfiltered view (same orphan semantics as a play with no session)."""
    async with StateDB() as db:
        show_id = await _make_show(db, topic="no-repo", repo=None)

        monkeypatch.setattr("lionagi.cli.monitor.detect_project", lambda path: (None, None))

        rows_filtered = await _gather_table_rows(
            db, since=None, entity_type="show", project="proj-a"
        )
        rows_unfiltered = await _gather_table_rows(db, since=None, entity_type="show", project=None)

    assert show_id[:16] not in [r["id"] for r in rows_filtered]
    assert show_id[:16] in [r["id"] for r in rows_unfiltered]


@pytest.mark.asyncio
async def test_gather_table_rows_show_nonexistent_repo_path_no_crash(
    temp_db_path: Path,
) -> None:
    """A repo path that doesn't exist on disk (dangling worktree, or the
    literal path+annotation string import_shows sometimes stores) must not
    crash detect_project's underlying git subprocess call — it just fails to
    resolve and the show is excluded under --project. Exercises the real
    detect_project(), not a monkeypatched stand-in."""
    async with StateDB() as db:
        show_id = await _make_show(
            db, topic="bogus", repo="/nonexistent/path/does-not-exist-xyz-1642"
        )

        rows = await _gather_table_rows(db, since=None, entity_type="show", project="proj-a")

    assert show_id[:16] not in [r["id"] for r in rows]


@pytest.mark.asyncio
async def test_gather_table_rows_play_row_renders_session_project(
    temp_db_path: Path,
) -> None:
    """A play row's PROJECT cell must reflect its linked session's project,
    not the hardcoded '-' placeholder."""
    async with StateDB() as db:
        show_id = await _make_show(db)
        sid = await _make_session(db, project="proj-a")
        play_id = await _make_play(db, show_id, name="play-a", session_id=sid)

        rows = await _gather_table_rows(db, since=None, entity_type="play", project=None)

    play_rows = [r for r in rows if r["id"] == play_id[:16]]
    assert len(play_rows) == 1
    assert play_rows[0]["project"] == "proj-a"


@pytest.mark.asyncio
async def test_gather_table_rows_orphan_play_renders_dash(temp_db_path: Path) -> None:
    """A play with no linked session still renders without crashing, with
    PROJECT falling back to '-' rather than raising on the missing subselect
    match."""
    async with StateDB() as db:
        show_id = await _make_show(db)
        play_no_session = await _make_play(db, show_id, name="no-session", session_id=None)

        rows = await _gather_table_rows(db, since=None, entity_type="play", project=None)

    by_id = {r["id"]: r for r in rows}
    assert by_id[play_no_session[:16]]["project"] == "-"


@pytest.mark.asyncio
async def test_gather_table_rows_type_filter_session(temp_db_path: Path) -> None:
    async with StateDB() as db:
        sid = await _make_session(db)
        inv_id = await _make_invocation(db)
        # Only sessions
        rows = await _gather_table_rows(db, since=None, entity_type="session", project=None)

    assert any(sid[:16] in r["id"] for r in rows)
    assert not any(r["type"] == "invocation" for r in rows)


@pytest.mark.asyncio
async def test_gather_table_rows_invocations(temp_db_path: Path) -> None:
    async with StateDB() as db:
        inv_id = await _make_invocation(db, skill="show")
        rows = await _gather_table_rows(db, since=None, entity_type="invocation", project=None)

    assert any(inv_id[:16] in r["id"] for r in rows)
    assert all(r["type"] == "invocation" for r in rows)


@pytest.mark.asyncio
async def test_gather_table_rows_shows(temp_db_path: Path) -> None:
    async with StateDB() as db:
        show_id = await _make_show(db, topic="my-topic")
        rows = await _gather_table_rows(db, since=None, entity_type="show", project=None)

    assert any(show_id[:16] in r["id"] for r in rows)
    assert all(r["type"] == "show" for r in rows)


@pytest.mark.asyncio
async def test_gather_table_rows_plays(temp_db_path: Path) -> None:
    async with StateDB() as db:
        show_id = await _make_show(db)
        play_id = await _make_play(db, show_id, status="running")
        rows = await _gather_table_rows(db, since=None, entity_type="play", project=None)

    assert any(play_id[:16] in r["id"] for r in rows)
    assert all(r["type"] == "play" for r in rows)


@pytest.mark.asyncio
async def test_gather_table_rows_since_filter(temp_db_path: Path) -> None:
    """Sessions with updated_at before the cutoff should be excluded."""
    async with StateDB() as db:
        sid_old = await _make_session(db)
        # Force updated_at to be in the past
        cutoff_past = time.time() - 3600
        await db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (cutoff_past - 10, sid_old),
        )

        sid_new = await _make_session(db)
        since = time.time() - 60  # last minute only
        rows = await _gather_table_rows(db, since=since, entity_type="session", project=None)

    ids = [r["id"] for r in rows]
    assert sid_new[:16] in ids
    assert not any(sid_old[:16] in i for i in ids)


# ── Regression: --since widens the status filter to terminal states ───────────
#
# `--since` used to only ever AND a time bound on top of a running-only
# filter, so a session that finished (however recently) could never appear
# no matter how narrow the --since window was. These lock in the fix: with
# --since given, terminal statuses widen into view within the window; with
# no --since, the default view stays running-only, unchanged.


@pytest.mark.asyncio
async def test_since_shows_terminal_session_default_hides_it(temp_db_path: Path) -> None:
    """A completed session is visible with --since but hidden in the default
    (no-flag) running-only view."""
    async with StateDB() as db:
        sid = await _make_session(db, status="completed")
        since = time.time() - 3600

        rows_since = await _gather_table_rows(db, since=since, entity_type="session", project=None)
        rows_default = await _gather_table_rows(db, since=None, entity_type="session", project=None)

    assert sid[:16] in [r["id"] for r in rows_since], "completed session must show with --since"
    assert sid[:16] not in [r["id"] for r in rows_default], "default view must stay running-only"


@pytest.mark.asyncio
async def test_since_default_view_running_only_unchanged(temp_db_path: Path) -> None:
    """Without --since, a mix of terminal-status sessions must never appear —
    the no-flag view's running-only behavior is unchanged by the fix."""
    async with StateDB() as db:
        running_id = await _make_session(db, status="running")
        for status in ("completed", "failed", "timed_out", "aborted", "cancelled"):
            await _make_session(db, status=status)

        rows = await _gather_table_rows(db, since=None, entity_type="session", project=None)

    ids = [r["id"] for r in rows]
    assert ids == [running_id[:16]]


@pytest.mark.asyncio
async def test_since_shows_terminal_invocation(temp_db_path: Path) -> None:
    """--since widens the invocations query to terminal statuses too."""
    async with StateDB() as db:
        inv_id = await _make_invocation(db, status="completed")
        since = time.time() - 3600

        rows_since = await _gather_table_rows(
            db, since=since, entity_type="invocation", project=None
        )
        rows_default = await _gather_table_rows(
            db, since=None, entity_type="invocation", project=None
        )

    assert inv_id[:16] in [r["id"] for r in rows_since]
    assert inv_id[:16] not in [r["id"] for r in rows_default]


@pytest.mark.asyncio
async def test_since_shows_terminal_play(temp_db_path: Path) -> None:
    """--since widens the plays query past the running-ish status tuple to
    every play status (e.g. 'merged')."""
    async with StateDB() as db:
        show_id = await _make_show(db)
        play_id = await _make_play(db, show_id, status="merged")
        since = time.time() - 3600

        rows_since = await _gather_table_rows(db, since=since, entity_type="play", project=None)
        rows_default = await _gather_table_rows(db, since=None, entity_type="play", project=None)

    assert play_id[:16] in [r["id"] for r in rows_since]
    assert play_id[:16] not in [r["id"] for r in rows_default]


@pytest.mark.asyncio
async def test_since_shows_terminal_show(temp_db_path: Path) -> None:
    """--since widens the shows query past the active-only filter to every
    show status (e.g. 'completed')."""
    async with StateDB() as db:
        show_id = await _make_show(db, status="completed")
        since = time.time() - 3600

        rows_since = await _gather_table_rows(db, since=since, entity_type="show", project=None)
        rows_default = await _gather_table_rows(db, since=None, entity_type="show", project=None)

    assert show_id[:16] in [r["id"] for r in rows_since]
    assert show_id[:16] not in [r["id"] for r in rows_default]


# ── Integration: _find_entity ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_entity_session(temp_db_path: Path) -> None:
    async with StateDB() as db:
        sid = await _make_session(db)
        result = await _find_entity(db, sid)

    assert result is not None
    entity_type, row = result
    assert entity_type == "session"
    assert row["id"] == sid


@pytest.mark.asyncio
async def test_find_entity_invocation(temp_db_path: Path) -> None:
    async with StateDB() as db:
        inv_id = await _make_invocation(db)
        result = await _find_entity(db, inv_id)

    assert result is not None
    entity_type, row = result
    assert entity_type == "invocation"


@pytest.mark.asyncio
async def test_find_entity_show(temp_db_path: Path) -> None:
    async with StateDB() as db:
        show_id = await _make_show(db)
        result = await _find_entity(db, show_id)

    assert result is not None
    entity_type, row = result
    assert entity_type == "show"


@pytest.mark.asyncio
async def test_find_entity_play(temp_db_path: Path) -> None:
    async with StateDB() as db:
        show_id = await _make_show(db)
        play_id = await _make_play(db, show_id)
        result = await _find_entity(db, play_id)

    assert result is not None
    entity_type, row = result
    assert entity_type == "play"


@pytest.mark.asyncio
async def test_find_entity_prefix_match(temp_db_path: Path) -> None:
    async with StateDB() as db:
        inv_id = await _make_invocation(db)
        # Search with first 4 chars
        result = await _find_entity(db, inv_id[:4])

    assert result is not None
    assert result[0] == "invocation"


@pytest.mark.asyncio
async def test_find_entity_not_found(temp_db_path: Path) -> None:
    async with StateDB() as db:
        result = await _find_entity(db, "nonexistentid999")
    assert result is None


# ── Integration: _run_table and _run_detail ───────────────────────────────────


@pytest.mark.asyncio
async def test_run_table_no_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "nonexistent.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", missing)
    output = await _run_table(since=None, entity_type=None, project=None)
    # Should return a graceful "no state.db" message
    assert "state.db" in output or "no" in output.lower()


@pytest.mark.asyncio
async def test_run_table_with_running_session(temp_db_path: Path) -> None:
    async with StateDB() as db:
        sid = await _make_session(db, project="test-project")
    output = await _run_table(since=None, entity_type=None, project=None)
    assert sid[:12] in output or "test-project" in output or "running" in output


@pytest.mark.asyncio
async def test_run_detail_session(temp_db_path: Path) -> None:
    async with StateDB() as db:
        sid = await _make_session(db, model="claude-opus-4", project="demo")
    output = await _run_detail(sid)
    assert "SESSION" in output
    assert "running" in output.lower()


@pytest.mark.asyncio
async def test_run_detail_invocation(temp_db_path: Path) -> None:
    async with StateDB() as db:
        inv_id = await _make_invocation(db, skill="codex-review")
    output = await _run_detail(inv_id)
    assert "INVOCATION" in output
    assert "codex-review" in output


@pytest.mark.asyncio
async def test_run_detail_invocation_shows_coordination_block(temp_db_path: Path) -> None:
    coordination = {
        "signals": {"emitted": {"ScheduleRunSucceeded": 1}, "received": 1, "acted_on": 1},
        "files_overlap": {"count": 1, "top": [{"path": "/repo/shared.py", "workers": 2}]},
    }
    async with StateDB() as db:
        inv_id = await _make_invocation(
            db, skill="scheduled:flow", node_metadata={"coordination": coordination}
        )
    output = await _run_detail(inv_id)
    assert "coordination" in output
    assert "emitted=1 received=1 acted_on=1 files_overlap=1" in output
    assert "/repo/shared.py" in output
    assert "workers=2" in output


@pytest.mark.asyncio
async def test_run_detail_invocation_omits_coordination_block_when_all_zero(
    temp_db_path: Path,
) -> None:
    coordination = {
        "signals": {"emitted": {}, "received": 0, "acted_on": 0},
        "files_overlap": {"count": 0, "top": []},
    }
    async with StateDB() as db:
        inv_id = await _make_invocation(
            db, skill="scheduled:agent", node_metadata={"coordination": coordination}
        )
    output = await _run_detail(inv_id)
    assert "coordination" not in output


@pytest.mark.asyncio
async def test_run_detail_invocation_no_node_metadata_omits_coordination(
    temp_db_path: Path,
) -> None:
    async with StateDB() as db:
        inv_id = await _make_invocation(db, skill="show")
    output = await _run_detail(inv_id)
    assert "coordination" not in output


@pytest.mark.asyncio
async def test_run_detail_invocation_malformed_nested_telemetry_does_not_raise(
    temp_db_path: Path,
) -> None:
    """A `coordination` dict whose nested `signals`/`files_overlap` values
    do not match the shape this module writes (e.g. hand-edited state.db,
    or a future writer bug) must render without raising."""
    coordination = {
        "signals": [1],
        "files_overlap": {"count": 1, "top": "not-a-list"},
    }
    async with StateDB() as db:
        inv_id = await _make_invocation(
            db, skill="scheduled:flow", node_metadata={"coordination": coordination}
        )
    output = await _run_detail(inv_id)
    assert "INVOCATION" in output
    assert "files_overlap=1" in output


@pytest.mark.asyncio
async def test_run_detail_show(temp_db_path: Path) -> None:
    async with StateDB() as db:
        show_id = await _make_show(db, topic="implement-auth")
    output = await _run_detail(show_id)
    assert "SHOW" in output
    assert "implement-auth" in output


@pytest.mark.asyncio
async def test_run_detail_play(temp_db_path: Path) -> None:
    async with StateDB() as db:
        show_id = await _make_show(db)
        play_id = await _make_play(db, show_id, name="backend-work")
    output = await _run_detail(play_id)
    assert "PLAY" in output
    assert "backend-work" in output


@pytest.mark.asyncio
async def test_run_detail_not_found(temp_db_path: Path) -> None:
    output = await _run_detail("no-such-id-xyz-999")
    assert "not found" in output.lower() or "error" in output.lower()


# ── Regression: detail view shows playbook name ───────────────────────────────


@pytest.mark.asyncio
async def test_run_detail_session_shows_playbook_name(temp_db_path: Path) -> None:
    """`li monitor <id>` for a play/flow session must surface the active
    playbook — a session row already carries playbook_name, but the detail
    view used to omit it entirely."""
    async with StateDB() as db:
        sid = await _make_session(db, invocation_kind="play")
        await db.execute(
            "UPDATE sessions SET playbook_name = ? WHERE id = ?", ("feature-impl", sid)
        )
    output = await _run_detail(sid)
    assert "feature-impl" in output


@pytest.mark.asyncio
async def test_run_detail_session_no_playbook_name_omits_line(temp_db_path: Path) -> None:
    """A plain `li agent` session has no playbook — no dangling 'playbook: -' line."""
    async with StateDB() as db:
        sid = await _make_session(db, invocation_kind="agent")
    output = await _run_detail(sid)
    assert "playbook:" not in output


# ── Regression: branch (agent leg) sub-step visibility ────────────────────────


async def _add_branch(db: StateDB, session_id: str, *, name: str, status: str = "running") -> str:
    pid = uuid.uuid4().hex
    await db.execute("INSERT INTO progressions(id, created_at) VALUES (?, ?)", (pid, time.time()))
    bid = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO branches(id, created_at, session_id, progression_id, name, status, started_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (bid, time.time(), session_id, pid, name, status, time.time()),
    )
    return bid


def test_render_branch_lines_empty() -> None:
    assert _render_branch_lines([]) == []


def test_render_branch_lines_shows_name_and_status() -> None:
    rows = [{"name": "reviewer", "status": "running", "started_at": time.time(), "ended_at": None}]
    lines = _render_branch_lines(rows)
    joined = "\n".join(lines)
    assert "reviewer" in joined
    assert "running" in joined


@pytest.mark.asyncio
async def test_run_detail_session_surfaces_branch_legs(temp_db_path: Path) -> None:
    """A play/flow's internal sub-steps (e.g. a "reviewer" leg, a
    "claude-code" leg) are recorded as branches with their own name/status —
    `li monitor <session_id>` must list them so a caller can see a leg
    transition without hand-polling sqlite."""
    async with StateDB() as db:
        sid = await _make_session(db, invocation_kind="play")
        await _add_branch(db, sid, name="reviewer", status="completed")
        await _add_branch(db, sid, name="claude-code", status="running")
    output = await _run_detail(sid)
    assert "reviewer" in output
    assert "claude-code" in output


@pytest.mark.asyncio
async def test_run_detail_play_surfaces_branch_legs(temp_db_path: Path) -> None:
    """Same sub-step visibility via the play id (not just the raw session id) —
    `li monitor <play_id>` drills through the linked session to its branches."""
    async with StateDB() as db:
        show_id = await _make_show(db)
        sid = await _make_session(db, invocation_kind="play")
        play_id = await _make_play(db, show_id, name="backend-impl", session_id=sid)
        await _add_branch(db, sid, name="reviewer", status="running")
    output = await _run_detail(play_id)
    assert "reviewer" in output


@pytest.mark.asyncio
async def test_run_detail_no_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "gone.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", missing)
    output = await _run_detail("some-id")
    assert "state.db" in output or "not found" in output.lower() or "error" in output.lower()


# ── Integration: argparse wiring ─────────────────────────────────────────────


def test_add_monitor_subparser():
    """Verify that `li monitor` is registered and accepts expected arguments."""
    import argparse

    from lionagi.cli.monitor import add_monitor_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_monitor_subparser(sub)

    # Table view
    args = parser.parse_args(["monitor"])
    assert args.id is None
    assert not args.watch

    # Detail view
    args = parser.parse_args(["monitor", "abc123"])
    assert args.id == "abc123"

    # Watch mode
    args = parser.parse_args(["monitor", "--watch"])
    assert args.watch

    # --since
    args = parser.parse_args(["monitor", "--since", "1h"])
    assert args.since == "1h"

    # --type
    args = parser.parse_args(["monitor", "--type", "session"])
    assert args.entity_type == "session"

    # --project
    args = parser.parse_args(["monitor", "--project", "myproject"])
    assert args.project == "myproject"

    # mon alias
    args = parser.parse_args(["mon", "--watch", "eid"])
    assert args.id == "eid"
    assert args.watch


def test_main_registers_monitor():
    """End-to-end: `li monitor --help` exits 0."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "lionagi.cli", "monitor", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "monitor" in result.stdout.lower() or "observe" in result.stdout.lower()


# ── Watch mode: SIGINT terminates cleanly ─────────────────────────────────────


# ── Regression: --type play filter ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_type_play_filter_includes_play_sessions(temp_db_path: Path) -> None:
    """Sessions with invocation_kind='play' must appear under --type play."""
    async with StateDB() as db:
        # Session that shows as TYPE=play in the all-rows view
        play_sess_id = await _make_session(db, invocation_kind="play", project="myproject")
        # Unrelated agent session must NOT appear when filtering by "play"
        agent_sess_id = await _make_session(db, invocation_kind="agent")

        rows = await _gather_table_rows(db, since=None, entity_type="play", project=None)

    ids = [r["id"] for r in rows]
    types = [r["type"] for r in rows]
    assert play_sess_id[:16] in ids, "play-kind session must be returned for --type play"
    assert agent_sess_id[:16] not in ids, "agent-kind session must not appear for --type play"
    assert all(t == "play" for t in types), "every returned row must have type='play'"


@pytest.mark.asyncio
async def test_type_play_filter_includes_both_sessions_and_plays(temp_db_path: Path) -> None:
    """--type play must return both play-kind sessions and play table rows."""
    async with StateDB() as db:
        # Session with invocation_kind="play" (from `li play NAME`)
        play_sess_id = await _make_session(db, invocation_kind="play")
        # Actual play row from shows/plays tables (from `li o show`)
        show_id = await _make_show(db)
        play_row_id = await _make_play(db, show_id, status="running")

        rows = await _gather_table_rows(db, since=None, entity_type="play", project=None)

    ids = [r["id"] for r in rows]
    assert play_sess_id[:16] in ids, "play-kind session not in --type play results"
    assert play_row_id[:16] in ids, "play table row not in --type play results"


# ── Regression: direct `li play` runs (no plays-table row) via --since ────────
#
# `create_play` has no production caller on the direct `li play NAME` path —
# a completed direct play exists only as a session row with
# invocation_kind='play'. The play view must surface that session (once
# --since widens past running-only) and must never double-list it against a
# plays-table row when one also exists for the same underlying session.


@pytest.mark.asyncio
async def test_since_shows_terminal_direct_play_session(temp_db_path: Path) -> None:
    """A completed direct-play session (invocation_kind=play, no plays-table
    row) appears in the play view with --since, and stays hidden by default."""
    async with StateDB() as db:
        play_sess_id = await _make_session(db, invocation_kind="play", status="completed")
        since = time.time() - 3600

        rows_since = await _gather_table_rows(db, since=since, entity_type="play", project=None)
        rows_default = await _gather_table_rows(db, since=None, entity_type="play", project=None)

    assert play_sess_id[:16] in [r["id"] for r in rows_since]
    assert play_sess_id[:16] not in [r["id"] for r in rows_default]


@pytest.mark.asyncio
async def test_since_no_double_listing_when_play_row_and_session_both_exist(
    temp_db_path: Path,
) -> None:
    """When a plays-table row's session_id points at a session that itself
    carries invocation_kind='play', the play must render exactly once (from
    the plays-table row), not a second time from the sessions branch."""
    async with StateDB() as db:
        show_id = await _make_show(db)
        play_sess_id = await _make_session(db, invocation_kind="play", status="completed")
        play_row_id = await _make_play(db, show_id, status="merged")
        await db.execute(
            "UPDATE plays SET session_id = ? WHERE id = ?", (play_sess_id, play_row_id)
        )
        since = time.time() - 3600

        rows = await _gather_table_rows(db, since=since, entity_type="play", project=None)

    ids = [r["id"] for r in rows]
    assert play_row_id[:16] in ids, "the plays-table row must still appear"
    assert play_sess_id[:16] not in ids, "the underlying session must not also appear"
    assert ids.count(play_row_id[:16]) == 1


@pytest.mark.asyncio
async def test_since_no_double_listing_in_all_view(temp_db_path: Path) -> None:
    """The same dedup must hold in the "all" (entity_type=None) view, since
    its play section is the same _query_running_plays call."""
    async with StateDB() as db:
        show_id = await _make_show(db)
        play_sess_id = await _make_session(db, invocation_kind="play", status="completed")
        play_row_id = await _make_play(db, show_id, status="merged")
        await db.execute(
            "UPDATE plays SET session_id = ? WHERE id = ?", (play_sess_id, play_row_id)
        )
        since = time.time() - 3600

        rows = await _gather_table_rows(db, since=since, entity_type=None, project=None)

    ids = [r["id"] for r in rows]
    assert play_row_id[:16] in ids
    assert play_sess_id[:16] not in ids


# ── Regression: dedup must not hide sessions from views without a play section ─
#
# The play-vs-session dedup lives in _gather_table_rows and applies only when
# the plays section is actually being rendered. A SQL-level exclusion would
# hide the backing session from the sessions-only view entirely, and would
# zero-render the play when the plays row's updated_at falls outside the
# --since window while the session's is inside it.


@pytest.mark.asyncio
async def test_type_session_view_still_shows_play_backed_session(temp_db_path: Path) -> None:
    """--type session renders every session, including one a plays-table row
    references — the play dedup only applies where plays are also shown."""
    async with StateDB() as db:
        show_id = await _make_show(db)
        play_sess_id = await _make_session(db, invocation_kind="play", status="completed")
        play_row_id = await _make_play(db, show_id, status="merged")
        await db.execute(
            "UPDATE plays SET session_id = ? WHERE id = ?", (play_sess_id, play_row_id)
        )
        since = time.time() - 3600

        rows = await _gather_table_rows(db, since=since, entity_type="session", project=None)

    assert play_sess_id[:16] in [r["id"] for r in rows]


@pytest.mark.asyncio
async def test_desynced_play_row_outside_window_session_still_renders_once(
    temp_db_path: Path,
) -> None:
    """plays.updated_at outside the --since window but sessions.updated_at
    inside: the run renders exactly once (via the session), not zero times."""
    async with StateDB() as db:
        show_id = await _make_show(db)
        play_sess_id = await _make_session(db, invocation_kind="play", status="completed")
        play_row_id = await _make_play(db, show_id, status="merged")
        old = time.time() - 7200
        await db.execute(
            "UPDATE plays SET session_id = ?, updated_at = ? WHERE id = ?",
            (play_sess_id, old, play_row_id),
        )
        since = time.time() - 3600

        rows = await _gather_table_rows(db, since=since, entity_type="play", project=None)

    ids = [r["id"] for r in rows]
    assert play_row_id[:16] not in ids, "stale plays row is outside the window"
    assert ids.count(play_sess_id[:16]) == 1, "the session must render exactly once"


@pytest.mark.asyncio
async def test_null_invocation_kind_session_not_hidden(temp_db_path: Path) -> None:
    """A session with no invocation_kind must never be swallowed by the play
    dedup in the sessions-only view, even when a plays row references it."""
    async with StateDB() as db:
        show_id = await _make_show(db)
        sid = await _make_session(db, invocation_kind=None, status="completed")
        play_row_id = await _make_play(db, show_id, status="merged")
        await db.execute("UPDATE plays SET session_id = ? WHERE id = ?", (sid, play_row_id))
        since = time.time() - 3600

        rows = await _gather_table_rows(db, since=since, entity_type="session", project=None)

    assert sid[:16] in [r["id"] for r in rows]


@pytest.mark.asyncio
async def test_null_invocation_kind_unbacked_session_renders_in_all_view(
    temp_db_path: Path,
) -> None:
    """A session with no invocation_kind and no plays row renders in the
    all-entities view — dedup only drops sessions whose play row is shown."""
    async with StateDB() as db:
        sid = await _make_session(db, invocation_kind=None, status="completed")
        since = time.time() - 3600

        rows = await _gather_table_rows(db, since=since, entity_type=None, project=None)

    assert [r["id"] for r in rows].count(sid[:16]) == 1


def test_watch_loop_recomputes_since_every_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """The watch loop re-derives the cutoff from the window string each tick
    (a sliding window), so terminal rows age out instead of accumulating."""
    import lionagi.cli.monitor as monitor_mod

    handlers: dict[int, Any] = {}
    monkeypatch.setattr(
        monitor_mod.signal, "signal", lambda signum, handler: handlers.setdefault(signum, handler)
    )

    ticks = iter([100.0, 200.0])
    monkeypatch.setattr(monitor_mod, "_since_timestamp", lambda window: next(ticks))
    monkeypatch.setattr(monitor_mod, "_clear_screen", lambda: None)

    captured: list[float | None] = []

    async def fake_run_table(*, since: float | None, entity_type: Any, project: Any) -> str:
        captured.append(since)
        if len(captured) >= 2:
            handlers[signal.SIGINT](signal.SIGINT, None)
        return ""

    monkeypatch.setattr(monitor_mod, "_run_table", fake_run_table)

    rc = monitor_mod._watch_loop(0, None, since_window="1h", entity_type=None, project=None)
    assert rc == 0
    assert captured == [100.0, 200.0], "each tick must use a freshly derived cutoff"


# ── Regression: AGENTS column ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_agents_column_reflects_branch_count(temp_db_path: Path) -> None:
    """AGENTS column shows branch count, not '-', for sessions."""
    async with StateDB() as db:
        sid = await _make_session(db)
        # Insert two branches for this session
        pid1, pid2 = uuid.uuid4().hex, uuid.uuid4().hex
        await db.execute(
            "INSERT INTO progressions(id, created_at) VALUES (?, ?)", (pid1, time.time())
        )
        await db.execute(
            "INSERT INTO progressions(id, created_at) VALUES (?, ?)", (pid2, time.time())
        )
        b1_id, b2_id = uuid.uuid4().hex, uuid.uuid4().hex
        for bid, pid in ((b1_id, pid1), (b2_id, pid2)):
            await db.execute(
                "INSERT INTO branches(id, created_at, session_id, progression_id) VALUES (?,?,?,?)",
                (bid, time.time(), sid, pid),
            )

        rows = await _gather_table_rows(db, since=None, entity_type=None, project=None)

    sess_rows = [r for r in rows if r["id"] == sid[:16]]
    assert sess_rows, "session must appear in table"
    assert sess_rows[0]["agents"] == "2", f"expected agents='2', got {sess_rows[0]['agents']!r}"


@pytest.mark.asyncio
async def test_session_agents_column_zero_when_no_branches(temp_db_path: Path) -> None:
    """AGENTS column is '0' (not '-') for a session with no branches."""
    async with StateDB() as db:
        sid = await _make_session(db)
        rows = await _gather_table_rows(db, since=None, entity_type=None, project=None)

    sess_rows = [r for r in rows if r["id"] == sid[:16]]
    assert sess_rows, "session must appear in table"
    assert sess_rows[0]["agents"] == "0", f"expected agents='0', got {sess_rows[0]['agents']!r}"


@pytest.mark.asyncio
async def test_play_agents_column_reflects_branch_count(temp_db_path: Path) -> None:
    """AGENTS column shows branch count for plays that have a linked session."""
    async with StateDB() as db:
        show_id = await _make_show(db)
        play_session_id = await _make_session(db, status="running")
        play_id = await _make_play(db, show_id, status="running")
        # Link the play to the session
        await db.execute("UPDATE plays SET session_id = ? WHERE id = ?", (play_session_id, play_id))
        # Add a branch on the play's session
        pid = uuid.uuid4().hex
        await db.execute(
            "INSERT INTO progressions(id, created_at) VALUES (?, ?)", (pid, time.time())
        )
        bid = uuid.uuid4().hex
        await db.execute(
            "INSERT INTO branches(id, created_at, session_id, progression_id) VALUES (?,?,?,?)",
            (bid, time.time(), play_session_id, pid),
        )

        rows = await _gather_table_rows(db, since=None, entity_type="play", project=None)

    play_rows = [r for r in rows if r["id"] == play_id[:16]]
    assert play_rows, "play must appear in table"
    assert play_rows[0]["agents"] == "1", f"expected agents='1', got {play_rows[0]['agents']!r}"


# ── Regression: background correlation handle ─────────────────────────────────


def test_session_id_env_var_used_as_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """LIONAGI_SESSION_ID env var is used as the orchestration session id."""
    import uuid

    from lionagi import Branch, Session

    bg_session_id = str(uuid.uuid4())
    monkeypatch.setenv("LIONAGI_SESSION_ID", bg_session_id)

    import os

    _session_id_env = os.environ.get("LIONAGI_SESSION_ID")
    b = Branch()
    s = (
        Session(id=_session_id_env, default_branch=b)
        if _session_id_env
        else Session(default_branch=b)
    )
    assert str(s.id) == bg_session_id, f"Session id {s.id!r} != pre-generated id {bg_session_id!r}"


def test_background_hint_includes_session_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """li o flow --background prints a 'li monitor <id>' hint."""
    import subprocess
    import sys

    save_dir = tmp_path / "bg_out"
    save_dir.mkdir()
    # Change cwd to tmp_path so the save path passes the allowed-roots check.
    monkeypatch.chdir(tmp_path)
    # Use --agent with a dummy name.  The agent profile won't exist so the
    # background subprocess will fail, but the *parent* prints the session hint
    # before Popen and returns 0 — that output is what we check.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "lionagi.cli",
            "o",
            "flow",
            "--background",
            "--save",
            str(save_dir),
            "--agent",
            "_no_such_agent_",
            "myprompt",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(tmp_path),
    )
    # The hint is printed before the subprocess is waited on, so it should
    # appear in stdout/stderr regardless of whether the subprocess succeeds.
    combined = result.stdout + result.stderr
    assert "li monitor" in combined, f"Expected 'li monitor <id>' hint in output, got:\n{combined}"


# ── Watch mode: SIGINT terminates cleanly ─────────────────────────────────────


def test_watch_mode_sigint_clean(temp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Watch loop exits cleanly when SIGINT is received."""
    import lionagi.cli.monitor as monitor_mod
    import lionagi.ln.concurrency as concurrency_mod

    handlers: dict[int, Any] = {}
    monkeypatch.setattr(
        monitor_mod.signal,
        "signal",
        lambda signum, handler: handlers.__setitem__(signum, handler),
    )
    monkeypatch.setattr(monitor_mod, "_clear_screen", lambda: None)

    def interrupting_run_async(coro: Any) -> str:
        coro.close()
        handlers[signal.SIGINT](signal.SIGINT, None)
        return ""

    monkeypatch.setattr(concurrency_mod, "run_async", interrupting_run_async)

    exit_code = monitor_mod._watch_loop(
        1,
        None,
        since_window=None,
        entity_type=None,
        project=None,
    )

    assert signal.SIGINT in handlers
    assert exit_code == 0


# ── Ambiguous short-id prefixes ───────────────────────────────────────────────


async def _make_session_with_id(db: StateDB, sid: str) -> str:
    prog_id = uuid.uuid4().hex
    await db.create_progression(prog_id)
    await db.create_session(
        {
            "id": sid,
            "progression_id": prog_id,
            "status": "running",
            "invocation_kind": "agent",
            "started_at": time.time(),
        }
    )
    return sid


@pytest.mark.asyncio
async def test_find_entity_rejects_ambiguous_prefix(temp_db_path: Path) -> None:
    from lionagi.cli._util import AmbiguousIdError

    async with StateDB() as db:
        first = await _make_session_with_id(db, "abcde01")
        second = await _make_session_with_id(db, "abcde02")

        with pytest.raises(AmbiguousIdError) as excinfo:
            await _find_entity(db, "abcde")

    assert set(excinfo.value.candidates) == {first, second}


@pytest.mark.asyncio
async def test_resolve_session_run_rejects_ambiguous_prefix(temp_db_path: Path) -> None:
    from lionagi.cli._util import AmbiguousIdError
    from lionagi.cli.monitor import _resolve_session_run

    async with StateDB() as db:
        await _make_session_with_id(db, "abcde01")
        await _make_session_with_id(db, "abcde02")

        with pytest.raises(AmbiguousIdError):
            await _resolve_session_run(db, "abcde")


@pytest.mark.asyncio
async def test_run_detail_propagates_ambiguous_prefix(temp_db_path: Path) -> None:
    """_run_detail's broad `except Exception` must not swallow the ambiguity
    into a rendered detail body — the caller has to set the exit code."""
    from lionagi.cli._util import AmbiguousIdError

    async with StateDB() as db:
        await _make_session_with_id(db, "abcde01")
        await _make_session_with_id(db, "abcde02")

    with pytest.raises(AmbiguousIdError):
        await _run_detail("abcde")


@pytest.mark.asyncio
async def test_monitor_detail_ambiguous_prefix_exits_unknown(temp_db_path: Path) -> None:
    import argparse

    import lionagi.cli.monitor as monitor_mod
    from lionagi.cli.status import EXIT_UNKNOWN

    async with StateDB() as db:
        await _make_session_with_id(db, "abcde01")
        await _make_session_with_id(db, "abcde02")

    args = argparse.Namespace(
        run_ids=None,
        since=None,
        id="abcde",
        entity_type=None,
        project=None,
        watch=False,
        refresh=2,
        interval=3.0,
        follow=False,
        chain=True,
        max_wait=None,
    )

    assert monitor_mod.run_monitor(args) == EXIT_UNKNOWN


def test_watch_loop_ambiguous_prefix_exits_unknown(
    temp_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Watch mode can't refresh its way out of an ambiguous id — it stops."""
    import lionagi.cli.monitor as monitor_mod
    from lionagi.cli._util import AmbiguousIdError
    from lionagi.cli.status import EXIT_UNKNOWN

    monkeypatch.setattr(monitor_mod, "_clear_screen", lambda: None)

    def raising_run_async(coro: Any) -> str:
        coro.close()
        raise AmbiguousIdError("abcde", "sessions", ["abcde01", "abcde02"])

    import lionagi.ln.concurrency as concurrency_mod

    monkeypatch.setattr(concurrency_mod, "run_async", raising_run_async)

    saved = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))
    try:
        exit_code = monitor_mod._watch_loop(
            1, "abcde", since_window=None, entity_type=None, project=None
        )
    finally:
        signal.signal(signal.SIGINT, saved[0])
        signal.signal(signal.SIGTERM, saved[1])

    assert exit_code == EXIT_UNKNOWN


# ── Watch mode: SIGTERM during a refresh ──────────────────────────────────────
#
# run_async installs its own signal handlers for the duration of the call, so a
# signal delivered inside a refresh surfaces as SigtermInterrupt/KeyboardInterrupt
# out of run_async rather than setting the loop's own flag. Both derive from
# BaseException, so the loop has to name them.


def test_watch_loop_exits_cleanly_on_sigterm_during_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lionagi.cli.monitor as monitor_mod
    import lionagi.ln.concurrency as concurrency_mod
    from lionagi.ln.concurrency.utils import SigtermInterrupt

    monkeypatch.setattr(monitor_mod, "_clear_screen", lambda: None)

    def sigterm_run_async(coro: Any) -> str:
        coro.close()
        raise SigtermInterrupt("SIGTERM during refresh")

    monkeypatch.setattr(concurrency_mod, "run_async", sigterm_run_async)

    saved = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))
    try:
        exit_code = monitor_mod._watch_loop(
            1, None, since_window=None, entity_type=None, project=None
        )
    finally:
        signal.signal(signal.SIGINT, saved[0])
        signal.signal(signal.SIGTERM, saved[1])

    assert exit_code == 0


def test_watch_loop_restores_prior_signal_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The loop's handlers must not outlive the loop, on any exit path."""
    import lionagi.cli.monitor as monitor_mod
    import lionagi.ln.concurrency as concurrency_mod
    from lionagi.ln.concurrency.utils import SigtermInterrupt

    monkeypatch.setattr(monitor_mod, "_clear_screen", lambda: None)

    def sigterm_run_async(coro: Any) -> str:
        coro.close()
        raise SigtermInterrupt("SIGTERM during refresh")

    monkeypatch.setattr(concurrency_mod, "run_async", sigterm_run_async)

    def sentinel_sigint(signum: int, frame: Any) -> None:
        pass

    def sentinel_sigterm(signum: int, frame: Any) -> None:
        pass

    saved = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))
    try:
        signal.signal(signal.SIGINT, sentinel_sigint)
        signal.signal(signal.SIGTERM, sentinel_sigterm)

        monitor_mod._watch_loop(1, None, since_window=None, entity_type=None, project=None)

        assert signal.getsignal(signal.SIGINT) is sentinel_sigint
        assert signal.getsignal(signal.SIGTERM) is sentinel_sigterm
    finally:
        signal.signal(signal.SIGINT, saved[0])
        signal.signal(signal.SIGTERM, saved[1])


def test_watch_loop_leaves_unrestorable_handlers_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A handler installed outside Python is reported as None and cannot be
    reinstalled, so taking it over would mean keeping it forever. The loop must
    decline to replace it rather than install a handler it can never remove."""
    import lionagi.cli.monitor as monitor_mod
    import lionagi.ln.concurrency as concurrency_mod
    from lionagi.ln.concurrency.utils import SigtermInterrupt

    monkeypatch.setattr(monitor_mod, "_clear_screen", lambda: None)

    def sigterm_run_async(coro: Any) -> str:
        coro.close()
        raise SigtermInterrupt("SIGTERM during refresh")

    monkeypatch.setattr(concurrency_mod, "run_async", sigterm_run_async)
    monkeypatch.setattr(signal, "getsignal", lambda signum: None)

    installed: list[int] = []
    monkeypatch.setattr(signal, "signal", lambda signum, handler: installed.append(signum))

    monitor_mod._watch_loop(1, None, since_window=None, entity_type=None, project=None)

    assert installed == []
