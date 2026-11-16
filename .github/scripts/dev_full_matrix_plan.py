"""Plan a full matrix run for one development commit."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO = "repos/ohdearquant/lionagi"
DEV_REF = "refs/heads/dev/lionagi"
SCHEDULED_RUNS = f"{REPO}/actions/workflows/dev-full-matrix.yml/runs?event=schedule&status=success"
CERTIFICATE = "Full dev matrix passed "
SHA = re.compile(r"[0-9a-f]{40}")
JsonReader = Callable[[str], dict[str, Any]]


def github_json(path: str) -> dict[str, Any]:
    response = subprocess.run(  # noqa: S603 — fixed command and repository routes, no shell
        ["gh", "api", "--hostname", "github.com", path],  # noqa: S607 — gh from the Actions runner
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if response.returncode:
        raise ValueError(f"GitHub request for {path} failed with exit {response.returncode}")
    result = json.loads(response.stdout)
    if not isinstance(result, dict):
        raise ValueError(f"GitHub response for {path} is not an object")
    return result


def _records(response: dict[str, Any], key: str) -> list[dict[str, Any]]:
    records = response.get(key)
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise ValueError(f"GitHub response has no valid {key} list")
    return records


def _previous_scheduled_run(run_id: int, read: JsonReader) -> int | None:
    # A rerun can encounter the record for its own earlier attempt.
    for page in (1, 2):
        runs = _records(read(f"{SCHEDULED_RUNS}&per_page=1&page={page}"), "workflow_runs")
        if not runs:
            return None
        run = runs[0]
        previous_id = run.get("id")
        if (
            type(previous_id) is not int
            or previous_id <= 0
            or run.get("event") != "schedule"
            or run.get("conclusion") != "success"
        ):
            raise ValueError("GitHub response does not identify a successful scheduled run")
        if previous_id != run_id:
            return previous_id
    return None


def _certifies(run_id: int, sha: str, read: JsonReader) -> bool:
    for page in range(1, 11):
        jobs = _records(
            read(f"{REPO}/actions/runs/{run_id}/jobs?filter=latest&per_page=100&page={page}"),
            "jobs",
        )
        if any(
            job.get("name") == CERTIFICATE + sha
            and job.get("status") == "completed"
            and job.get("conclusion") == "success"
            for job in jobs
        ):
            return True
        if len(jobs) < 100:
            return False
    raise ValueError("GitHub job history exceeds the pagination limit")


def dev_sha(read: JsonReader) -> str:
    ref = read(f"{REPO}/git/ref/heads/dev/lionagi")
    target = ref.get("object")
    if ref.get("ref") != DEV_REF or not isinstance(target, dict) or target.get("type") != "commit":
        raise ValueError("GitHub response does not identify the dev/lionagi commit")
    sha = target.get("sha")
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise ValueError("dev/lionagi does not resolve to a full commit SHA")
    return sha


def should_run(trigger: str, run_id: int, sha: str, read: JsonReader) -> bool:
    if trigger == "workflow_dispatch":
        return True
    previous_id = _previous_scheduled_run(run_id, read)
    return previous_id is None or not _certifies(previous_id, sha, read)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trigger", choices=("schedule", "workflow_dispatch"))
    parser.add_argument("run_id", type=int)
    args = parser.parse_args()
    try:
        if args.run_id < 1:
            raise ValueError("Expected a positive run ID")
        sha = dev_sha(github_json)
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            output.write(f"sha={sha}\n")
            output.flush()
            run = should_run(args.trigger, args.run_id, sha, github_json)
            output.write(f"run={str(run).lower()}\n")
    except (KeyError, OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Cannot plan the development matrix: {exc}\n")
    if run:
        print(f"Testing dev/lionagi at {sha}.")
    else:
        print(f"dev/lionagi at {sha} already passed the scheduled matrix. Skipping tests.")


if __name__ == "__main__":
    main()
