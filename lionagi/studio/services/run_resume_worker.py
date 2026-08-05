# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""One-shot worker that serializes resumes behind an active source run."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from lionagi.state.db import (
    SESSION_TERMINAL_STATUSES,
    StateDB,
    read_only_open_supported,
    state_db_file,
)


def _load_config(path: str) -> dict[str, Any]:
    config_path = Path(path)
    try:
        value = json.loads(config_path.read_text())
    finally:
        # The parent also owns best-effort cleanup; removing immediately keeps
        # the user's instruction off disk while this worker waits.
        config_path.unlink(missing_ok=True)
    if not isinstance(value, dict):
        raise ValueError("resume worker config must be a JSON object")
    return value


@contextmanager
def _branch_resume_lock(branch_id: str):
    """Serialize queued legs for one branch for the lifetime of each child."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows fallback
        yield
        return

    db_path = state_db_file()
    if db_path is None:
        raise RuntimeError("queued resume requires a local StateDB")
    lock_dir = Path(db_path).parent / "resume-locks"
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256(branch_id.encode()).hexdigest()
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_dir / f"{digest}.lock", flags, 0o600)
    with os.fdopen(fd, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


async def _wait_for_terminal(run_id: str) -> None:
    while True:
        async with StateDB(readonly=read_only_open_supported()) as db:
            session = await db.get_session(run_id)
        if session is None:
            raise RuntimeError("source run disappeared while its resume was queued")
        if session.get("status") in SESSION_TERMINAL_STATUSES:
            return
        await asyncio.sleep(0.25)


async def run_worker(config: dict[str, Any]) -> int:
    from .run_resume import (
        _build_resume_argv,
        _ensure_branch_snapshot_available,
        _validate_resume_inputs,
    )

    run_id = config.get("run_id")
    branch_id = config.get("branch_id")
    instruction = config.get("instruction")
    model = config.get("model")
    executable_prefix = config.get("executable_prefix")
    if not all(isinstance(value, str) and value for value in (run_id, branch_id, instruction)):
        raise ValueError("resume worker config is missing an identity or instruction")
    if model is not None and not isinstance(model, str):
        raise ValueError("resume worker model must be a string or null")
    if (
        not isinstance(executable_prefix, list)
        or not executable_prefix
        or any(not isinstance(token, str) or not token for token in executable_prefix)
    ):
        raise ValueError("resume worker executable prefix is invalid")
    _validate_resume_inputs(instruction, branch_id=branch_id, model=model)

    with _branch_resume_lock(branch_id):
        await _wait_for_terminal(run_id)
        # Terminal status is the hand-off signal: canonical writers publish
        # their atomic final branch snapshot before setting it.
        await _ensure_branch_snapshot_available(branch_id)
        argv = _build_resume_argv(
            executable_prefix,
            branch_id=branch_id,
            instruction=instruction,
            model=model,
        )
        process = await asyncio.create_subprocess_exec(*argv)
        return await process.wait()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        config = _load_config(args.config)
        return asyncio.run(run_worker(config))
    except Exception as exc:  # noqa: BLE001
        print(
            f"Queued Studio resume failed ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
