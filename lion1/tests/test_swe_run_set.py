"""run_set: what a launch refuses before any box, what `one` grades and what it keeps of a retried
attempt, and what the launch records about the set it selected."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench" / "swe"))
import codex_cli  # noqa: E402
import run_set  # noqa: E402
from run_swe import solve  # noqa: E402

from tests.test_swe_solve import Costs, ToolBox  # noqa: E402

DIFF = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"


def _args(**kw) -> argparse.Namespace:
    base = dict(
        set="t",
        repo=None,
        instances=None,
        limit=None,
        where="daytona",
        cpu=2,
        memory=2,
        run_id="r1",
        harness="lndl",
        backend="openrouter",
        model="deepseek/deepseek-v4.1-flash",
        max_rounds=5,
        settle=0,
        time_budget=1200.0,
        cost_budget=None,
        effort=None,
        thinking="2000",
        deliberate=False,
        criteria=False,
        tdd=False,
        unaided=False,
        keep_images=False,
        parallel=2,
    )
    base.update(kw)
    return argparse.Namespace(**base)


class FakeBox:
    async def start(self):
        return self

    async def stop(self):
        pass


def _one(args, tmp_path: Path, iid: str = "a") -> dict:
    ledger = tmp_path / "ledger.jsonl"
    row = {"instance_id": iid, "image": "img", "problem_statement": "It breaks."}
    asyncio.run(run_set.one(row, args, tmp_path, ledger, asyncio.Lock()))
    return json.loads(ledger.read_text().splitlines()[-1])


def test_retry_spend_is_retained(tmp_path, monkeypatch):
    # the attempt on the box that died was spend too
    def solving(*attempts):
        it = iter(attempts)

        async def fake_solve(row, *a, **k):
            return next(it)

        return fake_solve

    monkeypatch.setattr(run_set, "testbed", lambda *a, **k: FakeBox())
    monkeypatch.setattr(
        run_set,
        "solve",
        solving(
            {"instance_id": "a", "model_patch": "", "cost_usd": 0.30, "error": "SandboxDead: no IP"},
            {"instance_id": "a", "model_patch": "", "cost_usd": 0.10},
        ),
    )
    row = _one(_args(), tmp_path)
    assert row["cost_usd"] == pytest.approx(0.40) and row["cost_complete"] is True
    assert [r["cost_usd"] for r in row["retried"]] == [0.30] and row["retried"][0]["error"].startswith(
        "SandboxDead"
    )
    # a first attempt whose spend nobody measured: the known subtotal, flagged partial
    monkeypatch.setattr(
        run_set,
        "solve",
        solving(
            {"instance_id": "b", "model_patch": "", "cost_usd": None, "error": "SandboxDead: gone"},
            {"instance_id": "b", "model_patch": "", "cost_usd": 0.10, "cost_complete": True},
        ),
    )
    row = _one(_args(), tmp_path, "b")
    assert row["cost_usd"] == pytest.approx(0.10) and row["cost_complete"] is False


def _main(monkeypatch, tmp_path: Path, rows: list[dict]) -> list[tuple[str, argparse.Namespace]]:
    data = tmp_path / "set.json"
    data.write_text(json.dumps(rows))
    monkeypatch.setattr(run_set, "SETS", {"t": data})
    monkeypatch.setattr(run_set, "RESULTS", tmp_path / "results")
    ran: list[tuple[str, argparse.Namespace]] = []

    async def fake_one(row, args, out_dir, ledger, lock):
        ran.append((row["instance_id"], args))
        async with lock:
            with ledger.open("a") as f:
                backend = "codex-cli" if args.harness == "codex-cli" else args.backend
                row_out = {"instance_id": row["instance_id"], "resolved": False, "aided": not args.unaided}
                f.write(json.dumps({**row_out, "model": args.model, "backend": backend}) + "\n")

    monkeypatch.setattr(run_set, "one", fake_one)
    monkeypatch.setattr(run_set, "testbed", lambda *a, **k: pytest.fail("a box was made"))
    return ran


def test_unsupported_cost_limit_is_refused_before_run(tmp_path, monkeypatch, capsys):
    # a cap on an arm with no meter is refused up front
    rows = [{"instance_id": "a", "repo": "x", "image": "img"}]
    ran = _main(monkeypatch, tmp_path, rows)
    for arm in (dict(harness="codex-cli"), dict(backend="codex")):
        with pytest.raises(SystemExit, match="--cost-budget"):
            asyncio.run(run_set.main(_args(cost_budget=0.5, **arm)))
    assert ran == [] and not (tmp_path / "results").exists()  # no run directory, no manifest, no box
    # the loop's own entry refuses it too, before a box or a backend call
    box, backend = ToolBox(), Costs(None)
    with pytest.raises(ValueError, match="cost"):
        asyncio.run(
            solve(
                {"instance_id": "a", "problem_statement": "x"},
                "m",
                3,
                tmp_path,
                "codex",
                box=box,
                backend=backend,
                cost_budget=0.01,
            )
        )
    assert backend.calls == [] and backend.costs == [None]
    # the controls: an unmetered arm with no cap runs; a metered arm gets the default cap
    asyncio.run(run_set.main(_args(harness="codex-cli", cost_budget=None, run_id="r2")))
    asyncio.run(run_set.main(_args(cost_budget=None, run_id="r3")))
    assert [(i, a.cost_budget) for i, a in ran] == [("a", None), ("a", 1.0)]


def test_cli_time_zero_reaches_the_runner(tmp_path, monkeypatch):
    # `--time-budget 0` on codex-cli runs with no deadline
    ran = _main(monkeypatch, tmp_path, [{"instance_id": "a", "image": "img", "repo": "x"}])
    asyncio.run(run_set.main(_args(harness="codex-cli", time_budget=0.0, cost_budget=None)))
    assert len(ran) == 1


def test_the_launch_record_names_the_selected_set(tmp_path, monkeypatch, capsys):
    rows = [
        {"instance_id": "a", "repo": "x", "image": "img"},
        {"instance_id": "b", "repo": "x", "image": "img"},
        {"instance_id": "c", "repo": "y", "image": "img"},
    ]
    ran = _main(monkeypatch, tmp_path, rows)
    asyncio.run(run_set.main(_args(repo="x", limit=1)))
    assert [i for i, _ in ran] == ["a"]
    lines = [x for x in capsys.readouterr().out.splitlines() if x.startswith("launch record ")]
    assert len(lines) == 1
    record = json.loads(Path(lines[0].removeprefix("launch record ")).read_text())
    assert record["selected"] == ["a"]  # after the repo and limit filters, not the set's size


class TransportFailBox(FakeBox):
    """codex wrote its stream in the box; the host's exec call came back as a 504. `stop_rc` is what
    the stop probe answers: 0 when no codex process is left, 1 when one survived."""

    def __init__(self, stop_rc: int | None, fail: bool = True):
        self.stop_rc, self.fail, self.files, self.diffs, self.probes = stop_rc, fail, {}, 0, []

    async def run(self, cmd, *, timeout=120.0, env=None):
        if "curl -fsSL" in cmd:
            return 0, "codex-cli 0.153.4\n"
        if cmd == "git rev-parse HEAD":
            return 0, "c" * 40 + "\n"
        if f"{codex_cli.BIN} exec" in cmd:
            usage = {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 200}
            self.files[codex_cli.STREAM] = json.dumps({"type": "turn.completed", "usage": usage})
            if not self.fail:
                return 0, ""
            raise RuntimeError("DaytonaTimeoutError: Failed to execute command: 504 Gateway Time-out")
        if "/proc/" in cmd:  # the stop probe
            self.probes.append(cmd)
            if self.stop_rc is None:
                raise RuntimeError("the box is gone")
            return self.stop_rc, ""
        return 0, ""

    async def read(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write(self, path, text):
        self.files[path] = text

    async def diff(self, base="HEAD"):
        self.diffs += 1
        return DIFF


def test_unconfirmed_quiescence_blocks_grading(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    graded: list[str] = []

    async def fake_grade(box, row, patch, model_name, log, eval_timeout=1800.0, base=None):
        graded.append(row["instance_id"])
        return {"resolved": True, "status": "GRADED"}

    monkeypatch.setattr(run_set, "grade", fake_grade)
    # STOP found no codex process; one survived the kills; the probe itself failed. None of the three
    # proves the execution stopped: a command codex started outlives it under another name
    for stop_rc in (0, 1, None):
        box = TransportFailBox(stop_rc)
        monkeypatch.setattr(run_set, "testbed", lambda *a, box=box, **k: box)
        row = _one(_args(harness="codex-cli"), tmp_path, f"u{stop_rc}")
        assert graded == [] and box.diffs == 0 and len(box.probes) == 1
        assert row["grade"]["status"] == "EXECUTION_UNCERTAIN" and row["resolved"] is False
        assert json.loads((tmp_path / f"u{stop_rc}.json").read_text())["model_patch"] == ""
        assert row["execution_quiesced"] is False and row["stop_rc"] == stop_rc
        assert row["usage_complete"] is False and "504" in row["error"]

    # a row that says so, with a patch in hand, is still not graded
    async def uncertain(*a, **k):
        return {
            "instance_id": "p",
            "model_patch": DIFF,
            "execution_quiesced": False,
            "usage_complete": False,
            "error": "RuntimeError: 504",
        }

    monkeypatch.setattr(run_set, "testbed", lambda *a, **k: FakeBox())
    monkeypatch.setattr(run_set, "solve_codex_cli", uncertain)
    assert _one(_args(harness="codex-cli"), tmp_path, "p")["grade"]["status"] == "EXECUTION_UNCERTAIN"
    assert graded == []
    # the control: an exec that came back is graded once, with no probe run
    monkeypatch.setattr(run_set, "solve_codex_cli", codex_cli.solve)
    box = TransportFailBox(0, fail=False)
    monkeypatch.setattr(run_set, "testbed", lambda *a, **k: box)
    row = _one(_args(harness="codex-cli"), tmp_path, "s")
    assert graded == ["s"] and row["grade"]["status"] == "GRADED" and row["resolved"] is True
    assert box.probes == [] and "execution_quiesced" not in row and row["outcome"] == "Success"


ROWS = [{"instance_id": i, "repo": "x", "image": "img"} for i in ("a", "b", "c")]


def _legacy(out_dir: Path, name: str, **args) -> None:
    """A manifest as run_set wrote it before run.json: the launch's arguments under `args`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / name).write_text(json.dumps({"at": "then", "args": args}))


def test_resume_refuses_policy_mismatch_and_missing_manifest_keys(tmp_path, monkeypatch):
    ran = _main(monkeypatch, tmp_path, ROWS)
    out = tmp_path / "results"

    def refused(args, *words):
        before = sorted(p.name for p in (out / args.run_id).glob("*"))
        with pytest.raises(SystemExit) as e:
            asyncio.run(run_set.main(args))
        assert all(w in str(e.value) for w in words), str(e.value)
        # refused before any launch record is written or any instance runs
        assert sorted(p.name for p in (out / args.run_id).glob("*")) == before and ran == []

    # a run of one model, resumed under another: refused, the field named
    asyncio.run(run_set.main(_args(run_id="m", instances=["a"])))
    ran.clear()
    refused(
        _args(run_id="m", model="anthropic/other"), "model", "deepseek/deepseek-v4.1-flash", "anthropic/other"
    )
    refused(_args(run_id="m", unaided=True), "unaided")
    refused(_args(run_id="m", harness="codex-cli"), "harness")
    # a legacy run whose manifest does not say which harness ran it
    legacy = dict(unaided=False, model="deepseek/deepseek-v4.1-flash", backend="openrouter")
    _legacy(out / "k", "manifest-20260901-000000.json", **legacy)
    (out / "k" / "ledger.jsonl").write_text(
        json.dumps({"instance_id": "a", "aided": True, "model": "x"}) + "\n"
    )
    refused(_args(run_id="k"), "harness", "manifest-20260901-000000.json")
    # two legacy manifests that disagree: which one the rows came from is unknown
    _legacy(out / "two", "manifest-20260901-000000.json", **legacy, harness="lndl")
    _legacy(
        out / "two", "manifest-20260901-000001.json", **{**legacy, "model": "anthropic/other"}, harness="lndl"
    )
    refused(
        _args(run_id="two"), "ambiguous", "manifest-20260901-000000.json", "manifest-20260901-000001.json"
    )
    # rows and no manifest at all
    (out / "bare").mkdir()
    (out / "bare" / "ledger.jsonl").write_text(json.dumps({"instance_id": "a", "aided": True}) + "\n")
    refused(_args(run_id="bare"), "no run.json", "no manifest")
    # one matching manifest does not make its rows this run's; a row with no
    # policy verdict, or from another model, is refused rather than counted
    for iid, row in (
        ("nopolicy", {"model": "deepseek/deepseek-v4.1-flash", "backend": "openrouter"}),
        ("othermodel", {"aided": True, "model": "anthropic/other", "backend": "openrouter"}),
    ):
        _legacy(out / iid, "manifest-20260901-000000.json", **legacy, harness="lndl")
        (out / iid / "ledger.jsonl").write_text(
            json.dumps({"instance_id": "a", "resolved": True, **row}) + "\n"
        )
        refused(_args(run_id=iid), "a ", "aided" if iid == "nopolicy" else "model")


def test_a_matching_resume_skips_what_the_ledger_holds(tmp_path, monkeypatch):
    ran = _main(monkeypatch, tmp_path, ROWS)
    asyncio.run(run_set.main(_args(run_id="m", instances=["a"])))
    asyncio.run(run_set.main(_args(run_id="m")))
    assert [i for i, _ in ran] == ["a", "b", "c"]  # a once, then only what the ledger lacked


def test_manifest_creation_never_overwrites(tmp_path, monkeypatch, capsys):
    ran = _main(monkeypatch, tmp_path, ROWS)
    monkeypatch.setattr(run_set.time, "strftime", lambda fmt, *a: "20260922-120000")  # one second, twice
    asyncio.run(run_set.main(_args(run_id="s", instances=["a"])))
    asyncio.run(run_set.main(_args(run_id="s", instances=["b"])))
    records = [
        Path(x.removeprefix("launch record "))
        for x in capsys.readouterr().out.splitlines()
        if x.startswith("launch record ")
    ]
    assert len(records) == 2 and records[0] != records[1] and [i for i, _ in ran] == ["a", "b"]
    first, second = (json.loads(r.read_text()) for r in records)
    assert first["selected"] == ["a"] and second["selected"] == ["b"]  # the first launch's record survives
    assert first["run"] == second["run"] == "run.json"


def test_a_second_launch_of_a_running_run_is_refused(tmp_path, monkeypatch, capsys):
    import fcntl

    rows = [{"instance_id": "a", "repo": "x", "image": "img"}]
    ran = _main(monkeypatch, tmp_path, rows)
    out_dir = tmp_path / "results" / "r1"
    out_dir.mkdir(parents=True)
    held = (out_dir / "run.lock").open("w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another launch of r1, still running
    with pytest.raises(SystemExit, match="run.lock is held: another launch of this run is running"):
        asyncio.run(run_set.main(_args()))
    assert ran == []  # nothing was scheduled
    held.close()  # that launch ended: the lock is free
    asyncio.run(run_set.main(_args()))
    assert [i for i, _ in ran] == ["a"]
