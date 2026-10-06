"""run_set: what a launch refuses before any box, what `one` grades and what it keeps of a retried
attempt, and what the launch records about the set it selected."""

import argparse
import asyncio
import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

from tests import _git as isolated
from tests._swe import DIFF, CodexBox, GatewayTimeoutBox, Started, codex_cli, run_set

pytestmark = pytest.mark.usefixtures("bench_store")


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
        max_turns=5,
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
        khive_dir=".",
    )
    base.update(kw)
    return argparse.Namespace(**base)


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

    monkeypatch.setattr(run_set, "testbed", lambda *a, **k: Started())
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
    one = run_set.one

    async def counted(row, args, out_dir, ledger, lock):
        # the real `one` writes the row, so a resume reads back what the runner stamps
        ran.append((row["instance_id"], args))
        await one(row, args, out_dir, ledger, lock)

    async def solved(row, *a, **k):
        return {"instance_id": row["instance_id"], "model_patch": ""}

    async def hosts(box):
        return {"closed": True}

    monkeypatch.setattr(run_set, "one", counted)
    # the launch's source as a checkout would give it; the tests of that read put git back (`_git`)
    monkeypatch.setattr(run_set, "provenance", lambda: {"head": HEAD, "dirty": ""})
    monkeypatch.setattr(run_set, "testbed", lambda *a, **k: Started())
    for name in ("solve", "solve_codex_cli"):
        monkeypatch.setattr(run_set, name, solved)
    for name in ("close_code_hosts", "open_code_hosts"):
        monkeypatch.setattr(run_set, name, hosts)
    return ran


def test_unsupported_cost_limit_is_refused_before_run(tmp_path, monkeypatch, capsys):
    # a cap on an arm with no meter is refused up front
    rows = [{"instance_id": "a", "repo": "x", "image": "img"}]
    ran = _main(monkeypatch, tmp_path, rows)
    for arm in (dict(harness="codex-cli"), dict(backend="codex")):
        with pytest.raises(SystemExit, match="--cost-budget"):
            asyncio.run(run_set.main(_args(cost_budget=0.5, **arm)))
    for cap in (float("nan"), -1.0):  # a cap no spend is under, on any arm
        with pytest.raises(SystemExit, match="a cap is dollars, zero or more"):
            asyncio.run(run_set.main(_args(cost_budget=cap)))
    assert ran == [] and not (tmp_path / "results").exists()  # no run directory, no manifest, no box
    # the controls: an unmetered arm with no cap runs; a metered arm gets the default cap
    asyncio.run(run_set.main(_args(harness="codex-cli", cost_budget=None, run_id="r2")))
    asyncio.run(run_set.main(_args(cost_budget=None, run_id="r3")))
    assert [(i, a.cost_budget) for i, a in ran] == [("a", None), ("a", 1.0)]


def test_cli_time_zero_reaches_the_runner(tmp_path, monkeypatch):
    # `--time-budget 0` ("0 never") reaches either arm as no deadline, not as 1200 s
    _main(monkeypatch, tmp_path, [{"instance_id": "a", "image": "img", "repo": "x"}])
    seen: list = []

    async def solved(row, *a, **k):
        seen.append(k["time_budget"])
        return {"instance_id": row["instance_id"], "model_patch": ""}

    for name in ("solve", "solve_codex_cli"):
        monkeypatch.setattr(run_set, name, solved)
    for i, (harness, budget) in enumerate((("codex-cli", 0.0), ("lndl", 0.0), ("codex-cli", 100.0))):
        args = _args(harness=harness, time_budget=budget, cost_budget=None, run_id=f"t{i}")
        asyncio.run(run_set.main(args))
    assert seen == [None, None, 100.0]  # the last, the control: a set deadline reaches the arm as set


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


def test_the_dataset_bytes_are_not_held_through_the_batch(tmp_path, monkeypatch):
    rows = [{"instance_id": "a", "repo": "x", "image": "img", "pad": "p" * 5000}]
    _main(monkeypatch, tmp_path, rows)
    _git(monkeypatch)
    size = (tmp_path / "set.json").stat().st_size
    one = run_set.one
    held: list[str] = []

    async def probe(row, args, out_dir, ledger, lock):
        # the launch's own coroutines, main awaiting _run, as they stand while an instance runs
        for task in asyncio.all_tasks():
            coro = task.get_coro()
            while getattr(coro, "cr_frame", None) is not None:
                held.extend(
                    f"{coro.cr_frame.f_code.co_name}.{k}"
                    for k, v in coro.cr_frame.f_locals.items()
                    if isinstance(v, bytes) and len(v) >= size
                )
                coro = getattr(coro, "cr_await", None)
        await one(row, args, out_dir, ledger, lock)

    monkeypatch.setattr(run_set, "one", probe)
    asyncio.run(run_set.main(_args()))
    assert held == []  # the rows are what the batch holds; the file's bytes were let go once read


HEAD = "a" * 40
PROVENANCE = run_set.provenance


def _git(monkeypatch, head=(0, HEAD + "\n", ""), status=(0, "", "")) -> None:
    """git as the launch reads it: `rev-parse HEAD` and `status --porcelain` answer (rc, out, err)."""

    def run(argv, *, capture_output, text, cwd):
        rc, out, err = head if argv[1:] == ["rev-parse", "HEAD"] else status
        assert argv[1:] in (["rev-parse", "HEAD"], ["status", "--porcelain", "--untracked-files=normal"])
        return subprocess.CompletedProcess(argv, rc, out, err)

    monkeypatch.setattr(run_set.subprocess, "run", run)
    monkeypatch.setattr(run_set, "provenance", PROVENANCE)


def _record(capsys) -> dict:
    lines = [x for x in capsys.readouterr().out.splitlines() if x.startswith("launch record ")]
    assert len(lines) == 1
    return json.loads(Path(lines[0].removeprefix("launch record ")).read_text())


def test_the_manifest_digests_the_bytes_the_rows_were_read_from(tmp_path, monkeypatch, capsys):
    ran = _main(monkeypatch, tmp_path, [{"instance_id": "a", "repo": "x", "image": "img"}])
    _git(monkeypatch)
    data = tmp_path / "set.json"
    read = data.read_bytes()
    provenance = run_set.provenance

    def replaced_while_launching() -> dict:
        data.write_text(json.dumps([{"instance_id": "z", "repo": "x", "image": "img"}]))
        return provenance()

    monkeypatch.setattr(run_set, "provenance", replaced_while_launching)
    asyncio.run(run_set.main(_args()))
    record = _record(capsys)
    assert [i for i, _ in ran] == ["a"] and record["selected"] == ["a"]
    assert record["dataset"]["sha256"] == hashlib.sha256(read).hexdigest()
    assert hashlib.sha256(data.read_bytes()).hexdigest() != hashlib.sha256(read).hexdigest()


def test_a_git_read_that_fails_refuses_the_launch_before_anything_is_written(tmp_path, monkeypatch):
    ran = _main(monkeypatch, tmp_path, [{"instance_id": "a", "repo": "x", "image": "img"}])
    for git, refusal in (
        (
            dict(status=(128, "", "fatal: not a git repository\n")),
            "git status --porcelain .* exited 128: fatal",
        ),
        (dict(head=(128, "HEAD\n", "fatal: ambiguous argument 'HEAD'\n")), "git rev-parse HEAD exited 128"),
        (dict(head=(0, "abc1234\n", "")), "gave 'abc1234', not a commit id"),
    ):
        _git(monkeypatch, **git)
        with pytest.raises(SystemExit, match=refusal):
            asyncio.run(run_set.main(_args()))
    assert ran == [] and not (tmp_path / "results").exists()  # no run directory, no manifest, no box


def test_an_untracked_file_is_dirty_whatever_the_local_git_configuration_says(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "none"))  # this repository's config alone
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    for argv in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "one"]):
        subprocess.run([*git, *argv], cwd=repo, check=True)
    subprocess.run([*git, "config", "status.showUntrackedFiles", "no"], cwd=repo, check=True)
    (repo / "notes.txt").write_text("untracked\n")
    monkeypatch.setattr(run_set, "HERE", repo)
    bare = subprocess.run([*git, "status", "--porcelain"], cwd=repo, capture_output=True, text=True)
    assert bare.stdout == ""  # the configuration hides it from a bare status
    src = run_set.provenance()
    assert re.fullmatch(r"[0-9a-f]{40}", src["head"]) and src["dirty"] == "?? notes.txt"


def test_the_manifest_names_a_clean_tree_and_a_dirty_one_as_git_reads_them(tmp_path, monkeypatch, capsys):
    ran = _main(monkeypatch, tmp_path, [{"instance_id": "a", "repo": "x", "image": "img"}])
    _git(monkeypatch)
    asyncio.run(run_set.main(_args(run_id="clean")))
    clean = _record(capsys)
    _git(monkeypatch, status=(0, " M bench/swe/run_set.py\n?? notes.txt\n", ""))
    asyncio.run(run_set.main(_args(run_id="dirty")))
    dirty = _record(capsys)
    assert clean["head"] == dirty["head"] == HEAD and clean["dirty"] == ""
    assert "bench/swe/run_set.py" in dirty["dirty"] and "?? notes.txt" in dirty["dirty"]
    _git(monkeypatch, head=(0, "b" * 64 + "\n", ""))  # a sha256 repository's head is a commit id too
    asyncio.run(run_set.main(_args(run_id="sha256")))
    assert _record(capsys)["head"] == "b" * 64
    assert [i for i, _ in ran] == ["a", "a", "a"]


def test_unconfirmed_quiescence_blocks_grading(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    graded: list[str] = []

    async def fake_grade(box, row, patch, model_name, log, eval_timeout=1800.0, base=None):
        graded.append(row["instance_id"])
        return {"resolved": True, "status": "GRADED"}

    monkeypatch.setattr(run_set, "grade", fake_grade)
    monkeypatch.setattr(run_set, "solve_codex_cli", codex_cli.solve)
    # STOP found no codex process; one survived the kills; the probe itself failed. None of the three
    # proves the execution stopped: a command codex started outlives it under another name
    for stop_rc in (0, 1, None):
        box = GatewayTimeoutBox(stop_rc)
        monkeypatch.setattr(run_set, "testbed", lambda *a, box=box, **k: box)
        row = _one(_args(harness="codex-cli"), tmp_path, f"u{stop_rc}")
        assert graded == [] and box.diffs == 0 and [c for c in box.cmds if "/proc/" in c] == [codex_cli.STOP]
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

    monkeypatch.setattr(run_set, "testbed", lambda *a, **k: Started())
    monkeypatch.setattr(run_set, "solve_codex_cli", uncertain)
    assert _one(_args(harness="codex-cli"), tmp_path, "p")["grade"]["status"] == "EXECUTION_UNCERTAIN"
    assert graded == []
    # the control: an exec that came back is graded once, with no probe run
    monkeypatch.setattr(run_set, "solve_codex_cli", codex_cli.solve)
    box = CodexBox()
    monkeypatch.setattr(run_set, "testbed", lambda *a, **k: box)
    row = _one(_args(harness="codex-cli"), tmp_path, "s")
    assert graded == ["s"] and row["grade"]["status"] == "GRADED" and row["resolved"] is True
    assert not [c for c in box.cmds if "/proc/" in c]  # no stop probe ran, in any spelling
    assert "execution_quiesced" not in row and row["outcome"] == "Success"


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


def test_a_ledger_row_counts_only_when_it_says_it_came_from_this_run(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    run = {"unaided": True, "model": "m", "backend": "openrouter", "harness": "lndl"}
    assert run_set.ledger_clash(ledger, run) == []
    rows = [
        {
            "instance_id": "old-1",
            "resolved": True,
            "model": "m",
            "backend": "openrouter",
        },  # no policy verdict
        {"instance_id": "aided-1", "resolved": True, "aided": True, "model": "m", "backend": "openrouter"},
        {
            "instance_id": "unaided-1",
            "resolved": False,
            "aided": False,
            "model": "m",
            "backend": "openrouter",
        },
        {"instance_id": "cli-1", "resolved": False, "aided": False, "model": "m", "backend": "codex-cli"},
    ]
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert [c.split()[0] for c in run_set.ledger_clash(ledger, run)] == ["old-1", "aided-1", "cli-1"]
    aided = {**run, "unaided": False}
    assert [c.split()[0] for c in run_set.ledger_clash(ledger, aided)] == ["old-1", "unaided-1", "cli-1"]
    cli = {**run, "harness": "codex-cli"}
    assert [c.split()[0] for c in run_set.ledger_clash(ledger, cli)] == ["old-1", "aided-1", "unaided-1"]


@pytest.mark.parametrize("harness", ["lndl", "codex-cli"])
@pytest.mark.parametrize("unaided", [False, True])
def test_a_matching_resume_skips_what_the_ledger_holds(tmp_path, monkeypatch, harness, unaided):
    # the rows the runner wrote say they came from this run, on either arm and either policy
    ran = _main(monkeypatch, tmp_path, ROWS)
    asyncio.run(run_set.main(_args(run_id="m", instances=["a"], harness=harness, unaided=unaided)))
    ledger = tmp_path / "results" / "m" / "ledger.jsonl"
    run = vars(_args(harness=harness, unaided=unaided))
    assert run_set.ledger_clash(ledger, {k: run[k] for k in run_set.RUN_KEYS}) == []
    asyncio.run(run_set.main(_args(run_id="m", harness=harness, unaided=unaided)))
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


def test_the_manifest_keeps_the_status_columns_of_an_unstaged_change(tmp_path, monkeypatch):
    path = tmp_path / "repo"
    isolated.repo(path, {"a.py": "x = 1\n", "b.py": "x = 1\n"})
    (path / "a.py").write_text("x = 2\n")  # modified, unstaged
    (path / "b.py").write_text("x = 2\n")
    isolated.git(path, "add", "b.py")  # modified, staged
    data = tmp_path / "set.json"
    data.write_text("[]")
    monkeypatch.setattr(run_set, "SETS", {"t": data})
    monkeypatch.setattr(run_set, "HERE", path)
    doc = json.loads(
        run_set.manifest(_args(), tmp_path, [], data.read_bytes(), run_set.provenance()).read_text()
    )
    assert doc["dirty"] == " M a.py\nM  b.py"
    assert re.fullmatch(r"[0-9a-f]{40}", doc["head"])
