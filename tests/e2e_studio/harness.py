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


@dataclass
class SeededDaemon:
    """A running Studio daemon subprocess + the temp dir backing it."""

    tmp_dir: Path
    db_path: Path
    host: str
    port: int
    process: subprocess.Popen

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
        # Disable the in-process Claude Code mirror: it tails the real
        # ~/.claude/projects regardless of LIONAGI_HOME, which would inject
        # nondeterministic content and violate the "never touch the real
        # home dir" rule in spirit even though it's a different tree.
        env["LIONAGI_STUDIO_MIRROR_CLAUDE"] = "0"
        # Deterministic no-auth, API-only daemon regardless of ambient shell env.
        env.pop("LIONAGI_STUDIO_AUTH_TOKEN", None)
        env.pop("LIONAGI_STUDIO_FRONTEND_DIST", None)
        env.pop("LIONAGI_STUDIO_URL", None)

        process = subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-m",
                "uvicorn",
                "lionagi.studio.app:app",
                "--host",
                host,
                "--port",
                str(port),
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        daemon = cls(tmp_dir=tmp_dir, db_path=db_path, host=host, port=port, process=process)
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
                output = self.process.stdout.read() if self.process.stdout else ""
                raise RuntimeError(
                    f"seeded daemon exited early (code={self.process.returncode}):\n{output}"
                )
            try:
                with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310
                    if resp.status == 200:
                        return
            except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
                last_err = exc
            time.sleep(0.25)
        raise TimeoutError(f"seeded daemon never became healthy at {url}: {last_err}")

    def stop(self, *, timeout: float = 10.0) -> None:
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
