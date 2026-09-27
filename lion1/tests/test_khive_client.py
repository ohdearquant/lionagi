"""The khive client's inbox read: a read whose answer the daemon dropped is read again once, and any
other error, or a second drop, is raised. The ops string reaches kkernel as one positional argument
after `--`, so nothing in it reads as an option."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from hub.khive import LOST, Khive, KhiveError


class Dropping(Khive):
    """A client whose `exec` drops the first N answers the way kkernel did on 2026-09-22."""

    def __init__(self, tmp_path: Path, drops: int, error: str = LOST):
        (tmp_path / ".khive").mkdir(exist_ok=True)
        (tmp_path / ".khive" / "config.toml").write_text('[actor]\nid = "lambda:x"\n')
        (tmp_path / "kkernel").write_text("")  # never run: exec is replaced below
        super().__init__(tmp_path, "lambda:x", kkernel=str(tmp_path / "kkernel"))
        self.drops, self.error, self.ops = drops, error, []

    async def exec(self, ops: str):
        self.ops.append(ops)
        if self.drops:
            self.drops -= 1
            raise KhiveError(f"kkernel exited 1: actor: x | Error: {self.error}; not retrying")
        return [{"messages": [{"id": "m1"}]}]


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


def test_ops_is_one_positional_argument(tmp_path):
    """A stub kkernel records the argv it was started with: the ops string, an option-shaped one
    included, arrives unchanged as the last argument, after `--`."""
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text('[actor]\nid = "lambda:x"\n')
    log = tmp_path / "argv.jsonl"
    stub = tmp_path / "kkernel"
    stub.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print(json.dumps({'results': [{'ok': True, 'result': None}]}))\n"
    )
    stub.chmod(0o755)
    k = Khive(tmp_path, "lambda:x", kkernel=str(stub))
    ops = ['gtd.tasks(query="two words")', "--ops-file=gtd.tasks().json"]
    for op in ops:
        assert asyncio.run(k.exec(op)) == [None]
    argvs = [json.loads(line) for line in log.read_text().splitlines()]
    assert [a[-2:] for a in argvs] == [["--", op] for op in ops], argvs
    assert all(a[:4] == ["exec", "--strict", "--expect-actor", "lambda:x"] for a in argvs)


def test_cancelling_a_call_stops_kkernel_and_its_children(tmp_path, monkeypatch):
    # the child was left to the transport on cancel, and killed without a wait on timeout: its
    # grandchild held the pipes open past the loop's end
    (tmp_path / ".khive").mkdir()
    (tmp_path / ".khive" / "config.toml").write_text('[actor]\nid = "lambda:x"\n')
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
    k = Khive(tmp_path, "lambda:x", kkernel=str(stub), timeout=30)

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
    (tmp_path / ".khive" / "config.toml").write_text('[actor]\nid = "lambda:x"\n')
    stub = tmp_path / "kkernel"
    stub.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > {tmp_path / 'child'}\nexit 0\n")
    stub.chmod(0o755)
    k = Khive(tmp_path, "lambda:x", kkernel=str(stub), timeout=0.5)
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
