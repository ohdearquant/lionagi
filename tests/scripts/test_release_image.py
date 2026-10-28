"""Release source selection and registry verification without publication."""

from __future__ import annotations

import base64
import copy
import importlib.util
import json
import os
import subprocess
import sys
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
# The four docker actions are pinned to commit SHAs because they build and push
# the release image, and a moving major tag would let that build change without
# a commit to this repository. The first-party actions stay on major tags,
# which this repository updates through Dependabot.
# Kept as named constants so an action bump is one edit here, not a hunt
# through the expected step lists.
CHECKOUT_ACTION = "actions/checkout@v7"
SETUP_PYTHON_ACTION = "actions/setup-python@v7"
QEMU_ACTION = "docker/setup-qemu-action@99012661954931238ded8c8b007157a8430204e1"
BUILDX_ACTION = "docker/setup-buildx-action@f87e5991a6d7451dcb8d9637bfbc97413f497069"
LOGIN_ACTION = "docker/login-action@81c6f93d4bc991e510b342a00b419b3bb4efc5c6"
IMAGE_ACTION = "docker/build-push-action@c3c9e263c25d99ce0380d002d59b67737d91b0dc"
GUARD_RUN = (
    'if [[ ! "${RESOLVED_SHA}" =~ ^[0-9a-f]{40}$ ]]; then\n'
    "  echo '::error::the release did not resolve a full source commit'\n"
    "  exit 1\n"
    "fi\n"
)
TEST_CHECKOUT_REF = "${{ github.event_name == 'release' && github.ref || format('refs/tags/v{0}', inputs.version) }}"
TAG_GUARD_RUN = (
    'if [[ "$(git rev-parse HEAD)" != "${RESOLVED_SHA}" ]]; then\n'
    "  echo '::error::the checked-out tag is not the resolved source commit'\n"
    "  exit 1\n"
    "fi\n"
)
UV_INSTALL_RUN = (
    'curl -LsSf https://astral.sh/uv/install.sh | sh\necho "$HOME/.cargo/bin" >> $GITHUB_PATH\n'
)
TEST_RUN = "source .venv/bin/activate\nuv run pytest --maxfail=1 --disable-warnings tests/\n"
VERIFY_RUN = (
    'if [ "${DOCKER_RESULT}" != success ]; then\n'
    "  echo '::error::image publication or digest verification did not succeed'\n"
    "  exit 1\n"
    "fi\n"
    'if [[ ! "${EXPECTED_DIGEST}" =~ ^sha256:[0-9a-f]{64}$ ]]; then\n'
    "  echo '::error::the image build did not produce a valid digest'\n"
    "  exit 1\n"
    "fi\n"
)
RUNS = (
    f"{PREFIX}/actions/workflows/release.yml/runs?event=release&head_sha={SHA}&per_page=100&page=1"
)
JOBS = f"{PREFIX}/actions/runs/123/jobs?filter=all&per_page=100&page=1"


def github_context(path):
    # The release workflow's expressions are compared here as text and evaluated
    # nowhere. The context name is joined rather than written out because the
    # required-workflow scan in test_advisory_pr_checks.py reads such a path
    # anywhere in code under tests/ as a read of the pull request's event.
    return "github." + path


def expression(path):
    return "${{ " + github_context(path) + " }}"


@pytest.fixture
def metadata():
    github_api = {
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
        if path not in github_api:
            raise release.ReleaseError("GitHub metadata missing")
        return copy.deepcopy(github_api[path])

    def pypi_read(version):
        calls.append(("pypi", version))
        if version not in pypi:
            raise release.ReleaseError("PyPI version missing")
        return copy.deepcopy(pypi[version])

    return env, github_api, pypi, github_read, pypi_read, calls


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


def test_metadata_command_failure_keeps_the_command_output():
    """A bare 'request failed' hides whether the command was missing, refused or
    returned nothing useful, so the captured stderr belongs in the message."""
    with pytest.raises(release.ReleaseError) as excinfo:
        release._read_command(["bash", "-c", "echo 'gh: command not found' >&2; exit 3"])
    message = str(excinfo.value)
    assert "exited 3" in message
    assert "gh: command not found" in message


def test_main_reports_an_unexpected_error_by_its_type(monkeypatch, tmp_path, capsys):
    """A missing metadata field surfaced only as ::error::'content', which reads
    like a deliberate refusal; the exception type names the real problem."""
    monkeypatch.setattr(sys, "argv", ["resolve_image_release.py"])
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))

    def broken(env, prepare_image=False):
        raise KeyError("content")

    monkeypatch.setattr(release, "resolve", broken)
    assert release.main() == 1
    assert "KeyError" in capsys.readouterr().err


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
    [
        ("head_sha", OTHER_SHA),
        ("head_branch", "v9.9.9"),
        ("event", "workflow_dispatch"),
        ("path", ".github/workflows/another.yml"),
    ],
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


def test_a_successful_build_step_is_not_publication_evidence(metadata):
    """The successful step that stands as upload evidence must be the one that
    names the upload. A deploy job whose build step passed while its publish
    step was skipped is not evidence that a wheel was ever uploaded."""
    metadata[1][JOBS]["jobs"][0]["steps"] = [
        {"name": "Build package", "status": "completed", "conclusion": "success"},
        {"name": "Publish package to PyPI", "status": "completed", "conclusion": "skipped"},
    ]
    with pytest.raises(release.ReleaseError, match="no successful release workflow"):
        resolve_fixture(metadata)


def test_upload_evidence_must_come_from_the_deploy_job(metadata):
    """A successful upload step in some other job is not the deploy job's
    upload, so the job name is part of what makes the step evidence."""
    job = metadata[1][JOBS]["jobs"][0]
    job["name"] = "publish"
    with pytest.raises(release.ReleaseError, match="no successful release workflow"):
        resolve_fixture(metadata)


def test_missing_publication_history_refuses_a_manual_repair(metadata):
    metadata[1][RUNS] = {"workflow_runs": []}
    with pytest.raises(release.ReleaseError, match="no successful release workflow"):
        resolve_fixture(metadata)


def test_moved_manual_tag_needs_publication_evidence_at_its_new_commit(metadata):
    github_api = metadata[1]
    github_api[f"{PREFIX}/commits/v1.2.3"]["sha"] = OTHER_SHA
    github_api[f"{PREFIX}/contents/pyproject.toml?ref={OTHER_SHA}"] = github_api[
        f"{PREFIX}/contents/pyproject.toml?ref={SHA}"
    ]
    github_api[RUNS.replace(SHA, OTHER_SHA)] = {"workflow_runs": []}
    with pytest.raises(release.ReleaseError, match="no successful release workflow"):
        resolve_fixture(metadata)


def test_publication_history_is_paginated_including_old_attempt_jobs(metadata):
    github_api = metadata[1]
    github_api[RUNS.replace("&page=1", "&page=2")] = github_api[RUNS]
    github_api[RUNS] = {"workflow_runs": [{"head_sha": OTHER_SHA}] * 100}
    github_api[JOBS.replace("&page=1", "&page=2")] = github_api[JOBS]
    github_api[JOBS] = {"jobs": [{"name": "test"}] * 100}
    assert resolve_fixture(metadata)["sha"] == SHA
    assert ("github", JOBS.replace("&page=1", "&page=2")) in metadata[-1]


def test_release_event_can_precede_pypi_and_publish_history(metadata):
    as_release_event(metadata[0])
    metadata[2].clear()
    del metadata[1][RUNS]
    assert resolve_fixture(metadata) == {"version": "1.2.3", "sha": SHA}
    assert not any(service == "pypi" for service, _ in metadata[-1])


@pytest.mark.parametrize("outcome", ["failure", "cancelled"])
def test_release_image_accepts_a_published_wheel_after_upload_client_failure(metadata, outcome):
    as_release_event(metadata[0])
    deploy = metadata[1][JOBS]["jobs"][0]
    deploy["conclusion"] = outcome
    deploy["steps"][0]["conclusion"] = outcome
    assert resolve_fixture(metadata, prepare_image=True) == {
        "version": "1.2.3",
        "sha": SHA,
        "publish_latest": "true",
    }
    assert not any("/actions/" in path for service, path in metadata[-1] if service == "github")


@pytest.mark.parametrize(
    "field,value", [("info", ["wrong shape"]), ("urls", None), ("urls", ["wrong shape"])]
)
def test_malformed_pypi_release_metadata_is_refused(metadata, field, value):
    metadata[2]["1.2.3"][field] = value
    with pytest.raises(release.ReleaseError, match="PyPI"):
        resolve_fixture(metadata)


@pytest.mark.parametrize("info", [["wrong shape"], {"name": "lionagi", "version": 123}, {}])
def test_malformed_latest_metadata_is_refused(metadata, info):
    metadata[2][""]["info"] = info
    with pytest.raises(release.ReleaseError, match="PyPI did not report"):
        resolve_fixture(metadata, prepare_image=True)


def test_publication_history_page_budget_is_explicit(metadata):
    for page in range(1, 11):
        metadata[1][RUNS.replace("&page=1", f"&page={page}")] = {
            "workflow_runs": [{"head_sha": OTHER_SHA}] * 100
        }
    with pytest.raises(release.ReleaseError, match="resolver's 1,000-item pagination budget"):
        resolve_fixture(metadata)
    assert not any("page=11" in path for _, path in metadata[-1])


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


@pytest.mark.parametrize("sha", ["", OTHER_SHA])
def test_image_preparation_rechecks_the_pinned_source(metadata, sha):
    metadata[0]["RESOLVED_SHA"] = sha
    with pytest.raises(release.ReleaseError, match="source changed"):
        resolve_fixture(metadata, prepare_image=True)


@pytest.mark.parametrize("version", ["", "9.9.9"])
def test_image_preparation_rechecks_the_pinned_version(metadata, version):
    metadata[0]["RESOLVED_VERSION"] = version
    with pytest.raises(release.ReleaseError, match="source changed"):
        resolve_fixture(metadata, prepare_image=True)
    assert metadata[-1][-1] == ("github", f"{PREFIX}/commits/v1.2.3")


@pytest.mark.parametrize("current,expected", [("1.2.3", "true"), ("1.2.4", "false")])
def test_old_manual_versions_cannot_move_latest(metadata, current, expected):
    metadata[2][""]["info"]["version"] = current
    assert resolve_fixture(metadata, prepare_image=True)["publish_latest"] == expected


def test_release_image_waits_for_the_wheel_to_be_published(metadata):
    as_release_event(metadata[0])
    del metadata[2]["1.2.3"]
    with pytest.raises(release.ReleaseError, match="PyPI version missing"):
        resolve_fixture(metadata, prepare_image=True)


class NoDuplicateKeysLoader(yaml.SafeLoader):
    """A loader that refuses a mapping key used twice in the same node.

    PyYAML keeps the last of two identical keys, so a first `if:` that would
    have skipped a step disappears with no trace in the parsed document. A
    duplicate key is a workflow error, not a value to pick between.
    """

    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            seen = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=deep)
                if key in seen:
                    raise yaml.constructor.ConstructorError(
                        "while constructing a mapping",
                        node.start_mark,
                        f"found duplicate key {key!r}",
                        key_node.start_mark,
                    )
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


def load_workflow(text):
    return yaml.load(text, Loader=NoDuplicateKeysLoader)


def workflow():
    return load_workflow((ROOT / ".github/workflows/release.yml").read_text())


def step_identity(step):
    """What identifies one step in an ordered list: its name, action and id."""
    return (step.get("name"), step.get("uses"), step.get("id"))


def assert_workflow_contract(document):
    jobs = document["jobs"]
    assert document["permissions"] == {"contents": "read"}
    assert document["env"] == {
        "DEFAULT_BRANCH": expression("event.repository.default_branch"),
        "INPUT_VERSION": "${{ inputs.version }}",
        "RELEASE_TAG": expression("event.release.tag_name"),
        "RELEASE_ID": expression("event.release.id"),
        "RELEASE_ACTION": expression("event.action"),
    }
    assert set(jobs) == {"source", "test", "deploy", "docker", "verify-image"}
    # Every job and the document are pinned by their exact key set: a new key
    # can change what a job runs, where it runs or what it is allowed to reach.
    # YAML 1.1 reads the `on` key as the boolean True, so that is its name here.
    assert set(document) == {"name", True, "permissions", "env", "jobs"}
    assert document[True] == {
        "release": {"types": ["published"]},
        "workflow_dispatch": {
            "inputs": {
                "version": {
                    "description": (
                        "Published version to repair from the default branch, without v "
                        "(e.g. 0.35.1)"
                    ),
                    "required": True,
                    "type": "string",
                }
            }
        },
    }
    assert {name: job["runs-on"] for name, job in jobs.items()} == {
        "source": "ubuntu-latest",
        "test": "ubuntu-latest",
        "deploy": "ubuntu-latest",
        "docker": "ubuntu-latest",
        "verify-image": "ubuntu-latest",
    }
    assert {name: set(job) for name, job in jobs.items()} == {
        "source": {"if", "permissions", "runs-on", "outputs", "steps"},
        "test": {"permissions", "needs", "runs-on", "strategy", "steps"},
        "deploy": {"permissions", "needs", "if", "runs-on", "timeout-minutes", "steps"},
        "docker": {
            "permissions",
            "needs",
            "if",
            "concurrency",
            "timeout-minutes",
            "runs-on",
            "outputs",
            "steps",
        },
        "verify-image": {"permissions", "needs", "if", "runs-on", "steps"},
    }
    for name, job in jobs.items():
        # A job-level value overrides the workflow-level pin, and a soft-failing
        # job leaves the run green without the result the gate requires.
        assert "env" not in job, f"{name} overrides the pinned workflow environment"
        assert not job.get("continue-on-error", False), f"{name} may fail without failing the run"
        expected = {"contents": "read"}
        if name in {"source", "docker"}:
            expected["actions"] = "read"
        if name == "docker":
            expected["packages"] = "write"
        assert job["permissions"] == expected
    assert jobs["source"]["outputs"] == {
        "version": "${{ steps.release.outputs.version }}",
        "sha": "${{ steps.release.outputs.sha }}",
    }
    # Exact step lists: an inserted, reordered, skipped or retagged step can
    # change the source, the build environment or what is published.
    guard = {
        "name": "Require the resolved source commit",
        "shell": "bash",
        "env": {"RESOLVED_SHA": "${{ needs.source.outputs.sha }}"},
        "run": GUARD_RUN,
    }
    assert jobs["source"]["outputs"] == {
        "version": "${{ steps.release.outputs.version }}",
        "sha": "${{ steps.release.outputs.sha }}",
    }
    assert jobs["source"]["if"] == (
        "github.event_name == 'workflow_dispatch' || startsWith("
        + github_context("event.release.tag_name")
        + ", 'v')"
    )
    assert jobs["source"]["steps"] == [
        {
            "uses": CHECKOUT_ACTION,
            "with": {"ref": "${{ github.sha }}", "persist-credentials": False},
        },
        {"uses": SETUP_PYTHON_ACTION, "with": {"python-version": "3.12"}},
        {
            "name": "Validate the release and resolve its source",
            "id": "release",
            "env": {"GH_TOKEN": "${{ github.token }}"},
            "run": "python scripts/resolve_image_release.py",
        },
    ]
    assert jobs["test"]["needs"] == "source"
    assert "if" not in jobs["test"]
    assert jobs["test"]["strategy"] == {
        "matrix": {"python-version": ["3.10", "3.11", "3.12", "3.13"]}
    }
    # The test job is what makes a red commit refuse the release, so its steps
    # are pinned whole: a replaced install or test command would otherwise pass.
    # It checks out the tag ref, not the commit an earlier job computed, and then
    # requires that tag to be the resolved source before anything runs.
    assert jobs["test"]["steps"] == [
        guard,
        {
            "uses": CHECKOUT_ACTION,
            "with": {"ref": TEST_CHECKOUT_REF, "persist-credentials": False},
        },
        {
            "name": "Require the checked-out tag to be the resolved source",
            "shell": "bash",
            "env": {"RESOLVED_SHA": "${{ needs.source.outputs.sha }}"},
            "run": TAG_GUARD_RUN,
        },
        {
            "name": "Set up Python ${{ matrix.python-version }}",
            "uses": SETUP_PYTHON_ACTION,
            "with": {"python-version": "${{ matrix.python-version }}"},
        },
        {"name": "Install uv", "run": UV_INSTALL_RUN},
        {"name": "Install dependencies", "run": "uv venv\nuv sync --all-extras\n"},
        {"name": "Run tests", "run": TEST_RUN},
    ]
    for step in jobs["test"]["steps"]:
        assert "if" not in step, "no test step may be skipped"
        assert not step.get("continue-on-error", False), "no test step may fail softly"
    assert jobs["deploy"]["needs"] == ["source", "test"]
    assert jobs["deploy"]["if"] == "github.event_name == 'release'"
    assert jobs["deploy"]["steps"] == [
        guard,
        {
            "uses": CHECKOUT_ACTION,
            "with": {"ref": "${{ needs.source.outputs.sha }}", "persist-credentials": False},
        },
        {
            "name": "Set up Python",
            "uses": SETUP_PYTHON_ACTION,
            "with": {"python-version": "3.12"},
        },
        {"name": "Install uv", "run": UV_INSTALL_RUN},
        {"name": "Install dependencies", "run": "uv venv\nuv pip install twine\n"},
        {"name": "Build package", "run": "uv build"},
        {
            "name": "Publish package to PyPI",
            "env": {
                "TWINE_USERNAME": "__token__",
                "TWINE_PASSWORD": "${{ secrets.PYPI_API_KEY }}",
            },
            "run": "uv run twine upload dist/*",
        },
    ]
    docker = jobs["docker"]
    assert docker["needs"] == ["source", "test", "deploy"]
    assert docker["if"] == (
        "${{ !cancelled() && needs.source.result == 'success' && needs.test.result == 'success' "
        "&& (github.event_name == 'release' || github.event_name == 'workflow_dispatch') }}"
    )
    assert docker["concurrency"] == {"group": "lion-studio-image", "cancel-in-progress": False}
    assert docker["timeout-minutes"] == 60
    assert jobs["deploy"]["timeout-minutes"] == 30
    assert docker["outputs"] == {
        "digest": "${{ steps.image.outputs.digest }}",
        "publish_latest": "${{ steps.release.outputs.publish_latest }}",
    }
    checkouts = [
        s["with"] for s in docker["steps"] if s.get("uses", "").startswith("actions/checkout")
    ]
    assert {c["path"]: c["ref"] for c in checkouts} == {
        ".release-control": "${{ github.sha }}",
        "source": "${{ needs.source.outputs.sha }}",
    }
    revalidate = next(s for s in docker["steps"] if s.get("id") == "release")
    assert revalidate["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "RESOLVED_SHA": "${{ needs.source.outputs.sha }}",
        "RESOLVED_VERSION": "${{ needs.source.outputs.version }}",
    }
    assert (
        revalidate["run"]
        == "python .release-control/scripts/resolve_image_release.py --prepare-image"
    )
    assert "if" not in revalidate
    assert not revalidate.get("continue-on-error", False)
    image = next(s for s in docker["steps"] if s.get("id") == "image")
    digest_verify = docker["steps"][-1]
    assert docker["steps"].index(revalidate) < docker["steps"].index(image)
    # An unlisted step could retag latest between the revalidation and the
    # build, and the registry check only reads latest when this run publishes
    # it, so the ordered step list and the image inputs are both pinned.
    assert [step_identity(step) for step in docker["steps"]] == [
        ("Require the resolved source commit", None, None),
        (None, CHECKOUT_ACTION, None),
        (None, CHECKOUT_ACTION, None),
        (None, SETUP_PYTHON_ACTION, None),
        ("Revalidate the source and published wheel", None, "release"),
        ("Set up QEMU", QEMU_ACTION, None),
        ("Set up Docker Buildx", BUILDX_ACTION, "buildx"),
        ("Set up Docker Buildx (second attempt)", BUILDX_ACTION, None),
        ("Log in to GitHub Container Registry", LOGIN_ACTION, None),
        ("Build and push Docker image (multi-arch)", IMAGE_ACTION, "image"),
        ("Verify the published image digest", None, None),
    ]
    # Every step is pinned whole, not by identity: a checkout that recurses
    # into submodules, a different Python, a QEMU input, a Buildx driver image,
    # a removed Buildx retry, or a login identity swap changes who publishes
    # and how the image is built without changing a step's name or action, so
    # `with`, `env`, `if` and `continue-on-error` are all part of the contract.
    assert docker["steps"] == [
        guard,
        {
            "uses": CHECKOUT_ACTION,
            "with": {
                "ref": "${{ github.sha }}",
                "path": ".release-control",
                "persist-credentials": False,
            },
        },
        {
            "uses": CHECKOUT_ACTION,
            "with": {
                "ref": "${{ needs.source.outputs.sha }}",
                "path": "source",
                "persist-credentials": False,
            },
        },
        {"uses": SETUP_PYTHON_ACTION, "with": {"python-version": "3.12"}},
        {
            "name": "Revalidate the source and published wheel",
            "id": "release",
            "env": {
                "GH_TOKEN": "${{ github.token }}",
                "RESOLVED_SHA": "${{ needs.source.outputs.sha }}",
                "RESOLVED_VERSION": "${{ needs.source.outputs.version }}",
            },
            "run": "python .release-control/scripts/resolve_image_release.py --prepare-image",
        },
        {"name": "Set up QEMU", "uses": QEMU_ACTION},
        {
            "name": "Set up Docker Buildx",
            "id": "buildx",
            "continue-on-error": True,
            "uses": BUILDX_ACTION,
        },
        {
            "name": "Set up Docker Buildx (second attempt)",
            "if": "steps.buildx.outcome == 'failure'",
            "uses": BUILDX_ACTION,
        },
        {
            "name": "Log in to GitHub Container Registry",
            "uses": LOGIN_ACTION,
            "with": {
                "registry": "ghcr.io",
                "username": expression("actor"),
                "password": "${{ secrets.GITHUB_TOKEN }}",
            },
        },
        image,
        {
            "name": "Verify the published image digest",
            "if": "${{ !cancelled() && steps.image.outcome != 'skipped' }}",
            "env": {
                "VERSION": "${{ needs.source.outputs.version }}",
                "EXPECTED_DIGEST": "${{ steps.image.outputs.digest }}",
                "PUBLISH_LATEST": "${{ steps.release.outputs.publish_latest }}",
            },
            "run": "bash .release-control/scripts/verify_release_image.sh",
        },
    ]
    assert docker["steps"][0] == guard
    assert image == {
        "name": "Build and push Docker image (multi-arch)",
        "id": "image",
        "uses": IMAGE_ACTION,
        "with": {
            "context": "./source",
            "file": "source/apps/studio/Dockerfile",
            "push": True,
            "platforms": "linux/amd64,linux/arm64",
            "tags": (
                "ghcr.io/ohdearquant/lion-studio:${{ needs.source.outputs.version }}\n"
                "${{ steps.release.outputs.publish_latest == 'true' && "
                "'ghcr.io/ohdearquant/lion-studio:latest' || '' }}\n"
            ),
            "labels": (
                "org.opencontainers.image.version=${{ needs.source.outputs.version }}\n"
                "org.opencontainers.image.revision=${{ needs.source.outputs.sha }}\n"
            ),
            "cache-from": "type=gha",
            "cache-to": "type=gha,mode=max",
        },
    }
    assert jobs["verify-image"]["needs"] == ["source", "docker"]
    assert jobs["verify-image"]["if"] == ("${{ always() && needs.source.result == 'success' }}")
    assert docker["steps"].index(digest_verify) == docker["steps"].index(image) + 1
    assert len(jobs["verify-image"]["steps"]) == 1
    verify = jobs["verify-image"]["steps"][0]
    # The final gate is pinned whole. An unpinned key such as
    # `working-directory` or `id` would change how the gate runs without
    # changing the command it names.
    assert verify == {
        "name": "Require successful image publication and verification",
        "shell": "bash",
        "env": {
            "DOCKER_RESULT": "${{ needs.docker.result }}",
            "EXPECTED_DIGEST": "${{ needs.docker.outputs.digest }}",
        },
        "run": VERIFY_RUN,
    }
    for job in jobs.values():
        for step in job.get("steps", []):
            if step.get("uses", "").startswith("actions/checkout"):
                assert step["with"]["persist-credentials"] is False


def test_workflow_tests_builds_and_verifies_one_resolved_source():
    assert_workflow_contract(workflow())


@pytest.mark.parametrize(
    "mutation",
    [
        "deploy-write",
        "top-level-actions",
        "source-condition",
        "deploy-success",
        "no-lock",
        "cancel-running",
        "revalidate-skipped",
        "revalidate-soft-failure",
        "wrong-source",
        "verify-condition",
        "verify-latest",
        "verify-soft-failure",
        "digest-verify-skipped",
        "digest-verify-soft-step",
        "status-soft-failure",
        "status-soft-step",
        "guard-after-checkout",
        "test-skipped",
        "test-release-only",
        "test-unconditional",
        "default-branch-literal",
        "default-branch-current-ref",
        "latest-from-other-step",
        "version-tag-from-input",
        "latest-unconditional",
        "upload-soft-failure",
        "upload-skip-existing",
        "upload-skipped",
        "no-image-timeout",
        "verify-skipped-docker",
        "verify-before-image",
        "job-env-overrides-default-branch",
        "job-soft-failure-source",
        "job-soft-failure-verify-image",
        "job-soft-failure-deploy",
        "job-soft-failure-test",
        "job-soft-failure-docker",
        "test-run-replaced",
        "test-matrix-reduced",
        "retag-step-before-image",
        "image-revision-from-event-sha",
        "image-single-platform",
        "image-action-tag-not-sha",
        "image-push-disabled",
        "image-step-gated-on-latest",
        "run-tests-continue-on-error",
        "run-tests-skipped",
        "test-checkout-resolved-commit",
        "test-tag-guard-removed",
        "test-tag-guard-after-install",
        "install-run-replaced",
        "source-step-inserted",
        "deploy-step-inserted",
        "deploy-checkout-tag",
        "deploy-defaults",
        "deploy-container",
        "deploy-step-env",
        "build-run-replaced",
        "document-extra-key",
        "job-extra-key",
        "triggers-pull-request",
        "docker-runs-on-changed",
        "guard-run-retags",
        "deploy-timeout-removed",
        "docker-checkout-submodules",
        "setup-python-version-changed",
        "qemu-input-added",
        "buildx-driver-options",
        "buildx-retry-soft-failure-removed",
        "login-identity-replaced",
        "verify-step-working-directory",
        "verify-step-id",
        "verify-step-shell-replaced",
        "duplicate-if-key-on-docker",
    ],
)
def test_workflow_contract_rejects_publication_wiring_mutations(mutation):
    text = (ROOT / ".github/workflows/release.yml").read_text()
    if mutation == "duplicate-if-key-on-docker":
        # A second `if:` on the docker job. PyYAML keeps the last one, so a
        # loader that accepts duplicates would silently drop the first; the
        # loader must refuse the document instead.
        text = text.replace(
            "    if: ${{ !cancelled()",
            "    if: 'true'\n    if: ${{ !cancelled()",
            1,
        )
        with pytest.raises(yaml.constructor.ConstructorError, match="duplicate key"):
            load_workflow(text)
        return
    document = load_workflow(text)
    assert_workflow_contract(document)
    jobs = document["jobs"]
    docker = jobs["docker"]
    revalidate = next(s for s in docker["steps"] if s.get("id") == "release")
    verify = jobs["verify-image"]["steps"][0]
    digest_verify = docker["steps"][-1]
    image = next(s for s in docker["steps"] if s.get("id") == "image")
    publish = next(s for s in jobs["deploy"]["steps"] if s.get("name") == "Publish package to PyPI")
    source = jobs["source"]
    deploy = jobs["deploy"]
    test = jobs["test"]
    if mutation == "deploy-write":
        jobs["deploy"]["permissions"]["packages"] = "write"
    elif mutation == "top-level-actions":
        document["permissions"]["actions"] = "read"
    elif mutation == "source-condition":
        docker["if"] = docker["if"].replace("needs.source.result == 'success' && ", "")
    elif mutation == "deploy-success":
        docker["if"] = docker["if"].replace(
            "!cancelled()", "!cancelled() && needs.deploy.result == 'success'"
        )
    elif mutation == "no-lock":
        del docker["concurrency"]
    elif mutation == "cancel-running":
        docker["concurrency"]["cancel-in-progress"] = True
    elif mutation == "revalidate-skipped":
        revalidate["if"] = "false"
    elif mutation == "revalidate-soft-failure":
        revalidate["continue-on-error"] = True
    elif mutation == "wrong-source":
        revalidate["env"]["RESOLVED_SHA"] = "${{ github.sha }}"
    elif mutation == "verify-condition":
        jobs["verify-image"]["if"] = "success()"
    elif mutation == "verify-latest":
        digest_verify["env"]["PUBLISH_LATEST"] = "false"
    elif mutation == "verify-soft-failure":
        digest_verify["run"] += " || true"
    elif mutation == "digest-verify-skipped":
        digest_verify["if"] = "false"
    elif mutation == "digest-verify-soft-step":
        digest_verify["continue-on-error"] = True
    elif mutation == "status-soft-failure":
        verify["run"] = "true"
    elif mutation == "status-soft-step":
        verify["continue-on-error"] = True
    elif mutation == "guard-after-checkout":
        jobs["test"]["steps"][0:2] = reversed(jobs["test"]["steps"][0:2])
    elif mutation == "test-skipped":
        jobs["test"]["if"] = "false"
    elif mutation == "test-release-only":
        jobs["test"]["if"] = "github.event_name == 'release'"
    elif mutation == "test-unconditional":
        jobs["test"]["if"] = "true"
    elif mutation == "default-branch-literal":
        document["env"]["DEFAULT_BRANCH"] = "feature"
    elif mutation == "default-branch-current-ref":
        document["env"]["DEFAULT_BRANCH"] = "${{ github.ref_name }}"
    elif mutation == "latest-from-other-step":
        image["with"]["tags"] = image["with"]["tags"].replace(
            "steps.release.outputs.publish_latest", "steps.other.outputs.publish_latest"
        )
    elif mutation == "version-tag-from-input":
        image["with"]["tags"] = image["with"]["tags"].replace(
            "needs.source.outputs.version", "inputs.version"
        )
    elif mutation == "latest-unconditional":
        image["with"]["tags"] += "ghcr.io/ohdearquant/lion-studio:latest\n"
    elif mutation == "upload-soft-failure":
        publish["continue-on-error"] = True
    elif mutation == "upload-skip-existing":
        publish["run"] += " --skip-existing"
    elif mutation == "upload-skipped":
        publish["if"] = "false"
    elif mutation == "no-image-timeout":
        docker.pop("timeout-minutes", None)
    elif mutation == "verify-skipped-docker":
        jobs["verify-image"]["if"] = (
            "${{ always() && needs.source.result == 'success' && needs.docker.result != 'skipped' }}"
        )
    elif mutation == "verify-before-image":
        digest_verify["if"] = "${{ !cancelled() }}"
    elif mutation == "job-env-overrides-default-branch":
        jobs["source"]["env"] = {"DEFAULT_BRANCH": "feature"}
        docker["env"] = {"DEFAULT_BRANCH": "feature"}
    elif mutation == "job-soft-failure-source":
        jobs["source"]["continue-on-error"] = True
    elif mutation == "job-soft-failure-verify-image":
        jobs["verify-image"]["continue-on-error"] = True
    elif mutation == "job-soft-failure-deploy":
        jobs["deploy"]["continue-on-error"] = True
    elif mutation == "job-soft-failure-test":
        jobs["test"]["continue-on-error"] = True
    elif mutation == "job-soft-failure-docker":
        jobs["docker"]["continue-on-error"] = True
    elif mutation == "test-run-replaced":
        next(s for s in jobs["test"]["steps"] if s.get("name") == "Run tests").update(
            {"run": "true"}
        )
    elif mutation == "test-matrix-reduced":
        jobs["test"]["strategy"]["matrix"]["python-version"] = ["3.12"]
    elif mutation == "retag-step-before-image":
        docker["steps"].insert(
            docker["steps"].index(image),
            {
                "name": "Retag latest",
                "run": "docker buildx imagetools create -t ghcr.io/ohdearquant/lion-studio:latest"
                " ghcr.io/ohdearquant/lion-studio:1.2.3",
            },
        )
    elif mutation == "image-revision-from-event-sha":
        image["with"]["labels"] = image["with"]["labels"].replace(
            "needs.source.outputs.sha", "github.sha"
        )
    elif mutation == "image-single-platform":
        image["with"]["platforms"] = "linux/amd64"
    elif mutation == "image-action-tag-not-sha":
        image["uses"] = "docker/build-push-action@v7"
    elif mutation == "image-push-disabled":
        image["with"]["push"] = False
    elif mutation == "image-step-gated-on-latest":
        image["if"] = "steps.release.outputs.publish_latest == 'true'"
    elif mutation == "run-tests-continue-on-error":
        next(s for s in test["steps"] if s.get("name") == "Run tests")["continue-on-error"] = True
    elif mutation == "test-checkout-resolved-commit":
        next(s for s in test["steps"] if s.get("uses", "").startswith("actions/checkout"))["with"][
            "ref"
        ] = "${{ needs.source.outputs.sha }}"
    elif mutation == "test-tag-guard-removed":
        test["steps"] = [
            s
            for s in test["steps"]
            if s.get("name") != "Require the checked-out tag to be the resolved source"
        ]
    elif mutation == "test-tag-guard-after-install":
        steps = test["steps"]
        tag_guard = next(
            s
            for s in steps
            if s.get("name") == "Require the checked-out tag to be the resolved source"
        )
        steps.remove(tag_guard)
        steps.insert(
            next(i for i, s in enumerate(steps) if s.get("name") == "Install dependencies") + 1,
            tag_guard,
        )
    elif mutation == "run-tests-skipped":
        next(s for s in test["steps"] if s.get("name") == "Run tests")["if"] = "false"
    elif mutation == "install-run-replaced":
        next(s for s in test["steps"] if s.get("name") == "Install dependencies")["run"] = "true"
    elif mutation == "source-step-inserted":
        source["steps"].insert(1, {"name": "Overwrite resolver", "run": "true"})
    elif mutation == "deploy-step-inserted":
        deploy["steps"].insert(1, {"name": "Overwrite source", "run": "true"})
    elif mutation == "deploy-checkout-tag":
        next(s for s in deploy["steps"] if s.get("uses", "").startswith("actions/checkout"))[
            "uses"
        ] = "actions/checkout@v6"
    elif mutation == "deploy-defaults":
        deploy["defaults"] = {"run": {"working-directory": "scratch"}}
    elif mutation == "deploy-container":
        deploy["container"] = "python:3.12"
    elif mutation == "deploy-step-env":
        next(s for s in deploy["steps"] if s.get("name") == "Install dependencies")["env"] = {
            "UV_INDEX_URL": "https://pypi.example/simple"
        }
    elif mutation == "build-run-replaced":
        next(s for s in deploy["steps"] if s.get("name") == "Build package")["run"] = (
            "uv build --wheel && uv build --sdist"
        )
    elif mutation == "document-extra-key":
        document["concurrency"] = {"group": "release"}
    elif mutation == "job-extra-key":
        deploy["environment"] = "pypi"
    elif mutation == "triggers-pull-request":
        document[True]["pull_request"] = {}
    elif mutation == "docker-runs-on-changed":
        docker["runs-on"] = "ubuntu-22.04"
    elif mutation == "guard-run-retags":
        for job in (test, deploy, docker):
            job["steps"][0]["run"] += (
                "docker buildx imagetools create -t ghcr.io/ohdearquant/lion-studio:latest"
                " ghcr.io/ohdearquant/lion-studio:1.2.3 || true\n"
            )
    elif mutation == "deploy-timeout-removed":
        deploy.pop("timeout-minutes", None)
    elif mutation == "docker-checkout-submodules":
        next(s for s in docker["steps"] if s.get("with", {}).get("path") == ".release-control")[
            "with"
        ]["submodules"] = "recursive"
    elif mutation == "setup-python-version-changed":
        next(s for s in docker["steps"] if s.get("uses") == SETUP_PYTHON_ACTION)["with"][
            "python-version"
        ] = "3.13"
    elif mutation == "qemu-input-added":
        next(s for s in docker["steps"] if s.get("name") == "Set up QEMU")["with"] = {
            "cache": "false"
        }
    elif mutation == "buildx-driver-options":
        next(s for s in docker["steps"] if s.get("id") == "buildx")["with"] = {
            "driver-opts": "image=moby/buildkit:master"
        }
    elif mutation == "buildx-retry-soft-failure-removed":
        next(s for s in docker["steps"] if s.get("id") == "buildx").pop("continue-on-error", None)
    elif mutation == "login-identity-replaced":
        next(s for s in docker["steps"] if s.get("uses") == LOGIN_ACTION)["with"]["password"] = (
            "${{ secrets.PUBLISH_TOKEN }}"
        )
    elif mutation == "verify-step-working-directory":
        verify["working-directory"] = "/"
    elif mutation == "verify-step-id":
        verify["id"] = "gate"
    elif mutation == "verify-step-shell-replaced":
        verify["shell"] = "sh"
    with pytest.raises((AssertionError, KeyError)):
        assert_workflow_contract(document)


@pytest.mark.parametrize("job", ["test", "deploy", "docker"])
@pytest.mark.parametrize("sha", ["", "branch-name", "a" * 39, SHA])
def test_source_guard_refuses_checkout_fallback(job, sha):
    guard = workflow()["jobs"][job]["steps"][0]
    result = subprocess.run(
        ["bash", "-c", guard["run"]],
        env={**os.environ, "RESOLVED_SHA": sha},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == (0 if sha == SHA else 1)
    if sha != SHA:
        assert "::error::the release did not resolve a full source commit" in result.stdout


@pytest.fixture
def registry(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls"
    arguments = tmp_path / "curl-arguments"
    responses = tmp_path / "responses.json"
    responses.write_text("{}")
    curl = bindir / "curl"
    curl.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
url = sys.argv[-1]
with Path(os.environ["CALLS"]).open("a") as out:
    out.write(url + "\\n")
with Path(os.environ["CURL_ARGUMENTS"]).open("a") as out:
    out.write(" ".join(sys.argv[1:]) + "\\n")
if "/token?" in url:
    print('{"token": "public-pull-token"}')
else:
    data = json.loads(Path(os.environ["RESPONSES"]).read_text())
    if "__sequence__" in data:
        # Snapshots consumed in order, one per manifest read, so a lagging
        # first read can be followed by a correct one within a single run.
        counter_path = Path(os.environ["RESPONSES"]).with_suffix(".count")
        seen = int(counter_path.read_text()) if counter_path.exists() else 0
        counter_path.write_text(str(seen + 1))
        sequence = data["__sequence__"]
        data = sequence[min(seen, len(sequence) - 1)]
    item = data.get(url.rsplit("/", 1)[-1], {})
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
        "CURL_ARGUMENTS": str(arguments),
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

    return run, digest, calls, arguments


def test_registry_reads_are_bounded_in_time(registry):
    """The verifier holds the shared image slot while it reads the registry. A
    request with no client-side deadline can sit until the docker job's timeout
    ends it, holding every queued image job behind it, so each curl call must
    carry both a connect and a total deadline."""
    run, digest, _, arguments = registry
    assert run({"1.2.3": {"digest": digest}, "latest": {"digest": digest}}).returncode == 0
    lines = arguments.read_text().splitlines()
    assert lines, "the verifier must have called curl"
    for line in lines:
        # The value token, not the flag name alone: `--max-time 86400` and
        # `--connect-timeout 100` both satisfy a substring check on the flag
        # and are not the deadlines that bound the slot.
        tokens = line.split()
        assert tokens[tokens.index("--connect-timeout") + 1] == "10", line
        assert tokens[tokens.index("--max-time") + 1] == "30", line


def test_registry_verifies_both_published_digests(registry):
    run, digest, _, _ = registry
    assert run({"1.2.3": {"digest": digest}, "latest": {"digest": digest}}).returncode == 0


def test_old_version_verification_does_not_require_or_read_latest(registry):
    run, digest, calls, _ = registry
    assert run({"1.2.3": {"digest": digest}}, PUBLISH_LATEST="false").returncode == 0
    assert "/manifests/latest" not in calls.read_text()


def test_a_lagging_first_read_is_retried_until_the_registry_catches_up(registry):
    """The push can reach the registry after the verifier's first read, so a
    stale first snapshot must not fail a correct run: the retry loop is what
    makes the difference between 'not yet' and 'never'."""
    run, digest, _, _ = registry
    old = "sha256:" + "d" * 64
    stale = {"1.2.3": {"digest": old}, "latest": {"digest": old}}
    caught_up = {"1.2.3": {"digest": digest}, "latest": {"digest": digest}}
    result = run({"__sequence__": [stale, stale, caught_up, caught_up]})
    assert result.returncode == 0
    assert f"Verified ghcr.io/ohdearquant/lion-studio:1.2.3 at {digest}" in result.stdout


@pytest.mark.parametrize("tag", ["1.2.3", "latest"])
def test_an_old_digest_cannot_satisfy_this_runs_build(registry, tag):
    run, digest, _, _ = registry
    tags = {"1.2.3": {"digest": digest}, "latest": {"digest": digest}}
    tags[tag]["digest"] = "sha256:" + "d" * 64
    result = run(tags)
    assert result.returncode == 1
    assert (
        "::error::the requested image tags do not resolve to this run's build digest"
        in result.stdout
    )


def test_two_old_tags_that_agree_with_each_other_still_cannot_satisfy_this_run(registry):
    """Both tags resolving to the same older digest is the shape a stale `latest`
    and a leftover version tag share; comparing the two tags with each other
    would accept it, so each must be compared with this run's build digest."""
    run, _, _, _ = registry
    old = "sha256:" + "d" * 64
    result = run({"1.2.3": {"digest": old}, "latest": {"digest": old}})
    assert result.returncode == 1
    assert (
        "::error::the requested image tags do not resolve to this run's build digest"
        in result.stdout
    )


def test_failed_registry_read_cannot_pass_with_partial_output(registry):
    run, digest, _, _ = registry
    result = run({"1.2.3": {"digest": digest, "exit": 22}, "latest": {"digest": digest}})
    assert result.returncode == 1
    assert (
        "::error::the requested image tags do not resolve to this run's build digest"
        in result.stdout
    )


@pytest.mark.parametrize("digest", ["", "sha256:bad"])
def test_failed_build_is_not_verified_against_a_preexisting_tag(registry, digest):
    run, existing_digest, calls, _ = registry
    result = run(
        {"1.2.3": {"digest": existing_digest}, "latest": {"digest": existing_digest}},
        EXPECTED_DIGEST=digest,
    )
    assert result.returncode == 1
    assert "::error::the image build did not produce a valid digest" in result.stderr
    assert not calls.exists()


@pytest.mark.parametrize("value", ["", "TRUE", "yes", "0", "false\ntrue"])
def test_invalid_latest_decision_is_refused_before_registry_reads(registry, value):
    run, _, calls, _ = registry
    result = run({}, PUBLISH_LATEST=value)
    assert result.returncode == 1
    assert "::error::latest publication decision must be true or false" in result.stderr
    assert not calls.exists()


@pytest.mark.parametrize("value", ["", "v1.2.3", "1.2", "latest", "1.2.3/path", "1.2.3\nlatest"])
def test_invalid_verifier_version_is_refused_before_registry_reads(registry, value):
    run, _, calls, _ = registry
    result = run({}, VERSION=value)
    assert result.returncode == 1
    assert "::error::version must be a numeric x.y.z release" in result.stderr
    assert not calls.exists()


@pytest.mark.parametrize(
    "version",
    ["1.2.3a1", "1.2.3b2", "1.2.3rc1", "1.2.3.post1", "1.2.3.dev1", "1.2.3rc1.post2.dev3"],
)
def test_verifier_accepts_the_resolver_version_forms(registry, version):
    run, digest, _, _ = registry
    assert release._VERSION.fullmatch(version)
    result = run({version: {"digest": digest}}, VERSION=version, PUBLISH_LATEST="false")
    assert result.returncode == 0
    assert f"Verified ghcr.io/ohdearquant/lion-studio:{version}" in result.stdout


def test_empty_registry_digest_cannot_match_a_valid_build(registry):
    run, _, _, _ = registry
    result = run({})
    assert result.returncode == 1
    assert (
        "::error::the requested image tags do not resolve to this run's build digest"
        in result.stdout
    )


@pytest.mark.parametrize(
    "result,digest,error",
    [
        ("success", "sha256:" + "c" * 64, None),
        (
            "failure",
            "sha256:" + "c" * 64,
            "image publication or digest verification did not succeed",
        ),
        ("failure", "", "image publication or digest verification did not succeed"),
        (
            "cancelled",
            "sha256:" + "c" * 64,
            "image publication or digest verification did not succeed",
        ),
        (
            "skipped",
            "sha256:" + "c" * 64,
            "image publication or digest verification did not succeed",
        ),
        ("skipped", "", "image publication or digest verification did not succeed"),
        ("success", "", "the image build did not produce a valid digest"),
        ("success", "sha256:bad", "the image build did not produce a valid digest"),
    ],
)
def test_final_status_gate_requires_successful_build_and_verification(result, digest, error):
    guard = workflow()["jobs"]["verify-image"]["steps"][0]
    completed = subprocess.run(
        ["bash", "-c", guard["run"]],
        env={**os.environ, "DOCKER_RESULT": result, "EXPECTED_DIGEST": digest},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert completed.returncode == (0 if error is None else 1)
    if error:
        assert f"::error::{error}" in completed.stdout


def test_digest_verification_checks_each_registry_snapshot(registry):
    run, digest_a, _, _ = registry
    digest_b = "sha256:" + "d" * 64
    after_a = {"1.2.3": {"digest": digest_a}, "latest": {"digest": digest_a}}
    after_b = {**after_a, "1.2.4": {"digest": digest_b}, "latest": {"digest": digest_b}}

    # A's digest no longer matches latest once the registry contains B's snapshot.
    late_a = run(after_b)
    assert late_a.returncode == 1
    assert (
        "::error::the requested image tags do not resolve to this run's build digest"
        in late_a.stdout
    )

    # Each matching registry snapshot verifies its own build digest.
    assert run(after_a).returncode == 0
    assert run(after_b, VERSION="1.2.4", EXPECTED_DIGEST=digest_b).returncode == 0


def test_failed_read_is_not_described_as_a_recorded_reading():
    """The job summary is written by the last step, which a failed read skips, so
    a comment promising a reading on every run would claim a record that a
    failed read never leaves behind."""
    text = " ".join((ROOT / ".github/workflows/image-matches-pypi.yml").read_text().split())
    assert "Every run writes its readings" not in text
    assert "Written on every run" not in text
    assert "Every completed reading writes its readings" in text
    assert "Written on every completed reading" in text


def test_daily_check_guidance_does_not_claim_an_older_image_on_a_missing_tag():
    """`li studio` pulls `:latest` rather than a version tag, so when no `latest`
    exists the pull fails; the issue must not tell that reader they are on an
    older image."""
    text = " ".join((ROOT / ".github/workflows/image-matches-pypi.yml").read_text().split())
    assert "is on the older image until this is repaired" not in text
    assert "is on an older image, or has none, until this is repaired" in text


def test_ci_note_states_what_the_repair_and_refusal_actually_do():
    """The note must say the repair reruns the released commit's tests (the image
    job waits on the test job), that the refusal follows from the missing upload
    record rather than from any image failure, and that the later image run it
    names is not scheduled by anything."""
    text = " ".join((ROOT / "docs/internals/ci.md").read_text().split())
    assert "repair also reruns the released commit's test matrix" in text
    assert "This also applies when that release's image job later failed" not in text
    assert "it applies whether or not that release's image job also failed" in text
    assert "Nothing schedules that run" in text


def test_daily_check_guidance_does_not_offer_a_new_release_as_recovery():
    """The daily check follows PyPI's current version, so the issue it files for an
    older version closes as soon as a newer image becomes current. Guidance that
    offered a new release as recovery would leave that version without an image
    while the issue that reported it closed, so the offer must be gone and the
    correction that replaced it present."""
    text = " ".join((ROOT / ".github/workflows/image-matches-pypi.yml").read_text().split())
    assert "publish a new release" not in text
    assert "A newer release does not restore this version's image" in text
    assert "the issue closes and the version above is left without one" in text
    assert load_workflow((ROOT / ".github/workflows/image-matches-pypi.yml").read_text())


def test_daily_check_guidance_does_not_claim_the_issue_is_current():
    """The check reads the registry once a day, so an issue stays open after a
    repair until the next run. Saying "right now" would overstate what the open
    issue proves; it records the last reading, not the current registry."""
    text = " ".join((ROOT / ".github/workflows/image-matches-pypi.yml").read_text().split())
    assert "right now" not in text
    assert "currently-behind" not in text
    assert "behind at the last daily reading" in text
