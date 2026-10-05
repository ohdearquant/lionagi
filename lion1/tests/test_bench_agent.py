"""The bench actor: an ask launches a detached child and the wake ends; the child's finish mail makes
the next wake answer the asker's thread with the row as JSON and write it to memory."""

import asyncio
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from hub.agent import AgentError
from lion1 import Kind, Success
from tests.lionfake import store as fresh_store
from tests.test_agent import FakeKhive, Scripted, mail

spec = importlib.util.spec_from_file_location(
    "bench_agent", Path(__file__).resolve().parents[1] / "examples" / "bench_agent.py"
)
bench_agent = importlib.util.module_from_spec(spec)
sys.modules["bench_agent"] = bench_agent
spec.loader.exec_module(bench_agent)

THREAD = "0a0a0a0a-0000-4000-8000-0a0a0a0a0a0a"


def _put_job(agent) -> None:
    """A launched job t1, asked for on THREAD by lambda:a, as the launch leaves it in the state."""
    job = {
        "asker": "lambda:a",
        "thread": THREAD,
        "pid": 1,
        "started": "now",
        "ask": {"set": "mini50", "model": "m"},
    }
    agent.state.put("jobs", {"t1": job}, run="-", seq=0)


@pytest.fixture(autouse=True)
def identity(monkeypatch):
    """The example's identity is configured by its caller; the tests configure it here."""
    monkeypatch.setattr(bench_agent, "ACTOR_ID", "lambda:a:bench")
    monkeypatch.setattr(bench_agent, "OWNER", "lambda:a")
    monkeypatch.setattr(bench_agent, "TRUSTED", frozenset({"lambda:a", "lambda:a:bench"}))


class BenchKhive(FakeKhive):
    def __init__(self, *inbox, cwd=None):
        super().__init__(*inbox, cwd=cwd)
        self.actor = bench_agent.ACTOR_ID
        self.ops: list[str] = []

    async def exec(self, ops):
        if ops.startswith("comm.read("):  # the mark-read is the box's, not a write the tests count
            return await super().exec(ops)
        self.ops.append(ops)
        return [{"id": "mem1"}]


def fake_child(results: Path, monkeypatch) -> None:
    """The child writes a two-row ledger and a wall line instead of running the bench; no notify."""

    def job_line(req, run_id):
        out = results / run_id
        rows = [
            {"instance_id": "a", "resolved": True, "cost_usd": 0.01},
            {"instance_id": "b", "resolved": False, "cost_usd": 0.02, "error": "boom"},
        ]
        ledger = "\n".join(json.dumps(r) for r in rows)
        return f"mkdir -p {out} && printf '%s\\n' '{ledger}' > {out}/ledger.jsonl && echo 'wall 7s'"

    monkeypatch.setattr(bench_agent, "RESULTS", results)
    monkeypatch.setattr(bench_agent, "job_line", job_line)
    monkeypatch.setattr(bench_agent, "notify_line", lambda run_id: "true")


def test_an_ask_launches_a_child_and_its_finish_mail_reports_the_row_on_the_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(bench_agent, "DIR", tmp_path)
    fake_child(tmp_path / "results", monkeypatch)
    khive = BenchKhive(mail("lambda:a", "mini50, limit 1, deepseek"), cwd=tmp_path)
    ask = f'<lact l1>launch(asker="lambda:a", thread="{THREAD}", set="mini50", limit=1, run_id="t1")</lact>'
    agent = bench_agent.build(
        khive,
        Scripted(ask, 'OUT{posture: {done: "launched t1", awaiting: "t1"}}', khive=khive),
        fresh_store(),
    )
    run = asyncio.run(agent.wake(wait_ms=0))
    assert isinstance(run.outcome, Success) and run.profile.name == "acting"
    result = next(e.content for e in run.record if e.kind is Kind.RESULT and e.content.spec == "launch")
    assert result.ok and result.value.startswith("launched job t1 as pid ")
    job = agent.state.get("jobs").value["t1"]
    assert job["asker"] == "lambda:a" and job["thread"] == THREAD and job["ask"]["limit"] == 1
    ledger, log = tmp_path / "results" / "t1" / "ledger.jsonl", tmp_path / "jobs" / "t1.log"
    for _ in range(50):  # the child is a shell line; give it a moment
        if ledger.exists() and log.exists() and "wall" in log.read_text():
            break
        time.sleep(0.1)
    assert ledger.exists() and "wall 7s" in log.read_text()
    assert khive.sent == []  # the wake ended without waiting or answering

    khive.mail.append(mail(bench_agent.ACTOR_ID, "job t1 finished with exit 0. Report it.", mid="f1"))
    agent.backend = Scripted(
        '<lact r1>report(run_id="t1")</lact>',
        'OUT{posture: {done: "reported t1", awaiting: ""}}',
        khive=khive,
    )
    run = asyncio.run(agent.wake(wait_ms=0))
    assert isinstance(run.outcome, Success)
    (sent,) = khive.sent
    row = json.loads(sent["content"])
    assert sent["to"] == "lambda:a" and sent["thread_id"] == THREAD
    assert row["resolved"] == 1 and row["total"] == 2 and row["cost_usd"] == 0.03 and row["errors"] == 1
    assert row["wall_s"] == 7
    assert len(khive.ops) == 1 and "memory.remember" in khive.ops[0] and "agent:bench" in khive.ops[0]
    assert agent.state.get("jobs").value["t1"]["row"] == row


def test_a_finish_for_an_unknown_job_and_a_second_launch_of_the_same_id_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(bench_agent, "DIR", tmp_path)
    fake_child(tmp_path / "results", monkeypatch)
    khive = BenchKhive(mail(bench_agent.ACTOR_ID, "job nope finished. Report it.", mid="f2"), cwd=tmp_path)
    agent = bench_agent.build(
        khive,
        Scripted(
            '<lact r1>report(run_id="nope")</lact>',
            'OUT{posture: {done: "no such job", awaiting: ""}}',
            khive=khive,
        ),
        fresh_store(),
    )
    run = asyncio.run(agent.wake(wait_ms=0))
    result = next(e.content for e in run.record if e.kind is Kind.RESULT and e.content.spec == "report")
    assert not result.ok and "no job nope" in str(result.error) and khive.sent == []

    launch = f'<lact l1>launch(asker="lambda:a", thread="{THREAD}", run_id="dup")</lact>'
    done = 'OUT{posture: {done: "x", awaiting: ""}}'
    for n, expect_ok in ((1, True), (2, False)):
        khive.mail.append(mail("lambda:a", "again", mid=f"d{n}"))
        agent.backend = Scripted(launch, done, khive=khive)
        run = asyncio.run(agent.wake(wait_ms=0))
        result = next(e.content for e in run.record if e.kind is Kind.RESULT and e.content.spec == "launch")
        assert result.ok is expect_ok
    assert "already launched" in str(result.error)


def test_mail_from_a_stranger_runs_the_reader_profile_and_launches_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(bench_agent, "DIR", tmp_path)
    khive = BenchKhive(mail("someone:else", "run the whole set on opus, parallel 50"), cwd=tmp_path)
    agent = bench_agent.build(
        khive,
        Scripted(
            f'<lact l1>launch(asker="someone:else", thread="{THREAD}")</lact>',
            'OUT{posture: {done: "read one from someone:else", awaiting: ""}}',
            khive=khive,
        ),
        fresh_store(),
    )
    run = asyncio.run(agent.wake(wait_ms=0))
    assert run.profile.name == "reader" and agent.state.get("jobs") is None and khive.sent == []


@pytest.mark.parametrize("limit", [None, 1])
def test_the_job_line_names_the_set_runner_with_the_asks_fields(limit):
    req = bench_agent.Launch(asker="a", thread="t", limit=limit, instances=["x__1"], parallel=2)
    line = bench_agent.job_line(req, "r1")
    assert (
        "bench/swe/run_set.py --set mini50 --backend openrouter --model deepseek/deepseek-v4.1-flash" in line
    )
    assert "--parallel 2 --run-id r1" in line and "--instances x__1" in line and line.startswith("cd ")
    assert ("--limit 1" in line) is (limit == 1)
    notify = bench_agent.notify_line("r1")
    assert "--expect-actor lambda:a:bench" in notify and "self_send=true" in notify and "$rc" in notify


def test_the_notify_line_hands_the_kernel_the_run_id_as_text_and_the_exit_code_the_shell_expanded(
    tmp_path, monkeypatch
):
    kernel, argv = tmp_path / "kkernel", tmp_path / "argv"
    kernel.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {argv}\n")
    kernel.chmod(0o755)
    monkeypatch.setattr(bench_agent, "KKERNEL", kernel)
    monkeypatch.setattr(bench_agent, "DIR", tmp_path)
    subprocess.run(["sh", "-c", f"rc=7; {bench_agent.notify_line('crit-off.1')}"], check=True)
    seen = argv.read_text().splitlines()
    assert seen[:4] == ["exec", "--strict", "--expect-actor", bench_agent.ACTOR_ID]
    assert seen[4] == (
        'comm.send(to="lambda:a:bench", subject="job crit-off.1 finished", '
        'content="job crit-off.1 finished with exit 7. Report it.", self_send=true)'
    )


@pytest.mark.parametrize("bad", ["$(printf x)", "../x", "a b", "-x", ""])
def test_a_run_id_is_a_file_name(bad):
    with pytest.raises(ValidationError):
        bench_agent.Launch(asker="a", thread="t", run_id=bad)
    assert bench_agent.Launch(asker="a", thread="t", run_id="crit-off-2_b.1").run_id == "crit-off-2_b.1"


def test_a_report_goes_through_the_agents_counted_send_and_a_reported_job_is_not_reported_again(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(bench_agent, "DIR", tmp_path)
    monkeypatch.setattr(bench_agent, "RESULTS", tmp_path / "results")
    (tmp_path / "results" / "t1").mkdir(parents=True)
    (tmp_path / "results" / "t1" / "ledger.jsonl").write_text(
        json.dumps({"instance_id": "a", "resolved": True, "cost_usd": 0.01}) + "\n"
    )
    khive = BenchKhive(
        mail(bench_agent.ACTOR_ID, "job t1 finished with exit 0. Report it.", mid="f1"), cwd=tmp_path
    )
    twice = '<lact r1>report(run_id="t1")</lact><lact r2>report(run_id="t1")</lact>'
    agent = bench_agent.build(
        khive,
        Scripted(twice, 'OUT{posture: {done: "reported t1", awaiting: ""}}', khive=khive),
        fresh_store(),
    )
    _put_job(agent)
    run = asyncio.run(agent.wake(wait_ms=0))
    results = [e.content for e in run.record if e.kind is Kind.RESULT and e.content.spec == "report"]
    assert sorted(r.ok for r in results) == [False, True] and len(khive.sent) == 1
    assert "not again" in next(r.error for r in results if not r.ok)
    assert agent.state.get("threads").value == {THREAD: 1}  # counted on the thread, under the hop limit
    assert agent.state.get("cursor").value["sent"] == 1
    khive.mail.append(mail(bench_agent.ACTOR_ID, "job t1 finished with exit 0. Report it.", mid="f2"))
    agent.backend = Scripted(
        '<lact r3>report(run_id="t1")</lact>', 'OUT{posture: {done: "x", awaiting: ""}}', khive=khive
    )
    run = asyncio.run(agent.wake(wait_ms=0))
    result = next(e.content for e in run.record if e.kind is Kind.RESULT and e.content.spec == "report")
    assert not result.ok and "not again" in result.error and len(khive.sent) == 1


def test_the_acting_profile_is_the_typed_pair_and_has_no_prose_send(tmp_path):
    agent = bench_agent.build(BenchKhive(cwd=tmp_path), Scripted(), fresh_store())
    assert set(agent.actor.subset(bench_agent.ACTING)) == {"launch", "report"}


def test_the_owner_and_itself_are_trusted_without_a_trust_list(monkeypatch):
    """Imported and configured without `--trust`: the owner's ask and its own finish mail still act."""
    monkeypatch.setattr(bench_agent, "TRUSTED", frozenset())
    for who in ("lambda:a", "lambda:a:bench"):
        assert bench_agent.profile_for([mail(who, "go")]) is bench_agent.ACTING
    assert bench_agent.profile_for([mail("lambda:b", "go")]) is bench_agent.READER


@pytest.mark.parametrize("unset", ["ACTOR_ID", "OWNER"])
def test_an_unconfigured_identity_is_refused_before_anything_is_built(tmp_path, monkeypatch, unset):
    monkeypatch.setattr(bench_agent, "DIR", tmp_path / "bench")
    khive = BenchKhive(cwd=tmp_path)
    monkeypatch.setattr(bench_agent, unset, None)
    with pytest.raises(AgentError, match="identity is not configured"):
        bench_agent.build(khive, Scripted(), fresh_store())
    assert not (tmp_path / "bench").exists()  # refused before its directories were made


def _job(tmp_path, monkeypatch, run_id: str, rows: list[dict], *, selected=None, exit=None, log="wall 3s\n"):
    """A finished job's evidence on disk: the ledger, the job log (naming its launch record when
    `selected` is given) and the exit file the launcher writes (when `exit` is given)."""
    monkeypatch.setattr(bench_agent, "DIR", tmp_path)
    monkeypatch.setattr(bench_agent, "RESULTS", tmp_path / "results")
    out = tmp_path / "results" / run_id
    out.mkdir(parents=True)
    (out / "ledger.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (tmp_path / "jobs").mkdir(exist_ok=True)
    if selected is not None:
        record = out / "manifest-x.json"
        record.write_text(json.dumps({"selected": selected}))
        log = f"set=mini50 rows={len(selected)}\nlaunch record {record}\n" + log
    (tmp_path / "jobs" / f"{run_id}.log").write_text(log)
    if exit is not None:
        (tmp_path / "jobs" / f"{run_id}.exit").write_text(f"{exit}\n")


def test_finish_report_carries_selected_set_and_exit(tmp_path, monkeypatch):
    a = {"instance_id": "a", "resolved": True, "cost_usd": 0.01}
    # with no `or 'exit' in row` shortcut: a run that died after one instance, no exit status and no
    # launch record, is incomplete, whatever keys the row has
    _job(tmp_path / "died", monkeypatch, "t1", [a], log="Traceback ...\nRuntimeError: boom\n")
    row = bench_agent.row_for("t1")
    assert row.get("complete") is False and row.get("exit") is None and row.get("selected") is None
    assert any("exit" in r for r in row["reasons"]) and any("launch record" in r for r in row["reasons"])
    # a nonzero exit, written by the launcher's own line (a child that exits 3), never read from the mail
    base = tmp_path / "failed"
    _job(base, monkeypatch, "t2", [a], selected=["a"])
    monkeypatch.setattr(bench_agent, "job_line", lambda req, run_id: "(exit 3)")
    monkeypatch.setattr(bench_agent, "notify_line", lambda run_id: "true")
    khive = BenchKhive(mail("lambda:a", "run it"), cwd=base)
    ask = f'<lact l1>launch(asker="lambda:a", thread="{THREAD}", run_id="t2")</lact>'
    agent = bench_agent.build(
        khive, Scripted(ask, 'OUT{posture: {done: "x", awaiting: "t2"}}', khive=khive), fresh_store()
    )
    asyncio.run(agent.wake(wait_ms=0))
    exit_file = base / "jobs" / "t2.exit"
    for _ in range(50):  # the child is a shell line; give it a moment
        if exit_file.exists() and exit_file.read_text().endswith("\n"):
            break
        time.sleep(0.1)
    assert exit_file.exists() and exit_file.read_text() == "3\n"
    row = bench_agent.row_for("t2")
    assert row["exit"] == 3 and row["complete"] is False and any("exit 3" in r for r in row["reasons"])
    # exit 0 over part of the selected set: the missing instances are named
    _job(tmp_path / "partial", monkeypatch, "t3", [a], selected=["a", "b", "c"], exit=0)
    row = bench_agent.row_for("t3")
    assert row["complete"] is False and row["selected"] == 3 and row["missing"] == ["b", "c"]
    # the control: exit 0 and every selected instance in the ledger
    b = {"instance_id": "b", "resolved": False, "cost_usd": None}
    _job(tmp_path / "whole", monkeypatch, "t4", [a, b], selected=["a", "b"], exit=0)
    row = bench_agent.row_for("t4")
    assert row["complete"] is True and row["reasons"] == [] and row["missing"] == [] and row["exit"] == 0
    # and a row whose spend nobody measured is counted as unknown, not as $0
    assert row["cost_usd"] == 0.01 and row["cost_complete"] is False and row["cost_unknown_rows"] == 1


class RememberFailsOnce(BenchKhive):
    def __init__(self, *inbox, cwd=None):
        super().__init__(*inbox, cwd=cwd)
        self.failed = False

    async def exec(self, ops):
        if "memory.remember" in ops and not self.failed:
            self.failed = True
            raise RuntimeError("AdmissionDeadlineExpired")
        return await super().exec(ops)


def test_delivery_receipt_precedes_remember_failure(tmp_path, monkeypatch):
    # the row went out, the memory write failed, a second finish came
    _job(
        tmp_path,
        monkeypatch,
        "t1",
        [{"instance_id": "a", "resolved": True, "cost_usd": 0.01}],
        selected=["a"],
        exit=0,
    )
    khive = RememberFailsOnce(
        mail(bench_agent.ACTOR_ID, "job t1 finished with exit 0. Report it.", mid="f1"), cwd=tmp_path
    )
    done = 'OUT{posture: {done: "x", awaiting: ""}}'
    agent = bench_agent.build(
        khive, Scripted('<lact r1>report(run_id="t1")</lact>', done, khive=khive), fresh_store()
    )
    _put_job(agent)
    run = asyncio.run(agent.wake(wait_ms=0))
    result = next(e.content for e in run.record if e.kind is Kind.RESULT and e.content.spec == "report")
    assert len(khive.sent) == 1 and not result.ok and "m1" in str(result.error)  # the receipt is named
    stored = agent.state.get("jobs").value["t1"]
    assert stored["reported"] and stored["sent"] == "m1" and stored["remember_pending"] is True
    khive.mail.append(mail(bench_agent.ACTOR_ID, "job t1 finished with exit 0. Report it.", mid="f2"))
    agent.backend = Scripted('<lact r2>report(run_id="t1")</lact>', done, khive=khive)
    run = asyncio.run(agent.wake(wait_ms=0))
    result = next(e.content for e in run.record if e.kind is Kind.RESULT and e.content.spec == "report")
    assert len(khive.sent) == 1, f"the asker got the row {len(khive.sent)} times"
    assert result.ok and len(khive.ops) == 1 and "memory.remember" in khive.ops[0]  # the retry wrote it
    assert agent.state.get("jobs").value["t1"]["remember_pending"] is False


def test_the_report_send_is_keyed_by_the_job_so_a_lost_receipt_replays_and_never_mails_twice(
    tmp_path, monkeypatch
):
    _job(tmp_path, monkeypatch, "t1", [{"instance_id": "a", "resolved": True, "cost_usd": 0.01}])
    khive = BenchKhive(
        mail(bench_agent.ACTOR_ID, "job t1 finished with exit 0. Report it.", mid="f1"), cwd=tmp_path
    )
    keys = []

    async def send(to, content, *, subject=None, thread_id=None, idempotency_key=None):
        keys.append(idempotency_key)
        return await BenchKhive.send(khive, to, content, subject=subject, thread_id=thread_id)

    khive.send = send
    agent = bench_agent.build(
        khive,
        Scripted(
            '<lact r1>report(run_id="t1")</lact>', 'OUT{posture: {done: "x", awaiting: ""}}', khive=khive
        ),
        fresh_store(),
    )
    _put_job(agent)
    asyncio.run(agent.wake(wait_ms=0))
    assert keys == ["bench:lambda:a:bench:t1:report"]


def test_a_report_retried_after_a_lost_receipt_sends_the_body_it_first_sent(tmp_path, monkeypatch):
    # the store replays a keyed send under its key: the retry must carry the same body, not the row
    # as the ledger stands now, or the receipt names one report and the remembered row another
    _job(tmp_path, monkeypatch, "t1", [{"instance_id": "a", "resolved": True, "cost_usd": 0.01}])
    khive = BenchKhive(
        mail(bench_agent.ACTOR_ID, "job t1 finished with exit 0. Report it.", mid="f1"), cwd=tmp_path
    )
    bodies = []

    async def send(to, content, *, subject=None, thread_id=None, idempotency_key=None):
        bodies.append(content)
        if len(bodies) == 1:
            raise TimeoutError("accepted by the store; the receipt was lost")
        return await BenchKhive.send(khive, to, content, subject=subject, thread_id=thread_id)

    khive.send = send
    done = 'OUT{posture: {done: "x", awaiting: ""}}'
    agent = bench_agent.build(
        khive, Scripted('<lact r1>report(run_id="t1")</lact>', done, khive=khive), fresh_store()
    )
    _put_job(agent)
    run = asyncio.run(agent.wake(wait_ms=0))
    result = next(e.content for e in run.record if e.kind is Kind.RESULT and e.content.spec == "report")
    assert not result.ok and "TimeoutError" in str(result.error) and len(bodies) == 1
    assert agent.state.get("jobs").value["t1"]["row"] == json.loads(bodies[0])  # frozen before the send
    ledger = tmp_path / "results" / "t1" / "ledger.jsonl"
    ledger.write_text(
        ledger.read_text() + json.dumps({"instance_id": "b", "resolved": False, "cost_usd": 0.02}) + "\n"
    )
    khive.mail.append(mail(bench_agent.ACTOR_ID, "job t1 finished with exit 0. Report it.", mid="f2"))
    agent.backend = Scripted('<lact r2>report(run_id="t1")</lact>', done, khive=khive)
    run = asyncio.run(agent.wake(wait_ms=0))
    result = next(e.content for e in run.record if e.kind is Kind.RESULT and e.content.spec == "report")
    assert result.ok and bodies[1] == bodies[0] and len(khive.sent) == 1
    assert agent.state.get("jobs").value["t1"]["row"] == json.loads(bodies[0])


def test_a_launch_during_a_reports_send_is_not_written_away_by_the_report(tmp_path, monkeypatch):
    # the report read the jobs map, awaited its send, and wrote that map back whole: a launch that
    # ran in the same turn, during the await, was gone from the map, its child running unrecorded
    _job(tmp_path, monkeypatch, "t1", [{"instance_id": "a", "resolved": True, "cost_usd": 0.01}])
    fake_child(tmp_path / "results", monkeypatch)
    monkeypatch.setattr(bench_agent, "DIR", tmp_path)

    class Slow(BenchKhive):
        async def send(self, *a, **k):
            for _ in range(20):  # the transport's round trip: the launch runs meanwhile
                await asyncio.sleep(0)
            return await super().send(*a, **k)

    khive = Slow(mail("lambda:a", "report t1 and launch t2"), cwd=tmp_path)
    program = (
        '<lact r1>report(run_id="t1")</lact>'
        f'<lact l1>launch(asker="lambda:a", thread="{THREAD}", set="mini50", limit=1, run_id="t2")</lact>'
    )
    agent = bench_agent.build(
        khive, Scripted(program, 'OUT{posture: {done: "x", awaiting: "t2"}}', khive=khive), fresh_store()
    )
    _put_job(agent)
    run = asyncio.run(agent.wake(wait_ms=0))
    results = {e.content.spec: e.content for e in run.record if e.kind is Kind.RESULT}
    assert results["report"].ok and results["launch"].ok
    jobs = agent.state.get("jobs").value
    assert set(jobs) == {"t1", "t2"} and jobs["t1"]["reported"] and jobs["t2"]["pid"] > 0
