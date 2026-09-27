"""The Codex backend against a fake `codex` CLI that speaks the JSONL event stream."""

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from hub.harness import Codex, CodexError
from hub.harness import codex as codex_mod
from hub.harness.codex import DISABLED, INSTRUCTIONS
from lionagi import Message

# The fake keeps a per-scratch attempt counter beside `last.txt`, so a prompt can ask for a
# failure on the first attempt and an answer on the second. With FAKE_CODEX_LOG set it also writes
# what it saw while it ran (argv, its scratch, the framing files), since the scratch is gone after.
FAKE = """\
import json, os, sys
argv = sys.argv[1:]
prompt = sys.stdin.read()
out = argv[argv.index("-o") + 1]
model = argv[argv.index("-m") + 1] if "-m" in argv else None
effort = next((a for a in argv if a.startswith("model_reasoning_effort=")), None)
counter = os.path.join(os.path.dirname(out), "attempts")
n = int(open(counter).read()) + 1 if os.path.exists(counter) else 1
open(counter, "w").write(str(n))
if os.environ.get("FAKE_CODEX_LOG"):
    scratch = os.path.dirname(out)
    seen = {"argv": argv, "cwd": os.getcwd(), "scratch": scratch, "attempt": n,
            "agents_md": open(os.path.join(scratch, "AGENTS.md")).read(),
            "instructions": open(os.path.join(scratch, "instructions.md")).read()}
    with open(os.environ["FAKE_CODEX_LOG"], "a") as f:
        f.write(json.dumps(seen) + "\\n")
ev = lambda **e: print(json.dumps(e))
ev(type="thread.started", thread_id="t1")
usage = {"input_tokens": 120, "cached_input_tokens": 100, "cache_write_input_tokens": 0,
         "output_tokens": 9, "reasoning_output_tokens": 4}
if "FAIL401" in prompt:
    ev(type="turn.started")
    ev(type="error", message="Reconnecting... 1/5 (unexpected status 401 Unauthorized)")
    ev(type="turn.failed", error={"message": "unexpected status 401 Unauthorized: Missing bearer"})
    sys.exit(1)
if "AUTHTIMEOUT" in prompt:  # an auth failure whose prose also says timeout
    ev(type="turn.started")
    ev(type="turn.failed", error={"message": "unexpected status 401 Unauthorized: request timeout"})
    sys.exit(1)
if "SEPTEXT" in prompt:  # the answer carries U+2028 unescaped and only the event stream has it
    ev(type="turn.started")
    sys.stdout.flush()
    item = {"type": "item.completed", "item": {"type": "agent_message", "text": "left\u2028right"}}
    sys.stdout.buffer.write((json.dumps(item, ensure_ascii=False) + "\\n").encode())
    sys.stdout.buffer.flush()
    ev(type="turn.completed", usage=usage)
    open(out, "w").write("")
    sys.exit(0)
if "FLAKY429" in prompt and n == 1:
    ev(type="turn.started")
    ev(type="turn.failed", error={"message": "unexpected status 429 Too Many Requests"})
    sys.exit(1)
if "EMPTY" in prompt and n == 1:
    ev(type="turn.started")
    ev(type="turn.completed", usage={**usage, "output_tokens": 900, "reasoning_output_tokens": 900})
    sys.exit(0)
if "BLANKFINAL" in prompt:  # commentary carried the answer, the final message came back empty
    ev(type="turn.started")
    ev(type="item.completed", item={"type": "agent_message", "text": '<lact r1>run(cmd="ls")</lact>'})
    ev(type="item.completed", item={"type": "agent_message", "text": ""})
    ev(type="turn.completed", usage=usage)
    open(out, "w").write("")
    sys.exit(0)
disabled = [argv[i + 1] for i, a in enumerate(argv) if a == "--disable"]
text = (f"seen {len(prompt)} chars; model={model}; {effort}; disabled={disabled}; "
        f"home={os.environ.get('CODEX_HOME')}; last-role-marker={'<ASSISTANT>' in prompt}")
ev(type="item.completed", item={"type": "error", "message": "clamping hook timeout"})
ev(type="turn.started")
ev(type="item.completed", item={"type": "agent_message", "text": text})
ev(type="turn.completed", usage=usage)
open(out, "w").write(text)
"""


@pytest.fixture
def fake_cli(tmp_path: Path) -> str:
    script = tmp_path / "fake_codex.py"
    script.write_text(FAKE)
    cli = tmp_path / "codex"
    cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    return str(cli)


@pytest.fixture
def observed(tmp_path: Path, monkeypatch):
    """What the fake saw while it ran, one dict per attempt, read after the call."""
    log = tmp_path / "observed.jsonl"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    return lambda: [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_the_view_goes_in_as_one_transcript_and_usage_comes_back_shaped(fake_cli, observed):
    backend = Codex(model="gpt-6-luna", effort="xhigh", cli=fake_cli)
    text = asyncio.run(
        backend([Message("system", "be brief"), Message("user", "hi"), Message("assistant", "yo")])
    )
    assert text.startswith("seen ") and "model=gpt-6-luna" in text
    assert 'model_reasoning_effort="xhigh"' in text and "last-role-marker=True" in text
    assert f"disabled={list(DISABLED)}" in text  # nothing that acts, nothing from the shared home
    (seen,) = observed()
    assert seen["agents_md"].startswith("This process is a text harness")
    argv = seen["argv"]  # the CLI's agent framing replaced, its environment lines dropped
    assert f'model_instructions_file="{Path(seen["scratch"]) / "instructions.md"}"' in argv
    assert seen["instructions"] == INSTRUCTIONS
    assert "include_environment_context=false" in argv
    call = backend.calls[-1]
    assert call["usage"]["input_tokens"] == 20 and call["usage"]["cache_read_input_tokens"] == 100
    assert call["usage"]["output_tokens"] == 9 and call["usage"]["reasoning_output_tokens"] == 4
    assert call["total_cost_usd"] is None and call["duration_api_ms"] >= 0
    assert call["attempts"] == 1 and call["notes"] == ["clamping hook timeout"]


def test_a_failed_turn_is_named_from_the_stream_and_is_not_retried(fake_cli, observed):
    backend = Codex(cli=fake_cli)
    with pytest.raises(CodexError, match="401 Unauthorized: Missing bearer"):
        asyncio.run(backend([Message("user", "please FAIL401")]))
    assert [s["attempt"] for s in observed()] == [1]  # 401 is not retried


def test_codex_scratch_cleaned_on_terminal_paths(fake_cli, observed):
    # every call's mkdtemp was left behind (1290 lionagi-codex-* dirs in TMPDIR)
    backend = Codex(cli=fake_cli)
    assert asyncio.run(backend([Message("user", "hi")])).startswith("seen ")
    with pytest.raises(CodexError) as e:
        asyncio.run(backend([Message("user", "please FAIL401")]))
    ok, failed = (Path(s["scratch"]) for s in observed())
    assert ok != failed and not ok.exists() and not failed.exists()
    # the stream went with the scratch, so the failed envelope carries it and the error names no file
    assert "last.jsonl" not in str(e.value)
    call = backend.calls[-1]
    assert call["error"] == str(e.value) and call["attempts"] == 1
    assert '"turn.failed"' in call["stream"] and "stderr" in call


def test_codex_calls_have_independent_scratch(fake_cli, observed, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    backend = Codex(cli=fake_cli)

    async def both():
        return await asyncio.gather(
            backend([Message("user", "FLAKY429 a")]),
            backend([Message("user", "FLAKY429 b")]),
            return_exceptions=True,  # a shared scratch races the fake's counter; the paths say why
        )

    answers = asyncio.run(both())
    per_scratch: dict[str, list[int]] = {}
    for s in observed():
        per_scratch.setdefault(s["scratch"], []).append(s["attempt"])
    assert len(per_scratch) == 2, per_scratch  # one each, never shared
    assert all(isinstance(t, str) and t.startswith("seen ") for t in answers), answers
    assert sorted(per_scratch.values()) == [[1, 2], [1, 2]]  # each call's retry reused its own
    assert not any(Path(p).exists() for p in per_scratch)
    assert [c["attempts"] for c in backend.calls] == [2, 2]


def test_codex_cancellation_joins_before_cleanup(tmp_path: Path, monkeypatch):
    seen = tmp_path / "argv"
    cli = tmp_path / "codex"
    cli.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {seen}\nexec sleep 30\n")
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    procs = []
    real = asyncio.create_subprocess_exec

    async def capturing(*args, **kwargs):
        proc = await real(*args, **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capturing)
    at_cleanup = []
    real_rmtree = codex_mod.shutil.rmtree

    def rmtree(path, *args, **kwargs):
        at_cleanup.append((Path(path), [p.returncode for p in procs]))
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(codex_mod.shutil, "rmtree", rmtree)
    backend = Codex(model="m", cli=str(cli), timeout=30)

    async def main():
        task = asyncio.create_task(backend([Message("user", "hi")]))
        while not (procs and seen.exists() and seen.read_text().endswith("-\n")):
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    argv = seen.read_text().splitlines()
    scratch = Path(argv[argv.index("-C") + 1])
    assert procs[0].returncode is not None  # reaped: the child did not outlive the cancelled call
    assert at_cleanup == [(scratch, [procs[0].returncode])]  # removed once, after the child was joined
    assert not scratch.exists()


CODEX_BILLED_THEN_403 = """\
import json, os, sys
argv = sys.argv[1:]
sys.stdin.read()
out = argv[argv.index("-o") + 1]
counter = os.path.join(os.path.dirname(out), "attempts")
n = int(open(counter).read()) + 1 if os.path.exists(counter) else 1
open(counter, "w").write(str(n))
ev = lambda **e: print(json.dumps(e))
ev(type="turn.started")
if n == 1:  # billed: a reasoning-heavy turn that ran out of output, no message
    ev(type="turn.completed", usage={"input_tokens": 5000, "cached_input_tokens": 0, "output_tokens": 900})
    sys.exit(0)
ev(type="turn.failed", error={"message": "unexpected status 403 Forbidden"})
sys.exit(1)
"""


def test_failed_envelope_is_preserved(tmp_path: Path, monkeypatch):
    # measured on the bench: the billed empty attempt's usage was on no envelope
    script = tmp_path / "fake_codex.py"
    script.write_text(CODEX_BILLED_THEN_403)
    cli = tmp_path / "codex"
    cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    backend = Codex(cli=str(cli), retries=2)
    with pytest.raises(CodexError, match="403 Forbidden"):
        asyncio.run(backend([Message("user", "go")]))
    assert backend.calls, "the 900 billed output tokens of attempt 1 are on no envelope"
    call = backend.calls[-1]
    assert call["usage"]["output_tokens"] == 900 and call["attempts"] == 2
    assert "403 Forbidden" in call["error"] and call["total_cost_usd"] is None


_real_sleep = asyncio.sleep


async def _nosleep(_s, *a, **k):
    await _real_sleep(0)


def test_a_transient_failure_is_retried_and_the_attempts_are_recorded(fake_cli):
    backend = Codex(cli=fake_cli)
    text = asyncio.run(backend([Message("user", "FLAKY429 then answer")]))
    assert text.startswith("seen ")
    call = backend.calls[-1]
    assert call["attempts"] == 2
    assert any(n.startswith("attempt 1 failed: exited 1: ") and "429" in n for n in call["notes"])


def test_an_empty_answer_is_retried_and_both_attempts_usage_is_counted(fake_cli):
    backend = Codex(cli=fake_cli)
    text = asyncio.run(backend([Message("user", "EMPTY first")]))
    assert text.startswith("seen ")
    call = backend.calls[-1]
    assert call["attempts"] == 2
    assert call["usage"]["output_tokens"] == 909 and call["usage"]["reasoning_output_tokens"] == 904
    assert call["usage"]["cache_read_input_tokens"] == 200 and call["usage"]["input_tokens"] == 40
    assert any(n.startswith("attempt 1 failed: no message: events") for n in call["notes"])


def test_a_blank_final_message_yields_the_last_non_empty_one_without_a_retry(fake_cli):
    backend = Codex(cli=fake_cli)
    text = asyncio.run(backend([Message("user", "BLANKFINAL")]))
    assert text == '<lact r1>run(cmd="ls")</lact>'
    assert backend.calls[-1]["attempts"] == 1


def test_a_private_home_rides_in_the_environment_and_must_hold_the_login(fake_cli, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(CodexError, match="no auth.json; make it a symlink"):
        Codex(cli=fake_cli, home=str(home))
    (home / "auth.json").write_text("{}")
    backend = Codex(cli=fake_cli, home=str(home))
    text = asyncio.run(backend([Message("user", "hi")]))
    assert f"home={home}" in text


def test_a_missing_cli_is_named():
    env = dict(os.environ)
    try:
        os.environ["PATH"] = ""
        with pytest.raises(CodexError, match="not found"):
            Codex()
    finally:
        os.environ.clear()
        os.environ.update(env)


def test_cancelling_an_attempt_stops_the_child_and_its_children(tmp_path: Path, monkeypatch):
    # the CLI's grandchild holds the pipes: it goes with the group, and the pipes close with it
    cli = tmp_path / "codex"
    cli.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > {tmp_path / 'grandchild'}\nwait\n")
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    procs = []
    real = asyncio.create_subprocess_exec

    async def capturing(*args, **kwargs):
        proc = await real(*args, **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capturing)
    backend = Codex(model="m", cli=str(cli), timeout=30)

    async def main():
        task = asyncio.create_task(backend._attempt(b"hi", tmp_path))
        while not (procs and (tmp_path / "grandchild").exists()):
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return procs[0].returncode

    assert asyncio.run(main()) is not None  # reaped: the child did not outlive the cancelled task
    assert procs[0].stdout.at_eof() and procs[0].stderr.at_eof()  # the pipes were drained and closed
    grandchild = int((tmp_path / "grandchild").read_text())
    for _ in range(200):  # an orphan is reaped by init; give it a moment
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        raise AssertionError(f"the CLI's child {grandchild} outlived the cancelled attempt")


def test_an_auth_failure_is_not_retried_whatever_else_its_message_says(fake_cli, observed):
    # "timeout" in the message made a 401 transient, against the never-401/403 rule
    backend = Codex(cli=fake_cli)
    with pytest.raises(CodexError, match="401 Unauthorized: request timeout"):
        asyncio.run(backend([Message("user", "please AUTHTIMEOUT")]))
    assert [s["attempt"] for s in observed()] == [1]


def test_an_answer_carrying_a_unicode_line_separator_is_read_from_the_event_stream(fake_cli):
    # events were split with str.splitlines, so an agent message holding U+2028 broke in two and
    # was skipped; with last.txt empty the answer was lost
    assert asyncio.run(Codex(cli=fake_cli)([Message("user", "SEPTEXT")])) == "left\u2028right"


def test_a_timeout_after_the_cli_exited_still_stops_what_it_left_holding_the_pipes(tmp_path):
    # the group was killed only while the leader ran; a leader that had exited with its child still
    # holding stdout skipped the kill, and the drain then waited on that child with no deadline
    cli = tmp_path / "codex"
    cli.write_text(f"#!/bin/sh\ncat > /dev/null\nsleep 30 &\necho $! > {tmp_path / 'child'}\nexit 0\n")
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    backend = Codex(cli=str(cli), timeout=0.5, retries=0)
    t0 = time.monotonic()
    with pytest.raises(CodexError, match="timed out"):
        asyncio.run(asyncio.wait_for(backend([Message("user", "hi")]), 15))
    assert time.monotonic() - t0 < 10
    child = int((tmp_path / "child").read_text())
    for _ in range(200):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        raise AssertionError(f"the CLI's child {child} outlived the timed-out attempt")


def test_a_call_cancelled_in_its_backoff_keeps_the_bill_of_the_attempt_that_came_back(fake_cli, monkeypatch):
    # the envelope was built only on success or error: a cancel in the sleep after a billed empty
    # answer dropped that attempt's usage from `calls`
    backend = Codex(cli=fake_cli)
    backing_off = asyncio.Event()

    real_sleep = asyncio.sleep

    async def sleep(seconds):
        if not seconds:  # asyncio's own streams yield with sleep(0); only the backoff waits
            return await real_sleep(0)
        backing_off.set()
        await asyncio.Event().wait()

    async def main():
        task = asyncio.create_task(backend([Message("user", "EMPTY")]))
        await backing_off.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    monkeypatch.setattr(codex_mod.asyncio, "sleep", sleep)
    asyncio.run(asyncio.wait_for(main(), 15))
    (call,) = backend.calls
    assert call["error"] == "cancelled" and call["attempts"] == 1
    assert call["usage"]["output_tokens"] == 900 and call["notes"][-1].startswith(
        "attempt 1 failed: no message"
    )
