"""Offline command-line checks for GitHub flake reports."""

from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from scripts import flake_report

REPOSITORY = "ohdearquant/lionagi"
DOWNLOAD_REPOSITORY = f"github.com/{REPOSITORY}"
FIRST = "flake-failures-py3.10-attempt-1"
SECOND = "flake-failures-py3.14-attempt-2"
KNOWN = "tests/example/test_flake.py::test_known"
NEW = "tests/example/test_flake.py::test_new"
SIGNATURE = "AssertionError: unstable value"
MISSING_PAYLOAD = object()


def _artifact(artifact_id, name, *, expired=False):
    return {"id": artifact_id, "name": name, "expired": expired}


def _listing(*artifacts):
    return [{"total_count": len(artifacts), "artifacts": list(artifacts)}]


def _record(nodeid, run_id, matrix_leg="3.10"):
    return {
        "schema_version": 1,
        "nodeid": nodeid,
        "signature": SIGNATURE,
        "run_id": run_id,
        "matrix_leg": matrix_leg,
        "attempt": 1,
    }


@pytest.fixture
def report(monkeypatch, tmp_path):
    listings = {}
    downloads = {}
    calls = []

    def stub_gh(command, **_kwargs):
        calls.append(command)
        if command[:2] == ["gh", "api"]:
            assert command[2:6] == ["--hostname", "github.com", "--paginate", "--slurp"]
            (route,) = command[6:]
            prefix = f"repos/{REPOSITORY}/actions/runs/"
            suffix = "/artifacts?per_page=100"
            assert route.startswith(prefix) and route.endswith(suffix)
            run_id = route.removeprefix(prefix).removesuffix(suffix)
            pages = listings[run_id]
            if pages is None:
                return CompletedProcess(command, 1, "", "listing unavailable")
            payload = pages if isinstance(pages, str) else json.dumps(pages)
            return CompletedProcess(command, 0, payload, "")

        if "--pattern" in command:
            return CompletedProcess(command, 1, "", "artifact unavailable")
        assert command[:3] == ["gh", "run", "download"]
        _, _, _, run_id, repo_flag, repo, name_flag, name, dir_flag, target = command
        assert (repo_flag, repo, name_flag, dir_flag) == (
            "--repo",
            DOWNLOAD_REPOSITORY,
            "--name",
            "--dir",
        )
        records = downloads[(run_id, name)]
        if records is None:
            return CompletedProcess(command, 1, "", "artifact missing")
        if records is MISSING_PAYLOAD:
            return CompletedProcess(command, 0, "", "")
        directory = Path(target)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "failures.jsonl").write_text(
            "".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8"
        )
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(flake_report.subprocess, "run", stub_gh)
    quarantine = tmp_path / "quarantine.txt"
    quarantine.write_text(f"2026-07-01 | {KNOWN} | {SIGNATURE}\n", encoding="utf-8")

    def run(*runs, paths=()):
        args = [*map(str, paths), "--quarantine", str(quarantine)]
        if runs:
            run_file = tmp_path / "runs.json"
            rows = [
                row
                if isinstance(row, dict)
                else {"databaseId": row, "status": "completed", "conclusion": "success"}
                for row in runs
            ]
            run_file.write_text(json.dumps(rows), encoding="utf-8")
            args.extend(("--gh-runs", str(run_file)))
        return flake_report.main(args)

    return run, listings, downloads, calls


def test_empty_remote_runs_with_known_outcomes_are_read(report, capsys):
    run, listings, _, calls = report
    listings.update({"42": _listing(), "43": _listing(_artifact(3, "coverage", expired=True))})

    assert run(42, {"databaseId": 43, "status": "completed", "conclusion": "failure"}, "42") == 0

    output = capsys.readouterr()
    assert output.out.splitlines() == [
        "Runs requested: 2; runs read: 2; artifacts read: 0; distinct tests: 0.",
        "No flake failure records found.",
    ]
    assert output.err == ""
    assert len(calls) == 2
    assert all(call[1] == "api" for call in calls)


@pytest.mark.parametrize(
    ("outcome", "diagnostic"),
    [
        pytest.param(
            {"status": "completed", "conclusion": "cancelled"}, "cancelled", id="cancelled"
        ),
        pytest.param({}, "status=None, conclusion=None", id="id-only"),
        pytest.param({"status": "in_progress", "conclusion": ""}, "in_progress", id="in-progress"),
        pytest.param({"status": "completed", "conclusion": "timed_out"}, "timed_out", id="timeout"),
        pytest.param(
            {"status": "completed", "conclusion": "startup_failure"},
            "startup_failure",
            id="startup",
        ),
        pytest.param({"status": "completed", "conclusion": "skipped"}, "skipped", id="skipped"),
    ],
)
def test_unknown_or_interrupted_outcome_marks_run_incomplete(report, capsys, outcome, diagnostic):
    run, listings, _, calls = report
    listings["42"] = _listing()

    assert run({"databaseId": 42, **outcome}) == 3

    output = capsys.readouterr()
    assert "Incomplete flake report: 1 of 1 runs could not be read completely." in output.out
    assert "Runs requested: 1; runs read: 0; artifacts read: 0; distinct tests: 0." in output.out
    assert "run 42" in output.err and diagnostic in output.err
    assert len(calls) == 1 and calls[0][1] == "api"


def test_interrupted_run_keeps_its_readable_artifact_rows(report, capsys):
    run, listings, downloads, _ = report
    listings["42"] = _listing(_artifact(1, FIRST))
    downloads[("42", FIRST)] = [_record(KNOWN, "42")]

    assert run({"databaseId": 42, "status": "completed", "conclusion": "cancelled"}) == 3

    output = capsys.readouterr()
    assert "runs read: 0; artifacts read: 1; distinct tests: 1." in output.out
    assert KNOWN in output.out and "cancelled" in output.err


def test_conflicting_outcomes_for_duplicate_runs_are_an_input_error(report, capsys):
    run, _, _, calls = report

    assert run(42, {"run_id": "42", "status": "completed", "conclusion": "failure"}) == 2

    assert "conflicting outcomes for GitHub run 42" in capsys.readouterr().err
    assert calls == []


def test_empty_run_list_does_not_call_github(monkeypatch, report, capsys):
    from io import StringIO

    _, _, _, calls = report
    monkeypatch.setattr("sys.stdin", StringIO("[]"))
    assert flake_report.main(["--gh-runs", "-"]) == 0
    assert (
        "Runs requested: 0; runs read: 0; artifacts read: 0; distinct tests: 0."
        in capsys.readouterr().out
    )
    assert calls == []


def test_reports_artifacts_from_final_page_and_counts_distinct_runs_and_tests(report, capsys):
    run, listings, downloads, calls = report
    unrelated = [_artifact(i, f"other-{i}") for i in range(2, 101)]
    listings["42"] = [
        {"total_count": 101, "artifacts": [_artifact(1, FIRST), *unrelated]},
        {"total_count": 101, "artifacts": [_artifact(101, SECOND)]},
    ]
    listings["43"] = _listing(_artifact(102, SECOND))
    older_record = _record(KNOWN, "42", "3.14")
    del older_record["run_id"]
    downloads.update(
        {
            ("42", FIRST): [_record(KNOWN, "42")],
            ("42", SECOND): [older_record],
            ("43", SECOND): [
                _record(NEW, "43", "3.14"),
                _record("<pytest-session>", "43", "3.14"),
            ],
        }
    )

    assert run(42, 43, 42) == 0

    output = capsys.readouterr()
    assert output.err == ""
    assert "Runs requested: 2; runs read: 2; artifacts read: 3; distinct tests: 2." in output.out
    assert "       2  1     quarantined  3.10,3.14" in output.out
    assert "       1  1     NEW          3.14" in output.out
    assert KNOWN in output.out and NEW in output.out
    assert {tuple(call[3:8]) for call in calls if call[1] == "run"} == {
        ("42", "--repo", DOWNLOAD_REPOSITORY, "--name", FIRST),
        ("42", "--repo", DOWNLOAD_REPOSITORY, "--name", SECOND),
        ("43", "--repo", DOWNLOAD_REPOSITORY, "--name", SECOND),
    }


@pytest.mark.parametrize(
    "pages",
    [
        pytest.param(None, id="listing-command-failed"),
        pytest.param("not JSON", id="invalid-json"),
        pytest.param([{"total_count": 0, "artifacts": None}], id="null-artifact-list"),
        pytest.param(_listing(_artifact(1, FIRST, expired=None)), id="null-expiration"),
        pytest.param(
            [{"total_count": 2, "artifacts": [_artifact(1, FIRST)]}], id="truncated-pagination"
        ),
    ],
)
def test_bad_listing_marks_window_incomplete_without_downloading(report, capsys, pages):
    run, listings, _, calls = report
    listings["42"] = pages

    assert run(42) == 3

    output = capsys.readouterr().out
    assert "Incomplete flake report: 1 of 1 runs could not be read completely." in output
    assert "Runs requested: 1; runs read: 0; artifacts read: 0; distinct tests: 0." in output
    assert "No flake failure records found." in output
    assert len(calls) == 1 and calls[0][1] == "api"


@pytest.mark.parametrize("expired", [True, False], ids=["expired", "missing"])
def test_incomplete_run_retains_rows_from_its_successful_downloads(report, capsys, expired):
    run, listings, downloads, calls = report
    listings["42"] = _listing(_artifact(1, FIRST), _artifact(2, SECOND, expired=expired))
    listings["43"] = _listing()
    downloads.update({("42", FIRST): [_record(KNOWN, "42")], ("42", SECOND): None})

    assert run(42, 43) == 3

    output = capsys.readouterr().out
    assert "Incomplete flake report: 1 of 2 runs could not be read completely." in output
    assert "Runs requested: 2; runs read: 1; artifacts read: 1; distinct tests: 1." in output
    assert "quarantined" in output and KNOWN in output
    assert f"signature (1 failure(s), 1 run(s)): {SIGNATURE}" in output
    assert sum(call[1] == "run" for call in calls) == (1 if expired else 2)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(MISSING_PAYLOAD, id="missing-file"),
        pytest.param([], id="no-records"),
        pytest.param([{"nodeid": KNOWN}], id="malformed-record"),
        pytest.param([_record(KNOWN, "99")], id="wrong-run"),
    ],
)
def test_missing_or_unreadable_download_payload_marks_run_incomplete(report, capsys, payload):
    run, listings, downloads, _ = report
    listings["42"] = _listing(_artifact(1, FIRST))
    downloads[("42", FIRST)] = payload

    assert run(42) == 3
    output = capsys.readouterr()
    assert "Incomplete flake report: 1 of 1" in output.out
    assert "artifacts read: 0; distinct tests: 0." in output.out
    assert FIRST in output.err


@pytest.mark.parametrize("bad_id", ["../17", "--repo", "１", None, False])
def test_invalid_run_id_is_an_input_error_without_github_access(report, capsys, bad_id):
    run, _, _, calls = report

    assert run(bad_id) == 2
    assert "expected a positive GitHub run ID" in capsys.readouterr().err
    assert calls == []


def test_local_only_keeps_the_table_and_argument_error_status(report, tmp_path, capsys):
    run, _, _, calls = report
    artifact = tmp_path / "local.jsonl"
    record = _record(KNOWN, "42")
    artifact.write_text(json.dumps(record) + "\n", encoding="utf-8")

    assert run(paths=(artifact,)) == 0
    assert (
        capsys.readouterr().out == flake_report.render_report([record], quarantined={KNOWN}) + "\n"
    )
    assert run() == 2
    artifact.write_text("invalid JSON\n", encoding="utf-8")
    assert run(paths=(artifact,)) == 2
    assert calls == []
