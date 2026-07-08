"""Both-direction local validation (DESIGN_CONTRACT §5) — no Daytona required.

This is the path CI/tests use, and the fallback the harvester uses when
``DAYTONA_API_KEY`` is unset: a plain ``git worktree`` of an already-cloned
repo checked out at ``base_commit``, patches applied with ``git apply``, the
oracle command run locally.

    1. gold_passes: base_commit + gold_patch + test_patch → oracle MUST pass.
    2. null_fails:  base_commit + test_patch only         → oracle MUST fail.

Every subprocess call here is a fixed argv (no shell), so it is safe to run
against untrusted patch text — the patch is fed on stdin, never interpolated
into a command string.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

_OUTPUT_TAIL = 4000


@dataclass(slots=True)
class OracleRun:
    passed: bool
    output: str


def _run(args: list[str], *, cwd: Path, input_text: str | None = None, timeout: int = 300):
    return subprocess.run(  # noqa: S603 — fixed argv, no shell
        args,
        cwd=str(cwd),
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _make_worktree(repo_dir: Path, base_commit: str) -> Path:
    """A throwaway detached worktree of ``repo_dir`` at ``base_commit``."""
    wt = repo_dir.parent / f"_lionbench_wt_{uuid.uuid4().hex[:10]}"
    r = _run(["git", "worktree", "add", "--detach", str(wt), base_commit], cwd=repo_dir)
    if r.returncode != 0:
        raise RuntimeError(f"worktree add failed at {base_commit[:10]}: {r.stderr[-800:]}")
    return wt


def _remove_worktree(repo_dir: Path, wt: Path) -> None:
    _run(["git", "worktree", "remove", "--force", str(wt)], cwd=repo_dir)
    shutil.rmtree(wt, ignore_errors=True)


def _apply_patches(wt: Path, patches: list[str]) -> None:
    for patch in patches:
        if not patch.strip():
            continue
        r = _run(["git", "apply", "--whitespace=nowarn", "-"], cwd=wt, input_text=patch)
        if r.returncode != 0:
            raise RuntimeError(f"git apply failed: {r.stderr[-800:]}")


def run_oracle_in_worktree(
    repo_dir: Path,
    base_commit: str,
    patches: list[str],
    command: str,
    *,
    timeout: int = 300,
) -> OracleRun:
    """Checkout ``base_commit`` in a fresh worktree, apply ``patches`` in order,
    run ``command`` (shell string — the oracle command, e.g. ``uv run pytest ...``)
    with cwd=worktree. Returns pass/fail + captured output. Worktree is always
    cleaned up."""
    wt = _make_worktree(repo_dir, base_commit)
    try:
        _apply_patches(wt, patches)
        r = subprocess.run(  # noqa: S602 — oracle command is a caller-supplied shell string by design
            command,
            shell=True,
            cwd=str(wt),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        out = (r.stdout + r.stderr)[-_OUTPUT_TAIL:]
        return OracleRun(passed=r.returncode == 0, output=out)
    finally:
        _remove_worktree(repo_dir, wt)


def validate_both_directions(
    repo_dir: Path,
    base_commit: str,
    gold_patch: str,
    test_patch: str,
    oracle_command: str,
    *,
    timeout: int = 300,
) -> dict:
    """Run both validation directions and return a dict matching schema.Validation
    fields (minus leak_review, which is a separate — partly manual — check)."""
    gold = run_oracle_in_worktree(
        repo_dir, base_commit, [gold_patch, test_patch], oracle_command, timeout=timeout
    )
    null = run_oracle_in_worktree(
        repo_dir, base_commit, [test_patch], oracle_command, timeout=timeout
    )
    return {
        "gold_passes": gold.passed,
        "null_fails": not null.passed,
        "gold_output": gold.output,
        "null_output": null.output,
    }
