"""Unit tests for the runner's pure aggregation: contamination split + summarize
(Wilson CIs reused from harness/stats.py — checked here only at the seam, not
re-derived; harness/stats.py owns its own correctness tests)."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
from runner import (  # noqa: E402
    contamination_split,
    enforce_derivation_split,
    strip_self_leak,
    summarize,
)
from schema import Instance, OracleSpec  # noqa: E402


@dataclass
class _ExecResult:
    exit_code: int
    stdout: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class _RealShellSandbox:
    """Runs `exec` against a REAL local directory via subprocess — strip_self_leak
    only issues plain `test -d`/`rm -rf` shell commands, so this is a faithful
    stand-in for DaytonaSandbox.exec without touching Daytona/network at all."""

    async def exec(self, command: str, *, cwd: str | None = None, **_):
        r = subprocess.run(  # noqa: S602 — fixed test-only shell command, no untrusted input
            command, shell=True, cwd=cwd, capture_output=True, text=True, check=False
        )
        return _ExecResult(exit_code=r.returncode, stdout=r.stdout)


@pytest.mark.asyncio
async def test_strip_self_leak_removes_bench_dir_from_real_workspace(tmp_path):
    workdir = tmp_path / "repo"
    (workdir / "bench" / "lionagi").mkdir(parents=True)
    (workdir / "bench" / "lionagi" / "lionagi__1665.json").write_text("{}")
    (workdir / "lionagi" / "lndl").mkdir(parents=True)
    (workdir / "lionagi" / "lndl" / "lexer.py").write_text("# real source, unrelated")

    present = await strip_self_leak(_RealShellSandbox(), str(workdir))

    assert present is True
    assert not (workdir / "bench").exists()
    assert (workdir / "lionagi" / "lndl" / "lexer.py").exists()  # untouched


@pytest.mark.asyncio
async def test_strip_self_leak_is_a_noop_when_bench_absent(tmp_path):
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "lionagi").mkdir()

    present = await strip_self_leak(_RealShellSandbox(), str(workdir))

    assert present is False
    assert (workdir / "lionagi").exists()


def _r(instance_id, subject, adapter, passed, merged_at):
    return {
        "instance_id": instance_id,
        "subject": subject,
        "adapter": adapter,
        "passed": passed,
        "merged_at": merged_at,
    }


def test_contamination_split_keeps_only_post_cutoff_for_that_adapter():
    results = [
        _r("a__1", "lionagi", "claude", True, "2026-07-01T00:00:00Z"),
        _r("a__2", "lionagi", "claude", False, "2026-01-01T00:00:00Z"),
        _r("a__3", "lionagi", "codex", True, "2026-07-01T00:00:00Z"),
    ]
    cutoffs = {"claude": "2026-05-01"}  # codex has no cutoff entry
    clean = contamination_split(results, cutoffs)
    assert [r["instance_id"] for r in clean] == ["a__1"]


def test_contamination_split_empty_cutoffs_yields_empty():
    results = [_r("a__1", "lionagi", "claude", True, "2026-07-01T00:00:00Z")]
    assert contamination_split(results, {}) == []


def test_summarize_groups_by_subject_and_adapter():
    results = [
        _r("a__1", "lionagi", "claude", True, "2026-07-01T00:00:00Z"),
        _r("a__2", "lionagi", "claude", False, "2026-01-01T00:00:00Z"),
        _r("b__1", "rust-systems", "claude", True, "2026-07-01T00:00:00Z"),
        _r("b__2", "rust-systems", "codex", False, "2026-07-01T00:00:00Z"),
    ]
    summary = summarize(results, cutoffs={"claude": "2026-05-01"})

    assert set(summary.keys()) == {"lionagi", "rust-systems"}
    lionagi_claude = summary["lionagi"]["claude"]
    assert lionagi_claude["n"] == 2
    assert lionagi_claude["k"] == 1
    assert lionagi_claude["clean_n"] == 1  # only the post-cutoff instance
    assert lionagi_claude["clean_k"] == 1

    rust_codex = summary["rust-systems"]["codex"]
    assert rust_codex["n"] == 1
    assert rust_codex["k"] == 0
    assert rust_codex["clean_n"] == 0  # codex absent from cutoffs
    assert rust_codex["clean_pass_rate"] is None


def _inst(instance_id: str, source_pr: str | None) -> Instance:
    return Instance(
        instance_id=instance_id,
        repo="ohdearquant/lionagi",
        base_commit="deadbeef",
        task_text="x",
        oracle=OracleSpec(kind="pytest", held_out_paths=[], command="pytest", test_patch=""),
        gold_patch="diff",
        merged_at="2026-07-01T00:00:00Z",
        source_pr=source_pr,
    )


def test_derivation_split_keeps_one_and_reports_rest():
    instances = [
        _inst("lionagi__1843__fix", "lionagi#1843"),
        _inst("lionagi__1843__diagnosis", "lionagi#1843"),
        _inst("lionagi__1900", "lionagi#1900"),
    ]
    kept, excluded = enforce_derivation_split(instances)

    kept_ids = sorted(i.instance_id for i in kept)
    assert kept_ids == ["lionagi__1843__diagnosis", "lionagi__1900"]  # lexicographically smallest
    assert len(excluded) == 1
    assert excluded[0]["instance_id"] == "lionagi__1843__fix"
    assert excluded[0]["source_pr"] == "lionagi#1843"
    assert excluded[0]["kept_instead"] == "lionagi__1843__diagnosis"


def test_derivation_split_is_deterministic_across_call_order():
    a = enforce_derivation_split([_inst("z__1", "lionagi#1"), _inst("a__1", "lionagi#1")])
    b = enforce_derivation_split([_inst("a__1", "lionagi#1"), _inst("z__1", "lionagi#1")])
    assert [i.instance_id for i in a[0]] == [i.instance_id for i in b[0]] == ["a__1"]


def test_derivation_split_never_groups_none_source_pr_instances():
    instances = [_inst("a", None), _inst("b", None), _inst("c", None)]
    kept, excluded = enforce_derivation_split(instances)
    assert len(kept) == 3
    assert excluded == []


def test_summarize_ci_bounds_are_sane_probabilities():
    results = [
        _r(f"a__{i}", "lionagi", "claude", i % 2 == 0, "2026-07-01T00:00:00Z") for i in range(10)
    ]
    summary = summarize(results)
    block = summary["lionagi"]["claude"]
    lo, hi = block["ci95"]
    assert 0.0 <= lo <= block["pass_rate"] <= hi <= 1.0
