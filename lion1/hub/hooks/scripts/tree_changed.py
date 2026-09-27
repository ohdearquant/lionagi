"""A round hook: name the tracked files whose content changed since the previous round of this run,
and HEAD when it moved. stdin: {"event": "round", "run": ..., "round": ...}; stdout is the notice.
Run as `python -m hub.hooks.scripts.tree_changed` from a `hooks.toml`.

The tree moves under a long conversation: a model that reads a green test as its own misread is
reading a tree someone else edited. State lives under `.lion/tree_state/`, one file per run id, so
two runs (two chats) never compare against each other's round, and two hooks running at once never
write one file.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

__all__ = ("STATE", "IGNORED", "state_path", "snapshot", "notice", "main")

STATE = Path(".lion/tree_state")
IGNORED = (".lion/",)


def state_path(run: str) -> Path:
    """The run's own state file under STATE, named by a digest of the run id (any string is safe)."""
    return STATE / (hashlib.blake2b(run.encode(), digest_size=8).hexdigest() + ".json")


def _git(cwd: Path, *args: str) -> str:
    """A git line's stdout, empty when git refuses (not a work tree, no HEAD yet)."""
    done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    return done.stdout if done.returncode == 0 else ""


def snapshot(cwd: Path) -> tuple[str, dict[str, str]] | None:
    """(HEAD sha, path -> blake2b digest) over what a reader could see: tracked files present in the
    work tree plus untracked-but-present ones, `.lion/` out. None outside a git work tree."""
    head = _git(cwd, "rev-parse", "HEAD").strip()
    if not head:
        return None
    listed = _git(cwd, "ls-files", "-z").split("\0")
    untracked = _git(cwd, "ls-files", "--others", "--exclude-standard", "-z").split("\0")  # per file
    files: dict[str, str] = {}
    for rel in listed + untracked:
        if not rel or rel.startswith(IGNORED):
            continue
        f = cwd / rel
        if f.is_file():
            files[rel] = hashlib.blake2b(f.read_bytes(), digest_size=16).hexdigest()
    return head, files


def notice(prev: dict, head: str, files: dict[str, str]) -> list[str]:
    """The lines this round adds: the files whose content differs from the last round, then HEAD when
    it moved. A run with no previous round is compared against nothing."""
    was = prev.get("files", {})
    changed = sorted(p for p in set(was) | set(files) if was.get(p) != files.get(p))
    lines: list[str] = []
    if changed:
        named = ", ".join(changed)
        lines.append(f"tree: {len(changed)} file(s) changed since round {prev.get('round')}: {named}")
    old = str(prev.get("head", ""))
    if old != head:
        lines.append(f"tree: HEAD moved {old[:7] or '?'} -> {head[:7]}")
    return lines


def main() -> int:
    payload = json.load(sys.stdin)
    if payload.get("event") != "round":
        return 0
    run = str(payload.get("run", ""))
    cwd = Path.cwd()
    now = snapshot(cwd) if run else None
    if now is None:
        return 0
    head, files = now
    path = cwd / state_path(run)
    try:
        prev = json.loads(path.read_text()) if path.is_file() else None
    except ValueError:  # a write cut short: start over rather than fail every later round
        prev = None
    for line in notice(prev, head, files) if isinstance(prev, dict) else []:
        print(line)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")  # this process's own: no other hook replaces it
    tmp.write_text(json.dumps({"round": payload.get("round", 0), "head": head, "files": files}, indent=1))
    tmp.replace(path)  # whole or absent, never cut
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
