"""SWE-bench host runner — drive the in-sandbox agent over real instances, then
score with the official deterministic oracle.

Flow per instance (all agent execution happens INSIDE a Daytona sandbox; the host
only orchestrates and scores):

  1. create sandbox from the lionagi snapshot, overlay this-branch wheel
  2. clone the instance repo at ``base_commit`` (detached HEAD)
  3. best-effort ``pip install -e .`` so the agent can actually run the repo's
     tests during ReAct (the held-out test_patch is NEVER given to the agent)
  4. run ``_sandbox_entry.py`` with the issue text as the instruction; stream its
     signals live (count tool calls), download result.json → model_patch
  5. tear the sandbox down

Then write predictions.jsonl and invoke the official ``swebench`` Docker harness
(oracle.py) — resolved iff model_patch + held-out test_patch makes FAIL_TO_PASS
pass and keeps PASS_TO_PASS passing. No LLM judge anywhere.

    uv run python benchmarks/orchestration/suites/swebench/sandbox_runner.py \
        --limit 1 --repo django/django --model deepseek/deepseek-chat
    # add --oracle to run the Docker evaluation (needs Docker + `uv pip install swebench`)

Requires DAYTONA_API_KEY + the provider key (DEEPSEEK_API_KEY / OPENAI_API_KEY).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parents[1]))  # benchmarks/orchestration on path

from load import load_tasks  # noqa: E402
from oracle import harness_available, resolved_map, run_evaluation, write_predictions  # noqa: E402

from lionagi.tools.daytona import DaytonaSandbox, ensure_snapshot  # noqa: E402

SNAPSHOT = "lionagi-bench-py312-v2"
WHEEL = _HERE.parents[2].parent / "dist" / "lionagi-0.26.14-py3-none-any.whl"
ENTRY = _HERE / "_sandbox_entry.py"
RESULTS = _HERE.parent.parent / "results" / "swebench"

# Provider → the env var the in-sandbox agent needs.
_PROVIDER_KEY = {
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
}

_INSTRUCTION = """\
Resolve this GitHub issue by editing the repository source. Apply the method from
your instructions: reproduce the failure, localize it, make the minimal fix, then
verify against your reproduction and the nearest existing tests. End with a real,
non-empty code edit in place.

GitHub issue:
{problem}
"""


def _provider_env(model: str) -> dict[str, str]:
    provider = model.split("/", 1)[0]
    var = _PROVIDER_KEY.get(provider)
    if not var:
        raise SystemExit(f"unknown provider in model spec {model!r}; add it to _PROVIDER_KEY")
    val = os.environ.get(var)
    if not val:
        raise SystemExit(f"{var} not set (needed for model {model!r})")
    return {var: val}


async def _create_sandbox(env: dict[str, str], *, delete_on_exit: bool, attempts: int = 10):
    """Create a sandbox, retrying through the org's transient resource-cap pressure.

    The Daytona tier caps total CPU AND total disk (e.g. 10 CPU). With N concurrent
    runs, a finishing sandbox's resources aren't released the instant its ``delete()``
    is issued, so a peer's ``create`` can momentarily exceed a cap and raise
    ``Total CPU limit`` / ``Total disk limit``, or simply time out under create load.
    All three are transient — they clear within seconds as deletes settle. Retry with
    backoff instead of consuming the instance (the bug that ate the sphinx tail in v1,
    and again in v5 once the refine loop made sandboxes longer-lived → higher disk
    peak). delete_on_exit frees the resource as peers finish, so waiting is correct.
    """
    retryable = ("CPU limit", "Total CPU", "disk limit", "Total disk", "timed out", "Timeout")
    delay = 5.0
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return await DaytonaSandbox.create(
                snapshot=SNAPSHOT, env=env, delete_on_exit=delete_on_exit
            )
        except Exception as e:  # noqa: BLE001 — only transient cap/timeout is retryable
            msg = str(e)
            if not any(s in msg for s in retryable):
                raise
            last = e
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 30.0)
    raise last if last else RuntimeError("sandbox create failed")


async def run_instance(
    task,
    *,
    model: str,
    max_extensions: int,
    install_repo: bool,
    keep_sandbox: bool = False,
    refine_rounds: int = 2,
    lion_system: bool = True,
    run_id: str = "adhoc",
) -> dict:
    """Run one SWE-bench instance in a sandbox, return a prediction record."""
    ctx = task.context
    env = _provider_env(model)
    t0 = time.monotonic()
    tool_calls = {"reader": 0, "editor": 0, "bash": 0, "search": 0}
    last_run = {"status": "?"}

    refine_log: list[str] = []  # v6: per-round gate verdicts (empty/no_repro/repro_red/...)
    raw_signals: list[dict] = []  # every @@SIG@@ event, kept for post-hoc analysis

    def on_out(buf=[""]):  # noqa: B006 — closure-local line buffer
        def _h(chunk: str) -> None:
            buf[0] += chunk
            while "\n" in buf[0]:
                line, buf[0] = buf[0].split("\n", 1)
                if line.startswith("@@SIG@@ "):
                    try:
                        o = json.loads(line[8:])
                    except Exception:  # noqa: S112 — skip malformed signal lines
                        continue
                    raw_signals.append(o)
                    # A tool call is any signal carrying an ActionRequest payload
                    # — the entry sink sets "fn" only for those (envelope-agnostic:
                    # works whether the bus emits MessageAdded or a typed signal).
                    if o.get("fn") in tool_calls:
                        tool_calls[o["fn"]] += 1
                    elif o.get("t") in ("RunEnd", "RunFailed", "Done"):
                        last_run["status"] = o.get("s", o.get("t"))
                    elif o.get("t") == "RefineGate":
                        refine_log.append(o.get("verdict", "?"))
                    elif o.get("t") == "RefineVerified":
                        refine_log.append("verified")

        return _h

    async with await _create_sandbox(env, delete_on_exit=not keep_sandbox) as sb:
        home = await sb.home_dir()
        repo = f"{home}/repo"

        # overlay this-branch lionagi (snapshot carries the released deps)
        await sb.upload_file(WHEEL, f"{home}/{WHEEL.name}")
        r = await sb.exec(
            f"pip install --no-deps --force-reinstall {home}/{WHEEL.name}", timeout=300
        )
        if not r.ok:
            return _err(task, model, f"wheel overlay failed: {r.stdout[-400:]}", t0)

        # clone the bug's repo at base_commit (detached HEAD == the patch base)
        await sb.clone(f"https://github.com/{ctx['repo']}.git", repo, commit=ctx["base_commit"])

        repo_installed = False
        if install_repo:
            ins = await sb.exec("pip install -e . -q", cwd=repo, timeout=900)
            repo_installed = ins.ok  # best-effort; agent still works from reading

        # upload the in-sandbox driver + its spec (keys travel in the spec file)
        await sb.upload_file(ENTRY, f"{home}/_sandbox_entry.py")
        spec = {
            "repo_path": repo,
            "model": model,
            "instruction": _INSTRUCTION.format(problem=task.prompt),
            "max_extensions": max_extensions,
            "refine_rounds": refine_rounds,
            "lion_system": lion_system,
            "result_path": f"{home}/result.json",
            "control_path": f"{home}/control",
            # INSIDE repo so the agent's workspace-confined editor can write it;
            # excluded from model_patch by name in _compute_diff (see _sandbox_entry).
            "repro_path": f"{repo}/_swebench_repro.py",
            "branch_path": f"{home}/branch.json",
            "env": env,
        }
        await sb.write_text(json.dumps(spec), f"{home}/spec.json")

        code = await sb.exec_stream(
            f"python {home}/_sandbox_entry.py {home}/spec.json",
            on_stdout=on_out(),
        )
        try:
            result = json.loads(await sb.read_text(f"{home}/result.json"))
        except Exception as e:
            return _err(task, model, f"no result.json (exit {code}): {e}", t0)

        # ALWAYS persist the full record locally — we already paid for this compute,
        # so every instance's complete branch (messages, ReAct rounds with their
        # extension_needed decisions, tool calls/results, usage) + raw signal stream +
        # the agent's repro are kept for post-hoc analysis without re-running.
        inst_dir = RESULTS / "artifacts" / run_id / ctx["instance_id"]
        inst_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(Exception):
            inst_dir.joinpath("branch.json").write_text(await sb.read_text(f"{home}/branch.json"))
        with contextlib.suppress(Exception):
            inst_dir.joinpath("repro.py").write_text(
                await sb.read_text(f"{repo}/_swebench_repro.py")
            )
        with contextlib.suppress(Exception):
            inst_dir.joinpath("signals.jsonl").write_text(
                "\n".join(json.dumps(s, default=str) for s in raw_signals)
            )
        with contextlib.suppress(Exception):
            inst_dir.joinpath("result.json").write_text(json.dumps(result, default=str))
        with contextlib.suppress(Exception):
            inst_dir.joinpath("patch.diff").write_text(result.get("diff", "") or "")

    patch = result.get("diff", "") or ""
    return {
        "instance_id": ctx["instance_id"],
        "repo": ctx["repo"],
        "model_name_or_path": model.replace("/", "__"),
        "model_patch": patch,
        "status": result.get("status", "?"),
        "usage": result.get("usage", {}),
        "repo_installed": repo_installed,
        "tool_calls": dict(tool_calls),
        "patch_bytes": len(patch),
        "refine": refine_log,  # v6: gate verdicts per round ([] = solved first try)
        "wall_seconds": round(time.monotonic() - t0, 1),
    }


def _err(task, model: str, msg: str, t0: float) -> dict:
    return {
        "instance_id": task.context["instance_id"],
        "repo": task.context["repo"],
        "model_name_or_path": model.replace("/", "__"),
        "model_patch": "",
        "status": f"runner error: {msg}",
        "usage": {},
        "tool_calls": {},
        "patch_bytes": 0,
        "wall_seconds": round(time.monotonic() - t0, 1),
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=1)
    ap.add_argument("--repo", default="django/django", help="filter to one repo (or 'all')")
    ap.add_argument("--instance", default=None, help="run only these instance_id(s), comma-sep")
    ap.add_argument("--model", default="deepseek/deepseek-chat")
    ap.add_argument("--max-extensions", type=int, default=30)
    ap.add_argument(
        "--refine-rounds",
        type=int,
        default=2,
        help="harness re-invokes ReAct up to N times while the diff stays empty (v5)",
    )
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument(
        "--holdout", type=int, default=0, help="run N held-out instances from full Verified-500"
    )
    ap.add_argument("--no-install", action="store_true", help="skip pip install -e . in sandbox")
    ap.add_argument(
        "--no-lion-system",
        action="store_true",
        help="omit the LION_SYSTEM_MESSAGE boilerplate (A/B the stale system prompt)",
    )
    ap.add_argument("--oracle", action="store_true", help="run the Docker swebench evaluation")
    ap.add_argument("--keep-sandbox", action="store_true")
    args = ap.parse_args()

    repos = None if args.repo == "all" else (args.repo,)
    if args.holdout:
        from load import load_holdout  # noqa: E402

        tasks = load_holdout(n=args.holdout, repos=repos)
    elif args.instance:
        wanted = {s.strip() for s in args.instance.split(",")}
        tasks = [t for t in load_tasks(repos=None) if t.context["instance_id"] in wanted]
    else:
        tasks = load_tasks(repos=repos, limit=args.limit)
    print(f"running {len(tasks)} instance(s), model={args.model}, concurrency={args.concurrency}")

    # Stable per-run id (timestamp + model) — names the preds file AND the
    # per-instance artifact tree (results/swebench/artifacts/{run_id}/...).
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{args.model.replace('/', '__')}-{stamp}"
    run_config = {
        "run_id": run_id,
        "model": args.model,
        "max_extensions": args.max_extensions,
        "refine_rounds": args.refine_rounds,
        "lion_system": not args.no_lion_system,
        "install_repo": not args.no_install,
        "concurrency": args.concurrency,
        "n_instances": len(tasks),
        "started_utc": stamp,
    }

    assert WHEEL.exists(), f"build the wheel first: uv build --wheel ({WHEEL})"
    await ensure_snapshot(SNAPSHOT)

    sem = asyncio.Semaphore(args.concurrency)

    # Daytona faults that strike MID-RUN (not just at create): the org tier drops
    # long-lived sessions under load, and v6's multi-round sandboxes live long
    # enough to trip this — it ate 16 sphinx in the v6 full-50. These are transient;
    # the whole instance is retryable on a FRESH sandbox (the agent is stateless
    # across sandboxes — it re-clones + re-runs from scratch).
    transient_run = (
        "Failed to get session",
        "Failed to execute",
        "Failed to clone",
        "Failed to upload",
        "Total CPU",
        "Total disk",
        "timed out",
        "Timeout",
    )

    async def _guarded(t, instance_retries: int = 2):
        async with sem:
            print(f"  ▶ {t.context['instance_id']} ({t.context['repo']}) …", flush=True)
            t0 = time.monotonic()
            p = None
            for attempt in range(instance_retries + 1):
                try:
                    p = await run_instance(
                        t,
                        model=args.model,
                        max_extensions=args.max_extensions,
                        install_repo=not args.no_install,
                        keep_sandbox=args.keep_sandbox,
                        refine_rounds=args.refine_rounds,
                        lion_system=not args.no_lion_system,
                        run_id=run_id,
                    )
                    break
                except Exception as e:  # noqa: BLE001 — one bad instance must not abort the batch
                    msg = f"{type(e).__name__}: {e}"
                    if attempt < instance_retries and any(s in msg for s in transient_run):
                        print(
                            f"    ↻ {t.context['instance_id']}: transient Daytona fault, "
                            f"retry {attempt + 1}/{instance_retries} — {msg[:60]}",
                            flush=True,
                        )
                        await asyncio.sleep(8.0 * (attempt + 1))
                        continue
                    p = _err(t, args.model, msg, t0)
                    print(f"    ✗ {p['instance_id']}: {p['status'][:80]}", flush=True)
                    return p
            tc = p.get("tool_calls", {})
            rf = p.get("refine", [])
            rf_str = f" refine[{'>'.join(rf)}]" if rf else ""
            print(
                f"    ✓ {p['instance_id']}: patch={p['patch_bytes']}B "
                f"tools(r{tc.get('reader', 0)}/e{tc.get('editor', 0)}/b{tc.get('bash', 0)}){rf_str} "
                f"installed={p.get('repo_installed')} {p['wall_seconds']}s — {p['status'][:60]}",
                flush=True,
            )
            return p

    preds = await asyncio.gather(*(_guarded(t) for t in tasks))

    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{run_id}.preds.json").write_text(json.dumps(preds, indent=2))
    n_patched = sum(1 for p in preds if p["patch_bytes"] > 0)
    # Run manifest alongside the per-instance artifact tree: config + aggregate
    # stats, so a run is fully self-describing without re-deriving anything.
    art_dir = RESULTS / "artifacts" / run_id
    art_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        **run_config,
        "n_patched": n_patched,
        "preds": [
            {
                "instance_id": p["instance_id"],
                "patch_bytes": p["patch_bytes"],
                "tool_calls": p.get("tool_calls", {}),
                "refine": p.get("refine", []),
                "status": p["status"],
                "usage": p.get("usage", {}),
                "wall_seconds": p.get("wall_seconds"),
            }
            for p in preds
        ],
    }
    (art_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    print(f"\n{n_patched}/{len(preds)} produced a non-empty patch")
    print(f"artifacts: {art_dir}/  (branch.json, signals.jsonl, patch.diff, repro.py per instance)")

    if not args.oracle:
        print("skipping oracle (pass --oracle to run the Docker evaluation).")
        print(f"predictions saved: {RESULTS / f'{run_id}.preds.json'}")
        return

    if not harness_available():
        print("swebench harness not installed — `uv pip install swebench`. Skipping scoring.")
        return

    pred_path = write_predictions(preds, RESULTS / f"{run_id}.jsonl")
    print(f"running official oracle on {pred_path} …")
    report = run_evaluation(pred_path, run_id=run_id, model_name=preds[0]["model_name_or_path"])
    rmap = resolved_map(report)
    n_resolved = sum(rmap.values())
    print("\n" + "=" * 70)
    print(f"RESOLVED: {n_resolved}/{len(rmap)}  ({100 * n_resolved / max(1, len(rmap)):.1f}%)")
    for p in preds:
        mark = "✓" if rmap.get(p["instance_id"]) else "✗"
        print(f"  {mark} {p['instance_id']}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
