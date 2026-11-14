"""The nightly failure report routes to one labelled issue without contacting GitHub."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "dev_full_matrix_report", ROOT / ".github/scripts/dev_full_matrix_report.py"
)
assert SPEC is not None and SPEC.loader is not None
report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report)

PREFIX = "repos/ohdearquant/lionagi"
RUN_ID = "123"
ATTEMPT = "2"
SHA = "b" * 40
LABEL = "dev-full-matrix-failure"
REPORTER = {"login": "github-actions[bot]", "type": "Bot"}
JOBS = f"{PREFIX}/actions/runs/{RUN_ID}/attempts/{ATTEMPT}/jobs"
OPEN_ISSUES = f"{PREFIX}/issues?state=open&labels={LABEL}"
URL = f"https://github.com/ohdearquant/lionagi/actions/runs/{RUN_ID}/attempts/{ATTEMPT}"


@pytest.fixture
def github(monkeypatch):
    """Record exact GitHub routes and request bodies using offline page fixtures."""
    responses = {
        JOBS: [
            {
                "jobs": [
                    {"name": "Python 3.14", "conclusion": "timed_out"},
                    {"name": "Python 3.11", "conclusion": "cancelled"},
                    {"name": "Python 3.10", "conclusion": "success"},
                    {"name": "Python 9.99", "conclusion": "failure"},
                    {"name": "Infra Python 3.12", "conclusion": "failure"},
                ]
            },
            {"jobs": [{"name": "Python 3.13", "conclusion": "failure"}]},
        ]
    }
    calls = []

    def fixture_github(*arguments, data=None):
        calls.append((arguments, data))
        if arguments[:2] == ("--paginate", "--slurp"):
            return json.dumps(responses[arguments[2]])
        assert arguments[:2] == ("--method", "POST")
        return "{}"

    monkeypatch.setattr(report, "github", fixture_github)
    return responses, calls


def test_creates_missing_label_and_one_issue_with_this_attempts_failed_versions(github):
    responses, calls = github
    responses[OPEN_ISSUES] = [[{"number": 4, "pull_request": {}}], []]
    responses[f"{PREFIX}/labels"] = [[{"name": "another-label"}]]

    report.report_failure(SHA, RUN_ID, ATTEMPT)

    assert [args for args, _ in calls] == [
        ("--paginate", "--slurp", JOBS),
        ("--paginate", "--slurp", OPEN_ISSUES),
        ("--paginate", "--slurp", f"{PREFIX}/labels"),
        ("--method", "POST", f"{PREFIX}/labels"),
        ("--method", "POST", f"{PREFIX}/issues"),
    ]
    assert calls[-2][1]["name"] == LABEL
    issue = calls[-1][1]
    assert issue["labels"] == [LABEL]
    assert SHA in issue["body"]
    assert f"[Workflow run]({URL})" in issue["body"]
    assert "Failing Python versions: 3.13, 3.14." in issue["body"]
    assert all(version not in issue["body"] for version in ("3.10", "3.11", "3.12", "9.99"))


def test_reports_infrastructure_failure_by_commenting_on_the_existing_issue(github):
    responses, calls = github
    comments = f"{PREFIX}/issues/8/comments"
    responses[JOBS] = [{"jobs": [{"name": "Resolve dev commit", "conclusion": "failure"}]}]
    responses[OPEN_ISSUES] = [[{"number": 8, "body": None}]]
    responses[comments] = [[{"body": "A previous run failed"}]]

    report.report_failure("", RUN_ID, ATTEMPT)

    assert [args for args, _ in calls[-2:]] == [
        ("--paginate", "--slurp", comments),
        ("--method", "POST", comments),
    ]
    body = calls[-1][1]["body"]
    assert "Dev commit: unavailable" in body
    assert "No Python version reported a failure" in body
    assert URL in body


@pytest.mark.parametrize("already_in", ["issue", "comment"])
def test_reporting_the_same_attempt_again_does_not_write(github, already_in):
    responses, calls = github
    marker = f"<!-- dev-full-matrix: {RUN_ID}/{ATTEMPT} -->"
    report_entry = {"user": REPORTER, "body": marker}
    if already_in == "issue":
        responses[OPEN_ISSUES] = [[{"number": 8, **report_entry}]]
    else:
        responses[OPEN_ISSUES] = [[{"number": 8, "body": ""}]]
        responses[f"{PREFIX}/issues/8/comments"] = [[report_entry]]

    report.report_failure(SHA, RUN_ID, ATTEMPT)

    assert all(data is None for _, data in calls)


@pytest.mark.parametrize("author", [{"login": "contributor", "type": "User"}, None])
def test_only_workflow_reports_count_for_retry_deduplication(github, author):
    responses, calls = github
    marker = f"<!-- dev-full-matrix: {RUN_ID}/{ATTEMPT} -->"
    entry = {"user": author, "body": marker}
    responses[OPEN_ISSUES] = [[{"number": 8, **entry}]]
    comments = f"{PREFIX}/issues/8/comments"
    responses[comments] = [[entry]]

    report.report_failure(SHA, RUN_ID, ATTEMPT)

    args, body = calls[-1]
    assert args == ("--method", "POST", comments)
    assert URL in body["body"]


def test_invalid_dispatch_values_fail_before_any_request(github):
    _, calls = github
    for invalid in (("branch", RUN_ID, ATTEMPT), (SHA, "123/jobs", ATTEMPT), (SHA, RUN_ID, "0")):
        with pytest.raises(ValueError):
            report.report_failure(*invalid)
    assert not calls
