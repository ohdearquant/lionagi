#!/usr/bin/env python3
"""Check changed data files before they are committed to the repository."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STREAM_FILE_PREFIXES = ("benchmarks/", "lion1/tests/fixtures/", "lionagi/testing/data/")
LARGE_JSON_EXCEPTIONS = frozenset(
    {
        "apps/studio/frontend/package-lock.json",
        "benchmarks/comparisons/benchmark_results_20251015_141023.json",
        "tests/contracts/data/cli.json",
        "tests/contracts/data/http.json",
    }
)
MAX_JSON_BYTES = 256 * 1024


class GitError(Exception):
    """Git could not supply the data required to perform the check."""


def _git(*args: str) -> bytes:
    try:
        result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=False)
    except OSError as exc:
        raise GitError("cannot run git") from exc
    if result.returncode:
        raise GitError("git command failed")
    return result.stdout


def _resolve_base(requested: str | None) -> str:
    if requested is not None:
        try:
            return (
                _git(
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    "--end-of-options",
                    f"{requested}^{{commit}}",
                )
                .decode("ascii")
                .strip()
            )
        except GitError as exc:
            raise GitError(f"cannot resolve base {requested!r}") from exc

    try:
        parents = _git("rev-list", "--parents", "-n", "1", "HEAD").split()
    except GitError as exc:
        raise GitError("cannot inspect HEAD to choose a base") from exc
    if len(parents) >= 3:
        return os.fsdecode(parents[1])

    for ref in ("origin/dev/lionagi", "origin/main"):
        try:
            return _git("merge-base", "HEAD", ref).decode("ascii").strip()
        except GitError:
            continue
    raise GitError("cannot resolve a base; tried origin/dev/lionagi and origin/main")


def _check(base: str) -> int:
    print(f"Base: {base}")
    try:
        if base == _git("rev-parse", "HEAD").decode("ascii").strip():
            print("Base is HEAD; the changed-path population is empty.")
        paths = _git("diff", "--name-only", "-z", "--diff-filter=ACMR", base, "HEAD").split(b"\0")
    except GitError:
        print("Changed paths: unavailable")
        raise
    if paths and not paths[-1]:
        paths.pop()
    print(f"Changed paths: {len(paths)}")
    failures = []
    for raw_path in paths:
        path = os.fsdecode(raw_path)
        lowered = path.lower()
        if lowered.endswith((".jsonl", ".ndjson")) and not path.startswith(STREAM_FILE_PREFIXES):
            failures.append(f"{path!r}: rule A (data file outside permitted locations)")
        if lowered.endswith(".json") and path not in LARGE_JSON_EXCEPTIONS:
            try:
                size = int(_git("cat-file", "-s", f"HEAD:{path}"))
            except ValueError as exc:
                raise GitError(f"cannot determine HEAD blob size for {path!r}") from exc
            if size > MAX_JSON_BYTES:
                failures.append(f"{path!r}: rule B (JSON file is {size} bytes)")
    for failure in failures:
        print(failure)
    return int(bool(failures))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="compare against this commit or ref")
    args = parser.parse_args()
    try:
        base = _resolve_base(args.base)
    except GitError as exc:
        print("Base: unresolved")
        print("Changed paths: unavailable")
        print(f"data-file check: {exc}", file=sys.stderr)
        return 2
    try:
        return _check(base)
    except GitError as exc:
        print(f"data-file check: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
