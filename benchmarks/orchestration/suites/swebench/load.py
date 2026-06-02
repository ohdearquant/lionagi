"""SWE-bench Verified Mini loader — REAL bugs, DETERMINISTIC oracle.

Why this replaces the synthetic mutation suite: a hand-planted defect judged by
an LLM measures almost nothing (the planter knows the answer, the judge can be
led, a single defect saturates). SWE-bench is the opposite on every axis:

  - REAL: 50 actual GitHub issues from django (25) + sphinx (25), each with a
    developer gold patch. Post-2019 code, largely past cheap-model training
    cutoffs → less contamination than Defects4J/BugsInPy (arxiv 2411.13323).
  - DETERMINISTIC ORACLE: no LLM judge anywhere. An instance is "resolved" iff,
    after applying the agent's patch + the held-out ``test_patch`` at
    ``base_commit``, every ``FAIL_TO_PASS`` test passes and every ``PASS_TO_PASS``
    test still passes. Scoring is the official ``swebench`` harness in Docker.
  - MARKET-LEGIBLE: "X% on SWE-bench Verified Mini" is directly comparable to
    Devin / SWE-agent / the HAL leaderboard. The Mini-50 has the same difficulty
    distribution as the full Verified-500 at ~5GB instead of ~130GB of images.
  - DISCRIMINATIVE: cheap models resolve ~20-40%, strong agents ~70%+. A 30-50pp
    spread is exactly the measurement headroom the synthetic task lacked.

Source: huggingface.co/datasets/MariusHobbhahn/swe-bench-verified-mini

This module only LOADS + caches the data (no Docker needed). The runner produces
patches; the oracle (oracle.py) runs the Docker evaluation.
"""

from __future__ import annotations

import json
from pathlib import Path

from harness.task import Task

DATASET = "MariusHobbhahn/swe-bench-verified-mini"
_CACHE = Path(__file__).resolve().parent / "data" / "instances.json"


def _parse_list(v) -> list[str]:
    """FAIL_TO_PASS / PASS_TO_PASS arrive as JSON-encoded list strings."""
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v:
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return [v]
    return []


def fetch_and_cache() -> list[dict]:
    """Download the 50 instances from HuggingFace and cache locally as JSON.
    Idempotent: re-reads the cache if present (no network)."""
    if _CACHE.exists():
        return json.loads(_CACHE.read_text())
    from datasets import load_dataset  # benchmark-only dep

    ds = load_dataset(DATASET, split="test")
    rows = [dict(r) for r in ds]
    _CACHE.parent.mkdir(parents=True, exist_ok=True)
    _CACHE.write_text(json.dumps(rows, indent=2))
    return rows


def load_tasks(repos: tuple[str, ...] | None = None, limit: int | None = None) -> list[Task]:
    """SWE-bench instances as benchmark-agnostic Tasks.

    ``labels`` is empty — the ground truth is the test oracle, not a label.
    Everything the runner (clone+edit) and oracle (apply+test) need lives in
    ``context``. ``prompt`` is the issue text the agent must resolve.
    """
    rows = fetch_and_cache()
    if repos:
        rows = [r for r in rows if r["repo"] in repos]
    if limit:
        rows = rows[:limit]
    tasks: list[Task] = []
    for r in rows:
        tasks.append(
            Task(
                id=r["instance_id"],
                prompt=r["problem_statement"],
                labels=(),
                context={
                    "suite": "swebench",
                    "instance_id": r["instance_id"],
                    "repo": r["repo"],
                    "base_commit": r["base_commit"],
                    "environment_setup_commit": r.get("environment_setup_commit"),
                    "version": r.get("version"),
                    "test_patch": r["test_patch"],
                    "gold_patch": r["patch"],
                    "fail_to_pass": _parse_list(r.get("FAIL_TO_PASS")),
                    "pass_to_pass": _parse_list(r.get("PASS_TO_PASS")),
                    "hints_text": r.get("hints_text", ""),
                },
            )
        )
    return tasks


_FULL_DATASET = "princeton-nlp/SWE-bench_Verified"
_HOLDOUT_CACHE = Path(__file__).resolve().parent / "data" / "holdout.json"


def load_holdout(
    n: int = 20, repos: tuple[str, ...] | None = None, seed_offset: int = 0
) -> list[Task]:
    """A held-out slice from the full Verified-500, EXCLUDING the Mini-50.

    Overfit guard for the optimization loop: tune the harness on the Mini-50, then
    confirm the gain holds here on instances never seen during tuning. Same Task
    shape and oracle as load_tasks. Deterministic (no RNG): takes the first ``n``
    full-Verified instances whose id is not in the Mini-50, after ``seed_offset``.
    """
    mini_ids = {r["instance_id"] for r in fetch_and_cache()}
    if _HOLDOUT_CACHE.exists():
        rows = json.loads(_HOLDOUT_CACHE.read_text())
    else:
        from datasets import load_dataset  # benchmark-only dep

        rows = [dict(r) for r in load_dataset(_FULL_DATASET, split="test")]
        _HOLDOUT_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _HOLDOUT_CACHE.write_text(json.dumps(rows))
    pool = [r for r in rows if r["instance_id"] not in mini_ids]
    if repos:
        pool = [r for r in pool if r["repo"] in repos]
    pool = pool[seed_offset : seed_offset + n]
    return [
        Task(
            id=r["instance_id"],
            prompt=r["problem_statement"],
            labels=(),
            context={
                "suite": "swebench",
                "instance_id": r["instance_id"],
                "repo": r["repo"],
                "base_commit": r["base_commit"],
                "environment_setup_commit": r.get("environment_setup_commit"),
                "version": r.get("version"),
                "test_patch": r["test_patch"],
                "gold_patch": r["patch"],
                "fail_to_pass": _parse_list(r.get("FAIL_TO_PASS")),
                "pass_to_pass": _parse_list(r.get("PASS_TO_PASS")),
                "hints_text": r.get("hints_text", ""),
            },
        )
        for r in pool
    ]


if __name__ == "__main__":
    tasks = load_tasks()
    print(f"loaded {len(tasks)} SWE-bench Verified Mini tasks")
    from collections import Counter

    print("repos:", Counter(t.context["repo"] for t in tasks))
    t0 = tasks[0]
    print(f"\nexample: {t0.id}  ({t0.context['repo']} @ {t0.context['base_commit'][:10]})")
    print(f"  FAIL_TO_PASS: {len(t0.context['fail_to_pass'])} tests")
    print(f"  PASS_TO_PASS: {len(t0.context['pass_to_pass'])} tests")
    print(f"  problem: {t0.prompt[:160]}…")
