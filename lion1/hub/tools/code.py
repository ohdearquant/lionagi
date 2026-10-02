"""The coding tools over a tree: lines of a file by number, a search, an edit that matches once.
`bash` and `python` (hub.tools.shell) run the tests; these three are what a fix is found and written
with. They work through a `Tree`: `LocalTree` is a directory on this machine (the person's real
tree, so an edit is a write there); a tree inside a remote box implements the same three calls."""

from __future__ import annotations

import asyncio
import difflib
import re
import shutil
import subprocess
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from lionagi import Actor, Context

__all__ = ("ReadLines", "Search", "Edit", "Tree", "LocalTree", "numbered", "reend", "replace_once", "code")


class ReadLines(BaseModel):
    """A file by line number: `start` to `end` inclusive, numbered, with the file's length. Whole
    files are `read`; this is for the span a search pointed at."""

    path: str = Field(description="absolute, or relative to the console's directory")
    start: int = Field(default=1, ge=1)
    end: int | None = Field(default=None, description="inclusive; the end of the file when omitted")


class Search(BaseModel):
    """Lines matching a regular expression under a path: `file:line:text`, every match, ignored
    files skipped (.gitignore); `(no matches)` when none."""

    pattern: str = Field(description="a regular expression (rg / grep -E syntax)")
    path: str = Field(default=".", description="a file or directory, relative to the console's directory")
    include: str | None = Field(default=None, description="a file glob to search within, e.g. `*.py`")


class Edit(BaseModel):
    """Replace `old` with `new` in a file: `old` must occur exactly once, verbatim, so the edit
    lands where it was read; an ambiguous or absent `old` is refused and the file untouched."""

    path: str = Field(description="absolute, or relative to the console's directory")
    old: str = Field(min_length=1, description="the exact text to replace, as read_lines showed it")
    new: str = Field(description="what replaces it; empty deletes")


class Tree(Protocol):
    """Where the code is. `search` returns the tool's `file:line:text` lines, empty for no match,
    and raises on a bad pattern."""

    async def read(self, path: str) -> str: ...
    async def write(self, path: str, text: str) -> None: ...
    async def search(self, pattern: str, path: str, include: str | None) -> str: ...
    async def list_dir(self, path: str) -> list[str]: ...  # names, sorted, `/` after a directory


class LocalTree:
    """A directory on this machine and nothing outside it (a `..`, an absolute path or a symlink that
    leaves it is refused); `rg` when installed (it honours .gitignore), else `grep -rE`. A file is
    read and written with its bytes' own line endings, so an edit changes the span and nothing else."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.rg = shutil.which("rg")

    def inside(self, path: str) -> Path:
        target = (self.root / path).resolve()
        if target != self.root and self.root not in target.parents:
            raise PermissionError(f"{path} is outside {self.root}")
        return target

    async def read(self, path: str) -> str:
        with self.inside(path).open(newline="") as f:
            return f.read()

    async def write(self, path: str, text: str) -> None:
        with self.inside(path).open("w", newline="") as f:
            f.write(text)

    async def list_dir(self, path: str) -> list[str]:
        return sorted(p.name + ("/" if p.is_dir() else "") for p in self.inside(path).iterdir())

    async def search(self, pattern: str, path: str, include: str | None) -> str:
        self.inside(path)
        if self.rg:  # hidden files are part of the tree (.github/ci.yml); .git is not
            argv = [
                self.rg,
                "-n",
                "--no-heading",
                "--color",
                "never",
                "--hidden",
                "-H",  # a single-file path drops the filename column without this
                "-g",
                "!.git",
                "-e",
                pattern,
            ]
            if include:
                argv += ["-g", include]
        else:
            argv = ["grep", "-rnIEH", "--exclude-dir=.git", "-e", pattern]
            if include:
                argv += [f"--include={include}"]
        argv += ["--", path]
        done = await asyncio.to_thread(subprocess.run, argv, capture_output=True, text=True, cwd=self.root)
        if done.returncode > 1:  # 1 is "no matches" for both tools; 2 is a bad pattern or path
            raise ValueError(done.stderr.strip() or f"search exited {done.returncode}")
        return "".join(ln.removeprefix("./") + "\n" for ln in done.stdout.splitlines())  # `.` prints `./x`


def physical(text: str) -> list[str]:
    """`text` as the lines an editor shows, each with its own ending: a line ends at `\n` only, so
    U+2028 or NEL inside one is text (str.splitlines would break the line there, and the numbering
    and the ending each line keeps would disagree with what an edit matches against)."""
    return [line for line in re.split(r"(?<=\n)", text) if line]


def unended(line: str) -> str:
    """A physical line without its ending, which is one CRLF, else one LF, else none: a CR anywhere
    else, the last line's final one included, is text, read as it is and kept by an edit."""
    return line[:-2] if line.endswith("\r\n") else line[:-1] if line.endswith("\n") else line


def numbered(text: str, start: int = 1, end: int | None = None) -> tuple[str, int, int, int]:
    """(body, start, end, total): lines `start..end` as `n: line`, clipped to the file."""
    lines = [unended(line) for line in physical(text)]
    total = len(lines)
    start = max(start, 1)
    end = min(end, total) if end is not None else total
    body = "\n".join(f"{i}: {line}" for i, line in enumerate(lines[start - 1 : end], start))
    return body, start, end, total


def reend(before: str, after: str) -> str:
    """`after` (matched and edited with `\n` endings) with each line's ending as `before` had it:
    an unchanged line keeps its own, a changed line keeps the ending of the line it replaced, a new
    line takes the ending of the line before it (the file's first line's, for a new first line). A
    file with mixed endings stays mixed."""
    old = physical(before)
    bodies = [unended(line) for line in old]
    ends = [line[len(body) :] for line, body in zip(old, bodies, strict=True)]
    new = after.split("\n")
    final = new[-1] == ""  # the text ends with a newline
    if final:
        new.pop()
    default = ends[0] if ends and ends[0] else "\n"
    out: list[str] = []
    last = default
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, bodies, new, autojunk=False).get_opcodes():
        if op == "equal":
            for k in range(i1, i2):
                last = ends[k] or last
                out.append(bodies[k] + (ends[k] or last))
        elif op == "replace":
            for n, k in enumerate(range(j1, j2)):
                last = ends[i1 + min(n, i2 - i1 - 1)] or last
                out.append(new[k] + last)
        elif op == "insert":
            for k in range(j1, j2):
                out.append(new[k] + last)
    if not final:  # its last line has no ending, so it drops the one given it, never a CR of its own
        out[-1] = new[-1]
    return "".join(out)


def replace_once(text: str, old: str, new: str) -> str:
    """`text` with its one occurrence of `old` replaced; ValueError names the count otherwise."""
    n = text.count(old)
    if n != 1:
        raise ValueError(f"`old` occurs {n} times; it must occur exactly once (widen it with a line around)")
    return text.replace(old, new, 1)


def code(actor: Actor, tree: Tree) -> set[str]:
    """Register `read_lines`, `search` and `edit` over `tree`."""
    edits = asyncio.Lock()  # an edit is read-replace-write: two on one file in one round serialize

    @actor.handler(ReadLines)
    async def read_lines(req: ReadLines, ctx: Context) -> str:
        body, start, end, total = numbered(await tree.read(req.path), req.start, req.end)
        if total == 0:  # numbered's (1, 0, 0) is a range naming no line; say what it is instead
            return f"{req.path} is empty"
        return f"{req.path} lines {start}-{end} of {total}\n{body}"

    @actor.handler(Search)
    async def search(req: Search, ctx: Context) -> str:
        out = await tree.search(req.pattern, req.path, req.include)
        return out if out.strip() else "(no matches)"

    @actor.handler(Edit)
    async def edit(req: Edit, ctx: Context) -> str:
        async with edits:
            before = await tree.read(req.path)
            crlf = "\r\n" in before  # matched as read_lines showed it; written with each line's own ending
            after = replace_once(before.replace("\r\n", "\n") if crlf else before, req.old, req.new)
            await tree.write(req.path, reend(before, after) if crlf else after)
        return f"edited {req.path}: {before.count(chr(10))} -> {after.count(chr(10))} lines"

    return {"read_lines", "search", "edit"}
