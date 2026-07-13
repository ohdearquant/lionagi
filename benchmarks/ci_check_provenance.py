from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SUITES = ["concurrency-asyncio", "concurrency-trio", "ln-asyncio", "ln-trio", "fuzzy"]

# Suite -> required shared (non-lionagi) dependency keys; missing on either arm is a hard failure, not a skip.
REQUIRED_DEP_META_KEYS: dict[str, list[str]] = {
    "concurrency-asyncio": ["anyio"],
    "concurrency-trio": ["anyio"],
    "ln-asyncio": ["anyio"],
    "ln-trio": ["anyio"],
    "fuzzy": ["anyio", "orjson"],
}

# Interpreter identity (benchmarks/_compat.py:lionagi_provenance); must be identical across arms, always required.
PYTHON_IDENTITY_META_KEYS = ["python_full_version", "python_build", "python_compiler"]


def check(baseline_dir: Path, current_dir: Path, suites: list[str]) -> bool:
    """True iff every suite's arms show distinct lionagi installs, overlapping scenario
    coverage with nothing dropped from baseline, matching required dependency versions,
    and identical interpreter identity."""
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
        for dep in REQUIRED_DEP_META_KEYS.get(suite, ["anyio"]):
            b_version = b_meta.get(dep)
            c_version = c_meta.get(dep)
            if not b_version or not c_version:
                print(
                    f"[provenance] {suite}: {dep} is required for this suite but "
                    f"missing from result metadata -- baseline={b_version!r} "
                    f"current={c_version!r}. The dependency axis went unverified for "
                    "this suite instead of failing loud.",
                    file=sys.stderr,
                )
                ok = False
                continue
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
