#!/usr/bin/env python3
"""Parse and enforce the bounded pytest quarantine manifest."""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "tests" / "quarantine.txt"
DEFAULT_MAX_ENTRIES = 15


class QuarantineError(ValueError):
    """Raised when the quarantine manifest violates its contract."""


@dataclass(frozen=True, order=True)
class QuarantineEntry:
    quarantined_on: date
    nodeid: str
    signature: str


def apply_quarantine_markers(
    items: Sequence[Any],
    entries: Sequence[QuarantineEntry],
    marker_factory: Callable[..., Any],
) -> None:
    """Apply a marker to collected items whose exact nodeids are quarantined."""

    quarantined = {entry.nodeid: entry for entry in entries}
    for item in items:
        entry = quarantined.get(item.nodeid)
        if entry is not None:
            item.add_marker(
                marker_factory(
                    reason=f"quarantined {entry.quarantined_on.isoformat()}: {entry.signature}"
                )
            )


def load_manifest(
    path: Path = DEFAULT_MANIFEST,
    *,
    validate_paths: bool = False,
) -> list[QuarantineEntry]:
    """Load ``date | nodeid | signature`` entries from *path*."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise QuarantineError(f"cannot read quarantine manifest {path}: {exc}") from exc

    entries: list[QuarantineEntry] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        parts = [part.strip() for part in line.split("|", 2)]
        if len(parts) != 3 or not all(parts):
            raise QuarantineError(
                f"{path}:{line_number}: expected 'YYYY-MM-DD | pytest nodeid | signature'"
            )

        raw_date, nodeid, signature = parts
        try:
            quarantined_on = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise QuarantineError(
                f"{path}:{line_number}: invalid quarantine date {raw_date!r}"
            ) from exc
        if quarantined_on.isoformat() != raw_date:
            raise QuarantineError(f"{path}:{line_number}: date must use the YYYY-MM-DD form")
        if not nodeid.startswith("tests/") or "::" not in nodeid:
            raise QuarantineError(
                f"{path}:{line_number}: nodeid must be an exact test under tests/"
            )
        if nodeid in seen:
            raise QuarantineError(f"{path}:{line_number}: duplicate nodeid {nodeid}")
        if validate_paths:
            test_path = REPO_ROOT / nodeid.split("::", 1)[0]
            if not test_path.is_file():
                raise QuarantineError(
                    f"{path}:{line_number}: test file does not exist: {test_path}"
                )

        seen.add(nodeid)
        entries.append(QuarantineEntry(quarantined_on, nodeid, signature))

    return entries


def enforce_cap(
    entries: Sequence[QuarantineEntry],
    *,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> None:
    """Fail with the oldest entries named when the quarantine exceeds its cap."""

    if len(entries) <= max_entries:
        return
    oldest = sorted(entries)[: min(5, len(entries))]
    details = "\n".join(
        f"  {entry.quarantined_on.isoformat()} | {entry.nodeid}" for entry in oldest
    )
    raise QuarantineError(
        f"quarantine has {len(entries)} entries; hard cap is {max_entries}. "
        f"Oldest entries:\n{details}"
    )


def validate_nodeids(entries: Sequence[QuarantineEntry]) -> None:
    """Fail unless every exact manifest nodeid is collectable by pytest."""

    if not entries:
        return
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-n0",
            "--disable-warnings",
            *(entry.nodeid for entry in entries),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        output = "\n".join((result.stdout + result.stderr).strip().splitlines()[-20:])
        raise QuarantineError(f"one or more exact nodeids do not collect:\n{output}")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "count"))
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--max-entries", type=int, default=DEFAULT_MAX_ENTRIES)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        entries = load_manifest(args.manifest, validate_paths=args.command == "check")
        if args.command == "check":
            enforce_cap(entries, max_entries=args.max_entries)
            validate_nodeids(entries)
    except QuarantineError as exc:
        print(f"quarantine error: {exc}", file=sys.stderr)
        return 1

    if args.command == "count":
        print(len(entries))
    else:
        print(f"quarantine: {len(entries)} entries (hard cap {args.max_entries})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
