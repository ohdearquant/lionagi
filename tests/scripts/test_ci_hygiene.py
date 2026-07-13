"""Behavioral tests for the publication-hygiene command."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_SCRIPT = REPO_ROOT / "scripts" / "ci.sh"
NOTEBOOK_HYGIENE_SCRIPT = REPO_ROOT / "scripts" / "lint_notebook_hygiene.py"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


@pytest.fixture
def public_repo(tmp_path: Path) -> Path:
    for directory in ("scripts", "docs", "notebooks", "cookbooks"):
        (tmp_path / directory).mkdir()
    shutil.copy2(CI_SCRIPT, tmp_path / "scripts" / "ci.sh")
    shutil.copy2(NOTEBOOK_HYGIENE_SCRIPT, tmp_path / "scripts" / "lint_notebook_hygiene.py")
    return tmp_path


def _run_hygiene(repo: Path, **env_overrides: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(env_overrides)
    return subprocess.run(
        ["bash", "scripts/ci.sh", "lint-hygiene"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _write_notebook(path: Path, cell_type: str, source: str) -> None:
    path.write_text(
        json.dumps(
            {
                "cells": [
                    {
                        "cell_type": cell_type,
                        "metadata": {},
                        "source": [source],
                        **({"outputs": [], "execution_count": None} if cell_type == "code" else {}),
                    }
                ],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        )
    )


def test_ci_lint_job_runs_publication_hygiene() -> None:
    workflow = CI_WORKFLOW.read_text()
    assert "run: scripts/ci.sh lint-hygiene" in workflow


@pytest.mark.parametrize(
    "source",
    [
        "transform = lambda:x + 1\n",
        "factory = lambda:{}\n",
        "normalize = lambda:item.strip()\n",
    ],
)
def test_python_lambda_without_space_in_notebook_code_is_accepted(
    public_repo: Path, source: str
) -> None:
    _write_notebook(public_repo / "notebooks" / "example.ipynb", "code", source)

    result = _run_hygiene(public_repo)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "relative_path",
    [
        Path("notebooks/react.ipynb"),
        Path("cookbooks/example.ipynb"),
    ],
)
def test_machine_user_paths_in_public_notebooks_are_rejected(
    public_repo: Path, relative_path: Path
) -> None:
    _write_notebook(
        public_repo / relative_path,
        "code",
        "input_path = '/Users/example/project/input.txt'\n",
    )

    result = _run_hygiene(public_repo)

    assert result.returncode != 0
    assert "machine-local paths found" in result.stdout


def test_internal_identifier_in_notebook_markdown_is_rejected(public_repo: Path) -> None:
    _write_notebook(
        public_repo / "notebooks" / "example.ipynb",
        "markdown",
        "Assigned to lambda:sample-unit",
    )

    result = _run_hygiene(public_repo)

    assert result.returncode != 0
    assert "internal namespace identifiers" in result.stdout


def test_missing_ripgrep_fails_with_install_guidance(public_repo: Path) -> None:
    result = _run_hygiene(public_repo, RG_BIN="missing-ripgrep")

    assert result.returncode != 0
    assert "Install ripgrep" in result.stderr


def test_ripgrep_scanner_error_is_not_treated_as_no_matches(public_repo: Path) -> None:
    fake_bin = public_repo / "bin"
    fake_bin.mkdir()
    fake_rg = fake_bin / "rg"
    fake_rg.write_text("#!/usr/bin/env bash\nexit 2\n")
    fake_rg.chmod(0o755)

    result = _run_hygiene(public_repo, RG_BIN=str(fake_rg))

    assert result.returncode != 0
    assert "scanner error" in result.stderr
    assert "publication hygiene: ERROR" in result.stderr
