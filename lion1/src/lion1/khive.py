"""khive from a run: the actor's address, mailbox and store, one `kkernel exec` per call.

The actor has the address (ADR-0001); khive resolves it from the cwd's `.khive/config.toml`, or from
the file `--config` names, which wins over the cwd's (measured: from a directory whose own config
names another actor, the file's actor resolves and `--expect-actor <the cwd's>` is refused). So a
Khive is bound to a directory, or to a config file, and asserts on every call that it resolves to
the actor it was built for (`--expect-actor`). Ops are the verb DSL the MCP `request` tool takes;
string arguments are JSON literals, so they go through `json.dumps`. The binary reads the file a
`Store` checked (ADR-0026 C8) and no other: `--config` names `config`, made absolute from the directory
it was named in since the binary runs in `cwd`, or else the cwd's `.khive/config.toml`, which khive's
own discovery passes over for a `khive.toml` beside it; and the binary starts without `KHIVE_CONFIG`
and `KHIVE_DB`, either of which would choose another config or another database.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import shutil
import signal
import string
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = (
    "KhiveError",
    "LOST",
    "UNSTARTED",
    "ARGUMENT_BYTES",
    "lit",
    "actor_name",
    "Khive",
)


class KhiveError(Exception):
    """A khive call that failed. `kind` is the kind the store gave the first failed op's error, as
    "not_found" for an id it does not hold; None when the call failed before the store answered or
    the answer named no kind."""

    def __init__(self, message: str, kind: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind


LOST = "daemon response lost after request was sent"  # kkernel dropping a READ's answer; see inbox
UNSTARTED = "kkernel could not start"  # the launch itself failed, so nothing ran

# The most bytes one argument can hold on Linux: MAX_ARG_STRLEN, 32 pages of the smallest page size,
# its closing NUL included. macOS bounds only the total of the arguments and the environment, which is
# larger. An ops string is one argument, so a caller batching ops keeps each call's string within this.
ARGUMENT_BYTES = 32 * 4096 - 1


def _kind(bad: list[dict]) -> str | None:
    error = bad[0].get("error") if bad else None
    kind = error.get("kind") if isinstance(error, dict) else None
    return kind if isinstance(kind, str) else None


def lit(value: Any) -> str:
    """A DSL argument: strings, numbers, booleans, null, arrays and objects are JSON literals."""
    return json.dumps(value)


# a letter or digit, then letters, digits and `. _ - : @`, 3 to 100 in all; ASCII only, so no space,
# control, invisible or look-alike character; `:` and `@` keep the names khive gives mail (`email:a@b.c`)
_NAME_FIRST = frozenset(string.ascii_letters + string.digits)
_NAME_REST = _NAME_FIRST | frozenset("._-:@")


def _name_rule(value: str) -> str | None:
    """The first rule `value` breaks, in words, or None when it is a name."""
    if not 3 <= len(value) <= 100:
        return f"length {len(value)}: a name is 3 to 100 characters"
    for i, c in enumerate(value):
        if not c.isascii():
            return f"character {i + 1} is not ASCII"
        if i == 0 and c not in _NAME_FIRST:
            return "the first character is not a letter or digit"
        if c not in _NAME_REST:
            return f"character {i + 1} is not a letter, a digit or one of . _ - : @"
    return None


def actor_name(value: Any, where: str = "actor") -> str:
    """`value` when it is an actor's name, unchanged; any other value raises `ValueError` naming
    `where` and the rule it breaks. A name is a plain string compared as written, with no prefix of
    any kind. The refusal shows a long value by its first 6 characters and its length only, since a
    value refused here may be a secret pasted into the wrong field."""
    if not isinstance(value, str):
        raise ValueError(f"{where}: a {type(value).__name__} is not an actor name: a name is a string")
    rule = _name_rule(value)
    if rule is not None:
        shown = f"{value[:6]!r}…[{len(value)} chars]" if len(value) > 6 else repr(value)
        raise ValueError(f"{where}: {shown} is not an actor name: {rule}")
    return value


@dataclass
class Khive:
    cwd: Path  # the directory whose .khive/config.toml names the actor, and where kkernel runs
    actor: str  # what that directory (or `config`) must resolve to; every call asserts it
    kkernel: str | None = None
    timeout: float = 60.0
    calls: list[dict] = field(default_factory=list)
    config: Path | None = None  # an explicit khive config: passed as --config, over the cwd's own

    def __post_init__(self) -> None:
        actor_name(self.actor, "khive actor")
        self.cwd = Path(self.cwd)
        self.kkernel = self.kkernel or shutil.which("kkernel") or str(Path.home() / ".cargo/bin/kkernel")
        if not Path(self.kkernel).exists():
            raise KhiveError("kkernel not found on PATH or under ~/.cargo/bin")
        if self.config is not None:
            self.config = Path(self.config).absolute()
            if not self.config.is_file():  # explicit means explicit: never the cwd's instead
                raise KhiveError(f"{self.config}: no such khive config file")
        elif not (self.cwd / ".khive" / "config.toml").exists():
            raise KhiveError(f"{self.cwd} holds no .khive/config.toml; khive would resolve it as 'local'")

    async def exec(self, ops: str) -> list[Any]:
        """Run one DSL expression; each op's `result`, in order. An op that is not ok raises."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._argv(ops),
                cwd=str(self.cwd),
                env=self._env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # the CLI's whole process group: a kill ends its children too
            )
        except OSError as e:
            raise self._unstarted(ops, e) from None
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
        return self._answer(ops, proc.returncode, out, err)

    def exec_sync(self, ops: str) -> list[Any]:
        """`exec` for a caller that is not awaiting: the note store's five methods (ADR-0025 D6), each
        one call of the binary, blocking the loop for its length. An async store is a later change."""
        try:
            proc = subprocess.Popen(
                self._argv(ops),
                cwd=str(self.cwd),
                env=self._env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as e:
            raise self._unstarted(ops, e) from None
        # Only the launch is a failed start: once kkernel runs it may have applied the ops, so a fault
        # reading its answer propagates as it is. The rest is subprocess.run's own handling.
        with proc:
            try:
                out, err = proc.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise KhiveError(f"kkernel did not answer within {self.timeout}s: {ops[:120]}") from None
            except BaseException:
                proc.kill()
                raise
        return self._answer(ops, proc.returncode, out, err)

    def _unstarted(self, ops: str, e: OSError) -> KhiveError:
        # kkernel never started, so nothing ran. The ops string travels as one argument, and the OS
        # refuses a launch whose arguments and environment are too large (E2BIG) before the store's own
        # cap is reached: by their total, and on Linux also by any one string past 128 KiB. The error
        # does not say which bound, and either can be the large one, so both sizes are named.
        if e.errno == errno.E2BIG:
            env = sum(len(os.fsencode(k)) + len(os.fsencode(v)) + 2 for k, v in self._env().items())
            return KhiveError(
                f"{UNSTARTED}: the operating system refused its arguments and environment as too "
                f"large (E2BIG; the ops string is {len(os.fsencode(ops))} bytes, the environment "
                f"{env} bytes)"
            )
        return KhiveError(f"{UNSTARTED}: {type(e).__name__}: {e}")

    def _env(self) -> dict[str, str]:
        return {k: v for k, v in os.environ.items() if k not in ("KHIVE_CONFIG", "KHIVE_DB")}

    def _argv(self, ops: str) -> list[str]:
        # after `--`: an ops string that begins with `-` is the DSL, never an option
        config = self.config or (self.cwd / ".khive" / "config.toml").absolute()
        return [
            self.kkernel,
            "exec",
            "--strict",
            "--expect-actor",
            self.actor,
            "--config",
            str(config),
            "--",
            ops,
        ]

    def _answer(self, ops: str, returncode: int | None, out: bytes, err: bytes) -> list[Any]:
        # the CLI prints a config warning and its database line on every call; keep the rest
        noise = (" WARN ", "database:")
        detail = [ln for ln in err.decode(errors="replace").splitlines() if not any(n in ln for n in noise)]
        try:
            data = json.loads(out.decode()) if out.strip() else {}
        except json.JSONDecodeError as e:
            raise KhiveError(f"unparseable kkernel output: {e}: {out[:300]!r}") from None
        results = data.get("results") or []
        bad = [r for r in results if not r.get("ok")]
        if returncode != 0:
            # the op's own error is on stdout; stderr carries the refusal class and the actor line
            said = "; ".join(f"{r.get('tool')}: {str(r.get('error') or r)[:300]}" for r in bad)
            said = said or " | ".join(detail).strip() or out.decode(errors="replace").strip()
            raise KhiveError(f"kkernel exited {returncode}: {said[-600:]}", kind=_kind(bad))
        self.calls.append({"ops": ops, "status": data.get("status"), "summary": data.get("summary")})
        if bad:
            raise KhiveError(
                f"{bad[0].get('tool')}: {str(bad[0].get('error') or bad[0])[:500]}", kind=_kind(bad)
            )
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
            mail = page.get("messages", [])  # the store leaves the field out of an empty page
            if not isinstance(mail, list):  # anything else, null included, cannot be trusted as empty
                raise KhiveError(f"comm.inbox answered {type(mail).__name__} for messages, not a list")
            remaining -= slice_ms
            if mail or remaining <= 0:
                return mail

    # `send` is a method because the agent's boundary (`Bounded`) checks its recipient in code before
    # the call; every other verb, the mark-read included, is written at its call site as the DSL
    # (ADR-0025 D7)
    async def send(
        self,
        to: str,
        content: str,
        *,
        subject: str | None = None,
        thread_id: str | None = None,
        idempotency_key: str | None = None,
        self_send: bool = False,
    ) -> dict:
        """With a key, the same request again replays the message that landed (`replayed: true`, the
        same ids) instead of sending a second; another request under the key is `key_conflict`. comm
        refuses a message to the sender's own actor unless `self_send` says it is meant."""
        args = [f"to={lit(to)}", f"content={lit(content)}"]
        if subject:
            args.append(f"subject={lit(subject)}")
        if thread_id:
            args.append(f"thread_id={lit(thread_id)}")
        if idempotency_key:
            args.append(f"idempotency_key={lit(idempotency_key)}")
        if self_send:
            args.append("self_send=true")
        (sent,) = await self.exec(f"comm.send({', '.join(args)})")
        return sent
