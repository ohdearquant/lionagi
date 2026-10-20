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
# A task id and a loop name together name a leg's files in the run directory, so neither may
# reach outside it. At 100 characters each, an id, `-`, a loop name and the longest suffix
# (`.prompt.txt`, 11) come to 212 bytes, inside a file name's 255.
TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")


class _TaskLoader(yaml.SafeLoader):
    """safe_load keeps the last of a key a mapping names twice; this loader refuses it."""


def _mapping_without_repeats(loader: _TaskLoader, node: yaml.MappingNode) -> dict:
    seen: list = []
    for key_node, _ in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"found the key {key!r} twice in one mapping", key_node.start_mark
            )
        seen.append(key)
    return loader.construct_mapping(node, deep=True)


_TaskLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping_without_repeats
)


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
    prompt_file.write_text(task["prompt"], encoding="utf-8")
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
            encoding="utf-8",
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
    stem.with_suffix(".stdout.txt").write_text(stdout, encoding="utf-8")
    stem.with_suffix(".stderr.txt").write_text(stderr, encoding="utf-8")
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
            problems.append(f"{name}: `id` is not 1 to 100 letters, digits, `_` and `-`")
        elif tid.lower() in seen:
            # Each leg's files are named after its id, so on a volume that ignores letter case
            # two ids that differ only in case would write the same files.
            problems.append(f"{name}: `id` repeats, ignoring letter case")
        else:
            seen.add(tid.lower())
        for key in ("class", "prompt"):
            value = task.get(key)
            if not isinstance(value, str) or not value:
                problems.append(f"{name}: `{key}` is not a non-empty string")
                continue
            try:
                # The prompt file and the table are written as UTF-8 once legs have run.
                value.encode("utf-8")
            except UnicodeEncodeError:
                problems.append(f"{name}: `{key}` is not valid UTF-8 text")
                continue
            if key == "class" and value.splitlines() != [value]:
                # A table row is one line; a `|` is escaped where the row is written.
                problems.append(f"{name}: `class` holds a line break")
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
                try:
                    item.encode("utf-8")
                except UnicodeEncodeError:
                    # A lone surrogate never matches an answer read from a leg's output.
                    problems.append(f"{name}: `accept.{key}` has an item that is not valid UTF-8")
                    continue
                if not item.startswith("re:"):
                    continue
                try:
                    pattern = re.compile(item[3:], re.IGNORECASE)
                # A repeat count past the engine's limit raises OverflowError, and deep
                # nesting RecursionError, rather than re.error.
                except (re.error, OverflowError, RecursionError) as exc:
                    problems.append(f"{name}: `{item}` does not compile: {exc}")
                    continue
                if not item[3:].strip() or pattern.search("") is not None:
                    # A pattern that matches an empty answer would let all_of accept one and
                    # make none_of refuse every answer; a blank one is a stray space.
                    problems.append(
                        f"{name}: `accept.{key}` has a `re:` pattern that is blank or matches"
                        " an empty answer"
                    )
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
    try:
        loaded = yaml.load(tasks_bytes, Loader=_TaskLoader)  # noqa: S506 - a SafeLoader subclass
    # Deep nesting raises RecursionError rather than a YAMLError.
    except (yaml.YAMLError, RecursionError) as exc:
        print(f"{tasks_path} is not YAML: {exc}\nnothing run", file=sys.stderr)
        return 2
    if not isinstance(loaded, dict) or "tasks" not in loaded:
        print(f"{tasks_path} has no top-level `tasks`; nothing run", file=sys.stderr)
        return 2
    tasks = loaded["tasks"]
    task_set = len(tasks) if isinstance(tasks, list) else 0
    loops = args.loops.split(",")
    # Each of these would otherwise surface only once legs had run, or after the run directory
    # had been made.
    problems = task_problems(tasks)
    if not LI.is_file() or not os.access(LI, os.X_OK):
        problems.append(f"{LI} is not executable")
    if args.concurrency < 1:
        problems.append(f"--concurrency {args.concurrency} is below 1")
    # Read from the listing, so the name must match in letter case on any volume.
    profiles = {p.name for p in (HERE / ".lionagi" / "agents").glob("d4-*.md") if p.is_file()}
    for loop in loops:
        if not TASK_ID.fullmatch(loop):
            problems.append(f"--loops: {loop!r} is not 1 to 100 letters, digits, `_` and `-`")
        elif f"d4-{loop}.md" not in profiles:
            problems.append(f"--loops: no profile d4-{loop}.md")
    if len({loop.lower() for loop in loops}) < len(loops):
        # A loop named twice would run each task's leg twice into the same files.
        problems.append(f"--loops: {args.loops!r} names a loop twice, ignoring letter case")
    # resolve() reads a `..` after a missing part as text, where the OS refuses the path, so the
    # two agree on where --out lies only when the part before each `..` exists.
    parts = Path(args.out).parts
    gone = next(
        (
            Path(*parts[:i])
            for i, part in enumerate(parts)
            if part == ".." and not os.path.lexists(Path(*parts[:i]))
        ),
        None,
    )
    if not args.out:
        problems.append("--out is empty")
    elif os.path.lexists(Path(args.out)):
        # Through Path, which drops a trailing slash: with one, lstat follows a dangling link.
        problems.append(f"--out: {args.out} already exists")
    elif gone is not None:
        problems.append(f"--out: {args.out} goes up from {gone}, which does not exist")
    else:
        try:
            # Absolute: each child runs with cwd set to the read tree, so a relative path would
            # resolve there and the prompt file would not be found.
            out = Path(args.out).resolve()
            # From Python 3.13 resolve() returns through a link loop instead of raising, so the
            # deepest part of the path that exists is resolved strictly: a loop, a dangling link
            # or a file on the way is refused here, not by mkdir once the run has begun.
            parent = Path(args.out).absolute().parent
            while not os.path.lexists(parent) and parent != parent.parent:
                parent = parent.parent
            if not os.path.isdir(os.path.realpath(parent, strict=True)):
                problems.append(f"--out: {args.out} is under {parent}, which is not a directory")
        # resolve() raises RuntimeError on a link loop before Python 3.13; realpath raises OSError.
        except (OSError, RuntimeError) as exc:
            problems.append(f"--out: {args.out} does not resolve: {exc}")
    if args.only:
        wanted = args.only.split(",")
        known = (
            {t["id"] for t in tasks if isinstance(t, dict) and isinstance(t.get("id"), str)}
            if task_set
            else set()
        )
        problems += [f"--only: no task {tid!r}" for tid in wanted if tid not in known]
    if not problems:
        # Task `x-a` under loop `b` and task `x` under loop `a-b` would write the same files.
        stems = [f"{t['id']}-{loop}".lower() for t in tasks for loop in loops]
        clashes = sorted({stem for stem in stems if stems.count(stem) > 1})
        if clashes:
            problems.append(f"two legs would write the same files: {', '.join(clashes)}")
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        print("nothing run", file=sys.stderr)
        return 2
    if args.only:
        tasks = [t for t in tasks if t["id"] in wanted]
    header = {
        "tasks_file": str(tasks_path),
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
    # Made once everything above has been checked and `git` and `li` have answered for the
    # header, so a refused run, or an `li` that cannot start, leaves no run directory behind.
    try:
        out.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        # An ancestor without search permission or on a read-only file system shows only here.
        print(f"--out: {args.out} cannot be created: {exc}", file=sys.stderr)
        print("nothing run", file=sys.stderr)
        return 2
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
    # Every task the run covers has a line, so a leg with no recorded row reads `not run`.
    for task in sorted(tasks, key=lambda t: t["id"]):
        tid = task["id"]
        cells = []
        for loop in loops:
            r = next((x for x in rows if x["id"] == tid and x["loop"] == loop), None)
            if r is None:
                cells.append("not run")
                continue
            counts = [
                "-" if r[k] is None else str(r[k])
                for k in ("requests", "tool_calls", "wall_seconds")
            ]
            cells.append(" / ".join([*counts, "yes" if r["accepted"] else "no"]))
        cls = task["class"].replace("|", "\\|")
        lines.append(f"| {tid} | {cls} | " + " | ".join(cells) + " |")
    (out / "table.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    if stopped:
        return 130
    return 1 if failed or any(r["rc"] == "harness-error" for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
