"""The resumed-session Claude Code backend: what each round sends, and when it starts a new session."""

import asyncio
import json
import subprocess
import sys
from dataclasses import replace

import pytest

from hub.backend import claude_code
from hub.backend.claude_code import ClaudeCodeSession
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
    assert "--resume" not in first["args"] and _after(first["args"], "--system-prompt") == "the profile"
    assert first["prompt"] == "[in0] go\n\n<system round=1/4>"
    # each call forks the point it resumes, which stays on disk as it was: the session goes on in the fork
    assert _after(second["args"], "--resume") == _after(first["args"], "--session-id")
    assert "--fork-session" in second["args"] and "--system-prompt" not in second["args"]
    assert _after(second["args"], "--session-id") == s.session_id != _after(first["args"], "--session-id")
    assert second["prompt"] == "[r3 f1 read_file] the whole file\n\n<system round=2/4>"
    assert first["persist"] and second["persist"]
    assert s.restarts == 0


def test_a_failed_context_directive_does_not_restart_the_session(monkeypatch, tmp_path):
    """A hide of a ref that does not exist fails and leaves the view as it was: its result renders
    with no spec, so the session goes on; the control is the same hide of a real ref, which does."""
    from lionagi import Kind, RunRequest
    from lionagi.actor import _messages
    from lionagi.record import fold
    from tests.test_actor import scripted
    from tests.test_chat import reader

    async def run(turns: list[str]):
        actor, profile = reader(tmp_path)
        return await actor.run(profile, RunRequest(inputs=("go",), max_rounds=3), scripted(*turns))

    listing = '<lact f1>list_dir(path=".")</lact>'
    failed = asyncio.run(run([listing, '<lact h1>context.hide(refs=["nope"])</lact>', "done"]))
    landed = asyncio.run(run([listing, '<lact h1>context.hide(refs=["_r3"])</lact>', "done"]))

    def hide(r):  # the hide's result entry and the spec its message carries
        e = next(e for e in r.record if e.kind is Kind.RESULT and e.alias == "h1")
        return e, next(m.spec for m in _messages(fold(r.record)) if m.name == e.name)

    assert not hide(failed)[0].content.ok and hide(failed)[1] is None
    assert hide(landed)[0].content.ok and hide(landed)[1] == "context.hide"
    s, calls = _session(monkeypatch)
    asyncio.run(s([SYSTEM, IN0, SYS1]))
    asyncio.run(s([SYSTEM, IN0, M2, R3, SYS4]))
    refused = Message("user", "[r6 h1 context.hide] error: no such ref: nope", name="r6", spec=None)
    asyncio.run(s([SYSTEM, IN0, M2, R3, M5, refused, SYS7]))
    assert s.restarts == 0 and "--resume" in calls[2]["args"]


def test_a_context_directive_that_frees_more_than_it_rewrites_restarts_the_session(monkeypatch):
    s, calls = _session(monkeypatch)
    big = replace(R3, content="[r3 f1 read_file] the whole file " + "x" * 5000)
    asyncio.run(s([SYSTEM, IN0, SYS1]))
    asyncio.run(s([SYSTEM, IN0, M2, big, SYS4]))
    old_id = s.session_id
    # round 3: the hide landed, so r3 is out of the folded view and r6 carries the directive; no point
    # the session passed is intact (the first holds r3), so it starts over from the whole view
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
    m8 = Message("assistant", "ok", name="m8")
    sys9 = Message("system", "<system round=4/4>", name="sys9")
    asyncio.run(s([SYSTEM, IN0, M2, M5, R6, SYS7, m8, sys9]))
    fourth = calls[3]
    assert _after(fourth["args"], "--resume") == _after(third["args"], "--session-id")
    assert fourth["prompt"] == "<system round=4/4>" and s.restarts == 1


def test_a_view_that_shrank_by_less_than_a_restart_repays_keeps_the_session(monkeypatch):
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


@pytest.mark.parametrize("kept", [True, False])
def test_an_acting_sessions_failed_call_is_billed_once_whether_or_not_the_session_kept_it(monkeypatch, kept):
    """An acting session goes on in place, so a failure it kept is in the running totals the next
    call prints: that call's share is read against the failure's totals, not the point's before it.
    A session that did not keep the failure prints a lower total, and that share is unknown."""
    after = (0.7, 700) if kept else (0.2, 200)
    answers = iter(
        [
            ("ok", {"usage": {"input_tokens": 500}, "total_cost_usd": 0.1, "duration_api_ms": 100}),
            claude_code.ClaudeCodeError(
                "claude reported error_max_turns: cap",
                {"usage": {"output_tokens": 5}, "total_cost_usd": 0.6, "duration_api_ms": 600},
            ),
            ("ok", {"usage": {"input_tokens": 700}, "total_cost_usd": after[0], "duration_api_ms": after[1]}),
        ]
    )
    argvs: list[list[str]] = []

    async def fake(cli, args, prompt, *, model, timeout, cwd=None, persist=False):
        argvs.append(args)
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(claude_code, "_exec", fake)
    s = ClaudeCodeSession(cli="/bin/claude", tools=("Read",))
    asyncio.run(s([SYSTEM, IN0, SYS1]))
    with pytest.raises(claude_code.ClaudeCodeError, match="error_max_turns"):
        asyncio.run(s([SYSTEM, IN0, M2, R3, SYS4]))
    asyncio.run(s([SYSTEM, IN0, M2, R3, SYS4]))
    started, failed, after_it = argvs
    assert _after(failed, "--resume") == _after(after_it, "--resume") == _after(started, "--session-id")
    assert "--fork-session" not in failed + after_it  # in place, both times
    shares = [(c["total_cost_usd"], c["duration_api_ms"]) for c in s.calls]
    last = (pytest.approx(0.1), 100) if kept else (None, None)
    assert shares == [(pytest.approx(0.1), 100), (pytest.approx(0.5), 500), last]


def test_a_running_total_below_the_last_leaves_the_calls_share_unknown_and_never_negative(monkeypatch):
    """A failed call's envelope, or a resumed one's, can print a running total below the one before it
    (the CLI's total is read from what it printed, and a failure is not measured to accumulate). That
    call's share is unknown, not the negative difference, so a spend row never counts it down; the
    call after it is read against the point it resumed, whose total its fork carries on."""
    totals = iter([(0.4, 400), (0.1, 100), (0.45, 460)])

    async def fake(cli, args, prompt, *, model, timeout, cwd=None, persist=False):
        cost, ms = next(totals)
        envelope = {
            "usage": {"input_tokens": 10, "output_tokens": 1},
            "total_cost_usd": cost,
            "duration_api_ms": ms,
        }
        if cost == 0.1:
            raise claude_code.ClaudeCodeError("claude reported error_max_turns: cap", envelope)
        return "ok", envelope

    monkeypatch.setattr(claude_code, "_exec", fake)
    s = ClaudeCodeSession(cli="/bin/claude")
    asyncio.run(s([SYSTEM, IN0, SYS1]))
    with pytest.raises(claude_code.ClaudeCodeError, match="error_max_turns"):
        asyncio.run(s([SYSTEM, IN0, M2, R3, SYS4]))
    asyncio.run(s([SYSTEM, IN0, M2, R3, SYS4]))
    shares = [(c["total_cost_usd"], c["duration_api_ms"]) for c in s.calls]
    assert shares == [(pytest.approx(0.4), 400), (None, None), (pytest.approx(0.05), 60)]
    assert [c["cumulative"]["total_cost_usd"] for c in s.calls] == [0.4, 0.1, 0.45]
    assert "error_max_turns" in s.calls[1]["error"] and "error" not in s.calls[2]


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


def test_an_inputless_run_after_another_on_one_backend_is_planned_as_its_own_session(monkeypatch):
    """A run with no inputs has an empty view before its first call, so no message names it; the actor
    names the run to `plan` itself. The earlier run's session and its count are never the ones
    planned: the second run starts its own session within its own, smaller budget."""
    calls: list[tuple[list[str], str]] = []

    async def fake(cli, args, prompt, *, model, timeout, cwd=None, persist=False):
        calls.append((args, prompt))
        system = _after(args, "--system-prompt") if "--system-prompt" in args else ""
        return "ok", {
            "usage": {"input_tokens": 4000 + (len(system) + len(prompt)) // 4},
            "total_cost_usd": 0.01,
        }

    monkeypatch.setattr(claude_code, "_exec", fake)
    actor, s = Actor("worker"), ClaudeCodeSession(cli="/bin/claude")
    large = Profile("large", system="x" * 80_000)
    first = asyncio.run(actor.run(large, RunRequest(max_rounds=1, context_budget=100_000), s))
    second = asyncio.run(actor.run(Profile("small"), RunRequest(max_rounds=1, context_budget=10_000), s))
    assert first.id != second.id and s._run == second.id and len(calls) == 2
    (one, _), (two, prompt) = calls
    assert "--resume" not in two and _after(two, "--session-id") != _after(one, "--session-id")
    assert "context: ~0 / 10000 tokens" in prompt  # its own view's figure, not the other run's count
    assert s.calls[0]["usage"]["input_tokens"] > 10_000 > s.calls[1]["usage"]["input_tokens"]


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


def test_a_failed_session_call_lands_its_envelope_and_the_next_call_bills_only_its_own_share(monkeypatch):
    # the failure raised before the call was kept, so a spend row reading `calls` missed it whole
    printed = {"subtype": "error_max_turns", "usage": {"input_tokens": 9000, "output_tokens": 700}}
    answers = iter(
        [
            ("ok", {"usage": {"input_tokens": 500}, "total_cost_usd": 0.1}),
            claude_code.ClaudeCodeError(
                "claude reported error_max_turns: cap", {**printed, "total_cost_usd": 0.6}
            ),
            claude_code.ClaudeCodeError("claude did not answer within 300s"),
            # a fork's total is its point's plus its own: the failed fork's spend is not in it
            ("ok", {"usage": {"input_tokens": 700}, "total_cost_usd": 0.2}),
            claude_code.ClaudeCodeError("claude exited 1: cut off", {"usage": {"output_tokens": 5}}),
            ("ok", {"usage": {"input_tokens": 800}, "total_cost_usd": 0.35}),
        ]
    )
    prompts: list[str] = []
    resumed: list[str] = []

    async def fake(cli, args, prompt, *, model, timeout, cwd=None, persist=False):
        prompts.append(prompt)
        resumed.append(_after(args, "--resume") if "--resume" in args else "")
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(claude_code, "_exec", fake)
    s = ClaudeCodeSession(cli="/bin/claude")
    asyncio.run(s([SYSTEM, IN0, SYS1]))
    for error in ("error_max_turns", "did not answer"):
        with pytest.raises(claude_code.ClaudeCodeError, match=error):
            asyncio.run(s([SYSTEM, IN0, M2, R3, SYS4]))
    asyncio.run(s([SYSTEM, IN0, M2, R3, SYS4]))
    with pytest.raises(claude_code.ClaudeCodeError, match="cut off"):
        asyncio.run(s([SYSTEM, IN0, M2, R3, SYS4]))
    asyncio.run(s([SYSTEM, IN0, M2, R3, SYS4]))
    ok, capped, timed_out, after, cut_off, unknown = s.calls
    assert (
        capped["error"] == "claude reported error_max_turns: cap" and capped["usage"]["output_tokens"] == 700
    )
    assert capped["total_cost_usd"] == pytest.approx(0.5)  # what this call added to the session
    assert timed_out == {"total_cost_usd": None, "error": "claude did not answer within 300s"}
    assert after["total_cost_usd"] == pytest.approx(0.1)  # the failed call's share is not billed twice
    # what a failed call did not deliver is sent again, from the point the failed call resumed
    assert prompts[3] == prompts[1] and resumed[1] == resumed[2] == resumed[3] != ""
    # a printed envelope with no running total leaves its own share unknown; the call after it goes on
    # from the last point that answered, and is read against that point's total
    assert cut_off["total_cost_usd"] is None and cut_off["usage"]["output_tokens"] == 5
    assert resumed[5] == resumed[4] != resumed[3]
    assert (
        unknown["total_cost_usd"] == pytest.approx(0.15) and unknown["cumulative"]["total_cost_usd"] == 0.35
    )


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


def test_the_fold_starts_an_acting_session_over_from_the_whole_view_even_with_nothing_older_to_fold(
    monkeypatch,
):
    """The budget fold on a session running its own tools: the CLI's tool turns are what the figure
    measured over the budget, and every point it could go on from holds them, so it starts over."""
    calls: list[dict] = []

    async def fake(cli, args, prompt, *, model, timeout, cwd=None, persist=False):
        calls.append({"args": args, "prompt": prompt})
        return "ok", {"usage": {"input_tokens": 1, "output_tokens": 1}, "total_cost_usd": 0.0}

    monkeypatch.setattr(claude_code, "_exec", fake)
    s = ClaudeCodeSession(cli="/bin/claude", tools=("Read",))
    asyncio.run(s([SYSTEM, IN0, SYS1]))
    fold = Message("system", "<system round=2/4>\nfolded: nothing older to fold", name="sys3", spec="fold")
    asyncio.run(s([SYSTEM, IN0, SYS1, Message("assistant", "ok", name="m2"), fold]))
    assert "--resume" not in calls[1]["args"] and _after(calls[1]["args"], "--system-prompt") == "the profile"
    assert s.restarts == 1


def test_an_acting_session_starts_over_whole_on_a_hide_too_small_to_pay_by_what_was_sent(monkeypatch):
    """A session running its own tools: its CLI holds tool turns this backend does not estimate, so a
    hide that frees too little of what was sent to pay on a session without tools still starts it over
    whole."""
    fake, _ = _forking(READ, HIDE, "noted")
    monkeypatch.setattr(claude_code, "_exec", fake)
    one, two, three = _views(_chat(400))
    plain = ClaudeCodeSession(cli="/bin/claude")
    asyncio.run(plain(one))
    asyncio.run(plain(two))
    held = plain.plan(three[1:-1])
    assert not held.restart and held.frees > 0  # the control: without tools the same hide waits, told
    calls: list[list[str]] = []

    async def acting(cli, args, prompt, *, model, timeout, cwd=None, persist=False):
        calls.append(args)
        return "ok", {"usage": {"input_tokens": 1, "output_tokens": 1}, "total_cost_usd": 0.0}

    monkeypatch.setattr(claude_code, "_exec", acting)
    s = ClaudeCodeSession(cli="/bin/claude", tools=("Read",))
    asyncio.run(s(one))
    asyncio.run(s(two))
    planned = s.plan(three[1:-1])
    assert "--resume" in calls[1] and planned.restart
    assert (
        planned.frees == 0
    )  # what it drops is more than the view shows, and this backend does not estimate it
    asyncio.run(s(three))
    assert "--resume" not in calls[2] and s.restarts == 1


def test_an_acting_session_starts_over_on_a_directive_result_that_ends_the_call(monkeypatch):
    """A caller whose messages end with a context directive's result, no notice after it: that entry is
    the call's own tail, and the acting session starts over on it all the same, as its plan said."""
    calls: list[list[str]] = []

    async def acting(cli, args, prompt, *, model, timeout, cwd=None, persist=False):
        calls.append(args)
        return "ok", {"usage": {"input_tokens": 1, "output_tokens": 1}, "total_cost_usd": 0.0}

    monkeypatch.setattr(claude_code, "_exec", acting)
    s = ClaudeCodeSession(cli="/bin/claude", tools=("Read",))
    asyncio.run(s([SYSTEM, IN0, SYS1]))
    hidden = Message("user", "[_r3 h context.hide] hidden: _in0", name="_r3", spec="context.hide")
    view = [SYSTEM, IN0, SYS1, Message("assistant", "ok", name="m2"), hidden]
    assert s.plan(view[1:]).restart
    asyncio.run(s(view))
    assert "--resume" not in calls[1] and s.restarts == 1


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
    # an acting session goes on in place: its tool turns are what a restart is there to drop, so no
    # earlier point is kept to go back to
    assert _after(calls[1]["args"], "--resume") == _after(calls[0]["args"], "--session-id")
    assert "--fork-session" not in calls[1]["args"] and len(s._points) == 1
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
        resumed = _after(args, "--resume") if "--resume" in args else None
        run = next((r for r in ("A", "B") if f"{r}-first" in prompt), None) or runs[resumed]
        runs[_after(args, "--session-id")] = run
        n = sum(1 for r, _, _ in seen if r == run) + 1
        seen.append((run, "--resume" if resumed else "--session-id", resumed or _after(args, "--session-id")))
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


def test_a_request_still_being_sent_when_the_call_ends_lands_not_executed_once_it_arrives(monkeypatch):
    """The hook connected and sent half its request when the CLI exited: the call waits for the rest
    (within the read's own bound), lands the tool call not executed, and the hook is refused; a hook
    that never finishes sending asked nothing, and the call returns at the bound with nothing landed."""
    import shlex
    import socket
    import time

    async def late(finish: bool):
        actor = Actor("gated")
        hooks: list[str] = []
        state = {"calls": 0, "client": None}

        @actor.before
        def h(run, la, request):
            hooks.append(la.alias)

        async def fake_exec(cli, args, prompt, **kw):
            state["calls"] += 1
            if state["calls"] == 1:
                settings = json.loads(args[args.index("--settings") + 1])
                sock = shlex.split(settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"])[-1]
                client = socket.socket(socket.AF_UNIX)
                client.connect(sock)
                call = json.dumps(
                    {"tool_name": "Read", "tool_use_id": "toolu_slow", "tool_input": {"file_path": "n"}}
                )
                client.sendall(call[:20].encode())  # the rest is still on its way when the CLI exits
                state["client"], state["rest"] = client, call[20:].encode()
                await asyncio.sleep(0)
            else:
                await asyncio.sleep(0.05)
            return "ok", {"total_cost_usd": 0.0}

        async def sender():
            await asyncio.sleep(0.3)
            if finish:
                try:
                    state["client"].sendall(state["rest"])
                    state["client"].shutdown(socket.SHUT_WR)
                except OSError:  # the call hung up on the half-sent request: nothing lands, asserted below
                    pass

        monkeypatch.setattr(claude_code, "_exec", fake_exec)
        monkeypatch.setattr(claude_code, "_READ_S", 1.0, raising=False)
        backend = ClaudeCodeSession(cli="/bin/echo", tools=("Read",), actor=actor)
        took: list[float] = []

        async def wrapped(messages):
            t0 = time.monotonic()
            try:
                return await backend(messages)
            finally:
                took.append(time.monotonic() - t0)

        send = asyncio.create_task(sender())
        run = await actor.run(Profile("p"), RunRequest(inputs=("go",), max_rounds=2), wrapped)
        await send
        client = state["client"]
        client.settimeout(1.0)
        try:
            answer = client.recv(4096)
        except OSError as e:
            answer = type(e).__name__
        client.close()
        landed = [
            (e.alias, e.round, e.content.reason, e.meta.get("execution"))
            for e in run.record.of_kind(Kind.RESULT)
        ]
        return hooks, landed, answer, took[0]

    hooks, landed, answer, took = asyncio.run(late(True))
    assert hooks == [] and landed == [("toolu_slow", 1, "cancelled", "not executed")], landed
    assert json.loads(answer) == {"refuse": "cancelled before it started"}, answer
    assert 0.3 <= took < 1.0, took  # the call waited for the request, not for the bound
    hooks, landed, answer, took = asyncio.run(late(False))
    assert hooks == [] and landed == [] and answer == b"", (landed, answer)  # nothing arrived: nothing lands
    assert 1.0 <= took < 2.0, took  # the read's bound ended the wait


def test_a_cancel_as_a_hook_is_accepted_at_the_calls_end_still_returns(monkeypatch):
    """The hook connects as the CLI exits, so its task is created by the call's own settle, and the run
    is cancelled before that task takes its first step: the task never ran, the call still returns
    cancelled with no task left (one that never started cannot take itself off the call's books)."""
    import shlex
    import socket
    import time

    async def go():
        actor = Actor("gated")
        state = {"calls": 0, "client": None}

        async def fake_exec(cli, args, prompt, **kw):
            state["calls"] += 1
            settings = json.loads(args[args.index("--settings") + 1])
            sock = shlex.split(settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"])[-1]
            client = socket.socket(socket.AF_UNIX)
            client.connect(sock)
            client.sendall(b"{")  # the rest never comes
            state["client"] = client
            asyncio.get_running_loop().call_soon(task.cancel)  # lands as the call's settle begins
            return "ok", {"total_cost_usd": 0.0}

        monkeypatch.setattr(claude_code, "_exec", fake_exec)
        monkeypatch.setattr(claude_code, "_READ_S", 1.0, raising=False)
        backend = ClaudeCodeSession(cli="/bin/echo", tools=("Read",), actor=actor)
        task = asyncio.create_task(actor.run(Profile("p"), RunRequest(inputs=("go",), max_rounds=1), backend))
        t0 = time.monotonic()
        done, _ = await asyncio.wait({task}, timeout=3)
        state["client"].close()
        assert done and task.cancelled(), "the call did not return under the cancel"
        assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []
        return time.monotonic() - t0

    assert asyncio.run(go()) < 2.0


def test_an_ask_that_will_not_end_is_abandoned_at_the_bound_and_a_cancelling_caller_still_returns(
    monkeypatch,
):
    """The call's end cancels and joins its asks; one that swallows its cancel would keep the join open
    for ever, and a caller cancelling the call on every turn of that join would be swallowed with it
    (that is what a SIGINT becomes under asyncio.run). The join is bounded: at _JOIN_S the ask's hook
    connection is dropped, the ask is abandoned with a warning, and the caller's cancel is raised, so
    the call returns either way. The hook, the real one, reads EOF and refuses; the allow the abandoned
    ask answers later reaches nothing."""
    import shlex
    import time
    import warnings

    free = {"it": None, "came": None, "at": None}

    async def stuck(actor, run, requires, closed, came, reader, writer):
        await reader.read()  # the hook's request, then a defect: it swallows every cancel until let go
        came.set()
        free["at"] = time.monotonic()
        free["came"].set()
        while not free["it"].is_set():
            try:
                await free["it"].wait()
            except asyncio.CancelledError:
                continue
        await claude_code._answer(writer, {"allow": {"file_path": "notes.txt"}})  # too late to land

    async def go(cancelling: bool):
        actor = Actor("gated")
        state = {"hook": None}
        free["it"], free["came"], free["at"] = asyncio.Event(), asyncio.Event(), None

        async def fake_exec(cli, args, prompt, **kw):
            settings = json.loads(args[args.index("--settings") + 1])
            command = shlex.split(settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"])
            hook = await asyncio.create_subprocess_exec(  # the CLI's pre-tool hook, as the CLI runs it
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            hook.stdin.write(json.dumps(_tool("Read", "toolu_x", file_path="notes.txt")).encode())
            hook.stdin.close()
            state["hook"] = hook
            # the ask is accepted and has the request before the call ends: a hook slow to start (a
            # loaded machine) is waited for, never raced by a fixed sleep
            await asyncio.wait_for(free["came"].wait(), 20)
            return "ok", {"total_cost_usd": 0.0}

        monkeypatch.setattr(claude_code, "_exec", fake_exec)
        monkeypatch.setattr(claude_code, "_ask", stuck)
        monkeypatch.setattr(claude_code, "_JOIN_S", 0.5, raising=False)
        backend = ClaudeCodeSession(cli="/bin/echo", tools=("Read",), actor=actor)
        task = asyncio.create_task(actor.run(Profile("p"), RunRequest(inputs=("go",), max_rounds=1), backend))
        if cancelling:  # a caller that cancels on every turn, as a SIGINT storm would

            async def storm():
                # the ask is there to join, however long its hook took; one that never comes fails the test
                await asyncio.wait_for(free["came"].wait(), 20)
                while not task.done():
                    task.cancel()
                    await asyncio.sleep(0)

            asyncio.create_task(storm())
        t0 = time.monotonic()
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", RuntimeWarning)
                done, _ = await asyncio.wait({task}, timeout=25)  # the hook's start is not what is timed
            took = time.monotonic() - (free["at"] or t0)  # from the ask's arrival: the join, and its bound
            left = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            assert done, f"the call did not return within 25s (cancelling={cancelling}), tasks left: {left}"
            free["it"].set()  # the abandoned ask is let go: it answers allow now, to a dropped connection
            await asyncio.gather(*left, return_exceptions=True)
            out, err = await asyncio.wait_for(state["hook"].communicate(), 3)
            hook = (state["hook"].returncode, out.decode(), err.decode())
        finally:
            free["it"].set()  # let go on a failed assert too, so the loop can close
            await asyncio.gather(
                *[t for t in asyncio.all_tasks() if t is not asyncio.current_task()], return_exceptions=True
            )
        said = [str(w.message) for w in caught if "abandoned" in str(w.message)]
        return task.cancelled(), took, said, len(left), hook

    def refused(hook) -> None:
        rc, out, err = hook
        assert rc == 2 and out == "" and "could not be asked" in err, hook  # exit 2 = the tool does not run

    cancelled, took, said, left, hook = asyncio.run(go(False))
    assert not cancelled and 0.5 <= took < 2.0, (cancelled, took)
    assert len(said) == 1 and "1 gate ask(s) did not end within 0.5s" in said[0], said
    assert left == 1  # the abandoned ask, still there: abandoned is not killed
    refused(hook)
    cancelled, took, said, left, hook = asyncio.run(go(True))
    assert cancelled and 0.5 <= took < 2.0, (cancelled, took)
    assert len(said) == 1 and left == 1, (said, left)
    refused(hook)


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
    cancellation and admits anyway admits nothing: the call is over. A call cancelled again while it
    settles still leaves nothing of the gate running."""
    import time

    cli = _scripted(tmp_path, _tool("Read", "toolu_x", file_path="notes.txt"), script=STRANDED)

    async def arm(mode: str) -> None:
        (tmp_path / "mode").write_text(mode)
        errors: list[str] = []
        asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: errors.append(ctx["message"]))
        actor = Actor("gated")
        entered, approved = asyncio.Event(), asyncio.Event()
        runs = []

        cleaned = asyncio.Event()

        @actor.before
        async def approval(run, la, request):
            runs.append(run)
            entered.set()
            try:
                await approved.wait()
            except asyncio.CancelledError:
                if mode != "insist":
                    raise
            finally:
                if mode == "cancel-in-join":  # a hook whose cleanup takes a while after its cancel
                    await cleaned.wait()

        backend = ClaudeCodeSession(cli=cli, tools=("Read",), actor=actor, timeout=1.0)
        t0 = time.monotonic()
        request = RunRequest(inputs=("go",), max_rounds=1)
        task = asyncio.create_task(actor.run(Profile("worker"), request, backend))
        await asyncio.wait_for(entered.wait(), 5)
        if mode.startswith("cancel"):
            task.cancel()
        if mode == "cancel-twice":  # cancelled again while it settles: the gate is still joined
            await asyncio.sleep(0.05)
            task.cancel()
        if mode == "cancel-in-join":  # cancelled again while the hook cleans up: joined before the return
            await asyncio.sleep(0.4)
            task.cancel()
            await asyncio.sleep(0.1)
            assert not task.done() and len(runs[0].record.of_kind(Kind.RESULT)) == 0
            cleaned.set()
        done, _ = await asyncio.wait({task}, timeout=2.5)
        assert done and time.monotonic() - t0 < 2.0, mode
        if mode.startswith("cancel"):
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

    for mode in ("fail", "deadline", "cancel", "cancel-twice", "cancel-in-join", "insist"):
        asyncio.run(arm(mode))


def _forking(*texts: str):
    """A CLI whose sessions fork: a call resumed from a point holds that point's prompts and replies,
    reports its own context (4000) plus a token per four characters held, and prints a running total
    that is its point's plus its own cent."""
    calls: list[dict] = []
    held: dict[str | None, int] = {None: 0}
    total: dict[str | None, float] = {None: 0.0}
    replies = iter(texts)

    async def fake(cli, args, prompt, *, model, timeout, cwd=None, persist=False):
        base = _after(args, "--resume") if "--resume" in args else None
        sid = _after(args, "--session-id")
        text = next(replies)
        chars = held[base] + len(prompt)
        held[sid], total[sid] = chars + len(text), total[base] + 0.01
        calls.append({"args": args, "prompt": prompt, "count": 4000 + chars // 4})
        usage = {"input_tokens": 4000 + chars // 4, "output_tokens": 1}
        return text, {"usage": usage, "total_cost_usd": total[sid], "session_id": sid}

    return fake, calls


READ = '<lact f>read(path="notes")</lact>'
HIDE = '<lact h>context.hide(refs=["_r3"])</lact>'
SHOW = Message("user", "[_r9 v context.show] shown: _r3", name="_r9", spec="context.show")
S10 = Message("system", "<system round=4/9 seq=10>\n</system>", name="_s10")


def _chat(size: int) -> dict[str, Message]:
    """One conversation as the actor renders it: an input, a read of `size` characters, its hide."""
    m = {
        "in0": Message("user", "[_in0] read the notes, then put them away", name="_in0"),
        "s1": Message(
            "system", "<system round=1/9 seq=1>\ncontext: ~20 / 100000 tokens\n</system>", name="_s1"
        ),
        "t2": Message("assistant", READ, name="_t2"),
        "r3": Message("user", "[_r3 f read] " + "n" * size, name="_r3", spec="read"),
        "s4": Message("system", "<system round=2/9 seq=4>\nsettled:\n  f (_r3, read)\n</system>", name="_s4"),
        "t5": Message("assistant", HIDE, name="_t5"),
        "r6": Message("user", "[_r6 h context.hide] hidden: _r3", name="_r6", spec="context.hide"),
        "s7": Message("system", "<system round=3/9 seq=7>\n</system>", name="_s7"),
    }
    m["ph"] = Message(
        "user",
        f"[hidden; *name or context.show reopens: _r3 f read ~{size // 4} tokens]",
        name="_r3",
        folded=True,
    )
    return m


def _views(m: dict[str, Message]) -> tuple[list[Message], list[Message], list[Message]]:
    one = [SYSTEM, m["in0"], m["s1"]]
    two = [SYSTEM, m["in0"], m["s1"], m["t2"], m["r3"], m["s4"]]
    three = [SYSTEM, m["in0"], m["s1"], m["t2"], m["ph"], m["s4"], m["t5"], m["r6"], m["s7"]]
    return one, two, three


def test_a_hide_that_frees_less_than_a_restart_rewrites_waits_and_the_session_goes_on(monkeypatch):
    """A hide of a few hundred characters: starting over would write more than the next ten turns
    save, so the session goes on with only what is new, and says what it still holds."""
    fake, calls = _forking(READ, HIDE, "noted")
    monkeypatch.setattr(claude_code, "_exec", fake)
    s = ClaudeCodeSession(cli="/bin/claude")
    m = _chat(400)
    one, two, three = _views(m)
    asyncio.run(s(one))
    asyncio.run(s(two))
    plan = s.plan(three[1:-1], forced=False)  # asked before the notification is written
    assert not plan.restart and 50 < plan.frees < 110  # ~400 characters the view no longer shows
    asyncio.run(s(three))
    third = calls[2]
    assert _after(third["args"], "--resume") == _after(calls[1]["args"], "--session-id")
    assert third["prompt"] == m["r6"].content + "\n\n" + m["s7"].content and s.restarts == 0
    # the figure told is what the call stood in, the hidden read still counted, never the view's size
    assert abs(plan.tokens - third["count"]) <= len(m["s7"].content) // 4 + 1
    later = [*three[1:], Message("assistant", "noted", name="_t8")]
    assert s.plan(later).frees == plan.frees and not s.plan(later).restart  # held until a restart pays


def test_a_hide_that_frees_more_than_it_rewrites_forks_from_the_point_before_it(monkeypatch):
    """A hide of 20k characters: the session goes on from the last point it passed before the hidden
    read, sending only what came after it as a transcript, and the figure is that point's count
    plus what it sends, never the view's bare estimate."""
    fake, calls = _forking(READ, HIDE, "noted", "back")
    monkeypatch.setattr(claude_code, "_exec", fake)
    s = ClaudeCodeSession(cli="/bin/claude")
    m = _chat(20000)
    one, two, three = _views(m)
    asyncio.run(s(one))
    asyncio.run(s(two))
    plan = s.plan(three[1:-1])
    assert plan.restart and 4900 < plan.frees < 5100
    asyncio.run(s(three))
    third = calls[2]
    assert _after(third["args"], "--resume") == _after(calls[0]["args"], "--session-id")
    assert "--fork-session" in third["args"] and "--system-prompt" not in third["args"]
    assert (
        third["prompt"].startswith("<USER>\n[hidden;")
        and f"<ASSISTANT>\n{HIDE}\n</ASSISTANT>" in third["prompt"]
    )
    assert "n" * 100 not in third["prompt"] and "[_in0]" not in third["prompt"]  # the point holds it
    assert third["prompt"].endswith("<ASSISTANT>") and s.restarts == 1
    assert abs(plan.tokens - third["count"]) <= (len(m["s7"].content) + 40) // 4
    assert s.calls[2]["total_cost_usd"] == pytest.approx(0.01)  # its own share over the forked point
    # a show of what the restart dropped goes back to the point that still holds it, read from the
    # cache, rather than writing the 20k again
    four = [*three[:4], m["r3"], *three[5:], Message("assistant", "noted", name="_t8"), SHOW, S10]
    asyncio.run(s(four))
    fourth = calls[3]
    assert _after(fourth["args"], "--resume") == _after(calls[1]["args"], "--session-id") and s.restarts == 2
    assert fourth["prompt"].startswith("<USER>\n[_r6 h context.hide]") and "n" * 100 not in fourth["prompt"]


def test_a_show_of_what_no_live_point_holds_is_sent_as_new(monkeypatch):
    """The same show when the point holding the read has passed the cache's life: going back to it
    would write the 20k again, so the entry is sent as new on the current session."""
    now = [0.0]
    monkeypatch.setattr(claude_code, "_clock", lambda: now[0])
    fake, calls = _forking(READ, HIDE, "noted", "back")
    monkeypatch.setattr(claude_code, "_exec", fake)
    s = ClaudeCodeSession(cli="/bin/claude")
    m = _chat(20000)
    one, two, three = _views(m)
    asyncio.run(s(one))
    asyncio.run(s(two))
    now[0] = 3000.0
    asyncio.run(s(three))  # forks from the first point, which is read again now
    now[0] = claude_code._CACHE_TTL_S + 100  # the second point, the one holding the read, is past it
    four = [*three[:4], m["r3"], *three[5:], Message("assistant", "noted", name="_t8"), SHOW, S10]
    asyncio.run(s(four))
    fourth = calls[3]
    assert _after(fourth["args"], "--resume") == _after(calls[2]["args"], "--session-id") and s.restarts == 1
    assert fourth["prompt"] == "\n\n".join((m["r3"].content, SHOW.content, S10.content))


def test_a_show_of_what_the_session_still_holds_sends_nothing_again(monkeypatch):
    fake, calls = _forking(READ, HIDE, "noted", "back")
    monkeypatch.setattr(claude_code, "_exec", fake)
    s = ClaudeCodeSession(cli="/bin/claude")
    m = _chat(400)
    one, two, three = _views(m)
    for view in (one, two, three):
        asyncio.run(s(view))
    four = [*three[:4], m["r3"], *three[5:], Message("assistant", "noted", name="_t8"), SHOW, S10]
    planned = s.plan(four[1:-1])
    assert (planned.restart, planned.frees) == (False, 0)
    asyncio.run(s(four))
    assert calls[3]["prompt"] == SHOW.content + "\n\n" + S10.content and s.restarts == 0
    # the count the call reported is the one planned, and the notice the plan did not yet see
    assert 0 <= calls[3]["count"] - planned.tokens <= len(S10.content) // 4 + 2


def test_with_no_point_left_intact_the_session_starts_over_from_the_whole_view(monkeypatch):
    """A summary over the first round's notice: every point the session passed holds it, so the
    restart writes the whole view as a new session, the system prompt with it."""
    fake, calls = _forking(READ, HIDE, "noted")
    monkeypatch.setattr(claude_code, "_exec", fake)
    s = ClaudeCodeSession(cli="/bin/claude")
    m = _chat(20000)
    one, two, _ = _views(m)
    asyncio.run(s(one))
    asyncio.run(s(two))
    summary = Message("user", "[summary before _s1] the notes say three things", name="_s1", folded=True)
    three = [SYSTEM, m["in0"], summary, m["s4"], m["t5"], m["r6"], m["s7"]]
    asyncio.run(s(three))
    third = calls[2]
    assert "--resume" not in third["args"] and _after(third["args"], "--system-prompt") == "the profile"
    assert third["prompt"].startswith("<USER>\n[_in0]") and "n" * 100 not in third["prompt"]
    assert s.restarts == 1


def test_a_point_past_the_cache_life_is_priced_as_written_again(monkeypatch):
    """The same small hide, after an hour idle: the session's prefix would be written again whether it
    went on or started over, so starting over costs nothing extra and happens."""
    now = [0.0]
    monkeypatch.setattr(claude_code, "_clock", lambda: now[0])
    fake, calls = _forking(READ, HIDE, "noted")
    monkeypatch.setattr(claude_code, "_exec", fake)
    s = ClaudeCodeSession(cli="/bin/claude")
    one, two, three = _views(_chat(400))
    asyncio.run(s(one))
    asyncio.run(s(two))
    assert not s.plan(three[1:-1]).restart  # within the hour: it waits (the control)
    now[0] = claude_code._CACHE_TTL_S + 1
    assert s.plan(three[1:-1]).restart
    asyncio.run(s(three))
    assert s.restarts == 1


@pytest.mark.parametrize(
    ("usage", "warm"), [({"cache_read_input_tokens": 5000}, True), ({"output_tokens": 5}, False)]
)
def test_a_failed_call_that_read_the_cache_starts_its_points_hour_again(monkeypatch, usage, warm):
    """The call failed, but it was billed for reading the point's prefix, so the provider's entry is
    as fresh as a success would leave it: an hour after the point's last success it is still warm.
    A failure that reports no cache read leaves the point's hour where it was."""
    now = [0.0]
    monkeypatch.setattr(claude_code, "_clock", lambda: now[0])
    fake, calls = _forking(READ, HIDE, "noted")
    failures = [
        claude_code.ClaudeCodeError(
            "claude reported error_max_turns: cap", {"usage": usage, "total_cost_usd": 0.05}
        )
    ]

    async def failing_once(cli, args, prompt, **kw):
        if len(calls) == 2 and failures:
            raise failures.pop()
        return await fake(cli, args, prompt, **kw)

    monkeypatch.setattr(claude_code, "_exec", failing_once)
    s = ClaudeCodeSession(cli="/bin/claude")
    one, two, three = _views(_chat(400))
    asyncio.run(s(one))
    now[0] = 100.0
    asyncio.run(s(two))
    now[0] = 100.0 + claude_code._CACHE_TTL_S + 1
    assert s.plan(three[1:-1]).restart  # the control: past the hour since its last read, it starts over
    now[0] = 3000.0
    with pytest.raises(claude_code.ClaudeCodeError, match="error_max_turns"):
        asyncio.run(s(three))
    now[0] = 100.0 + claude_code._CACHE_TTL_S + 1
    assert s.plan(three[1:-1]).restart is not warm
    asyncio.run(s(three))
    assert s.restarts == (0 if warm else 1)
    if warm:
        assert _after(calls[2]["args"], "--resume") == _after(calls[1]["args"], "--session-id")


@pytest.mark.parametrize("fails", [False, True], ids=["answered", "failed with a cache read"])
@pytest.mark.parametrize(
    ("timing", "read_after"),
    [
        ({"duration_ms": 50000, "time_to_request_ms": 20000, "duration_api_ms": 1}, 30.0),
        ({"duration_ms": 90000, "time_to_request_ms": 0, "duration_api_ms": 1}, 60.0),
        ({}, 60.0),
        ({"duration_ms": 50000, "duration_api_ms": 1}, 60.0),
        ({"duration_ms": 50000, "time_to_request_ms": True, "duration_api_ms": 1}, 60.0),
        ({"duration_ms": 20000, "time_to_request_ms": 50000, "duration_api_ms": 1}, 60.0),
    ],
    ids=[
        "one request",
        "a turn longer than the call",
        "no time printed",
        "no first-request time printed",
        "a time printed as true",
        "a first request after the turn ended",
    ],
)
def test_a_points_hour_runs_from_the_providers_read_not_the_clis_startup(
    monkeypatch, fails, timing, read_after
):
    """Every call takes 60 s: 10 s starting the CLI, which can wait on MCP servers, then a 50 s turn
    whose request goes out 20 s in and is answered 30 s later. The provider read the point's prefix as
    the request arrived, so its hour runs from 30 s after the call went out, the reply's time less the
    rest of the turn: a second short of that hour the point is still warm though more than an hour has
    passed since the call went out, and a second past it a small hide starts over. The CLI's running api
    total, printed unchanged here, plays no part. With either time missing or not a number, a first
    request after the turn's end, or a turn longer than the call (which would place the read before the
    call went out), the hour runs from the reply, so the point is never taken as expired while the
    provider may still hold it. A failed call that reports a cache read starts the point's hour the
    same way."""
    now = [0.0]
    monkeypatch.setattr(claude_code, "_clock", lambda: now[0])
    fake, calls = _forking(READ, HIDE, "noted")
    sent: list[float] = []

    async def slow(cli, args, prompt, **kw):
        sent.append(now[0])
        now[0] += 60.0
        if fails and len(sent) == 3:
            raise claude_code.ClaudeCodeError(
                "claude reported error_max_turns: cap",
                {"usage": {"cache_read_input_tokens": 5000}, "total_cost_usd": 0.05, **timing},
            )
        text, envelope = await fake(cli, args, prompt, **kw)
        return text, {**envelope, **timing}

    monkeypatch.setattr(claude_code, "_exec", slow)
    s = ClaudeCodeSession(cli="/bin/claude")
    one, two, three = _views(_chat(400))
    asyncio.run(s(one))
    now[0] = 1000.0
    asyncio.run(s(two))
    if fails:
        now[0] = 2000.0
        with pytest.raises(claude_code.ClaudeCodeError, match="error_max_turns"):
            asyncio.run(s(three))
    read = sent[-1] + read_after
    now[0] = read + claude_code._CACHE_TTL_S - 1
    assert not s.plan(three[1:-1]).restart
    now[0] = read + claude_code._CACHE_TTL_S + 1
    assert s.plan(three[1:-1]).restart


@pytest.mark.parametrize(
    ("served", "asked", "size", "restart"),
    [
        ("claude-haiku-4-5-20251001", None, 1000, True),
        ("claude-opus-5-5", None, 1000, False),
        ("claude-opus-5-5", None, 1600, True),
        ("claude-fable-5-1", None, 1600, False),
        (None, "opus", 1000, False),
    ],
)
def test_a_restart_is_priced_at_the_cache_prices_of_the_model_the_cli_reports_serving(
    monkeypatch, served, asked, size, restart
):
    """A write costs twice an input token on every model, and a read a tenth of one on most, a
    twentieth on Opus 5.5 and a fortieth on Fable 5.1: the same hide pays for a restart on one model
    and not on the next. With no served model printed, an alias prices as the model the CLI runs."""
    monkeypatch.delenv("ANTHROPIC_DEFAULT_OPUS_MODEL", raising=False)
    fake, _ = _forking(READ, HIDE, "noted")

    async def serving(cli, args, prompt, **kw):
        text, envelope = await fake(cli, args, prompt, **kw)
        return text, ({**envelope, "modelUsage": {served: {"inputTokens": 4000}}} if served else envelope)

    monkeypatch.setattr(claude_code, "_exec", serving)
    s = ClaudeCodeSession(cli="/bin/claude", model=asked)
    one, two, three = _views(_chat(size))
    asyncio.run(s(one))
    asyncio.run(s(two))
    assert s.plan(three[1:-1]).restart is restart
    asyncio.run(s(three))
    assert s.restarts == int(restart)


@pytest.mark.parametrize(
    ("envelope", "asked", "ratio"),
    [
        ({"modelUsage": {"claude-opus-5-5-20261001": {}}}, None, 40),
        ({"modelUsage": {"claude-opus-5-50-20261001": {}}}, None, 20),
        ({"modelUsage": {"m": {"canonicalModel": "claude-fable-5-1"}}}, None, 80),
        (
            {
                "modelUsage": {
                    "claude-haiku-4-5": {"inputTokens": 10},
                    "claude-mythos-5-1": {"cacheReadInputTokens": 9000},
                }
            },
            None,
            80,
        ),
        (
            {
                "modelUsage": {
                    "claude-haiku-4-5": {"cacheReadInputTokens": 9000},
                    "claude-mythos-5-1": {"inputTokens": 10},
                }
            },
            None,
            20,
        ),
        ({"modelUsage": {"claude-haiku-4-5": {}}}, "claude-opus-5-5", 20),
        ({"modelUsage": {"claude-haiku-4-5": {}}}, "opus", 20),
        ({"modelUsage": {"opus": {}}}, None, 40),
        ({"modelUsage": {"m": {}}}, "claude-opus-5-5", 40),
        ({}, "claude-opus-5-5", 40),
        ({}, "claude-fable-5", 20),
        ({}, "opus", 40),
        ({}, "fable[1m]", 80),
        ({}, "sonnet", 20),
        ({}, "gpt-4o", 20),
        ({}, None, 20),
    ],
    ids=[
        "a dated id",
        "an id that only begins like one the table names",
        "the canonical name",
        "several, the one that took the most input",
        "several, the other way round",
        "the served model over the one asked for",
        "the served model over an alias asked for",
        "an alias served",
        "a served name that is no model id, the one asked for",
        "none served, the one asked for",
        "Fable 5 reads at a tenth",
        "an alias, as the CLI resolves it",
        "an alias with the long context",
        "an alias of a model at a tenth",
        "another provider's model",
        "nothing named",
    ],
)
def test_a_cache_writes_price_in_reads_is_the_served_models(monkeypatch, envelope, asked, ratio):
    for family in ("OPUS", "FABLE", "SONNET", "HAIKU"):
        monkeypatch.delenv(f"ANTHROPIC_DEFAULT_{family}_MODEL", raising=False)
    assert claude_code._write_over_read(envelope, asked) == ratio


def test_an_alias_prices_as_the_model_the_environment_points_it_at(monkeypatch):
    """`ANTHROPIC_DEFAULT_OPUS_MODEL` moves what the CLI runs for `opus`, and the call inherits it."""
    monkeypatch.setenv("ANTHROPIC_DEFAULT_OPUS_MODEL", "claude-haiku-4-5")
    assert claude_code._write_over_read({}, "opus") == 20
    monkeypatch.setenv("ANTHROPIC_DEFAULT_OPUS_MODEL", "claude-fable-5-1")
    assert claude_code._write_over_read({}, "opus") == 80


def test_a_continued_chats_first_figure_stands_on_the_last_session_calls_count(monkeypatch):
    """No point yet: the first call starts from the whole view, and the figure is the count the last
    call of the chat's previous session reported, moved by the characters since, never the bare
    estimate (which read ~15k where the call stood in ~31.7k)."""
    fake, calls = _forking("next")
    monkeypatch.setattr(claude_code, "_exec", fake)
    _, _, three = _views(_chat(400))
    body = three[1:-1]
    prompt = claude_code._transcript(body)
    s = ClaudeCodeSession(cli="/bin/claude", base=(31000, len(prompt) - 400))
    assert s.plan(body) == claude_code.SessionPlan(False, 0, 31100)
    assert ClaudeCodeSession(cli="/bin/claude").plan(body).tokens is None  # no count: the actor's own
    asyncio.run(s(three))
    assert s.calls[0]["prompt_chars"] == len(calls[0]["prompt"])
    assert s.base == (calls[0]["count"], len(calls[0]["prompt"]))  # a start sets its own


def _through_the_actor(tmp_path, monkeypatch, size: int):
    """A console run on the session backend: a read of `size` characters, its hide, two more turns."""
    from tests.test_chat import reader

    (tmp_path / "notes.txt").write_text("n" * size)
    turns = [
        '<lact f1>read(path="notes.txt")</lact>',
        '<lact h1>context.hide(refs=["f1"])</lact>',
        "noted",
        "ok",
    ]
    fake, calls = _forking(*turns)
    monkeypatch.setattr(claude_code, "_exec", fake)
    actor, profile = reader(tmp_path)
    s = ClaudeCodeSession(cli="/bin/claude")
    run = asyncio.run(actor.run(profile, RunRequest(inputs=("read the notes",), max_rounds=4), s))
    hidden = next(e for e in run.record if e.kind is Kind.RESULT and e.alias == "h1")
    assert hidden.content.ok and len(calls) == 4
    notices = [str(e.content) for e in run.record if str(e.content).startswith("<system round=")]
    figures = [int(n.split("context: ~")[1].split(" ")[0]) for n in notices]
    return s, calls, notices, figures


def test_through_the_actor_a_small_hide_is_told_as_still_held_and_the_figure_keeps_it(tmp_path, monkeypatch):
    s, calls, notices, figures = _through_the_actor(tmp_path, monkeypatch, 400)
    assert s.restarts == 0
    for before, call in zip(calls, calls[1:], strict=False):  # each call goes on from the one before
        assert _after(call["args"], "--resume") == _after(before["args"], "--session-id")
        assert "read the notes" not in call["prompt"]  # only what the view gained is sent
    assert "session: still holds ~" in notices[2] and "session:" not in notices[1]
    assert "session: still holds" not in notices[3]  # told once, while the amount stands
    # the figure is what each call stood in, the held read included: it did not drop at the hide
    assert figures[2] >= figures[1]
    for figure, call, notice in zip(figures[1:], calls[1:], notices[1:], strict=False):
        assert 0 <= call["count"] - figure <= len(notice) // 4 + 2


def test_through_the_actor_a_large_hide_is_told_as_a_restart_and_the_figure_drops(tmp_path, monkeypatch):
    s, calls, notices, figures = _through_the_actor(tmp_path, monkeypatch, 20000)
    assert s.restarts == 1 and "--fork-session" in calls[2]["args"]
    assert _after(calls[2]["args"], "--resume") == _after(calls[0]["args"], "--session-id")
    assert (
        "session: starts over from this view; ~" in notices[2]
        and "tokens it held beyond the view are gone" in notices[2]
    )
    assert figures[2] < figures[1] - 4000
    for figure, call, notice in zip(figures[1:], calls[1:], notices[1:], strict=False):
        assert abs(call["count"] - figure) <= (len(notice) + 60) // 4


def test_through_the_actor_the_runtimes_fold_forks_from_the_point_before_what_it_folded(
    tmp_path, monkeypatch
):
    """The view past its budget: the runtime folds the read, and the session starts over from the
    point made before the read was sent, so the read is gone from what the session holds."""
    from tests.test_chat import reader

    (tmp_path / "notes.txt").write_text("n" * 20000)
    fake, calls = _forking('<lact f1>read(path="notes.txt")</lact>', "noted", "ok")
    monkeypatch.setattr(claude_code, "_exec", fake)
    actor, profile = reader(tmp_path)
    s = ClaudeCodeSession(cli="/bin/claude")
    run = asyncio.run(
        actor.run(profile, RunRequest(inputs=("read the notes",), max_rounds=3, view_budget=3000), s)
    )
    notices = [str(e.content) for e in run.record if str(e.content).startswith("<system round=")]
    assert "1 older entries" in notices[2] and "session: starts over from this view; ~" in notices[2]
    assert "--fork-session" in calls[2]["args"]
    assert _after(calls[2]["args"], "--resume") == _after(calls[0]["args"], "--session-id")
    # the folded read goes as its placeholder; only the settled line's short preview carries its text
    assert "[hidden; *name or context.show reopens: _r3 f1 read ~" in calls[2]["prompt"]
    assert "n" * 1000 not in calls[2]["prompt"]
    figures = [int(n.split("context: ~")[1].split(" ")[0]) for n in notices]
    assert figures[2] < figures[1] - 4000
    for figure, call, notice in zip(figures[1:], calls[1:], notices[1:], strict=False):
        assert abs(call["count"] - figure) <= (len(notice) + 60) // 4  # told what each call stood in


def test_a_whole_view_restart_prices_the_system_prompt_it_writes_again(monkeypatch):
    """The summary that starts a session over from the whole view, under a long system prompt: a new
    session writes the prompt again where going on does not, so here going on is cheaper."""
    fake, calls = _forking(READ, HIDE, "noted")
    monkeypatch.setattr(claude_code, "_exec", fake)
    s = ClaudeCodeSession(cli="/bin/claude")
    m = _chat(20000)
    one, two, _ = _views(m)
    system = Message("system", "p" * 100_000)
    asyncio.run(s([system, *one[1:]]))
    asyncio.run(s([system, *two[1:]]))
    summary = Message("user", "[summary before _s1] the notes say three things", name="_s1", folded=True)
    three = [system, m["in0"], summary, m["s4"], m["t5"], m["r6"], m["s7"]]
    assert not s.plan(three[:-1]).restart
    asyncio.run(s(three))
    assert "--resume" in calls[2]["args"] and s.restarts == 0


def test_what_the_session_holds_beyond_the_view_counts_each_of_two_identical_replies():
    view = [Message("assistant", "ok" * 200)]
    held = ((claude_code._REPLY, hash("ok" * 200), 400), (claude_code._REPLY, hash("ok" * 200), 400))
    assert claude_code._beyond(view, held) == 400  # one shown, the other held beyond it
    assert claude_code._beyond(view + view, held) == 0
