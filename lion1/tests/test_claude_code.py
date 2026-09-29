"""The resumed-session Claude Code backend: what each round sends, and when it starts a new session."""

import asyncio
import json
import subprocess
import sys
from dataclasses import replace

import pytest

from hub.harness import claude_code
from hub.harness.claude_code import ClaudeCodeSession
from lionagi import Actor, Kind, Message, Profile, RunRequest


def _fake_exec(calls: list[dict]):
    async def fake(cli, args, prompt, *, model, timeout, cwd=None, persist=False):
        n = len(calls)
        calls.append({"args": args, "prompt": prompt, "persist": persist})
        # the CLI's own shape on a resumed session: usage is per call, cost and api time accumulate
        usage = {"input_tokens": 500 + 50 * n, "output_tokens": (4, 3, 3, 5)[n]}
        return "ok", {"usage": usage, "total_cost_usd": 0.001 * (n + 1), "duration_api_ms": 1000 * (n + 1)}

    return fake


def _after(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


SYSTEM = Message("system", "the profile")
IN0 = Message("user", "[in0] go", name="in0")
SYS1 = Message("system", "<system round=1/4>", name="sys1")
M2 = Message("assistant", '<lact f1>read_file(path="a")</lact>', name="m2")
R3 = Message("user", "[r3 f1 read_file] the whole file", name="r3", spec="read_file")
SYS4 = Message("system", "<system round=2/4>", name="sys4")
M5 = Message("assistant", '<lact h1>context.hide(refs=["r3"])</lact>', name="m5")
R6 = Message("user", "[r6 h1 context.hide] hidden: r3", name="r6", spec="context.hide")
SYS7 = Message("system", "<system round=3/4>", name="sys7")


def _session(monkeypatch) -> tuple[ClaudeCodeSession, list[dict]]:
    calls: list[dict] = []
    monkeypatch.setattr(claude_code, "_exec", _fake_exec(calls))
    return ClaudeCodeSession(cli="/bin/claude"), calls


def test_round_one_opens_the_session_and_later_rounds_resume_it_with_only_the_new_entries(monkeypatch):
    s, calls = _session(monkeypatch)
    asyncio.run(s([SYSTEM, IN0, SYS1]))
    asyncio.run(s([SYSTEM, IN0, M2, R3, SYS4]))
    first, second = calls
    assert _after(first["args"], "--session-id") == s.session_id
    assert _after(first["args"], "--system-prompt") == "the profile"
    assert first["prompt"] == "[in0] go\n\n<system round=1/4>"
    assert "--resume" in second["args"] and "--system-prompt" not in second["args"]
    assert second["prompt"] == "[r3 f1 read_file] the whole file\n\n<system round=2/4>"
    assert first["persist"] and second["persist"]
    assert s.restarts == 0


def test_a_context_directive_restarts_the_session_from_the_folded_view(monkeypatch):
    s, calls = _session(monkeypatch)
    asyncio.run(s([SYSTEM, IN0, SYS1]))
    asyncio.run(s([SYSTEM, IN0, M2, R3, SYS4]))
    old_id = s.session_id
    # round 3: the hide landed, so r3 is out of the folded view and r6 carries the directive
    asyncio.run(s([SYSTEM, IN0, M2, M5, R6, SYS7]))
    third = calls[2]
    assert s.restarts == 1 and s.session_id != old_id
    assert "--session-id" in third["args"] and "--resume" not in third["args"]
    assert _after(third["args"], "--system-prompt") == "the profile"
    assert "the whole file" not in third["prompt"]  # the hidden result is gone
    assert "<ASSISTANT>\n<lact f1>" in third["prompt"]  # the model's own turns ride as a transcript
    assert third["prompt"].endswith("<ASSISTANT>")
    assert set(s.sent) == {"in0", "m2", "m5", "r6", "sys7"}
    # round 4 resumes the new session with only what is new
    m8 = Message("assistant", "x", name="m8")
    sys9 = Message("system", "<system round=4/4>", name="sys9")
    asyncio.run(s([SYSTEM, IN0, M2, M5, R6, m8, sys9]))
    fourth = calls[3]
    assert _after(fourth["args"], "--resume") == s.session_id
    assert fourth["prompt"] == "<system round=4/4>"


def test_a_view_that_shrank_without_a_directive_does_not_restart(monkeypatch):
    s, calls = _session(monkeypatch)
    asyncio.run(s([SYSTEM, IN0, SYS1]))
    asyncio.run(s([SYSTEM, IN0, M2, R3, SYS4]))
    # the budget folded r3 out; the model asked nothing, so the CLI's history stands
    asyncio.run(s([SYSTEM, IN0, M2, Message("assistant", "y", name="m5"), SYS7]))
    third = calls[2]
    assert s.restarts == 0 and "--resume" in third["args"]
    assert third["prompt"] == "<system round=3/4>"


def test_a_resumed_call_records_its_own_usage_and_the_cost_it_added(monkeypatch):
    s, _ = _session(monkeypatch)
    asyncio.run(s([SYSTEM, IN0, SYS1]))
    asyncio.run(s([SYSTEM, IN0, M2, R3, SYS4]))
    first, second = s.calls
    assert first["usage"]["output_tokens"] == 4 and second["usage"]["output_tokens"] == 3
    assert first["total_cost_usd"] == pytest.approx(0.001)
    assert second["total_cost_usd"] == pytest.approx(0.001)
    assert second["duration_api_ms"] == 1000
    assert second["cumulative"] == {"total_cost_usd": pytest.approx(0.002), "duration_api_ms": 2000}


def test_another_run_on_the_same_backend_gets_its_own_session(monkeypatch):
    s, calls = _session(monkeypatch)
    asyncio.run(s([SYSTEM, IN0, SYS1]))
    other = Message("system", "another profile")
    asyncio.run(s([other, Message("user", "[in0] a different question", name="in0"), SYS1]))
    first, second = calls
    assert "--session-id" in second["args"] and "--resume" not in second["args"]
    assert _after(second["args"], "--session-id") != _after(first["args"], "--session-id")
    assert _after(second["args"], "--system-prompt") == "another profile"
    assert second["prompt"] == "[in0] a different question\n\n<system round=1/4>"
    assert s.restarts == 0  # another record, not a changed view
    asyncio.run(s([other, Message("user", "[in0] a third question", name="in0"), SYS1]))
    assert "--session-id" in calls[2]["args"] and calls[2]["prompt"].startswith("[in0] a third question")


def test_two_runs_with_the_same_first_input_get_their_own_sessions(monkeypatch):
    """The actor names the run on every message; the session follows the run, not the content."""
    s, calls = _session(monkeypatch)

    def run_a(m: Message) -> Message:
        return replace(m, run="run-a")

    def run_b(m: Message) -> Message:
        return replace(m, run="run-b")

    in1a = Message("user", "[in1] FIRST_DETAILS", name="in1")
    in1b = Message("user", "[in1] SECOND_DETAILS", name="in1")
    sys2 = Message("system", "<system round=1/4>", name="sys2")
    asyncio.run(s([run_a(m) for m in (SYSTEM, IN0, in1a, sys2)]))
    asyncio.run(s([run_b(m) for m in (SYSTEM, IN0, in1b, sys2)]))
    m3 = Message("assistant", '<lact f1>read_file(path="a")</lact>', name="m3")
    r4 = Message("user", "[r4 f1 read_file] the file", name="r4", spec="read_file")
    sys5 = Message("system", "<system round=2/4>", name="sys5")
    asyncio.run(s([run_a(m) for m in (SYSTEM, IN0, in1a, sys2, m3, r4, sys5)]))
    first, second, third = calls
    assert _after(first["args"], "--session-id") != _after(second["args"], "--session-id")
    assert "SECOND_DETAILS" in second["prompt"] and "FIRST_DETAILS" not in second["prompt"]
    assert _after(third["args"], "--resume") == _after(first["args"], "--session-id")
    assert "r4" in third["prompt"] and "FIRST_DETAILS" not in third["prompt"]
    assert s.restarts == 0


def test_cancelling_a_call_stops_the_child(tmp_path, monkeypatch):
    cli = tmp_path / "claude"
    cli.write_text("#!/bin/sh\nsleep 30\n")
    cli.chmod(0o755)
    procs = []
    real = asyncio.create_subprocess_exec

    async def capturing(*args, **kwargs):
        proc = await real(*args, **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capturing)

    async def main():
        task = asyncio.create_task(claude_code._exec(str(cli), [], "hi", model=None, timeout=30))
        while not procs:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return procs[0].returncode

    assert asyncio.run(main()) is not None  # reaped: the child did not outlive the cancelled task


REFUSAL = (  # the CLI's result envelope on stdout, exit 1
    '{"type":"result","subtype":"success","is_error":false,"stop_reason":"refusal","result":"",'
    '"total_cost_usd":0.0213,"usage":{"input_tokens":4000,"output_tokens":12}}'
)
MAX_TURNS = (  # exit 0, the envelope saying the run failed
    '{"type":"result","subtype":"error_max_turns","is_error":true,"result":"stopped at the turn cap",'
    '"total_cost_usd":0.5,"usage":{"input_tokens":9000,"output_tokens":700}}'
)


def _cli(tmp_path, name: str, stdout: str, rc: int, stderr: str = "") -> str:
    out, err = tmp_path / f"{name}.out", tmp_path / f"{name}.err"
    out.write_text(stdout)
    err.write_text(stderr)
    cli = tmp_path / name
    cli.write_text(f"#!/bin/sh\ncat > /dev/null\ncat {out}\ncat {err} >&2\nexit {rc}\n")
    cli.chmod(0o755)
    return str(cli)


def test_failed_envelope_is_preserved(tmp_path):
    # the Codex half is tests/test_codex.py's
    refused = _cli(tmp_path, "refused", REFUSAL, 1)
    for backend, call in (
        (claude_code.ClaudeCode(cli=refused), [Message("user", "go")]),
        (claude_code.ClaudeCodeAgentic(cli=refused), "go"),
    ):
        with pytest.raises(claude_code.ClaudeCodeError, match="refusal"):
            asyncio.run(backend(call))
        assert len(backend.calls) == 1
        envelope = backend.calls[0]
        assert envelope["total_cost_usd"] == 0.0213 and envelope["usage"]["output_tokens"] == 12
        assert "refusal" in envelope["error"] and "result" not in envelope
    capped = claude_code.ClaudeCode(cli=_cli(tmp_path, "capped", MAX_TURNS, 0))
    with pytest.raises(claude_code.ClaudeCodeError, match="error_max_turns"):
        asyncio.run(capped([Message("user", "go")]))
    assert capped.calls[0]["total_cost_usd"] == 0.5 and "error_max_turns" in capped.calls[0]["error"]
    # the detail is whole: the reason sits at the end of a long stderr, and nothing says what it cost
    crashed = claude_code.ClaudeCode(cli=_cli(tmp_path, "crashed", "", 2, "x" * 2000 + " TAIL-REASON"))
    with pytest.raises(claude_code.ClaudeCodeError, match="TAIL-REASON"):
        asyncio.run(crashed([Message("user", "go")]))
    assert crashed.calls[0]["total_cost_usd"] is None and "TAIL-REASON" in crashed.calls[0]["error"]


def test_both_chat_backends_hand_back_this_calls_prompt_count(monkeypatch):
    # fresh input, cache read and cache write together are the prompt this call sent, the CLI's own
    # context included; the actor anchors its context figure on it (ADR-0009/C2)
    async def fake(cli, args, prompt, *, model, timeout, cwd=None, persist=False):
        usage = {
            "input_tokens": 100,
            "cache_read_input_tokens": 2000,
            "cache_creation_input_tokens": 50,
            "output_tokens": 4,
        }
        return "ok", {"usage": usage, "total_cost_usd": 0.001}

    monkeypatch.setattr(claude_code, "_exec", fake)
    for backend in (claude_code.ClaudeCode(cli="/bin/claude"), ClaudeCodeSession(cli="/bin/claude")):
        text = asyncio.run(backend([SYSTEM, IN0, SYS1]))
        assert text == "ok" and getattr(text, "prompt_tokens", None) == 2150, type(backend).__name__


def test_the_prompt_count_is_the_last_turns_when_the_cli_ran_several(monkeypatch):
    # 5 tool turns read 10750 in all; the last turn's own usage is the context the next stands in, and
    # without it the average over the turns
    envelopes = iter(
        [
            {
                "input_tokens": 500,
                "cache_read_input_tokens": 10000,
                "cache_creation_input_tokens": 250,
                "iterations": [
                    {
                        "type": "message",
                        "input_tokens": 8,
                        "cache_read_input_tokens": 3000,
                        "cache_creation_input_tokens": 92,
                    }
                ],
            },
            {"input_tokens": 500, "cache_read_input_tokens": 10000, "cache_creation_input_tokens": 250},
        ]
    )

    async def fake(cli, args, prompt, *, model, timeout, cwd=None, persist=False):
        return "ok", {"usage": next(envelopes), "num_turns": 5, "total_cost_usd": 0.01}

    monkeypatch.setattr(claude_code, "_exec", fake)
    s = ClaudeCodeSession(cli="/bin/claude")
    assert asyncio.run(s([SYSTEM, IN0, SYS1])).prompt_tokens == 3100
    assert asyncio.run(s([SYSTEM, IN0, M2, R3, SYS4])).prompt_tokens == 2150


def test_the_runtimes_fold_restarts_the_session_from_the_folded_view(monkeypatch):
    calls: list[dict] = []

    async def fake(cli, args, prompt, *, model, timeout, cwd=None, persist=False):
        calls.append({"args": args, "prompt": prompt})
        return "ok", {"usage": {"input_tokens": 1, "output_tokens": 1}, "total_cost_usd": 0.0}

    monkeypatch.setattr(claude_code, "_exec", fake)
    s = ClaudeCodeSession(cli="/bin/claude")
    asyncio.run(s([SYSTEM, IN0, SYS1]))
    asyncio.run(s([SYSTEM, IN0, M2, R3, SYS4]))
    fold = Message(
        "system", "<system round=3/4>\nfolded: the view was ~90000 of 60000 tokens", name="sys6", spec="fold"
    )
    asyncio.run(s([SYSTEM, IN0, M2, SYS4, fold]))  # R3 folded out of the view
    assert s.restarts == 1 and "--resume" in calls[1]["args"] and "--session-id" in calls[2]["args"]
    assert "the whole file" not in calls[2]["prompt"] and "folded" in calls[2]["prompt"]


def test_the_provider_shape_hands_the_cli_its_own_tools_the_directories_and_the_one_mcp(monkeypatch):
    """The Claude Code provider: with tools the CLI runs them inside each round, in the agent's cwd, with the
    agent's repository reachable, the named MCP the only one loaded; the default stays the chat shape."""
    calls: list[dict] = []

    async def fake(cli, args, prompt, *, model, timeout, cwd=None, persist=False):
        calls.append({"args": args, "cwd": cwd})
        return "ok", {
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "total_cost_usd": 0.0,
            "duration_api_ms": 1,
        }

    monkeypatch.setattr(claude_code, "_exec", fake)
    s = ClaudeCodeSession(
        cli="/bin/claude",
        tools=("Bash", "Read"),
        allowed=("mcp__khive",),
        max_turns=30,
        cwd="/tmp/agent",
        add_dirs=("/tmp/repo",),
        mcp_config="/tmp/agent/mcp.json",
    )
    asyncio.run(s([SYSTEM, IN0, SYS1]))
    asyncio.run(s([SYSTEM, IN0, M2, R3, SYS4]))
    for call in calls:
        a = call["args"]
        assert _after(a, "--tools") == "Bash,Read" and _after(a, "--allowedTools") == "Bash,Read,mcp__khive"
        assert _after(a, "--max-turns") == "30" and _after(a, "--add-dir") == "/tmp/repo"
        assert _after(a, "--mcp-config") == "/tmp/agent/mcp.json" and call["cwd"] == "/tmp/agent"
    assert "--resume" in calls[1]["args"]
    plain = ClaudeCodeSession(cli="/bin/claude")
    asyncio.run(plain([SYSTEM, IN0, SYS1]))
    a = calls[2]["args"]
    assert _after(a, "--tools") == "" and _after(a, "--max-turns") == "1" and "--allowedTools" not in a
    assert "--add-dir" not in a and "--mcp-config" not in a and calls[2]["cwd"] is None
    assert all("--settings" not in c["args"] for c in calls)  # no actor given: no pre-tool hook
    free = ClaudeCodeSession(
        cli="/bin/claude", tools=("Bash",), max_turns=0, effort="medium"
    )  # 0: no turn cap
    asyncio.run(free([SYSTEM, IN0, SYS1]))
    assert "--max-turns" not in calls[3]["args"] and _after(calls[3]["args"], "--tools") == "Bash"
    assert _after(calls[3]["args"], "--effort") == "medium" and "--effort" not in calls[2]["args"]


def test_two_runs_whose_calls_overlap_keep_their_own_session_and_bill(monkeypatch):
    # the call switched the backend to its run, awaited the CLI, then wrote whatever run was current
    # by then: with run B's call begun in between, A's answer landed in B's session state, B's cost
    # delta subtracted A's, and A's next call opened a new session instead of resuming its own
    entered = {"A": asyncio.Event(), "B": asyncio.Event()}
    release = {"A": asyncio.Event(), "B": asyncio.Event()}
    seen: list[tuple[str, str, str]] = []
    runs: dict[str, str] = {}
    cost = {("A", 1): 0.4, ("B", 1): 0.1, ("A", 2): 0.6}  # the CLI's cost accumulates per session

    async def fake(cli, args, prompt, *, model, timeout, cwd=None, persist=False):
        flag = "--session-id" if "--session-id" in args else "--resume"
        sid = _after(args, flag)
        run = next((r for r in ("A", "B") if f"{r}-first" in prompt), None) or runs[sid]
        runs[sid] = run
        n = sum(1 for r, _, _ in seen if r == run) + 1
        seen.append((run, flag, sid))
        if n == 1:
            entered[run].set()
            await release[run].wait()
        return "ok", {"usage": {"input_tokens": 10}, "total_cost_usd": cost[(run, n)]}

    monkeypatch.setattr(claude_code, "_exec", fake)
    s = ClaudeCodeSession(cli="/bin/claude")

    def view(run: str, *later: Message) -> list[Message]:
        return [
            Message("system", "profile", run=run),
            Message("user", f"{run}-first", name="in0", run=run),
            Message("system", "<system round=1/4>", name="sys1", run=run),
            *later,
        ]

    async def main():
        a = asyncio.create_task(s(view("A")))
        await entered["A"].wait()
        b = asyncio.create_task(s(view("B")))
        await entered["B"].wait()
        release["A"].set()
        await a  # A comes back while B's call is out
        release["B"].set()
        await b
        await s(view("A", Message("system", "<system round=2/4>", name="sys2", run="A")))

    asyncio.run(asyncio.wait_for(main(), 5))
    (a1, _, sid_a), (b1, _, sid_b), (a2, flag, sid) = seen
    assert (a1, b1, a2) == ("A", "B", "A") and sid_a != sid_b
    assert (flag, sid) == ("--resume", sid_a)  # A's second call resumes A's session
    assert [round(c["total_cost_usd"], 6) for c in s.calls] == [0.4, 0.1, 0.2]  # each call's own bill


def test_a_timeout_after_the_cli_exited_still_stops_what_it_left_holding_the_pipes(tmp_path):
    # the group was killed only while the CLI ran; its exited leader's child still held stdout, the
    # kill was skipped and the drain waited on that child with no deadline
    import os
    import time

    cli = tmp_path / "claude"
    cli.write_text(f"#!/bin/sh\ncat > /dev/null\nsleep 30 &\necho $! > {tmp_path / 'child'}\nexit 0\n")
    cli.chmod(0o755)
    t0 = time.monotonic()
    with pytest.raises(claude_code.ClaudeCodeError, match="timed out|did not answer"):
        asyncio.run(asyncio.wait_for(claude_code._exec(str(cli), [], "hi", model=None, timeout=0.5), 15))
    assert time.monotonic() - t0 < 10
    child = int((tmp_path / "child").read_text())
    for _ in range(200):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        raise AssertionError(f"the CLI's child {child} outlived the timed-out call")


# the gate over an acting session (ADR-0009/C6): a scripted CLI that runs its pre-tool hook, as the real
# one does, once per tool call it was scripted to make, and writes down what each hook answered
SCRIPTED = """import json, subprocess, sys
from pathlib import Path

here = Path(sys.argv[0]).parent
sys.stdin.read()
argv = sys.argv[1:]
hook = json.loads(argv[argv.index("--settings") + 1])["hooks"]["PreToolUse"][0]["hooks"][0]
seen = []
for call in json.loads((here / "calls.json").read_text()):
    done = subprocess.run(hook["command"], shell=True, input=json.dumps(call), capture_output=True, text=True)
    seen.append({"rc": done.returncode, "out": done.stdout, "err": done.stderr})
(here / "seen.json").write_text(json.dumps({"hook": hook, "seen": seen}))
print(json.dumps({"result": "done", "usage": {"input_tokens": 10}}))
"""


def _scripted(tmp_path, *calls: dict, script: str = SCRIPTED) -> str:
    cli = tmp_path / "claude"
    cli.write_text(f"#!{sys.executable}\n{script}")
    cli.chmod(0o755)
    (tmp_path / "calls.json").write_text(json.dumps(calls))
    return str(cli)


def _tool(name: str, use_id: str, **args) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": name, "tool_use_id": use_id, "tool_input": args}


def test_an_acting_sessions_tool_calls_ask_the_gate_and_land_on_the_record(tmp_path):
    cli = _scripted(
        tmp_path,
        _tool("Bash", "toolu_a", command="git status"),  # needs `shell`, which the profile holds
        _tool("Read", "toolu_b", file_path="notes.txt"),  # a before hook rewrites it
        _tool("Bash", "toolu_c", command="rm -rf build"),  # a before hook refuses it
        _tool("Edit", "toolu_d", file_path="a.py"),  # needs `edit`, which the profile does not hold
    )
    actor = Actor("gated", privileges={"shell", "edit"})

    @actor.before
    def guard(run, la, request):
        if "rm " in str(getattr(request, "command", "")):
            return "no rm here"
        if la.name == "Read":
            return type(request).model_validate({"file_path": "notes/today.txt"})
        return None

    backend = ClaudeCodeSession(
        cli=cli, tools=("Bash", "Read", "Edit"), actor=actor, requires={"Bash": ("shell",), "Edit": ("edit",)}
    )
    profile = Profile("worker", privileges=frozenset({"shell"}))
    run = asyncio.run(actor.run(profile, RunRequest(inputs=("go",), max_rounds=1), backend))
    a, b, c, d = json.loads((tmp_path / "seen.json").read_text())["seen"]
    assert a["rc"] == 0 and json.loads(a["out"])["hookSpecificOutput"] == {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "updatedInput": {"command": "git status"},
    }
    assert b["rc"] == 0 and json.loads(b["out"])["hookSpecificOutput"]["updatedInput"] == {
        "file_path": "notes/today.txt"
    }
    assert (c["rc"], c["err"]) == (2, "refused: no rm here")
    assert (d["rc"], d["err"]) == (2, "Edit requires ['edit']")
    acts = {e.alias: e for e in run.record if e.kind is Kind.RESULT}
    assert set(acts) == {"toolu_a", "toolu_b", "toolu_c", "toolu_d"}
    assert all(e.round == 1 for e in acts.values())
    ran = acts["toolu_a"]
    assert (ran.content.spec, ran.content.ok, ran.content.value) == ("Bash", True, {"command": "git status"})
    assert ran.meta["execution"] == "started"
    assert acts["toolu_b"].content.value == {"file_path": "notes/today.txt"}  # what the CLI was told to run
    for alias, reason in (("toolu_c", "refused"), ("toolu_d", "privilege")):
        e = acts[alias]
        assert (e.content.ok, e.content.reason, e.meta["execution"]) == (False, reason, "not executed")
    assert acts["toolu_d"].content.error == "Edit requires ['edit']"
    # the next turn's view carries the acts, so the model reads them after a restart drops the session
    rendered = "\n".join(m.content for m in actor._render(run, actor.subset(profile)))
    assert f"[{ran.name} toolu_a Bash]" in rendered and "error: refused: no rm here" in rendered


def test_the_gate_admits_nothing_when_it_cannot_decide(tmp_path):
    """No live run, an id already on the record, and a hook that cannot reach the runtime: each stops the
    tool; the CLI runs a tool whose hook failed any other way than exit 2."""
    cli = _scripted(tmp_path, _tool("Bash", "toolu_a", command="ls"), _tool("Read", "toolu_b", file_path="x"))
    actor = Actor("gated")
    backend = ClaudeCodeSession(cli=cli, tools=("Bash", "Read"), actor=actor)
    asyncio.run(backend([Message("system", "p", run="gone"), Message("user", "go", name="_in0", run="gone")]))
    seen = json.loads((tmp_path / "seen.json").read_text())
    assert [s["rc"] for s in seen["seen"]] == [2, 2]
    assert "the runtime's gate failed: LookupError: gated has no live run 'gone'" == seen["seen"][0]["err"]
    # the same id twice: the second call is refused, and its refusal lands under the id suffixed
    (tmp_path / "calls.json").write_text(
        json.dumps([_tool("Bash", "toolu_a", command="ls"), _tool("Bash", "toolu_a", command="ls")])
    )
    run = asyncio.run(actor.run(Profile("worker"), RunRequest(inputs=("go",), max_rounds=1), backend))
    first, again = json.loads((tmp_path / "seen.json").read_text())["seen"]
    assert first["rc"] == 0 and again["rc"] == 2
    assert again["err"] == "refused: tool-use id 'toolu_a' is already on the record"
    acts = [e for e in run.record if e.kind is Kind.RESULT]
    assert [e.alias for e in acts] == ["toolu_a", f"toolu_a_{acts[1].seq}"]
    assert (acts[1].content.ok, acts[1].content.reason, acts[1].meta["execution"]) == (
        False,
        "refused",
        "not executed",
    )
    # the call is over and its socket gone: the same hook, asked now, stops the tool
    command = json.loads((tmp_path / "seen.json").read_text())["hook"]["command"]
    late = subprocess.run(
        command,
        shell=True,
        input=json.dumps(_tool("Bash", "t", command="ls")),
        capture_output=True,
        text=True,
    )
    assert late.returncode == 2 and "the runtime's gate could not be asked" in late.stderr


def test_a_request_the_hooks_left_that_cannot_be_sent_is_a_refusal_that_lands(tmp_path):
    cli = _scripted(
        tmp_path, _tool("Read", "toolu_a", file_path="a.txt"), _tool("Read", "toolu_b", file_path="b.txt")
    )
    actor = Actor("gated")

    @actor.before
    def tag(run, la, request):
        # a value a hook can hold and JSON cannot carry: an object, and bytes that are not UTF-8
        extra = object() if request.file_path == "a.txt" else b"\xff\xfe"
        return type(request).model_validate({"file_path": request.file_path, "metadata": extra})

    backend = ClaudeCodeSession(cli=cli, tools=("Read",), actor=actor)
    run = asyncio.run(actor.run(Profile("worker"), RunRequest(inputs=("go",), max_rounds=1), backend))
    a, b = json.loads((tmp_path / "seen.json").read_text())["seen"]
    assert (a["rc"], b["rc"]) == (2, 2)
    assert a["err"].startswith("refused: ") and "PydanticSerializationError" in a["err"]
    assert b["err"].startswith("refused: ") and "UnicodeDecodeError" in b["err"]
    acts = [
        (e.alias, e.content.ok, e.content.reason, e.meta["execution"])
        for e in run.record.of_kind(Kind.RESULT)
    ]
    assert acts == [(alias, False, "refused", "not executed") for alias in ("toolu_a", "toolu_b")]


# a CLI that starts its pre-tool hook and does not wait for the answer: it fails, or runs past the call's
# deadline, while the gate still waits on a before hook
STRANDED = """import json, subprocess, sys, time
from pathlib import Path

here = Path(sys.argv[0]).parent
sys.stdin.read()
argv = sys.argv[1:]
hook = json.loads(argv[argv.index("--settings") + 1])["hooks"]["PreToolUse"][0]["hooks"][0]
call = json.loads((here / "calls.json").read_text())[0]
asked = subprocess.Popen(
    hook["command"], shell=True, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
)
asked.stdin.write(json.dumps(call).encode())
asked.stdin.close()
if (here / "mode").read_text() == "fail":
    time.sleep(0.5)
    sys.exit(1)
time.sleep(30)
"""


def test_a_hook_that_connects_as_the_call_ends_runs_no_gate_after_the_call_returned(monkeypatch):
    """The CLI's hook connects, then the CLI exits a few event-loop turns later, before the gate's task
    for that connection has run: the gate does not run after the call returned, the hook is refused,
    and the call lands once in the call's own round, whatever the window (0 to 7 turns): started when the
    hooks ran inside the call, not executed when the call ended first, its hook accepted at the end."""
    import shlex
    import socket

    async def window(k: int):
        actor = Actor("gated")
        hooks: list[tuple[str, int]] = []
        state = {"returned": 0, "calls": 0, "client": None}

        @actor.before
        def h(run, la, request):
            hooks.append((la.alias, state["returned"]))  # how many calls had returned when the hook ran

        async def fake_exec(cli, args, prompt, **kw):
            state["calls"] += 1
            if state["calls"] == 1:
                settings = json.loads(args[args.index("--settings") + 1])
                sock = shlex.split(settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"])[-1]
                client = socket.socket(socket.AF_UNIX)
                client.connect(sock)
                call = {"tool_name": "Read", "tool_use_id": f"toolu_{k}", "tool_input": {"file_path": "n"}}
                client.sendall(json.dumps(call).encode())
                client.shutdown(socket.SHUT_WR)
                state["client"] = client
                for _ in range(k):  # the CLI exits this many turns after its hook connected
                    await asyncio.sleep(0)
            else:
                await asyncio.sleep(0.05)  # the run is still live during its next round
            return "ok", {"total_cost_usd": 0.0}

        monkeypatch.setattr(claude_code, "_exec", fake_exec)
        backend = ClaudeCodeSession(cli="/bin/echo", tools=("Read",), actor=actor)

        async def wrapped(messages):
            try:
                return await backend(messages)
            finally:
                state["returned"] += 1

        run = await actor.run(Profile("p"), RunRequest(inputs=("go",), max_rounds=2), wrapped)
        state["client"].close()
        landed = [(e.alias, e.round, e.meta.get("execution")) for e in run.record.of_kind(Kind.RESULT)]
        return hooks, landed

    for k in range(8):
        hooks, landed = asyncio.run(window(k))
        assert all(returned == 0 for _, returned in hooks), (k, hooks)  # no hook after the call returned
        # the call lands once, in the call's own round: started when its hooks ran, else not executed
        assert landed == [(f"toolu_{k}", 1, "started" if hooks else "not executed")], (k, landed)


def test_two_requests_with_one_tool_use_id_at_the_gate_together_admit_it_once(monkeypatch):
    """Both pass the repeat check before either lands (both are at the before hook); the first to land
    is admitted, the second lands as a repeat under the suffixed id and its hook is refused, whether
    the hooks let it through or the call's end cancelled it at the gate."""
    import shlex
    import socket

    async def twins(cancelled: bool):
        actor = Actor("gated")
        waiting, release = 0, asyncio.Event()

        @actor.before
        async def hold(run, la, request):
            nonlocal waiting
            waiting += 1
            await release.wait()

        clients: list[socket.socket] = []

        async def fake_exec(cli, args, prompt, **kw):
            settings = json.loads(args[args.index("--settings") + 1])
            sock = shlex.split(settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"])[-1]
            for n in range(2):
                client = socket.socket(socket.AF_UNIX)
                client.connect(sock)
                call = {"tool_name": "Read", "tool_use_id": "toolu_d", "tool_input": {"file_path": f"n{n}"}}
                client.sendall(json.dumps(call).encode())
                client.shutdown(socket.SHUT_WR)
                clients.append(client)
            while waiting < 2:
                await asyncio.sleep(0)
            if (
                cancelled
            ):  # the call ends while both are held: the first cancelled lands, the second is its repeat
                for _ in range(2):
                    await asyncio.sleep(0)
            else:
                release.set()
                for _ in range(4):
                    await asyncio.sleep(0)
            return "ok", {"total_cost_usd": 0.0}

        monkeypatch.setattr(claude_code, "_exec", fake_exec)
        backend = ClaudeCodeSession(cli="/bin/echo", tools=("Read",), actor=actor)
        run = await actor.run(Profile("p"), RunRequest(inputs=("go",), max_rounds=1), backend)
        release.set()  # after the call: the hooks it held were cancelled at its end
        await asyncio.sleep(0.05)
        answers = []
        for client in clients:
            client.settimeout(0.5)
            answers.append(client.recv(4096))
            client.close()
        landed = [
            (e.alias, e.content.ok, e.content.reason, e.meta["execution"])
            for e in run.record.of_kind(Kind.RESULT)
        ]
        return landed, answers

    landed, answers = asyncio.run(twins(False))
    assert landed[0] == ("toolu_d", True, None, "started") and answers[0] == b'{"allow": {"file_path": "n0"}}'
    assert landed[1] == (f"toolu_d_{landed[1][0][8:]}", False, "refused", "not executed") and len(landed) == 2
    assert answers[1] == b'{"refuse": "refused: tool-use id \'toolu_d\' is already on the record"}'
    landed, answers = asyncio.run(twins(True))
    assert [(a[:7], ok, reason, ex) for a, ok, reason, ex in landed] == [
        ("toolu_d", False, "cancelled", "not executed"),
        ("toolu_d", False, "cancelled", "not executed"),
    ]
    assert landed[0][0] == "toolu_d" and landed[1][0].startswith("toolu_d_") and answers == [b"", b""]


def test_a_call_that_ends_while_the_gate_waits_lands_the_tool_call_not_executed_before_it_returns(tmp_path):
    """The CLI fails, runs past the call's deadline, or the call is cancelled, while a before hook waits on an
    approval: the call's deadline ends it, the tool call lands not executed before it returns, nothing of the
    gate is left running, and the approval that comes later lands nothing. A hook that swallows the
    cancellation and admits anyway admits nothing: the call is over."""
    import time

    cli = _scripted(tmp_path, _tool("Read", "toolu_x", file_path="notes.txt"), script=STRANDED)

    async def arm(mode: str) -> None:
        (tmp_path / "mode").write_text(mode)
        errors: list[str] = []
        asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: errors.append(ctx["message"]))
        actor = Actor("gated")
        entered, approved = asyncio.Event(), asyncio.Event()
        runs = []

        @actor.before
        async def approval(run, la, request):
            runs.append(run)
            entered.set()
            try:
                await approved.wait()
            except asyncio.CancelledError:
                if mode != "insist":
                    raise

        backend = ClaudeCodeSession(cli=cli, tools=("Read",), actor=actor, timeout=1.0)
        t0 = time.monotonic()
        request = RunRequest(inputs=("go",), max_rounds=1)
        task = asyncio.create_task(actor.run(Profile("worker"), request, backend))
        await asyncio.wait_for(entered.wait(), 5)
        if mode == "cancel":
            task.cancel()
        done, _ = await asyncio.wait({task}, timeout=2.5)
        assert done and time.monotonic() - t0 < 2.0, mode
        if mode == "cancel":
            assert task.cancelled()
        else:
            error = task.exception()
            match = "exited 1" if mode == "fail" else "did not answer"
            assert isinstance(error, claude_code.ClaudeCodeError) and match in str(error), mode
        assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == [], mode
        (run,) = runs
        acts = [
            (e.alias, e.content.ok, e.content.reason, e.meta["execution"])
            for e in run.record.of_kind(Kind.RESULT)
        ]
        assert acts == [("toolu_x", False, "cancelled", "not executed")], mode
        landed = len(run.record)
        approved.set()
        await asyncio.sleep(0.3)
        assert len(run.record) == landed and errors == [], mode

    for mode in ("fail", "deadline", "cancel", "insist"):
        asyncio.run(arm(mode))
