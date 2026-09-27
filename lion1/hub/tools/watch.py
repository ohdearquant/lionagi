"""The repository in a box, as the round notice reports it: which files the change touches and since
when it has stood still, and the model's own acceptance checks. `Watch.section` is an actor section;
`criteria(actor, watch)` registers the `criteria` command; `accept` is the gate on OUT{}.

Files under tests are named apart (a grader replaces them with its own copy, so scaffolding there is
never the fix); so are new files outside the tracked tree (a repro script at the root, a throwaway
package beside the source), which are scaffolding and start no clock: counted as the fix, they
settled a run that was still investigating at a fifth of its time budget. Past `notice_after`
unchanged rounds the line asks for OUT{} or a named gap; past `settle_after` the run stops and the
change is taken as it stands. On every resolved bench run before this the source was edited exactly
once and every round after it was verification, five to twenty of them; the model has no stopping
rule of its own."""

from __future__ import annotations

import asyncio
import hashlib
import re
import shlex
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from lionagi import Actor, Context

from .box import Box

__all__ = (
    "Criteria",
    "by_file",
    "is_new",
    "is_test",
    "checks_line",
    "digest_of",
    "Watch",
    "criteria",
    "VERIFIERS",
    "accept",
)


class Criteria(BaseModel):
    """Declare the acceptance checks before editing: shell commands that fail now and must all exit 0
    once the issue is fixed, one per symptom the issue names (a reproduction script, the tests the
    issue points at). The loop runs them after every change and ends the run when they all pass;
    declaring again replaces them. Checks that already pass are refused: they do not test the issue.
    """

    cmds: list[str] = Field(description="each must exit 0 when the issue is fixed")
    timeout: int = Field(default=300, description="seconds per command")


_QUOTED_HEADER = re.compile(r'^"((?:[^"\\]|\\.)*)" "((?:[^"\\]|\\.)*)"$')
_C_ESCAPES = {
    "n": "\n",
    "t": "\t",
    '"': '"',
    "\\": "\\",
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "r": "\r",
    "v": "\v",
}


def _unquote_git_path(body: str) -> str:
    """A git-quoted path's inner text (`core.quotePath`, on by default): octal-escaped bytes and
    `\\" \\\\ \\t \\n` undone back to the raw path, byte by byte so a multi-byte UTF-8 char split
    across consecutive `\\NNN` escapes decodes whole."""
    out = bytearray()
    i = 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in "01234567":
                out.append(int(body[i + 1 : i + 4], 8))
                i += 4
                continue
            out.extend(_C_ESCAPES.get(nxt, nxt).encode())
            i += 2
            continue
        out.extend(c.encode())
        i += 1
    return out.decode("utf-8", "surrogateescape")


def by_file(diff: str) -> dict[str, str]:
    """Each file's hunk text by path, from the concatenated `git diff` output."""
    out: dict[str, str] = {}
    for chunk in re.split(r"^(?=diff --git )", diff, flags=re.M):
        if not chunk.strip():
            continue
        head = chunk.splitlines()[0].removeprefix("diff --git ")
        quoted = _QUOTED_HEADER.match(head)
        if quoted:
            # `"a/<path>" "b/<path>"`: git quotes both sides together once either needs it
            path = _unquote_git_path(quoted.group(2)).removeprefix("b/")
        else:
            # `a/<path> b/<path>`; the b side is the path as it stands now
            path = head.split(" b/", 1)[1] if " b/" in head else head
        out[path] = out.get(path, "") + chunk
    return out


def is_new(chunk: str) -> bool:
    """A file the repository did not have: its hunk diffs against /dev/null."""
    return "\n--- /dev/null\n" in chunk


def is_test(path: str) -> bool:
    """The default test classifier: under a `tests` directory, a conftest, or named like a test."""
    parts = path.split("/")
    name = parts[-1]
    if "tests" in parts[:-1] or name == "conftest.py":
        return True
    return name.startswith("test_") or name.endswith("_test.py")


def checks_line(results: list[tuple[str, int]]) -> str:
    return "; ".join(f"`{cmd[:80]}` exit {rc}" for cmd, rc in results)


def digest_of(chunks: dict[str, str], source: list[str]) -> str:
    """The source change as one short digest: the same edit standing is the same digest, whatever
    order the paths came in (the diff lists tracked files before new ones; the section sorts)."""
    return hashlib.blake2b("".join(chunks[p] for p in sorted(source)).encode(), digest_size=8).hexdigest()


class Watch:
    """The change in `box` as it stands, round by round. `test` says which paths are tests."""

    def __init__(
        self,
        box: Box,
        *,
        notice_after: int = 3,
        settle_after: int = 0,
        test: Callable[[str], bool] = is_test,
    ):
        self.box = box
        self.notice_after = notice_after
        self.settle_after = settle_after  # 0: never stop
        self.is_test = test
        self.digest: str | None = None
        self.since = 0  # the round whose edits produced the current change
        self.stable = 0  # rounds the change has now stood unchanged
        self.source: list[str] = []
        self.tests: list[str] = []
        self.scratch: list[str] = []  # new files outside the tracked tree
        self._tracked: dict[str, bool] = {}  # top-level directory -> the repository tracks files there
        self.criteria: list[str] | None = None  # the model's acceptance checks, commands that must exit 0
        self.criteria_timeout = 300
        self.checks: list[tuple[str, int]] = []  # (cmd, exit code) from the last run of the checks
        self.checked: str | None = None  # the source digest the checks were last run against
        self.passed = False
        self._lock = asyncio.Lock()

    async def declare(self, cmds: list[str], timeout: int) -> str:
        """The acceptance checks, run once on the tree as it stands; all passing means they test nothing.
        Two declarations in one round run concurrently, so the state moves only once the verdict is in,
        and a refused set leaves whatever stands untouched.
        """
        if not cmds:
            return "refused: no checks given"
        async with self._lock:
            results = await self._run(list(cmds), timeout)
            if all(rc == 0 for _, rc in results):
                return (
                    f"refused: all {len(cmds)} checks already exit 0 on the current tree, so they do not "
                    "test the issue; declare checks that fail until it is fixed"
                )
            self.criteria, self.criteria_timeout = list(cmds), timeout
            self.checks, self.checked, self.passed = results, self.digest, False
        return f"declared {len(cmds)} acceptance checks; on the current tree: " + checks_line(results)

    async def check(self) -> list[tuple[str, int]]:
        async with self._lock:
            results = await self._run(self.criteria or [], self.criteria_timeout)
            self.checks, self.checked = results, self.digest
            self.passed = bool(results) and all(rc == 0 for _, rc in results)
        return results

    async def _run(self, cmds: list[str], timeout: int) -> list[tuple[str, int]]:
        results = []
        for cmd in cmds:
            rc, _ = await self.box.run(cmd, timeout=timeout)
            results.append((cmd, rc))
        return results

    async def _in_tree(self, path: str) -> bool:
        """A new file is source only inside a top-level directory the repository already tracks."""
        if "/" not in path:
            return False
        top = path.split("/", 1)[0]
        if top not in self._tracked:
            _, out = await self.box.run(f"git ls-files -- {shlex.quote(top)} | head -1")
            self._tracked[top] = bool(out.strip())
        return self._tracked[top]

    async def classify(self, chunks: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
        """(tests, scratch, source) of a diff's files: the same reading for the section and the gate."""
        tests = sorted(p for p in chunks if self.is_test(p))
        scratch = []
        for p in chunks:
            if not self.is_test(p) and is_new(chunks[p]) and not await self._in_tree(p):
                scratch.append(p)
        source = sorted(p for p in chunks if not self.is_test(p) and p not in scratch)
        return tests, scratch, source

    async def section(self, run: Any) -> str | None:
        chunks = by_file(await self.box.diff())
        self.tests, self.scratch, self.source = await self.classify(chunks)
        aside = []
        if self.tests:
            aside.append(f"{', '.join(self.tests)} changed under tests, not part of the fix")
        if self.scratch:
            aside.append(
                f"{', '.join(self.scratch)} are new files outside the tracked tree, not part of the fix"
            )
        if not self.source:
            self.digest, self.stable = None, 0
            if aside:
                return "repository: no source change yet; " + "; ".join(aside)
            return None
        digest = digest_of(chunks, self.source)
        if digest != self.digest:
            # run.round - 1: the edit happened the round before this one observed it; at round 1
            # there is no round 0 to attribute it to, so the first sighting names round 1 itself
            self.digest, self.since, self.stable = digest, max(run.round - 1, 1), 0
        else:
            self.stable += 1
        line = f"repository: the fix touches {', '.join(self.source)}, unchanged since round {self.since}"
        for a in aside:
            line += "; " + a
        if self.criteria and self.checked != digest:
            await self.check()
        if self.criteria:
            n = sum(rc == 0 for _, rc in self.checks)
            line += f"; acceptance {n}/{len(self.checks)} pass: " + checks_line(self.checks)
            if self.passed:
                return line + "; all acceptance checks pass, the run ends here"
        if self.stable >= self.notice_after:
            line += (
                f"; it has stood for {self.stable} rounds: if it is verified, finish with OUT{{}} now; "
                "if not, write what is unverified to note.gap and check exactly that"
            )
        if self.settle_after and self.stable + 1 >= self.settle_after:
            line += "; the run ends after this round if the fix still does not change"
        return line

    def stop(self) -> bool:
        if self.passed:
            return True
        return bool(self.settle_after) and bool(self.source) and self.stable >= self.settle_after

    def state(self) -> dict:
        return {
            "source": self.source,
            "tests": self.tests,
            "scratch": self.scratch,
            "since": self.since,
            "stable": self.stable,
            "criteria": self.criteria,
            "checks": self.checks,
            "passed": self.passed,
        }


def criteria(actor: Actor, watch: Watch) -> set[str]:
    """Register `criteria` on the actor: the run then ends on the model's own checks, not on its word."""

    @actor.handler(Criteria)
    async def declare(req: Criteria, ctx: Context) -> str:
        return await watch.declare(req.cmds, req.timeout)

    return {"criteria"}


VERIFIERS = frozenset({"bash", "python"})  # a result of one of these is a command run on the tree


def _ran(entry: Any) -> bool:
    """A RESULT of a verifier that reached its handler: a command the actor refused (unknown, invalid
    arguments, a hook) or one that raised never ran, whatever its name; a non-zero rc did run."""
    kind = getattr(entry, "kind", None)
    content = getattr(entry, "content", None)
    return (
        getattr(kind, "name", "") == "RESULT"
        and getattr(content, "spec", None) in VERIFIERS
        and bool(getattr(content, "ok", False))
    )


async def accept(watch: Watch, run: Any, output: Any) -> str | None:
    """The gate on OUT{}: an unchanged repository is not done; a change nothing ran against since is
    not verified (an edit and the command that checks it are two rounds: in one round they run
    together, so the command saw the tree before the edit); a failing declared check is not done."""
    diff = await watch.box.diff()
    if not diff.strip():
        return "the repository has no changes; you declared done without editing anything"
    chunks = by_file(diff)
    tests, scratch, source = await watch.classify(
        chunks
    )  # read afresh: a scratch file of this round is scratch
    if not source:
        aside = []
        if tests:
            aside.append(f"under tests: {', '.join(tests)}")
        if scratch:
            aside.append(f"new outside the tracked tree: {', '.join(scratch)}")
        return (
            f"the change is {'; '.join(aside)}, and nothing else: a fix edits the source the tests "
            "exercise; edit it, run the nearest tests, then finish"
        )
    if digest_of(chunks, source) != watch.digest:
        return (
            "the source changed in this round; nothing has run against that change yet, so it is not "
            "verified: run the nearest tests in the next round, then finish"
        )
    ran_after = any(_ran(e) and e.round > watch.since for e in getattr(run, "record", ()))
    if not ran_after:
        return (
            f"no command has run since the source changed in round {watch.since}, so the change is not "
            "verified: run the nearest tests first, then finish"
        )
    if watch.criteria:
        results = await watch.check()
        if not watch.passed:
            failing = [(c, rc) for c, rc in results if rc != 0]
            return "your own acceptance checks fail: " + checks_line(failing)
    return None
