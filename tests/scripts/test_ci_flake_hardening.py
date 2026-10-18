"""Contract tests for CI quarantine and flake telemetry tooling."""

from __future__ import annotations

import os
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from scripts.check_suite_coverage import (
    CoverageCheckError,
    collected_count,
    collected_nodeids,
    collection_failures,
    executed_count,
    executed_nodeids,
    verdict,
)
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
    assert '"quarantine"' not in gate
    assert "run: scripts/ci.sh test-python-quarantine" in workflow
    assert "run: scripts/ci.sh lint-quarantine" in workflow


def test_ci_gate_needs_list_agrees_with_its_own_script_gates() -> None:
    # Both halves of the aggregate live in one file: the YAML `needs:` list
    # decides which jobs ci-gate waits for, and the embedded script's
    # hard_gates / conditional_gates lists decide which results it judges. A
    # job present in one but not the other is either an unjudged wait (green
    # regardless of its result) or a KeyError at gate time. Deriving both
    # sides from the same source keeps this test from pinning a stale copy of
    # production text, and makes the agreement itself the assertion.
    import ast

    import yaml

    jobs = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]
    gate = jobs["ci-gate"]
    needs = set(gate["needs"])

    script = gate["steps"][0]["run"]
    body = script.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    consts: dict[str, object] = {}
    for node in ast.walk(ast.parse(body)):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in ("hard_gates", "conditional_gates")
        ):
            consts[node.targets[0].id] = ast.literal_eval(node.value)

    hard = set(consts["hard_gates"])
    conditional = set(consts["conditional_gates"])

    assert hard | conditional == needs
    assert not (hard & conditional)
    assert "quarantine" not in needs
    assert "performance" not in needs
    # Every conditional job's filter flag must be an output the changes job
    # actually declares, or the gate's else-branch fails it unconditionally.
    declared_outputs = set(jobs["changes"]["outputs"])
    assert set(consts["conditional_gates"].values()) <= declared_outputs


# Jobs this repository deliberately does not gate on, each for a stated reason.
# This set is the POLICY, and it is the one thing here that is written down
# rather than derived: adding a job to it is a decision to let that job fail
# without blocking a merge. Everything else defined in ci.yml must be judged.
_UNGATED_JOBS = {
    # Advisory lanes, documented as such in ci.yml.
    "performance",
    "quarantine",
    # Not a gate subject: publish runs only on main after merge, and ci-gate
    # is the aggregate itself.
    "publish",
    "ci-gate",
}


def test_every_job_defined_in_the_workflow_is_gated_or_explicitly_ungated() -> None:
    # The agreement test above compares ci-gate's two lists AGAINST EACH
    # OTHER, so it holds just as well when a job is dropped from both at
    # once — and a job dropped from both is one the aggregate neither waits
    # for nor judges, which is the exact escape the aggregate exists to
    # prevent. The population is taken here from the workflow's own job
    # DEFINITIONS instead, a different part of the file from the lists under
    # test, so a synchronized deletion leaves the job defined, ungated, and
    # unlisted below — and fails.
    import yaml

    jobs = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]
    gated = set(jobs["ci-gate"]["needs"])
    defined = set(jobs)

    ungated = defined - gated - _UNGATED_JOBS
    assert not ungated, (
        f"jobs defined in ci.yml but neither gated by ci-gate nor listed as "
        f"deliberately ungated: {sorted(ungated)}"
    )
    # The exclusions must still exist; a stale name here would silently widen
    # the allowance for whatever job is added under it later.
    assert _UNGATED_JOBS <= defined


def test_ci_gate_runs_and_reads_its_inputs_regardless_of_dependency_results() -> None:
    # Two wirings outside the script body decide whether the script ever sees
    # the truth. Without `always()` a failed dependency SKIPS ci-gate, and a
    # skipped required check satisfies branch protection — a green that never
    # ran. Without the toJSON(needs) binding the script judges something other
    # than this run's results. Neither is visible in the script's own text.
    import yaml

    gate = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]["ci-gate"]

    assert gate["if"].strip() == "${{ always() }}"
    assert gate["steps"][0]["env"]["NEEDS_CONTEXT"].strip() == "${{ toJSON(needs) }}"


def test_the_studio_filter_covers_every_input_to_the_studio_image() -> None:
    # studio-docker may skip when the studio filter reports no image-relevant
    # change, so a build input the filter does not match is a path to a green
    # gate over an image that would not build. The inputs are derived from the
    # Dockerfile itself rather than restated here, plus the context-control
    # file, which is an input precisely because it decides what the build
    # context contains: a root .dockerignore excluding a COPY source makes the
    # build fail while matching none of the source paths.
    import re

    import yaml

    jobs = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]
    filters = yaml.safe_load(jobs["changes"]["steps"][1]["with"]["filters"])
    studio_rules = filters["studio"]

    def covered(path: str) -> bool:
        for rule in studio_rules:
            if rule == path:
                return True
            if rule.endswith("/**") and path.startswith(rule[:-2]):
                return True
        return False

    dockerfile = (REPO_ROOT / "apps/studio/Dockerfile").read_text()
    sources: list[str] = []
    for line in dockerfile.splitlines():
        m = re.match(r"^COPY\s+(?!--from)(.+)$", line.strip())
        if not m:
            continue
        # Everything but the destination; a trailing glob is not a path.
        parts = m.group(1).split()[:-1]
        sources.extend(p for p in parts if not p.endswith("*"))

    assert sources, "no COPY sources parsed from the Studio Dockerfile"
    uncovered = [s for s in sources if not covered(s)]
    assert not uncovered, f"Studio image inputs not matched by the studio filter: {uncovered}"

    # Not a COPY source, and the reason it must be listed anyway: it changes
    # what every COPY source resolves to.
    assert covered(".dockerignore"), (
        "a root .dockerignore is a Studio image input — it decides what the "
        "build context contains — and must trigger the studio filter"
    )


def _extract_gate_parts() -> tuple[str, list[str], dict[str, str]]:
    import ast

    import yaml

    gate = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]["ci-gate"]
    script = gate["steps"][0]["run"]
    body = script.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    consts: dict[str, object] = {}
    for node in ast.walk(ast.parse(body)):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in ("hard_gates", "conditional_gates")
        ):
            consts[node.targets[0].id] = ast.literal_eval(node.value)
    return body, list(consts["hard_gates"]), dict(consts["conditional_gates"])


def test_ci_gate_script_judges_by_every_list_entry() -> None:
    # The agreement test proves the lists exist and match needs; this proves
    # the verdict path CONSUMES them. A refactor that keeps the lists as
    # data but stops iterating one of them would leave every other assertion
    # green while the gate judges nothing — here, each entry individually
    # flipped to a failing result must flip the verdict, executing the same
    # script body the workflow runs.
    import json
    import os
    import subprocess
    import sys

    body, hard_gates, conditional_gates = _extract_gate_parts()

    def run_gate(ctx: dict) -> int:
        return subprocess.run(
            [sys.executable, "-c", body],
            env={**os.environ, "NEEDS_CONTEXT": json.dumps(ctx)},
            capture_output=True,
        ).returncode

    def green_context() -> dict:
        ctx: dict = {name: {"result": "success"} for name in hard_gates}
        ctx["changes"]["outputs"] = {flag: "true" for flag in conditional_gates.values()}
        for job in conditional_gates:
            ctx[job] = {"result": "success"}
        return ctx

    assert run_gate(green_context()) == 0

    for name in hard_gates:
        ctx = green_context()
        ctx[name]["result"] = "failure"
        assert run_gate(ctx) == 1, f"gate ignored failing hard gate {name!r}"

    for job in conditional_gates:
        ctx = green_context()
        ctx[job]["result"] = "skipped"
        assert run_gate(ctx) == 1, f"gate ignored {job!r} skipping while its inputs changed"


def test_required_ci_wrapper_excludes_only_performance_and_quarantine() -> None:
    script = CI_SCRIPT.read_text()

    assert "not performance and not flaky_quarantine" in script
    assert "pytest-rerunfailures" not in script


def _wrapper_functions() -> dict[str, str]:
    """Return each shell function in scripts/ci.sh by name, body included."""
    bodies: dict[str, str] = {}
    name: str | None = None
    lines: list[str] = []
    for line in CI_SCRIPT.read_text().splitlines():
        opened = re.match(r"^([A-Za-z0-9_-]+)\(\) \{$", line)
        if opened:
            name = opened.group(1)
            lines = []
        elif name and line == "}":
            bodies[name] = "\n".join(lines)
            name = None
        elif name:
            lines.append(line)
    return bodies


def test_every_suite_lane_replaces_a_crashed_worker_and_checks_its_own_coverage() -> None:
    # Enumerated over the wrapper's own suite lanes rather than asserted once,
    # so a lane added later without either half fails here instead of quietly
    # running a partial suite and reporting it as a whole one.
    functions = _wrapper_functions()
    lanes = {name: body for name, body in functions.items() if name.startswith("test-python")}

    assert len(lanes) >= 3, f"parsed {sorted(functions)} out of the wrapper; it has no suite lanes"
    for name, body in lanes.items():
        assert "uv run pytest" in body, f"{name} is not a pytest lane"
        # An explicit flag so MAX_WORKER_RESTART can change the limit for a job
        # that needs the other trade; an override of the default asserted below.
        assert '--max-worker-restart="${MAX_WORKER_RESTART:-4}"' in body, name
        coverage_lines = [
            line.strip() for line in body.splitlines() if "_check_suite_coverage " in line
        ]
        assert coverage_lines, name
        assert all(line.endswith("|| rc=1") for line in coverage_lines), name


def test_every_suite_lane_keeps_the_project_asyncio_mode() -> None:
    # pyproject.toml runs pytest-asyncio in strict mode so that a test written
    # for anyio runs on each backend it is parametrized with. A lane that passes
    # --asyncio-mode on the command line overrides that, and in auto mode every
    # trio case runs on asyncio while its test id still names trio.
    functions = _wrapper_functions()
    lanes = {name: body for name, body in functions.items() if name.startswith("test-python")}

    assert len(lanes) >= 3, f"parsed {sorted(functions)} out of the wrapper; it has no suite lanes"
    for name, body in lanes.items():
        assert "uv run pytest" in body, f"{name} is not a pytest lane"
        assert "--asyncio-mode" not in body, name
    for workflow in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        assert "--asyncio-mode" not in workflow.read_text(encoding="utf-8"), workflow.name


def test_project_config_allows_xdist_worker_restarts(
    request: pytest.FixtureRequest,
) -> None:
    # Reads the effective ini configuration rather than the text of any one
    # file, since a caller who never runs scripts/ci.sh (a hand-typed `pytest
    # tests/`, an editor's runner) inherits addopts and nothing else -- so
    # asserting the flag's presence in the wrapper proves nothing about that
    # caller. This covers every ordinary invocation from the repository; a
    # caller that selects a different config with `pytest -c <file>` is
    # protected only if that file sets the flag itself.
    #
    # The limit must not be 0. At 0, xdist shuts every worker down at the first
    # crash, and the run then reports the counts it had reached as if they were
    # the whole suite: measured on this suite, one crashed worker left 9681 of
    # 23822 collected tests unrun. At 4 the measured crash was replaced, but
    # the restart limit does not guarantee the run reaches every test; the
    # suite check covers that case.
    flags = [
        opt for opt in request.config.getini("addopts") if opt.startswith("--max-worker-restart")
    ]

    assert len(flags) == 1, f"addopts carries {flags} rather than exactly one restart limit"
    assert int(flags[0].split("=", 1)[1]) > 0


def test_a_run_that_left_tests_unrun_is_refused_and_names_both_numbers() -> None:
    collected = _coverage_identities(23822)
    executed = set(sorted(collected)[:14141])
    code, line = verdict(collected=collected, executed=executed, failures=[])

    assert code == 1
    assert "23822" in line and "14141" in line and "9681" in line

    code, line = verdict(collected=collected, executed=collected, failures=[])

    assert code == 0
    assert "23822 of 23822" in line


def _coverage_identities(count: int) -> set[tuple[str, str, str]]:
    return {
        ("tests/test_sample.py", "tests.test_sample", f"test_case_{index}")
        for index in range(count)
    }


def _write_junit_cases(tmp_path: Path, cases: list[tuple[str, str, str, bool]]) -> Path:
    body = []
    for file, classname, name, skipped in cases:
        result = "<skipped />" if skipped else ""
        body.append(
            f'<testcase file="{file}" classname="{classname}" name="{name}">{result}</testcase>'
        )
    report = tmp_path / "junit.xml"
    report.write_text(
        f'<testsuites><testsuite name="pytest" tests="{len(cases)}">'
        f"{''.join(body)}</testsuite></testsuites>"
    )
    return report


def test_module_skips_cannot_cover_tests_stopped_by_maxfail(tmp_path: Path) -> None:
    collected = _coverage_identities(5)
    cases = [(*next(iter(sorted(collected))), False)]
    cases.extend(
        (f"tests/skip_{index}.py", "pytest", f"skip_{index}.py", True) for index in range(5)
    )
    executed = executed_nodeids(_write_junit_cases(tmp_path, cases))
    code, line = verdict(collected=collected, executed=executed, failures=[])

    assert code == 1
    assert "4 never ran" in line


def test_repeated_worker_failures_do_not_count_as_distinct_tests(tmp_path: Path) -> None:
    collected = _coverage_identities(9)
    repeated = next(iter(sorted(collected)))
    cases = [(*repeated, False)] * 5
    executed = executed_nodeids(_write_junit_cases(tmp_path, cases))
    code, line = verdict(collected=collected, executed=executed, failures=[])

    assert code == 1
    assert "8 never ran" in line
    assert "test_case_1" in line or "test_case_0" in line


def test_duplicate_worker_reports_do_not_refuse_a_fully_covered_run(tmp_path: Path) -> None:
    collected = _coverage_identities(3)
    cases = [(*identity, False) for identity in sorted(collected)]
    cases.extend([(*cases[0][:3], False), (*cases[0][:3], False)])
    executed = executed_nodeids(_write_junit_cases(tmp_path, cases))
    code, line = verdict(collected=collected, executed=executed, failures=[])

    assert code == 0
    assert "3 of 3" in line


def test_a_collection_error_is_refused_and_named_rather_than_read_as_a_surplus() -> None:
    # A module that cannot be imported contributes one junit entry and no
    # collected tests, so that entry cannot satisfy any collected test ID.
    collected = _coverage_identities(10)
    code, line = verdict(collected=collected, executed=collected, failures=["tests/needs_numpy.py"])

    assert code == 1
    assert "tests/needs_numpy.py" in line
    assert "more than were collected" not in line


def test_a_collection_error_cannot_cancel_a_truncation_out() -> None:
    # Collection failures and missing test IDs are reported independently,
    # because one needs an environment fix and the other needs test failures read.
    collected = _coverage_identities(100)
    executed = set(sorted(collected)[:98])
    code, line = verdict(
        collected=collected,
        executed=executed,
        failures=["tests/a.py", "tests/b.py"],
    )

    assert code == 1
    assert "tests/a.py" in line and "tests/b.py" in line
    assert "98 of 100" in line


def test_verdict_will_not_judge_without_the_collection_failures_it_cannot_infer() -> None:
    # No default: the whole defect was a caller that never supplied this, and a
    # default would let the next one arrive at the old answer in silence.
    with pytest.raises(TypeError):
        verdict(collected=set(), executed=set())  # type: ignore[call-arg]


def test_collection_failures_are_read_off_the_report_and_exclude_fixture_errors(
    tmp_path: Path,
) -> None:
    # Both entries carry an <error>; only one of them means a module yielded no
    # tests. Counting the suite's `errors` attribute would take both.
    report = tmp_path / "junit.xml"
    report.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="2" failures="0" skipped="0" tests="2">
<testcase classname="" name="needs_numpy" file="tests/needs_numpy.py">
  <error message="collection failure">ModuleNotFoundError</error>
</testcase>
<testcase classname="tests.test_thing" name="test_one" file="tests/test_thing.py">
  <error message="failed on setup with &quot;RuntimeError&quot;">boom</error>
</testcase>
</testsuite></testsuites>"""
    )

    assert collection_failures(report) == ["tests/needs_numpy.py"]


def test_a_junit_report_the_check_cannot_read_refuses_rather_than_certifying(
    tmp_path: Path,
) -> None:
    # The check decides whether a gate passes, so every way of failing to read
    # its input has to raise rather than return a number that happens to match.
    with pytest.raises(CoverageCheckError):
        executed_count(tmp_path / "absent.xml")

    no_suite = tmp_path / "no_suite.xml"
    no_suite.write_text("<testsuites></testsuites>")
    with pytest.raises(CoverageCheckError):
        executed_count(no_suite)

    no_attribute = tmp_path / "no_attribute.xml"
    no_attribute.write_text('<testsuites><testsuite name="a"></testsuite></testsuites>')
    with pytest.raises(CoverageCheckError):
        executed_count(no_attribute)

    two_suites = tmp_path / "two.xml"
    two_suites.write_text(
        '<testsuites><testsuite name="a" tests="3">'
        '<testcase file="a.py" classname="a" name="test_1"/>'
        '<testcase file="a.py" classname="a" name="test_2"/>'
        '<testcase file="a.py" classname="a" name="test_3"/></testsuite>'
        '<testsuite name="b" tests="4">'
        '<testcase file="b.py" classname="b" name="test_1"/>'
        '<testcase file="b.py" classname="b" name="test_2"/>'
        '<testcase file="b.py" classname="b" name="test_3"/>'
        '<testcase file="b.py" classname="b" name="test_4"/></testsuite></testsuites>'
    )
    assert executed_count(two_suites) == 7


def test_a_collection_that_finds_nothing_refuses_to_certify_a_run(tmp_path: Path) -> None:
    # An empty denominator would make every run look fully covered, which is
    # the failure this check exists to catch, so it is an error and not a zero.
    (tmp_path / "test_one.py").write_text("def test_a():\n    pass\n")

    assert collected_count("not performance", [str(tmp_path)]) == 1

    with pytest.raises(CoverageCheckError):
        collected_count("performance", [str(tmp_path)])


def test_collected_ids_match_junit_address_fields(tmp_path: Path) -> None:
    (tmp_path / "test_sample.py").write_text(
        "import pytest\n"
        "class TestGroup:\n"
        "    @pytest.mark.parametrize('value', [1, 2], ids=['first', 'second'])\n"
        "    def test_values(self, value):\n"
        "        assert value in (1, 2)\n"
    )

    nodeids = collected_nodeids("not performance", [str(tmp_path)])
    file = next(iter(nodeids))[0]
    module = file.replace(os.sep, ".").removesuffix(".py")

    assert nodeids == {
        (file, f"{module}.TestGroup", "test_values[first]"),
        (file, f"{module}.TestGroup", "test_values[second]"),
    }


def test_docs_job_always_guard_survives_upstream_changes_failure() -> None:
    # Regression: `docs` gained `needs: changes` (so its ADR-check steps can
    # read `needs.changes.outputs.adr`) with no job-level `if:`. Under GitHub
    # Actions default semantics, a job with `needs:` and no `if:` runs only
    # when the needed job succeeds -- so a failed, skipped, or cancelled
    # `changes` silently skipped `docs`, including its documentation contract,
    # strict build, markdownlint and link-check steps, none of which read that
    # output. The job-level guard must therefore NOT depend on the `changes`
    # job's result; only the ADR steps are output-gated.
    workflow = CI_WORKFLOW.read_text()
    docs_job = workflow.split("  docs:", 1)[1].split("  test:", 1)[0]

    assert "needs: changes" in docs_job
    assert "if: ${{ !cancelled() }}" in docs_job
    assert "needs.changes.result" not in docs_job
    # The ADR steps remain gated on the output, so they skip (rather than
    # fail) when `changes` produced no output.
    assert docs_job.count("if: needs.changes.outputs.adr == 'true'") >= 1


def test_no_checkout_leaves_the_token_in_git_config() -> None:
    # Most jobs install and run the pull request's own code. actions/checkout
    # writes the job token into .git/config unless told not to, where that code
    # could read it. GitHub resolves an action's owner and repository without
    # regard to case, so the match here does too.
    import yaml

    jobs = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]
    checkouts = [
        (name, step)
        for name, job in jobs.items()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).lower().startswith("actions/checkout@")
    ]

    assert {name for name, _ in checkouts} >= {"docs", "test", "lint"}
    persisting = [
        name
        for name, step in checkouts
        if (step.get("with") or {}).get("persist-credentials") is not False
    ]
    assert persisting == [], f"checkouts that persist the token: {persisting}"


def test_every_required_ci_exclusion_has_a_workflow_lane() -> None:
    workflow = CI_WORKFLOW.read_text()
    performance_job = workflow.split("  performance:", 1)[1].split("  quarantine:", 1)[0]

    assert 'uv run pytest -m "performance"' in performance_job
    assert "continue-on-error" not in performance_job
    assert "run: scripts/ci.sh test-python-quarantine" in workflow


def test_every_job_has_a_timeout_and_every_suite_step_stops_before_its_job() -> None:
    # Runs on a pushed branch share one concurrency group and are never
    # cancelled, so a single hung job holds that branch's CI for the six-hour
    # default. A suite step stops before its job so the failure telemetry and
    # the peak-RSS report still run inside the job's limit.
    import yaml

    jobs = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]
    assert {"test", "quarantine", "lint", "ci-gate"} <= set(jobs)
    unbounded = [name for name, job in jobs.items() if not job.get("timeout-minutes")]
    assert unbounded == [], f"jobs without timeout-minutes: {unbounded}"

    suite_steps = [
        (name, job["timeout-minutes"], step)
        for name, job in jobs.items()
        for step in job.get("steps", [])
        if re.search(r"scripts/ci\.sh test-python", str(step.get("run", "")))
    ]
    assert {name for name, _, _ in suite_steps} >= {"test", "quarantine"}
    late = [
        (name, step.get("name"))
        for name, job_limit, step in suite_steps
        if not 0 < step.get("timeout-minutes", 0) < job_limit
    ]
    assert late == [], f"suite steps that do not stop before their job: {late}"
