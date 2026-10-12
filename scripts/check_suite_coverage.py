#!/usr/bin/env python3
"""Refuse a pytest run that executed fewer tests than it collected.

A run can end before it reaches every collected test and still print a summary
that reads like a whole suite: an xdist worker that crashes under
``--max-worker-restart=0`` shuts every other worker down, and ``--maxfail``
stops the session at its cap. Neither prints how much of the suite was left
unrun, so two heads compared on "N failed / M passed" can be compared over
different populations without either side saying so.

The junit report counts the tests that actually produced a result. Collecting
the same targets under the same marker expression counts the tests that were
supposed to run. When those two numbers differ, the run covered less than it
claims and this script says so and exits nonzero.

A collection error is the second condition, and comparing those two numbers
cannot see it, because it moves BOTH of them in opposite directions: the module
contributes one junit testcase carrying a collection failure, and contributes
nothing at all to the re-collection. Compared raw, N collection errors would read
as a surplus of N, which looks like a crashed xdist worker that never happened.
They can also cancel a real truncation out: executed = collected - unrun +
errors, so a run that stopped at its --maxfail cap with as many collection
errors as tests it skipped would read exactly clean.

Collection errors are therefore counted off the report directly rather than
inferred from a difference, and the two conditions are reported separately
because their remedies are: an absent dependency is an environment fix, and a
truncated session means real failures need reading.
"""

from __future__ import annotations

import argparse
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from xml.etree import ElementTree


class CoverageCheckError(Exception):
    """The check could not read one of its two numbers, so it refuses."""


#: The message pytest gives the ``<error>`` of a testcase that stands in for a
#: module it could not import. Fixture and teardown errors carry their own
#: wording, and they belong to a test that really ran, so only this one means a
#: module contributed no tests at all.
COLLECTION_FAILURE_MESSAGE = "collection failure"


def _suites(junit: Path) -> list[ElementTree.Element]:
    try:
        root = ElementTree.parse(junit).getroot()
    except (OSError, ElementTree.ParseError) as error:
        raise CoverageCheckError(f"cannot read the junit report {junit}: {error}") from error
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        raise CoverageCheckError(f"the junit report {junit} holds no testsuite element")
    return suites


def executed_count(junit: Path) -> int:
    """Return the number of entries the junit report holds, results and all.

    This is the raw ``tests`` attribute, which counts a collection error as an
    entry. `collection_failures` is what separates the two.
    """
    total = 0
    for suite in _suites(junit):
        tests = suite.get("tests")
        if tests is None:
            raise CoverageCheckError(f"a testsuite in {junit} carries no tests attribute")
        total += int(tests)
    return total


def collection_failures(junit: Path) -> list[str]:
    """Name every module the report says could not be collected.

    Read off the report rather than derived from a count, because the count is
    exactly what a collection error corrupts. Names, not a number, because the
    remedy is per-module and a bare count sends the reader back to the log.
    """
    names = []
    for suite in _suites(junit):
        for case in suite.iter("testcase"):
            for error in case.iter("error"):
                if error.get("message") == COLLECTION_FAILURE_MESSAGE:
                    names.append(case.get("file") or case.get("name") or "<unnamed>")
    return sorted(names)


def collected_count(markexpr: str, targets: list[str]) -> int:
    """Collect the same targets under the same marker expression and count them.

    Runs in-process with a plugin rather than parsing collection output, whose
    shape depends on the verbosity the caller's addopts happen to set.
    """
    import pytest

    class _Counter:
        items = None

        def pytest_collection_finish(self, session) -> None:  # noqa: ANN001
            self.items = len(session.items)

    counter = _Counter()
    args = ["--collect-only", "-n0", "-p", "no:cacheprovider", "-m", markexpr, *targets]
    noise = io.StringIO()
    with redirect_stdout(noise):
        code = pytest.main(args, plugins=[counter])
    if counter.items is None:
        raise CoverageCheckError(
            f"collection did not finish (pytest exit {code}); the last of its output was: "
            f"{noise.getvalue()[-400:]!r}"
        )
    if counter.items == 0:
        raise CoverageCheckError(
            f"collected 0 tests from {' '.join(targets)} under -m {markexpr!r}: "
            "the check has no denominator, so it cannot certify the run"
        )
    return counter.items


def verdict(collected: int, executed: int, failures: list[str]) -> tuple[int, str]:
    """Return the exit code and the line that names which condition tripped.

    Two conditions, reported separately because their remedies are different. A
    module that failed to collect means a dependency is missing and nothing in
    it ran; a shortfall means the session ended early and there are real
    failures to read. A run can hit both, and each one alone is a refusal.

    ``executed`` counts every junit entry, so the collection-error stand-ins are
    subtracted before any comparison. Without that subtraction each one adds to
    the left side while removing from the right, so it would read as a surplus
    and pass.

    A run can still report MORE results than the collection holds: xdist records
    a result of its own for the test a crashed worker was running, on top of the
    ones the workers reported. That surplus is not a coverage hole, and the
    crash it comes from is already a failure in the same report, so it is said
    out loud and not refused.
    """
    results = executed - len(failures)
    reasons = []
    if failures:
        reasons.append(
            f"{len(failures)} module(s) failed to collect and contributed no tests "
            f"({', '.join(failures)}) -- a missing dependency, not a failure to read"
        )
    if results < collected:
        reasons.append(
            f"only {results} of {collected} collected tests ran, {collected - results} never "
            "ran -- the session ended early (a crashed xdist worker can shut it down, and "
            "--maxfail stops it at its cap), so there are real failures above to read"
        )
    if reasons:
        return 1, "suite coverage: REFUSED -- " + "; and ".join(reasons)
    if results > collected:
        surplus = results - collected
        return 0, (
            f"suite coverage: {results} results for {collected} collected tests, "
            f"{surplus} more than were collected -- xdist reports one of these for a "
            "worker that crashed while running a test"
        )
    return 0, f"suite coverage: {results} of {collected} collected tests ran"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", required=True, type=Path, help="the run's junit xml report")
    parser.add_argument("-m", "--markexpr", required=True, help="the run's marker expression")
    parser.add_argument("targets", nargs="+", help="the run's test targets")
    args = parser.parse_args(argv)
    try:
        executed = executed_count(args.junit)
        failures = collection_failures(args.junit)
        collected = collected_count(args.markexpr, args.targets)
    except CoverageCheckError as error:
        print(f"suite coverage: UNCHECKED -- {error}", file=sys.stderr)
        return 1
    code, line = verdict(collected, executed, failures)
    print(line, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
