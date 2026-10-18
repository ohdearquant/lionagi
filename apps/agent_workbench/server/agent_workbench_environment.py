# SPDX-License-Identifier: Apache-2.0
"""Agent Workbench: tool-using tasks across eight work domains.

Each episode is one task: a few files in a private workspace, a question or a bug to fix, and a
small fixed tool set. The episode ends when the agent submits, runs out of tool calls, makes
three invalid calls in a row, or the trainer ends it. Only the final observation carries a reward,
and an episode that ends without a submitted answer earns 0.

Reward (0..1) = 0.7 x correctness + 0.1 x inspected the workspace before submitting
              + 0.1 x verified before submitting (check, or run_tests for code tasks)
              + 0.1 x made no invalid tool calls.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import signal
import subprocess
import tempfile
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
    "run_tests {} - code tasks only: run test_solution.py.\n"
    f'check {{"answer": str}} - ask whether an answer is correct before you submit (at most {MAX_CHECKS} times).\n'
    'submit {"answer": str} - give your final answer and end the episode.\n'
    f"You have {MAX_STEPS} tool calls. Read the inputs before answering, and verify before you submit."
)

_SANDBOX_USER = "nobody"


def _sandbox_ids() -> tuple[int, int] | None:
    """Run agent code as an unprivileged user when the server runs as root (inside the image)."""
    if os.geteuid() != 0:
        return None
    try:
        entry = pwd.getpwnam(_SANDBOX_USER)
    except KeyError:
        return None
    return entry.pw_uid, entry.pw_gid


def _clip(text: str, limit: int = OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... {len(text) - limit} more characters not shown]"


class AgentWorkbenchEnvironment(Environment):
    SUPPORTS_CONCURRENT_SESSIONS: bool = True

    def __init__(self) -> None:
        self._state = State(episode_id=str(uuid4()), step_count=0)
        self._task: BuiltTask | None = None
        self._ws: Path | None = None
        self._done = False
        self._final_reward = 0.0
        self._ids = _sandbox_ids()

    # ------------------------------------------------------------------ lifecycle

    def reset(
        self,
        seed: int | None = None,
        episode_id: str | None = None,
        task_id: str | None = None,
        **kwargs: Any,
    ) -> WorkbenchObservation:
        self._cleanup()
        ids = sorted(TASKS)
        if task_id is None:
            task_id = ids[(seed or 0) % len(ids)]
        if task_id not in TASKS:
            raise ValueError(f"unknown task_id {task_id!r}")
        self._task = build_task(task_id)
        self._state = State(episode_id=episode_id or str(uuid4()), step_count=0)
        self._done = False
        self._final_reward = 0.0
        self._invalid_total = 0
        self._invalid_run = 0
        self._inspected = False
        self._verified = False
        self._checks = 0
        self._ws = Path(tempfile.mkdtemp(prefix="wb-")).resolve()
        for rel, content in self._task.files.items():
            path = self._ws / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self._own_workspace()
        return WorkbenchObservation(
            task=self._task.instruction,
            tools=TOOLS_TEXT,
            output="Workspace files:\n" + self._listing(),
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
        self._only(args, set())
        self._inspected = True
        return self._obs(self._listing())

    def _tool_read_file(self, args: dict) -> WorkbenchObservation:
        self._only(args, {"path", "start", "end"})
        path = self._path(args.get("path"))
        if not path.is_file():
            raise _RefusalError(f"no such file: {args.get('path')!r}")
        start = self._int(args.get("start", 1), "start")
        lines = path.read_text(errors="replace").splitlines()
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
        return self._obs(self._run([SANDBOX_PYTHON, "-E", "-s", ".run.py"]))

    def _tool_write_file(self, args: dict) -> WorkbenchObservation:
        self._only(args, {"path", "content"})
        content = args.get("content")
        if not isinstance(content, str) or len(content) > WRITE_CHARS:
            raise _RefusalError(
                f"write_file needs a string 'content' of at most {WRITE_CHARS} characters"
            )
        path = self._path(args.get("path"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        self._chown(path)
        return self._obs(f"wrote {path.relative_to(self._ws)} ({len(content)} characters)")

    def _tool_run_tests(self, args: dict) -> WorkbenchObservation:
        self._only(args, set())
        if self._task.answer_kind != "code":
            raise _RefusalError("run_tests is only for code tasks; use check to test an answer")
        self._inspected = True
        self._verified = True
        return self._obs(self._run([SANDBOX_PYTHON, "-E", "-s", "test_solution.py"]))

    def _tool_check(self, args: dict) -> WorkbenchObservation:
        self._only(args, {"answer"})
        if self._task.answer_kind == "code":
            raise _RefusalError("for code tasks use run_tests")
        if self._checks >= MAX_CHECKS:
            raise _RefusalError(f"check can be used at most {MAX_CHECKS} times")
        answer = self._answer(args)
        self._checks += 1
        self._verified = True
        credit, why = score_answer(self._task, answer)
        verdict = "correct" if credit >= 1.0 else f"not fully correct: {why}"
        return self._obs(f"check: {verdict} ({MAX_CHECKS - self._checks} checks left)")

    def _tool_submit(self, args: dict) -> WorkbenchObservation:
        self._only(args, {"answer"})
        if self._task.answer_kind == "code":
            return self._finish(str(args.get("answer", "")), None)
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
            else score_answer(self._task, answer, code_runner=self._grade_code)
        )
        reward = 0.7 * correctness
        reward += 0.1 * (self._inspected and answer is not None)
        reward += 0.1 * (self._verified and answer is not None)
        reward += 0.1 * (self._invalid_total == 0 and answer is not None)
        self._final_reward = round(min(1.0, max(0.0, reward)), 4)
        self._done = True
        text = note or f"Submitted. Result: {why}."
        return self._obs(text, done=True, correctness=correctness)

    def _grade_code(self, task: BuiltTask) -> float:
        passed = 0
        for name, call_args, want in task.code_cases:
            probe = (
                "import json, sys\n"
                "sys.path.insert(0, '.')\n"
                f"from solution import {name}\n"
                f"print(json.dumps({name}(*json.loads(sys.argv[1]))))\n"
            )
            self._write_private(".grade.py", probe)
            out = self._run(
                [SANDBOX_PYTHON, "-E", "-s", ".grade.py", json.dumps(list(call_args))], raw=True
            )
            lines = [ln for ln in out.strip().splitlines() if ln.strip()]
            try:
                got = json.loads(lines[-1]) if lines else None
            except ValueError:
                got = None
            if got == json.loads(json.dumps(want)):
                passed += 1
        return passed / len(task.code_cases)

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
            output=output,
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
        obs.error = reason
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
        if not isinstance(rel, str) or not rel.strip():
            raise _RefusalError("'path' must be a non-empty string")
        target = (self._ws / rel).resolve()
        root = self._ws.resolve()
        if target != root and root not in target.parents:
            raise _RefusalError("paths must stay inside the workspace")
        if target.name.startswith(".run") or target.name.startswith(".grade"):
            raise _RefusalError("that file is reserved")
        return target

    def _listing(self) -> str:
        rows = []
        for p in sorted(self._ws.rglob("*")):
            if p.is_file() and not p.name.startswith("."):
                rows.append(f"{p.relative_to(self._ws)} ({p.stat().st_size} bytes)")
        return "\n".join(rows) or "(empty)"

    def _own_workspace(self) -> None:
        if self._ids is None:
            return
        for p in [self._ws, *self._ws.rglob("*")]:
            os.chown(p, *self._ids)

    def _write_private(self, name: str, text: str) -> None:
        # The agent's code can plant a symlink at this name; never follow it as root.
        path = self._ws / name
        if path.is_symlink() or path.exists():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        self._chown(path)

    def _chown(self, path: Path) -> None:
        if self._ids is not None:
            os.chown(path, *self._ids)

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
        # Runs the agent's own code on purpose, as an unprivileged user in its workspace.
        proc = subprocess.Popen(  # noqa: S603
            argv,
            cwd=self._ws,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            **kwargs,
        )
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
        if self._ids is None:
            return
        # In the image, every process of the sandbox user is the agent's, including any that left the
        # process group with setsid().
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid == self._ids[0]:
                    os.kill(int(entry.name), signal.SIGKILL)
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue

    def _cleanup(self) -> None:
        if self._ws is not None:
            shutil.rmtree(self._ws, ignore_errors=True)
            self._ws = None


class _RefusalError(Exception):
    pass
