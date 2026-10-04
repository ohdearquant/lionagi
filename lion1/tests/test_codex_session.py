"""The Codex session backend against a fake `codex` CLI that keeps threads: `exec` starts one,
`exec resume <id>` goes on in it, and `turn.completed` prints the thread's running totals."""

import asyncio
import gc
import json
import stat
import sys
from pathlib import Path

import pytest

from hub.backend import CodexError, CodexSession
from hub.backend import codex as codex_mod
from hub.backend.codex import INSTRUCTIONS
from lion1 import Message

# Each thread is a file beside the script: the prompts it was sent and its running totals. A turn's
# own input is 100 plus a quarter of every character the thread holds, so a resumed turn reads its
# whole history, and what `turn.completed` prints is the sum over the thread, as the real CLI does.
FAKE = """\
import json, os, sys, time
argv = sys.argv[1:]
here = os.path.dirname(os.path.abspath(__file__))
if argv == ["--version"]:
    print(os.environ.get("FAKE_CODEX_VERSION", "codex-cli 0.156.1"))
    sys.exit(0)
if argv[:2] == ["debug", "models"]:
    print(json.dumps({"models": [{"slug": "gpt-6-luna", "tool_mode": "code_mode_only"}]}))
    sys.exit(0)
assert argv[0] == "exec", argv
resumed = argv[1] == "resume"
prompt = sys.stdin.read()
threads = os.path.join(here, "threads")
os.makedirs(threads, exist_ok=True)
if resumed:
    thread = argv[2]
    state = json.load(open(os.path.join(threads, thread)))
else:
    thread = "0199aaaa-0000-4000-8000-%012d" % (len(os.listdir(threads)) + 1)
    state = {"prompts": [], "replies": [],
             "totals": {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}}
out = argv[argv.index("-o") + 1]
scratch = os.path.dirname(out)
counter = os.path.join(here, "attempts")
n = int(open(counter).read()) + 1 if os.path.exists(counter) else 1
open(counter, "w").write(str(n))
catalog = next(a.split("=", 1)[1].strip('"') for a in argv if a.startswith("model_catalog_json="))
seen = {"argv": argv, "cwd": os.getcwd(), "scratch": scratch, "thread": thread, "resumed": resumed,
        "prompt": prompt, "catalog": json.load(open(catalog)), "agents_md": open("AGENTS.md").read(),
        "instructions": open(os.path.join(scratch, "instructions.md")).read()}
with open(os.path.join(here, "observed.jsonl"), "a") as f:
    f.write(json.dumps(seen) + "\\n")
ev = lambda **e: print(json.dumps(e), flush=True)
if not os.environ.get("FAKE_CODEX_NO_THREAD"):
    ev(type="thread.started", thread_id=thread)
ev(type="turn.started")
state["prompts"].append(prompt)  # the CLI keeps a prompt it was sent, whatever comes of the turn
json.dump(state, open(os.path.join(threads, thread), "w"))
if "RESUME429" in prompt and resumed:
    ev(type="turn.failed", error={"message": "unexpected status 429 Too Many Requests"})
    sys.exit(1)
if "FAIL401" in prompt:
    ev(type="turn.failed", error={"message": "unexpected status 401 Unauthorized: Missing bearer"})
    sys.exit(1)
if "HANG" in prompt:
    time.sleep(60)
text = "reply %d of thread %s" % (len(state["replies"]) + 1, thread[-2:])
held = sum(len(p) for p in state["prompts"]) + sum(len(r) for r in state["replies"])
turn = {"input_tokens": 100 + held // 4, "cached_input_tokens": 40 if resumed else 0, "output_tokens": 5}
state["replies"].append(text)
state["totals"] = {k: state["totals"][k] + turn[k] for k in turn}
json.dump(state, open(os.path.join(threads, thread), "w"))
printed = dict(state["totals"])
if "LOWER" in prompt:  # a thread whose totals came back below the call before
    printed["input_tokens"] = 7
if "NOINPUT" in prompt:
    del printed["input_tokens"]
if "MUTEONCE" in prompt and not os.path.exists(os.path.join(here, "muted")):
    open(os.path.join(here, "muted"), "w").close()  # one turn that is billed and says nothing
    ev(type="turn.completed", usage=printed)
    sys.exit(0)
ev(type="item.completed", item={"type": "agent_message", "text": text})
ev(type="turn.completed", usage=printed)
open(out, "w").write(text)
home = os.environ.get("CODEX_HOME")
if home and os.environ.get("FAKE_CODEX_ROLLOUT"):
    day = os.path.join(home, "sessions", "2026", "10", "03")
    os.makedirs(day, exist_ok=True)
    with open(os.path.join(day, "rollout-2026-10-03T00-00-00-%s.jsonl" % thread), "a") as f:
        f.write(json.dumps({"type": "session_meta", "payload": {"id": thread}}) + "\\n")
        f.write(os.environ["FAKE_CODEX_ROLLOUT"] + "\\n")
"""

SYSTEM = Message("system", "You are the agent of this run. Answer in the command form.")
TOKEN_COUNT = {
    "type": "event_msg",
    "payload": {
        "type": "token_count",
        "info": {"model_context_window": 258400, "last_token_usage": {"input_tokens": 191}},
        "rate_limits": {"primary": {"used_percent": 25.0, "window_minutes": 10080}},
    },
}


@pytest.fixture
def cli(tmp_path: Path) -> str:
    script = tmp_path / "fake_codex.py"
    script.write_text(FAKE)
    path = tmp_path / "codex"
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture
def observed(tmp_path: Path):
    log = tmp_path / "observed.jsonl"
    return lambda: [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.fixture(autouse=True)
def _nosleep(monkeypatch):
    real = asyncio.sleep

    async def brief(_s, *a, **k):
        await real(0)

    monkeypatch.setattr(codex_mod.asyncio, "sleep", brief)


def _chat(size: int = 400) -> dict[str, Message]:
    """One conversation as the actor renders it: an input, a read of `size` characters, its hide."""
    m = {
        "in0": Message("user", "[_in0] read the notes, then put them away", name="_in0"),
        "s1": Message(
            "system", "<system turn=1/9 seq=1>\ncontext: ~20 / 100000 tokens\n</system>", name="_s1"
        ),
        "r3": Message("user", "[_r3 f read] " + "n" * size, name="_r3", spec="read"),
        "s4": Message("system", "<system turn=2/9 seq=4>\nsettled:\n  f (_r3, read)\n</system>", name="_s4"),
        "r6": Message("user", "[_r6 h context.hide] hidden: _r3", name="_r6", spec="context.hide"),
        "s7": Message("system", "<system turn=3/9 seq=7>\n</system>", name="_s7"),
    }
    m["ph"] = Message(
        "user",
        f"[hidden; *name or context.show reopens: _r3 f read ~{size // 4} tokens]",
        name="_r3",
        folded=True,
    )
    return m


def _turn(reply: str, name: str) -> Message:
    return Message("assistant", reply, name=name)


def _three(
    s: CodexSession, m: dict[str, Message]
) -> tuple[list[Message], list[Message], list[Message], str, str]:
    """The first two calls of the conversation and the view its third call will be handed."""
    one = [SYSTEM, m["in0"], m["s1"]]
    first = asyncio.run(s(one))
    two = [*one, _turn(first, "_t2"), m["r3"], m["s4"]]
    second = asyncio.run(s(two))
    three = [*one, _turn(first, "_t2"), m["ph"], m["s4"], _turn(second, "_t5"), m["r6"], m["s7"]]
    return one, two, three, first, second


def test_the_first_call_starts_a_thread_and_each_later_one_resumes_it_with_only_what_the_view_gained(
    cli, observed
):
    s = CodexSession(model="gpt-6-luna", effort="low", cli=cli)
    m = _chat()
    one = [SYSTEM, m["in0"], m["s1"]]
    first = asyncio.run(s(one))
    two = [*one, _turn(first, "_t2"), m["r3"], m["s4"]]
    second = asyncio.run(s(two))
    a, b = observed()
    assert first == "reply 1 of thread 01" and second == "reply 2 of thread 01"
    # a start: a persisted `exec`, read-only by its config key, with the flags of the one-shot shape
    assert a["argv"][:1] == ["exec"] and not a["resumed"] and "--ephemeral" not in a["argv"]
    assert 'sandbox_mode="read-only"' in a["argv"] and "--ignore-user-config" in a["argv"]
    assert "--sandbox" not in a["argv"] and "-C" not in a["argv"]  # the resume form takes neither
    assert a["argv"][-1] == "-" and a["argv"][a["argv"].index("-m") + 1] == "gpt-6-luna"
    assert a["prompt"] == m["in0"].content + "\n\n" + m["s1"].content
    # going on: the same thread, the same flags, only the entries the thread lacks
    assert b["argv"][:3] == ["exec", "resume", a["thread"]]
    assert [x.replace(b["scratch"], "S") for x in b["argv"][3:]] == [
        x.replace(a["scratch"], "S") for x in a["argv"][1:]
    ]
    assert b["prompt"] == m["r3"].content + "\n\n" + m["s4"].content
    for seen in (a, b):
        # the run's system prompt is the CLI's instructions, written for every call in the call's own
        # scratch; and the catalogue with nothing that acts goes with it
        assert seen["instructions"] == SYSTEM.content and seen["agents_md"] == codex_mod.SCRATCH_AGENTS_MD
        assert seen["catalog"]["models"] == [{"slug": "gpt-6-luna", **codex_mod.CHAT}]
    assert (
        a["scratch"] != b["scratch"] and not Path(a["scratch"]).exists() and not Path(b["scratch"]).exists()
    )
    assert s.session_id == a["thread"] and s.restarts == 0
    assert set(s.sent) == {"_in0", "_s1", "_r3", "_s4"}
    c1, c2 = s.calls
    assert (c1["step"], c2["step"]) == ("start", "continue")
    assert c1["thread_id"] == c2["thread_id"] == a["thread"]
    # the stream's totals are the thread's: a call's share is read against the call before
    assert c1["usage"]["input_tokens"] == c1["cumulative"]["input_tokens"] == first.prompt_tokens
    assert c2["cumulative"]["input_tokens"] == first.prompt_tokens + second.prompt_tokens
    assert second.prompt_tokens == 100 + (len(a["prompt"]) + len(first) + len(b["prompt"])) // 4
    # the envelope's own shape: the fresh input, and what the cache read of it
    assert c2["usage"]["input_tokens"] == second.prompt_tokens - 40
    assert c2["usage"]["cache_read_input_tokens"] == 40 and c2["usage"]["output_tokens"] == 5
    # every prompt and reply the thread held when the call went out
    assert (
        c2["prompt_chars"] == len(a["prompt"]) + len(first) + len(b["prompt"]) and "usage_unknown" not in c2
    )
    # every call's child runs in one directory (a thread resumed elsewhere is sent AGENTS.md again),
    # which goes with the backend
    assert a["cwd"] == b["cwd"] != str(Path(a["scratch"]).resolve()) and Path(a["cwd"]).is_dir()
    del s
    gc.collect()
    assert not Path(a["cwd"]).exists()


def test_a_view_with_no_system_message_runs_under_the_chat_framing(cli, observed):
    s = CodexSession(cli=cli)
    asyncio.run(s([Message("user", "hi", name="_in0")]))
    assert observed()[0]["instructions"] == INSTRUCTIONS


def test_a_hide_that_frees_less_than_starting_over_sends_again_waits_and_the_thread_goes_on(cli, observed):
    s = CodexSession(cli=cli)
    m = _chat(400)
    _, _, three, _, _ = _three(s, m)
    plan = s.plan(three[1:-1])  # asked before the notification is written
    assert not plan.restart and 50 < plan.frees < 110  # ~400 characters the view no longer shows
    third = asyncio.run(s(three))
    c = observed()[2]
    assert c["resumed"] and c["prompt"] == m["r6"].content + "\n\n" + m["s7"].content
    assert s.restarts == 0 and s.calls[2]["step"] == "continue"
    # the figure told is what the call stood in, the hidden read still counted, never the view's size
    assert abs(plan.tokens - third.prompt_tokens) <= len(m["s7"].content) // 4 + 1


def test_a_hide_that_frees_more_than_starting_over_sends_again_starts_a_new_thread_from_the_view(
    cli, observed
):
    s = CodexSession(cli=cli)
    m = _chat(20000)
    _, _, three, first, second = _three(s, m)
    plan = s.plan(three[1:-1])
    assert plan.restart and 4900 < plan.frees < 5100
    third = asyncio.run(s(three))
    a, _, c = observed()
    assert not c["resumed"] and c["thread"] != a["thread"] and s.session_id == c["thread"]
    # the whole view as one transcript, the placeholder standing where the read was
    assert c["prompt"].startswith("<USER>\n[_in0]") and c["prompt"].endswith("<ASSISTANT>")
    assert m["ph"].content in c["prompt"] and "n" * 50 not in c["prompt"]
    assert f"<ASSISTANT>\n{first}\n</ASSISTANT>" in c["prompt"] and second in c["prompt"]
    assert s.restarts == 1 and s.calls[2]["step"] == "full"
    # a new thread's totals are its own
    assert (
        s.calls[2]["usage"]["input_tokens"] == s.calls[2]["cumulative"]["input_tokens"] == third.prompt_tokens
    )
    assert third.prompt_tokens == 100 + len(c["prompt"]) // 4
    # and the new thread is the one gone on from
    four = [*three, _turn(third, "_t8"), Message("user", "[_in9] thanks", name="_in9")]
    assert s.plan(four[1:]).frees == 0
    asyncio.run(s(four))
    d = observed()[3]
    assert d["argv"][:3] == ["exec", "resume", c["thread"]] and d["prompt"] == "[_in9] thanks"


def test_the_fold_starts_a_new_thread_whatever_it_frees(cli, observed):
    s = CodexSession(cli=cli)
    m = _chat(40)
    one = [SYSTEM, m["in0"], m["s1"]]
    first = asyncio.run(s(one))
    fold = Message(
        "system", "<system turn=2/9>\nfolded: the view was ~90000 of 60000 tokens", name="_s3", spec="fold"
    )
    view = [*one, _turn(first, "_t2")]
    assert s.plan(view[1:], forced=True).restart and not s.plan(view[1:]).restart
    asyncio.run(s([*view, fold]))
    a, b = observed()
    assert not b["resumed"] and b["thread"] != a["thread"] and fold.content in b["prompt"]
    assert s.restarts == 1 and s.calls[1]["step"] == "full"


def test_a_thread_past_its_cache_life_starts_over_on_a_small_hide(cli, observed, monkeypatch):
    """Going on would send the whole thread outside the cache anyway, so the hide's saving is free."""
    now = [1000.0]
    monkeypatch.setattr(codex_mod, "_clock", lambda: now[0])
    s = CodexSession(cli=cli)
    m = _chat(400)
    _, _, three, _, _ = _three(s, m)
    assert not s.plan(three[1:-1]).restart
    now[0] += codex_mod._CACHE_TTL_S
    assert s.plan(three[1:-1]).restart
    asyncio.run(s(three))
    assert not observed()[2]["resumed"] and s.restarts == 1


def test_the_plan_tells_the_count_the_next_call_will_stand_in(cli):
    s = CodexSession(cli=cli, base=(1000, 400))
    m = _chat()
    view = [m["in0"], m["s1"]]
    whole = len(m["in0"].content + "\n\n" + m["s1"].content)
    plan = s.plan(view)
    assert (plan.restart, plan.frees, plan.tokens) == (False, 0, 1000 + (whole - 400) // 4)
    assert CodexSession(cli=cli).plan(view).tokens is None  # nothing to stand a first figure on
    first = asyncio.run(s([SYSTEM, *view]))
    assert s.base == (first.prompt_tokens, whole)  # each start sets its own
    later = [*view, _turn(first, "_t2"), m["r3"]]
    assert s.plan(later).tokens == first.prompt_tokens + (len(first) + len(m["r3"].content)) // 4


@pytest.mark.parametrize("marker", ["LOWER", "NOINPUT"])
def test_a_total_below_the_call_before_or_missing_leaves_the_share_unknown(cli, marker):
    s = CodexSession(cli=cli)
    first = asyncio.run(s([SYSTEM, Message("user", "[_in0] hello", name="_in0")]))
    view = [SYSTEM, Message("user", "[_in0] hello", name="_in0"), _turn(first, "_t1")]
    second = asyncio.run(s([*view, Message("user", f"[_in2] {marker}", name="_in2")]))
    call = s.calls[1]
    assert second.prompt_tokens is None  # never a negative count, never the thread's total
    assert call["usage_unknown"] is True and call["prompt_tokens"] is None
    # the input is left out with its cached part, so the fresh figure is not their difference gone wrong
    assert call["usage"]["input_tokens"] == 0 and call["usage"]["cache_read_input_tokens"] == 0
    assert call["usage"]["output_tokens"] == 5  # the totals that did go up are still told
    assert s.plan([*view[1:], Message("user", f"[_in2] {marker}", name="_in2")]).tokens is None


def test_a_call_of_two_billed_attempts_names_the_answering_attempts_count_beside_their_sum(cli, observed):
    """A turn that ends with no message is billed and retried. `usage` is what the call spent, both
    attempts; the count the next call stands on is the answering attempt's alone."""
    s = CodexSession(cli=cli)
    reply = asyncio.run(s([SYSTEM, Message("user", "[_in0] MUTEONCE", name="_in0")]))
    a, b = observed()
    [call] = s.calls
    one = 100 + len(b["prompt"]) // 4
    assert call["attempts"] == 2 and a["thread"] != b["thread"] and a["prompt"] == b["prompt"]
    assert call["usage"]["input_tokens"] == 2 * one
    assert call["prompt_tokens"] == reply.prompt_tokens == one
    assert s.base == (one, call["prompt_chars"])


def test_a_retry_after_a_failure_on_a_resumed_thread_starts_a_new_thread_from_the_whole_view(cli, observed):
    """The CLI may have kept the failed attempt's prompt, so sending it to that thread again could
    put it there twice; the retry leaves the thread behind."""
    s = CodexSession(cli=cli)
    one = [SYSTEM, Message("user", "[_in0] hello", name="_in0")]
    first = asyncio.run(s(one))
    two = [*one, _turn(first, "_t1"), Message("user", "[_in2] RESUME429 please", name="_in2")]
    second = asyncio.run(s(two))
    a, b, c = observed()
    assert b["argv"][:3] == ["exec", "resume", a["thread"]] and b["prompt"] == "[_in2] RESUME429 please"
    assert not c["resumed"] and c["thread"] != a["thread"]
    assert c["prompt"].startswith("<USER>\n[_in0] hello") and "RESUME429" in c["prompt"]
    assert second == "reply 1 of thread 02" and s.session_id == c["thread"]
    call = s.calls[1]
    assert call["attempts"] == 2 and call["step"] == "full" and call["thread_id"] == c["thread"]
    assert "attempt 1 failed: exited 1: unexpected status 429 Too Many Requests" in call["notes"]
    assert "the retry starts a new thread from the whole view" in call["notes"]
    assert second.prompt_tokens == call["usage"]["input_tokens"] == 100 + len(c["prompt"]) // 4
    assert s.restarts == 0  # the view did not ask for this one
    three = [*two, _turn(second, "_t3"), Message("user", "[_in4] and then", name="_in4")]
    asyncio.run(s(three))
    assert observed()[3]["argv"][:3] == ["exec", "resume", c["thread"]]


def test_a_call_that_fails_on_a_resumed_thread_is_never_gone_on_from(cli, observed):
    s = CodexSession(cli=cli)
    one = [SYSTEM, Message("user", "[_in0] hello", name="_in0")]
    first = asyncio.run(s(one))
    two = [*one, _turn(first, "_t1"), Message("user", "[_in2] FAIL401", name="_in2")]
    with pytest.raises(CodexError, match="401 Unauthorized"):
        asyncio.run(s(two))
    assert s.calls[1]["error"].startswith("codex exited 1") and s.calls[1]["step"] == "continue"
    assert s.session_id is None and s.sent == {}
    three = [*one, _turn(first, "_t1"), Message("user", "[_in2] again", name="_in2")]
    assert s.plan(three[1:]).tokens is not None  # the first start's figure still anchors the next
    asyncio.run(s(three))
    c = observed()[2]
    assert (
        not c["resumed"] and c["prompt"].startswith("<USER>\n[_in0] hello") and s.calls[2]["step"] == "start"
    )


def test_a_start_that_fails_leaves_the_thread_before_it_as_it_was(cli, observed):
    """A new thread's failure touched nothing the session holds: the thread held is still gone on from."""
    s = CodexSession(cli=cli)
    m = _chat(40)
    one = [SYSTEM, m["in0"], m["s1"]]
    first = asyncio.run(s(one))
    fold = Message("system", "<system turn=2/9>\nfolded: FAIL401", name="_s3", spec="fold")
    with pytest.raises(CodexError):
        asyncio.run(s([*one, _turn(first, "_t2"), fold]))
    a = observed()[0]
    assert s.session_id == a["thread"]
    asyncio.run(s([*one, _turn(first, "_t2"), Message("user", "[_in3] go on", name="_in3")]))
    assert observed()[2]["argv"][:3] == ["exec", "resume", a["thread"]]


def test_a_call_cancelled_on_a_resumed_thread_is_never_gone_on_from(cli, observed):
    s = CodexSession(cli=cli)
    one = [SYSTEM, Message("user", "[_in0] hello", name="_in0")]

    async def go() -> None:
        first = await s(one)
        two = [*one, _turn(first, "_t1"), Message("user", "[_in2] HANG", name="_in2")]
        task = asyncio.create_task(s(two))
        for _ in range(500):
            if len(observed()) == 2:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert s.session_id is None
        await s([*one, _turn(first, "_t1"), Message("user", "[_in2] again", name="_in2")])

    asyncio.run(asyncio.wait_for(go(), 30))
    assert [c["resumed"] for c in observed()] == [False, True, False]
    assert len(s.calls) == 2  # the cancelled call's one attempt never came back: nothing to bill


def test_a_stream_that_names_no_thread_leaves_nothing_to_resume(cli, observed, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_NO_THREAD", "1")
    s = CodexSession(cli=cli)
    one = [SYSTEM, Message("user", "[_in0] hello", name="_in0")]
    first = asyncio.run(s(one))
    assert s.session_id is None and s.calls[0]["thread_id"] is None and first.prompt_tokens is not None
    asyncio.run(s([*one, _turn(first, "_t1"), Message("user", "[_in2] more", name="_in2")]))
    b = observed()[1]
    assert not b["resumed"] and b["prompt"].startswith("<USER>\n[_in0] hello")


def test_two_runs_through_one_backend_each_keep_their_own_thread(cli, observed):
    s = CodexSession(cli=cli)

    def view(run: str, *more: Message) -> list[Message]:
        head = [
            Message("system", f"the agent of {run}", run=run),
            Message("user", f"[_in0] {run}", name="_in0", run=run),
        ]
        return head + [Message(m.role, m.content, name=m.name, run=run) for m in more]

    async def go() -> None:
        a1, b1 = await asyncio.gather(s(view("a")), s(view("b")))  # both out at once: each lands its own
        await s(view("a", _turn(a1, "_t1"), Message("user", "[_in2] a again", name="_in2")))
        await s(view("b", _turn(b1, "_t1"), Message("user", "[_in2] b again", name="_in2")))

    asyncio.run(go())
    calls = observed()
    thread = {c["prompt"]: c["thread"] for c in calls[:2]}
    assert len(set(thread.values())) == 2
    third, fourth = calls[2], calls[3]
    assert third["argv"][:3] == ["exec", "resume", thread["[_in0] a"]] and third["prompt"] == "[_in2] a again"
    assert (
        fourth["argv"][:3] == ["exec", "resume", thread["[_in0] b"]] and fourth["prompt"] == "[_in2] b again"
    )
    assert third["instructions"] == "the agent of a" and fourth["instructions"] == "the agent of b"
    assert s.plan([], run="c").tokens is None and s.session_id is None  # a run not seen has no thread


def test_a_caller_naming_no_run_gets_a_new_thread_when_its_first_entry_differs(cli, observed):
    s = CodexSession(cli=cli)
    asyncio.run(s([SYSTEM, Message("user", "[_in0] one conversation", name="_in0")]))
    asyncio.run(s([SYSTEM, Message("user", "[_in0] another", name="_in0")]))
    a, b = observed()
    assert not b["resumed"] and b["thread"] != a["thread"] and b["prompt"] == "[_in0] another"
    assert s.calls[1]["step"] == "start" and s.restarts == 0


def test_the_envelope_carries_the_window_and_the_rate_limits_the_cli_wrote_for_the_thread(
    cli, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    (home / "auth.json").write_text("{}")
    monkeypatch.setenv("FAKE_CODEX_ROLLOUT", json.dumps(TOKEN_COUNT))
    s = CodexSession(cli=cli, home=str(home))
    asyncio.run(s([SYSTEM, Message("user", "[_in0] hello", name="_in0")]))
    call = s.calls[0]
    assert call["context_window"] == 258400
    assert call["rate_limits"] == {"primary": {"used_percent": 25.0, "window_minutes": 10080}}


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        json.dumps({"type": "event_msg", "payload": {"type": "agent_message"}}),
        json.dumps(
            {"type": "event_msg", "payload": {"type": "token_count", "info": None, "rate_limits": None}}
        ),
        json.dumps(
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"model_context_window": True}}}
        ),
        json.dumps({"payload": 3}),
        json.dumps({"payload": {"type": "token_count", "info": "unknown", "rate_limits": "none"}}),
        json.dumps({"payload": {"type": "token_count", "info": [1]}}),
    ],
)
def test_a_session_file_with_no_readable_count_adds_nothing_and_fails_nothing(
    cli, tmp_path, monkeypatch, line
):
    home = tmp_path / "home"
    home.mkdir()
    (home / "auth.json").write_text("{}")
    monkeypatch.setenv("FAKE_CODEX_ROLLOUT", line)
    s = CodexSession(cli=cli, home=str(home))
    assert asyncio.run(s([SYSTEM, Message("user", "[_in0] hello", name="_in0")])) == "reply 1 of thread 01"
    assert "context_window" not in s.calls[0] and "rate_limits" not in s.calls[0]


def test_no_session_file_adds_nothing(cli, tmp_path):
    (tmp_path / "auth.json").write_text("{}")
    s = CodexSession(cli=cli, home=str(tmp_path))  # the fake wrote none
    asyncio.run(s([SYSTEM, Message("user", "[_in0] hello", name="_in0")]))
    assert "context_window" not in s.calls[0] and s.calls[0]["thread_id"]


def test_a_chat_on_a_codex_model_keeps_one_thread_and_a_continued_chat_stands_on_its_last_count(
    cli, observed, tmp_path, monkeypatch
):
    """`lion chat --model codex/<model>`: the console's system prompt is the CLI's instructions, the
    model has no tools of its own, and a chat continued from its log seeds the new session's first
    figure with the count the last call's answering attempt reported and the characters it stood in:
    never the sum over a retried call's attempts, and nothing when that count was unknown."""
    import shutil

    import lion_cli.agent as agent_mod
    from lion_cli.chat import chat_main
    from lion_cli.main import parser

    which, seeds = shutil.which, []

    class Spy(CodexSession):
        async def __call__(self, messages):
            seeds.append(self.base)
            return await super().__call__(messages)

    monkeypatch.setattr(
        shutil, "which", lambda name, *a, **k: cli if name == "codex" else which(name, *a, **k)
    )
    monkeypatch.setattr(agent_mod, "CodexSession", Spy)
    work = tmp_path / "work"
    work.mkdir()
    args, log = ["chat", "--dir", str(work), "--model", "codex/gpt-6-luna"], str(tmp_path / "c.jsonl")
    said = ["--say", "MUTEONCE hi", "--turns", "1"]  # the first attempt is billed and says nothing
    asyncio.run(chat_main(parser().parse_args([*args, "--log", log, *said])))
    muted, a = observed()
    assert muted["prompt"] == a["prompt"]
    assert not a["resumed"] and a["argv"][a["argv"].index("-m") + 1] == "gpt-6-luna"
    assert a["instructions"] != INSTRUCTIONS and len(a["instructions"]) > 500  # the console's own prompt
    assert "hi" in a["prompt"] and a["instructions"] not in a["prompt"]
    assert {"--disable", "shell_tool"} <= set(a["argv"]) and 'sandbox_mode="read-only"' in a["argv"]
    assert seeds == [None]
    one = 100 + len(a["prompt"]) // 4
    usage = lambda f: [json.loads(x) for x in Path(f).read_text().splitlines() if '"USAGE"' in x]  # noqa: E731
    [row] = usage(log)
    assert row["usage"]["input_tokens"] == 2 * one and row["prompt_tokens"] == one
    log2 = str(tmp_path / "c2.jsonl")  # a continued chat keeps its own log
    again = ["-r", log, "--log", log2, "--say", "and again NOINPUT", "--turns", "1"]
    asyncio.run(chat_main(parser().parse_args([*args, *again])))
    b = observed()[2]
    assert not b["resumed"] and b["thread"] != a["thread"]  # a new process: a new thread, the whole view
    assert "hi" in b["prompt"] and "and again" in b["prompt"]
    assert seeds[1] == (one, len(a["prompt"]))
    # the figure the model is shown stands on that count: the view it was asked on, before its note
    asked, _, note = b["prompt"].rpartition("\n\n<SYSTEM>")
    asked += note.rpartition("</SYSTEM>")[2]  # the transcript's closing cue stood there too
    assert f"context: ~{one + (len(asked) - len(a['prompt'])) // 4} / " in note
    # that call's count was unknown: its row says so and seeds nothing
    assert usage(log2)[-1]["prompt_tokens"] is None
    asyncio.run(chat_main(parser().parse_args([*args, "-r", log2, "--say", "once more", "--turns", "1"])))
    assert seeds[2] is None
    # a call whose share of its thread's totals was unknown says that in its row too
    import io

    from lion_cli.view import Console

    rows = io.StringIO()
    Console("codex", calls=[{"model": "codex", "usage_unknown": True, "prompt_tokens": None}])._bill(rows, 1)
    assert json.loads(rows.getvalue())["usage_unknown"] is True


def test_an_unmeasured_release_is_refused_before_a_thread_is_started(cli, observed, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_VERSION", "codex-cli 0.157.0")
    s = CodexSession(cli=cli)
    with pytest.raises(CodexError, match="codex-cli 0.157.0 is not a release"):
        asyncio.run(s([SYSTEM, Message("user", "[_in0] hello", name="_in0")]))
    assert observed() == [] and s.calls[0]["attempts"] == 0 and s.session_id is None
    noted = CodexSession(cli=cli, unmeasured=True)
    asyncio.run(noted([SYSTEM, Message("user", "[_in0] hello", name="_in0")]))
    assert noted.calls[0]["notes"][0].startswith("codex-cli 0.157.0 is not a release")
