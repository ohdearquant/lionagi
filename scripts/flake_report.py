#!/usr/bin/env python3
"""Summarize pytest flake artifacts over a CI run window."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

if __package__:
    from .quarantine import DEFAULT_MANIFEST, QuarantineError, load_manifest
else:
    from quarantine import DEFAULT_MANIFEST, QuarantineError, load_manifest


def _walk_json(value: object) -> Iterator[dict[str, object]]:
    if isinstance(value, dict):
        if {"nodeid", "signature"}.issubset(value):
            yield value
            return
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _records_from_file(path: Path) -> Iterator[dict[str, object]]:
    if path.suffix == ".jsonl":
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            yield from _walk_json(value)
        return

    value = json.loads(path.read_text(encoding="utf-8"))
    yield from _walk_json(value)


def records_from_paths(paths: Iterable[Path]) -> Iterator[dict[str, object]]:
    for path in paths:
        files = sorted(path.rglob("*.json*")) if path.is_dir() else [path]
        for file_path in files:
            yield from _records_from_file(file_path)


def _read_gh_run_ids(path: str) -> list[str]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    value = json.loads(raw)
    rows = value if isinstance(value, list) else [value]
    run_ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        run_id = row.get("databaseId") or row.get("run_id") or row.get("id")
        if run_id is not None:
            run_ids.append(str(run_id))
    return list(dict.fromkeys(run_ids))


def _download_gh_artifacts(run_ids: Iterable[str], destination: Path) -> list[Path]:
    downloaded: list[Path] = []
    for run_id in run_ids:
        run_dir = destination / run_id
        result = subprocess.run(
            [
                "gh",
                "run",
                "download",
                run_id,
                "--pattern",
                "flake-failures-*",
                "--dir",
                str(run_dir),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            downloaded.append(run_dir)
        else:
            detail = result.stderr.strip().splitlines()
            reason = detail[-1] if detail else f"exit {result.returncode}"
            print(f"warning: run {run_id}: {reason}", file=sys.stderr)
    return downloaded


def render_report(
    records: Iterable[dict[str, object]],
    *,
    quarantined: set[str],
) -> str:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for record in records:
        nodeid = str(record.get("nodeid") or "<pytest-session>")
        grouped[nodeid].append(record)

    if not grouped:
        return "No flake failure records found."

    lines = [
        "failures  runs  status       legs         nodeid",
        "--------  ----  -----------  -----------  ------",
    ]
    for nodeid, failures in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])):
        status = "quarantined" if nodeid in quarantined else "NEW"
        legs = ",".join(sorted({str(item.get("matrix_leg", "?")) for item in failures}))
        run_ids = {str(item["run_id"]) for item in failures if item.get("run_id") is not None}
        run_count = str(len(run_ids)) if run_ids else "?"
        lines.append(f"{len(failures):8}  {run_count:4}  {status:11}  {legs:11}  {nodeid}")
        signatures = Counter(str(item.get("signature", "<missing>")) for item in failures)
        for signature, count in sorted(signatures.items(), key=lambda item: (-item[1], item[0])):
            signature_run_ids = {
                str(item["run_id"])
                for item in failures
                if item.get("signature", "<missing>") == signature
                and item.get("run_id") is not None
            }
            signature_runs = f", {len(signature_run_ids)} run(s)" if signature_run_ids else ""
            lines.append(f"          signature ({count} failure(s){signature_runs}): {signature}")
    return "\n".join(lines)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="*", type=Path, help="failure JSONL files/directories")
    parser.add_argument(
        "--gh-runs",
        metavar="PATH",
        help="JSON from `gh run list/view --json ...`; use - for stdin",
    )
    parser.add_argument("--quarantine", type=Path, default=DEFAULT_MANIFEST)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.inputs and not args.gh_runs:
        print("provide artifact paths or --gh-runs", file=sys.stderr)
        return 2

    try:
        quarantined = {entry.nodeid for entry in load_manifest(args.quarantine)}
    except QuarantineError as exc:
        print(f"quarantine error: {exc}", file=sys.stderr)
        return 2

    try:
        direct_records = list(records_from_paths(args.inputs))
        if not args.gh_runs:
            print(render_report(direct_records, quarantined=quarantined))
            return 0

        run_ids = _read_gh_run_ids(args.gh_runs)
        with tempfile.TemporaryDirectory(prefix="lionagi-flakes-") as temp_dir:
            downloaded = _download_gh_artifacts(run_ids, Path(temp_dir))
            records = [*direct_records, *records_from_paths(downloaded)]
            print(render_report(records, quarantined=quarantined))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"flake report error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
