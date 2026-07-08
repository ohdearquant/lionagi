"""Local-fallback validation test — a tiny real git repo in tmp_path, no network,
no Daytona. This is the path CI/tests use (DESIGN_CONTRACT §5): both directions
of a genuine gold_patch/test_patch pair against a genuine bug, checked with real
`git worktree`/`git apply`/pytest subprocess calls."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from validate import validate_both_directions  # noqa: E402

_BUGGY_CALC = "def add(a, b):\n    return a - b  # bug: should add\n"
_FIXED_CALC = "def add(a, b):\n    return a + b\n"
_TEST_ADD = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def _git(args: list[str], cwd: Path) -> None:
    r = subprocess.run(  # noqa: S603 — fixed argv, test fixture only
        ["git", *args],  # noqa: S607 — git resolved on PATH by design
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"


def _make_fixture_repo(tmp_path: Path) -> tuple[Path, str, str, str]:
    """A tiny repo with a real bug: base_commit has the bug, a later commit both
    fixes it and adds the regression test. Returns (repo_dir, base_commit,
    gold_patch, test_patch)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "lionbench@example.com"], repo)
    _git(["config", "user.name", "lionbench"], repo)

    (repo / "calc.py").write_text(_BUGGY_CALC)
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "base: buggy add"], repo)
    base_commit = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "HEAD"],  # noqa: S607 — git resolved on PATH by design
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    (repo / "calc.py").write_text(_FIXED_CALC)
    (repo / "test_calc.py").write_text(_TEST_ADD)
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "fix add + regression test"], repo)

    gold_patch = subprocess.run(  # noqa: S603
        ["git", "diff", f"{base_commit}..HEAD", "--", "calc.py"],  # noqa: S607
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    test_patch = subprocess.run(  # noqa: S603
        ["git", "diff", f"{base_commit}..HEAD", "--", "test_calc.py"],  # noqa: S607
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return repo, base_commit, gold_patch, test_patch


def test_gold_passes_and_null_fails_on_a_real_bug(tmp_path):
    repo, base_commit, gold_patch, test_patch = _make_fixture_repo(tmp_path)
    result = validate_both_directions(
        repo, base_commit, gold_patch, test_patch, f"{sys.executable} -m pytest test_calc.py -q"
    )
    assert result["gold_passes"] is True
    assert result["null_fails"] is True


def test_vacuous_test_patch_is_caught_by_null_check(tmp_path):
    """A test_patch that ALSO passes on the unedited base (doesn't exercise the
    bug) must fail the null_fails check — this is the instance-validation
    rejection path, exercised directly against the oracle mechanics."""
    repo, base_commit, gold_patch, _ = _make_fixture_repo(tmp_path)
    vacuous_test_patch = (
        "diff --git a/test_vacuous.py b/test_vacuous.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/test_vacuous.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def test_always_true():\n"
        "+    assert True\n"
    )
    result = validate_both_directions(
        repo,
        base_commit,
        gold_patch,
        vacuous_test_patch,
        f"{sys.executable} -m pytest test_vacuous.py -q",
    )
    assert result["gold_passes"] is True
    assert result["null_fails"] is False  # vacuous — passes even without the fix
