"""What the model is told about a command that did not succeed follows whether its handler was entered:
refused or cancelled before it, `not executed`; entered and failed, `failed after starting`; cancelled
while it ran, `completion unknown`. The fact rides the RESULT entry (ADR-0005/C1, ADR-0006/C2)."""

import asyncio
import json

from pydantic import BaseModel

from hub.chat import entry_row, load_log
from lion1 import Actor, Kind, Profile, Result, RunRequest
from tests.test_actor import scripted


class Touch(BaseModel):
    path: str


class Mark(BaseModel):
    path: str


class Hold(BaseModel):
    text: str = ""


class Cut(BaseModel):
    target: str


class Guarded(BaseModel):
    text: str


def _exec_actor(name: str, entered: list[str], parked: list[str]) -> Actor:
    actor = Actor(name, privileges=("write",))

    @actor.handler(Touch)
    async def touch(req: Touch, ctx):
        entered.append(ctx.alias)  # the side effect happened
        raise RuntimeError("disk full after the write")

    @actor.handler(Mark)
    async def mark(req: Mark, ctx):
        entered.append(ctx.alias)
        return f"marked {req.path}"

    @actor.handler(Hold)
    async def hold(req: Hold, ctx):
        entered.append(ctx.alias)
        parked.append(ctx.alias)
        await asyncio.Event().wait()

    @actor.handler(Cut, effect="pure")  # beside the command it stops: a write would wait for it
    async def cut(req: Cut, ctx):
        while req.target not in parked:
            await asyncio.sleep(0)
        ctx.run.pending[req.target].cancel()
        return f"cancelled {req.target}"

    @actor.handler(Guarded, requires=("write",))
    async def guarded(req: Guarded, ctx):
        entered.append(ctx.alias)
        return req.text

    return actor


def _exec_run(actor: Actor, turn: str):
    run = asyncio.run(
        actor.run(Profile("p"), RunRequest(inputs=("go",), max_rounds=2), scripted(turn, "done"))
    )
    notices = [e.content for e in run.record if e.kind is Kind.SYSTEM]
    return run, notices[1]


def _exec_block(notice: str, header: str) -> list[str]:
    """The aliases listed under one header of a notification."""
    out, inside = [], False
    for ln in notice.splitlines():
        if not ln.startswith("  "):
            inside = ln == header
        elif inside:
            out.append(ln.strip().split(":", 1)[0])
    return out


def _exec_facts(entries) -> dict[str, str | None]:
    return {e.alias: e.meta.get("execution") for e in entries if e.kind is Kind.RESULT}


def test_started_failure_is_not_reported_as_unexecuted():
    entered: list[str] = []
    run, notice = _exec_run(_exec_actor("exec-started", entered, []), '<lact t1>touch(path="a")</lact>')
    assert entered == ["t1"]
    assert "t1" not in _exec_block(notice, "not executed:")
    assert _exec_block(notice, "failed after starting:") == ["t1"]
    assert "t1: RuntimeError: disk full after the write" in notice
    assert _exec_facts(run.record) == {"t1": "started"}


def test_after_hook_refused_reason_does_not_erase_started():
    entered: list[str] = []
    actor = _exec_actor("exec-after-hook", entered, [])

    @actor.after
    def veto(run, la, req, result):
        return Result(result.spec, False, error="refused: the audit hook vetoed it", reason="refused")

    run, notice = _exec_run(actor, '<lact m1>mark(path="a")</lact>')
    assert entered == ["m1"] and run.record.get("m1").content.reason == "refused"  # the hook's result stands
    assert "m1" not in _exec_block(notice, "not executed:")
    assert _exec_block(notice, "failed after starting:") == ["m1"]
    assert _exec_facts(run.record) == {"m1": "started"}


def test_cancel_after_handler_entry_reads_completion_unknown():
    entered: list[str] = []
    actor = _exec_actor("exec-cancel-started", entered, [])
    run, notice = _exec_run(actor, '<lact h>hold()</lact><lact k>cut(target="h")</lact>')
    assert entered == ["h"] and run.record.get("h").content.reason == "cancelled"
    assert "h" not in _exec_block(notice, "not executed:")
    assert _exec_block(notice, "completion unknown:") == ["h"]
    assert _exec_facts(run.record) == {"h": "completion unknown", "k": "started"}
    assert run.pending == {}


def test_a_started_command_whose_result_cannot_land_is_not_told_unexecuted():
    actor = Actor("exec-no-landing")

    @actor.handler(Mark)
    async def squat(req: Mark, ctx):
        ctx.run.record.append(Kind.INPUT, "taken", alias=ctx.alias)  # its own alias, gone before it lands
        return "ran"

    run, notice = _exec_run(actor, '<lact s1>mark(path="a")</lact>')
    assert run.record.get("s1").kind is Kind.INPUT  # no RESULT: the alias was taken while the call ran
    assert "s1" not in _exec_block(notice, "not executed:")
    assert _exec_block(notice, "failed after starting:") == ["s1"]


# nothing reached the handler, so `not executed` is the truth


def test_privilege_refusal_is_not_executed():
    entered: list[str] = []
    run, notice = _exec_run(_exec_actor("exec-privilege", entered, []), '<lact g1>guarded(text="x")</lact>')
    assert entered == [] and run.record.get("g1").content.reason == "privilege"
    assert _exec_block(notice, "not executed:") == ["g1"]


def test_validation_refusal_is_not_executed():
    entered: list[str] = []
    run, notice = _exec_run(_exec_actor("exec-invalid", entered, []), "<lact t1>touch()</lact>")
    assert entered == [] and run.record.get("t1").content.reason == "invalid"
    assert _exec_block(notice, "not executed:") == ["t1"]


def test_before_hook_refusal_is_not_executed():
    entered: list[str] = []
    actor = _exec_actor("exec-before", entered, [])
    actor.before(lambda run, la, req: "closed for the night")
    run, notice = _exec_run(actor, '<lact t1>touch(path="a")</lact>')
    assert entered == [] and run.record.get("t1").content.reason == "refused"
    assert _exec_block(notice, "not executed:") == ["t1"]


def test_cancel_while_waiting_in_a_before_hook_is_not_executed():
    entered: list[str] = []
    parked: list[str] = []
    actor = _exec_actor("exec-cancel-hook", entered, parked)

    @actor.before
    async def slow_gate(run, la, req):
        if la.alias == "h":
            parked.append("h")
            await asyncio.Event().wait()

    run, notice = _exec_run(actor, '<lact h>hold()</lact><lact k>cut(target="h")</lact>')
    assert entered == [] and run.record.get("h").content.reason == "cancelled"
    assert _exec_block(notice, "not executed:") == ["h"]
    assert run.pending == {}


def test_a_command_that_ran_and_failed_is_not_told_as_not_executed():
    actor = Actor("exec-ran-then-raised")
    written = []

    @actor.handler(Touch)
    async def touch(req: Touch, ctx):
        written.append(req.path)
        raise RuntimeError("disk full after the write")

    run = asyncio.run(
        actor.run(
            Profile("p"),
            RunRequest(inputs=("go",), max_rounds=2),
            scripted('<lact t1>touch(path="a")</lact>', "x"),
        )
    )
    assert written == ["a"]
    notice = [e.content for e in run.record if e.kind is Kind.SYSTEM][1]
    told_not_run = notice.split("not executed:", 1)[1] if "not executed:" in notice else ""
    assert "t1:" not in told_not_run, notice


def test_execution_fact_survives_log_round_trip(tmp_path):
    entered: list[str] = []
    run, _ = _exec_run(
        _exec_actor("exec-log", entered, []),
        '<lact t1>touch(path="a")</lact><lact m1>mark(path="b")</lact><lact g1>guarded(text="x")</lact>',
    )
    facts = _exec_facts(run.record)
    assert facts == {"t1": "started", "m1": "started", "g1": "not executed"}
    log = tmp_path / "log.jsonl"
    log.write_text("".join(json.dumps(entry_row(e), default=str) + "\n" for e in run.record))
    assert _exec_facts(load_log(log)) == facts
    # a log written before the fact existed still loads, and its absence claims nothing either way
    old = {"seq": 0, "kind": "RESULT", "name": "_r0", "round": 1, "alias": "x", "meta": {}}
    old["content"] = {"spec": "touch", "ok": False, "value": None, "error": "boom", "reason": "failed"}
    (tmp_path / "old.jsonl").write_text(json.dumps(old) + "\n")
    assert _exec_facts(load_log(tmp_path / "old.jsonl")) == {"x": None}
