"""Resolve the released source shared by package and container publication."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 test environments.
    import tomli as tomllib

JsonReader = Callable[[str], dict[str, Any]]
_VERSION = re.compile(
    r"[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?(?:\.dev[0-9]+)?"
)
_SHA = re.compile(r"[0-9a-f]{40}")


class ReleaseError(ValueError):
    """Publication lacks a verified release or source binding."""


def _read_command(command: list[str]) -> dict[str, Any]:
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=60)
    if result.returncode:
        raise ReleaseError(f"release metadata request failed: {command[0]}")
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ReleaseError("release metadata is not an object")
    return value


def github_json(path: str) -> dict[str, Any]:
    return _read_command(["gh", "api", path])


def pypi_json(version: str) -> dict[str, Any]:
    suffix = f"/{version}" if version else ""
    return _read_command(
        [
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "--retry",
            "2",
            "--max-time",
            "30",
            f"https://pypi.org/pypi/lionagi{suffix}/json",
        ]
    )


def _pages(read: JsonReader, route: str, key: str):
    for page in range(1, 11):
        response = read(f"{route}&per_page=100&page={page}")
        items = response.get(key)
        if not isinstance(items, list):
            raise ReleaseError(f"GitHub response has no {key} list")
        yield from items
        if len(items) < 100:
            return
    raise ReleaseError(
        "release publication history exceeds the resolver's 1,000-item pagination budget"
    )


def _require_published_wheel(read: JsonReader, version: str) -> None:
    package = read(version)
    info = package.get("info")
    if (
        not isinstance(info, dict)
        or info.get("name") != "lionagi"
        or info.get("version") != version
    ):
        raise ReleaseError("PyPI package identity does not match the release version")
    files = package.get("urls")
    if not isinstance(files, list) or any(not isinstance(item, dict) for item in files):
        raise ReleaseError("PyPI did not report a wheel file list")
    if not any(
        item.get("packagetype") == "bdist_wheel" and item.get("yanked") is False for item in files
    ):
        raise ReleaseError("the release has no non-yanked wheel on PyPI")


def _require_publish_evidence(read: JsonReader, repo: str, tag: str, sha: str) -> None:
    # A release's target_commitish may be a moving branch. The Actions run
    # records the commit that actually ran the wheel upload, even if a tag moves.
    runs = _pages(
        read,
        f"repos/{repo}/actions/workflows/release.yml/runs?event=release&head_sha={sha}",
        "workflow_runs",
    )
    for run in runs:
        if (
            run.get("event") != "release"
            or run.get("head_sha") != sha
            or run.get("head_branch") != tag
            or run.get("path") != ".github/workflows/release.yml"
        ):
            continue
        run_id = run.get("id")
        if not isinstance(run_id, int) or isinstance(run_id, bool):
            raise ReleaseError("release workflow run has no numeric id")
        # A later retry may fail because PyPI already has the wheel. Retain the
        # successful upload from an earlier attempt of that same run.
        jobs = _pages(read, f"repos/{repo}/actions/runs/{run_id}/jobs?filter=all", "jobs")
        for job in jobs:
            if (
                job.get("name") != "deploy"
                or job.get("head_sha") != sha
                or job.get("status") != "completed"
                or job.get("conclusion") != "success"
            ):
                continue
            if any(
                step.get("name") == "Publish package to PyPI"
                and step.get("status") == "completed"
                and step.get("conclusion") == "success"
                for step in job.get("steps", [])
            ):
                return
    raise ReleaseError("no successful release workflow wheel upload binds this tag to its commit")


def resolve(
    env: Mapping[str, str],
    read_github: JsonReader = github_json,
    read_pypi: JsonReader = pypi_json,
    *,
    prepare_image: bool = False,
) -> dict[str, str]:
    event = env.get("GITHUB_EVENT_NAME")
    repo = env.get("GITHUB_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ReleaseError("invalid GitHub repository")
    if event == "workflow_dispatch":
        default_branch = env.get("DEFAULT_BRANCH")
        if not default_branch or env.get("GITHUB_REF") != f"refs/heads/{default_branch}":
            raise ReleaseError("manual image publication must run from the default branch")
        version = env.get("INPUT_VERSION", "")
        tag = f"v{version}"
    elif event == "release":
        tag = env.get("RELEASE_TAG", "")
        version = tag[1:] if tag.startswith("v") else ""
        if env.get("RELEASE_ACTION") != "published" or env.get("GITHUB_REF") != f"refs/tags/{tag}":
            raise ReleaseError("expected a published release at its tag ref")
    else:
        raise ReleaseError("unsupported release event")
    if not _VERSION.fullmatch(version):
        raise ReleaseError(
            "version must be a numeric x.y.z release, optionally with a/b/rc/post/dev suffix"
        )

    release = read_github(f"repos/{repo}/releases/tags/{tag}")
    if (
        release.get("tag_name") != tag
        or release.get("draft") is not False
        or not release.get("published_at")
    ):
        raise ReleaseError("version does not name a published GitHub release")
    if event == "release" and str(release.get("id")) != env.get("RELEASE_ID"):
        raise ReleaseError("published release identity changed")
    sha = read_github(f"repos/{repo}/commits/{tag}").get("sha", "")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise ReleaseError("release tag does not resolve to a full commit SHA")
    if event == "release" and sha != env.get("GITHUB_SHA"):
        raise ReleaseError("release tag moved away from the event commit")
    if prepare_image and (sha != env.get("RESOLVED_SHA") or version != env.get("RESOLVED_VERSION")):
        raise ReleaseError("release source changed after validation")

    source = read_github(f"repos/{repo}/contents/pyproject.toml?ref={sha}")
    if source.get("encoding") != "base64":
        raise ReleaseError("release source package metadata is unavailable")
    project = tomllib.loads(base64.b64decode(source["content"]).decode())["project"]
    if project.get("name") != "lionagi" or project.get("version") != version:
        raise ReleaseError("release tag and source package version do not match")

    if event == "workflow_dispatch" or prepare_image:
        _require_published_wheel(read_pypi, version)
    if event == "workflow_dispatch":
        _require_publish_evidence(read_github, repo, tag, sha)
    result = {"version": version, "sha": sha}
    if prepare_image:
        latest = read_pypi("").get("info")
        if (
            not isinstance(latest, dict)
            or latest.get("name") != "lionagi"
            or not isinstance(latest.get("version"), str)
            or not _VERSION.fullmatch(latest["version"])
        ):
            raise ReleaseError("PyPI did not report its current lionagi version")
        result["publish_latest"] = str(latest["version"] == version).lower()
    return result


def main() -> int:
    try:
        if sys.argv[1:] not in ([], ["--prepare-image"]):
            raise ReleaseError("usage: resolve_image_release.py [--prepare-image]")
        values = resolve(os.environ, prepare_image=bool(sys.argv[1:]))
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            for key, value in values.items():
                output.write(f"{key}={value}\n")
        print(f"Validated lionagi {values['version']} at {values['sha']}")
    except (
        ReleaseError,
        KeyError,
        TypeError,
        ValueError,
        OSError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
