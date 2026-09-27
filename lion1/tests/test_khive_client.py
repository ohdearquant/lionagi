"""The khive client's inbox read: a read whose answer the daemon dropped is read again once, and any
other error, or a second drop, is raised. The ops string reaches kkernel as one positional argument
after `--`, so nothing in it reads as an option."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from hub.khive import LOST, Khive, KhiveError


class Dropping(Khive):
    """A client whose `exec` drops the first N answers the way kkernel did on 2026-09-22."""

    def __init__(self, tmp_path: Path, drops: int, error: str = LOST):
        (tmp_path / ".khive").mkdir(exist_ok=True)
        (tmp_path / ".khive" / "config.toml").write_text('[actor]\nid = "lambda:x"\n')
        super().__init__(tmp_path, "lambda:x")
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
