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
    """Create a sandbox, retrying through the org's transient CPU-cap pressure.

    The Daytona tier caps total CPU (e.g. 10). With N concurrent runs, a finishing
    sandbox's CPU isn't released the instant its ``delete()`` is issued, so a peer's
    ``create`` can momentarily exceed the cap and raise ``Total CPU limit``. That is
    transient — it clears within seconds as the delete settles. Retry with backoff
    instead of consuming the instance (the bug that ate the sphinx tail in v1).
    """
    delay = 5.0
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return await DaytonaSandbox.create(
                snapshot=SNAPSHOT, env=env, delete_on_exit=delete_on_exit
            )
        except Exception as e:  # noqa: BLE001 — only CPU-cap is retryable; re-raise others
            if "CPU limit" not in str(e) and "Total CPU" not in str(e):
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
) -> dict:
    """Run one SWE-bench instance in a sandbox, return a prediction record."""
    ctx = task.context
    env = _provider_env(model)
    t0 = time.monotonic()
    tool_calls = {"reader": 0, "editor": 0, "bash": 0, "search": 0}
    last_run = {"status": "?"}

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
                    if o.get("t") == "ActionRequestSignal" and o.get("fn") in tool_calls:
                        tool_calls[o["fn"]] += 1
                    elif o.get("t") in ("RunEnd", "RunFailed", "Done"):
                        last_run["status"] = o.get("s", o.get("t"))

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
            "result_path": f"{home}/result.json",
            "control_path": f"{home}/control",
            "messages_path": f"{home}/messages.json",
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

        if keep_sandbox:  # diagnosis: pull the full conversation locally
            with contextlib.suppress(Exception):
                dbg = RESULTS / "debug"
                dbg.mkdir(parents=True, exist_ok=True)
                txt = await sb.read_text(f"{home}/messages.json")
                out_path = dbg / f"{ctx['instance_id']}.messages.json"
                out_path.write_text(txt)
                print(f"    📝 messages → {out_path}", flush=True)

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
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument(
        "--holdout", type=int, default=0, help="run N held-out instances from full Verified-500"
    )
    ap.add_argument("--no-install", action="store_true", help="skip pip install -e . in sandbox")
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

    assert WHEEL.exists(), f"build the wheel first: uv build --wheel ({WHEEL})"
    await ensure_snapshot(SNAPSHOT)

    sem = asyncio.Semaphore(args.concurrency)

    async def _guarded(t):
        async with sem:
            print(f"  ▶ {t.context['instance_id']} ({t.context['repo']}) …", flush=True)
            t0 = time.monotonic()
            try:
                p = await run_instance(
                    t,
                    model=args.model,
                    max_extensions=args.max_extensions,
                    install_repo=not args.no_install,
                    keep_sandbox=args.keep_sandbox,
                )
            except Exception as e:  # noqa: BLE001 — one bad instance must not abort the batch
                p = _err(t, args.model, f"{type(e).__name__}: {e}", t0)
                print(f"    ✗ {p['instance_id']}: {p['status'][:80]}", flush=True)
                return p
            tc = p.get("tool_calls", {})
            print(
                f"    ✓ {p['instance_id']}: patch={p['patch_bytes']}B "
                f"tools(r{tc.get('reader', 0)}/e{tc.get('editor', 0)}/b{tc.get('bash', 0)}) "
                f"installed={p.get('repo_installed')} {p['wall_seconds']}s — {p['status'][:60]}",
                flush=True,
            )
            return p

    preds = await asyncio.gather(*(_guarded(t) for t in tasks))

    RESULTS.mkdir(parents=True, exist_ok=True)
    run_id = f"swebench-{args.model.replace('/', '__')}-n{len(preds)}"
    (RESULTS / f"{run_id}.preds.json").write_text(json.dumps(preds, indent=2))
    n_patched = sum(1 for p in preds if p["patch_bytes"] > 0)
    print(f"\n{n_patched}/{len(preds)} produced a non-empty patch")

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
