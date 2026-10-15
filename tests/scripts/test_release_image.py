"""Release source selection and registry verification without publication."""

from __future__ import annotations

import base64
import copy
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "resolve_image_release", ROOT / "scripts/resolve_image_release.py"
)
assert SPEC is not None and SPEC.loader is not None
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)

SHA = "a" * 40
OTHER_SHA = "b" * 40
REPO = "example/lionagi"
PREFIX = f"repos/{REPO}"
RUNS = (
    f"{PREFIX}/actions/workflows/release.yml/runs?event=release&head_sha={SHA}&per_page=100&page=1"
)
JOBS = f"{PREFIX}/actions/runs/123/jobs?filter=all&per_page=100&page=1"


@pytest.fixture
def metadata():
    github = {
        f"{PREFIX}/releases/tags/v1.2.3": {
            "id": 45,
            "tag_name": "v1.2.3",
            "draft": False,
            "published_at": "2026-01-01T00:00:00Z",
        },
        f"{PREFIX}/commits/v1.2.3": {"sha": SHA},
        f"{PREFIX}/contents/pyproject.toml?ref={SHA}": {
            "encoding": "base64",
            "content": base64.b64encode(
                b'[project]\nname = "lionagi"\nversion = "1.2.3"\n'
            ).decode(),
        },
        RUNS: {
            "workflow_runs": [
                {
                    "id": 123,
                    "event": "release",
                    "head_sha": SHA,
                    "head_branch": "v1.2.3",
                    "path": ".github/workflows/release.yml",
                    "status": "completed",
                    "conclusion": "failure",
                }
            ]
        },
        JOBS: {
            "jobs": [
                {
                    "name": "deploy",
                    "head_sha": SHA,
                    "status": "completed",
                    "conclusion": "success",
                    "steps": [
                        {
                            "name": "Publish package to PyPI",
                            "status": "completed",
                            "conclusion": "success",
                        }
                    ],
                }
            ]
        },
    }
    pypi = {
        "1.2.3": {
            "info": {"name": "lionagi", "version": "1.2.3"},
            "urls": [{"packagetype": "bdist_wheel", "yanked": False}],
        },
        "": {"info": {"name": "lionagi", "version": "1.2.3"}},
    }
    env = {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": OTHER_SHA,
        "DEFAULT_BRANCH": "main",
        "INPUT_VERSION": "1.2.3",
        "RESOLVED_SHA": SHA,
        "RESOLVED_VERSION": "1.2.3",
    }
    calls = []

    def github_read(path):
        calls.append(("github", path))
        if path not in github:
            raise release.ReleaseError("GitHub metadata missing")
        return copy.deepcopy(github[path])

    def pypi_read(version):
        calls.append(("pypi", version))
        if version not in pypi:
            raise release.ReleaseError("PyPI version missing")
        return copy.deepcopy(pypi[version])

    return env, github, pypi, github_read, pypi_read, calls


def resolve_fixture(metadata, **kwargs):
    env, _, _, read_github, read_pypi, _ = metadata
    return release.resolve(env, read_github, read_pypi, **kwargs)


def as_release_event(env):
    env.update(
        GITHUB_EVENT_NAME="release",
        GITHUB_REF="refs/tags/v1.2.3",
        GITHUB_SHA=SHA,
        RELEASE_TAG="v1.2.3",
        RELEASE_ACTION="published",
        RELEASE_ID="45",
    )


def test_manual_repair_uses_the_release_commit_not_the_workflow_commit(metadata):
    assert resolve_fixture(metadata) == {"version": "1.2.3", "sha": SHA}


@pytest.mark.parametrize(
    "version", ["", "v1.2.3", "latest", "1.2", "1.2.3/evil", "1.2.3\nlatest=x"]
)
def test_invalid_version_is_rejected_before_metadata_reads(metadata, version):
    metadata[0]["INPUT_VERSION"] = version
    with pytest.raises(release.ReleaseError, match="version must"):
        resolve_fixture(metadata)
    assert metadata[-1] == []


@pytest.mark.parametrize("ref", ["refs/heads/feature", "refs/tags/v1.2.3"])
def test_manual_workflow_must_come_from_the_default_branch(metadata, ref):
    metadata[0]["GITHUB_REF"] = ref
    with pytest.raises(release.ReleaseError, match="default branch"):
        resolve_fixture(metadata)
    assert metadata[-1] == []


@pytest.mark.parametrize(
    "field,value", [("draft", True), ("published_at", None), ("tag_name", "v9.9.9")]
)
def test_unpublished_or_wrong_release_is_refused(metadata, field, value):
    metadata[1][f"{PREFIX}/releases/tags/v1.2.3"][field] = value
    with pytest.raises(release.ReleaseError, match="published GitHub release"):
        resolve_fixture(metadata)


def test_missing_release_is_refused(metadata):
    del metadata[1][f"{PREFIX}/releases/tags/v1.2.3"]
    with pytest.raises(release.ReleaseError, match="metadata missing"):
        resolve_fixture(metadata)


def test_source_version_must_match_the_tag(metadata):
    metadata[1][f"{PREFIX}/contents/pyproject.toml?ref={SHA}"]["content"] = base64.b64encode(
        b'[project]\nname = "lionagi"\nversion = "9.9.9"\n'
    ).decode()
    with pytest.raises(release.ReleaseError, match="source package version"):
        resolve_fixture(metadata)


def test_manual_repair_requires_a_published_non_yanked_wheel(metadata):
    metadata[2]["1.2.3"]["urls"][0]["yanked"] = True
    with pytest.raises(release.ReleaseError, match="non-yanked wheel"):
        resolve_fixture(metadata)


def test_missing_pypi_version_is_refused(metadata):
    del metadata[2]["1.2.3"]
    with pytest.raises(release.ReleaseError, match="PyPI version missing"):
        resolve_fixture(metadata)


def test_pypi_version_must_match(metadata):
    metadata[2]["1.2.3"]["info"]["version"] = "9.9.9"
    with pytest.raises(release.ReleaseError, match="PyPI package identity"):
        resolve_fixture(metadata)


@pytest.mark.parametrize(
    "field,value",
    [("head_sha", OTHER_SHA), ("head_branch", "v9.9.9"), ("event", "workflow_dispatch")],
)
def test_another_run_is_not_publication_evidence(metadata, field, value):
    metadata[1][RUNS]["workflow_runs"][0][field] = value
    with pytest.raises(release.ReleaseError, match="no successful release workflow"):
        resolve_fixture(metadata)


@pytest.mark.parametrize(
    "field,value", [("head_sha", OTHER_SHA), ("status", "in_progress"), ("conclusion", "failure")]
)
def test_another_or_unsuccessful_deploy_is_not_publication_evidence(metadata, field, value):
    metadata[1][JOBS]["jobs"][0][field] = value
    with pytest.raises(release.ReleaseError, match="no successful release workflow"):
        resolve_fixture(metadata)


def test_skipped_upload_is_not_publication_evidence(metadata):
    metadata[1][JOBS]["jobs"][0]["steps"][0]["conclusion"] = "skipped"
    with pytest.raises(release.ReleaseError, match="no successful release workflow"):
        resolve_fixture(metadata)


def test_missing_publication_history_refuses_a_manual_repair(metadata):
    metadata[1][RUNS] = {"workflow_runs": []}
    with pytest.raises(release.ReleaseError, match="no successful release workflow"):
        resolve_fixture(metadata)


def test_moved_manual_tag_needs_publication_evidence_at_its_new_commit(metadata):
    github = metadata[1]
    github[f"{PREFIX}/commits/v1.2.3"]["sha"] = OTHER_SHA
    github[f"{PREFIX}/contents/pyproject.toml?ref={OTHER_SHA}"] = github[
        f"{PREFIX}/contents/pyproject.toml?ref={SHA}"
    ]
    github[RUNS.replace(SHA, OTHER_SHA)] = {"workflow_runs": []}
    with pytest.raises(release.ReleaseError, match="no successful release workflow"):
        resolve_fixture(metadata)


def test_publication_history_is_paginated_including_old_attempt_jobs(metadata):
    github = metadata[1]
    github[RUNS.replace("&page=1", "&page=2")] = github[RUNS]
    github[RUNS] = {"workflow_runs": [{"head_sha": OTHER_SHA}] * 100}
    github[JOBS.replace("&page=1", "&page=2")] = github[JOBS]
    github[JOBS] = {"jobs": [{"name": "test"}] * 100}
    assert resolve_fixture(metadata)["sha"] == SHA
    assert ("github", JOBS.replace("&page=1", "&page=2")) in metadata[-1]


def test_release_event_can_precede_pypi_and_publish_history(metadata):
    as_release_event(metadata[0])
    metadata[2].clear()
    del metadata[1][RUNS]
    assert resolve_fixture(metadata) == {"version": "1.2.3", "sha": SHA}
    assert not any(service == "pypi" for service, _ in metadata[-1])


def test_release_event_rejects_a_moved_tag(metadata):
    as_release_event(metadata[0])
    metadata[1][f"{PREFIX}/commits/v1.2.3"]["sha"] = OTHER_SHA
    with pytest.raises(release.ReleaseError, match="tag moved"):
        resolve_fixture(metadata)


def test_release_event_identity_cannot_change(metadata):
    as_release_event(metadata[0])
    metadata[0]["RELEASE_ID"] = "99"
    with pytest.raises(release.ReleaseError, match="identity changed"):
        resolve_fixture(metadata)


def test_image_preparation_rechecks_the_pinned_source(metadata):
    metadata[0]["RESOLVED_SHA"] = OTHER_SHA
    with pytest.raises(release.ReleaseError, match="source changed"):
        resolve_fixture(metadata, prepare_image=True)


@pytest.mark.parametrize("current,expected", [("1.2.3", "true"), ("1.2.4", "false")])
def test_old_manual_versions_cannot_move_latest(metadata, current, expected):
    metadata[2][""]["info"]["version"] = current
    assert resolve_fixture(metadata, prepare_image=True)["publish_latest"] == expected


def test_release_image_waits_for_the_wheel_to_be_published(metadata):
    as_release_event(metadata[0])
    del metadata[2]["1.2.3"]
    with pytest.raises(release.ReleaseError, match="PyPI version missing"):
        resolve_fixture(metadata, prepare_image=True)


def test_workflow_tests_builds_and_verifies_one_resolved_source():
    jobs = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())["jobs"]
    for name in ("test", "deploy"):
        checkout = next(
            s for s in jobs[name]["steps"] if s.get("uses", "").startswith("actions/checkout")
        )
        assert checkout.get("with", {}).get("ref") == "${{ needs.source.outputs.sha }}"
        assert "source" in jobs[name]["needs"]
    docker = jobs["docker"]
    checkouts = [
        s["with"] for s in docker["steps"] if s.get("uses", "").startswith("actions/checkout")
    ]
    assert {c["path"]: c["ref"] for c in checkouts} == {
        ".release-control": "${{ github.sha }}",
        "source": "${{ needs.source.outputs.sha }}",
    }
    image = next(s for s in docker["steps"] if s.get("id") == "image")
    assert image["with"]["context"] == "./source"
    assert image["with"]["file"] == "source/apps/studio/Dockerfile"
    assert "needs.source.outputs.version" in image["with"]["tags"]
    assert "publish_latest == 'true'" in image["with"]["tags"]
    assert "needs.deploy.result == 'success'" in docker["if"]
    assert jobs["deploy"]["if"] == "github.event_name == 'release'"
    verify = jobs["verify-image"]["steps"][-1]
    assert verify["env"]["VERSION"] == "${{ needs.source.outputs.version }}"
    assert verify["env"]["EXPECTED_DIGEST"] == "${{ needs.docker.outputs.digest }}"
    assert verify["run"] == "bash scripts/verify_release_image.sh"
    for job in jobs.values():
        for step in job.get("steps", []):
            if step.get("uses", "").startswith("actions/checkout"):
                assert step["with"]["persist-credentials"] is False


@pytest.fixture
def registry(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls"
    responses = tmp_path / "responses.json"
    responses.write_text("{}")
    curl = bindir / "curl"
    curl.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
url = sys.argv[-1]
with Path(os.environ["CALLS"]).open("a") as out:
    out.write(url + "\\n")
if "/token?" in url:
    print('{"token": "public-pull-token"}')
else:
    item = json.loads(Path(os.environ["RESPONSES"]).read_text()).get(url.rsplit("/", 1)[-1], {})
    print("HTTP/2 200\\r\\nDocker-Content-Digest: " + item.get("digest", "") + "\\r\\n")
    sys.exit(item.get("exit", 0))
""")
    jq = bindir / "jq"
    jq.write_text(
        '#!/usr/bin/env python3\nimport json, sys\nprint(json.load(sys.stdin)["token"])\n'
    )
    sleep = bindir / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n")
    for command in (curl, jq, sleep):
        command.chmod(0o755)
    digest = "sha256:" + "c" * 64
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "CALLS": str(calls),
        "RESPONSES": str(responses),
        "VERSION": "1.2.3",
        "EXPECTED_DIGEST": digest,
        "PUBLISH_LATEST": "true",
    }

    def run(tags, **overrides):
        responses.write_text(json.dumps(tags))
        return subprocess.run(
            ["bash", str(ROOT / "scripts/verify_release_image.sh")],
            env={**env, **overrides},
            capture_output=True,
            text=True,
            timeout=10,
        )

    return run, digest, calls


def test_registry_verifies_both_published_digests(registry):
    run, digest, _ = registry
    assert run({"1.2.3": {"digest": digest}, "latest": {"digest": digest}}).returncode == 0


def test_old_version_verification_does_not_require_or_read_latest(registry):
    run, digest, calls = registry
    assert run({"1.2.3": {"digest": digest}}, PUBLISH_LATEST="false").returncode == 0
    assert "/manifests/latest" not in calls.read_text()


@pytest.mark.parametrize("tag", ["1.2.3", "latest"])
def test_an_old_digest_cannot_satisfy_this_runs_build(registry, tag):
    run, digest, _ = registry
    tags = {"1.2.3": {"digest": digest}, "latest": {"digest": digest}}
    tags[tag]["digest"] = "sha256:" + "d" * 64
    assert run(tags).returncode != 0


def test_failed_registry_read_cannot_pass_with_partial_output(registry):
    run, digest, _ = registry
    assert (
        run({"1.2.3": {"digest": digest, "exit": 22}, "latest": {"digest": digest}}).returncode != 0
    )


@pytest.mark.parametrize("digest", ["", "sha256:bad"])
def test_failed_build_is_not_verified_against_a_preexisting_tag(registry, digest):
    run, _, calls = registry
    assert run({}, EXPECTED_DIGEST=digest).returncode != 0
    assert not calls.exists()
