"""Tests for scripts/rss_report.py: peak-RSS summary and in-flight crash-suspect detection."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.rss_report import summarize


def _write_log(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_no_logs_found(tmp_path: Path) -> None:
    assert summarize(tmp_path) == "no RSS logs found"


def test_worker_with_only_completed_tests_reports_last_completed(tmp_path: Path) -> None:
    _write_log(
        tmp_path / "rss-gw0.jsonl",
        [
            {"worker": "gw0", "test": "t1", "phase": "start", "peak_kb": 100},
            {"worker": "gw0", "test": "t1", "phase": "end", "peak_kb": 110, "delta_kb": 10},
            {"worker": "gw0", "test": "t2", "phase": "start", "peak_kb": 110},
            {"worker": "gw0", "test": "t2", "phase": "end", "peak_kb": 120, "delta_kb": 10},
        ],
    )

    report = summarize(tmp_path)

    assert "2 tests completed" in report
    assert "last test completed: t2" in report
    assert "IN-FLIGHT" not in report


def test_worker_killed_mid_test_reports_in_flight_crash_suspect(tmp_path: Path) -> None:
    """A test with a "start" row but no matching "end" row is the one running
    when the worker died -- exactly the case a truncated log leaves behind."""
    _write_log(
        tmp_path / "rss-gw1.jsonl",
        [
            {"worker": "gw1", "test": "t1", "phase": "start", "peak_kb": 100},
            {"worker": "gw1", "test": "t1", "phase": "end", "peak_kb": 110, "delta_kb": 10},
            {"worker": "gw1", "test": "t2_crashes", "phase": "start", "peak_kb": 110},
            # No "end" row for t2_crashes: the worker died mid-test.
        ],
    )

    report = summarize(tmp_path)

    assert "IN-FLIGHT AT DEATH" in report
    assert "t2_crashes" in report
    assert "1 tests completed" in report


def test_malformed_trailing_line_is_skipped_not_fatal(tmp_path: Path) -> None:
    log = tmp_path / "rss-gw2.jsonl"
    log.write_text(
        json.dumps({"worker": "gw2", "test": "t1", "phase": "start", "peak_kb": 100})
        + "\n"
        + json.dumps({"worker": "gw2", "test": "t1", "phase": "end", "peak_kb": 105, "delta_kb": 5})
        + "\n"
        + '{"worker": "gw2", "test": "t2", "phase":'  # truncated mid-write
    )

    report = summarize(tmp_path)

    assert "skipped 1 malformed line" in report
    assert "last test completed: t1" in report


def test_top_growers_ranked_across_workers(tmp_path: Path) -> None:
    _write_log(
        tmp_path / "rss-gw0.jsonl",
        [
            {"worker": "gw0", "test": "small", "phase": "start", "peak_kb": 100},
            {"worker": "gw0", "test": "small", "phase": "end", "peak_kb": 105, "delta_kb": 5},
        ],
    )
    _write_log(
        tmp_path / "rss-gw1.jsonl",
        [
            {"worker": "gw1", "test": "big", "phase": "start", "peak_kb": 100},
            {"worker": "gw1", "test": "big", "phase": "end", "peak_kb": 50100, "delta_kb": 50000},
        ],
    )

    report = summarize(tmp_path)
    top_section = report.split("top 20 peak-raisers")[1]

    # The biggest grower (big, on gw1) must be listed before the smaller one.
    assert top_section.index("big") < top_section.index("small")
