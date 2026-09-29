"""A whole SWE-bench set through the LNDL loop, one box per instance, graded in that box.

    uv run --with swebench --with daytona python bench/swe/run_set.py --set mini50 --where daytona \\
        --backend openrouter --model deepseek/deepseek-v4.1-flash --parallel 4 --run-id ds-mini50

`--where local` runs the eval image under docker on this machine (amd64 emulation: one at a time is
what it can bear) and streams images: pull, solve, grade, remove. `--where daytona` creates one
Daytona sandbox per instance from the same image, natively x86_64, `--parallel` wide; nothing lands
on this machine but the records. Grading happens inside the instance's own box either way
(sandbox.grade mirrors the official harness), so there is no second container and no oracle pass.

The ledger (one JSON line per instance) is the resumable state: instances already in it are
skipped, so a run can be stopped and restarted under the same run id. A run id is one run: its
`run.json` records the policy, model, backend and harness it was started with, and a launch that
differs, a legacy run whose manifests are missing a field or disagree, or a ledger row that does not
say it came from that run is refused before anything is written. Each launch adds its own
`manifest-<time>[-n].json`, never over another, pointing at `run.json`.

`--harness codex-cli` keeps the boxes, the model and the grading and swaps the loop: the codex CLI
runs inside the box as the agent, on the same model through OpenRouter, for the same time budget.

Spend nobody measured is unknown, never $0: a row's `cost_usd` is the known subtotal and
`cost_complete: false` says it is partial (None when nothing is known), a retried attempt's spend
stays on the row, and the summary adds only what is known and counts the rows that are not. A cost
cap on an arm with no meter is refused before any box. A run whose execution was not confirmed
stopped is not graded.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from codex_cli import solve as solve_codex_cli  # noqa: E402
from run_swe import HERE, RESULTS, solve  # noqa: E402
from sandbox import close_code_hosts, grade, open_code_hosts, testbed  # noqa: E402

from hub.tools import docker  # noqa: E402

SETS = {
    "mini50": HERE / "data" / "verified_mini50.json",
    "verified500": HERE / "data" / "verified_500.json",
    "local": HERE / "data" / "local_verified.json",
}


async def image_present(image: str) -> bool:
    out = await docker("image", "inspect", "--format", "{{.Id}}", image, check=False)
    return out.startswith("sha256:")


async def pull(image: str) -> None:
    out = await docker("pull", "--platform", "linux/amd64", image, check=False)
    if not await image_present(image):
        raise RuntimeError(f"pull failed for {image}: {out[-300:]}")


RUN_KEYS = ("unaided", "model", "backend", "harness")  # what makes two launches one run


def ledger_clash(ledger: Path, run: dict) -> list[str]:
    """The ledger rows that do not say they came from `run`, each with the field that differs or is
    missing: a resumed run must not count another run's row, nor a row that says nothing (a row from
    before the fields were written is not this run's because one manifest matches)."""
    if not ledger.exists():
        return []
    want = {
        "aided": not run["unaided"],
        "model": run["model"],
        "backend": "codex-cli" if run["harness"] == "codex-cli" else run["backend"],
    }
    out = []
    for r in (json.loads(line) for line in ledger.read_text().splitlines() if line.strip()):
        for k, v in want.items():
            if k not in r:
                out.append(f"{r['instance_id']} has no {k}")
                break
            if r[k] != v:
                out.append(f"{r['instance_id']} {k}={r[k]!r}")
                break
    return out


def _cost_known(r: dict) -> bool:
    """Whether a row's spend is wholly known: a row that never reached paid work has none to know."""
    if "cost_usd" not in r:
        return True
    return (
        r["cost_usd"] is not None
        and r.get("cost_complete") is not False
        and r.get("usage_complete") is not False
    )


def ledger_ids(ledger: Path) -> set[str]:
    if not ledger.exists():
        return set()
    return {json.loads(line)["instance_id"] for line in ledger.read_text().splitlines() if line.strip()}


async def one(row: dict, args: argparse.Namespace, out_dir: Path, ledger: Path, lock: asyncio.Lock) -> dict:
    iid = row["instance_id"]
    t0 = time.perf_counter()
    rec: dict = {"instance_id": iid, "resolved": False}
    harness = "codexcli" if args.harness == "codex-cli" else f"lndl-{args.backend}"
    model_name = f"lionagi-{harness}-{args.model.replace('/', '_')}"
    prior: str | None = None  # a box that died under the first attempt; the second gets a fresh one
    retried: list[dict] = []  # that attempt's record: its spend is the instance's too
    for attempt in (1, 2):
        box = None
        try:
            if args.where == "local":
                if not await image_present(row["image"]):
                    await pull(row["image"])
                box = testbed(row["image"], "local")
                eval_timeout = 3600.0  # emulated
            else:
                box = testbed(
                    row["image"],
                    "daytona",
                    cpu=args.cpu,
                    memory=args.memory,
                    label=f"lionagi-swe-{args.run_id}",
                )
                eval_timeout = 1800.0
            await box.start()
            if args.harness == "codex-cli":
                rec = await solve_codex_cli(
                    row,
                    args.model,
                    out_dir,
                    box=box,
                    time_budget=args.time_budget or None,
                    unaided=args.unaided,
                )
            else:
                closed = await close_code_hosts(box) if args.unaided else None
                rec = await solve(
                    row,
                    args.model,
                    args.max_rounds,
                    out_dir,
                    args.backend,
                    box=box,
                    settle=args.settle,
                    time_budget=args.time_budget or None,
                    cost_budget=args.cost_budget or None,
                    effort=args.effort,
                    thinking=args.thinking,
                    deliberate=args.deliberate,
                    criteria=args.criteria,
                    tdd=args.tdd,
                    unaided=args.unaided,
                )
                if closed is not None:
                    rec["unaided"] = closed
            rec["resolved"] = False
            if rec.get("execution_quiesced") is False:
                # the harness could not confirm the run stopped: the tree may still be changing
                rec["grade"] = {"status": "EXECUTION_UNCERTAIN", "resolved": False}
            else:
                if args.unaided:
                    await open_code_hosts(box)  # the eval script may install; the model's run is over
                if rec["model_patch"].strip():
                    t1 = time.perf_counter()
                    verdict = await grade(
                        box,
                        row,
                        rec["model_patch"],
                        model_name,
                        out_dir / f"{iid}.test_output.log",
                        eval_timeout=eval_timeout,
                        base=rec.get("base"),
                    )
                    rec["resolved"] = bool(verdict.get("resolved"))
                    rec["grade"] = {k: v for k, v in verdict.items() if k != "tests_status"}
                    rec["grade_seconds"] = time.perf_counter() - t1
                else:
                    rec["grade"] = {"status": "EMPTY_PATCH"}
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {e}"
        finally:
            if box is not None:
                try:
                    await box.stop()
                except Exception as e:  # a leaked box is reported, never hidden
                    rec["close_error"] = f"{type(e).__name__}: {e}"
            if args.where == "local" and not args.keep_images:
                await docker("image", "rm", row["image"], check=False)
        if prior is not None:
            rec["retried_after"] = prior
        if attempt == 1 and str(rec.get("error", "")).startswith("SandboxDead"):
            prior = rec["error"]
            print(f"{iid}: {prior[:100]}; retrying on a fresh box", flush=True)
            retried.append({k: v for k, v in rec.items() if k not in ("calls", "model_patch")})
            rec = {"instance_id": iid, "resolved": False}
            continue
        break
    if retried:
        attempts = [*retried, rec]
        costs = [a["cost_usd"] for a in attempts if "cost_usd" in a]
        if costs:
            known = [c for c in costs if c is not None]
            rec["cost_usd"] = sum(known) if known else None
        rec["cost_complete"] = all(_cost_known(a) for a in attempts)
        rec["retried"] = retried
    rec["total_seconds"] = time.perf_counter() - t0
    rec["aided"] = not args.unaided
    rec["model"] = args.model  # the row says which run it belongs to even when no loop ran
    rec["backend"] = "codex-cli" if args.harness == "codex-cli" else args.backend
    rec["difficulty"] = row.get("difficulty")
    rec["repo"] = row.get("repo")
    rec["where"] = args.where
    (out_dir / f"{iid}.json").write_text(json.dumps(rec, indent=1))
    async with lock:
        with ledger.open("a") as f:
            f.write(json.dumps({k: v for k, v in rec.items() if k not in ("calls", "model_patch")}) + "\n")
    spent = rec.get("cost_usd", 0.0)
    cost = "unknown" if spent is None else f"${spent:.3f}" + ("" if _cost_known(rec) else "+?")
    print(
        f"{iid}: resolved={rec['resolved']} {rec.get('outcome')} rounds={rec.get('rounds')} "
        f"cost={cost} {rec['total_seconds']:.0f}s "
        f"{rec.get('grade', {}).get('status', '')} {rec.get('error', '')}",
        flush=True,
    )
    return rec


def summary(ledger: Path) -> str:
    rows = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
    n = len(rows)
    if not n:
        return "no rows"
    res = sum(bool(r.get("resolved")) for r in rows)
    cost = sum(float(r["cost_usd"]) for r in rows if r.get("cost_usd") is not None)
    unknown = sum(1 for r in rows if not _cost_known(r))
    errs = sum(1 for r in rows if r.get("error"))
    by_diff: dict[str, list[int]] = {}
    for r in rows:
        d = by_diff.setdefault(r.get("difficulty") or "?", [0, 0])
        d[0] += bool(r.get("resolved"))
        d[1] += 1
    head = f"resolved {res}/{n} ({100 * res / n:.0f}%)  " + (
        f"cost ${cost:.2f} known; unknown or partial on {unknown} of {n} rows"
        if unknown
        else f"cost ${cost:.2f} (${cost / n:.4f}/instance)"
    )
    lines = [f"{head}  errors {errs}"]
    for k, (a, b) in sorted(by_diff.items()):
        lines.append(f"  {k:16} {a}/{b}")
    return "\n".join(lines)


def provenance() -> dict:
    """The source a launch runs from: HEAD, a full commit id, and what was dirty, untracked files
    included whatever the local git configuration says. A git read that fails refuses the launch: an
    empty head attests nothing, and an empty status reads as a clean tree."""

    def git(*a: str) -> str:
        r = subprocess.run(["git", *a], capture_output=True, text=True, cwd=HERE)
        if r.returncode != 0:
            raise SystemExit(
                f"git {' '.join(a)} exited {r.returncode}: {r.stderr.strip()[:200]}; the launch's source "
                "is unknown, so nothing was launched"
            )
        return r.stdout

    head = git("rev-parse", "HEAD").strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):  # a sha1 or a sha256 repository
        raise SystemExit(f"git rev-parse HEAD gave {head[:80]!r}, not a commit id; nothing was launched")
    # a porcelain line's first two columns are its state: the leading space of an unstaged change stays
    return {"head": head, "dirty": git("status", "--porcelain", "--untracked-files=normal").rstrip("\n")}


def manifest(args: argparse.Namespace, out_dir: Path, selected: list[str], digest: str, src: dict) -> Path:
    """What this launch was, written before the first box: the arguments, the source (HEAD and what
    was dirty), the digest of the bytes the rows were read from, the codex config when that arm runs,
    and the run it belongs to. A later launch under the same id adds its own file, created
    exclusively, so two launches in one second never write one file."""
    doc = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "args": vars(args),
        "head": src["head"],
        "dirty": src["dirty"],
        "dataset": {"path": str(SETS[args.set]), "sha256": digest},
        "selected": selected,  # the instances this launch asked for, after every filter
        "run": "run.json",
    }
    if args.harness == "codex-cli":
        from codex_cli import CODEX_VERSION, CONFIG

        doc["codex"] = {"version": CODEX_VERSION, "config": CONFIG.format(model=args.model)}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    n = 1
    while True:
        path = out_dir / (f"manifest-{stamp}.json" if n == 1 else f"manifest-{stamp}-{n}.json")
        try:
            with path.open("x") as f:
                f.write(json.dumps(doc, indent=1, default=str))
            return path
        except FileExistsError:
            n += 1


async def main(args: argparse.Namespace) -> None:
    # refused before any box: a cap nothing can enforce
    unmetered = args.harness == "codex-cli" or args.backend == "codex"
    if args.cost_budget and unmetered:
        raise SystemExit(
            f"--cost-budget {args.cost_budget}: this arm has no cost meter to cap against (the codex "
            "CLI and the codex backend report tokens, not dollars); run it with no --cost-budget"
        )
    if args.cost_budget is None and not unmetered:
        args.cost_budget = 1.0
    data = SETS[args.set].read_bytes()  # one read: the manifest digests the bytes these rows came from
    digest = hashlib.sha256(data).hexdigest()
    rows = json.loads(data)
    del data  # the rows are what the run holds; the bytes are not kept through the batch
    if args.repo:
        rows = [r for r in rows if r["repo"] == args.repo]
    if args.instances:
        rows = [r for r in rows if r["instance_id"] in set(args.instances)]
    if args.limit:
        rows = rows[: args.limit]
    src = provenance()  # before anything is written
    out_dir = RESULTS / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger = out_dir / "ledger.jsonl"
    # the run this id already is, resolved before anything is written or any done id counted
    canonical = out_dir / "run.json"
    legacy = sorted(out_dir.glob("manifest-*.json"))
    if canonical.exists():
        run, source = json.loads(canonical.read_text()), canonical
    elif legacy:  # a run from before run.json: its manifests must agree and say every field
        recorded = {p.name: json.loads(p.read_text()).get("args", {}) for p in legacy}
        for name, a in recorded.items():
            if missing := [k for k in RUN_KEYS if k not in a]:
                raise SystemExit(
                    f"{out_dir / name} records no {', '.join(missing)}: which run this id holds is unknown; "
                    "start another run id"
                )
        if len({tuple(a[k] for k in RUN_KEYS) for a in recorded.values()}) > 1:
            seen = "; ".join(
                f"{n}: " + ", ".join(f"{k}={a[k]!r}" for k in RUN_KEYS) for n, a in recorded.items()
            )
            raise SystemExit(
                f"{out_dir} is ambiguous: its manifests record different runs ({seen}); start another run id"
            )
        run, source = {k: recorded[legacy[0].name][k] for k in RUN_KEYS}, out_dir / legacy[0].name
    elif ledger_ids(ledger):
        raise SystemExit(
            f"{ledger} holds rows but {out_dir} has no run.json and no manifest: which run produced "
            "them is unknown; start another run id"
        )
    else:
        run, source = {k: getattr(args, k) for k in RUN_KEYS}, None
    if differs := [k for k in RUN_KEYS if run.get(k) != getattr(args, k)]:
        raise SystemExit(
            f"{source} records "
            + ", ".join(f"{k}={run.get(k)!r} where this launch has {getattr(args, k)!r}" for k in differs)
            + "; a run id is one run, start another"
        )
    if clash := ledger_clash(ledger, run):
        raise SystemExit(
            f"{ledger} holds {len(clash)} row(s) that do not say they came from this run "
            f"(first {min(len(clash), 5)}: {', '.join(clash[:5])}); a run id is one run, start another"
        )
    if source != canonical:
        try:
            with canonical.open("x") as f:
                f.write(json.dumps({**run, "from": source.name if source else None}, indent=1))
        except FileExistsError:
            raise SystemExit(
                f"{canonical} appeared while this launch started; launch again to check against it"
            ) from None
    # one process per run: two launches resuming the same id would each read the same ledger and
    # schedule the same unfinished instances; the lock is held until this process ends
    lock_file = (out_dir / "run.lock").open("w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        raise SystemExit(f"{out_dir / 'run.lock'} is held: another launch of this run is running") from None
    try:
        await _run(args, out_dir, ledger, rows, digest, src)
    finally:
        lock_file.close()


async def _run(
    args: argparse.Namespace, out_dir: Path, ledger: Path, rows: list[dict], digest: str, src: dict
) -> None:
    print(
        f"launch record {manifest(args, out_dir, [r['instance_id'] for r in rows], digest, src)}", flush=True
    )
    done = ledger_ids(ledger)
    todo = [r for r in rows if r["instance_id"] not in done]
    print(
        f"set={args.set} where={args.where} rows={len(rows)} done={len(done)} todo={len(todo)} "
        f"backend={args.backend} model={args.model} parallel={args.parallel}",
        flush=True,
    )
    sem = asyncio.Semaphore(args.parallel)
    lock = asyncio.Lock()

    async def guarded(row: dict) -> None:
        async with sem:
            await one(row, args, out_dir, ledger, lock)

    t0 = time.perf_counter()
    await asyncio.gather(*(guarded(r) for r in todo))
    print(f"\nwall {time.perf_counter() - t0:.0f}s\n{summary(ledger)}\nledger {ledger}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=sorted(SETS), default="mini50")
    ap.add_argument("--where", choices=["local", "daytona"], default="daytona")
    ap.add_argument("--repo", default=None)
    ap.add_argument("--instances", nargs="*")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument(
        "--harness",
        choices=["lndl", "codex-cli"],
        default="lndl",
        help="who runs the loop: this runtime (LNDL, `--backend` says which model API) or the codex CLI "
        "inside the box on the same model through OpenRouter (bench/swe/codex_cli.py)",
    )
    ap.add_argument("--backend", choices=["rounds", "session", "openrouter", "codex"], default="openrouter")
    ap.add_argument("--model", default="deepseek/deepseek-v4.1-flash")
    ap.add_argument("--effort", default=None, help="codex only: model_reasoning_effort, e.g. xhigh")
    ap.add_argument(
        "--thinking",
        default="2000",
        help="openrouter only: a reasoning token budget (default 2000: 42/50 on Mini-50 against 33/50 "
        "off), off, on (the provider's default), low, medium, high",
    )
    # rounds are the backstop: with the model's thinking off a round is one or two actions, and a
    # lower cap cut the longest instances mid-edit; settle, time and cost do the stopping
    ap.add_argument("--max-rounds", type=int, default=100)
    ap.add_argument(
        "--criteria",
        action="store_true",
        help="ask for acceptance checks and end the run when they pass (26 vs 32 of 50 on Mini-50); "
        "off, the run ends on settle, budgets or OUT{}",
    )
    ap.add_argument(
        "--deliberate",
        action="store_true",
        help="open each round with written reasoning lines; the arm for the no-thinking regime",
    )
    ap.add_argument(
        "--tdd",
        action="store_true",
        help="ask for a failing test of the asked-for behaviour before any source edit (on the hard set "
        "unaided, most misses declared done with the hidden, mostly new, tests failing)",
    )
    ap.add_argument(
        "--settle",
        type=int,
        default=0,
        help="rounds the fix may stand unchanged before the run stops; 0 (default) never: the run ends on "
        "OUT{} or a budget (on the hard set most runs settled, some mid-verification)",
    )
    ap.add_argument(
        "--aided",
        dest="unaided",
        action="store_false",
        help="leave the code hosts open. The default is unaided: github and kin are closed in every box and "
        "the prompt says so, both arms alike, because with them open the bench measures answer access "
        "(on the 45 instances rated one hour or more: 44 of 45 codex runs and 14 of 45 LNDL runs fetched "
        "the published fix), and only the unaided number is worth optimizing against",
    )
    ap.add_argument(
        "--time-budget",
        type=float,
        default=1200.0,
        help="seconds of wall clock per instance in the loop; 0 never",
    )
    ap.add_argument(
        "--cost-budget",
        type=float,
        default=None,
        help="dollars per instance on the backend; 0 never; default 1 on a metered arm, none on the codex "
        "arms (they have no meter, and a cap there is refused)",
    )
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--cpu", type=int, default=2, help="vCPU per Daytona sandbox")
    ap.add_argument("--memory", type=int, default=2, help="GiB per Daytona sandbox; the org pool is shared")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--keep-images", action="store_true")
    asyncio.run(main(ap.parse_args()))
