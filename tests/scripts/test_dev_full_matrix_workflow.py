"""The nightly development matrix stays separate from required CI."""

from __future__ import annotations

from pathlib import Path

import yaml

GITHUB = Path(__file__).resolve().parents[2] / ".github"
SETUP = ".github/actions/setup-python-tests"
PLAN_SHA = "${{ needs.plan.outputs.sha }}"
WORKFLOW_SHA = "${{ github.workflow_sha }}"
MATRIX_VERSION = "${{ matrix.python-version }}"


def _workflow(name):
    return yaml.safe_load((GITHUB / "workflows" / name).read_text())


def _triggers(workflow):
    return workflow.get("on", workflow.get(True))


def _checkouts(job):
    return [
        step["with"]
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    ]


def test_nightly_has_only_schedule_and_dispatch_and_stays_out_of_required_ci():
    nightly = _workflow("dev-full-matrix.yml")
    required = _workflow("ci.yml")
    assert _triggers(nightly) == {"schedule": [{"cron": "23 7 * * *"}], "workflow_dispatch": None}
    assert set(_triggers(required)) == {"push", "pull_request", "workflow_dispatch"}
    assert "ci-gate" not in nightly["jobs"]
    assert not any(
        "dev-full-matrix.yml" in job.get("uses", "") for job in required["jobs"].values()
    )
    assert "test" in required["jobs"]["ci-gate"]["needs"]


def test_nightly_runs_each_supported_python_at_the_resolved_dev_sha():
    jobs = _workflow("dev-full-matrix.yml")["jobs"]
    plan = jobs["plan"]
    test = jobs["test"]
    assert _checkouts(plan) == [{"ref": WORKFLOW_SHA, "persist-credentials": False}]
    assert plan["outputs"]["sha"] == "${{ steps.plan.outputs.sha }}"
    assert plan["outputs"]["run"] == "${{ steps.plan.outputs.run }}"
    assert test["needs"] == "plan"
    assert test["if"] == "needs.plan.outputs.run == 'true'"
    assert test["strategy"] == {
        "fail-fast": False,
        "matrix": {"python-version": ["3.10", "3.11", "3.12", "3.13", "3.14"]},
    }
    assert test["env"]["UV_PYTHON"] == MATRIX_VERSION
    assert _checkouts(test) == [
        {"ref": PLAN_SHA, "persist-credentials": False},
        {
            "ref": WORKFLOW_SHA,
            "path": ".github/.workflow-source",
            "sparse-checkout": SETUP,
            "persist-credentials": False,
        },
    ]


def test_only_a_successful_schedule_can_certify_or_carry_forward_a_dev_sha():
    jobs = _workflow("dev-full-matrix.yml")["jobs"]
    certificate = jobs["certify"]
    assert certificate["name"] == f"Full dev matrix passed {PLAN_SHA}"
    assert certificate["needs"] == ["plan", "test"]
    # A changed head needs successful tests. A skip renews only the exact SHA
    # the planner already verified against a successful scheduled run.
    assert " ".join(certificate["if"].split()) == (
        "${{ !cancelled() && github.event_name == 'schedule' && needs.plan.result == 'success' && "
        "((needs.plan.outputs.run == 'true' && needs.test.result == 'success') || "
        "(needs.plan.outputs.run == 'false' && needs.test.result == 'skipped')) }}"
    )
    assert certificate["steps"][0]["env"] == {
        "DEV_SHA": PLAN_SHA,
        "DID_RUN": "${{ needs.plan.outputs.run }}",
    }


def test_the_only_issue_writer_runs_after_scheduled_failure_on_the_workflow_source():
    workflow = _workflow("dev-full-matrix.yml")
    jobs = workflow["jobs"]
    assert workflow["permissions"] == {}
    assert jobs["plan"]["permissions"] == {"actions": "read", "contents": "read"}
    assert jobs["test"]["permissions"] == {"contents": "read"}
    assert jobs["certify"].get("permissions", {}) == {}
    report = jobs["report"]
    assert report["permissions"] == {"actions": "read", "contents": "read", "issues": "write"}
    assert report["needs"] == ["plan", "test", "certify"]
    assert " ".join(report["if"].split()) == (
        "${{ !cancelled() && github.event_name == 'schedule' && "
        "(needs.plan.result == 'failure' || needs.test.result == 'failure' || "
        "needs.certify.result == 'failure') }}"
    )
    assert _checkouts(report) == [{"ref": WORKFLOW_SHA, "persist-credentials": False}]
    step = next(step for step in report["steps"] if "run" in step)
    assert step["env"] == {"GH_TOKEN": "${{ github.token }}", "TESTED_SHA": PLAN_SHA}


def test_nightly_and_required_tests_share_setup_and_keep_timed_suite_steps():
    nightly = _workflow("dev-full-matrix.yml")["jobs"]["test"]
    required = _workflow("ci.yml")["jobs"]["test"]
    for job, uses, options in [
        (nightly, f"./.github/.workflow-source/{SETUP}", {"cache-prefix": "nightly-uv"}),
        (required, f"./{SETUP}", {}),
    ]:
        setup = next(step for step in job["steps"] if step.get("uses") == uses)
        suite = next(
            step for step in job["steps"] if step.get("run") == "scripts/ci.sh test-python"
        )
        assert setup["with"] == {"python-version": MATRIX_VERSION, **options}
        assert job["steps"].index(setup) < job["steps"].index(suite)
        assert suite["timeout-minutes"] == 45
        assert suite["timeout-minutes"] < job["timeout-minutes"]
        assert suite["env"]["MAXFAIL"] == 0
    required_plain = next(
        step for step in required["steps"] if step.get("run") == "scripts/ci.sh test-python"
    )
    assert (
        required_plain["if"]
        == "matrix.python-version != '3.10' || github.event_name == 'pull_request'"
    )
    coverage = next(
        step for step in required["steps"] if step.get("run") == "scripts/ci.sh test-python-cov"
    )
    assert (
        coverage["if"] == "matrix.python-version == '3.10' && github.event_name != 'pull_request'"
    )
    assert coverage["timeout-minutes"] == 60
    assert coverage["timeout-minutes"] < required["timeout-minutes"]
