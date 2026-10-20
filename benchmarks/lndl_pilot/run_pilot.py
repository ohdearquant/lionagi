"""ADR-0132 D4 pilot: one task set, two loops, same model/tools/bound/instructions.

Each task runs once per loop (profiles d4-react / d4-lndl, which differ only in
api_loop). The two runs of a task are adjacent in time and their order
alternates by task, so drift lands on both arms. Requests, tool calls and wall
time come from the `[api] spend:` line `li agent` prints for an API-model leg;
acceptance is the task set's own check.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
TREE = HERE / "tree"
LI = HERE / "venv" / "bin" / "li"
SCRATCH_HOME = HERE / "home"
SPEND = re.compile(r"\[api\] spend: (\d+) request\(s\), (\d+) tool call\(s\), ([0-9.]+)s")
RUN_TIMEOUT_S = 420
# A task id names the leg's files in the run directory, so it may not reach outside it.
TASK_ID = re.compile(r"[A-Za-z0-9_-]+")


def _matches(text: str, item: str) -> bool:
    if item.startswith("re:"):
        return re.search(item[3:], text, re.IGNORECASE) is not None
    return item.lower() in text.lower()


def check_answer(answer: str, accept: dict) -> bool:
    if not all(_matches(answer, item) for item in accept.get("all_of", [])):
        return False
    return not any(_matches(answer, item) for item in accept.get("none_of", []))


def run_one(task: dict, loop: str, out: Path) -> dict:
    stem = out / f"{task['id']}-{loop}"
    prompt_file = stem.with_suffix(".prompt.txt")
    prompt_file.write_text(task["prompt"])
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(SCRATCH_HOME),
        "LIONAGI_HOME": str(SCRATCH_HOME / ".lionagi"),
        "ANTHROPIC_API_KEY": os.environ["ANTHROPIC_API_KEY"],
    }
    argv = [
        str(LI),
        "agent",
        "-a",
        f"d4-{loop}",
        "--no-mcp-config",
        "--prompt-file",
        str(prompt_file),
    ]
    started = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603 - argv built here from fixed paths
            argv,
            cwd=TREE,
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=RUN_TIMEOUT_S,
        )
        rc, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        rc = "harness-timeout"
        # The output so far arrives as bytes even with text=True, and can end inside a character.
        stdout = (
            exc.stdout.decode(errors="replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        stderr = (
            exc.stderr.decode(errors="replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )
    except Exception as exc:  # noqa: BLE001 - one leg's failure is its own row, not the run's end
        # subprocess.run kills the child before it re-raises, so the leg's output is lost; the
        # stderr file keeps what went wrong, and with no spend line the D4 rule stays unset.
        rc = "harness-error"
        stdout, stderr = "", f"{type(exc).__name__}: {exc}\n"
    elapsed = round(time.monotonic() - started, 3)
    stem.with_suffix(".stdout.txt").write_text(stdout)
    stem.with_suffix(".stderr.txt").write_text(stderr)
    spends = SPEND.findall(stderr) or SPEND.findall(stdout)
    requests = tool_calls = wall = None
    if spends:
        r, t, w = spends[-1]
        requests, tool_calls, wall = int(r), int(t), float(w)
    answer = stdout.strip()
    return {
        "id": task["id"],
        "class": task["class"],
        "loop": loop,
        "rc": rc,
        "requests": requests,
        "tool_calls": tool_calls,
        "wall_seconds": wall,
        "harness_seconds": elapsed,
        "spend_lines": len(spends),
        "accepted": rc == 0 and check_answer(answer, task["accept"]),
        "answer_chars": len(answer),
    }


def run_pair(
    index: int,
    task: dict,
    loops: list[str],
    out: Path,
    stop: threading.Event,
    record: Callable[[dict], None],
) -> None:
    order = loops if index % 2 == 0 else list(reversed(loops))
    for loop in order:
        if stop.is_set():
            # Stopped between a pair's two legs: the second leg is not started.
            return
        try:
            row = run_one(task, loop, out)
        except Exception as exc:  # noqa: BLE001 - raised after the leg ran: still its own row
            # Writing the leg's output or running its check failed. The leg has no spend line
            # here, so the D4 rule stays unset, and the run exits 1.
            print(f"{task['id']} {loop}: {type(exc).__name__}: {exc}", file=sys.stderr)
            row = {
                "id": task["id"],
                "class": task["class"],
                "loop": loop,
                "rc": "harness-error",
                "requests": None,
                "tool_calls": None,
                "wall_seconds": None,
                "harness_seconds": None,
                "spend_lines": 0,
                "accepted": False,
                "answer_chars": 0,
            }
        record(row)


def task_problems(tasks: object) -> list[str]:
    """What in the task set would fail, or pass an answer unchecked, once legs had run."""
    if not isinstance(tasks, list) or not tasks:
        return ["`tasks` is not a non-empty list"]
    problems = []
    seen = set()
    for n, task in enumerate(tasks, 1):
        tid = task.get("id") if isinstance(task, dict) else None
        name = tid if isinstance(tid, str) else f"task {n}"
        if not isinstance(task, dict):
            problems.append(f"{name}: not a mapping")
            continue
        if not isinstance(tid, str) or not TASK_ID.fullmatch(tid):
            problems.append(f"{name}: `id` is not letters, digits, `_` and `-`")
        elif tid in seen:
            problems.append(f"{name}: `id` repeats")
        seen.add(tid)
        if task.get("none_of"):
            # check_answer reads `none_of` from `accept` only, so one beside it would be ignored.
            problems.append(f"{name}: `none_of` outside `accept`")
        for key in ("class", "prompt"):
            if not isinstance(task.get(key), str) or not task[key]:
                problems.append(f"{name}: `{key}` is not a non-empty string")
        accept = task.get("accept")
        if not isinstance(accept, dict) or set(accept) - {"all_of", "none_of"}:
            problems.append(f"{name}: `accept` is not a mapping of `all_of` and `none_of`")
            continue
        items = {key: accept.get(key, []) for key in ("all_of", "none_of")}
        for key, value in items.items():
            if not isinstance(value, list) or not all(isinstance(i, str) and i for i in value):
                problems.append(f"{name}: `accept.{key}` is not a list of non-empty strings")
                continue
            for item in value:
                if item.startswith("re:"):
                    try:
                        re.compile(item[3:])
                    except re.error as exc:
                        problems.append(f"{name}: `{item}` does not compile: {exc}")
        if items["all_of"] == []:
            # With nothing required, an empty answer would be accepted.
            problems.append(f"{name}: `accept.all_of` is empty")
    return problems


def summarize(rows: list[dict], loops: list[str], partial: str = "") -> dict:
    """Totals per loop and the D4 rule. `partial` names why the rows are not the whole run."""
    summary = {}
    for loop in loops:
        mine = [r for r in rows if r["loop"] == loop]
        accepted = sum(r["accepted"] for r in mine)
        requests = sum(r["requests"] or 0 for r in mine)
        summary[loop] = {
            "runs": len(mine),
            "accepted": accepted,
            "requests": requests,
            "tool_calls": sum(r["tool_calls"] or 0 for r in mine),
            "wall_seconds": round(sum(r["wall_seconds"] or 0 for r in mine), 1),
            "missing_spend": sum(r["spend_lines"] == 0 for r in mine),
            "requests_per_accepted": round(requests / accepted, 3) if accepted else None,
        }
    if partial:
        summary["partial"] = partial
    if set(loops) == {"react", "lndl"}:
        react, lndl = summary["react"], summary["lndl"]
        missing = react["missing_spend"] + lndl["missing_spend"]
        # The rule is read over the whole task set, and a leg with no spend line counts as zero
        # requests, so either one leaves the rule's inputs incomplete.
        unset = [partial] if partial else []
        if missing:
            unset.append(f"{missing} leg(s) have no spend line")
        if unset:
            summary["d4_rule"] = None
            summary["d4_rule_unset"] = "; ".join(unset)
            return summary
        summary["d4_rule"] = {
            "lndl_fewer_requests_per_accepted": (
                lndl["requests_per_accepted"] is not None
                and react["requests_per_accepted"] is not None
                and lndl["requests_per_accepted"] < react["requests_per_accepted"]
            ),
            "lndl_accepts_no_fewer": lndl["accepted"] >= react["accepted"],
        }
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--only", default="")
    ap.add_argument("--loops", default="react,lndl")
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set in this process; nothing run", file=sys.stderr)
        return 2
    # Without the read tree every leg would fail to start, and without `li` the header could not
    # be written; either way the run would record nothing worth keeping.
    missing = [str(path) for path in (TREE, LI) if not path.exists()]
    if missing:
        print(f"{', '.join(missing)} not found; nothing run", file=sys.stderr)
        return 2
    tasks_path = Path(args.tasks).resolve()
    tasks_bytes = tasks_path.read_bytes()
    tasks = yaml.safe_load(tasks_bytes)["tasks"]
    task_set = len(tasks) if isinstance(tasks, list) else 0
    loops = args.loops.split(",")
    # Each of these would otherwise surface only once legs had run, or after the run directory
    # had been made.
    problems = task_problems(tasks)
    if not os.access(LI, os.X_OK):
        problems.append(f"{LI} is not executable")
    if args.concurrency < 1:
        problems.append(f"--concurrency {args.concurrency} is below 1")
    for loop in loops:
        if not (HERE / ".lionagi" / "agents" / f"d4-{loop}.md").is_file():
            problems.append(f"--loops: no profile d4-{loop}.md")
    if args.only:
        wanted = args.only.split(",")
        known = {t.get("id") for t in tasks if isinstance(t, dict)} if task_set else set()
        problems += [f"--only: no task {tid!r}" for tid in wanted if tid not in known]
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        print("nothing run", file=sys.stderr)
        return 2
    if args.only:
        tasks = [t for t in tasks if t["id"] in wanted]
    # Absolute: each child runs with cwd set to the read tree, so a relative path would
    # resolve there and the prompt file would not be found. Made once everything above has been
    # checked, so a refused run leaves no run directory behind.
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    header = {
        # Relative to this directory, as summarize.py reads it, when the task file is in it.
        "tasks_file": str(
            tasks_path.relative_to(HERE) if tasks_path.is_relative_to(HERE) else tasks_path
        ),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "tasks_sha256": hashlib.sha256(tasks_bytes).hexdigest(),
        "task_count": len(tasks),
        "read_tree_head": subprocess.run(  # noqa: S603
            ["git", "-C", str(TREE), "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "runner_li": subprocess.run(  # noqa: S603
            [str(LI), "--version"],
            capture_output=True,
            text=True,
            env={
                "HOME": str(SCRATCH_HOME),
                "LIONAGI_HOME": str(SCRATCH_HOME / ".lionagi"),
                "PATH": "/usr/bin:/bin",
            },
        ).stdout.strip(),
        "profiles": {
            loop: hashlib.sha256(
                (HERE / ".lionagi" / "agents" / f"d4-{loop}.md").read_bytes()
            ).hexdigest()
            for loop in loops
        },
        "loops": loops,
        "concurrency": args.concurrency,
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (out / "header.json").write_text(json.dumps(header, indent=2))
    recorded: list[dict] = []
    lock = threading.Lock()

    def record(row: dict) -> None:
        # Written as each leg ends, so a stopped run keeps the rows of the legs that finished. The
        # summary and table count a row only once it is in results.jsonl; a row that cannot be
        # written stops its task on a harness error.
        with lock:
            with (out / "results.jsonl").open("a") as fh:
                fh.write(json.dumps(row) + "\n")
            recorded.append(row)
            print(json.dumps(row), flush=True)

    stop = threading.Event()
    stopped = ""
    pool = cf.ThreadPoolExecutor(max_workers=args.concurrency)
    futures = [pool.submit(run_pair, i, t, loops, out, stop, record) for i, t in enumerate(tasks)]
    try:
        cf.wait(futures)
    except KeyboardInterrupt:
        # Queued pairs are cancelled and a running pair starts no second leg. An interrupt from
        # the terminal reaches the running legs' `li agent` processes too, so they end and their
        # rows are kept; a second interrupt stops the wait for them.
        stop.set()
        pool.shutdown(wait=False, cancel_futures=True)
        stopped = "interrupted"
        try:
            # A future the shutdown cancelled never counts as done to cf.wait, so wait on the rest.
            cf.wait([f for f in futures if not f.cancelled()])
        except KeyboardInterrupt:
            stopped = "interrupted twice"
    else:
        pool.shutdown()
    with lock:
        rows = sorted(recorded, key=lambda r: (r["id"], r["loop"]))
    failed = [f for f in futures if f.done() and not f.cancelled() and f.exception() is not None]
    for fut in failed:
        print(f"a task stopped on a harness error: {fut.exception()!r}", file=sys.stderr)
    reasons = []
    if len(tasks) < task_set:
        reasons.append(f"the run covers {len(tasks)} of {task_set} tasks (--only)")
    if failed:
        reasons.append(f"{len(failed)} task(s) stopped on a harness error")
    if stopped:
        reasons.append(f"{stopped} after {len(rows)} of {len(tasks) * len(loops)} legs")
    if stopped == "interrupted twice":
        # The legs still running are not waited for here, but the process exits only once they
        # end (the executor joins its threads at exit), and each appends its row as it does.
        reasons.append("rows of legs still running may follow in results.jsonl")
    summary = summarize(rows, loops, partial="; ".join(reasons))
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    lines = [
        "| id | class | " + " | ".join(f"{l} req / tools / wall / ok" for l in loops) + " |",  # noqa: E741
        "|---|---|" + "---|" * len(loops),
    ]
    # Every task the run covers has a line, so a leg that never started reads `not run`.
    for task in sorted(tasks, key=lambda t: t["id"]):
        tid = task["id"]
        cells = []
        for loop in loops:
            r = next((x for x in rows if x["id"] == tid and x["loop"] == loop), None)
            if r is None:
                cells.append("not run")
                continue
            cells.append(
                f"{r['requests']} / {r['tool_calls']} / {r['wall_seconds']} / {'yes' if r['accepted'] else 'no'}"
            )
        lines.append(f"| {tid} | {task['class']} | " + " | ".join(cells) + " |")
    (out / "table.md").write_text("\n".join(lines) + "\n")
    print(json.dumps(summary, indent=2))
    if stopped:
        return 130
    return 1 if failed or any(r["rc"] == "harness-error" for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
