"""Unit coverage for where the seeded daemon's own output goes and what the wrapper reports.

The Playwright setup keeps the log directory as a failure artifact, so two
things have to hold without starting a server: the child's stdout/stderr go to
a file that outlives the temp dir, and the wrapper's exit status tells the
truth when the child dies on its own. Both are driven here with a stub process
and a plain ``sys.executable`` child that is not a server; the Playwright specs
remain the end-to-end coverage.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import types
from pathlib import Path

import pytest

from . import harness, run_seeded_daemon
from .harness import SeededDaemon

CHILD_LOG_ENV = "LIONAGI_E2E_STUDIO_CHILD_LOG"


class RunningProcess:
    """A child that stays up: just enough of subprocess.Popen for start()/stop()."""

    returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, _signum: int) -> None:
        pass

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0
        return 0

    def kill(self) -> None:
        pass


class SignalledProcess:
    """A child a signal already killed; poll() reports it and there is nothing to signal."""

    returncode = -int(signal.SIGTERM)

    def poll(self) -> int:
        return self.returncode


def _drive_start_without_a_daemon(monkeypatch) -> dict:
    """Let SeededDaemon.start() run against a stub child: no seeding, no health wait."""
    captured: dict = {}

    async def skip_seed(_db_path: Path, _tmp_dir: Path) -> None:
        pass

    def fake_popen(_args, *, env, stdout, **_kwargs):
        captured["env"] = env
        captured["stdout_path"] = Path(stdout.name)
        return RunningProcess()

    monkeypatch.setattr(harness, "_seed", skip_seed)
    monkeypatch.setattr(SeededDaemon, "_wait_healthy", lambda self, _timeout: None)
    monkeypatch.setattr(harness.subprocess, "Popen", fake_popen)
    return captured


def _stub_daemon(tmp_dir: Path, process: object) -> SeededDaemon:
    return SeededDaemon(
        tmp_dir=tmp_dir,
        db_path=tmp_dir / "state.db",
        host="127.0.0.1",
        port=4321,
        process=process,
    )


def test_child_log_leaves_the_temp_dir_when_one_is_configured(monkeypatch, tmp_path: Path) -> None:
    kept_log = tmp_path / "kept" / "studio-daemon-child.log"
    monkeypatch.setenv(CHILD_LOG_ENV, str(kept_log))

    captured = _drive_start_without_a_daemon(monkeypatch)
    daemon = SeededDaemon.start(port=1, ready_timeout=0.01)
    try:
        assert captured["env"][CHILD_LOG_ENV] == str(kept_log)
        assert captured["stdout_path"] == kept_log
        assert daemon.child_log_path == kept_log
        kept_log.write_text("the child's own output\n", encoding="utf-8")
    finally:
        daemon.stop()

    # stop() removed the temp dir; the configured file is outside it and survives.
    assert not daemon.tmp_dir.exists()
    assert kept_log.read_text(encoding="utf-8") == "the child's own output\n"

    # A retry within one run appends: the failed attempt's lines stay.
    captured = _drive_start_without_a_daemon(monkeypatch)
    second = SeededDaemon.start(port=1, ready_timeout=0.01)
    try:
        assert captured["stdout_path"].read_text(encoding="utf-8") == "the child's own output\n"
    finally:
        second.stop()

    # Unset -- the pytest harness -- the child log stays beside the temp dir it
    # belongs to, which is the file today's callers read.
    monkeypatch.delenv(CHILD_LOG_ENV)
    captured = _drive_start_without_a_daemon(monkeypatch)
    default_daemon = SeededDaemon.start(port=1, ready_timeout=0.01)
    try:
        assert default_daemon.child_log_path == default_daemon.tmp_dir / "daemon-child.log"
        assert captured["stdout_path"] == default_daemon.child_log_path
    finally:
        default_daemon.stop()


def test_wrapper_reports_the_child_exit_code(monkeypatch, tmp_path: Path, capsys) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "print('child said something'); raise SystemExit(3)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    daemon = _stub_daemon(tmp_path, process)
    monkeypatch.setattr(run_seeded_daemon.SeededDaemon, "start", lambda **_kwargs: daemon)

    assert run_seeded_daemon.main(["--port", "4321"]) == 3
    assert "code=3" in capsys.readouterr().err
    assert not tmp_path.exists()


def test_wrapper_reports_a_child_killed_by_signal(monkeypatch, tmp_path: Path, capsys) -> None:
    daemon = _stub_daemon(tmp_path, SignalledProcess())
    monkeypatch.setattr(run_seeded_daemon.SeededDaemon, "start", lambda **_kwargs: daemon)

    assert run_seeded_daemon.main(["--port", "4321"]) == 128 + int(signal.SIGTERM)
    assert f"signal={int(signal.SIGTERM)}" in capsys.readouterr().err


def test_wrapper_returns_zero_for_a_requested_stop(monkeypatch, tmp_path: Path, capsys) -> None:
    daemon = _stub_daemon(tmp_path, RunningProcess())
    monkeypatch.setattr(run_seeded_daemon.SeededDaemon, "start", lambda **_kwargs: daemon)

    # Stand in for the SIGTERM the e2e teardown sends: call the handler the
    # wrapper installed from the loop's sleep, so no real signal is raised.
    def stop_during_sleep(_seconds: float) -> None:
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)

    monkeypatch.setattr(run_seeded_daemon, "time", types.SimpleNamespace(sleep=stop_during_sleep))

    assert run_seeded_daemon.main(["--port", "4321"]) == 0
    err = capsys.readouterr().err
    assert "child exited" not in err
    assert "killed by signal" not in err


def test_wait_healthy_reads_only_this_attempts_child_output(monkeypatch, tmp_path: Path) -> None:
    """A retry appends to one child log; an early-exit error must not quote the failed attempt."""
    child_log = tmp_path / "studio-daemon-child.log"
    child_log_offset = len(b"attempt 1: address already in use\n")
    child_log.write_bytes(b"attempt 1: address already in use\n")
    monkeypatch.setenv(CHILD_LOG_ENV, str(child_log))

    async def skip_seed(_db_path: Path, _tmp_dir: Path) -> None:
        pass

    class ExitedProcess:
        returncode = 1

        def poll(self) -> int:
            return self.returncode

    def exit_with_a_new_error(_args, *, env, stdout, **_kwargs):
        assert env[CHILD_LOG_ENV] == str(child_log)
        stdout.write("attempt 2: an unrelated failure\n")
        return ExitedProcess()

    monkeypatch.setattr(harness, "_seed", skip_seed)
    monkeypatch.setattr(harness.subprocess, "Popen", exit_with_a_new_error)

    with pytest.raises(RuntimeError, match="seeded daemon exited early") as exc_info:
        SeededDaemon.start(port=1, ready_timeout=0.01)

    message = str(exc_info.value)
    assert "attempt 2: an unrelated failure" in message
    assert "address already in use" not in message
    assert child_log.stat().st_size > child_log_offset


def test_wrapper_reports_a_child_that_exited_before_a_requested_stop(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """A requested stop must not turn a daemon that had already died into a clean stop."""

    class DiedProcess:
        """Still up when the loop checks, gone by the time the stop is read."""

        returncode: int | None = None

        def __init__(self) -> None:
            self.polls = 0

        def poll(self) -> int | None:
            self.polls += 1
            if self.polls == 1:
                return None
            self.returncode = 4
            return self.returncode

        def send_signal(self, _signum: int) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            pass

    daemon = _stub_daemon(tmp_path, DiedProcess())
    monkeypatch.setattr(run_seeded_daemon.SeededDaemon, "start", lambda **_kwargs: daemon)

    # Stand in for the SIGTERM the e2e teardown sends: call the handler the
    # wrapper installed, so no real signal is raised.
    def stop_during_sleep(_seconds: float) -> None:
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)

    monkeypatch.setattr(run_seeded_daemon, "time", types.SimpleNamespace(sleep=stop_during_sleep))

    assert run_seeded_daemon.main(["--port", "4321"]) == 4
    assert "code=4" in capsys.readouterr().err


def test_wrapper_reports_a_failure_before_the_daemon_is_ready(monkeypatch, capsys) -> None:
    """A daemon that never answers /health fails the run on one line, not a traceback."""

    async def skip_seed(_db_path: Path, _tmp_dir: Path) -> None:
        pass

    class ExitedProcess:
        returncode = 2

        def poll(self) -> int:
            return self.returncode

    def exit_before_ready(_args, *, env, stdout, **_kwargs):
        stdout.write("address already in use\n")
        return ExitedProcess()

    monkeypatch.setattr(harness, "_seed", skip_seed)
    monkeypatch.setattr(harness.subprocess, "Popen", exit_before_ready)

    assert run_seeded_daemon.main(["--port", "4321", "--ready-timeout", "0.01"]) == 1
    err = capsys.readouterr().err
    assert "failed before ready" in err
    assert "seeded daemon exited early" in err
    assert "code=2" in err


def test_wrapper_asks_a_running_child_for_a_thread_dump_before_stopping_it(
    monkeypatch, tmp_path: Path
) -> None:
    """A hung daemon's stacks must reach the child log before the group is stopped."""
    if not hasattr(signal, "SIGUSR1"):
        pytest.skip("SIGUSR1 is POSIX-only")

    sent: list[int] = []

    class RecordingProcess:
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def send_signal(self, signum: int) -> None:
            sent.append(signum)

        def wait(self, timeout: float | None = None) -> int:
            self.returncode = 0
            return 0

        def kill(self) -> None:
            pass

    daemon = _stub_daemon(tmp_path, RecordingProcess())
    monkeypatch.setattr(run_seeded_daemon.SeededDaemon, "start", lambda **_kwargs: daemon)

    slept: list[float] = []

    def stop_on_the_first_sleep(_seconds: float) -> None:
        slept.append(_seconds)
        if len(slept) == 1:
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)

    monkeypatch.setattr(
        run_seeded_daemon, "time", types.SimpleNamespace(sleep=stop_on_the_first_sleep)
    )

    assert run_seeded_daemon.main(["--port", "4321"]) == 0

    dump_signal = int(signal.SIGUSR1)
    assert dump_signal in sent
    assert sent.index(dump_signal) < sent.index(int(signal.SIGTERM))
