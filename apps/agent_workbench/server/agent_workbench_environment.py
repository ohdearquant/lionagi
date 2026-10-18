# SPDX-License-Identifier: Apache-2.0
"""Agent Workbench: tool-using tasks across eight work domains.

Each episode is one task: a few files in a private workspace, a question or a bug to fix, and a
small fixed tool set. The episode ends when the agent submits, runs out of tool calls, makes
three invalid calls in a row, or the trainer ends it. Only the final observation carries a reward,
and an episode that ends without a submitted answer earns 0.

Reward (0..1) = 0.7 x correctness + 0.1 x read a file or ran code before submitting
              + 0.1 x verified what it submitted (check confirmed that exact answer, or, for code
                      tasks, the tests passed after its last change)
              + 0.1 x made no invalid tool calls.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

from openenv.core.env_server.interfaces import Environment
from openenv.core.env_server.types import State

try:
    from ..models import WorkbenchAction, WorkbenchObservation
    from .tasks import TASKS, BuiltTask, build_task, score_answer
except ImportError:
    from models import WorkbenchAction, WorkbenchObservation
    from server.tasks import TASKS, BuiltTask, build_task, score_answer

MAX_STEPS = 20
MAX_INVALID_IN_A_ROW = 3
MAX_CHECKS = 2
OUTPUT_CHARS = 1500
READ_LINES = 60
PY_TIMEOUT_S = 10
WRITE_CHARS = 20_000
SANDBOX_PYTHON = os.environ.get("WORKBENCH_SANDBOX_PYTHON") or shutil.which("python3") or "python3"

TOOLS_TEXT = (
    'Reply with exactly one JSON object per turn: {"tool": <name>, "args": {...}}.\n'
    "list_files {} - list the files in your workspace.\n"
    'read_file {"path": str, "start": int (optional, 1-based line), "end": int (optional)} - '
    f"show up to {READ_LINES} numbered lines.\n"
    'run_python {"code": str} - run a Python 3 script in your workspace (cwd) and show its output; '
    f"{PY_TIMEOUT_S} s limit, no network needed.\n"
    'write_file {"path": str, "content": str} - create or replace a file in your workspace.\n'
    "run_tests {} - code tasks only: run the examples of test_solution.py as given.\n"
    f'check {{"answer": str}} - ask whether an answer is correct before you submit (at most {MAX_CHECKS} times).\n'
    'submit {"answer": str} - give your final answer and end the episode.\n'
    f"You have {MAX_STEPS} tool calls. Read the inputs before answering, and verify before you submit."
)

# Inside the image the server runs as root and gives each episode its own unprivileged uid, so
# concurrent episodes cannot read each other's workspaces or kill each other's processes.
_SANDBOX_UID_BASE = 20000
_SANDBOX_UID_SLOTS = 1024
_uids_in_use: set[int] = set()
_uids_lock = threading.Lock()
_next_uid_slot = 0
# World-writable places agent code can leave files in outside its workspace.
_SHARED_TMP_DIRS = ("/tmp", "/var/tmp", "/dev/shm")  # noqa: S108
# The files the server itself writes into a workspace, to run the agent's code and to grade it.
_SERVER_FILES = (".run.py", ".grade.py", ".grade_tests.py")
_SERVER_DIRS = tuple(f"{name}/" for name in _SERVER_FILES)
# A workspace holding more than this is not hashed, so a run on it cannot be shown to change nothing.
_DIGEST_BYTES = 64 * 1024 * 1024
# Directories a walk holds open at once; a deeper tree is moved up to be removed (see _remove_entries).
_WALK_DEPTH = 128
_MAX_PASSES = 1 << 16


def _claim_sandbox_uid() -> int | None:
    """A uid no other live episode holds, or None when the server is not root (no sandbox).

    Slots are handed out in rotation, so a uid an episode just released is the last to be reused.
    """
    global _next_uid_slot
    if os.geteuid() != 0:
        return None
    with _uids_lock:
        for step in range(_SANDBOX_UID_SLOTS):
            uid = _SANDBOX_UID_BASE + (_next_uid_slot + step) % _SANDBOX_UID_SLOTS
            if uid not in _uids_in_use:
                _uids_in_use.add(uid)
                _next_uid_slot = (uid - _SANDBOX_UID_BASE + 1) % _SANDBOX_UID_SLOTS
                return uid
    raise RuntimeError("no free sandbox uid: too many live episodes")


def _release_sandbox_uid(uid: int | None) -> None:
    if uid is not None:
        with _uids_lock:
            _uids_in_use.discard(uid)


def _walk_tree(
    top: str | Path,
    descend: Callable[[int, str, str, os.stat_result], bool],
    *,
    strict: bool = False,
) -> Iterator[tuple[int, str, str, os.stat_result, bool]]:
    """Every entry under the directory *top*, depth first, as ``(dir_fd, name, rel, st, left)``.

    Each entry is yielded once with *left* False, read by ``lstat`` through the open descriptor
    *dir_fd* of the directory holding it; *rel* is its path below *top*. A directory *descend*
    accepts (and may change the mode of first) is then entered, and yielded again with *left* True
    once everything under it was. Nothing follows a symlink, and the walk keeps its own stack, so
    neither a path longer than PATH_MAX nor a tree deeper than the interpreter's recursion limit
    stops it; agent code can build either in a second. At most _WALK_DEPTH directories are open at
    once, so a directory that deep is yielded but not entered. What cannot be read is skipped, or
    raises when *strict*, a directory too deep to enter included.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
    # One frame per open directory: (fd, depth, its parent's fd, name, rel, stat, names left).
    stack: list[tuple[int, int, int, str, str, os.stat_result | None, Iterator[str]]] = []

    def enter(fd: int, depth: int, parent_fd: int, name: str, rel: str, st: Any) -> None:
        try:
            names = sorted(os.listdir(fd))
        except OSError:
            if strict:
                os.close(fd)
                raise
            names = []
        stack.append((fd, depth, parent_fd, name, rel, st, iter(names)))

    try:
        top_fd = os.open(top, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        if strict:
            raise
        return
    try:
        enter(top_fd, 0, -1, "", "", None)
        while stack:
            fd, depth, parent_fd, name, rel, st, names = stack[-1]
            entry = next(names, None)
            if entry is None:
                stack.pop()
                os.close(fd)
                if st is not None:
                    yield parent_fd, name, rel, st, True
                continue
            entry_rel = f"{rel}/{entry}" if rel else entry
            try:
                entry_st = os.stat(entry, dir_fd=fd, follow_symlinks=False)
            except OSError:
                if strict:
                    raise
                continue
            yield fd, entry, entry_rel, entry_st, False
            if not stat.S_ISDIR(entry_st.st_mode) or not descend(fd, entry, entry_rel, entry_st):
                continue
            if depth + 1 >= _WALK_DEPTH:
                if strict:
                    raise OSError(errno.ELOOP, "the tree is too deep to walk", entry_rel)
                continue
            try:
                child = os.open(entry, flags, dir_fd=fd)
            except OSError:
                if strict:
                    raise
                continue
            enter(child, depth + 1, fd, entry, entry_rel, entry_st)
    finally:
        for frame in stack:
            os.close(frame[0])


def _chmod_dir(name: str, dir_fd: int, mode: int) -> None:
    """Set the mode of the directory *name* in *dir_fd* without following a symlink."""
    try:
        os.chmod(name, mode, dir_fd=dir_fd, follow_symlinks=False)
    except NotImplementedError:
        # A C library that cannot chmod without following: nothing of the agent's runs while the
        # server walks its files, so the entry checked here is the entry changed.
        if stat.S_ISDIR(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode):
            os.chmod(name, mode, dir_fd=dir_fd)


def _remove_entries(
    root: str | Path,
    owned: Callable[[os.stat_result], bool],
    enter: Callable[[os.stat_result], bool],
    keep_dir: Callable[[int, str], None] | None = None,
) -> None:
    """Remove every entry under the directory *root* that *owned* accepts, entering the directories
    *enter* accepts; an owned one gets its permissions back first, so a lock the agent set keeps
    nothing. An owned directory that cannot be removed is handed to *keep_dir*. An owned directory
    too deep to enter is moved up into *root* and removed on a later pass, so no depth of tree
    outlasts the removal.
    """
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return

    def descend(dir_fd: int, name: str, _rel: str, st: os.stat_result) -> bool:
        if not enter(st):
            return False
        if owned(st):
            try:
                _chmod_dir(name, dir_fd, 0o700)
            except (OSError, NotImplementedError):
                pass
        return True

    try:
        for _ in range(_MAX_PASSES):
            moved = False
            for dir_fd, name, rel, st, left in _walk_tree(root, descend):
                if not owned(st):
                    continue
                try:
                    if left:
                        try:
                            os.rmdir(name, dir_fd=dir_fd)
                        except OSError:
                            if keep_dir is not None:
                                keep_dir(dir_fd, name)
                    elif not stat.S_ISDIR(st.st_mode):
                        os.unlink(name, dir_fd=dir_fd)
                    elif rel.count("/") + 1 >= _WALK_DEPTH and enter(st):
                        os.rename(
                            name, f".wb-deep-{uuid4().hex}", src_dir_fd=dir_fd, dst_dir_fd=root_fd
                        )
                        moved = True
                except (OSError, NotImplementedError):
                    continue
            if not moved:
                break
    finally:
        os.close(root_fd)


def _remove_tree(root: Path) -> None:
    """Remove a workspace even after its agent took the permissions away from its own directories.

    A *root* that is no longer a directory (agent code put a symlink in its place) is removed as
    what it is, and nothing below ever leaves the tree.
    """
    try:
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            os.unlink(root)
            return
    except OSError:
        return
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    _remove_entries(root, lambda _st: True, lambda _st: True)
    try:
        os.rmdir(root)
    except OSError:
        pass


def _sendable(text: str) -> str:
    """*text* as valid UTF-8. A lone surrogate, from a file name or a path the agent chose, would
    fail the observation's JSON encoding and with it the whole step."""
    return text.encode("utf-8", "replace").decode("utf-8")


def _clip(text: str, limit: int = OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... {len(text) - limit} more characters not shown]"


class AgentWorkbenchEnvironment(Environment):
    SUPPORTS_CONCURRENT_SESSIONS: bool = True

    def __init__(self) -> None:
        self._state = State(episode_id=str(uuid4()), step_count=0)
        self._task: BuiltTask | None = None
        # The workspace sits in a directory of its own that agent code cannot write, so it can
        # neither be renamed nor replaced, only emptied.
        self._home: Path | None = None
        self._ws: Path | None = None
        self._done = False
        self._final_reward = 0.0
        self._ids: tuple[int, int] | None = None

    # ------------------------------------------------------------------ lifecycle

    def reset(
        self,
        seed: int | None = None,
        episode_id: str | None = None,
        task_id: str | None = None,
        **kwargs: Any,
    ) -> WorkbenchObservation:
        ids = sorted(TASKS)
        if task_id is None:
            task_id = ids[(seed or 0) % len(ids)]
        if task_id not in TASKS:
            # Refused before anything is torn down, so a running episode is left as it was.
            raise ValueError(f"unknown task_id {task_id!r}")
        self._cleanup()
        self._task = build_task(task_id)
        self._state = State(episode_id=episode_id or str(uuid4()), step_count=0)
        self._done = False
        self._final_reward = 0.0
        self._invalid_total = 0
        self._invalid_run = 0
        self._inspected = False
        self._checked: set[str] = set()
        self._tested_last_change = False
        self._tested_digest: str | None = None
        self._checks = 0
        try:
            uid = _claim_sandbox_uid()
            self._ids = None if uid is None else (uid, uid)
            self._home = Path(tempfile.mkdtemp(prefix="wb-")).resolve()
            self._ws = self._home / "ws"
            self._ws.mkdir(mode=0o700)
            for rel, content in self._task.files.items():
                path = self._ws / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            self._own_workspace()
        except BaseException:
            # A half-built episode holds a uid and a directory nothing else would release.
            self._cleanup()
            self._task = None
            raise
        return WorkbenchObservation(
            task=self._task.instruction,
            tools=TOOLS_TEXT,
            output=_sendable("Workspace files:\n" + self._listing()),
            steps_left=MAX_STEPS,
            done=False,
            reward=0.0,
            metadata={"task_id": task_id, "domain": self._task.spec.domain},
        )

    def step(self, action: WorkbenchAction, **kwargs: Any) -> WorkbenchObservation:  # type: ignore[override]
        if self._task is None:
            # A stateless HTTP /step builds a fresh instance; give it the default task.
            self.reset()
        if self._done:
            return self._obs("The episode has ended.", done=True)
        self._state.step_count += 1
        handler = getattr(self, f"_tool_{action.tool}", None)
        if handler is None:
            return self._refuse(
                f"unknown tool {action.tool!r}; use one of the tools listed at reset"
            )
        try:
            result = handler(action.args)
        except _RefusalError as exc:
            return self._refuse(str(exc))
        self._invalid_run = 0
        if self._done:
            return result
        if self._state.step_count >= MAX_STEPS:
            return self._finish(
                None, "No tool calls left; the episode ends without a submitted answer."
            )
        return result

    @property
    def state(self) -> State:
        return self._state

    def close(self) -> None:
        self._cleanup()

    # ------------------------------------------------------------------ tools

    def _tool_list_files(self, args: dict) -> WorkbenchObservation:
        # Reset already shows the listing, so listing again is not reading the inputs.
        self._only(args, set())
        return self._obs(self._listing())

    def _tool_read_file(self, args: dict) -> WorkbenchObservation:
        self._only(args, {"path", "start", "end"})
        path = self._path(args.get("path"))
        start = self._int(args.get("start", 1), "start")
        fd = self._open_regular(path, os.O_RDONLY, args.get("path"))
        with os.fdopen(fd, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
        end = self._int(args.get("end", start + READ_LINES - 1), "end")
        start, end = max(1, start), min(len(lines), end, start + READ_LINES - 1)
        body = "\n".join(f"{n:>4} {lines[n - 1]}" for n in range(start, end + 1))
        self._inspected = True
        tail = f"\n[lines {start}-{end} of {len(lines)}]" if lines else "[empty file]"
        return self._obs(_clip(body + tail))

    def _tool_run_python(self, args: dict) -> WorkbenchObservation:
        self._only(args, {"code"})
        code = args.get("code")
        if not isinstance(code, str) or not code.strip():
            raise _RefusalError("run_python needs a non-empty string 'code'")
        self._write_private(".run.py", code)
        self._inspected = True
        self._tested_last_change = False  # the script may have changed the files
        return self._obs(self._run([SANDBOX_PYTHON, "-E", "-s", ".run.py"]))

    def _tool_write_file(self, args: dict) -> WorkbenchObservation:
        self._only(args, {"path", "content"})
        content = args.get("content")
        if not isinstance(content, str) or len(content) > WRITE_CHARS:
            raise _RefusalError(
                f"write_file needs a string 'content' of at most {WRITE_CHARS} characters"
            )
        try:
            data = content.encode("utf-8")
        except UnicodeEncodeError:
            raise _RefusalError("write_file 'content' must be text UTF-8 can encode") from None
        path = self._path(args.get("path"))
        try:
            missing = [
                d for d in reversed(path.parents) if self._ws in d.parents and not d.exists()
            ]
            for d in missing:
                d.mkdir()
                self._chown(d)
        except OSError as exc:
            raise _RefusalError(f"cannot write {args.get('path')!r}: {exc.strerror}") from None
        fd = self._open_regular(path, os.O_WRONLY | os.O_CREAT, args.get("path"))
        # Truncated only once the target is known to be a regular file.
        os.ftruncate(fd, 0)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        self._chown(path)
        self._tested_last_change = False
        return self._obs(f"wrote {path.relative_to(self._ws)} ({len(content)} characters)")

    def _tool_run_tests(self, args: dict) -> WorkbenchObservation:
        self._only(args, set())
        if self._task.answer_kind != "code":
            raise _RefusalError("run_tests is only for code tasks; use check to test an answer")
        # The examples as the task gave them, whatever the workspace's copy now says.
        before = self._workspace_digest()
        self._write_private(".grade_tests.py", self._task.files["test_solution.py"])
        output = self._run([SANDBOX_PYTHON, "-E", "-s", "-B", ".grade_tests.py"])
        passed = all(self._grade_cases(self._task.code_visible))
        # Verified only when the tests pass on the files as they stand: submit compares the files
        # with these, so a run that changes any of them tested something other than what is graded.
        self._tested_digest = before if passed else None
        self._tested_last_change = self._tested_digest is not None
        return self._obs(output)

    def _tool_check(self, args: dict) -> WorkbenchObservation:
        self._only(args, {"answer"})
        if self._task.answer_kind == "code":
            raise _RefusalError("for code tasks use run_tests")
        if self._checks >= MAX_CHECKS:
            raise _RefusalError(f"check can be used at most {MAX_CHECKS} times")
        answer = self._answer(args)
        self._checks += 1
        credit, _why = score_answer(self._task, answer)
        # The reason is withheld: "some items are missing" against a single candidate would say
        # whether that candidate belongs, and two checks would then score items without solving.
        if credit >= 1.0:
            self._checked.add(answer.strip())
        verdict = "correct" if credit >= 1.0 else "not correct"
        return self._obs(f"check: {verdict} ({MAX_CHECKS - self._checks} checks left)")

    def _tool_submit(self, args: dict) -> WorkbenchObservation:
        self._only(args, {"answer"})
        return self._finish(self._answer(args), None)

    def _tool_finish(self, args: dict) -> WorkbenchObservation:
        # Not offered to the agent: the trainer sends it to close an episode that did not end itself
        # (a malformed reply, a spent token budget), and such an episode earns nothing.
        self._only(args, set())
        return self._finish(None, "The episode ended without a submitted answer.")

    # ------------------------------------------------------------------ scoring

    def _finish(self, answer: str | None, note: str | None) -> WorkbenchObservation:
        correctness, why = (
            (0.0, "no answer was submitted")
            if answer is None
            else score_answer(self._task, answer, code_runner=self._grade_cases)
        )
        if self._task.answer_kind == "code":
            # The files the tests passed on are still the files being graded.
            verified = self._tested_last_change and self._workspace_digest() == self._tested_digest
        else:
            verified = answer is not None and answer.strip() in self._checked
        reward = 0.7 * correctness
        reward += 0.1 * (self._inspected and answer is not None)
        reward += 0.1 * (verified and answer is not None)
        reward += 0.1 * (self._invalid_total == 0 and answer is not None)
        self._final_reward = round(min(1.0, max(0.0, reward)), 4)
        self._done = True
        # Nothing of a finished episode is needed again, and a stateless caller may never close it.
        self._cleanup()
        text = note or f"Submitted. Result: {why}."
        return self._obs(text, done=True, correctness=correctness)

    def _grade_cases(self, cases: list[tuple[str, tuple, Any]]) -> list[bool]:
        # Each case runs on its own: before it, no process of the agent's is left, none of its files
        # is in the shared tmp directories, and none of the server's script files is in the
        # workspace. A case that leaves the workspace different fails, and so does every case after
        # it, so no call can hand state to the next.
        before = self._workspace_digest()
        results = []
        for name, call_args, want in cases:
            self._clear_agent_state()
            probe = (
                "import json, sys\n"
                "sys.path.insert(0, '.')\n"
                f"from solution import {name}\n"
                f"print(json.dumps({name}(*json.loads(sys.argv[1]))))\n"
            )
            self._write_private(".grade.py", probe)
            out = self._run(
                [SANDBOX_PYTHON, "-E", "-s", "-B", ".grade.py", json.dumps(list(call_args))],
                raw=True,
            )
            lines = [ln for ln in out.strip().splitlines() if ln.strip()]
            try:
                got = json.loads(lines[-1]) if lines else None
            except ValueError:
                got = None
            results.append(
                before is not None
                and got == json.loads(json.dumps(want))
                and self._workspace_digest() == before
            )
        return results

    def _workspace_digest(self) -> str | None:
        """A hash of every entry in the workspace but the server's own files: path, type, mode, and
        a regular file's modification time and bytes or a symlink's target. None when an entry
        cannot be read or the files hold more than _DIGEST_BYTES."""
        digest, total = hashlib.sha256(), 0
        try:
            for dir_fd, name, rel, st, left in _walk_tree(
                self._workspace(),
                lambda _fd, _name, rel, _st: rel not in _SERVER_FILES,
                strict=True,
            ):
                if left or rel in _SERVER_FILES or rel.startswith(_SERVER_DIRS):
                    continue
                digest.update(f"{rel}\0{st.st_mode}\0".encode("utf-8", "surrogateescape"))
                if stat.S_ISLNK(st.st_mode):
                    digest.update(os.fsencode(os.readlink(name, dir_fd=dir_fd)))
                elif stat.S_ISREG(st.st_mode):
                    total += st.st_size
                    if total > _DIGEST_BYTES:
                        return None
                    digest.update(f"{st.st_mtime_ns}\0".encode())
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
                    with os.fdopen(fd, "rb") as fh:
                        for chunk in iter(lambda fh=fh: fh.read(1 << 20), b""):
                            digest.update(chunk)
        except OSError:
            return None
        return digest.hexdigest()

    # ------------------------------------------------------------------ helpers

    def _obs(
        self, output: str, done: bool = False, correctness: float | None = None
    ) -> WorkbenchObservation:
        meta: dict[str, Any] = {
            "task_id": self._task.spec.task_id,
            "domain": self._task.spec.domain,
        }
        if correctness is not None:
            meta["correctness"] = correctness
        return WorkbenchObservation(
            output=_sendable(output),
            steps_left=max(0, MAX_STEPS - self._state.step_count),
            done=done,
            reward=self._final_reward if done else 0.0,
            metadata=meta,
        )

    def _refuse(self, reason: str) -> WorkbenchObservation:
        self._invalid_total += 1
        self._invalid_run += 1
        if self._invalid_run >= MAX_INVALID_IN_A_ROW:
            return self._finish(
                None, f"Refused: {reason}. Three invalid calls in a row end the episode."
            )
        if self._state.step_count >= MAX_STEPS:
            return self._finish(None, f"Refused: {reason}. No tool calls left.")
        obs = self._obs("")
        obs.error = _sendable(reason)
        return obs

    def _only(self, args: Any, allowed: set[str]) -> None:
        if not isinstance(args, dict):
            raise _RefusalError("args must be a JSON object")
        extra = set(args) - allowed
        if extra:
            raise _RefusalError(
                f"unexpected argument(s) {sorted(extra)}; allowed: {sorted(allowed) or 'none'}"
            )

    def _answer(self, args: dict) -> str:
        answer = args.get("answer")
        if isinstance(answer, (int, float)) and not isinstance(answer, bool):
            answer = str(answer)
        if not isinstance(answer, str) or not answer.strip():
            raise _RefusalError("'answer' must be a non-empty string")
        return answer

    @staticmethod
    def _int(value: Any, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise _RefusalError(f"'{name}' must be an integer")
        return value

    def _path(self, rel: Any) -> Path:
        # A path names a file inside the workspace without passing through a symlink, so the
        # server, which runs as root, never reads or writes through a link the agent planted.
        if not isinstance(rel, str) or not rel.strip() or "\x00" in rel:
            raise _RefusalError("'path' must be a non-empty string without NUL characters")
        parts = Path(rel).parts
        if Path(rel).is_absolute() or ".." in parts:
            raise _RefusalError("paths must stay inside the workspace (relative, no '..')")
        if not parts:
            raise _RefusalError("'path' must name a file, not the workspace itself")
        if any(part.startswith((".run", ".grade")) for part in parts):
            raise _RefusalError("that file is reserved")
        target = self._workspace()
        for part in parts:
            target = target / part
            try:
                is_link = stat.S_ISLNK(os.lstat(target).st_mode)
            except FileNotFoundError:
                break  # nothing further down can exist yet
            except OSError as exc:
                raise _RefusalError(f"cannot use the path {rel!r}: {exc.strerror}") from None
            if is_link:
                raise _RefusalError(
                    "paths must stay inside the workspace and may not pass through a symlink"
                )
        return self._ws.joinpath(*parts)

    def _workspace(self) -> Path:
        """The workspace, checked to be a real directory and given back the permissions agent code
        may have taken from it; a refusal when agent code removed it."""
        try:
            st = os.lstat(self._ws)
        except OSError:
            raise _RefusalError("the workspace directory is gone; nothing can run in it") from None
        if not stat.S_ISDIR(st.st_mode):
            raise _RefusalError("the workspace is no longer a directory; nothing can run in it")
        if stat.S_IMODE(st.st_mode) != 0o700:
            os.chmod(self._ws, 0o700)
        return self._ws

    @staticmethod
    def _open_regular(path: Path, flags: int, shown: Any) -> int:
        """An open descriptor for *path* when it is a regular file, else a refusal.

        O_NONBLOCK keeps a FIFO from blocking the open; the type is read from the open descriptor,
        so nothing can swap the file between the check and the use.
        """
        try:
            fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644)
        except FileNotFoundError:
            raise _RefusalError(f"no such file: {shown!r}") from None
        except OSError as exc:
            raise _RefusalError(f"cannot open {shown!r}: {exc.strerror}") from None
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise _RefusalError(f"{shown!r} is not a regular file")
        os.set_blocking(fd, True)
        return fd

    def _listing(self) -> str:
        # Symlinks are neither followed nor listed: the size of a link's target is not the agent's to
        # see. What cannot be read, under a directory the agent locked, is left out.
        rows = [
            f"{rel} ({st.st_size} bytes)"
            for _fd, name, rel, st, left in _walk_tree(self._workspace(), lambda *_: True)
            if not left and stat.S_ISREG(st.st_mode) and not name.startswith(".")
        ]
        return _clip("\n".join(sorted(rows)) or "(empty)")

    def _own_workspace(self) -> None:
        if self._ids is None:
            return
        # The episode's uid may pass through its home, not write it.
        os.chmod(self._home, 0o711)  # noqa: S103 - search only, no read or write
        os.chmod(self._ws, 0o700)
        for p in [self._ws, *self._ws.rglob("*")]:
            os.chown(p, *self._ids, follow_symlinks=False)

    def _write_private(self, name: str, text: str) -> None:
        try:
            data = text.encode("utf-8")
        except UnicodeEncodeError:
            raise _RefusalError(f"cannot prepare {name}: the text is not valid UTF-8") from None
        path = self._clear_private(name)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            self._chown(path)
        except OSError as exc:
            raise _RefusalError(f"cannot prepare {name}: {exc.strerror}") from None

    def _clear_private(self, name: str) -> Path:
        # The agent's code can plant a symlink, or a directory it locked, at this name; never
        # follow it as root, and refuse rather than fail when it cannot be cleared.
        path = self._workspace() / name
        try:
            if os.path.lexists(path):
                if stat.S_ISDIR(os.lstat(path).st_mode):
                    _remove_tree(path)
                else:
                    path.unlink()
        except OSError as exc:
            raise _RefusalError(f"cannot prepare {name}: {exc.strerror}") from None
        if os.path.lexists(path):
            raise _RefusalError(f"cannot prepare {name}: it could not be removed")
        return path

    def _clear_agent_state(self) -> None:
        """Leave nothing of the agent's running or in the shared tmp directories, and none of the
        server's script files in the workspace, so the next run starts from the workspace alone."""
        if self._ids is not None:
            self._kill_until_none()
            self._remove_shared_tmp_files()
        for name in (".run.py", ".grade_tests.py"):
            self._clear_private(name)

    def _chown(self, path: Path) -> None:
        if self._ids is not None:
            os.chown(path, *self._ids, follow_symlinks=False)

    def _run(self, argv: list[str], raw: bool = False) -> str:
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self._ws),
            "PYTHONDONTWRITEBYTECODE": "1",
            "LANG": "C.UTF-8",
        }
        kwargs: dict[str, Any] = {}
        if self._ids is not None:
            uid, gid = self._ids
            kwargs.update(user=uid, group=gid, extra_groups=[])
        try:
            # Runs the agent's own code on purpose, as an unprivileged user in its workspace.
            proc = subprocess.Popen(  # noqa: S603
                argv,
                cwd=self._workspace(),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # Whatever bytes the agent's code prints come back as text.
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
                **kwargs,
            )
        except OSError as exc:
            raise _RefusalError(f"cannot run in the workspace: {exc.strerror}") from None
        try:
            stdout, stderr = proc.communicate(timeout=PY_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self._kill_agent_processes(proc)
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            return f"timed out after {PY_TIMEOUT_S} s"
        finally:
            # Nothing the agent started may outlive the call: a leftover process could hold the output
            # pipe open or race the server's own writes into the workspace.
            self._kill_agent_processes(proc)
        if raw:
            return stdout
        out = (stdout + (("\n" + stderr) if stderr else "")).strip() or "(no output)"
        return _clip(f"{out}\n[exit code {proc.returncode}]")

    def _kill_agent_processes(self, proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        self._kill_sandbox_uid()

    def _kill_sandbox_uid(self) -> int:
        """Send SIGKILL to every process of this episode's uid; return how many were found."""
        if self._ids is None:
            return 0
        # In the image, every process of this episode's uid is the agent's, including any that left the
        # process group with setsid(). No other episode runs under this uid.
        found = 0
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid == self._ids[0]:
                    found += 1
                    os.kill(int(entry.name), signal.SIGKILL)
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
        return found

    def _kill_until_none(self) -> None:
        # Until a scan finds none, so nothing forks past a single scan of /proc.
        for _ in range(10):
            if not self._kill_sandbox_uid():
                break

    def _remove_shared_tmp_files(self) -> None:
        # A file the agent left in /tmp, at any depth, would otherwise be there for the next
        # episode under this uid, or for the next case its code is graded on. Only directories this
        # uid could have written in are entered, its own and world-writable ones, so no other
        # episode's home is. Only this uid's entries go: a directory of its own that holds another
        # uid's files is handed to root as a sticky, world-writable directory, so the other uid
        # keeps its files and the uid's next holder inherits nothing.
        uid = self._ids[0]

        def to_root(dir_fd: int, name: str) -> None:
            os.chown(name, 0, 0, dir_fd=dir_fd, follow_symlinks=False)
            _chmod_dir(name, dir_fd, 0o1777)

        for root in _SHARED_TMP_DIRS:
            _remove_entries(
                root,
                lambda st: st.st_uid == uid,
                lambda st: st.st_uid == uid or bool(st.st_mode & stat.S_IWOTH),
                to_root,
            )

    def _cleanup(self) -> None:
        if self._ids is not None:
            # Processes first, so nothing writes into a tree being removed.
            self._kill_until_none()
            self._remove_shared_tmp_files()
        if self._home is not None:
            _remove_tree(self._home)
            self._home = None
        self._ws = None
        if self._ids is not None:
            _release_sandbox_uid(self._ids[0])
            self._ids = None


class _RefusalError(Exception):
    pass
