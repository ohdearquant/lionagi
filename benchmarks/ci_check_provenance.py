from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SUITES = ["concurrency-asyncio", "concurrency-trio", "ln-asyncio", "ln-trio", "fuzzy"]

# Shared (non-lionagi) dependency versions recorded in each result JSON's
# meta. These must match across baseline and current: the baseline venv's
# install is now constrained to the current venv's resolved versions (see
# benchmarks.yml), so any drift here means that constraint mechanism broke.
SHARED_DEP_META_KEYS = ["anyio", "orjson"]

# Interpreter identity recorded in each result JSON's meta
# (benchmarks/_compat.py:lionagi_provenance). Unlike lionagi_file (must
# DIFFER) and python_executable (a venv-local path, expected to differ
# even on an identical interpreter binary), these describe the actual
# Python build running the benchmark and must be IDENTICAL across arms --
# always present, never suite-specific, so unlike SHARED_DEP_META_KEYS a
# missing value here is itself a failure, not something to skip.
PYTHON_IDENTITY_META_KEYS = ["python_full_version", "python_build", "python_compiler"]


def check(baseline_dir: Path, current_dir: Path, suites: list[str]) -> bool:
    """Return True iff, for every suite: (1) baseline and current used
    distinct lionagi installs, (2) baseline and current scenario sets
    overlap and current covers every scenario baseline reported,
    (3) shared dependency versions match across both arms, and (4) the
    two arms ran under the identical Python interpreter build.

    (1) is only meaningful if the baseline run and the current run
    actually imported different code. A prior version of this job ran
    `python -m benchmarks.X` from the repo checkout root, whose cwd is
    prepended to sys.path by `-m` -- since the checkout root contains a
    `lionagi/` source directory, both venvs' `import lionagi` silently
    resolved there instead of to their own site-packages install, and the
    gate compared identical code against itself. Every result JSON now
    records lionagi.__file__ (benchmarks/_compat.py:lionagi_provenance).

    (2) guards two failure modes of the same soft-skip mechanism: each
    bench script drops a scenario from its results if a symbol it needs is
    missing from whichever lionagi install ran it
    (benchmarks/_compat.py:soft_import).

    The partial case -- some scenarios missing, not all -- is intentional
    when the OLDER baseline predates a brand-new symbol the PR adds:
    current has a scenario baseline doesn't, which is fine. It is a bug
    report, silently swallowed, when the NEWER current install is missing
    a symbol the OLDER baseline still has: something in the PR broke or
    removed an API a benchmark scenario depends on, and ci_compare.py only
    iterates current's results, so it would never notice the scenario went
    missing. Any scenario present in baseline but absent from current
    fails this check.

    The total case -- one side's results object is entirely empty (e.g.
    the baseline install couldn't import the module at all) -- is a
    different, worse failure: with baseline empty, the partial-case diff
    above (`baseline_scenarios - current_scenarios`) is also empty, since
    baseline has nothing to be missing FROM current. ci_compare.py then
    finds no scenario has a matching baseline entry, skips all of them as
    "no baseline", and exits 0 -- the gate ran and reported success while
    comparing nothing. Any suite where baseline or current has zero
    scenarios, or where the two scenario sets share no overlap at all,
    fails this check.

    (3) guards the dependency axis of the same-machine A/B design: only
    the lionagi implementation should differ between baseline and
    current, not a transitive dependency's version. If a package like
    anyio or orjson released a newer version between the baseline commit
    and today, an unconstrained baseline install could pick it up while
    current stays pinned to what uv.lock resolved (or vice versa), and a
    compare delta could then reflect dependency drift instead of a
    lionagi change. benchmarks.yml constrains the baseline install to
    current's exact resolved versions; this is the check that the
    constraint actually held.

    (4) guards the interpreter itself, the one thing "same-machine A/B"
    assumes is truly shared: if the two venvs are created under different
    Python builds, every CPU-bound scenario can show a uniform,
    one-directional delta that has nothing to do with the code under
    test and survives even paired-in-time interleaving (drift-cancelling
    only helps when the underlying speed difference is noise, not a
    structural interpreter difference). This has actually happened here: a
    bare `uv venv` (no --python) silently honored this repo's committed
    .python-version instead of the CI matrix's Python version, putting one
    arm on a materially different interpreter than the other.
    benchmarks.yml now pins both venv creations to the literal
    $pythonLocation binary rather than a version string; this is the check
    that the pin actually held, comparing the fully-detailed version
    string plus build/compiler identity rather than just the short
    version -- two builds can report the same "3.12.13" while being
    differently optimized (e.g. PGO+LTO vs. not).
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
        overlap = b_scenarios & c_scenarios
        if not b_scenarios or not c_scenarios or not overlap:
            # A fourth variant of the same "gate silently becomes a no-op"
            # class as (1)/(2) above: if baseline's results object is empty
            # (e.g. the baseline install couldn't import the module at all,
            # so soft_import dropped every scenario in the suite), `dropped
            # = b_scenarios - c_scenarios` below is also empty -- there is
            # nothing to report as "missing from current" when baseline
            # never had anything to begin with. ci_compare.py then iterates
            # current's scenarios, finds none of them have a baseline entry,
            # skips all of them as "no baseline", and exits 0: the gate ran,
            # produced no signal, and reported success. Same failure shape
            # if current is the empty side, or if the two sets are simply
            # disjoint (no scenario name shared at all).
            which = (
                "baseline"
                if not b_scenarios
                else "current"
                if not c_scenarios
                else "baseline and current (disjoint sets)"
            )
            print(
                f"[provenance] {suite}: {which} reported zero overlapping scenarios "
                f"(baseline={sorted(b_scenarios)}, current={sorted(c_scenarios)}). "
                "ci_compare.py would silently skip every scenario as 'no baseline' and "
                "exit 0 -- the benchmark gate would be disabled for this suite instead "
                "of failing loud.",
                file=sys.stderr,
            )
            ok = False

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

        b_meta = baseline.get("meta", {})
        c_meta = current.get("meta", {})
        for dep in SHARED_DEP_META_KEYS:
            b_version = b_meta.get(dep)
            c_version = c_meta.get(dep)
            if not b_version or not c_version:
                continue  # not applicable to this suite (e.g. orjson outside fuzzy)
            if b_version != c_version:
                print(
                    f"[provenance] {suite}: {dep} version differs between arms -- "
                    f"baseline={b_version} current={c_version}. A compare delta for "
                    "this suite could reflect this dependency's version change "
                    "instead of a lionagi change; the baseline install's constraint "
                    "on current's resolved versions (see benchmarks.yml) did not "
                    "hold.",
                    file=sys.stderr,
                )
                ok = False

        for key in PYTHON_IDENTITY_META_KEYS:
            b_val = b_meta.get(key)
            c_val = c_meta.get(key)
            if not b_val or not c_val:
                print(
                    f"[provenance] {suite}: missing {key} in result metadata -- cannot "
                    "verify baseline and current ran under the same interpreter build.",
                    file=sys.stderr,
                )
                ok = False
                continue
            if b_val != c_val:
                print(
                    f"[provenance] {suite}: {key} differs between arms -- "
                    f"baseline={b_val!r} current={c_val!r}. Same-machine A/B requires "
                    "the exact same Python interpreter binary in both venvs -- only the "
                    "lionagi implementation should differ. A CPU-bound compare delta "
                    "here could reflect an interpreter/build difference (e.g. one arm's "
                    "venv silently resolved a different Python than the other) instead "
                    "of a lionagi change.",
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
