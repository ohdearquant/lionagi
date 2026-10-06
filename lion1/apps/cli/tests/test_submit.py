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
from lion1.khive import KhiveError

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
