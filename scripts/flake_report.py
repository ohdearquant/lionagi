#!/usr/bin/env python3
"""Summarize pytest flake artifacts over a CI run window."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

if __package__:
    from .quarantine import DEFAULT_MANIFEST, QuarantineError, load_manifest
else:
    from quarantine import DEFAULT_MANIFEST, QuarantineError, load_manifest


_REPOSITORY = "ohdearquant/lionagi"
_ARTIFACT_PREFIX = "flake-failures-"
_INCOMPLETE = 3
_RUN_ID = re.compile(r"[1-9][0-9]*")


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
            raise ValueError("expected a GitHub run object")
        run_id = row.get("databaseId") or row.get("run_id") or row.get("id")
        if not _RUN_ID.fullmatch(str(run_id)):
            raise ValueError("expected a positive GitHub run ID")
        run_ids.append(str(run_id))
    return list(dict.fromkeys(run_ids))


def _gh(*args: str) -> str:
    result = subprocess.run(  # noqa: S603, S607 — fixed executable and scoped GitHub routes
        ["gh", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        detail = result.stderr.strip().splitlines()
        raise ValueError(detail[-1] if detail else f"gh exited with status {result.returncode}")
    return result.stdout


@dataclass(frozen=True)
class _Artifact:
    id: int
    name: str
    expired: bool


def _list_gh_artifacts(run_id: str) -> list[_Artifact]:
    pages = json.loads(
        _gh(
            "api",
            "--hostname",
            "github.com",
            "--paginate",
            "--slurp",
            f"repos/{_REPOSITORY}/actions/runs/{run_id}/artifacts?per_page=100",
        )
    )
    if not isinstance(pages, list) or not pages:
        raise ValueError("malformed artifact listing")
    totals: set[int] = set()
    artifacts: dict[int, _Artifact] = {}
    for page in pages:
        if not isinstance(page, dict):
            raise ValueError("malformed artifact listing page")
        total, rows = page.get("total_count"), page.get("artifacts")
        if type(total) is not int or total < 0 or not isinstance(rows, list):
            raise ValueError("malformed artifact listing page")
        totals.add(total)
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("malformed artifact metadata")
            artifact_id, name, expired = row.get("id"), row.get("name"), row.get("expired")
            if (
                type(artifact_id) is not int
                or artifact_id <= 0
                or not isinstance(name, str)
                or not name
                or type(expired) is not bool
            ):
                raise ValueError("malformed artifact metadata")
            if artifact_id in artifacts:
                raise ValueError("duplicate artifact in listing")
            artifacts[artifact_id] = _Artifact(artifact_id, name, expired)
    if totals != {len(artifacts)}:
        raise ValueError("incomplete artifact listing")
    matching = [
        artifact for artifact in artifacts.values() if artifact.name.startswith(_ARTIFACT_PREFIX)
    ]
    if len({artifact.name for artifact in matching}) != len(matching):
        raise ValueError("ambiguous matching artifact names")
    return matching


def _artifact_records(
    run_id: str, artifact: _Artifact, destination: Path
) -> list[dict[str, object]]:
    if artifact.expired:
        raise ValueError("artifact has expired")
    artifact_dir = destination / run_id / str(artifact.id)
    _gh(
        "run",
        "download",
        run_id,
        "--repo",
        f"github.com/{_REPOSITORY}",
        "--name",
        artifact.name,
        "--dir",
        str(artifact_dir),
    )
    records: list[dict[str, object]] = []
    for line in (artifact_dir / "failures.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict) or not all(
            isinstance(record.get(key), str) and record[key] for key in ("nodeid", "signature")
        ):
            raise ValueError("malformed flake failure record")
        if str(record.get("run_id", run_id)) != run_id:
            raise ValueError("flake failure record names a different run")
        record.setdefault("run_id", run_id)
        records.append(record)
    if not records:
        raise ValueError("artifact contains no flake failure records")
    return records


def _collect_gh_records(
    run_ids: Iterable[str], destination: Path
) -> tuple[list[dict[str, object]], int, int]:
    records: list[dict[str, object]] = []
    artifacts_read = incomplete_runs = 0
    for run_id in run_ids:
        try:
            artifacts = _list_gh_artifacts(run_id)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            print(f"warning: run {run_id}: could not list artifacts: {exc}", file=sys.stderr)
            incomplete_runs += 1
            continue
        incomplete = False
        for artifact in artifacts:
            try:
                records.extend(_artifact_records(run_id, artifact, destination))
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                print(f"warning: run {run_id}, artifact {artifact.name!r}: {exc}", file=sys.stderr)
                incomplete = True
            else:
                artifacts_read += 1
        incomplete_runs += incomplete
    return records, artifacts_read, incomplete_runs


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
            remote_records, artifacts_read, incomplete = _collect_gh_records(
                run_ids, Path(temp_dir)
            )
        records = [*direct_records, *remote_records]
        if incomplete:
            print(
                f"Incomplete flake report: {incomplete} of {len(run_ids)} runs could not be read completely."
            )
        tests = {str(record["nodeid"]) for record in records if record.get("nodeid")}
        tests.discard("<pytest-session>")
        print(
            f"Runs requested: {len(run_ids)}; runs read: {len(run_ids) - incomplete}; "
            f"artifacts read: {artifacts_read}; distinct tests: {len(tests)}."
        )
        print(render_report(records, quarantined=quarantined))
    except (OSError, ValueError) as exc:
        print(f"flake report error: {exc}", file=sys.stderr)
        return 2
    return _INCOMPLETE if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
