# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Changed data-file checks use committed Git data and the selected base."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts import check_data_files

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_data_files.py"
ALLOWED_JSON = "tests/contracts/data/cli.json"
JSON_EXCEPTIONS = [
    "apps/studio/frontend/package-lock.json",
    "benchmarks/comparisons/benchmark_results_20251015_141023.json",
    "tests/contracts/data/cli.json",
    "tests/contracts/data/http.json",
]


def _git(repo: Path, env: dict[str, str], *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True, timeout=30
    )
    return result.stdout.strip()


def _write(repo: Path, name: str, contents: bytes) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)


def _commit(repo: Path, env: dict[str, str], message: str = "change") -> str:
    _git(repo, env, "add", "--all")
    _git(repo, env, "commit", "--quiet", "-m", message)
    return _git(repo, env, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    empty_config = tmp_path / "empty-git-config"
    empty_config.write_text("", encoding="utf-8")
    env = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_COUNT"):
        env.pop(key, None)
    env.update(
        GIT_CONFIG_GLOBAL=str(empty_config),
        GIT_CONFIG_SYSTEM=str(empty_config),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Test Author",
        GIT_AUTHOR_EMAIL="author@example.invalid",
        GIT_COMMITTER_NAME="Test Author",
        GIT_COMMITTER_EMAIL="author@example.invalid",
        GIT_TERMINAL_PROMPT="0",
    )
    _git(repo, env, "init", "--quiet", "-b", "main")
    (repo / "scripts").mkdir()
    shutil.copyfile(SCRIPT, repo / "scripts" / SCRIPT.name)
    _write(repo, "tests/rename-me.txt", b"unchanged content\n")
    _write(repo, ALLOWED_JSON, b"a" * (300 * 1024))
    base = _commit(repo, env, "base")
    return repo, env, base


def _run(repo: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(repo / "scripts" / SCRIPT.name), *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


@pytest.mark.parametrize("name", ["tests/x.jsonl", "TESTS/X.JSONL", "tests/x.ndjson"])
def test_added_stream_file_outside_allowed_locations_fails(repository, name):
    repo, env, base = repository
    _write(repo, name, b"row\n")
    _commit(repo, env)
    result = _run(repo, env, "--base", base)
    assert result.returncode == 1
    assert name in result.stdout and "rule A" in result.stdout
    assert "Changed paths: 1" in result.stdout


@pytest.mark.parametrize(
    "name",
    ["benchmarks/x/y.jsonl", "lion1/tests/fixtures/x.ndjson", "lionagi/testing/data/x.JSONL"],
)
def test_allowed_stream_file_passes(repository, name):
    repo, env, base = repository
    _write(repo, name, b"row\n")
    _commit(repo, env)
    result = _run(repo, env, "--base", base)
    assert result.returncode == 0, result.stderr
    assert "Changed paths: 1" in result.stdout
    assert "rule A" not in result.stdout


def test_renamed_stream_file_uses_destination(repository):
    repo, env, base = repository
    _git(repo, env, "mv", "tests/rename-me.txt", "tests/renamed.ndjson")
    _commit(repo, env)
    result = _run(repo, env, "--base", base)
    assert result.returncode == 1
    assert "tests/renamed.ndjson" in result.stdout and "rule A" in result.stdout
    assert "Changed paths: 1" in result.stdout


def test_copied_stream_file_uses_destination(repository):
    repo, env, base = repository
    shutil.copyfile(repo / "tests/rename-me.txt", repo / "tests/copied.ndjson")
    _commit(repo, env)
    result = _run(repo, env, "--base", base)
    assert result.returncode == 1
    assert "tests/copied.ndjson" in result.stdout and "rule A" in result.stdout
    assert "Changed paths: 1" in result.stdout


def test_deleted_stream_file_is_outside_population(repository):
    repo, env, _ = repository
    name = "tests/deleted.ndjson"
    _write(repo, name, b"row\n")
    base = _commit(repo, env, "add old data")
    (repo / name).unlink()
    _commit(repo, env, "remove old data")
    result = _run(repo, env, "--base", base)
    assert result.returncode == 0
    assert "Changed paths: 0" in result.stdout


@pytest.mark.parametrize("name", JSON_EXCEPTIONS)
def test_each_modified_large_json_exception_passes(repository, name):
    repo, env, base = repository
    if name != ALLOWED_JSON:
        _write(repo, name, b"a" * (300 * 1024))
        base = _commit(repo, env, "add old data")
    _write(repo, name, b"b" * (301 * 1024))
    _commit(repo, env)
    result = _run(repo, env, "--base", base)
    assert result.returncode == 0
    assert "Changed paths: 1" in result.stdout
    assert "rule B" not in result.stdout


def test_json_size_boundary_and_exact_exceptions(repository):
    repo, env, base = repository
    _write(repo, "tests/large.json", b"s" * (300 * 1024))
    _write(repo, "tests/small.json", b"s" * (200 * 1024))
    _write(repo, "tests/boundary.json", b"s" * (256 * 1024))
    _write(repo, "tests/over.JSON", b"s" * (256 * 1024 + 1))
    _write(repo, ALLOWED_JSON, b"b" * (301 * 1024))
    _commit(repo, env)
    result = _run(repo, env, "--base", base)
    assert result.returncode == 1
    assert "Changed paths: 5" in result.stdout
    assert "tests/large.json" in result.stdout and "307200" in result.stdout
    assert "tests/over.JSON" in result.stdout and "262145" in result.stdout
    assert "rule B" in result.stdout
    assert "tests/small.json" not in result.stdout
    assert "tests/boundary.json" not in result.stdout
    assert ALLOWED_JSON not in result.stdout
    assert "ssssssss" not in result.stdout


@pytest.mark.parametrize("committed_size,working_size,expected", [(200, 300, 0), (300, 200, 1)])
def test_size_uses_head_blob_not_working_tree(repository, committed_size, working_size, expected):
    repo, env, base = repository
    name = "tests/changing.json"
    _write(repo, name, b"x" * (committed_size * 1024))
    _commit(repo, env)
    _write(repo, name, b"x" * (working_size * 1024))
    result = _run(repo, env, "--base", base)
    assert result.returncode == expected
    assert (name in result.stdout) is (expected == 1)


def test_nul_paths_preserve_unicode_and_spaces_and_population(repository):
    repo, env, base = repository
    bad_name = "tests/caf\u00e9 long file.jsonl"
    _write(repo, bad_name, b"private-row\n")
    _write(repo, "tests/good.txt", b"ok\n")
    _commit(repo, env)
    result = _run(repo, env, "--base", base)
    assert result.returncode == 1
    assert bad_name in result.stdout
    assert "Changed paths: 2" in result.stdout
    assert "private-row" not in result.stdout


def test_unresolvable_base_fails_closed(repository):
    repo, env, _ = repository
    result = _run(repo, env, "--base", "not-a-real-reference")
    assert result.returncode == 2
    assert "not-a-real-reference" in result.stderr


def test_same_head_base_reports_empty_comparison(repository):
    repo, env, _ = repository
    result = _run(repo, env, "--base", "HEAD")
    assert result.returncode == 0
    assert "base is head" in result.stdout.lower()
    assert "Changed paths: 0" in result.stdout


@pytest.mark.parametrize("ref", ["refs/remotes/origin/dev/lionagi", "refs/remotes/origin/main"])
def test_default_non_merge_base_uses_available_remote(repository, ref):
    repo, env, base = repository
    _git(repo, env, "update-ref", ref, base)
    _write(repo, "tests/added.jsonl", b"row\n")
    _commit(repo, env)
    result = _run(repo, env)
    assert result.returncode == 1
    assert base in result.stdout
    assert "tests/added.jsonl" in result.stdout


def test_default_non_merge_without_remote_base_fails_closed(repository):
    repo, env, _ = repository
    result = _run(repo, env)
    assert result.returncode == 2
    assert "origin/dev/lionagi" in result.stderr
    assert "origin/main" in result.stderr


def test_default_merge_base_is_first_parent(repository):
    repo, env, _ = repository
    _git(repo, env, "checkout", "--quiet", "-b", "feature")
    _write(repo, "tests/introduced.jsonl", b"row\n")
    _commit(repo, env)
    _git(repo, env, "checkout", "--quiet", "main")
    first_parent = _git(repo, env, "rev-parse", "HEAD")
    _git(repo, env, "merge", "--quiet", "--no-ff", "-m", "merge feature", "feature")
    result = _run(repo, env)
    assert result.returncode == 1
    assert first_parent in result.stdout
    assert "tests/introduced.jsonl" in result.stdout
    assert "Changed paths: 1" in result.stdout


def test_ci_wrapper_dispatches_to_the_data_file_check(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    trace = tmp_path / "trace"
    uv = fake_bin / "uv"
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$TRACE_FILE"\n', encoding="utf-8")
    uv.chmod(0o755)
    env = os.environ.copy()
    env.update(PATH=f"{fake_bin}{os.pathsep}{env['PATH']}", TRACE_FILE=str(trace))
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "ci.sh"), "lint-data-files", "--base", "BASE"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert trace.read_text(encoding="utf-8").splitlines() == [
        "run",
        "--no-sync",
        "python",
        str(SCRIPT),
        "--base",
        "BASE",
    ]


def test_lint_workflow_runs_check_on_pull_requests_with_merge_base():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    jobs = workflow["jobs"]
    assert "lint" in jobs["ci-gate"]["needs"]
    steps = jobs["lint"]["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["fetch-depth"] == 2
    guard = next(step for step in steps if "lint-data-files" in step.get("run", ""))
    assert guard["run"] == "scripts/ci.sh lint-data-files --base HEAD^1"
    assert "github.event_name=='pull_request'" in guard["if"].replace(" ", "")
    hygiene = next(
        i for i, step in enumerate(steps) if step.get("run") == "scripts/ci.sh lint-hygiene"
    )
    assert steps.index(guard) > hygiene


def test_stream_allowlist_rule_without_subprocess(monkeypatch, capsys):
    def git(*args):
        return b"benchmarks/data.jsonl\0" if args[0] == "diff" else b"head\n"

    monkeypatch.setattr(check_data_files, "_git", git)
    assert check_data_files._check("base") == 0
    assert "Changed paths: 1" in capsys.readouterr().out


def test_nul_split_preserves_complete_names_without_subprocess(monkeypatch, capsys):
    bad_name = "tests/caf\u00e9 long file.jsonl"

    def git(*args):
        if args[0] == "diff":
            return f"{bad_name}\0tests/good.txt\0".encode()
        return b"head\n"

    monkeypatch.setattr(check_data_files, "_git", git)
    assert check_data_files._check("base") == 1
    output = capsys.readouterr().out
    assert bad_name in output
    assert "Changed paths: 2" in output


def test_invalid_base_returns_two_without_subprocess(monkeypatch, capsys):
    def fail(*_args):
        raise check_data_files.GitError("invalid reference")

    monkeypatch.setattr(check_data_files, "_git", fail)
    monkeypatch.setattr(sys, "argv", ["check_data_files.py", "--base", "not-a-reference"])
    assert check_data_files.main() == 2
    assert "not-a-reference" in capsys.readouterr().err


def test_default_merge_uses_first_parent_without_subprocess(monkeypatch):
    def git(*args):
        assert args == ("rev-list", "--parents", "-n", "1", "HEAD")
        return b"head first-parent second-parent\n"

    monkeypatch.setattr(check_data_files, "_git", git)
    assert check_data_files._resolve_base(None) == "first-parent"


@pytest.mark.parametrize("ref", ["origin/dev/lionagi", "origin/main"])
def test_default_non_merge_falls_back_to_available_ref_without_subprocess(monkeypatch, ref):
    def git(*args):
        if args[0] == "rev-list":
            return b"head parent\n"
        if args == ("merge-base", "HEAD", ref):
            return b"resolved\n"
        raise check_data_files.GitError("missing ref")

    monkeypatch.setattr(check_data_files, "_git", git)
    assert check_data_files._resolve_base(None) == "resolved"


def test_default_without_resolvable_base_names_attempts_without_subprocess(monkeypatch):
    def git(*args):
        if args[0] == "rev-list":
            return b"head parent\n"
        raise check_data_files.GitError("missing ref")

    monkeypatch.setattr(check_data_files, "_git", git)
    with pytest.raises(check_data_files.GitError, match="origin/dev/lionagi.*origin/main"):
        check_data_files._resolve_base(None)


def test_json_blob_size_boundary_and_exception_paths_without_subprocess(monkeypatch, capsys):
    sizes = {"tests/at-limit.json": 262144, "tests/over.json": 262145}
    paths = [*JSON_EXCEPTIONS, *sizes]

    def git(*args):
        if args[0] == "rev-parse":
            return b"head\n"
        if args[0] == "diff":
            return b"\0".join(path.encode() for path in paths) + b"\0"
        assert args[:2] == ("cat-file", "-s")
        assert args[2].startswith("HEAD:")
        return str(sizes[args[2][len("HEAD:") :]]).encode()

    monkeypatch.setattr(check_data_files, "_git", git)
    assert check_data_files._check("base") == 1
    output = capsys.readouterr().out
    assert "Changed paths: 6" in output
    assert "tests/over.json" in output and "262145" in output
    assert "tests/at-limit.json" not in output
    assert all(path not in output for path in JSON_EXCEPTIONS)
