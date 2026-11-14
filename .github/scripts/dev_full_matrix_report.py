"""Report a failed dev full-matrix run to the repository's tracking issue."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections.abc import Iterator

REPOSITORY = "ohdearquant/lionagi"
API_ROOT = f"repos/{REPOSITORY}"
LABEL = "dev-full-matrix-failure"
TITLE = "The dev Python test matrix failed"
PYTHON_VERSIONS = ("3.10", "3.11", "3.12", "3.13", "3.14")


def github(*arguments: str, data: dict | None = None) -> str:
    args = ["gh", "api", "--hostname", "github.com", *arguments]
    if data is not None:
        args.extend(("--input", "-"))
    result = subprocess.run(  # noqa: S603, S607 — fixed command, validated routes, no shell
        args,
        input=json.dumps(data) if data is not None else None,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"GitHub request failed: {result.stderr.strip()}")
    return result.stdout


def read_rows(route: str, *, key: str | None = None) -> Iterator[dict]:
    pages = json.loads(github("--paginate", "--slurp", route))
    for page in pages:
        rows = page if key is None else page[key]
        yield from rows


def report_failure(tested_sha: str, run_id: str, attempt: str) -> None:
    if tested_sha and not re.fullmatch(r"[0-9a-f]{40}", tested_sha):
        raise ValueError("expected a full dev commit SHA or an empty value")
    if not all(re.fullmatch(r"[1-9][0-9]*", value) for value in (run_id, attempt)):
        raise ValueError("expected a positive run ID and attempt number")

    jobs = read_rows(f"{API_ROOT}/actions/runs/{run_id}/attempts/{attempt}/jobs", key="jobs")
    failed_names = {
        job["name"] for job in jobs if job.get("conclusion") in {"failure", "timed_out"}
    }
    failed = [version for version in PYTHON_VERSIONS if f"Python {version}" in failed_names]
    details = (
        "Failing Python versions: " + ", ".join(failed) + "."
        if failed
        else "No Python version reported a failure. Inspect the workflow jobs for the failure."
    )
    url = f"https://github.com/{REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}"
    marker = f"<!-- dev-full-matrix: {run_id}/{attempt} -->"
    target = f"`{tested_sha}`" if tested_sha else "unavailable"
    body = f"Dev commit: {target}\n\n{details}\n\n[Workflow run]({url})\n\n{marker}"

    open_issues = read_rows(f"{API_ROOT}/issues?state=open&labels={LABEL}")
    existing = next((issue for issue in open_issues if "pull_request" not in issue), None)
    if existing is None:
        if not any(label["name"] == LABEL for label in read_rows(f"{API_ROOT}/labels")):
            github(
                "--method",
                "POST",
                f"{API_ROOT}/labels",
                data={
                    "name": LABEL,
                    "color": "b60205",
                    "description": "Failure in the scheduled dev Python test matrix",
                },
            )
        github(
            "--method",
            "POST",
            f"{API_ROOT}/issues",
            data={"title": TITLE, "body": body, "labels": [LABEL]},
        )
        return

    number = existing["number"]
    if type(number) is not int or number <= 0:
        raise ValueError("GitHub returned an invalid issue number")
    comments_route = f"{API_ROOT}/issues/{number}/comments"
    if marker in (existing.get("body") or "") or any(
        marker in (comment.get("body") or "") for comment in read_rows(comments_route)
    ):
        return
    github("--method", "POST", comments_route, data={"body": body})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tested_sha")
    parser.add_argument("run_id")
    parser.add_argument("attempt")
    args = parser.parse_args()
    report_failure(args.tested_sha, args.run_id, args.attempt)


if __name__ == "__main__":
    main()
