"""Contract tests for CI quarantine and flake telemetry tooling."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from scripts.collect_flake_failures import records_from_junit
from scripts.flake_report import render_report
from scripts.quarantine import (
    QuarantineEntry,
    QuarantineError,
    apply_quarantine_markers,
    enforce_cap,
    load_manifest,
)
from scripts.quarantine import (
    main as quarantine_main,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CI_SCRIPT = REPO_ROOT / "scripts" / "ci.sh"


def test_quarantine_manifest_parses_metadata_and_signature_delimiters(tmp_path: Path) -> None:
    manifest = tmp_path / "quarantine.txt"
    manifest.write_text(
        "# date | nodeid | signature\n"
        "2026-07-01 | tests/example/test_case.py::test_one | AssertionError: a | b\n"
    )

    assert load_manifest(manifest) == [
        QuarantineEntry(
            date(2026, 7, 1),
            "tests/example/test_case.py::test_one",
            "AssertionError: a | b",
        )
    ]


def test_quarantine_cap_names_oldest_entries() -> None:
    entries = [
        QuarantineEntry(
            date(2026, 1, 1) + timedelta(days=index),
            f"tests/example/test_case.py::test_{index}",
            "AssertionError",
        )
        for index in range(16)
    ]

    with pytest.raises(QuarantineError, match="test_0") as exc_info:
        enforce_cap(entries, max_entries=15)

    assert "test_15" not in str(exc_info.value)
    assert "hard cap is 15" in str(exc_info.value)


def test_quarantine_check_collects_each_exact_nodeid(tmp_path: Path, capsys) -> None:
    manifest = tmp_path / "quarantine.txt"
    manifest.write_text(
        "2026-07-01 | "
        "tests/scripts/test_ci_flake_hardening.py::"
        "test_quarantine_check_collects_each_exact_nodeid | AssertionError: example\n"
    )

    assert quarantine_main(["check", "--manifest", str(manifest)]) == 0

    manifest.write_text(
        "2026-07-01 | tests/scripts/test_ci_flake_hardening.py::test_missing | "
        "AssertionError: example\n"
    )
    assert quarantine_main(["check", "--manifest", str(manifest)]) == 1
    assert "do not collect" in capsys.readouterr().err


def test_collection_hook_marks_a_manifest_node() -> None:
    entry = QuarantineEntry(
        date(2026, 7, 1),
        "tests/example/test_case.py::test_one",
        "AssertionError: example",
    )
    added = []
    item = type(
        "FakeItem",
        (),
        {"nodeid": entry.nodeid, "add_marker": lambda self, mark: added.append(mark)},
    )()
    apply_quarantine_markers([item], [entry], pytest.mark.flaky_quarantine)

    assert len(added) == 1
    assert added[0].mark.name == "flaky_quarantine"


def test_junit_failure_becomes_exact_nodeid_record(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite>
  <testcase classname="tests.sample.test_demo.TestThing"
            name="test_value[param]" file="tests/sample/test_demo.py" line="12">
    <failure message="assertion failed">traceback line
E   AssertionError: expected stable value
    </failure>
  </testcase>
</testsuite></testsuites>
"""
    )

    records = list(records_from_junit(junit, matrix_leg="3.14", run_id="42", attempt=2))

    assert records == [
        {
            "schema_version": 1,
            "nodeid": "tests/sample/test_demo.py::TestThing::test_value[param]",
            "matrix_leg": "3.14",
            "signature": "AssertionError: expected stable value",
            "run_id": "42",
            "attempt": 2,
        }
    ]


def test_junit_xdist_crash_signature_ignores_worker_identity(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite>
  <testcase classname="tests.sample.test_demo" name="test_crash"
            file="tests/sample/test_demo.py" line="12">
    <error message="worker 'gw7' crashed while running 'tests/sample/test_demo.py::test_crash'">
worker 'gw7' crashed while running 'tests/sample/test_demo.py::test_crash'
    </error>
  </testcase>
</testsuite></testsuites>
"""
    )

    records = list(records_from_junit(junit, matrix_leg="3.13", run_id="42", attempt=1))

    assert records[0]["nodeid"] == "tests/sample/test_demo.py::test_crash"
    assert records[0]["signature"] == "xdist worker crashed while running test"


def test_flake_report_counts_and_labels_quarantined_and_new() -> None:
    quarantined = "tests/sample/test_demo.py::test_known"
    records = [
        {
            "nodeid": quarantined,
            "matrix_leg": "3.10",
            "signature": "AssertionError: x",
            "run_id": "10",
        },
        {
            "nodeid": quarantined,
            "matrix_leg": "3.14",
            "signature": "AssertionError: x",
            "run_id": "10",
        },
        {
            "nodeid": "tests/sample/test_demo.py::test_new",
            "matrix_leg": "3.14",
            "signature": "RuntimeError: y",
            "run_id": "11",
        },
    ]

    report = render_report(records, quarantined={quarantined})

    assert "       2  1     quarantined  3.10,3.14" in report
    assert "       1  1     NEW          3.14" in report
    assert "signature (2 failure(s), 1 run(s)): AssertionError: x" in report
    assert "signature (1 failure(s), 1 run(s)): RuntimeError: y" in report


def test_flake_report_shows_every_signature_and_independent_run_count() -> None:
    nodeid = "tests/sample/test_demo.py::test_mixed"
    records = [
        {"nodeid": nodeid, "matrix_leg": "3.10", "signature": "Error: old", "run_id": "1"},
        {"nodeid": nodeid, "matrix_leg": "3.14", "signature": "Error: old", "run_id": "2"},
        {"nodeid": nodeid, "matrix_leg": "3.14", "signature": "Error: new", "run_id": "2"},
    ]

    report = render_report(records, quarantined=set())

    assert "       3  2     NEW" in report
    assert "signature (2 failure(s), 2 run(s)): Error: old" in report
    assert "signature (1 failure(s), 1 run(s)): Error: new" in report


def test_workflow_keeps_quarantine_outside_fail_closed_gate() -> None:
    workflow = CI_WORKFLOW.read_text()
    gate = workflow.split("  ci-gate:", 1)[1].split("  publish:", 1)[0]

    assert "  quarantine:\n    continue-on-error: true" in workflow
    assert (
        "needs: [lint, docs, test, frontend, studio-e2e, changes, studio-docker, vscode, marketplace]"
        in gate
    )
    assert '"quarantine"' not in gate
    assert "run: scripts/ci.sh test-python-quarantine" in workflow
    assert "run: scripts/ci.sh lint-quarantine" in workflow


def test_required_ci_wrapper_excludes_only_performance_and_quarantine() -> None:
    script = CI_SCRIPT.read_text()

    assert "not performance and not flaky_quarantine" in script
    assert '--max-worker-restart="${MAX_WORKER_RESTART:-0}"' in script
    assert "pytest-rerunfailures" not in script
