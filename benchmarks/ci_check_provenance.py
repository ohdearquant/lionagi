from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SUITES = ["concurrency-asyncio", "concurrency-trio", "ln-asyncio", "ln-trio", "fuzzy"]


def check(baseline_dir: Path, current_dir: Path, suites: list[str]) -> bool:
    """Return True iff, for every suite, (1) baseline and current used
    distinct lionagi installs, and (2) current covers every scenario
    baseline reported.

    (1) is only meaningful if the baseline run and the current run
    actually imported different code. A prior version of this job ran
    `python -m benchmarks.X` from the repo checkout root, whose cwd is
    prepended to sys.path by `-m` -- since the checkout root contains a
    `lionagi/` source directory, both venvs' `import lionagi` silently
    resolved there instead of to their own site-packages install, and the
    gate compared identical code against itself. Every result JSON now
    records lionagi.__file__ (benchmarks/_compat.py:lionagi_provenance).

    (2) guards a different failure mode of the same soft-skip mechanism:
    each bench script drops a scenario from its results if a symbol it
    needs is missing from whichever lionagi install ran it
    (benchmarks/_compat.py:soft_import). That is intentional when the
    OLDER baseline predates a brand-new symbol the PR adds -- current has
    a scenario baseline doesn't, which is fine. It is a bug report,
    silently swallowed, when the NEWER current install is missing a
    symbol the OLDER baseline still has: something in the PR broke or
    removed an API that a benchmark scenario depends on, and
    ci_compare.py only iterates current's results, so it would never
    notice the scenario went missing. Any scenario present in baseline
    but absent from current fails this check.
    """
    ok = True
    for suite in suites:
        b_path = baseline_dir / f"{suite}.json"
        c_path = current_dir / f"{suite}.json"
        if not b_path.exists() or not c_path.exists():
            print(
                f"[provenance] {suite}: result file missing ({b_path} / {c_path})",
                file=sys.stderr,
            )
            ok = False
            continue
        baseline = json.loads(b_path.read_text(encoding="utf-8"))
        current = json.loads(c_path.read_text(encoding="utf-8"))

        b_file = baseline.get("meta", {}).get("lionagi_file")
        c_file = current.get("meta", {}).get("lionagi_file")
        if not b_file or not c_file:
            print(
                f"[provenance] {suite}: missing lionagi_file in result metadata",
                file=sys.stderr,
            )
            ok = False
        elif b_file == c_file:
            print(
                f"[provenance] {suite}: baseline and current both imported lionagi "
                f"from the SAME path ({b_file}). The A/B comparison measured "
                "identical code, not a regression signal -- a working directory is "
                "likely shadowing one venv's install "
                "(see benchmarks/_compat.py:lionagi_provenance).",
                file=sys.stderr,
            )
            ok = False
        else:
            print(f"[provenance] {suite}: OK -- baseline={b_file} current={c_file}")

        b_scenarios = set(baseline.get("results", {}).keys())
        c_scenarios = set(current.get("results", {}).keys())
        dropped = b_scenarios - c_scenarios
        if dropped:
            print(
                f"[provenance] {suite}: scenario(s) present in baseline but MISSING "
                f"from current: {sorted(dropped)}. The current lionagi install is "
                "missing a symbol these scenarios need (see the soft_import warnings "
                "in this suite's run logs) -- this looks like a regression, not an "
                "expected new-API gap (which only ever appears the other way around: "
                "current has scenarios baseline lacks, never the reverse).",
                file=sys.stderr,
            )
            ok = False
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline-dir", required=True, type=Path)
    ap.add_argument("--current-dir", required=True, type=Path)
    ap.add_argument("--suites", nargs="+", default=SUITES)
    args = ap.parse_args()

    ok = check(args.baseline_dir, args.current_dir, args.suites)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
