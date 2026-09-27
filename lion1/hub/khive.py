"""khive from a run: the actor's address, mailbox and store, one `kkernel exec` per call.

The actor has the address (ADR-0001); khive resolves it from the cwd's `.khive/config.toml`, so a
Khive is bound to a directory and asserts on every call that the directory resolves to the actor it
was built for (`--expect-actor`). Ops are the verb DSL the MCP `request` tool takes; string
arguments are JSON literals, so they go through `json.dumps`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = (
    "KhiveError",
    "LOST",
    "lit",
    "Khive",
)


class KhiveError(Exception):
    pass


LOST = "daemon response lost after request was sent"  # kkernel dropping a READ's answer; see inbox


def lit(value: Any) -> str:
    """A DSL argument: strings, numbers, booleans, null, arrays and objects are JSON literals."""
    return json.dumps(value)


@dataclass
class Khive:
    cwd: Path  # the directory whose .khive/config.toml names the actor
    actor: str  # what that directory must resolve to; every call asserts it
    kkernel: str | None = None
    timeout: float = 60.0
    calls: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cwd = Path(self.cwd)
        self.kkernel = self.kkernel or shutil.which("kkernel") or str(Path.home() / ".cargo/bin/kkernel")
        if not Path(self.kkernel).exists():
            raise KhiveError("kkernel not found on PATH or under ~/.cargo/bin")
        if not (self.cwd / ".khive" / "config.toml").exists():
            raise KhiveError(f"{self.cwd} holds no .khive/config.toml; khive would resolve it as 'local'")

    async def exec(self, ops: str) -> list[Any]:
        """Run one DSL expression; each op's `result`, in order. An op that is not ok raises."""
        # after `--`: an ops string that begins with `-` is the DSL, never an option
        argv = [self.kkernel, "exec", "--strict", "--expect-actor", self.actor, "--", ops]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(self.cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # the CLI's whole process group: a kill ends its children too
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except (TimeoutError, asyncio.CancelledError) as e:
            # the child is this client's to stop: a cancelled task must not leave it running, nor a
            # grandchild holding the pipes open past the loop's end
            # the group, whether or not its leader has exited: a child the leader left behind can
            # hold the pipes open, and the drain below would wait on it with no deadline
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
            await proc.communicate()  # drains and closes the pipes the cancelled read left open
            if isinstance(e, asyncio.CancelledError):
                raise
            raise KhiveError(f"kkernel did not answer within {self.timeout}s: {ops[:120]}") from None
        # the CLI prints a config warning and its database line on every call; keep the rest
        noise = (" WARN ", "database:")
        detail = [ln for ln in err.decode(errors="replace").splitlines() if not any(n in ln for n in noise)]
        try:
            data = json.loads(out.decode()) if out.strip() else {}
        except json.JSONDecodeError as e:
            raise KhiveError(f"unparseable kkernel output: {e}: {out[:300]!r}") from None
        results = data.get("results") or []
        bad = [r for r in results if not r.get("ok")]
        if proc.returncode != 0:
            # the op's own error is on stdout; stderr carries the refusal class and the actor line
            said = "; ".join(f"{r.get('tool')}: {str(r.get('error') or r)[:300]}" for r in bad)
            said = said or " | ".join(detail).strip() or out.decode(errors="replace").strip()
            raise KhiveError(f"kkernel exited {proc.returncode}: {said[-600:]}")
        self.calls.append({"ops": ops, "status": data.get("status"), "summary": data.get("summary")})
        if bad:
            raise KhiveError(f"{bad[0].get('tool')}: {str(bad[0].get('error') or bad[0])[:500]}")
        return [r.get("result") for r in results]

    # the mailbox

    # comm.inbox long-polls at most this long per call; a longer wait is sliced. 20 s, not the verb's
    # 30 s ceiling: the daemon client's own 30 s deadline covers connect+write+read, so a full-length
    # empty poll lands after it and reads as lost.
    WAIT_CAP_MS = 20_000

    async def inbox(
        self, *, wait_ms: int = 0, limit: int = 50, status: str = "unread", offset: int = 0
    ) -> list[dict]:
        """Mail in the box (unread by default), newest first; with wait_ms the call waits that long for
        mail before saying none. status="read" with offset pages what earlier wakes took."""
        remaining = max(0, wait_ms)
        while True:
            args = [f"status={lit(status)}", f"limit={limit}"]
            if offset:
                args.append(f"offset={offset}")
            slice_ms = min(remaining, self.WAIT_CAP_MS)
            if slice_ms:
                args.append(f"wait_ms={slice_ms}")
            try:
                (page,) = await self.exec(f"comm.inbox({', '.join(args)})")
            except KhiveError as e:
                # a read whose answer the daemon dropped is read again, once; a write is never
                # retried here. A guard until the store retries its own read verbs.
                if LOST not in str(e):
                    raise
                (page,) = await self.exec(f"comm.inbox({', '.join(args)})")
            mail = page.get("messages") or []
            remaining -= slice_ms
            if mail or remaining <= 0:
                return mail

    async def mark_read(self, ids: list[str]) -> None:
        if ids:
            await self.exec(f"comm.read(ids={lit(ids)})")

    async def send(
        self,
        to: str,
        content: str,
        *,
        subject: str | None = None,
        thread_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """With a key, the same request again replays the message that landed (`replayed: true`, the
        same ids) instead of sending a second; another request under the key is `key_conflict`."""
        args = [f"to={lit(to)}", f"content={lit(content)}"]
        if subject:
            args.append(f"subject={lit(subject)}")
        if thread_id:
            args.append(f"thread_id={lit(thread_id)}")
        if idempotency_key:
            args.append(f"idempotency_key={lit(idempotency_key)}")
        (sent,) = await self.exec(f"comm.send({', '.join(args)})")
        return sent
