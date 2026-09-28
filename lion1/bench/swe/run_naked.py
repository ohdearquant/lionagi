"""SWE-bench Verified with naked Claude Code: the baseline the LNDL loop is compared against.

Per instance: copy /testbed out of the eval image to a host directory, bind-mount that directory back
into a container from the same image (so the repository's Python environment is the container's), run
one `claude -p` on the host with its own Read/Grep/Glob/Edit/Write/Bash over that directory, tell it
to run tests through `docker exec`, and take `git diff` of the host directory as the patch. Same
model, same CLI, same flags, same oracle as run_swe.py; the only difference is who runs the loop.

    uv run python bench/swe/run_naked.py --instances django__django-11119 [--model sonnet] [--oracle]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from hub.harness import ClaudeCodeAgentic

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_swe import DATA, RESULTS, oracle  # noqa: E402

from hub.tools import docker as _docker  # noqa: E402

TOOLS = ("Read", "Grep", "Glob", "Edit", "Write", "Bash")

PROMPT = """\
Resolve this GitHub issue by editing the repository source in the current directory.

The repository's Python environment is NOT on this machine. It lives in a running docker container
named {container}, where this same directory is mounted at /testbed. Run any Python, test or shell
command for the repository as:

    docker exec {container} bash -c 'source /opt/miniconda3/bin/activate testbed; cd /testbed && <command>'

Edit files here with your own tools; the container sees the edits at once. Do not commit.

GitHub issue:
{problem}
"""


async def solve(row: dict, model: str, max_turns: int, out_dir: Path) -> dict:
    iid = row["instance_id"]
    t0 = time.perf_counter()
    rec: dict[str, Any] = {
        "instance_id": iid,
        "model": model,
        "backend": "naked",
        "model_patch": "",
        "outcome": None,
    }
    name = f"lionagi-naked-{uuid.uuid4().hex[:8]}"
    host = Path(tempfile.mkdtemp(prefix=f"swe-{iid}-"))
    harness = None
    try:
        # the repository out of the image: a throwaway container, docker cp, gone
        tmp = f"{name}-src"
        await _docker("create", "--platform", "linux/amd64", "--name", tmp, row["image"])
        await _docker("cp", f"{tmp}:/testbed/.", str(host))
        rec["base"] = _baseline(host)
        await _docker("container", "remove", "--force", tmp)
        await _docker(
            "run",
            "-d",
            "--name",
            name,
            "--platform",
            "linux/amd64",
            "-v",
            f"{host}:/testbed",
            "-w",
            "/testbed",
            row["image"],
            "sleep",
            "infinity",
        )
        harness = ClaudeCodeAgentic(model=model, tools=TOOLS, max_turns=max_turns, cwd=host, timeout=1800)
        text = await harness(PROMPT.format(container=name, problem=row["problem_statement"]))
        env = harness.calls[-1]
        rec["outcome"] = env.get("subtype") or "completed"
        rec["final_text"] = text[-2000:]
        _spend(rec, env)
        rec["model_patch"] = _diff(host, rec["base"])
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"
        # the fix on disk is graded and the spend is counted whatever ended the run
        if harness is not None and harness.calls:
            _spend(rec, harness.calls[-1])
        try:
            if "base" in rec:  # no baseline read: nothing was handed to a model, so there is no patch
                rec["model_patch"] = _diff(host, rec["base"])
        except Exception as e2:
            rec["diff_error"] = f"{type(e2).__name__}: {e2}"
    finally:
        await _docker("container", "remove", "--force", name, check=False)
        shutil.rmtree(host, ignore_errors=True)
    rec["seconds"] = time.perf_counter() - t0
    (out_dir / f"{iid}.json").write_text(json.dumps(rec, indent=1))
    return rec


def _spend(rec: dict, env: dict) -> None:
    """The harness's last envelope: turns, cost and tokens, on the record."""
    rec["turns"] = env.get("num_turns")
    rec["cost_usd"] = float(env.get("total_cost_usd") or 0)
    u = env.get("usage", {})
    rec["tokens"] = {
        "input": u.get("input_tokens", 0),
        "cache_read": u.get("cache_read_input_tokens", 0),
        "cache_create": u.get("cache_creation_input_tokens", 0),
        "output": u.get("output_tokens", 0),
    }


def _baseline(host: Path) -> str:
    """The commit the model is handed, read before it runs: the prediction is the diff against it,
    so a commit the model makes stays in the patch."""
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=host, capture_output=True, text=True)
    sha = r.stdout.strip()
    if r.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RuntimeError(f"no baseline commit: rev-parse exited {r.returncode}: {r.stderr.strip()[:200]}")
    return sha


def _diff(host: Path, base: str) -> str:
    """The tree against `base`, staged or committed since or not, plus new source files, no bytecode."""
    parts = []
    tracked = subprocess.run(
        ["git", "diff", "--binary", base], cwd=host, capture_output=True, text=True
    ).stdout
    if tracked.strip():
        parts.append(tracked)
    others = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"], cwd=host, capture_output=True, text=True
    ).stdout.split()
    for path in others:
        if path.endswith((".pyc", ".pyo")) or "__pycache__" in path:
            continue
        d = subprocess.run(
            ["git", "diff", "--no-index", "--binary", "/dev/null", path],
            cwd=host,
            capture_output=True,
            text=True,
        ).stdout
        if d.strip():
            parts.append(d)
    return "".join(parts)


async def main(args: argparse.Namespace) -> None:
    rows = {r["instance_id"]: r for r in json.loads(DATA.read_text())}
    ids = args.instances or list(rows)
    run_id = args.run_id or time.strftime("naked_%Y%m%d_%H%M%S")
    out_dir = RESULTS / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(args.parallel)

    async def one(iid: str) -> dict:
        async with sem:
            rec = await solve(rows[iid], args.model, args.max_turns, out_dir)
            print(
                f"{iid}: {rec.get('outcome')} turns={rec.get('turns')} patch={len(rec['model_patch'])}c "
                f"cost=${rec.get('cost_usd', 0):.3f} {rec['seconds']:.0f}s {rec.get('error', '')}"
            )
            return rec

    recs = await asyncio.gather(*(one(i) for i in ids))
    preds = out_dir / "predictions.jsonl"
    with preds.open("w") as f:
        for r in recs:
            row = {
                "instance_id": r["instance_id"],
                "model_name_or_path": f"claude-code-naked-{args.model}",
                "model_patch": r["model_patch"],
            }
            f.write(json.dumps(row) + "\n")
    print(f"predictions: {preds}")
    if args.oracle:
        report = oracle(preds, run_id, ids, args.parallel)
        resolved = set(report.get("resolved_ids", []))
        for r in recs:
            r["resolved"] = r["instance_id"] in resolved
            (out_dir / f"{r['instance_id']}.json").write_text(json.dumps(r, indent=1))
        print(f"resolved {len(resolved)}/{len(ids)}: {sorted(resolved)}")
        print(f"total cost ${sum(r.get('cost_usd', 0) for r in recs):.3f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--instances", nargs="*")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--max-turns", type=int, default=60)
    ap.add_argument("--parallel", type=int, default=2)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--oracle", action="store_true")
    asyncio.run(main(ap.parse_args()))
