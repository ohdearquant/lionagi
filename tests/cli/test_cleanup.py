# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``li state cleanup`` (lionagi/cli/cleanup.py).

All tests use tmp_path so nothing in the real ~/.lionagi tree is touched.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from lionagi.cli.cleanup import (
    _format_bytes,
    cleanup_logs,
    cleanup_runs,
    cleanup_teams,
)

# ── helpers ────────────────────────────────────────────────────────────────────

_DAY = 86_400


def _age(path: Path, days: float) -> None:
    """Back-date a path's mtime by `days` days from now."""
    ts = time.time() - days * _DAY
    os.utime(path, (ts, ts))


def _make_run_dir(root: Path, name: str, age_days: float = 0.0) -> Path:
    """Create a minimal run directory and back-date it."""
    d = root / name
    d.mkdir(parents=True)
    manifest = d / "run.json"
    manifest.write_text(json.dumps({"run_id": name}), encoding="utf-8")
    if age_days:
        _age(d, age_days)
        _age(manifest, age_days)
    return d


def _make_team_file(root: Path, name: str, *, valid: bool = True) -> Path:
    """Create a team JSON file; if not valid, write invalid content."""
    root.mkdir(parents=True, exist_ok=True)
    p = root / f"{name}.json"
    if valid:
        p.write_text(json.dumps({"id": name, "members": []}), encoding="utf-8")
    else:
        p.write_text("not json", encoding="utf-8")
    return p


def _make_team_file_no_id(root: Path, name: str) -> Path:
    """Create a team JSON file that has no ``id`` field."""
    root.mkdir(parents=True, exist_ok=True)
    p = root / f"{name}.json"
    p.write_text(json.dumps({"name": name}), encoding="utf-8")
    return p


def _make_log_file(root: Path, rel: str, age_days: float = 0.0) -> Path:
    """Create a log file under root and optionally back-date it."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("log line\n", encoding="utf-8")
    if age_days:
        _age(p, age_days)
    return p


# ── _format_bytes ─────────────────────────────────────────────────────────────


def test_format_bytes_b() -> None:
    assert _format_bytes(512) == "512.0 B"


def test_format_bytes_kib() -> None:
    result = _format_bytes(1024)
    assert "KiB" in result


def test_format_bytes_mib() -> None:
    result = _format_bytes(1024 * 1024)
    assert "MiB" in result


# ── cleanup_runs ──────────────────────────────────────────────────────────────


def test_cleanup_runs_dry_run_does_not_delete(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Dry-run must not remove any directory."""
    runs = tmp_path / "runs"
    _make_run_dir(runs, "old-run", age_days=60)

    result = cleanup_runs(runs, older_than_days=30, dry_run=True)

    assert result["removed"] == 1
    assert (runs / "old-run").exists(), "dry-run must not remove the directory"
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "old-run" in out


def test_cleanup_runs_removes_old_dirs(tmp_path: Path) -> None:
    """Old run directories are removed; recent ones are kept."""
    runs = tmp_path / "runs"
    _make_run_dir(runs, "old-run-1", age_days=45)
    _make_run_dir(runs, "old-run-2", age_days=60)
    _make_run_dir(runs, "recent-run", age_days=5)

    result = cleanup_runs(runs, older_than_days=30, dry_run=False)

    assert result["removed"] == 2
    assert result["errors"] == 0
    assert not (runs / "old-run-1").exists()
    assert not (runs / "old-run-2").exists()
    assert (runs / "recent-run").exists(), "recent run must be kept"


def test_cleanup_runs_keeps_recent(tmp_path: Path) -> None:
    """Runs newer than the threshold must survive."""
    runs = tmp_path / "runs"
    _make_run_dir(runs, "run-1d", age_days=1)
    _make_run_dir(runs, "run-10d", age_days=10)

    result = cleanup_runs(runs, older_than_days=30, dry_run=False)

    assert result["removed"] == 0
    assert (runs / "run-1d").exists()
    assert (runs / "run-10d").exists()


def test_cleanup_runs_missing_root_is_noop(tmp_path: Path) -> None:
    """Non-existent runs root must not raise; returns zeros."""
    result = cleanup_runs(tmp_path / "no-such-dir", older_than_days=30, dry_run=False)
    assert result["removed"] == 0
    assert result["errors"] == 0


def test_cleanup_runs_counts_bytes(tmp_path: Path) -> None:
    """bytes_freed should be positive when directories contain files."""
    runs = tmp_path / "runs"
    d = _make_run_dir(runs, "old", age_days=60)
    # Write a file with known content so there is something to measure.
    (d / "data.txt").write_bytes(b"x" * 1024)
    _age(d, 60)

    result = cleanup_runs(runs, older_than_days=30, dry_run=False)

    assert result["removed"] == 1
    assert result["bytes_freed"] > 0


def test_cleanup_runs_older_than_filtering(tmp_path: Path) -> None:
    """--older-than boundary: exactly-at-threshold is kept, one-day-over is removed."""
    runs = tmp_path / "runs"
    # 29 days old — should be kept when threshold is 30
    _make_run_dir(runs, "barely-recent", age_days=29)
    # 31 days old — should be removed
    _make_run_dir(runs, "just-over", age_days=31)

    result = cleanup_runs(runs, older_than_days=30, dry_run=False)

    assert result["removed"] == 1
    assert (runs / "barely-recent").exists()
    assert not (runs / "just-over").exists()


# ── cleanup_teams ─────────────────────────────────────────────────────────────


def test_cleanup_teams_dry_run_does_not_delete(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Dry-run must not remove any file."""
    teams = tmp_path / "teams"
    p = _make_team_file(teams, "orphan", valid=False)

    result = cleanup_teams(teams, dry_run=True)

    assert result["removed"] == 1
    assert p.exists(), "dry-run must not remove the file"
    out = capsys.readouterr().out
    assert "dry-run" in out


def test_cleanup_teams_removes_invalid_json(tmp_path: Path) -> None:
    """Team files with unparseable JSON are treated as orphaned and removed."""
    teams = tmp_path / "teams"
    bad = _make_team_file(teams, "bad", valid=False)
    good = _make_team_file(teams, "good", valid=True)

    result = cleanup_teams(teams, dry_run=False)

    assert result["removed"] == 1
    assert not bad.exists()
    assert good.exists()


def test_cleanup_teams_removes_no_id(tmp_path: Path) -> None:
    """Team files without an ``id`` field are orphaned."""
    teams = tmp_path / "teams"
    p = _make_team_file_no_id(teams, "noid")

    result = cleanup_teams(teams, dry_run=False)

    assert result["removed"] == 1
    assert not p.exists()


def test_cleanup_teams_keeps_valid_files(tmp_path: Path) -> None:
    """Valid team files with an ``id`` are NOT removed."""
    teams = tmp_path / "teams"
    _make_team_file(teams, "alpha", valid=True)
    _make_team_file(teams, "beta", valid=True)

    result = cleanup_teams(teams, dry_run=False)

    assert result["removed"] == 0
    assert (teams / "alpha.json").exists()
    assert (teams / "beta.json").exists()


def test_cleanup_teams_missing_root_is_noop(tmp_path: Path) -> None:
    result = cleanup_teams(tmp_path / "no-teams", dry_run=False)
    assert result["removed"] == 0


# ── cleanup_logs ──────────────────────────────────────────────────────────────


def test_cleanup_logs_dry_run_does_not_delete(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    logs = tmp_path / "logs"
    p = _make_log_file(logs, "old.log", age_days=60)

    result = cleanup_logs(logs, older_than_days=30, dry_run=True)

    assert result["removed"] == 1
    assert p.exists()
    out = capsys.readouterr().out
    assert "dry-run" in out


def test_cleanup_logs_removes_old_files(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    old_log = _make_log_file(logs, "session.log", age_days=60)
    old_jsonl = _make_log_file(logs, "sub/events.jsonl", age_days=45)
    recent = _make_log_file(logs, "recent.log", age_days=5)

    result = cleanup_logs(logs, older_than_days=30, dry_run=False)

    assert result["removed"] == 2
    assert not old_log.exists()
    assert not old_jsonl.exists()
    assert recent.exists()


def test_cleanup_logs_older_than_filtering(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    _make_log_file(logs, "a.log", age_days=29)
    _make_log_file(logs, "b.log", age_days=31)

    result = cleanup_logs(logs, older_than_days=30, dry_run=False)

    assert result["removed"] == 1
    assert (logs / "a.log").exists()
    assert not (logs / "b.log").exists()


def test_cleanup_logs_missing_root_is_noop(tmp_path: Path) -> None:
    result = cleanup_logs(tmp_path / "no-logs", older_than_days=30, dry_run=False)
    assert result["removed"] == 0


# ── run_cleanup integration ───────────────────────────────────────────────────


def test_run_cleanup_force_skips_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """--force must bypass the interactive confirmation prompt."""
    import argparse

    from lionagi.cli.cleanup import run_cleanup

    monkeypatch.setattr("lionagi._paths.LIONAGI_HOME", tmp_path)

    runs = tmp_path / "runs"
    _make_run_dir(runs, "old", age_days=60)

    args = argparse.Namespace(
        older_than=30,
        dry_run=False,
        runs=True,
        teams=False,
        logs=False,
        clean_all=False,
        force=True,
    )
    rc = run_cleanup(args)
    assert rc == 0

    out = capsys.readouterr().out
    assert "removed" in out


def test_run_cleanup_dry_run_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """--dry-run with --all prints summary and deletes nothing."""
    import argparse

    from lionagi.cli.cleanup import run_cleanup

    monkeypatch.setattr("lionagi._paths.LIONAGI_HOME", tmp_path)

    # Populate some state.
    runs = tmp_path / "runs"
    _make_run_dir(runs, "old-x", age_days=60)
    teams = tmp_path / "teams"
    _make_team_file(teams, "orphan", valid=False)
    logs = tmp_path / "logs"
    _make_log_file(logs, "old.log", age_days=60)

    args = argparse.Namespace(
        older_than=30,
        dry_run=True,
        runs=False,
        teams=False,
        logs=False,
        clean_all=True,
        force=True,
    )
    rc = run_cleanup(args)
    assert rc == 0

    # Nothing must have been deleted.
    assert (runs / "old-x").exists()
    assert (teams / "orphan.json").exists()
    assert (logs / "old.log").exists()

    out = capsys.readouterr().out
    assert "dry-run" in out


def test_run_cleanup_default_scope_is_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """When no scope flag is given, the command defaults to cleaning everything."""
    import argparse

    from lionagi.cli.cleanup import run_cleanup

    monkeypatch.setattr("lionagi._paths.LIONAGI_HOME", tmp_path)

    runs = tmp_path / "runs"
    _make_run_dir(runs, "old", age_days=60)

    args = argparse.Namespace(
        older_than=30,
        dry_run=True,  # safe — nothing will actually be deleted
        runs=False,
        teams=False,
        logs=False,
        clean_all=False,  # intentionally unset — should default to all
        force=True,
    )
    rc = run_cleanup(args)
    assert rc == 0

    out = capsys.readouterr().out
    # With dry_run, we should see the "would remove" summary line.
    assert "would remove" in out


def test_run_cleanup_summary_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Output summary line must contain count and space metrics."""
    import argparse

    from lionagi.cli.cleanup import run_cleanup

    monkeypatch.setattr("lionagi._paths.LIONAGI_HOME", tmp_path)

    runs = tmp_path / "runs"
    _make_run_dir(runs, "old", age_days=60)

    args = argparse.Namespace(
        older_than=30,
        dry_run=True,
        runs=True,
        teams=False,
        logs=False,
        clean_all=False,
        force=True,
    )
    run_cleanup(args)
    out = capsys.readouterr().out
    # Summary line should mention count and a byte unit.
    assert "item" in out
    assert any(unit in out for unit in ("B", "KiB", "MiB", "GiB"))
