"""`lion run`, `lion flow` and `lion result` (ADR-0026 C1 to C4, D1 to D4) over the in-memory pack: the id
before the first call, the step rows and the runs they name, what a template reads, the result mail, and
the rows read back by id."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import sys
from pathlib import Path

import pytest
from lion_cli import submit
from lion_cli.main import main
from lion_cli.submit import SubmitError, cast, profile_from

from lion1.actor import DEFAULT_HANDLERS
from lion1.khive import Khive, KhiveError

PROFILES = """
[profile.writer]
system = "You write."
tools = ["files"]
model = "fake/writer"

[profile.mailer]
system_file = "mailer.md"
tools = ["files", "khive", "send"]
model = "fake/mailer"
defaults = { max_turns = 3 }
"""

TWO = """
[flow.two]
[[flow.two.step]]
name = "plan"
profile = "writer"
say = "plan {topic}"
[[flow.two.step]]
name = "write"
profile = "writer"
say = "write from {plan.output}"
"""


def lion(*argv: str) -> int | str | None:
    try:
        main(list(argv))
    except SystemExit as e:
        return e.code
    return 0


def inputs(messages) -> list[str]:
    """The inputs a call carries, each without the `[_inN] ` its message is framed with."""
    return [re.sub(r"^\[_in\d+\] ", "", str(m.content)) for m in messages if m.role == "user"]


def out(result) -> str:
    """A turn that ends the job with `result`."""
    return "OUT{result: " + json.dumps(result) + "}"


def said(messages) -> str:
    """The first input a call carries: the job's text."""
    return inputs(messages)[0]


def backends(monkeypatch, **by_model) -> None:
    """`backend_for` answering each model with its function of the messages, one backend per run."""

    def backend_for(model, options=None, *, cwd=None, codex=None):
        answer = by_model[model]

        async def backend(messages):
            got = answer(messages)
            return await got if asyncio.iscoroutine(got) else got

        return backend

    monkeypatch.setattr(submit, "backend_for", backend_for)


@pytest.fixture
def actor_dir(tmp_path, console_khive):
    (tmp_path / "profiles.toml").write_text(PROFILES)
    (tmp_path / "mailer.md").write_text("You report.")
    return tmp_path


# C1


def test_lion_run_prints_the_id_before_the_first_call_and_the_row_exists_then(
    actor_dir, console_khive, monkeypatch, capsys
):
    seen = {}

    def first(messages):
        seen["printed"] = capsys.readouterr().out.split()
        seen["rows"] = {k: dict(v) for k, v in console_khive.runs.items()}
        return 'OUT{result: "done"}'

    backends(monkeypatch, **{"fake/writer": first})
    assert lion("run", "--dir", str(actor_dir), "--profile", "writer", "--say", "write it") == 0
    (run_id,) = seen["rows"]  # the run's row, and no other, exists when the first call goes out
    assert seen["printed"] == [run_id]  # and its id is all that printed before it
    assert seen["rows"][run_id]["outcome"] is None and seen["rows"][run_id]["profile"] == "writer"
    out = capsys.readouterr().out
    assert out.startswith(f"run {run_id} success\n") and '"result": "done"' in out
    assert console_khive.runs[run_id]["outcome"] == "success"


def test_lion_run_fills_its_text_from_vars_and_takes_each_file_as_an_input(actor_dir, monkeypatch, capsys):
    (actor_dir / "notes.txt").write_text("the notes")
    seen = []

    def answer(messages):
        seen.extend(inputs(messages))
        return 'OUT{result: "ok"}'

    backends(monkeypatch, **{"fake/writer": answer})
    argv = ["run", "--dir", str(actor_dir), "--profile", "writer", "--say", "fix {what} {{as is}}"]
    assert lion(*argv, "--var", "what=the bug", "--file", str(actor_dir / "notes.txt")) == 0
    assert seen[:2] == ["fix the bug {as is}", "the notes"]
    # without a variable the text is taken as written: its braces are the job's own
    assert lion("run", "--dir", str(actor_dir), "--profile", "writer", "--say", "map {a: 1}") == 0
    assert "map {a: 1}" in seen
    assert "{nope} names nothing" in str(lion(*argv[:-1], "say {nope}", "--var", "what=x"))


def test_lion_flow_prints_the_flow_id_then_each_steps_as_it_opens(
    actor_dir, console_khive, monkeypatch, capsys
):
    (actor_dir / "flows.toml").write_text(TWO)
    printed = []

    def answer(messages):
        printed.append(capsys.readouterr().out.splitlines())
        return out(said(messages).split()[0])

    backends(monkeypatch, **{"fake/writer": answer})
    assert lion("flow", "--dir", str(actor_dir), "--name", "two", "--var", "topic=tea") == 0
    flow_id = printed[0][0]
    (plan, write) = (s["run_id"] for s in console_khive.steps[flow_id].values())
    assert printed == [[flow_id, f"step plan {plan}"], [f"step write {write}"]]
    assert console_khive.runs[flow_id]["profile"] == "flow:two"
    assert capsys.readouterr().out.startswith(f"flow two {flow_id} success\n")


# C2


def test_a_flows_steps_are_runs_of_their_own_named_by_the_flow_runs_step_rows(
    actor_dir, console_khive, monkeypatch
):
    (actor_dir / "flows.toml").write_text(TWO)
    backends(monkeypatch, **{"fake/writer": lambda m: out(said(m).split()[0])})
    assert lion("flow", "--dir", str(actor_dir), "--name", "two", "--var", "topic=tea") == 0
    (flow_id,) = console_khive.steps
    flow = console_khive.runs[flow_id]
    assert flow["outcome"] == "success" and json.loads(flow["output"]) == {"result": "write"}
    assert flow["turns"] == 0 and console_khive.messages[flow_id] == {}  # the flow made no call
    steps = console_khive.steps[flow_id]
    assert [(i, s["name"], s["profile"], s["outcome"]) for i, s in steps.items()] == [
        (0, "plan", "writer", "success"),
        (1, "write", "writer", "success"),
    ]
    for s in steps.values():
        run = console_khive.runs[s["run_id"]]
        assert run["session_id"] == s["run_id"] and json.loads(run["trigger"]) == [flow_id]
        assert run["outcome"] == "success" and console_khive.messages[s["run_id"]]  # its own messages


def test_a_fanout_step_writes_one_step_row_and_one_run_per_item_in_order(
    actor_dir, console_khive, monkeypatch
):
    (actor_dir / "flows.toml").write_text(
        """
[flow.fan]
parallel = 2
[[flow.fan.step]]
name = "each_file"
profile = "writer"
say = "read {item}"
each = "files"
[[flow.fan.step]]
name = "sum"
profile = "writer"
say = "sum {each_file.output}"
"""
    )
    width = {"now": 0, "most": 0}

    async def answer(messages):
        text = said(messages)
        if text.startswith("sum "):
            return out(text)
        width["now"] += 1
        width["most"] = max(width["most"], width["now"])
        await asyncio.sleep(0.01)
        width["now"] -= 1
        return out(text.split()[1] + "!")

    backends(monkeypatch, **{"fake/writer": answer})
    assert lion("flow", "--dir", str(actor_dir), "--name", "fan", "--var", 'files=["a", "b", "c"]') == 0
    assert width["most"] == 2  # three items, two at once
    (flow_id,) = console_khive.steps
    steps = console_khive.steps[flow_id]
    assert [s["name"] for s in steps.values()] == ["each_file[0]", "each_file[1]", "each_file[2]", "sum"]
    runs = [s["run_id"] for s in steps.values()]
    assert len(set(runs)) == 4
    for k, item in enumerate("abc"):  # each item's run read its own item
        texts = [r["text"] for r in console_khive.messages[runs[k]].values() if r["role"] == "user"]
        assert texts == [f"read {item}"]
    assert json.loads(steps[3]["inputs_from"]) == ["each_file"]
    summed = json.loads(console_khive.runs[flow_id]["output"])["result"]
    assert summed == 'sum [{"result": "a!"}, {"result": "b!"}, {"result": "c!"}]'  # in item order


def test_a_failed_step_closes_the_flow_stopped_and_opens_no_step_after_it(
    actor_dir, console_khive, monkeypatch, capsys
):
    (actor_dir / "flows.toml").write_text(
        TWO + '[[flow.two.step]]\nname = "after"\nprofile = "writer"\nsay = "after {write.text}"\n'
    )

    def answer(messages):
        if said(messages).startswith("write"):
            raise RuntimeError("the model is down")
        return 'OUT{result: "planned"}'

    backends(monkeypatch, **{"fake/writer": answer})
    assert lion("flow", "--dir", str(actor_dir), "--name", "two", "--var", "topic=tea") == 1
    (flow_id,) = console_khive.steps
    steps = console_khive.steps[flow_id]
    assert [(s["name"], s["outcome"]) for s in steps.values()] == [("plan", "success"), ("write", "stopped")]
    assert console_khive.runs[steps[1]["run_id"]]["outcome"] == "stopped"
    flow = console_khive.runs[flow_id]
    assert flow["outcome"] == "stopped"
    failed = json.loads(flow["output"])
    assert failed["failed"] == "write" and "the model is down" in failed["error"]
    assert len(console_khive.runs) == 3  # the flow, plan and write: `after` never opened
    assert f"flow two {flow_id} stopped at write" in capsys.readouterr().out


# C3


def test_a_steps_inputs_from_lists_exactly_the_steps_its_template_read(actor_dir, console_khive, monkeypatch):
    (actor_dir / "flows.toml").write_text(
        """
[flow.three]
output = "b"
[[flow.three.step]]
name = "a"
profile = "writer"
say = "first"
[[flow.three.step]]
name = "b"
profile = "writer"
say = "second, after {a.text}"
[[flow.three.step]]
name = "c"
profile = "writer"
say = "third, {a.output} about {topic}, {{literal}}"
"""
    )
    seen = []

    def answer(messages):
        seen.append(said(messages))
        return "a word\n" + out(said(messages).split(",")[0])

    backends(monkeypatch, **{"fake/writer": answer})
    assert lion("flow", "--dir", str(actor_dir), "--name", "three", "--var", "topic=tea") == 0
    (flow_id,) = console_khive.steps
    reads = [json.loads(s["inputs_from"]) for s in console_khive.steps[flow_id].values()]
    assert reads == [[], ["a"], ["a"]]  # c read a and a variable, never b
    assert seen[1] == 'second, after a word\nOUT{result: "first"}'  # a's last text, whole
    assert seen[2] == 'third, {"result": "first"} about tea, {literal}'
    assert json.loads(console_khive.runs[flow_id]["output"]) == {"result": "second"}  # `output` names b


@pytest.mark.parametrize(
    ("say", "each", "names"),
    [
        ("{nope}", None, "{nope} names nothing"),
        ("{later.text}", None, "{later.text} names nothing"),  # a step after it
        ("{plan.texts}", None, "{plan.texts} names nothing"),
        ("{item}", None, "only a fanout step has an item"),
        ("a lone { here", None, "a lone '{'"),
        ("x {item}", "missing", "each missing names nothing"),
        ("x {item}", "topic", "each topic: the variable holds no JSON list"),
        ("x {item}", "plan.text", "each plan.text names nothing"),
    ],
)
def test_a_template_naming_nothing_refuses_the_flow_before_any_row(
    actor_dir, console_khive, monkeypatch, say, each, names
):
    bad = f'[[flow.two.step]]\nname = "bad"\nprofile = "writer"\nsay = "{say}"\n'
    bad += f'each = "{each}"\n' if each else ""
    later = '[[flow.two.step]]\nname = "later"\nprofile = "writer"\nsay = "z"\n'
    (actor_dir / "flows.toml").write_text(TWO + bad + later)
    backends(monkeypatch, **{"fake/writer": lambda m: pytest.fail("no call goes out")})
    refused = str(lion("flow", "--dir", str(actor_dir), "--name", "two", "--var", "topic=tea"))
    assert "step bad" in refused and names in refused
    assert console_khive.runs == {} and console_khive.steps == {} and console_khive.sent == []


def test_a_flow_whose_step_names_a_missing_profile_refuses_before_any_row(actor_dir, console_khive):
    nobody = TWO.replace('profile = "writer"\nsay = "write', 'profile = "nobody"\nsay = "w')
    (actor_dir / "flows.toml").write_text(nobody)
    refused = str(lion("flow", "--dir", str(actor_dir), "--name", "two", "--var", "topic=tea"))
    assert "no [profile.nobody]; there is mailer, writer" in refused and console_khive.runs == {}


# C4


def test_a_run_that_ends_mails_the_submitter_its_id_outcome_and_output_head(
    actor_dir, console_khive, monkeypatch
):
    backends(monkeypatch, **{"fake/writer": lambda m: out(list(range(60)))})
    assert lion("run", "--dir", str(actor_dir), "--profile", "writer", "--say", "count") == 0
    (run_id,) = console_khive.runs
    (mail,) = console_khive.sent
    assert mail["to"] == "console" and mail["self_send"] is True  # the directory's own actor
    assert mail["subject"] == f"run {run_id} success" and "thread_id" not in mail
    body = mail["content"].splitlines()
    assert body[:3] == ["{", '  "result": [', "    0,"] and body[39] == "    37,"
    assert body[40] == "[24 more lines]" and body[-1] == f"lion result --dir {actor_dir} {run_id}"
    argv = ["run", "--dir", str(actor_dir), "--profile", "writer", "--say", "count", "--notify", "a:other"]
    assert lion(*argv, "--thread", "t9") == 0
    other = console_khive.sent[1]
    assert other["to"] == "a:other" and other["thread_id"] == "t9" and "self_send" not in other


def test_a_flow_that_ends_mails_its_name_id_and_outcome(actor_dir, console_khive, monkeypatch):
    (actor_dir / "flows.toml").write_text(TWO)
    backends(monkeypatch, **{"fake/writer": lambda m: 'OUT{result: "x"}'})
    assert lion("flow", "--dir", str(actor_dir), "--name", "two", "--var", "topic=tea") == 0
    (flow_id,) = console_khive.steps
    (mail,) = console_khive.sent
    assert mail["subject"] == f"flow two {flow_id} success"
    assert mail["content"].startswith('{\n  "result": "x"\n}')


def test_a_result_mail_that_fails_leaves_the_rows_complete_and_prints_the_error(
    actor_dir, console_khive, monkeypatch, capsys
):
    console_khive.refuse_send = "comm is down"
    backends(monkeypatch, **{"fake/writer": lambda m: 'OUT{result: "kept"}'})
    assert lion("run", "--dir", str(actor_dir), "--profile", "writer", "--say", "go") == 1
    (run_id,) = console_khive.runs
    row = console_khive.runs[run_id]
    assert row["outcome"] == "success" and json.loads(row["output"]) == {"result": "kept"}
    assert console_khive.sessions[run_id]["ended"] is not None and console_khive.messages[run_id]
    err = capsys.readouterr().err
    assert "the result mail was not sent: KhiveError: comm is down" in err


@pytest.fixture
def leaving(monkeypatch):
    """`leaving()`, called in the test's body, for the call phase's capture puts back any stream a
    fixture set: standard output and error become pipes, and the function it returns gives what came on
    standard output, then closes both read ends, as a caller does that took the id and went."""
    open_ends: list[int] = []
    streams: list = []

    def pipes():
        out_r, out_w = os.pipe()
        err_r, err_w = os.pipe()
        open_ends.extend((out_r, err_r))
        streams.extend((os.fdopen(out_w, "w"), os.fdopen(err_w, "w")))
        monkeypatch.setattr(sys, "stdout", streams[0])
        monkeypatch.setattr(sys, "stderr", streams[1])

        def leave() -> str:
            assert sys.stdout is streams[0]  # the run printed into the pipe, not into a capture
            got = os.read(out_r, 65536).decode()
            while open_ends:
                os.close(open_ends.pop())
            return got

        return leave

    yield pipes
    for fd in open_ends:
        os.close(fd)
    for s in streams:
        s.close()


def test_a_run_whose_reader_leaves_after_the_id_ends_its_rows_and_says_why_it_failed(
    actor_dir, console_khive, monkeypatch, leaving
):
    seen = {}
    leave = leaving()

    def first(messages):
        seen["printed"] = leave().split()
        return out("done")

    console_khive.refuse_send = "comm is down"  # so the end writes to standard error too
    backends(monkeypatch, **{"fake/writer": first})
    assert lion("run", "--dir", str(actor_dir), "--profile", "writer", "--say", "go") == 1
    (run_id,) = console_khive.runs
    assert seen["printed"] == [run_id]
    assert console_khive.runs[run_id]["outcome"] == "success"
    assert console_khive.sessions[run_id]["ended"] is not None


def test_a_flow_whose_reader_leaves_after_its_id_runs_every_step_and_mails_its_end(
    actor_dir, console_khive, monkeypatch, leaving
):
    (actor_dir / "flows.toml").write_text(TWO)
    seen = []
    leave = leaving()

    def answer(messages):
        if not seen:
            seen.extend(leave().splitlines())  # the flow's id and its first step's, then gone
        return out(said(messages).split()[0])

    backends(monkeypatch, **{"fake/writer": answer})
    assert lion("flow", "--dir", str(actor_dir), "--name", "two", "--var", "topic=tea") == 0
    (flow_id,) = console_khive.steps
    steps = console_khive.steps[flow_id]
    assert seen == [flow_id, f"step plan {steps[0]['run_id']}"]
    assert [s["outcome"] for s in steps.values()] == ["success", "success"]
    assert console_khive.runs[flow_id]["outcome"] == "success"
    (mail,) = console_khive.sent
    assert mail["subject"] == f"flow two {flow_id} success"


def test_a_cancelled_run_closes_its_rows_stopped_mails_its_end_and_exits_130(
    actor_dir, console_khive, monkeypatch
):
    async def cancelled(messages):
        raise asyncio.CancelledError

    backends(monkeypatch, **{"fake/writer": cancelled})
    assert lion("run", "--dir", str(actor_dir), "--profile", "writer", "--say", "go") == 130
    (run_id,) = console_khive.runs
    assert console_khive.runs[run_id]["outcome"] == "stopped"
    (mail,) = console_khive.sent
    assert mail["subject"] == f"run {run_id} stopped" and mail["content"].startswith(
        "error: CancelledError\n"
    )


def test_a_cancelled_flow_closes_stopped_at_its_step_mails_its_end_and_exits_130(
    actor_dir, console_khive, monkeypatch
):
    (actor_dir / "flows.toml").write_text(TWO)

    async def answer(messages):
        if said(messages).startswith("write"):
            raise asyncio.CancelledError
        return out(said(messages).split()[0])

    backends(monkeypatch, **{"fake/writer": answer})
    assert lion("flow", "--dir", str(actor_dir), "--name", "two", "--var", "topic=tea") == 130
    (flow_id,) = console_khive.steps
    flow = console_khive.runs[flow_id]
    assert flow["outcome"] == "stopped" and json.loads(flow["output"])["failed"] == "write"
    (mail,) = console_khive.sent
    assert mail["subject"] == f"flow two {flow_id} stopped"
    assert mail["content"].startswith("error: CancelledError\n")


def interrupted_at(monkeypatch, prefix: str, armed=lambda: True, times: int = 1) -> list[str]:
    """The first op starting with `prefix`, once `armed()`, cancels the command's own task `times` times,
    as interrupts landing there would, then goes on and is answered. The ops it cut into."""
    exec_ = Khive.exec
    cut: list[str] = []

    async def exec(self, ops: str) -> list:
        if ops.startswith(prefix) and armed() and not cut:
            cut.append(ops)
            (command,) = [
                t for t in asyncio.all_tasks() if t.get_coro().__name__ in ("run_main", "flow_main")
            ]
            for _ in range(times):
                command.cancel()
                for _ in range(3):
                    await asyncio.sleep(0)
        return await exec_(self, ops)

    monkeypatch.setattr(Khive, "exec", exec)
    return cut


def test_a_run_interrupted_while_its_mail_is_sent_sends_it_once_then_exits_130(
    actor_dir, console_khive, monkeypatch, capsys
):
    backends(monkeypatch, **{"fake/writer": lambda m: out("done")})
    cut = interrupted_at(monkeypatch, "comm.send(")
    assert lion("run", "--dir", str(actor_dir), "--profile", "writer", "--say", "go") == 130
    (run_id,) = console_khive.runs
    assert cut and console_khive.runs[run_id]["outcome"] == "success"
    (mail,) = console_khive.sent
    assert mail["subject"] == f"run {run_id} success"
    assert capsys.readouterr().out.splitlines()[1] == f"run {run_id} success"  # its end is said


def test_a_run_interrupted_while_its_row_is_read_back_still_mails_its_end_then_exits_130(
    actor_dir, console_khive, monkeypatch
):
    failed: list[bool] = []

    async def broken(messages):
        failed.append(True)
        raise RuntimeError("the backend broke")

    backends(monkeypatch, **{"fake/writer": broken})
    cut = interrupted_at(monkeypatch, 'lion.get(table="runs"', armed=lambda: bool(failed))
    assert lion("run", "--dir", str(actor_dir), "--profile", "writer", "--say", "go") == 130
    (run_id,) = console_khive.runs
    assert cut and console_khive.runs[run_id]["outcome"] == "stopped"
    (mail,) = console_khive.sent
    assert mail["subject"] == f"run {run_id} stopped"
    assert mail["content"].startswith("error: RuntimeError: the backend broke\n")
    assert "could not be read back" not in mail["content"]  # the read-back finished


def test_a_flow_interrupted_while_its_mail_is_sent_sends_it_once_then_exits_130(
    actor_dir, console_khive, monkeypatch
):
    (actor_dir / "flows.toml").write_text(TWO)
    backends(monkeypatch, **{"fake/writer": lambda m: out(said(m).split()[0])})
    cut = interrupted_at(monkeypatch, "comm.send(")
    assert lion("flow", "--dir", str(actor_dir), "--name", "two", "--var", "topic=tea") == 130
    (flow_id,) = console_khive.steps
    assert cut and console_khive.runs[flow_id]["outcome"] == "success"
    (mail,) = console_khive.sent
    assert mail["subject"] == f"flow two {flow_id} success"


def test_a_run_interrupted_twice_while_its_mail_is_sent_stops_at_once_and_says_how_to_read_it(
    actor_dir, console_khive, monkeypatch, capsys
):
    backends(monkeypatch, **{"fake/writer": lambda m: out("done")})
    cut = interrupted_at(monkeypatch, "comm.send(", times=2)
    assert lion("run", "--dir", str(actor_dir), "--profile", "writer", "--say", "go") == 130
    (run_id,) = console_khive.runs
    assert cut and console_khive.runs[run_id]["outcome"] == "success"  # its row closed before its end
    assert console_khive.sent == []
    printed = capsys.readouterr()
    assert printed.out.splitlines() == [run_id]  # no end line
    assert (
        f"run {run_id} stopped before its end: its result mail may not have gone; "
        f"lion result --dir {shlex.quote(str(actor_dir.resolve()))} {run_id}"
    ) in printed.err.splitlines()


def test_a_flow_interrupted_twice_while_its_mail_is_sent_stops_at_once_and_says_how_to_read_it(
    actor_dir, console_khive, monkeypatch, capsys
):
    (actor_dir / "flows.toml").write_text(TWO)
    backends(monkeypatch, **{"fake/writer": lambda m: out(said(m).split()[0])})
    cut = interrupted_at(monkeypatch, "comm.send(", times=2)
    assert lion("flow", "--dir", str(actor_dir), "--name", "two", "--var", "topic=tea") == 130
    (flow_id,) = console_khive.steps
    assert cut and console_khive.runs[flow_id]["outcome"] == "success"
    assert console_khive.sent == []
    printed = capsys.readouterr()
    assert f"flow two {flow_id} success" not in printed.out
    assert (
        f"flow two {flow_id} stopped before its end: its result mail may not have gone; "
        f"lion result --dir {shlex.quote(str(actor_dir.resolve()))} {flow_id}"
    ) in printed.err.splitlines()


def test_a_second_interrupt_raised_where_it_lands_ends_the_run_with_no_mail(
    actor_dir, console_khive, monkeypatch, capsys
):
    # a second SIGINT raises KeyboardInterrupt in the frame it lands in, here as the run's rows close
    backends(monkeypatch, **{"fake/writer": interrupting})
    exec_ = Khive.exec

    async def exec(self, ops: str) -> list:
        if ops.startswith("lion.run_close("):
            raise KeyboardInterrupt
        return await exec_(self, ops)

    monkeypatch.setattr(Khive, "exec", exec)
    assert lion("run", "--dir", str(actor_dir), "--profile", "writer", "--say", "go") == 130
    (run_id,) = console_khive.runs
    assert console_khive.sent == []
    assert f"run {run_id} stopped before its end: " in capsys.readouterr().err


def test_a_run_interrupted_in_its_run_then_while_its_mail_is_sent_stops_at_once(
    actor_dir, console_khive, monkeypatch, capsys
):
    async def cancelled(messages):
        raise asyncio.CancelledError

    backends(monkeypatch, **{"fake/writer": cancelled})
    cut = interrupted_at(monkeypatch, "comm.send(")  # the command's second interrupt: its first was the run's
    assert lion("run", "--dir", str(actor_dir), "--profile", "writer", "--say", "go") == 130
    (run_id,) = console_khive.runs
    assert cut and console_khive.runs[run_id]["outcome"] == "stopped"
    assert console_khive.sent == []
    assert f"run {run_id} stopped before its end: " in capsys.readouterr().err


def test_a_flow_interrupted_at_a_step_then_while_its_mail_is_sent_stops_at_once(
    actor_dir, console_khive, monkeypatch, capsys
):
    (actor_dir / "flows.toml").write_text(TWO)

    async def answer(messages):
        if said(messages).startswith("write"):
            raise asyncio.CancelledError
        return out(said(messages).split()[0])

    backends(monkeypatch, **{"fake/writer": answer})
    cut = interrupted_at(monkeypatch, "comm.send(")
    assert lion("flow", "--dir", str(actor_dir), "--name", "two", "--var", "topic=tea") == 130
    (flow_id,) = console_khive.steps
    assert cut and console_khive.runs[flow_id]["outcome"] == "stopped"
    assert console_khive.sent == []
    assert f"flow two {flow_id} stopped before its end: " in capsys.readouterr().err


def test_a_run_whose_row_was_never_written_says_so_in_its_end_and_its_mail(
    actor_dir, console_khive, monkeypatch, capsys
):
    one = console_khive.one

    def refusing(verb, p):
        if verb == "lion.run_open":
            raise KhiveError("run_open: the disk is full")
        return one(verb, p)

    monkeypatch.setattr(console_khive, "one", refusing)
    backends(monkeypatch, **{"fake/writer": lambda m: out("never")})
    assert lion("run", "--dir", str(actor_dir), "--profile", "writer", "--say", "go") == 1
    printed = capsys.readouterr()
    run_id = printed.out.split()[0]
    assert run_id not in console_khive.runs and printed.out.splitlines()[1] == f"run {run_id} stopped"
    assert "its row could not be read back: KhiveError" in printed.err
    (mail,) = console_khive.sent
    assert mail["content"].startswith("error: KhiveError: run_open: the disk is full\n")
    body = mail["content"].split("\n\n")
    assert body[-2].startswith("its row could not be read back: KhiveError")
    assert body[-1] == f"lion result --dir {actor_dir} {run_id}"  # a store that was gone may hold it later


def test_the_result_mails_command_reads_back_a_directory_with_a_space():
    class Khive:
        actor = "a"
        sent: list[str] = []

        async def send(self, to, body, **kw):
            self.sent.append(body)

    khive = Khive()
    asyncio.run(submit.result_mail(khive, "a", "s", "out", Path("/tmp/My Project"), "r1"))
    assert shlex.split(khive.sent[0].splitlines()[-1]) == ["lion", "result", "--dir", "/tmp/My Project", "r1"]


def test_lion_result_prints_the_row_and_with_history_the_messages(
    actor_dir, console_khive, monkeypatch, capsys
):
    backends(monkeypatch, **{"fake/writer": lambda m: 'OUT{result: "read back"}'})
    assert lion("run", "--dir", str(actor_dir), "--profile", "writer", "--say", "go on") == 0
    (run_id,) = console_khive.runs
    capsys.readouterr()
    assert lion("result", "--dir", str(actor_dir), run_id) == 0
    row = json.loads(capsys.readouterr().out)
    assert row == console_khive.runs[run_id] and json.loads(row["output"]) == {"result": "read back"}
    assert lion("result", "--dir", str(actor_dir), run_id, "--history") == 0
    lines = capsys.readouterr().out.splitlines()
    shown = [m for m in lines if re.match(r"\[\d+\] ", m)]
    assert shown[0] == "[0] user input: go on" and len(shown) == len(console_khive.messages[run_id])
    assert "not in this namespace" in str(lion("result", "--dir", str(actor_dir), "nope"))


def test_lion_result_prints_a_flows_step_rows_beside_its_row(actor_dir, console_khive, monkeypatch, capsys):
    (actor_dir / "flows.toml").write_text(TWO)
    backends(monkeypatch, **{"fake/writer": lambda m: 'OUT{result: "x"}'})
    assert lion("flow", "--dir", str(actor_dir), "--name", "two", "--var", "topic=tea") == 0
    (flow_id,) = console_khive.steps
    capsys.readouterr()
    assert lion("result", "--dir", str(actor_dir), flow_id) == 0
    out = capsys.readouterr().out
    steps = [json.loads(line) for line in out.splitlines() if line.startswith('{"flow_run_id"')]
    assert [s["name"] for s in steps] == ["plan", "write"]


# D2


def test_a_profile_builds_through_the_chats_builder_with_exactly_its_tools(actor_dir):
    async def built(name: str):
        async with cast(actor_dir, profile_from(actor_dir, name), submitter="a:caller") as (
            actor,
            profile,
            _,
        ):
            return actor, profile

    _, writer = asyncio.run(built("writer"))
    defaults = {h.name for h in DEFAULT_HANDLERS}
    assert writer.specs - defaults == {"read", "list_dir"} and writer.privileges == frozenset()
    assert writer.system.startswith("You write.\n\nYou are fake/writer, doing one job on lion1 for a:caller")
    assert "Tools: read(path), list_dir(path)." in writer.system
    actor, mailer = asyncio.run(built("mailer"))
    assert mailer.specs - defaults == {"read", "list_dir", "khive_ops", "send"}
    assert mailer.privileges == frozenset({"comm.send"}) and actor.privileges >= {"comm.send"}
    assert mailer.system.startswith("You report.") and "send(text), to a:caller alone" in mailer.system
    assert mailer.defaults == {"max_turns": 3}


def test_the_send_tool_mails_the_submitter_alone_on_the_submissions_thread(
    actor_dir, console_khive, monkeypatch
):
    turns = iter(['<lact s>send(text="halfway")</lact>', 'OUT{result: "done"}'])
    backends(monkeypatch, **{"fake/mailer": lambda m: next(turns)})
    argv = ["run", "--dir", str(actor_dir), "--profile", "mailer", "--say", "go", "--notify", "a:boss"]
    assert lion(*argv, "--thread", "t7") == 0
    halfway, result = console_khive.sent
    assert (halfway["to"], halfway["content"], halfway["thread_id"]) == ("a:boss", "halfway", "t7")
    assert result["to"] == "a:boss" and result["subject"].endswith(" success")


@pytest.mark.parametrize(
    ("table", "refused"),
    [
        ('model = "m"\ncolour = "red"', "['colour']: a profile holds"),
        ('model = "m"\ntools = ["files", "web"]', "tools ['web']: the tools are"),
        ('tools = ["files"]', "names no model"),
        ('model = "m"\nbox = "vm"', "box names where the shell runs, and tools has no shell"),
        ('model = "m"\ntools = ["shell"]\nimage = "x"', "image is the Daytona box's"),
        ('model = "m"\ntools = ["shell"]\nbox = "daytona"\noffline = true', "offline takes true or false"),
        ('model = "m"\nsystem = "a"\nsystem_file = "b"', "system or system_file, not both"),
        ('model = "m"\ndefaults = { turns = 3 }', "defaults name no bound"),
    ],
)
def test_the_profile_file_refuses_what_it_does_not_name(tmp_path, table, refused):
    (tmp_path / "profiles.toml").write_text(f"[profile.p]\n{table}\n")
    with pytest.raises(SubmitError, match=re.escape(refused)):
        profile_from(tmp_path, "p")


def test_a_profile_file_or_profile_that_is_not_there_refuses_naming_what_is(tmp_path):
    with pytest.raises(SubmitError, match="no profile file"):
        profile_from(tmp_path, "p")
    (tmp_path / "profiles.toml").write_text('[profile.a]\nmodel = "m"\n[profile.b]\nmodel = "m"\n')
    with pytest.raises(SubmitError, match=r"no \[profile.c\]; there is a, b"):
        profile_from(tmp_path, "c")
    assert profile_from(tmp_path, "a", "other").model == "other"  # the command's model wins


class Box:
    """A Daytona box for `cast`: its diff is `patch`, or the error `broken` names."""

    made: list[Box] = []

    def __init__(self, image, workdir, patch="--- a/f\n+++ b/f\n", broken=None):
        self.image, self.workdir, self.prelude, self.dead = image, workdir, "", None
        self.patch, self.broken, self.stopped = patch, broken, False
        self.box_id = f"sbx-{len(Box.made) + 1}"
        Box.made.append(self)

    async def start(self):
        return self

    async def push(self, directory):
        return ["f.txt"]

    async def exec(self, argv, *, stdin=None, timeout=120, env=None):
        return {"rc": 0, "stdout": "ab" * 20 + "\n", "stderr": "", "elapsed": 0.0, "timed_out": False}

    async def diff(self, base="HEAD"):
        if self.broken:
            raise RuntimeError(self.broken)
        return self.patch

    async def stop(self):
        self.stopped = True


@pytest.fixture
def boxes(actor_dir, monkeypatch):
    """`[profile.boxed]`, its shell in a Daytona box; returns the boxes made and the error each one's
    diff raises, which is none until a test names it."""
    with (actor_dir / "profiles.toml").open("a") as f:
        f.write('\n[profile.boxed]\ntools = ["shell"]\nbox = "daytona"\nmodel = "fake/boxed"\n')
    broken: list[str | None] = [None]
    Box.made = []
    monkeypatch.setattr(
        submit, "DaytonaSandbox", lambda image, workdir: Box(image, workdir, broken=broken[0])
    )
    backends(monkeypatch, **{"fake/boxed": lambda m: out("done")})
    return Box.made, broken


def test_a_boxed_run_prints_its_id_first_and_keeps_its_patch_unapplied_beside_its_rows(
    actor_dir, console_khive, boxes, capsys
):
    made, _ = boxes
    (actor_dir / "f.txt").write_text("as it was\n")
    assert lion("run", "--dir", str(actor_dir), "--profile", "boxed", "--say", "edit f") == 0
    (run_id,) = console_khive.runs
    got = capsys.readouterr()
    assert got.out.splitlines()[:2] == [run_id, f"run {run_id} success"]  # the box's notices are not here
    assert "pushed" in got.err and "saved as" in got.err and "+++ b/f" not in got.out
    assert (actor_dir / ".lion" / "runs" / f"{run_id}.patch").read_text() == made[0].patch
    assert (actor_dir / "f.txt").read_text() == "as it was\n" and made[0].stopped


def test_a_boxed_run_whose_patch_fails_keeps_the_box_and_still_mails_its_end(
    actor_dir, console_khive, boxes, capsys
):
    made, broken = boxes
    broken[0] = "git diff: the copy has no repository"
    assert lion("run", "--dir", str(actor_dir), "--profile", "boxed", "--say", "edit f") == 1
    (run_id,) = console_khive.runs
    assert console_khive.runs[run_id]["outcome"] == "success" and not made[0].stopped
    err = " ".join(capsys.readouterr().err.split())
    assert f"box sbx-1: kept, not stopped: taking its patch failed (RuntimeError: {broken[0]})" in err
    (mail,) = console_khive.sent
    assert mail["subject"] == f"run {run_id} success"
    assert mail["content"].startswith(f"error: RuntimeError: {broken[0]}")
    # a submitter that never reads standard error (an MCP caller) learns the kept box's name here
    kept = f"notice: box sbx-1: kept, not stopped: taking its patch failed (RuntimeError: {broken[0]})"
    assert kept in mail["content"]


def command_task() -> asyncio.Task:
    (command,) = [t for t in asyncio.all_tasks() if t.get_coro().__name__ in ("run_main", "flow_main")]
    return command


async def interrupting(messages):
    """A model call the command's first interrupt lands in: its task cancelled, as by a first SIGINT."""
    command_task().cancel()
    await asyncio.Event().wait()


def interrupted_in_its(monkeypatch, step: str, times: int = 1, on: type | None = None) -> list[str]:
    """The box's `step`, its diff or its stop, cancels the command's own task `times` times, as
    interrupts landing there would, then goes on; `cut` lands in the list returned if it was cut short,
    and `mail` as the result mail goes. `on` is the sandbox class, the Daytona box's when not given."""
    on = on or Box
    real, exec_, seen = getattr(on, step), Khive.exec, []

    async def exec(self, ops: str) -> list:
        if ops.startswith("comm.send("):
            seen.append("mail")
        return await exec_(self, ops)

    async def interrupted(self, *args):
        try:
            for _ in range(times):
                command_task().cancel()
                for _ in range(3):
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            seen.append("cut")
            raise
        return await real(self, *args)

    monkeypatch.setattr(on, step, interrupted)
    monkeypatch.setattr(Khive, "exec", exec)
    return seen


def test_a_boxed_run_interrupted_while_its_patch_is_taken_takes_it_whole_and_mails_every_notice(
    actor_dir, console_khive, boxes, monkeypatch
):
    made, _ = boxes
    seen = interrupted_in_its(monkeypatch, "diff")
    assert lion("run", "--dir", str(actor_dir), "--profile", "boxed", "--say", "edit f") == 130
    (run_id,) = console_khive.runs
    patch = actor_dir / ".lion" / "runs" / f"{run_id}.patch"
    assert seen == ["mail"] and made[0].stopped and patch.read_text() == made[0].patch
    (mail,) = console_khive.sent
    assert f"line(s), saved as {patch}\n" in mail["content"]
    assert "\nnotice: box: not applied; " in mail["content"]


def test_a_boxed_run_interrupted_in_its_run_then_while_its_patch_is_taken_keeps_the_box_and_ends_there(
    actor_dir, console_khive, boxes, monkeypatch, capsys
):
    made, _ = boxes
    backends(monkeypatch, **{"fake/boxed": interrupting})
    seen = interrupted_in_its(monkeypatch, "diff")  # the command's second interrupt: the first was the run's
    assert lion("run", "--dir", str(actor_dir), "--profile", "boxed", "--say", "edit f") == 130
    (run_id,) = console_khive.runs
    assert seen == ["cut"] and not made[0].stopped and console_khive.sent == []  # no mail after it
    assert not (actor_dir / ".lion" / "runs" / f"{run_id}.patch").exists()
    err = capsys.readouterr().err
    assert "box sbx-1: kept, not stopped: interrupted while its patch was taken; " in err
    assert f"run {run_id} stopped before its end: " in err


def test_a_boxed_run_interrupted_while_its_box_stops_waits_for_the_stop_then_mails(
    actor_dir, console_khive, boxes, monkeypatch
):
    made, _ = boxes
    seen = interrupted_in_its(monkeypatch, "stop")
    assert lion("run", "--dir", str(actor_dir), "--profile", "boxed", "--say", "edit f") == 130
    assert seen == ["mail"] and made[0].stopped
    (mail,) = console_khive.sent
    assert "while it was stopped" not in mail["content"]


def test_a_boxed_run_interrupted_in_its_run_then_while_its_box_stops_cuts_the_stop_and_ends_there(
    actor_dir, console_khive, boxes, monkeypatch, capsys
):
    made, _ = boxes
    backends(monkeypatch, **{"fake/boxed": interrupting})
    seen = interrupted_in_its(monkeypatch, "stop")  # the command's second interrupt: the first was the run's
    assert lion("run", "--dir", str(actor_dir), "--profile", "boxed", "--say", "edit f") == 130
    (run_id,) = console_khive.runs
    assert seen == ["cut"] and not made[0].stopped and console_khive.sent == []  # no mail after it
    assert (actor_dir / ".lion" / "runs" / f"{run_id}.patch").read_text() == made[0].patch
    err = capsys.readouterr().err
    assert "box sbx-1: interrupted while it was stopped; it may still be running, at " in err
    assert f"run {run_id} stopped before its end: " in err


def test_a_boxed_run_interrupted_twice_while_its_box_stops_cuts_the_stop_and_ends_there(
    actor_dir, console_khive, boxes, monkeypatch, capsys
):
    made, _ = boxes
    seen = interrupted_in_its(
        monkeypatch, "stop", times=2
    )  # the first is carried by the stop, not the second
    assert lion("run", "--dir", str(actor_dir), "--profile", "boxed", "--say", "edit f") == 130
    (run_id,) = console_khive.runs
    assert seen == ["cut"] and not made[0].stopped and console_khive.sent == []
    assert console_khive.runs[run_id]["outcome"] == "success"
    err = capsys.readouterr().err
    assert "box sbx-1: interrupted while it was stopped; it may still be running, at " in err
    assert f"run {run_id} stopped before its end: " in err


def test_a_flow_interrupted_at_a_boxed_step_then_while_its_box_stops_ends_there(
    actor_dir, console_khive, boxes, monkeypatch, capsys
):
    made, _ = boxes
    flows = TWO.replace('profile = "writer"\nsay = "plan', 'profile = "boxed"\nsay = "plan')
    (actor_dir / "flows.toml").write_text(flows)
    backends(monkeypatch, **{"fake/boxed": interrupting})
    seen = interrupted_in_its(monkeypatch, "stop")
    assert lion("flow", "--dir", str(actor_dir), "--name", "two", "--var", "topic=tea") == 130
    (flow_id,) = console_khive.steps
    assert seen == ["cut"] and not made[0].stopped and console_khive.sent == []
    assert console_khive.runs[flow_id]["outcome"] == "stopped"
    err = capsys.readouterr().err
    assert "box sbx-1: interrupted while it was stopped; it may still be running, at " in err
    assert f"flow two {flow_id} stopped before its end: " in err


def second_as_the_rows_close(monkeypatch) -> list[str]:
    """The command's second interrupt as SIGINT gives it, a `KeyboardInterrupt` raised where it lands, here
    as the run's rows close; `second` lands in the list returned then, and after it `patch`, `stop` or
    `mail` for each that starts."""
    exec_, diff, stop, seen = Khive.exec, Box.diff, Box.stop, []

    async def exec(self, ops: str) -> list:
        if ops.startswith("comm.send("):
            seen.append("mail")
        if ops.startswith("lion.run_close(") and "second" not in seen:
            seen.append("second")
            raise KeyboardInterrupt
        return await exec_(self, ops)

    async def diffed(self, *args):
        seen.append("patch")
        return await diff(self, *args)

    async def stopped(self):
        seen.append("stop")
        return await stop(self)

    monkeypatch.setattr(Khive, "exec", exec)
    monkeypatch.setattr(Box, "diff", diffed)
    monkeypatch.setattr(Box, "stop", stopped)
    return seen


def test_a_boxed_run_interrupted_in_its_run_then_as_its_rows_close_takes_no_patch_and_names_the_box(
    actor_dir, console_khive, boxes, monkeypatch, capsys
):
    made, _ = boxes
    backends(monkeypatch, **{"fake/boxed": interrupting})
    seen = second_as_the_rows_close(monkeypatch)
    assert lion("run", "--dir", str(actor_dir), "--profile", "boxed", "--say", "edit f") == 130
    (run_id,) = console_khive.runs
    assert seen == ["second"] and not made[0].stopped and console_khive.sent == []  # nothing after it
    assert not (actor_dir / ".lion" / "runs" / f"{run_id}.patch").exists()
    err = capsys.readouterr().err
    assert "box sbx-1: kept, not stopped: interrupted twice before its patch was taken; " in err
    assert f"run {run_id} stopped before its end: " in err


def test_a_flow_interrupted_at_a_boxed_step_then_as_its_rows_close_takes_no_patch_and_names_the_box(
    actor_dir, console_khive, boxes, monkeypatch, capsys
):
    made, _ = boxes
    flows = TWO.replace('profile = "writer"\nsay = "plan', 'profile = "boxed"\nsay = "plan')
    (actor_dir / "flows.toml").write_text(flows)
    backends(monkeypatch, **{"fake/boxed": interrupting})
    seen = second_as_the_rows_close(monkeypatch)
    assert lion("flow", "--dir", str(actor_dir), "--name", "two", "--var", "topic=tea") == 130
    (flow_id,) = console_khive.steps
    assert seen == ["second"] and not made[0].stopped and console_khive.sent == []
    err = capsys.readouterr().err
    assert "box sbx-1: kept, not stopped: interrupted twice before its patch was taken; " in err
    assert f"flow two {flow_id} stopped before its end: " in err


class Local:
    """A local sandbox for `cast`: started and stopped, by name, and nothing else."""

    made: list[Local] = []

    def __init__(self, directory, name, offline=False):
        self.name, self.stopped = name, False
        Local.made.append(self)

    async def ensure_image(self):
        return None

    async def start(self):
        return self

    async def stop(self):
        self.stopped = True


def test_a_sandboxed_run_interrupted_in_its_run_then_as_its_rows_close_names_the_sandbox_unstopped(
    actor_dir, console_khive, monkeypatch, capsys
):
    with (actor_dir / "profiles.toml").open("a") as f:
        f.write('\n[profile.local]\ntools = ["shell"]\nmodel = "fake/local"\n')
    Local.made = []
    monkeypatch.setattr(submit, "Sandbox", Local)
    backends(monkeypatch, **{"fake/local": interrupting})
    seen = second_as_the_rows_close(monkeypatch)
    assert lion("run", "--dir", str(actor_dir), "--profile", "local", "--say", "list") == 130
    (run_id,) = console_khive.runs
    (box,) = Local.made
    assert seen == ["second"] and not box.stopped and console_khive.sent == []
    err = capsys.readouterr().err
    assert f"sandbox {box.name}: interrupted twice before it was stopped; it may be running" in err
    assert f"run {run_id} stopped before its end: " in err


@pytest.fixture
def local(actor_dir, monkeypatch):
    """`[profile.local]`, its shell in a local sandbox; returns the sandboxes made."""
    with (actor_dir / "profiles.toml").open("a") as f:
        f.write('\n[profile.local]\ntools = ["shell"]\nmodel = "fake/local"\n')
    Local.made = []
    monkeypatch.setattr(submit, "Sandbox", Local)
    backends(monkeypatch, **{"fake/local": lambda m: out("done")})
    return Local.made


def test_a_sandboxed_run_interrupted_while_its_sandbox_stops_waits_for_the_stop_then_mails(
    actor_dir, console_khive, local, monkeypatch
):
    seen = interrupted_in_its(monkeypatch, "stop", on=Local)
    assert lion("run", "--dir", str(actor_dir), "--profile", "local", "--say", "list") == 130
    (box,) = local
    assert seen == ["mail"] and box.stopped


@pytest.mark.parametrize("first", ["in its run", "as it stops"])
def test_a_sandboxed_runs_second_interrupt_as_its_sandbox_stops_cuts_the_stop_and_names_it(
    actor_dir, console_khive, local, monkeypatch, capsys, first
):
    if first == "in its run":
        backends(monkeypatch, **{"fake/local": interrupting})
    seen = interrupted_in_its(monkeypatch, "stop", times=1 if first == "in its run" else 2, on=Local)
    assert lion("run", "--dir", str(actor_dir), "--profile", "local", "--say", "list") == 130
    (run_id,) = console_khive.runs
    (box,) = local
    assert seen == ["cut"] and not box.stopped and console_khive.sent == []  # no mail after it
    err = capsys.readouterr().err
    assert f"sandbox {box.name}: interrupted while it was stopped; it may still be running" in err
    assert f"run {run_id} stopped before its end: " in err


def test_a_boxed_run_interrupted_before_its_run_then_as_its_box_stops_names_the_box(
    actor_dir, console_khive, boxes, monkeypatch, capsys
):
    # the first interrupt lands as the box is filled, before any run: no patch, and the box is stopped
    made, _ = boxes
    push = Box.push

    async def pushed(self, directory):
        command_task().cancel()
        await asyncio.sleep(0)
        return await push(self, directory)

    monkeypatch.setattr(Box, "push", pushed)
    seen = interrupted_in_its(monkeypatch, "stop")  # the second, as it stops
    assert lion("run", "--dir", str(actor_dir), "--profile", "boxed", "--say", "edit f") == 130
    assert seen == ["cut"] and not made[0].stopped and console_khive.runs == {}
    assert "box sbx-1: interrupted while it was stopped; it may still be running" in capsys.readouterr().err


class Unwritable:
    """A standard error that takes no line."""

    def write(self, text):
        raise OSError(5, "Input/output error")

    def flush(self):
        pass


@pytest.mark.parametrize("on", ["box", "sandbox"])
def test_a_second_interrupt_whose_notices_standard_error_cannot_take_still_ends_with_130_and_no_mail(
    actor_dir, console_khive, boxes, local, monkeypatch, on
):
    made, _ = boxes
    profile = "boxed" if on == "box" else "local"
    backends(monkeypatch, **{f"fake/{profile}": interrupting})
    seen = second_as_the_rows_close(monkeypatch)
    monkeypatch.setattr(sys, "stderr", Unwritable())
    assert lion("run", "--dir", str(actor_dir), "--profile", profile, "--say", "edit f") == 130
    assert seen == ["second"] and console_khive.sent == []  # the interrupt, never the failed line
    assert not (made or local)[0].stopped


def test_a_step_whose_box_fails_after_its_run_stops_the_flow_with_the_runs_outcome_and_the_error(
    actor_dir, console_khive, boxes
):
    made, broken = boxes
    broken[0] = "git diff: the copy has no repository"
    flows = TWO.replace('profile = "writer"\nsay = "plan', 'profile = "boxed"\nsay = "plan')
    (actor_dir / "flows.toml").write_text(flows)
    assert lion("flow", "--dir", str(actor_dir), "--name", "two", "--var", "topic=tea") == 1
    (flow_id,) = console_khive.steps
    assert [s["name"] for s in console_khive.steps[flow_id].values()] == ["plan"]  # write never opened
    failed = json.loads(console_khive.runs[flow_id]["output"])
    assert failed == {"failed": "plan", "outcome": "success", "error": f"RuntimeError: {broken[0]}"}
    assert console_khive.runs[flow_id]["outcome"] == "stopped" and not made[0].stopped
    (mail,) = console_khive.sent
    assert "\nnotice: box sbx-1: kept, not stopped: taking its patch failed" in mail["content"]


def failing_stop(monkeypatch, on: type, interrupts: int) -> None:
    """`on`'s stop fails with an I/O error, after the command's own task is cancelled `interrupts` times in
    it; a cut it takes ends in the error too, as a stop whose own cleanup fails can end."""

    async def stop(self):
        try:
            for _ in range(interrupts):
                command_task().cancel()
                for _ in range(3):
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        raise OSError(5, "stop EIO")

    monkeypatch.setattr(on, "stop", stop)


@pytest.mark.parametrize("on", ["box", "sandbox"])
@pytest.mark.parametrize(
    "interrupted", ["never", "in its run", "as it stops", "in its run and as it stops", "twice as it stops"]
)
def test_a_stop_that_fails_names_what_it_left_and_never_takes_the_place_of_an_interrupt(
    actor_dir, console_khive, boxes, local, monkeypatch, capsys, on, interrupted
):
    made, _ = boxes
    profile = "boxed" if on == "box" else "local"
    model = interrupting if "in its run" in interrupted else lambda m: out("done")
    backends(monkeypatch, **{f"fake/{profile}": model})  # the fixtures each set theirs alone
    stops = {"never": 0, "in its run": 0, "as it stops": 1, "in its run and as it stops": 1}
    failing_stop(monkeypatch, Box if on == "box" else Local, stops.get(interrupted, 2))
    code = lion("run", "--dir", str(actor_dir), "--profile", profile, "--say", "edit f")
    (run_id,) = console_khive.runs
    failed = "its stop failed (OSError: [Errno 5] stop EIO); it may still be running"
    named = f"box sbx-1: {failed}, at " if on == "box" else f"sandbox {local[0].name}: {failed}"
    err = capsys.readouterr().err
    assert named in err and not (made or local)[0].stopped
    if interrupted == "never":  # the error is the run's
        assert code == 1
        (mail,) = console_khive.sent
        assert mail["content"].startswith("error: OSError: [Errno 5] stop EIO")
        assert f"notice: {named}" in mail["content"]
    elif interrupted in ("in its run", "as it stops"):  # the command's first: its mail goes, then 130
        assert code == 130
        (mail,) = console_khive.sent
        assert mail["content"].startswith("error: CancelledError") and f"notice: {named}" in mail["content"]
    else:  # its second, though the stop turned it into its error: no mail after it
        assert code == 130 and console_khive.sent == []
        assert f"run {run_id} stopped before its end: " in err


def test_a_box_whose_patch_fails_after_the_runs_interrupt_is_kept_and_the_interrupt_goes_on(
    actor_dir, console_khive, boxes, monkeypatch
):
    made, broken = boxes
    backends(monkeypatch, **{"fake/boxed": interrupting})
    broken[0] = "patch EIO"
    assert lion("run", "--dir", str(actor_dir), "--profile", "boxed", "--say", "edit f") == 130
    assert not made[0].stopped
    (mail,) = console_khive.sent
    assert mail["content"].startswith("error: CancelledError")
    assert (
        "notice: box sbx-1: kept, not stopped: taking its patch failed (RuntimeError: patch EIO)"
        in mail["content"]
    )


def closing(monkeypatch, cancels: int, fails: bool = False) -> list[str]:
    """`Store.close_flow` cancels the command's own task `cancels` times as it starts, as interrupts
    landing in the flow's close would, then closes the row, or is refused when `fails`. What it did,
    `mail` as the result mail goes, in order."""
    from lion1 import Store

    real, exec_, seen = Store.close_flow, Khive.exec, []

    async def exec(self, ops: str) -> list:
        if ops.startswith("comm.send("):
            seen.append("mail")
        return await exec_(self, ops)

    async def close_flow(self, *args):
        seen.append("close")
        try:
            for _ in range(cancels):
                command_task().cancel()
                for _ in range(3):
                    await asyncio.sleep(0)
            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            seen.append("cut")
            raise
        if fails:
            raise KhiveError("lion.run_close: made-up refusal")
        await real(self, *args)
        seen.append("closed")

    monkeypatch.setattr(Store, "close_flow", close_flow)
    monkeypatch.setattr(Khive, "exec", exec)
    return seen


@pytest.mark.parametrize("first", ["in a step", "in the close"])
def test_a_second_interrupt_in_the_flows_close_cuts_it_and_says_its_row_may_stay_open(
    actor_dir, console_khive, monkeypatch, capsys, first
):
    (actor_dir / "flows.toml").write_text(TWO)
    stepping = first == "in a step"
    backends(monkeypatch, **{"fake/writer": interrupting if stepping else (lambda m: out("x"))})
    seen = closing(monkeypatch, cancels=1 if stepping else 2)
    assert lion("flow", "--dir", str(actor_dir), "--name", "two", "--var", "topic=tea") == 130
    (flow_id,) = console_khive.steps
    assert seen == ["close", "cut"] and console_khive.sent == []
    assert console_khive.runs[flow_id].get("outcome") is None  # the row stays open, as said
    err = capsys.readouterr().err
    assert f"flow {flow_id}: its close was cut, so its row may stay open" in err
    assert f"flow two {flow_id} stopped before its end: " in err


def test_the_first_interrupt_in_the_flows_close_lets_it_finish_and_goes_on_after_it(
    actor_dir, console_khive, monkeypatch, capsys
):
    (actor_dir / "flows.toml").write_text(TWO)
    backends(monkeypatch, **{"fake/writer": lambda m: out("x")})
    seen = closing(monkeypatch, cancels=1)
    assert lion("flow", "--dir", str(actor_dir), "--name", "two", "--var", "topic=tea") == 130
    (flow_id,) = console_khive.steps
    assert seen == ["close", "closed", "mail"]  # the row closes before the mail, which goes once
    assert console_khive.runs[flow_id]["outcome"] == "success"
    assert "may stay open" not in capsys.readouterr().err


@pytest.mark.parametrize("interrupt", [None, "in a step", "in the close"])
def test_a_flows_close_that_fails_is_named_and_an_interrupt_goes_on_in_its_place(
    actor_dir, console_khive, monkeypatch, capsys, interrupt
):
    (actor_dir / "flows.toml").write_text(TWO)
    stepping = interrupt == "in a step"
    backends(monkeypatch, **{"fake/writer": interrupting if stepping else (lambda m: out("x"))})
    seen = closing(monkeypatch, cancels=int(interrupt == "in the close"), fails=True)
    code = lion("flow", "--dir", str(actor_dir), "--name", "two", "--var", "topic=tea")
    (flow_id,) = console_khive.steps
    (mail,) = console_khive.sent
    assert seen == ["close", "mail"]
    err = capsys.readouterr().err
    if interrupt is None:  # the close's own error, the flow's end as before
        assert code != 130 and "KhiveError: lion.run_close: made-up refusal" in mail["content"]
        assert "its close failed" not in err
    else:  # named as a cut close is, and the interrupt goes on: the mail, then 130
        assert code == 130 and mail["content"].startswith("error: CancelledError\n")
        named = (
            f"flow {flow_id}: its close failed (KhiveError: lion.run_close: made-up refusal); "
            "its row may stay open"
        )
        assert named in err and named in mail["content"]


def opening(monkeypatch, cancels: int, fails: bool = False, torn: bool = False) -> list[str]:
    """`Store.open_flow` cancels the command's own task `cancels` times as it starts, as interrupts
    landing in the flow's open would, then opens the row, or is refused when `fails`. `torn`: the row
    opened, two interrupts come and the open's own task is cancelled first, as the loop's end does after
    a second SIGINT. What it did, in order, beside what `closing` sees of the close and the mail."""
    from lion1 import Store

    real, seen = Store.open_flow, closing(monkeypatch, cancels=0)

    async def open_flow(self, *args, **kwargs):
        seen.append("open")
        try:
            for _ in range(cancels):
                command_task().cancel()
                for _ in range(3):
                    await asyncio.sleep(0)
            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            seen.append("cut")
            raise
        if fails:
            raise KhiveError("lion.run_open: made-up refusal")
        await real(self, *args, **kwargs)
        seen.append("opened")
        if torn:
            command = command_task()
            asyncio.get_running_loop().call_soon(lambda: (command.cancel(), command.cancel()))
            asyncio.current_task().cancel()
            await asyncio.sleep(0)  # ends here cancelled, before the command reads how it ended

    monkeypatch.setattr(Store, "open_flow", open_flow)
    return seen


def flow_row(fake) -> tuple[str, dict]:
    ((flow_id, row),) = [(k, r) for k, r in fake.runs.items() if r["profile"] == "flow:two"]
    return flow_id, row


def test_the_first_interrupt_in_the_flows_open_lets_it_finish_gives_its_id_and_closes_its_row_stopped(
    actor_dir, console_khive, monkeypatch, capsys
):
    (actor_dir / "flows.toml").write_text(TWO)
    backends(monkeypatch, **{"fake/writer": lambda m: out("x")})
    seen = opening(monkeypatch, cancels=1)
    assert lion("flow", "--dir", str(actor_dir), "--name", "two", "--var", "topic=tea") == 130
    assert seen == ["open", "opened", "close", "closed", "mail"]  # no step opens; the row closes, then mails
    flow_id, row = flow_row(console_khive)
    assert row["outcome"] == "stopped" and json.loads(row["output"]) == {
        "failed": None,
        "error": "CancelledError",
    }
    (mail,) = console_khive.sent
    assert console_khive.steps == {} and mail["content"].startswith("error: CancelledError\n")
    out_, err = capsys.readouterr()
    assert out_.splitlines()[0] == flow_id and "may stay open" not in err  # the id, for `lion result`


def test_a_second_interrupt_in_the_flows_open_cuts_it_and_says_its_row_may_stay_open(
    actor_dir, console_khive, monkeypatch, capsys
):
    (actor_dir / "flows.toml").write_text(TWO)
    backends(monkeypatch, **{"fake/writer": lambda m: out("x")})
    seen = opening(monkeypatch, cancels=2)
    assert lion("flow", "--dir", str(actor_dir), "--name", "two", "--var", "topic=tea") == 130
    assert seen == ["open", "cut"] and console_khive.sent == []  # no close after it
    (said,) = [line for line in capsys.readouterr().err.splitlines() if "its open was cut" in line]
    assert re.fullmatch(r"flow [0-9a-f]{8}: its open was cut, so its row may stay open", said)


def test_a_second_interrupt_in_the_flows_open_whose_task_the_loops_end_cancelled_says_its_row_may_stay_open(
    actor_dir, console_khive, monkeypatch, capsys
):
    # the store wrote the row, then the open's own task was cancelled before the command read its end:
    # whether the row is there is not known to the command, so it says the row may stay open
    (actor_dir / "flows.toml").write_text(TWO)
    backends(monkeypatch, **{"fake/writer": lambda m: out("x")})
    seen = opening(monkeypatch, cancels=0, torn=True)
    assert lion("flow", "--dir", str(actor_dir), "--name", "two", "--var", "topic=tea") == 130
    flow_id, row = flow_row(console_khive)
    assert seen == ["open", "opened"] and row["outcome"] is None and console_khive.sent == []
    out_, err = capsys.readouterr()
    assert f"flow {flow_id}: its open was cut, so its row may stay open" in err and flow_id not in out_


@pytest.mark.parametrize("interrupt", [None, "in the open"])
def test_a_flows_open_that_is_refused_closes_nothing_and_an_interrupt_goes_on_in_its_place(
    actor_dir, console_khive, monkeypatch, capsys, interrupt
):
    (actor_dir / "flows.toml").write_text(TWO)
    backends(monkeypatch, **{"fake/writer": lambda m: out("x")})
    seen = opening(monkeypatch, cancels=int(interrupt is not None), fails=True)
    if interrupt is None:  # the refusal goes on as itself
        with pytest.raises(KhiveError, match="made-up refusal"):
            lion("flow", "--dir", str(actor_dir), "--name", "two", "--var", "topic=tea")
    else:
        assert lion("flow", "--dir", str(actor_dir), "--name", "two", "--var", "topic=tea") == 130
        err = capsys.readouterr().err
        assert re.search(
            r"flow [0-9a-f]{8}: its open failed \(KhiveError: lion.run_open: made-up refusal\); "
            r"its row may stay open",
            err,
        )
    assert seen == ["open"] and console_khive.sent == []  # no close: the store opened no row


@pytest.mark.parametrize("interrupts", [1, 2])
def test_interrupts_as_the_flows_id_is_printed_still_close_its_row_or_say_it_may_stay_open(
    actor_dir, console_khive, monkeypatch, capsys, interrupts
):
    (actor_dir / "flows.toml").write_text(TWO)
    backends(monkeypatch, **{"fake/writer": lambda m: out("x")})
    seen, emit = closing(monkeypatch, cancels=0), submit._emit

    def printing(line, stream=None):
        emit(line, stream)
        if stream is None and re.fullmatch(r"[0-9a-f]{8}", line):  # the flow's id: interrupts land here
            command_task().cancel()  # the first, as a first SIGINT does, waits for the next await
            if interrupts == 2:
                raise KeyboardInterrupt  # the second, as a second SIGINT does, is raised where it lands

    monkeypatch.setattr(submit, "_emit", printing)
    assert lion("flow", "--dir", str(actor_dir), "--name", "two", "--var", "topic=tea") == 130
    flow_id, row = flow_row(console_khive)
    err = capsys.readouterr().err
    if interrupts == 1:  # the flow closes stopped, then its one mail goes
        assert seen == ["close", "closed", "mail"] and row["outcome"] == "stopped"
        assert "may stay open" not in err
    else:  # the second ends it at once: no step, the close cut and said, and no mail
        assert console_khive.steps == {} and console_khive.sent == []
        assert seen == ["close", "cut"] and row["outcome"] is None
        assert f"flow {flow_id}: its close was cut, so its row may stay open" in err
        assert f"flow two {flow_id} stopped before its end: " in err


@pytest.mark.parametrize("launched", [False, True])
def test_a_second_interrupt_as_the_sandbox_starts_names_it_running_only_once_its_container_was_sent(
    actor_dir, console_khive, local, monkeypatch, capsys, launched
):
    async def start(self):
        self.launched = launched
        command_task().cancel()  # two interrupts land here before it
        command_task().cancel()  # goes on: the second ends the command
        await asyncio.Event().wait()

    monkeypatch.setattr(Local, "start", start)
    assert lion("run", "--dir", str(actor_dir), "--profile", "local", "--say", "list") == 130
    (sandbox,) = local
    err = capsys.readouterr().err
    running = f"sandbox {sandbox.name}: interrupted twice before it was stopped; it may be running"
    none = f"sandbox {sandbox.name}: interrupted twice before its container was started; none runs"
    assert (running in err, none in err) == (launched, not launched)
    assert not sandbox.stopped and console_khive.sent == []
