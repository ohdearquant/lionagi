#!/usr/bin/env python3
"""Refuse a pytest run that executed fewer tests than it collected.

A run can end before it reaches every collected test and still print a summary
that reads like a whole suite: an xdist worker that crashes under
``--max-worker-restart=0`` shuts every other worker down, and ``--maxfail``
stops the session at its cap. Neither prints how much of the suite was left
unrun, so two heads compared on "N failed / M passed" can be compared over
different populations without either side saying so.

The JUnit report can contain repeated entries for one test after an xdist
worker crash, and collection-time skips can produce entries for tests pytest
did not collect. This check compares distinct test IDs from the report with
distinct IDs collected from the same targets and marker expression. Any
collected ID missing from the report makes the command exit nonzero.

A collection error is reported separately: its JUnit placeholder is not a test
ID, and the module contributes no IDs to the re-collection. The placeholder
therefore cannot count as a test result or hide a collection failure.

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
    """The check could not read a report or collection, so it refuses."""


#: The message pytest gives the ``<error>`` of a testcase that stands in for a
#: module it could not import. Fixture and teardown errors carry their own
#: wording, and they belong to a test that really ran, so only this one means a
#: module contributed no tests at all.
COLLECTION_FAILURE_MESSAGE = "collection failure"
JunitIdentity = tuple[str, str, str]


def _suites(junit: Path) -> list[ElementTree.Element]:
    try:
        root = ElementTree.parse(junit).getroot()
    except (OSError, ElementTree.ParseError) as error:
        raise CoverageCheckError(f"cannot read the junit report {junit}: {error}") from error
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        raise CoverageCheckError(f"the junit report {junit} holds no testsuite element")
    return suites


def _nodeid_identity(nodeid: str) -> JunitIdentity:
    """Return the JUnit identity pytest derives from a collected node ID."""
    address, bracket, parameters = nodeid.partition("[")
    names = address.split("::")
    names[0] = names[0].replace("/", ".")  # pytest node IDs always use "/"
    if names[0].endswith(".py"):
        names[0] = names[0][:-3]
    names[-1] += bracket + parameters
    return (nodeid.split("::", 1)[0], ".".join(names[:-1]), names[-1])


def executed_nodeids(junit: Path) -> set[JunitIdentity]:
    """Return distinct real test IDs represented in the JUnit report."""
    nodeids: set[JunitIdentity] = set()
    for suite in _suites(junit):
        if suite.get("tests") is None:
            raise CoverageCheckError(f"a testsuite in {junit} carries no tests attribute")
        for case in suite.iter("testcase"):
            if any(
                error.get("message") == COLLECTION_FAILURE_MESSAGE for error in case.iter("error")
            ):
                continue
            file = case.get("file")
            classname = case.get("classname")
            name = case.get("name")
            if file is None or classname is None or name is None:
                raise CoverageCheckError(f"a testcase in {junit} has no file, classname, or name")
            nodeids.add((file, classname, name))
    return nodeids


def executed_count(junit: Path) -> int:
    """Return the number of distinct test IDs represented in the JUnit report."""
    return len(executed_nodeids(junit))


def collection_failures(junit: Path) -> list[str]:
    """Name every module the report says could not be collected.

    Read from the report because a collection error has no collected test IDs.
    Names, not a number, because the remedy is per-module and a bare count
    sends the reader back to the log.
    """
    names = []
    for suite in _suites(junit):
        for case in suite.iter("testcase"):
            for error in case.iter("error"):
                if error.get("message") == COLLECTION_FAILURE_MESSAGE:
                    names.append(case.get("file") or case.get("name") or "<unnamed>")
    return sorted(names)


def collected_nodeids(markexpr: str, targets: list[str]) -> set[JunitIdentity]:
    """Collect the same targets and marker expression as distinct test IDs.

    Runs in-process with a plugin rather than parsing collection output, whose
    shape depends on the verbosity the caller's addopts happen to set.
    """
    import pytest

    class _Counter:
        nodeids: set[JunitIdentity] | None = None

        def pytest_collection_finish(self, session) -> None:  # noqa: ANN001
            self.nodeids = {_nodeid_identity(item.nodeid) for item in session.items}

    counter = _Counter()
    args = ["--collect-only", "-n0", "-p", "no:cacheprovider", "-m", markexpr, *targets]
    noise = io.StringIO()
    with redirect_stdout(noise):
        code = pytest.main(args, plugins=[counter])
    if counter.nodeids is None:
        raise CoverageCheckError(
            f"collection did not finish (pytest exit {code}); the last of its output was: "
            f"{noise.getvalue()[-400:]!r}"
        )
    if not counter.nodeids:
        raise CoverageCheckError(
            f"collected 0 tests from {' '.join(targets)} under -m {markexpr!r}: "
            "the check has no denominator, so it cannot certify the run"
        )
    return counter.nodeids


def collected_count(markexpr: str, targets: list[str]) -> int:
    """Return the number of distinct tests in the matching collection."""
    return len(collected_nodeids(markexpr, targets))


def verdict(
    collected: set[JunitIdentity], executed: set[JunitIdentity], failures: list[str]
) -> tuple[int, str]:
    """Return the exit code and the line that names which condition tripped.

    Two conditions, reported separately because their remedies are different. A
    module that failed to collect means a dependency is missing and nothing in
    it ran; a shortfall means the session ended early and there are real
    failures to read. A run can hit both, and each one alone is a refusal.

    Both sides are sets of pytest test IDs. Collection placeholders and
    repeated reports for a crashed worker therefore cannot make a missing test
    look covered.
    """
    ran = collected & executed
    missing = collected - ran
    reasons = []
    if failures:
        reasons.append(
            f"{len(failures)} module(s) failed to collect and contributed no tests "
            f"({', '.join(failures)}) -- a missing dependency, not a failure to read"
        )
    if missing:
        names = sorted(f"{classname}::{name}" for _, classname, name in missing)
        shown = ", ".join(names[:10])
        if len(names) > 10:
            shown += f", and {len(names) - 10} more"
        reasons.append(
            f"only {len(ran)} of {len(collected)} collected tests ran, {len(missing)} never "
            "ran -- the session ended early (a crashed xdist worker can shut it down, and "
            f"--maxfail stops it at its cap); missing test IDs: {shown}"
        )
    if reasons:
        return 1, "suite coverage: REFUSED -- " + "; and ".join(reasons)
    return 0, f"suite coverage: {len(ran)} of {len(collected)} collected tests ran"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", required=True, type=Path, help="the run's junit xml report")
    parser.add_argument("-m", "--markexpr", required=True, help="the run's marker expression")
    parser.add_argument("targets", nargs="+", help="the run's test targets")
    args = parser.parse_args(argv)
    try:
        executed = executed_nodeids(args.junit)
        failures = collection_failures(args.junit)
        collected = collected_nodeids(args.markexpr, args.targets)
    except CoverageCheckError as error:
        print(f"suite coverage: UNCHECKED -- {error}", file=sys.stderr)
        return 1
    code, line = verdict(collected, executed, failures)
    print(line, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
