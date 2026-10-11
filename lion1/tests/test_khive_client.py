"""The khive client's inbox read: a read whose answer the daemon dropped is read again once, and any
other error, or a second drop, is raised. The ops string reaches kkernel as one positional argument
after `--`, so nothing in it reads as an option."""

import asyncio
import errno
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lion1.khive import LOST, UNSTARTED, Khive, KhiveError, actor_name
from tests.lionfake import routed_config


class Dropping(Khive):
    """A client whose `exec` drops the first N answers, as a daemon under load can."""

    def __init__(self, tmp_path: Path, drops: int, error: str = LOST, page: dict | None = None):
        (tmp_path / ".khive").mkdir(exist_ok=True)
        (tmp_path / ".khive" / "config.toml").write_text(routed_config("agent-a", tmp_path))
        (tmp_path / "kkernel").write_text("")  # never run: exec is replaced below
        super().__init__(tmp_path, "agent-a", kkernel=str(tmp_path / "kkernel"))
        self.drops, self.error, self.ops = drops, error, []
        self.page = {"messages": [{"id": "m1"}]} if page is None else page

    async def exec(self, ops: str):
        self.ops.append(ops)
        if self.drops:
            self.drops -= 1
            raise KhiveError(f"kkernel exited 1: actor: x | Error: {self.error}; not retrying")
        return [self.page]


@pytest.mark.parametrize(
    "name", ["abc", "builder", "Builder-2", "a.b_c-d", "chair:desk", "email:p@example.com", "9" * 100]
)
def test_an_actor_name_is_a_plain_string_kept_as_written(name):
    assert actor_name(name) is name


@pytest.mark.parametrize(
    "name, rule",
    [
        ("", "length 0: a name is 3 to 100 characters"),
        ("ab", "length 2: a name is 3 to 100 characters"),
        ("9" * 101, "length 101: a name is 3 to 100 characters"),
        ("-builder", "the first character is not a letter or digit"),
        (":desk", "the first character is not a letter or digit"),
        (" builder", "the first character is not a letter or digit"),
        ("two words", "character 4 is not a letter, a digit or one of . _ - : @"),
        ("builder\n", "character 8 is not a letter, a digit or one of . _ - : @"),  # a trailing newline too
        ("build\u200ber", "character 6 is not ASCII"),  # invisible
        ("b\u00fcilder", "character 2 is not ASCII"),  # no look-alikes
        ("\u00fcber", "character 1 is not ASCII"),
        ("path/name", "character 5 is not a letter, a digit or one of . _ - : @"),
        ("glob*", "character 5 is not a letter, a digit or one of . _ - : @"),
        (None, "a name is a string"),
        (7, "a name is a string"),
    ],
)
def test_anything_else_is_refused_naming_where_it_came_from_and_the_rule_it_breaks(name, rule):
    with pytest.raises(ValueError) as caught:
        actor_name(name, "chair")
    assert str(caught.value).startswith("chair: ")
    assert str(caught.value).endswith(f" is not an actor name: {rule}")


def test_a_refused_long_value_is_shown_by_its_first_six_characters_and_its_length():
    """A value refused as a name may be a secret pasted into the wrong field, so the refusal never
    carries it whole; a short one cannot be one and is shown as it is."""
    pasted = "xq9" + "Q7" * 19 + "/"  # secret-shaped, 42 characters, refused for the `/`
    with pytest.raises(ValueError) as caught:
        actor_name(pasted, "owner")
    assert str(caught.value) == (
        "owner: 'xq9Q7Q'…[42 chars] is not an actor name: "
        "character 42 is not a letter, a digit or one of . _ - : @"
    )
    assert pasted[:7] not in str(caught.value)
    with pytest.raises(ValueError, match=r"^owner: 'x y' is not an actor name: character 2 "):
        actor_name("x y", "owner")


def test_a_client_for_a_name_that_is_no_actor_name_is_refused_before_anything_is_read(tmp_path):
    # no config and no kkernel here: the name is checked first, so the refusal names it and nothing else
    with pytest.raises(ValueError, match="khive actor: 'x' is not an actor name"):
        Khive(tmp_path, "x", kkernel=str(tmp_path / "absent"))


def test_a_dropped_inbox_read_is_read_again_once_and_only_that_error(tmp_path):
    k = Dropping(tmp_path, 1)
    assert asyncio.run(k.inbox(wait_ms=0)) == [{"id": "m1"}]
    assert len(k.ops) == 2 and k.ops[0] == k.ops[1]
    with pytest.raises(KhiveError, match="daemon response lost"):
        asyncio.run(Dropping(tmp_path, 2).inbox(wait_ms=0))  # twice in a row is raised, not retried forever
    assert (
        all("wait_ms=" not in op for op in k.ops) and Khive.WAIT_CAP_MS == 20_000
    )  # under the daemon deadline
    with pytest.raises(KhiveError, match="permission denied"):
        asyncio.run(
            Dropping(tmp_path, 1, error="permission denied").inbox(wait_ms=0)
        )  # another error: no retry


@pytest.mark.parametrize("messages", ["", {}, 0, "x", 5, None])
def test_a_page_whose_messages_are_not_a_list_is_refused_never_read_as_empty(tmp_path, messages):
    with pytest.raises(KhiveError, match="not a list"):
        asyncio.run(Dropping(tmp_path, 0, page={"messages": messages}).inbox(wait_ms=0))
    assert (
        asyncio.run(Dropping(tmp_path, 0, page={"count": 0}).inbox(wait_ms=0)) == []
    )  # the store's empty page
    assert asyncio.run(Dropping(tmp_path, 0, page={"messages": []}).inbox(wait_ms=0)) == []


def test_ops_is_one_positional_argument(tmp_path):
    """A stub kkernel records the argv it was started with: the ops string, an option-shaped one
    included, arrives unchanged as the last argument, after `--`."""
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text(routed_config("agent-a", tmp_path))
    log = tmp_path / "argv.jsonl"
    stub = tmp_path / "kkernel"
    stub.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print(json.dumps({'results': [{'ok': True, 'result': None}]}))\n"
    )
    stub.chmod(0o755)
    k = Khive(tmp_path, "agent-a", kkernel=str(stub))
    ops = ['gtd.tasks(query="two words")', "--ops-file=gtd.tasks().json"]
    for op in ops:
        assert asyncio.run(k.exec(op)) == [None]
    argvs = [json.loads(line) for line in log.read_text().splitlines()]
    assert [a[-2:] for a in argvs] == [["--", op] for op in ops], argvs
    assert all(a[:4] == ["exec", "--strict", "--expect-actor", "agent-a"] for a in argvs)


def _call(k: Khive, how: str, ops: str):
    return asyncio.run(k.exec(ops)) if how == "exec" else k.exec_sync(ops)


@pytest.mark.parametrize("how", ["exec", "exec_sync"])
def test_an_ops_string_past_the_argument_bound_is_a_khive_error_and_kkernel_never_starts(tmp_path, how):
    """The ops string travels as one argument, so past what the operating system takes for one
    command the launch itself is refused (E2BIG) before kkernel starts. The caller
    gets a KhiveError naming the string's size, as for every other refusal, with no kind: the store
    never answered."""
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text(routed_config("agent-a", tmp_path))
    started = tmp_path / "started"
    stub = tmp_path / "kkernel"
    stub.write_text(
        f"#!{sys.executable}\nopen({str(started)!r}, 'w').close()\nprint('{{\"results\": []}}')\n"
    )
    stub.chmod(0o755)
    k = Khive(tmp_path, "agent-a", kkernel=str(stub))
    assert _call(k, how, "gtd.tasks()") == []  # the stub starts for an ordinary call
    started.unlink()

    ops = f"gtd.tasks(query={json.dumps('x' * 4_000_000)})"
    with pytest.raises(KhiveError) as raised:
        _call(k, how, ops)
    assert "refused its arguments and environment as too large (E2BIG" in str(raised.value)
    assert f"the ops string is {len(ops.encode())} bytes" in str(raised.value)
    assert raised.value.kind is None
    assert not started.exists()


@pytest.mark.parametrize("how", ["exec", "exec_sync"])
def test_an_environment_past_the_bound_is_named_beside_a_small_ops_string(tmp_path, monkeypatch, how):
    """When the environment is the large one, the message names both sizes and does not put the
    excess on the ops string."""
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text(routed_config("agent-a", tmp_path))
    started = tmp_path / "started"
    stub = tmp_path / "kkernel"
    stub.write_text(
        f"#!{sys.executable}\nopen({str(started)!r}, 'w').close()\nprint('{{\"results\": []}}')\n"
    )
    stub.chmod(0o755)
    k = Khive(tmp_path, "agent-a", kkernel=str(stub))
    bound = os.sysconf("SC_ARG_MAX")
    monkeypatch.setenv("LION1_TEST_PAD", "x" * (bound + 65_536))

    with pytest.raises(KhiveError) as raised:
        _call(k, how, "gtd.tasks()")
    said = str(raised.value)
    assert said.startswith(f"{UNSTARTED}: the operating system refused its arguments"), said
    assert f"the ops string is {len(b'gtd.tasks()')} bytes" in said
    env = re.search(r"the environment (\d+) bytes", said)
    assert env is not None and int(env.group(1)) > bound, said
    assert not started.exists()


@pytest.mark.parametrize("how", ["exec", "exec_sync"])
def test_a_fault_after_kkernel_started_is_not_called_a_failed_start(tmp_path, monkeypatch, how):
    """Only the launch is a failed start. Once kkernel runs it may have applied the ops, so a fault
    reading its answer reaches the caller as it is, never as "could not start"."""
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text(routed_config("agent-a", tmp_path))
    started = tmp_path / "started"
    stub = tmp_path / "kkernel"
    stub.write_text(
        f"#!{sys.executable}\nopen({str(started)!r}, 'w').close()\nprint('{{\"results\": []}}')\n"
    )
    stub.chmod(0o755)
    k = Khive(tmp_path, "agent-a", kkernel=str(stub))

    def lost_sync(self, *args, **kwargs):
        self.wait()
        raise OSError(errno.EBADF, "Bad file descriptor")

    async def lost(self, *args, **kwargs):
        await self.wait()
        raise OSError(errno.EBADF, "Bad file descriptor")

    monkeypatch.setattr(subprocess.Popen, "communicate", lost_sync)
    monkeypatch.setattr(asyncio.subprocess.Process, "communicate", lost)
    with pytest.raises(OSError) as raised:
        _call(k, how, "gtd.tasks()")
    assert raised.value.errno == errno.EBADF
    assert started.exists()


@pytest.mark.parametrize("how", ["exec", "exec_sync"])
def test_a_kkernel_that_cannot_be_started_is_a_khive_error(tmp_path, how):
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text(routed_config("agent-a", tmp_path))
    stub = tmp_path / "kkernel"
    stub.write_text("")  # present, so the client is built, but not executable
    stub.chmod(0o644)
    k = Khive(tmp_path, "agent-a", kkernel=str(stub))
    with pytest.raises(KhiveError, match="^kkernel could not start: PermissionError"):
        _call(k, how, "gtd.tasks()")


def test_cancelling_a_call_stops_kkernel_and_its_children(tmp_path, monkeypatch):
    # the child was left to the transport on cancel, and killed without a wait on timeout: its
    # grandchild held the pipes open past the loop's end
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text(routed_config("agent-a", tmp_path))
    stub = tmp_path / "kkernel"
    stub.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > {tmp_path / 'grandchild'}\nwait\n")
    stub.chmod(0o755)
    procs = []
    real = asyncio.create_subprocess_exec

    async def capturing(*args, **kwargs):
        proc = await real(*args, **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capturing)
    k = Khive(tmp_path, "agent-a", kkernel=str(stub), timeout=30)

    async def main():
        task = asyncio.create_task(k.exec("comm.inbox()"))
        while not (procs and (tmp_path / "grandchild").exists()):
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return procs[0].returncode

    assert asyncio.run(main()) is not None  # reaped: the child did not outlive the cancelled task
    assert procs[0].stdout.at_eof() and procs[0].stderr.at_eof()
    grandchild = int((tmp_path / "grandchild").read_text())
    for _ in range(200):
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        raise AssertionError(f"kkernel's child {grandchild} outlived the cancelled call")
    (tmp_path / "grandchild").unlink()
    k.timeout = 0.2
    with pytest.raises(KhiveError, match="did not answer within"):
        asyncio.run(k.exec("comm.inbox()"))
    assert procs[1].returncode is not None and procs[1].stdout.at_eof()  # the timeout path reaps too


def test_a_timeout_after_kkernel_exited_still_stops_what_it_left_holding_the_pipes(tmp_path):
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text(routed_config("agent-a", tmp_path))
    stub = tmp_path / "kkernel"
    stub.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > {tmp_path / 'child'}\nexit 0\n")
    stub.chmod(0o755)
    k = Khive(tmp_path, "agent-a", kkernel=str(stub), timeout=0.5)
    t0 = time.monotonic()
    with pytest.raises(KhiveError, match="did not answer within"):
        asyncio.run(asyncio.wait_for(k.exec("comm.inbox()"), 15))
    assert time.monotonic() - t0 < 10
    child = int((tmp_path / "child").read_text())
    for _ in range(200):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        raise AssertionError(f"kkernel's child {child} outlived the timed-out call")


def test_an_explicit_config_rides_every_call_and_a_missing_one_is_refused_never_the_cwds(tmp_path):
    """`config=` names the khive config the calls sign with: kkernel gets it as `--config` before
    `--`, and the cwd needs no `.khive/config.toml` of its own; a config that is not a file refuses
    at construction, while without `config=` the same cwd is refused for holding no config."""
    log = tmp_path / "argv.jsonl"
    stub = tmp_path / "kkernel"
    stub.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print(json.dumps({'results': [{'ok': True, 'result': None}]}))\n"
    )
    stub.chmod(0o755)
    elsewhere = tmp_path / "elsewhere" / "config.toml"
    elsewhere.parent.mkdir()
    elsewhere.write_text('[actor]\nid = "agent-b"\n')
    cwd = tmp_path / "work"
    cwd.mkdir()  # no .khive here
    k = Khive(cwd, "agent-b", kkernel=str(stub), config=elsewhere)
    assert asyncio.run(k.exec("comm.inbox(limit=1)")) == [None]
    (argv,) = [json.loads(line) for line in log.read_text().splitlines()]
    assert argv == [
        "exec",
        "--strict",
        "--expect-actor",
        "agent-b",
        "--config",
        str(elsewhere),
        "--",
        "comm.inbox(limit=1)",
    ]
    with pytest.raises(KhiveError, match="no such khive config file"):
        Khive(cwd, "agent-b", kkernel=str(stub), config=tmp_path / "missing.toml")
    with pytest.raises(KhiveError, match="holds no .khive/config.toml"):  # the control: no config= at all
        Khive(cwd, "agent-b", kkernel=str(stub))


def test_kkernel_reads_the_config_the_client_names_never_one_the_environment_names(tmp_path, monkeypatch):
    """A relative `config=` reaches kkernel absolute, as named from the caller's directory, since kkernel
    runs in `cwd`; and kkernel starts without KHIVE_CONFIG and KHIVE_DB, either of which would choose
    another config or another database than the one a store checked. The rest of the environment rides
    as it is."""
    log = tmp_path / "seen.jsonl"
    stub = tmp_path / "kkernel"
    stub.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps([sys.argv[1:], os.getcwd(), "
        "{k: os.environ.get(k) for k in ('KHIVE_CONFIG', 'KHIVE_DB', 'LION1_PROBE')}]) + '\\n')\n"
        "print(json.dumps({'results': [{'ok': True, 'result': None}]}))\n"
    )
    stub.chmod(0o755)
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "config.toml").write_text('[actor]\nid = "agent-b"\n')
    work = tmp_path / "work"
    (work / ".khive").mkdir(parents=True)
    (work / ".khive" / "config.toml").write_text('[actor]\nid = "agent-b"\n')
    (work / "khive.toml").write_text('[actor]\nid = "agent-b"\n')  # khive's own discovery prefers it
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KHIVE_CONFIG", str(tmp_path / "other.toml"))
    monkeypatch.setenv("KHIVE_DB", str(tmp_path / "other.db"))
    monkeypatch.setenv("LION1_PROBE", "rides")
    named = Khive(work, "agent-b", kkernel=str(stub), config=Path("elsewhere/config.toml"))
    own = Khive(work, "agent-b", kkernel=str(stub))
    for k in (named, own):
        asyncio.run(k.exec("comm.inbox(limit=1)"))
        k.exec_sync("comm.inbox(limit=1)")
    seen = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(seen) == 4
    for _, cwd, env in seen:
        assert env == {"KHIVE_CONFIG": None, "KHIVE_DB": None, "LION1_PROBE": "rides"}
        assert os.path.samefile(cwd, work)
    for (
        argv,
        _,
        _,
    ) in seen:  # the file named, or with none named the cwd's .khive/config.toml, never khive.toml
        config = argv[argv.index("--config") + 1]
        assert os.path.isabs(config)
    named_files = {argv[argv.index("--config") + 1] for argv, _, _ in seen[:2]}
    own_files = {argv[argv.index("--config") + 1] for argv, _, _ in seen[2:]}
    assert [os.path.samefile(f, tmp_path / "elsewhere" / "config.toml") for f in named_files] == [True]
    assert [os.path.samefile(f, work / ".khive" / "config.toml") for f in own_files] == [True]


@pytest.mark.parametrize("code", [0, 1])
@pytest.mark.parametrize(
    "error, kind",
    [
        ({"kind": "not_found", "message": "not found: ev1"}, "not_found"),
        ({"message": "upstream_not_found: no route"}, None),
        ("not_found", None),
    ],
    ids=["the store's not-found", "a message naming not_found", "bare text"],
)
def test_a_failed_op_raises_with_the_kind_the_store_gave_its_error_and_no_other(tmp_path, code, error, kind):
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text(routed_config("agent-a", tmp_path))
    answer = {"results": [{"ok": False, "tool": "get", "error": error}], "status": "partial"}
    stub = tmp_path / "kkernel"
    stub.write_text(f"#!{sys.executable}\nimport sys\nprint({json.dumps(answer)!r})\nsys.exit({code})\n")
    stub.chmod(0o755)
    with pytest.raises(KhiveError) as raised:
        asyncio.run(Khive(tmp_path, "agent-a", kkernel=str(stub)).exec('get(id="ev1")'))
    assert raised.value.kind == kind


def test_a_wait_past_the_cap_is_sliced_and_an_offset_rides_the_read(tmp_path):
    k = Dropping(tmp_path, 0, page={"messages": []})
    assert asyncio.run(k.inbox(wait_ms=45_000, status="read", offset=50)) == []
    assert k.ops == [
        'comm.inbox(status="read", limit=50, offset=50, wait_ms=20000)',
        'comm.inbox(status="read", limit=50, offset=50, wait_ms=20000)',
        'comm.inbox(status="read", limit=50, offset=50, wait_ms=5000)',
    ]  # three slices under the daemon's deadline, until the wait is spent
    k = Dropping(tmp_path, 0)
    assert asyncio.run(k.inbox(wait_ms=45_000)) == [{"id": "m1"}] and len(k.ops) == 1  # mail ends the wait
    assert not hasattr(k, "mark_read")  # the mark-read is written at its call site as the DSL (ADR-0025 D7)
