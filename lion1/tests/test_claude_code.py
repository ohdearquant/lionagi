"""The resumed-session Claude Code backend: what each round sends, and when it starts a new session."""

import asyncio
from dataclasses import replace

import pytest

from hub.harness import claude_code
from hub.harness.claude_code import ClaudeCodeSession
from lionagi import Message


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
    # context included; the actor anchors its context figure on it (ADR-0008 A2)
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
    free = ClaudeCodeSession(
        cli="/bin/claude", tools=("Bash",), max_turns=0, effort="medium"
    )  # 0: no turn cap
    asyncio.run(free([SYSTEM, IN0, SYS1]))
    assert "--max-turns" not in calls[3]["args"] and _after(calls[3]["args"], "--tools") == "Bash"
    assert _after(calls[3]["args"], "--effort") == "medium" and "--effort" not in calls[2]["args"]
