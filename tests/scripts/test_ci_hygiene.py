"""Behavioral tests for the publication-hygiene command."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("rg") is None,
    reason="ripgrep required for publication-hygiene self-tests",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_SCRIPT = REPO_ROOT / "scripts" / "ci.sh"
NOTEBOOK_HYGIENE_SCRIPT = REPO_ROOT / "scripts" / "lint_notebook_hygiene.py"
PY_HYGIENE_SCRIPT = REPO_ROOT / "scripts" / "lint_python_hygiene.py"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


@pytest.fixture
def public_repo(tmp_path: Path) -> Path:
    for directory in ("scripts", "docs", "notebooks", "cookbooks"):
        (tmp_path / directory).mkdir()
    shutil.copy2(CI_SCRIPT, tmp_path / "scripts" / "ci.sh")
    shutil.copy2(NOTEBOOK_HYGIENE_SCRIPT, tmp_path / "scripts" / "lint_notebook_hygiene.py")
    shutil.copy2(PY_HYGIENE_SCRIPT, tmp_path / "scripts" / "lint_python_hygiene.py")
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


def test_push_trigger_has_no_notebook_paths_ignore() -> None:
    # Regression: GitHub Actions skips an entire workflow run when
    # every changed path matches paths-ignore. A push trigger that ignores
    # notebooks would bypass the lint job (and its publication-hygiene scan)
    # for a notebook-only push to main/develop.
    workflow = CI_WORKFLOW.read_text()
    push_block = workflow.split("  push:", 1)[1].split("  pull_request:", 1)[0]
    push_keys = {line.strip() for line in push_block.splitlines()}

    assert not any(key.startswith("paths-ignore:") for key in push_keys)
    assert "*.ipynb" not in push_block


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


@pytest.mark.parametrize(
    "source",
    [
        "transform = lambda:x + 1\n",
        "factory = lambda:{}\n",
        "normalize = lambda:item.strip()\n",
    ],
)
@pytest.mark.parametrize(
    "relative_path",
    [
        Path("docs/example.py"),
        Path("notebooks/example.py"),
        Path("cookbooks/example.py"),
    ],
)
def test_python_lambda_without_space_in_python_source_is_accepted(
    public_repo: Path, relative_path: Path, source: str
) -> None:
    # Regression for #2149: the .py handling added to close the gap must not
    # trip on Python's own zero-arg `lambda:` closure syntax written as real
    # code (as opposed to a leaked namespace identifier in a comment/string).
    (public_repo / relative_path).write_text(source)

    result = _run_hygiene(public_repo)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "source",
    [
        "# assigned per lambda:leo direction\ndef f():\n    return 1\n",
        'def send():\n    return dict(to="lambda:leo", subject="hi")\n',
        '"""Docstring narrating work owned by lambda:sample-unit."""\n',
    ],
)
def test_internal_identifier_in_python_source_is_rejected(public_repo: Path, source: str) -> None:
    # Regression for #2149: a leaked internal namespace identifier in a .py
    # comment, string literal, or docstring under docs/notebooks/cookbooks
    # must fail the gate -- previously the `-g '!*.py'` exclusion let it
    # through silently with no replacement scan.
    (public_repo / "cookbooks" / "example.py").write_text(source)

    result = _run_hygiene(public_repo)

    assert result.returncode != 0
    assert "internal namespace identifiers" in result.stdout


def test_python_lambda_in_fstring_replacement_field_is_accepted(public_repo: Path) -> None:
    (public_repo / "cookbooks" / "example.py").write_text('value = f"{(lambda:x + 1)()}"\n')

    result = _run_hygiene(public_repo)

    assert result.returncode == 0, result.stdout + result.stderr


def test_internal_identifier_in_fstring_literal_segment_is_rejected(
    public_repo: Path,
) -> None:
    (public_repo / "cookbooks" / "example.py").write_text(
        'value = f"assigned to lambda:sample-unit: {1}"\n'
    )

    result = _run_hygiene(public_repo)

    assert result.returncode != 0
    assert "internal namespace identifiers" in result.stdout


@pytest.mark.parametrize(
    "content",
    [
        "as Ocean confirmed the plan\n",
        "**R4 (Ocean-level):** confirmed without escalation.\n",
        "directed separately by Ocean.\n",
    ],
)
def test_bare_founder_name_mention_is_rejected(public_repo: Path, content: str) -> None:
    # Regression for #2150: the founder-name check previously only matched
    # the possessive "Ocean's" and missed bare mentions -- exactly the leak
    # shape #2115 hand-fixed throughout docs/_archive/.
    (public_repo / "docs" / "example.md").write_text(content)

    result = _run_hygiene(public_repo)

    assert result.returncode != 0
    assert "founder-name process narration" in result.stdout


def test_possessive_founder_name_mention_is_still_rejected(public_repo: Path) -> None:
    (public_repo / "docs" / "example.md").write_text("per Ocean's direction\n")

    result = _run_hygiene(public_repo)

    assert result.returncode != 0
    assert "founder-name process narration" in result.stdout


@pytest.mark.parametrize(
    "content",
    [
        "documented in Ocean's notes on the matter\n",
        "this reflects Ocean's opinion\n",
        "captures Ocean's take on the design\n",
    ],
)
def test_possessive_founder_name_mention_with_non_whitelisted_noun_is_rejected(
    public_repo: Path, content: str
) -> None:
    # Regression: an earlier change narrowed the possessive branch to a 12-word
    # noun whitelist (directive/direction/decision/.../mandate), so leaks like
    # "Ocean's notes"/"Ocean's opinion" silently passed. The possessive form
    # must stay a catch-all regardless of the noun that follows.
    (public_repo / "docs" / "example.md").write_text(content)

    result = _run_hygiene(public_repo)

    assert result.returncode != 0
    assert "founder-name process narration" in result.stdout


def test_founder_actor_narration_with_public_name_on_same_line_is_rejected(
    public_repo: Path,
) -> None:
    (public_repo / "docs" / "example.md").write_text(
        "Ocean approved the plan after Haiyang reviewed it.\n"
    )

    result = _run_hygiene(public_repo)

    assert result.returncode != 0
    assert "founder-name process narration" in result.stdout


def test_geographic_ocean_prose_is_accepted(public_repo: Path) -> None:
    (public_repo / "docs" / "example.md").write_text("Pacific Ocean currents are studied here.\n")

    result = _run_hygiene(public_repo)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "content",
    [
        "lionagi: author Haiyang (Ocean) Li\n",
        "Author: Haiyang Li - Ocean\n",
        "copyright: Copyright &copy; 2024-2026 Ocean Li and LionAGI Contributors\n",
    ],
)
def test_founder_public_byline_is_not_rejected(public_repo: Path, content: str) -> None:
    # False-positive guard for #2150: the founder's own public credit lines
    # (author bylines, the mkdocs.yml copyright notice) must keep passing
    # once the check widens past the possessive-only pattern.
    (public_repo / "docs" / "example.md").write_text(content)

    result = _run_hygiene(public_repo)

    assert result.returncode == 0, result.stdout + result.stderr


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
