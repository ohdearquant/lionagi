#!/usr/bin/env python3
"""Hold the core suite's dependence on lionhub to a list that may only shrink.

lionhub ships inside the lionagi wheel, and core still finds its providers through it, so part
of the core suite (``tests/``) fails when lionhub cannot be imported. That part is named, one
test module per line, in ``tests/hub_dependent_modules.txt``. A run of the core suite with
``-p scripts.hubless_pytest_plugin`` refuses every ``lionhub`` import and writes a report; this
script reads it and fails when:

- a module not on the list fails because lionhub was refused;
- a module on the list no longer does, so the list must lose it;
- the list gained a module compared with ``--base-list`` (the list before the change).

A failure counts as caused by lionhub when lionhub was refused while that test ran, or when the
refusal is in the failure's own text (an import error while collecting the module). Failures with
another cause are left to the ordinary test job.

``--control`` checks the check: taking a listed module off the list, and adding a module that
needs the hub but is not listed, must each produce a failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIST_PATH = ROOT / "tests" / "hub_dependent_modules.txt"

#: The package the run refuses, and the text every refusal carries in its error message.
REFUSED_ROOT = "lionhub"
REFUSAL_MARK = "refused: this run checks the core without lionhub"
#: Where the plugin writes its report, read from the environment of the pytest run.
REPORT_ENV = "LIONAGI_HUBLESS_REPORT"
#: The report property each test report carries: how many refusals happened while it ran.
REFUSALS_PROPERTY = "lionhub_refusals"
#: pytest exit codes that mean the run did not complete: interrupted, internal error, usage
#: error, no tests collected. Exit code 1 (tests failed) is expected here.
INCOMPLETE_EXIT_CODES = frozenset({2, 3, 4, 5})

_CONTROL_MODULE = "tests/__control__/test_needs_the_hub_and_is_not_listed.py"


def read_list(text: str) -> list[str]:
    """The module paths in a list file: one per line, ``#`` starts a comment.

    Raises ``ValueError`` on a duplicate, so one module cannot be counted twice.
    """
    modules: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line in modules:
            raise ValueError(f"line {number}: {line} is listed twice")
        modules.append(line)
    return modules


def hub_modules(report: dict) -> set[str]:
    """The test modules with a failure caused by the refused lionhub, from a plugin report.

    Raises ``ValueError`` when the report shows a run that did not complete or saw no test,
    since nothing can be concluded from it.
    """
    exit_code = report.get("exit_code")
    if exit_code in INCOMPLETE_EXIT_CODES:
        raise ValueError(f"the pytest run did not complete (exit code {exit_code})")
    modules = report.get("modules")
    if not isinstance(modules, dict) or not modules:
        raise ValueError("the report names no test module, so the run checked nothing")
    tests = sum(entry.get("tests", 0) for entry in modules.values())
    if tests <= 0:
        raise ValueError("the report counts no test, so the run checked nothing")
    return {name for name, entry in modules.items() if entry.get("hub_failures", 0) > 0}


def problems(needing: set[str], listed: list[str], base: list[str] | None = None) -> list[str]:
    """Every way the run and the list disagree, one sentence each; empty when they agree."""
    found: list[str] = []
    listed_set = set(listed)
    for module in sorted(needing - listed_set):
        found.append(f"{module} fails with lionhub refused and is not on the list")
    for module in sorted(listed_set - needing):
        found.append(f"{module} no longer needs lionhub: take it off the list")
    if base is not None:
        for module in sorted(listed_set - set(base)):
            found.append(f"{module} was added to the list, which may only shrink")
    return found


def control(needing: set[str], listed: list[str]) -> list[str]:
    """Run the comparison on two planted changes and return each one it failed to catch."""
    missed: list[str] = []
    taken_off = sorted(needing & set(listed))
    if taken_off:
        module = taken_off[0]
        shortened = [name for name in listed if name != module]
        if not any(module in line for line in problems(needing, shortened)):
            missed.append(f"taking {module} off the list was not caught")
    planted = needing | {_CONTROL_MODULE}
    if not any(_CONTROL_MODULE in line for line in problems(planted, listed)):
        missed.append("a module that needs the hub and is not listed was not caught")
    return missed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", type=Path, required=True, help="the plugin's JSON report")
    parser.add_argument("--list", type=Path, default=LIST_PATH, help="the list file")
    parser.add_argument(
        "--base-list", type=Path, default=None, help="the list file before the change"
    )
    parser.add_argument(
        "--control", action="store_true", help="also check that planted changes are caught"
    )
    args = parser.parse_args(argv)

    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        needing = hub_modules(report)
        listed = read_list(args.list.read_text(encoding="utf-8"))
        base = (
            read_list(args.base_list.read_text(encoding="utf-8"))
            if args.base_list is not None
            else None
        )
    except (OSError, ValueError) as exc:
        print(f"core-without-hub: cannot check: {exc}", file=sys.stderr)
        return 2

    tests = sum(entry.get("tests", 0) for entry in report["modules"].values())
    print(
        f"core-without-hub: {tests} tests in {len(report['modules'])} modules ran with "
        f"{REFUSED_ROOT} refused; {len(needing)} modules need it, {len(listed)} are listed."
    )
    if base is None:
        print("core-without-hub: no base list given, so growth of the list was not checked.")

    rc = 0
    found = problems(needing, listed, base)
    for line in found:
        print(f"  {line}")
    if found:
        rc = 1
        print("core-without-hub: the modules that need lionhub in this run:")
        for module in sorted(needing):
            print(f"    {module}")

    if args.control:
        missed = control(needing, listed)
        for line in missed:
            print(f"  control: {line}", file=sys.stderr)
        if missed:
            rc = 1
        else:
            print("core-without-hub: control passed, planted changes to the list are caught.")
    if rc == 0:
        print("core-without-hub: the list matches the run.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
