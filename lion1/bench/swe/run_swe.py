"""SWE-bench Verified through the LNDL loop: the model fixes real issues inside the instance sandbox.

Per instance: start the eval image as a box, give the model the issue and the hub's tools (bash,
python, read_lines, search, edit), let it work in turns, take `git diff` as the patch when it
finishes with OUT{}, write predictions.jsonl. `--oracle` then scores with the official harness.

    uv run python bench/swe/run_swe.py --instances django__django-11119 --khive-dir DIR \\
        [--model sonnet] [--oracle]

`--khive-dir` names the directory whose `.khive/config.toml` names the actor and routes the lion
pack's tables to their own file (ADR-0026 C8); every run's rows go there.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from hub.backend import ClaudeCode, ClaudeCodeSession, Codex, OpenRouter
from hub.tools import Box, BoxTree, SandboxDead, code, shell
from hub.tools import watch as watching
from hub.tools.watch import Watch
from lion1 import Actor, Exhausted, Kind, Profile, RunRequest, Store, Success
from lion1.actor import reported_cost
from lion1.khive import Khive

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sandbox import testbed  # noqa: E402

HERE = Path(__file__).resolve().parent
# A private codex home (auth.json symlinked) keeps the shared home's AGENTS.md and plugin skills
# out of the model's prompt; without one the call runs from the shared home, the CLI's default.
# It lives beside this file, `bench/swe/codex-home`, and is never tracked.
_BENCH_HOME = HERE / "codex-home"
CODEX_HOME = str(_BENCH_HOME) if _BENCH_HOME.is_dir() else None
DATA = HERE / "data" / "local_verified.json"
RESULTS = HERE / "results"


# --- what the model may say -----------------------------------------------------------------


class Done(BaseModel):
    """You are finished: the fix is in place in the repository."""

    summary: str = Field(description="what was wrong and what you changed, two or three sentences")
    files: list[str] = Field(description="the files you changed")


def _reasoning(thinking: str) -> dict | None:
    """The OpenRouter reasoning field for a `--thinking` value: off, on (the provider's default), an
    effort word, or a token budget."""
    if thinking == "off":
        return {"enabled": False}
    if thinking == "on":
        return None
    if thinking.isdigit():
        return {"max_tokens": int(thinking)}
    if thinking in ("low", "medium", "high", "xhigh", "max"):
        return {"effort": thinking}
    raise ValueError(f"--thinking takes off, on, low, medium, high, xhigh, max or a budget, not {thinking!r}")


def store_of(directory: str | Path) -> Store:
    """The store a run writes its rows to: the actor the directory's `.khive/config.toml` names."""
    d = Path(directory)
    return Store(Khive(d, tomllib.loads((d / ".khive" / "config.toml").read_text())["actor"]["id"]))


def build_actor(box: Box, store: Store, watch: Watch | None = None) -> Actor:
    """The hub's tools over the box: `bash` and `python` (shell), `read_lines`, `search` and `edit`
    (code); plus `criteria` when the run ends on the model's own checks."""
    actor = Actor("swe", privileges={"sandbox"}, store=store)
    if watch is not None:
        watching.criteria(actor, watch)
    shell(actor, box)
    code(actor, BoxTree(box))
    return actor


FIXER = Profile(
    name="fixer",
    privileges=frozenset({"sandbox"}),
    system=(
        "You fix one GitHub issue in the repository at your commands' root. Work in turns: locate the "
        "code the issue is about, reproduce the problem with a small script or an existing test when "
        "you can, make the minimal correct edit, then run the nearest existing tests. You need not add "
        "tests to the repository; a short script or the existing suite is verification enough. Issue "
        "several commands in "
        "one turn when they do not depend on each other; every alias is unique for the whole run. Keep "
        "notes of what you have established (note.<key>) and hide large results you no longer need. "
        "Finish with OUT{} only when the fix is in place; never finish with the repository unchanged."
    ),
)

NO_TESTS = (
    "You need not add tests to the repository; a short script or the existing suite is verification enough."
)
assert NO_TESTS in FIXER.system  # the sentence `--tdd` swaps out
TDD = (  # on the hard set unaided, most misses declared done with the hidden tests failing, most of
    # them new tests of the behaviour the issue asks for, which the model's reproduction never exercised
    "Before any source edit, add a test of the behaviour the issue asks for (not only the crash it reports) "
    "beside the nearest existing test in the repository's suite, run it and see it fail; then edit, and run "
    "it again with the nearest tests. The fix is graded by a hidden test written from the issue, so yours "
    "should read like one."
)
CRITERIA = (  # the run ends on the model's own checks, not on its word
    " Before any edit, declare the acceptance checks with criteria(cmds=[...]): commands that fail now"
    " and must all exit 0 once the issue is fixed, one per symptom the issue names. The run ends by"
    " itself when they all pass after your edit, so you need not declare done."
)
DELIBERATE = (  # with the model's own thinking off, the written turn is where the work gets done
    " Open each turn with one to three plain lines, before any action: what the last results"
    " established, what is still uncertain, and what this turn will settle. Those lines stay in view"
    " for later turns; act on them in the same turn."
)


def guidance(*, criteria: bool = False, deliberate: bool = False, tdd: bool = False) -> str:
    """The fixer's system text under the run's flags: `--tdd` replaces the no-tests sentence,
    `--criteria` and `--deliberate` append theirs."""
    base = FIXER.system.replace(NO_TESTS, TDD) if tdd else FIXER.system
    return base + (CRITERIA if criteria else "") + (DELIBERATE if deliberate else "")


PROMPT = """\
Resolve this GitHub issue by editing the repository source. The repository is at the root your
commands see; its Python environment is the one `bash` and `python` use.

GitHub issue:
{problem}
"""
UNAIDED = (  # the same sentence in both arms: the published fix is out of bounds and out of reach
    "\nThis run is unaided: the upstream project's later fixes, pull requests, commits and releases are "
    "out of bounds, and the code hosts and package indexes are unreachable from here (no `pip download`, "
    "no `git fetch`). Work from the repository and the issue.\n"
)


async def baseline(box: Any) -> str:
    """The commit the model is handed, read before it runs: the prediction is the diff against it
    and the grade resets to it, so a commit the model makes neither vanishes from the patch nor
    survives the reset. `HEAD` at diff time would move with that commit. The commit is the one line
    that is one: what git writes on stderr (a trace, a warning) comes back in the same output, and on
    Daytona in the same stream."""
    rc, out = await box.run("git rev-parse HEAD")
    shas = [s for s in (ln.strip() for ln in out.split("\n")) if re.fullmatch(r"[0-9a-f]{40}", s)]
    if rc != 0 or len(shas) != 1:
        raise RuntimeError(f"no baseline commit: rev-parse exited {rc}: {out.strip()[:200]}")
    return shas[0]


def _harvest(rec: dict, backend: Any) -> None:
    """What the backend spent, per turn, so the cache curve over a long run can be read back. A call
    whose cost nobody reported is unknown, never $0: `cost_usd` is the known subtotal (None when no
    call's cost is known) and `cost_complete` says whether every call is in it. A figure the run would
    not sum (ADR-0003/S7) is unknown too, and a sum past a float's range is kept as the largest float.
    A failed call's envelope is spend like any other."""
    calls = getattr(backend, "calls", [])
    figures = [
        math.nan if c.get("total_cost_usd") is None else reported_cost(c["total_cost_usd"]) for c in calls
    ]
    known = [f for f in figures if not math.isnan(f)]
    rec["cost_usd"] = min(sum(known, 0.0), sys.float_info.max) if known or not calls else None
    rec["cost_complete"] = all(
        not math.isnan(f) and c.get("cost_complete") is not False for c, f in zip(calls, figures, strict=True)
    )
    u = [c.get("usage", {}) for c in calls]
    rec["calls"] = []
    for c, x, f in zip(calls, u, figures, strict=True):
        entry = {
            "api_ms": c.get("duration_api_ms"),
            "provider": c.get("provider"),  # who served the call; OpenRouter routes by availability
            "attempts": c.get("attempts"),
            "input": x.get("input_tokens", 0),
            "cache_read": x.get("cache_read_input_tokens", 0),
            "cache_create": x.get("cache_creation_input_tokens", 0),
            "output": x.get("output_tokens", 0),
            "reasoning": x.get("reasoning_output_tokens"),
            "finish": c.get("finish_reason"),
            "cost_usd": None if math.isnan(f) else f,
            "notes": c.get("notes") or None,
        }
        if c.get("message") is not None:
            entry["message"] = c["message"]  # what came back when no readable text did
        if c.get("tool_calls") is not None:
            entry["tool_calls"] = c["tool_calls"]  # a turn the provider parsed as native tool calls
        if c.get("error") is not None:
            entry["error"] = c["error"]  # a failed call: spend, not a turn
        rec["calls"].append(entry)
    rec["tokens"] = {
        "input": sum(x.get("input_tokens", 0) for x in u),
        "cache_read": sum(x.get("cache_read_input_tokens", 0) for x in u),
        "cache_create": sum(x.get("cache_creation_input_tokens", 0) for x in u),
        "output": sum(x.get("output_tokens", 0) for x in u),
    }


async def solve(
    row: dict,
    model: str,
    max_turns: int,
    out_dir: Path,
    backend_kind: str = "turns",
    box: Box | None = None,
    settle: int = 0,
    unaided: bool = False,
    time_budget: float | None = 1200.0,
    cost_budget: float | None = None,
    effort: str | None = None,
    thinking: str = "2000",
    deliberate: bool = False,
    criteria: bool = False,
    tdd: bool = False,
    backend: Any = None,
    *,
    store: Store,
) -> dict:
    """One instance through the loop. A caller-supplied `box` is already started and stays open
    afterwards (so it can be graded in place); otherwise a local docker box is made and closed here.

    Three budgets end a run early, the diff graded as it stands: `settle` turns with the fix
    unchanged (0: never), `time_budget` seconds of wall clock, `cost_budget` dollars on the backend.
    One instance without a cap is one parallel slot gone for as long as the model cares to go on.
    The codex backend has no meter (a subscription), so a cost cap there is refused before anything
    starts rather than never reached. A cap no spend is under (NaN, or below zero) is refused too, as it
    would stop the run by cost before its first call; infinity is no cap."""
    if cost_budget is not None and not cost_budget >= 0:
        raise ValueError(f"cost_budget {cost_budget}: a cap is dollars, zero or more")
    if cost_budget and backend_kind == "codex":
        raise ValueError(f"cost_budget {cost_budget}: the codex backend reports no cost to cap against")
    iid = row["instance_id"]
    owned = box is None
    if box is None:
        box = testbed(row["image"])
    t0 = time.perf_counter()
    rec: dict[str, Any] = {
        "instance_id": iid,
        "model": model,
        "backend": backend_kind,
        "model_patch": "",
        "outcome": None,
    }
    watch = None
    live: dict = {}  # the run, held past a backend failure: what its readings counted is evidence
    try:
        if owned:
            await box.start()
        rec["base"] = await baseline(box)
        watch = Watch(box, settle_after=settle, base=rec["base"])
        actor = build_actor(box, store, watch if criteria else None)
        actor.section(watch.section)
        progress = out_dir / "progress.log"  # one line per turn across the whole set, appended

        @actor.bus.on("turn.started")
        def _progress(event) -> None:
            with progress.open("a") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {iid} turn {event.payload['turn']}\n")

        if backend is not None:
            pass
        elif backend_kind == "openrouter":
            # Provider slugs are lowercase: a capitalised slug matches nothing and falls through to the
            # cheapest provider while the record reads as pinned. fp8 hosts in a fixed order, no other
            # fallback, and `data_collection` denied. A preset named as the model (`@preset/...`) routes
            # server-side, but with fallbacks on it is not a pin.
            provider = (
                {
                    "order": ["coreweave", "deepinfra", "fireworks"],
                    "allow_fallbacks": False,
                    "data_collection": "deny",
                }
                if model.startswith("deepseek/")
                else None
            )
            # The model's own thinking runs under a 2000-token budget (the default): on Mini-50 the
            # budget resolved 42/50 against 33/50 with thinking off, once the frame below was a stop
            # sequence. Unbounded thinking had looked worse (35/50), but its losses were the
            # imitated frames, not the reasoning; the budget is a soft limit on DeepInfra (calls
            # reach 2x it) and mostly trims the long tail.
            # The completion ends where the model starts writing the loop's own notification frame:
            # what follows is an imagined continuation (invented results, more programs against
            # them), and with thinking on a run could spend 3 to 8 turns of 32k tokens on it and
            # time out with no patch. The program before the frame is the turn and runs as written.
            backend = OpenRouter(
                model=model,
                timeout=600,
                provider=provider,
                reasoning=_reasoning(thinking),
                stop=["<system turn="],
            )
        elif backend_kind == "codex":
            backend = Codex(model=model, effort=effort, timeout=900, home=CODEX_HOME)
        else:
            backend = (ClaudeCodeSession if backend_kind == "session" else ClaudeCode)(
                model=model, timeout=600
            )

        cap = getattr(backend, "max_tokens", None)

        @actor.section
        def last_turn(run) -> str | None:
            # What the backend saw of the model's last turn and the loop could not. A turn cut at
            # the output cap ran only in part and was read as a parse error; a blank turn was read
            # as prose with no program. Either way the model was told the wrong thing and did it
            # again (thinking on: turn after turn of long programs cut at the cap, then time).
            calls = getattr(backend, "calls", None)
            if not calls:
                return None
            c = calls[-1]
            u = c.get("usage") or {}
            if c.get("finish_reason") == "length":
                reasoning = u.get("reasoning_output_tokens") or 0
                return (
                    f"your last turn was cut off at the output cap of {cap} tokens"
                    + (f", {reasoning} of them reasoning" if reasoning else "")
                    + "; only what arrived before the cut was read, so write shorter turns"
                )
            if c.get("message") is not None:
                return (
                    f"your last turn came back with no readable text ({u.get('output_tokens') or 0} "
                    f"output tokens, finish_reason={c.get('finish_reason')!r})"
                )
            return None

        async def accept(run, output) -> str | None:
            return await watching.accept(watch, run, output)

        req = RunRequest(
            inputs=(PROMPT.format(problem=row["problem_statement"]) + (UNAIDED if unaided else ""),),
            emits=(Done,),
            max_turns=max_turns,
            time_budget=time_budget,
            accept=accept,
        )
        stopped_by: list[str] = []

        def stop() -> bool:
            live.update(actor.runs)
            if getattr(box, "dead", None):  # every command would fail the same way; end it now
                stopped_by.append("box")
                return True
            if watch.stop():
                stopped_by.append("criteria" if watch.passed else "settle")
                return True
            # a call that said no cost is unknown: the cap counts none of it, as the agent's day cap counts
            # a backend that writes none (ADR-0013 C5), and the record marks the run's cost incomplete
            said = [c["total_cost_usd"] for c in backend.calls if c.get("total_cost_usd") is not None]
            # past a float's range the sum is the largest float, as the record keeps it: past any finite cap
            spent = min(sum(reported_cost(figure) for figure in said), sys.float_info.max)
            # "not under", as the run's own check: a malformed figure (NaN) stops the run as spent
            if cost_budget is not None and not spent < cost_budget:
                stopped_by.append("cost")
                return True
            return False

        system = guidance(criteria=criteria, deliberate=deliberate, tdd=tdd)
        profile = replace(FIXER, system=system) if system != FIXER.system else FIXER
        run = await actor.run(profile, req, backend, stop=stop)
        if getattr(box, "dead", None):
            raise SandboxDead(box.dead)  # an infrastructure failure, not a run; the runner retries
        rec["outcome"] = type(run.outcome).__name__
        if isinstance(run.outcome, Exhausted):
            rec["outcome"] += f":{run.outcome.reason}"
        if stopped_by:
            rec["stopped_by"] = stopped_by[0]
        rec["turns"] = run.turn
        rec["repairs"] = dict(run.repairs)  # the slips the reading repaired, counted by name
        # each unknown command with the nearest known name and its edit distance, measured while the
        # name repair is off
        rec["unknown_names"] = [list(m) for m in run.misnamed]
        rec["seconds_in_loop"] = run.elapsed
        rec["watch"] = watch.state()
        rec["model_patch"] = await box.diff(rec["base"])
        _harvest(rec, backend)
        if isinstance(run.outcome, Success):
            rec["done"] = run.output["done"].model_dump()
        (out_dir / f"{iid}.record.txt").write_text(_dump(run))
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"
        if backend is not None:  # the turns that ran before the failure are still evidence
            rec.setdefault("turns", sum(1 for c in backend.calls if "error" not in c))
            _harvest(rec, backend)
            run = next(iter(live.values()), None)  # none: the failure came before any reading
            rec.setdefault("repairs", dict(run.repairs) if run else {})
            rec.setdefault("unknown_names", [list(m) for m in run.misnamed] if run else [])
        if watch is not None:
            rec["watch"] = watch.state()
        try:
            # a fix on disk is graded whatever ended the loop; with no baseline read, against HEAD
            rec["model_patch"] = await box.diff(rec.get("base") or "HEAD")
        except Exception as e2:
            rec["diff_error"] = f"{type(e2).__name__}: {e2}"
    finally:
        if owned:
            await box.stop()
    rec["seconds"] = time.perf_counter() - t0
    (out_dir / f"{iid}.json").write_text(json.dumps(rec, indent=1))
    return rec


def _dump(run) -> str:
    lines = [f"run {run.id}: {type(run.outcome).__name__} after {run.turn} turns"]
    for e in run.record:
        if e.kind is Kind.RESULT:
            res = e.content
            body = f"{e.alias} <- {res.spec}: " + (res.preview(400) if res.ok else "ERR " + str(res.error))
        else:
            body = str(e.content)
        lines.append(f"[{e.seq} {e.name} t{e.turn}] {body}")
    return "\n".join(lines)


def oracle(preds: Path, run_id: str, instance_ids: list[str], workers: int) -> dict:
    """The harness's report on `preds`. The harness skips an instance whose per-instance report
    exists under its run id and still writes a fresh top-level report from the cached ones, so the
    id it is handed carries the predictions' digest: new predictions under a reused run id are
    graded, never read from an earlier evaluation. The harness is handed a snapshot of the bytes
    the digest names, so an overlapping run rewriting `preds` changes nothing here. Only a report
    this call wrote, new or changed since it started, is read; a nonzero exit, no such report, or a
    report that does not name exactly the predictions handed in raises; the log keeps both streams
    either way."""
    data = preds.read_bytes()
    digest = hashlib.sha256(data).hexdigest()[:16]
    harness_id = f"{run_id}.{digest}"
    submitted = sorted(
        {json.loads(line)["instance_id"] for line in data.decode().splitlines() if line.strip()}
    )
    snapshot = preds.with_name(f"{preds.stem}.{digest}{preds.suffix}")
    # whole or absent: two evaluations of the same bytes share the name, and a reader must never see
    # a half-written one, so the bytes land beside it and move in atomically
    part = snapshot.with_name(f"{snapshot.name}.{os.getpid()}.part")
    try:
        part.write_bytes(data)
        os.replace(part, snapshot)
    except BaseException as e:  # the snapshot is absent, and so is the scratch file
        try:
            part.unlink(missing_ok=True)
        except OSError as e2:  # a second fault: the first is what ends the call, this one rides along
            e.add_note(f"the scratch file {part.name} could not be removed: {type(e2).__name__}: {e2}")
        raise
    del data  # the harness reads the snapshot; the bytes are not held through its run
    cmd = [
        "uv",
        "run",
        "--no-project",
        "--with",
        "swebench",
        "python",
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        "SWE-bench/SWE-bench_Verified",
        "--predictions_path",
        str(snapshot),
        "--run_id",
        harness_id,
        "--max_workers",
        str(workers),
        "--instance_ids",
        *instance_ids,
    ]
    # the report is named literally, glob syntax in the run id or not
    pattern = f"*{glob.escape(harness_id)}.json"
    # what a report was when this call started, by time and digest: an earlier report's bytes are
    # not held through the evaluation
    before = {p: _seen(p) for p in preds.parent.glob(pattern)}
    log = preds.parent / f"oracle_{run_id}.log"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(preds.parent), timeout=7200)
    except subprocess.TimeoutExpired as e:  # the streams so far, then the timeout stands
        log.write_text(_text(e.stdout) + "\n--- stderr ---\n" + _text(e.stderr))
        raise
    log.write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr)
    if proc.returncode != 0:
        raise RuntimeError(f"the harness exited {proc.returncode}; see oracle_{run_id}.log")
    after = {p: _seen(p) for p in preds.parent.glob(pattern)}
    reports = sorted((p for p, seen in after.items() if before.get(p) != seen), key=lambda p: after[p][0])
    if not reports:
        raise RuntimeError(f"no harness report from this evaluation; see oracle_{run_id}.log")
    report = json.loads(reports[-1].read_text())
    graded = sorted(report.get("submitted_ids", []))
    if graded != submitted:
        raise RuntimeError(
            f"{reports[-1].name} reports {graded}, not the predictions handed in {submitted}; "
            f"see oracle_{run_id}.log"
        )
    return report


def _seen(report: Path) -> tuple[int, str]:
    return report.stat().st_mtime_ns, hashlib.sha256(report.read_bytes()).hexdigest()


def _text(stream: str | bytes | None) -> str:
    return stream.decode(errors="replace") if isinstance(stream, bytes) else (stream or "")


def selected(rows: dict, wanted: list[str] | None) -> list[str]:
    """The instances to run, all of them when none is named; an id not in the data is refused before
    any instance starts paid work."""
    ids = wanted or list(rows)
    missing = [i for i in ids if i not in rows]
    if missing:
        raise SystemExit(f"not in {DATA}: {missing}")
    return ids


def cost_known(r: dict) -> bool:
    """Whether a row's spend is wholly known: a row that never reached paid work has none to know."""
    if "cost_usd" not in r:
        return True
    return (
        r["cost_usd"] is not None
        and r.get("cost_complete") is not False
        and r.get("usage_complete") is not False
    )


def write_predictions(out_dir: Path, recs: list[dict], name: str) -> Path:
    """The harness's input: one line per instance, its patch under the arm's model name."""
    preds = out_dir / "predictions.jsonl"
    with preds.open("w") as f:
        for r in recs:
            row = {"instance_id": r["instance_id"], "model_name_or_path": name}
            f.write(json.dumps({**row, "model_patch": r["model_patch"]}) + "\n")
    print(f"predictions: {preds}")
    return preds


async def main(args: argparse.Namespace) -> None:
    rows = {r["instance_id"]: r for r in json.loads(DATA.read_text())}
    ids = selected(rows, args.instances)
    run_id = args.run_id or time.strftime("%Y%m%d_%H%M%S")
    out_dir = RESULTS / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(args.parallel)

    async def one(iid: str) -> dict:
        async with sem:
            rec = await solve(
                rows[iid],
                args.model,
                args.max_turns,
                out_dir,
                args.backend,
                cost_budget=1.0,
                store=store_of(args.khive_dir),
            )
            spent = rec.get("cost_usd", 0.0)
            print(
                f"{iid}: {rec.get('outcome')} turns={rec.get('turns')} patch={len(rec['model_patch'])}c "
                f"cost={'unknown' if spent is None else f'${spent:.3f}'} {rec['seconds']:.0f}s "
                f"{rec.get('error', '')}"
            )
            return rec

    recs = await asyncio.gather(*(one(i) for i in ids))
    preds = write_predictions(out_dir, recs, f"lionagi-lndl-{args.backend}-{args.model.replace('/', '_')}")
    if args.oracle:
        report = oracle(preds, run_id, ids, args.parallel)
        resolved = set(report.get("resolved_ids", []))
        for r in recs:
            r["resolved"] = r["instance_id"] in resolved
            (out_dir / f"{r['instance_id']}.json").write_text(json.dumps(r, indent=1))
        print(f"resolved {len(resolved)}/{len(ids)}: {sorted(resolved)}")
        spent = [r.get("cost_usd") for r in recs]
        unknown = sum(1 for r in recs if not cost_known(r))
        print(
            f"total cost ${sum(c for c in spent if c is not None):.3f}"
            + (f" known; unknown or partial on {unknown} of {len(recs)}" if unknown else "")
        )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--instances", nargs="*")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--max-turns", type=int, default=30)
    ap.add_argument("--parallel", type=int, default=2)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--oracle", action="store_true")
    ap.add_argument("--backend", choices=["turns", "session", "openrouter"], default="turns")
    ap.add_argument(
        "--khive-dir", required=True, help="the directory whose .khive/config.toml names the actor"
    )
    asyncio.run(main(ap.parse_args()))
