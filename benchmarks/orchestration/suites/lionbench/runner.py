"""lionbench runner — drive each configured adapter over each instance, score
with the held-out oracle, report per-(subject, adapter) pass rates with Wilson
CIs and a contamination (clean-subset) split.

    uv run python -m benchmarks.orchestration.suites.lionbench.runner \\
        --manifest-dir benchmarks/orchestration/suites/lionbench/data \\
        --adapters lionagi,claude,codex --model deepseek/deepseek-chat \\
        --cutoffs cutoffs.json

Flow per (instance, adapter) — all inside one Daytona sandbox:
  1. create sandbox from the lionbench snapshot (``image.SNAPSHOT_NAME``)
  2. clone the instance's repo at ``base_commit`` (detached HEAD)
  3. best-effort ``pip install -e .``
  4. run the adapter → unified diff (+ raw token/tool-call counts where the
     adapter can report them — first-class output for a future repeat-class
     efficiency analysis, not just pass/fail)
  5. apply the held-out ``test_patch`` (never shown to the adapter before this)
  6. run the oracle command, score pass/fail on exit code
  7. persist patch.diff / oracle_output.txt / result.json per (subject,
     instance, adapter)

Requires DAYTONA_API_KEY + whichever provider keys the configured adapters need.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE), str(_HERE.parents[1])):  # self dir + benchmarks/orchestration
    if _p not in sys.path:
        sys.path.insert(0, _p)

from adapters import ADAPTER_REGISTRY  # noqa: E402
from arms import ArmConfig, build_arm, injection_manifest, reset_record  # noqa: E402
from harness.stats import wilson  # noqa: E402 — reuse, don't reinvent (AEP)
from image import SNAPSHOT_NAME, lionbench_image  # noqa: E402
from schema import Instance, list_subjects, usable_instances  # noqa: E402

from lionagi.tools.daytona import DaytonaSandbox, ensure_snapshot  # noqa: E402

RESULTS = _HERE.parent.parent / "results" / "lionbench"


async def _create_sandbox(
    snapshot: str, env: dict[str, str], *, delete_on_exit: bool, attempts: int = 10
):
    """Create a sandbox, retrying through the org's transient resource-cap pressure.

    Same retry shape as ``suites/swebench/sandbox_runner._create_sandbox`` — copied
    rather than imported because that function hardcodes its own module-level
    ``SNAPSHOT`` constant and isn't parameterizable by snapshot name. See that
    file's docstring for why the retryable set is what it is (org tier CPU/disk
    caps clear within seconds as concurrent peers' deletes settle)."""
    retryable = ("CPU limit", "Total CPU", "disk limit", "Total disk", "timed out", "Timeout")
    delay = 5.0
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return await DaytonaSandbox.create(
                snapshot=snapshot, env=env, delete_on_exit=delete_on_exit
            )
        except Exception as e:  # noqa: BLE001 — only transient cap/timeout is retryable
            msg = str(e)
            if not any(s in msg for s in retryable):
                raise
            last = e
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 30.0)
    raise last if last else RuntimeError("sandbox create failed")


async def strip_self_leak(sandbox, workdir: str) -> bool:
    """Unconditionally remove ``bench/`` from a just-checked-out workspace.

    Layout ruling (bench-v0 design contract §11 addendum): instances live in
    per-repo PUBLIC ``bench/`` folders. Any ``base_commit`` at/after a repo's
    ``bench/`` folder existing has the instance corpus — including this very
    instance's own ``gold_patch`` — sitting INSIDE the agent's checkout. There
    is no clean "is this instance affected" ordering test worth trusting (a
    repo could rename/move the folder, backfill it, etc.), so the guard runs
    unconditionally on every instance rather than trying to reason about which
    ones are safe. Returns whether ``bench/`` was actually present (for
    logging), not whether the instance needed it."""
    check = await sandbox.exec(f"test -d {workdir}/bench && echo yes || echo no", cwd=workdir)
    present = check.stdout.strip() == "yes"
    if present:
        await sandbox.exec(f"rm -rf {workdir}/bench", cwd=workdir)
    return present


async def run_one(
    instance: Instance, adapter_name: str, adapter, *, run_id: str, arm: ArmConfig | None = None
) -> dict:
    """Run one (instance, adapter) cell inside a fresh sandbox; return a scored record.

    ``arm`` is the khive-injection bench arm (M0/M1/M2, see ``arms.py``). When
    set, the result gains an ``injection`` block: ``providers_fired`` reads
    whatever per-turn ``ProviderReport`` data the adapter surfaces via
    ``last_context_reports`` (``[]`` until the sandbox entry wires a
    ContextProviderRegistry onto its agent branch — an honest gap, same class
    as ``last_usage``/``last_tool_calls`` being ``{}`` for the CLI-harness
    adapters today). M2 additionally requires a caller-supplied namespace
    reset outcome (there is no verified khive verb for the reset yet, so the
    default here records it as NOT performed — ``reset_ok=False`` — which
    correctly forces ``injection_effective=False`` rather than silently
    scoring an unisolated M2 run as clean; see ``arms.reset_record``)."""
    t0 = time.monotonic()
    art_dir = RESULTS / run_id / instance.subject / instance.instance_id / adapter_name
    art_dir.mkdir(parents=True, exist_ok=True)

    async with await _create_sandbox(SNAPSHOT_NAME, {}, delete_on_exit=True) as sb:
        home = await sb.home_dir()
        workdir = f"{home}/repo"
        await sb.clone(
            f"https://github.com/{instance.repo}.git", workdir, commit=instance.base_commit
        )
        await strip_self_leak(sb, workdir)
        await sb.exec("pip install -e . -q", cwd=workdir, timeout=900)

        try:
            diff = await adapter.run(sb, instance, workdir)
        except Exception as e:  # noqa: BLE001 — a failed adapter run is a scored failure, not a crash
            diff = ""
            (art_dir / "adapter_error.txt").write_text(f"{type(e).__name__}: {e}")

        (art_dir / "patch.diff").write_text(diff or "")

        # Apply the held-out test_patch only NOW — never shown to the adapter.
        test_patch_path = f"{home}/_lionbench_test.patch"
        await sb.write_text(instance.oracle.test_patch, test_patch_path)
        apply_r = await sb.exec(f"git apply --whitespace=nowarn {test_patch_path}", cwd=workdir)

        oracle_r = await sb.exec(instance.oracle.command, cwd=workdir, timeout=600)
        (art_dir / "oracle_output.txt").write_text(oracle_r.stdout)

        passed = apply_r.ok and oracle_r.ok
        result = {
            "instance_id": instance.instance_id,
            "subject": instance.subject,
            "adapter": adapter_name,
            "repo": instance.repo,
            "merged_at": instance.merged_at,
            "passed": passed,
            "test_patch_applied": apply_r.ok,
            "oracle_exit_code": oracle_r.exit_code,
            "patch_bytes": len(diff or ""),
            "wall_seconds": round(time.monotonic() - t0, 1),
            # Raw efficiency signal, kept even though v0 only scores pass/fail —
            # first-class output for a future repeat-class analysis.
            "usage": getattr(adapter, "last_usage", {}),
            "tool_calls": getattr(adapter, "last_tool_calls", {}),
        }
        if arm is not None:
            reset = None
            if arm.name == "M2":
                reset = reset_record(
                    arm,
                    ok=False,
                    detail=(
                        "namespace reset verb not yet wired into the runner (open "
                        "dependency, INJECTION_DESIGN.md §9 R2) — this M2 run is not "
                        "namespace-isolated from the previous instance"
                    ),
                )
            reports = getattr(adapter, "last_context_reports", [])
            result["injection"] = injection_manifest(arm, reports, reset=reset)
        (art_dir / "result.json").write_text(json.dumps(result, indent=2))
        return result


# ---------------------------------------------------------------------------
# Pure aggregation — no sandbox, no network. Fully unit-testable.
# ---------------------------------------------------------------------------


def enforce_derivation_split(instances: list[Instance]) -> tuple[list[Instance], list[dict]]:
    """Instances sharing a ``source_pr`` (a fix instance, a diagnosis instance, a
    long-horizon composite, ... all derived from the SAME merged PR) must never
    co-occur in one eval run — solving one leaks the others. Deterministically
    keep exactly one per ``source_pr`` group (lexicographically smallest
    ``instance_id`` — stable across runs) and report every exclusion instead of
    silently dropping it.

    Instances with ``source_pr=None`` are never grouped against each other —
    ``None`` means "derivation not recorded", not "shares a derivation"."""
    by_source: dict[str, list[Instance]] = {}
    kept: list[Instance] = []
    for inst in instances:
        if inst.source_pr is None:
            kept.append(inst)
            continue
        by_source.setdefault(inst.source_pr, []).append(inst)

    excluded: list[dict] = []
    for source_pr, group in by_source.items():
        if len(group) == 1:
            kept.append(group[0])
            continue
        ordered = sorted(group, key=lambda i: i.instance_id)
        chosen = ordered[0]
        kept.append(chosen)
        for inst in ordered[1:]:
            excluded.append(
                {
                    "instance_id": inst.instance_id,
                    "source_pr": source_pr,
                    "excluded_reason": "derivation-split",
                    "kept_instead": chosen.instance_id,
                }
            )
    return kept, excluded


def contamination_split(results: list[dict], cutoffs: dict[str, str]) -> list[dict]:
    """The subset of ``results`` merged strictly after the result's adapter's
    model cutoff date (ISO ``YYYY-MM-DD``, keyed by adapter name in ``cutoffs``).
    Adapters absent from ``cutoffs`` contribute nothing to the clean subset
    (conservative: report full-set only)."""
    clean = []
    for r in results:
        cutoff = cutoffs.get(r["adapter"])
        merged_at = r.get("merged_at", "")
        if cutoff and merged_at and merged_at[:10] > cutoff:
            clean.append(r)
    return clean


def _proportion_block(rs: list[dict]) -> dict:
    k = sum(1 for r in rs if r["passed"])
    n = len(rs)
    p = wilson(k, n)
    return {"n": n, "k": k, "pass_rate": p.p, "ci95": [p.lo, p.hi]}


def summarize(results: list[dict], cutoffs: dict[str, str] | None = None) -> dict:
    """Pass counts + Wilson 95% CI, grouped by subject AND by adapter, full-set
    and clean-subset. Shape: ``{subject: {adapter: {..., clean_*}}}``."""
    cutoffs = cutoffs or {}
    clean_results = contamination_split(results, cutoffs)

    by_subject: dict[str, dict[str, list[dict]]] = {}
    clean_by_subject: dict[str, dict[str, list[dict]]] = {}
    for r in results:
        by_subject.setdefault(r["subject"], {}).setdefault(r["adapter"], []).append(r)
    for r in clean_results:
        clean_by_subject.setdefault(r["subject"], {}).setdefault(r["adapter"], []).append(r)

    summary: dict[str, dict] = {}
    for subject, by_adapter in by_subject.items():
        summary[subject] = {}
        for adapter, rs in by_adapter.items():
            block = _proportion_block(rs)
            clean_rs = clean_by_subject.get(subject, {}).get(adapter, [])
            if clean_rs:
                cblock = _proportion_block(clean_rs)
                block["clean_n"] = cblock["n"]
                block["clean_k"] = cblock["k"]
                block["clean_pass_rate"] = cblock["pass_rate"]
                block["clean_ci95"] = cblock["ci95"]
            else:
                block["clean_n"] = 0
                block["clean_k"] = 0
                block["clean_pass_rate"] = None
                block["clean_ci95"] = None
            summary[subject][adapter] = block
    return summary


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest-dir", default=str(_HERE / "data"))
    ap.add_argument(
        "--subjects", default=None, help="comma-separated subjects to run (default: all present)"
    )
    ap.add_argument("--adapters", default="lionagi", help="comma-separated: lionagi,claude,codex")
    ap.add_argument(
        "--model", default="deepseek/deepseek-chat", help="model for the lionagi adapter"
    )
    ap.add_argument("--cutoffs", default=None, help="path to a JSON {adapter: 'YYYY-MM-DD'} file")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--run-id", default=None)
    ap.add_argument(
        "--arm",
        choices=("M0", "M1", "M2"),
        default=None,
        help="khive-injection bench arm (INJECTION_DESIGN.md §3); omit to run with no "
        "injection wiring at all (distinct from M0, which records injection_effective=None)",
    )
    ap.add_argument(
        "--namespace", default=None, help="pinned khive namespace, required for --arm M1/M2"
    )
    args = ap.parse_args()

    arm = build_arm(args.arm, args.namespace) if args.arm else None
    if arm is not None:
        arm.assert_runnable()

    manifest_dir = Path(args.manifest_dir)
    subjects = (
        [s.strip() for s in args.subjects.split(",")]
        if args.subjects
        else list_subjects(manifest_dir)
    )
    instances: list[Instance] = []
    for subject in subjects:
        instances.extend(usable_instances(manifest_dir, subject=subject))

    instances, excluded = enforce_derivation_split(instances)
    if args.limit:
        instances = instances[: args.limit]
    if not instances:
        raise SystemExit(f"no validated instances found under {manifest_dir} (subjects={subjects})")

    adapter_names = [a.strip() for a in args.adapters.split(",")]
    adapters = {}
    for name in adapter_names:
        cls = ADAPTER_REGISTRY[name]
        adapters[name] = cls(args.model) if name == "lionagi" else cls()

    cutoffs = json.loads(Path(args.cutoffs).read_text()) if args.cutoffs else {}

    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print(
        f"running {len(instances)} instance(s) across {len(subjects)} subject(s) "
        f"x {len(adapters)} adapter(s), run_id={run_id}"
    )
    if excluded:
        out_dir = RESULTS / run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "derivation_split_excluded.json").write_text(json.dumps(excluded, indent=2))
        for e in excluded:
            print(
                f"  ⚠ derivation-split: excluding {e['instance_id']} (source_pr={e['source_pr']}, "
                f"kept {e['kept_instead']} instead)",
                flush=True,
            )

    await ensure_snapshot(SNAPSHOT_NAME, image=lionbench_image())

    sem = asyncio.Semaphore(args.concurrency)

    async def _guarded(instance: Instance, name: str, adapter):
        async with sem:
            print(f"  ▶ {instance.instance_id} / {name} …", flush=True)
            try:
                r = await run_one(instance, name, adapter, run_id=run_id, arm=arm)
            except Exception as e:  # noqa: BLE001 — one bad cell must not abort the batch
                r = {
                    "instance_id": instance.instance_id,
                    "subject": instance.subject,
                    "adapter": name,
                    "repo": instance.repo,
                    "merged_at": instance.merged_at,
                    "passed": False,
                    "error": f"{type(e).__name__}: {e}",
                }
            mark = "✓" if r.get("passed") else "✗"
            print(f"    {mark} {instance.instance_id} / {name}", flush=True)
            return r

    results = await asyncio.gather(
        *(_guarded(i, name, a) for i in instances for name, a in adapters.items())
    )

    summary = summarize(list(results), cutoffs)
    out_dir = RESULTS / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    (out_dir / "results.json").write_text(json.dumps(list(results), indent=2))

    print("\n" + "=" * 70)
    for subject, by_adapter in summary.items():
        print(f"[{subject}]")
        for adapter, s in by_adapter.items():
            clean = f" clean={s['clean_k']}/{s['clean_n']}" if s["clean_n"] else " clean=n/a"
            print(
                f"  {adapter}: {s['k']}/{s['n']} pass_rate={s['pass_rate']:.2f} "
                f"CI95=[{s['ci95'][0]:.2f},{s['ci95'][1]:.2f}]{clean}"
            )
    print("=" * 70)
    print(f"artifacts: {out_dir}/")


if __name__ == "__main__":
    asyncio.run(main())
