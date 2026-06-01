# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Daytona sandbox integration — run lionagi agents inside isolated cloud containers.

Why this exists
---------------
lionagi's coding tools (``CodingToolkit``) execute file edits and shell commands
on the *host*. For untrusted or destructive work (running a model-written patch,
executing a project's test suite, SWE-bench), that host is the wrong place. The
clean model is to run the **entire agent process inside a sandbox** against a
local checkout, and exchange with it over the reactive bus — emission flows out
as a signal stream, control flows in as a polled signal file. The container is
the isolation boundary; the bus is the protocol across it.

This module is the host-side half: a thin async wrapper over Daytona's
``AsyncSandbox`` covering the lifecycle a lionagi run needs — create (from a
reusable snapshot), clone a repo at a commit, push/pull files, exec (blocking or
live-streamed), and tear down. The in-sandbox half (the agent driver that emits
signals to stdout and polls a control file) is application code; see
``benchmarks/orchestration/suites/swebench`` for the SWE-bench driver.

``daytona`` is an optional dependency (``pip install lionagi[sandbox]``); the
import is deferred so importing lionagi never requires it.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from daytona import AsyncSandbox


def _require_daytona():
    try:
        import daytona  # noqa: F401
    except ImportError as e:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "Daytona support requires the 'daytona' package. "
            "Install it with: pip install 'lionagi[sandbox]'  (or: uv add daytona)"
        ) from e
    return daytona


@dataclass(slots=True)
class ExecResult:
    """Result of a command run in the sandbox."""

    exit_code: int
    stdout: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class DaytonaSandbox:
    """Async-context-managed Daytona sandbox tuned for lionagi agent runs.

    Usage::

        async with await DaytonaSandbox.create(snapshot="lionagi-bench") as sb:
            await sb.clone("https://github.com/django/django.git",
                           f"{sb.home}/repo", commit="abc1234")
            await sb.write_text("print('hi')", f"{sb.home}/repo/x.py")
            r = await sb.exec("python x.py", cwd=f"{sb.home}/repo")
            print(r.stdout)
        # sandbox auto-deleted on exit (override with delete_on_exit=False)

    The object is a holder over an ``AsyncSandbox``; all methods are passthroughs
    that normalize return shapes (``ExecResult``, ``str``, ``bytes``).
    """

    def __init__(self, client: Any, sandbox: AsyncSandbox, *, delete_on_exit: bool = True):
        self._client = client
        self._sb = sandbox
        self._delete_on_exit = delete_on_exit
        self._home: str | None = None

    # -- lifecycle -------------------------------------------------------

    @classmethod
    async def create(
        cls,
        *,
        snapshot: str | None = None,
        image: Any | None = None,
        env: dict[str, str] | None = None,
        resources: dict[str, int] | None = None,
        labels: dict[str, str] | None = None,
        auto_stop_minutes: int | None = 15,
        delete_on_exit: bool = True,
        create_timeout: float = 180.0,
    ) -> DaytonaSandbox:
        """Create a sandbox and return a ready ``DaytonaSandbox``.

        ``snapshot`` names a prebuilt snapshot (the fast path — instant create).
        ``image`` is a ``daytona.Image`` for an on-the-fly build (slower; prefer
        snapshots for repeated runs). Exactly one of snapshot/image is typically
        set; with neither, Daytona's default image is used.

        ``env`` is injected as sandbox environment (e.g. provider API keys).
        ``auto_stop_minutes`` auto-stops an idle sandbox as a credit backstop.
        """
        _require_daytona()
        from daytona import (
            AsyncDaytona,
            CreateSandboxFromImageParams,
            CreateSandboxFromSnapshotParams,
            Resources,
        )

        client = AsyncDaytona()
        try:
            common: dict[str, Any] = {}
            if env:
                common["env_vars"] = env
            if labels:
                common["labels"] = labels
            if auto_stop_minutes is not None:
                common["auto_stop_interval"] = auto_stop_minutes

            if image is not None:
                res = Resources(**resources) if resources else None
                params = CreateSandboxFromImageParams(image=image, resources=res, **common)
            elif snapshot is not None:
                params = CreateSandboxFromSnapshotParams(snapshot=snapshot, **common)
            else:
                params = CreateSandboxFromSnapshotParams(**common) if common else None

            sb = await client.create(params, timeout=create_timeout)
            return cls(client, sb, delete_on_exit=delete_on_exit)
        except Exception:
            await client.close()
            raise

    async def __aenter__(self) -> DaytonaSandbox:
        return self

    async def __aexit__(self, *exc) -> None:
        try:
            if self._delete_on_exit:
                await self.delete()
        finally:
            await self._client.close()

    async def delete(self) -> None:
        await self._sb.delete()

    @property
    def id(self) -> str:
        return self._sb.id

    @property
    def sandbox(self) -> AsyncSandbox:
        """The underlying ``AsyncSandbox`` for operations not wrapped here."""
        return self._sb

    async def home_dir(self) -> str:
        if self._home is None:
            self._home = await self._sb.get_user_home_dir()
        return self._home

    # -- git -------------------------------------------------------------

    async def clone(
        self,
        url: str,
        path: str,
        *,
        commit: str | None = None,
        branch: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        """Clone ``url`` into ``path``; optionally check out a specific commit.

        Daytona's git.clone checks out ``commit_id`` directly when given, which is
        what SWE-bench needs (detached HEAD at the bug's base_commit).
        """
        await self._sb.git.clone(
            url, path, branch=branch, commit_id=commit, username=username, password=password
        )

    async def git_diff(self, repo_path: str, *, staged_all: bool = True) -> str:
        """Unified diff of working tree vs HEAD — the model_patch.

        Stages everything first (``git add -A``) so new files appear in the diff,
        then ``git diff --cached``.
        """
        if staged_all:
            await self.exec("git add -A", cwd=repo_path)
        r = await self.exec("git diff --cached", cwd=repo_path)
        return r.stdout

    # -- exec ------------------------------------------------------------

    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> ExecResult:
        """Run a shell command, blocking until it completes."""
        resp = await self._sb.process.exec(command, cwd=cwd, env=env, timeout=timeout)
        return ExecResult(exit_code=int(resp.exit_code or 0), stdout=resp.result or "")

    async def exec_stream(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
        session_id: str | None = None,
    ) -> int:
        """Run a command async in a session, streaming stdout/stderr live.

        ``on_stdout`` receives output chunks as they arrive — the host's live
        signal channel (parse ``@@SIG@@`` lines off this). Returns the exit code.
        A ``cd`` prefix scopes cwd because session commands ignore the cwd arg.
        """
        from daytona import SessionExecuteRequest

        sid = session_id or f"run-{os.urandom(4).hex()}"
        full_cmd = f"cd {cwd} && {command}" if cwd else command
        await self._sb.process.create_session(sid)
        try:
            req = SessionExecuteRequest(command=full_cmd, run_async=True)
            resp = await self._sb.process.execute_session_command(sid, req)
            cmd_id = resp.cmd_id
            await self._sb.process.get_session_command_logs_async(
                sid,
                cmd_id,
                on_stdout or (lambda _c: None),
                on_stderr or (lambda _c: None),
            )
            cmd = await self._sb.process.get_session_command(sid, cmd_id)
            return int(getattr(cmd, "exit_code", 0) or 0)
        finally:
            with contextlib.suppress(Exception):
                await self._sb.process.delete_session(sid)

    # -- filesystem ------------------------------------------------------

    async def upload_bytes(self, data: bytes, dst: str) -> None:
        await self._sb.fs.upload_file(data, dst)

    async def upload_file(self, src: str | Path, dst: str) -> None:
        await self._sb.fs.upload_file(str(src), dst)

    async def write_text(self, text: str, dst: str) -> None:
        await self._sb.fs.upload_file(text.encode("utf-8"), dst)

    async def download(self, path: str) -> bytes:
        data = await self._sb.fs.download_file(path)
        return data or b""

    async def read_text(self, path: str) -> str:
        return (await self.download(path)).decode("utf-8", errors="replace")

    async def mkdir(self, path: str, mode: str = "755") -> None:
        await self._sb.fs.create_folder(path, mode)


async def ensure_snapshot(
    name: str,
    *,
    image: Any | None = None,
    resources: dict[str, int] | None = None,
    on_logs: Callable[[str], None] | None = None,
    rebuild: bool = False,
) -> str:
    """Build a named snapshot once; return its name. Reuses an existing one.

    The slow step (installing the dependency tree) happens here, a single time.
    Sandboxes created from the snapshot start in well under a second. Pass
    ``rebuild=True`` to force a fresh build (e.g. after changing the dep set).
    """
    _require_daytona()
    from daytona import AsyncDaytona, CreateSnapshotParams, Resources

    client = AsyncDaytona()
    try:
        if not rebuild:
            # snapshot.get raises if absent → fall through and build it
            with contextlib.suppress(Exception):
                await client.snapshot.get(name)
                return name
        img = image if image is not None else lionagi_image()
        res = Resources(**resources) if resources else None
        await client.snapshot.create(
            CreateSnapshotParams(name=name, image=img, resources=res),
            on_logs=on_logs,
            timeout=0,
        )
        return name
    finally:
        await client.close()


def lionagi_image(
    *,
    python: str = "3.12",
    pip_spec: str = "lionagi==0.26.14",
    extra_pip: tuple[str, ...] = ("pyyaml", "pytest"),
    apt: tuple[str, ...] = ("git",),
):
    """A declarative ``daytona.Image`` with git + lionagi's dependency tree.

    Built once into a named snapshot, then reused: install lionagi from PyPI here
    to capture the *dependency* tree, and overlay this-branch's wheel per run with
    ``pip install --no-deps`` so code iteration doesn't reinstall deps each time.
    """
    _require_daytona()
    from daytona import Image

    img = Image.debian_slim(python)
    if apt:
        img = img.run_commands(f"apt-get update && apt-get install -y {' '.join(apt)}")
    img = img.pip_install(pip_spec, *extra_pip)
    return img
