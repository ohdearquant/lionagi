"""Spins up a Lion Studio daemon subprocess pointed entirely at a temp dir.

Hard safety rule: this harness must NEVER read or write the real
``~/.lionagi`` (state db, agents/playbooks roots, or anything else under it).
Every path the daemon could resolve is redirected via env vars set *before*
the subprocess starts -- the daemon's path constants are module-level and
resolved once at import time, so an in-process monkeypatch would not reach a
separate subprocess; only subprocess env does. The resolved db path is also
asserted to live under the temp dir before any seeding happens, so a future
change to path-resolution logic that silently drifted back to the real home
directory would fail loudly here instead of quietly reading/writing it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from lionagi.state.db import StateDB

from .fixtures import seed_filesystem_fixtures, seed_state_db

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_LIONAGI_HOME = Path("~/.lionagi").expanduser().resolve()

# Where the daemon child's own output goes when a caller wants to keep it past
# stop(). ``stop()`` removes the temp dir, so the Playwright setup -- whose log
# directory becomes a failure artifact -- points this outside it. Unset, the
# file stays beside the state db in the temp dir, which is what the pytest
# callers in this suite read.
CHILD_LOG_ENV = "LIONAGI_E2E_STUDIO_CHILD_LOG"

_OPERATOR_SCRIPT = {
    "version": 1,
    "responses": [
        {
            "type": "error",
            "kind": "timeout",
            "message": "scripted wait",
            "delay_ms": 10_000,
            "when": {"prompt_contains": "wait until I stop you"},
        },
        {
            "type": "stream",
            "chunks": [
                {
                    "type": "text",
                    "content": "Continuation complete.",
                    "is_delta": True,
                },
                {"type": "result", "metadata": {"done": True}},
            ],
            "when": {"prompt_contains": "Continue with the next check."},
        },
        {
            "type": "stream",
            "chunks": [
                {"type": "text", "content": "Fleet ", "is_delta": True},
                {"type": "text", "content": "ready.", "is_delta": True},
                {"type": "result", "metadata": {"done": True}},
            ],
        },
    ],
}


def _free_port() -> int:
    """Bind to port 0 and read back the OS-assigned free port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _seed(db_path: Path, tmp_dir: Path) -> None:
    seed_filesystem_fixtures(tmp_dir)
    db = StateDB(db_path)
    await db.open()
    try:
        # The one line of defense that must never be able to silently pass
        # against the real home directory: verify the path StateDB actually
        # resolved to (not just the path we intended to pass it) is under
        # the temp dir, and is not the real state db.
        resolved = db.path
        if resolved is None or not resolved.resolve().is_relative_to(tmp_dir.resolve()):
            raise RuntimeError(
                f"refusing to seed outside the temp dir: resolved db path "
                f"{resolved} is not under {tmp_dir}"
            )
        if resolved.resolve() == REAL_LIONAGI_HOME / "state.db":
            raise RuntimeError("refusing to seed the real ~/.lionagi/state.db")
        await seed_state_db(db)
    finally:
        await db.close()


class DaemonExitedEarly(RuntimeError):
    """The daemon child exited before it answered /health.

    Carries the child's own ``returncode`` so a caller can name the code or
    signal (``128 + signum`` for a signal death) without re-reading a process
    that teardown has already signalled.
    """

    def __init__(self, message: str, returncode: int | None) -> None:
        super().__init__(message)
        self.returncode = returncode


@dataclass
class SeededDaemon:
    """A running Studio daemon subprocess + the temp dir backing it."""

    tmp_dir: Path
    db_path: Path
    host: str
    port: int
    process: subprocess.Popen
    child_log_path: Path | None = None
    # Byte offset in ``child_log_path`` where this attempt's output starts. The
    # log is appended to across a retry, so an early-exit error built from the
    # whole file would quote a failed attempt's output (and an "address already
    # in use" from attempt 1 would drive attempt 2's port retry). Recorded
    # before spawn; stays 0 for a caller that built the object directly.
    child_log_offset: int = 0

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @classmethod
    def start(
        cls,
        *,
        host: str = "127.0.0.1",
        port: int | None = None,
        ready_timeout: float = 30.0,
    ) -> SeededDaemon:
        tmp_dir = Path(tempfile.mkdtemp(prefix="lionagi-e2e-studio-"))
        db_path = tmp_dir / "state.db"

        assert db_path.resolve().is_relative_to(tmp_dir.resolve()), (
            f"refusing to seed outside temp dir: {db_path} not under {tmp_dir}"
        )
        assert db_path.resolve() != REAL_LIONAGI_HOME / "state.db", (
            "refusing to seed the real ~/.lionagi/state.db"
        )

        try:
            asyncio.run(_seed(db_path, tmp_dir))
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

        if port is None:
            port = _free_port()

        env = dict(os.environ)
        env["LIONAGI_HOME"] = str(tmp_dir)
        env["LIONAGI_STATE_DB_URL"] = str(db_path)
        env["LIONAGI_SHOWS_ROOT"] = str(tmp_dir / "shows")
        env["LIONAGI_STUDIO_HOST"] = host
        env["LIONAGI_STUDIO_PORT"] = str(port)
        # Keep the mirror explicitly off in this harness. An isolated
        # LIONAGI_HOME already blocks ambient transcript roots by default, but
        # this also protects the fixture if a caller opts ambient imports in.
        env["LIONAGI_STUDIO_MIRROR_CLAUDE"] = "0"
        operator_script = tmp_dir / "operator-script.json"
        operator_script.write_text(json.dumps(_OPERATOR_SCRIPT), encoding="utf-8")
        env["LIONAGI_STUDIO_OPERATOR_PROVIDER"] = "scripted"
        env["LIONAGI_STUDIO_OPERATOR_MODEL"] = "scripted-test"
        env["LIONAGI_TEST_SCRIPT"] = str(operator_script)
        # Deterministic no-auth, API-only daemon regardless of ambient shell env.
        env.pop("LIONAGI_STUDIO_AUTH_TOKEN", None)
        env.pop("LIONAGI_STUDIO_FRONTEND_DIST", None)
        env.pop("LIONAGI_STUDIO_URL", None)

        # The child's output goes to a file, never a pipe: nothing drains a
        # pipe after startup, so uvicorn's per-request access log would fill
        # the 64KB buffer during any long-lived run and block the event loop
        # mid-write — the daemon then accepts connections and answers nothing.
        # A file outside the temp dir survives stop(), which is what a caller
        # collecting the log as an artifact needs. That file is appended to, not
        # truncated: a retry within one run then keeps the failed attempt's
        # output, the same way the wrapper's per-server log does.
        child_log_path = tmp_dir / "daemon-child.log"
        mode = "w"
        if override := os.environ.get(CHILD_LOG_ENV):
            child_log_path = Path(override)
            child_log_path.parent.mkdir(parents=True, exist_ok=True)
            mode = "a"
        # Size before this attempt writes, so _wait_healthy reports only its own
        # output: the file is opened append, and a retry shares it with the
        # attempt that failed on the port.
        child_log_offset = child_log_path.stat().st_size if child_log_path.exists() else 0
        with child_log_path.open(mode, encoding="utf-8") as child_log:
            process = subprocess.Popen(  # noqa: S603
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "tests.e2e_studio.studio_app:app",
                    "--host",
                    host,
                    "--port",
                    str(port),
                ],
                cwd=REPO_ROOT,
                env=env,
                stdout=child_log,
                stderr=subprocess.STDOUT,
                text=True,
            )

        daemon = cls(
            tmp_dir=tmp_dir,
            db_path=db_path,
            host=host,
            port=port,
            process=process,
            child_log_path=child_log_path,
            child_log_offset=child_log_offset,
        )
        try:
            daemon._wait_healthy(ready_timeout)
        except Exception:
            daemon.stop()
            raise
        return daemon

    def _wait_healthy(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        url = f"{self.base_url}/health"
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                output = ""
                if self.child_log_path is not None and self.child_log_path.exists():
                    output = self._read_child_output()
                raise DaemonExitedEarly(
                    f"seeded daemon exited early (code={self.process.returncode}):\n{output}",
                    self.process.returncode,
                )
            try:
                with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310
                    if resp.status == 200:
                        return
            except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
                last_err = exc
            time.sleep(0.25)
        raise TimeoutError(f"seeded daemon never became healthy at {url}: {last_err}")

    def _read_child_output(self) -> str:
        """This attempt's slice of the child log, from the pre-spawn offset."""
        assert self.child_log_path is not None
        with self.child_log_path.open("rb") as handle:
            handle.seek(self.child_log_offset)
            return handle.read().decode("utf-8", errors="replace")

    def stop(self, *, timeout: float = 5.0) -> None:
        # Bounded below the Playwright setup's own 10 s SIGKILL fallback: a
        # wrapper stop that waits less than that finishes reporting before the
        # setup gives up on the process group.
        if self.process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    self.process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self.process.wait(timeout=5)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
