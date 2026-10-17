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


class HungChild:
    """A child that ignores SIGTERM until it is killed; every signal is recorded."""

    returncode: int | None = None

    def __init__(self) -> None:
        self.signals: list[int] = []

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, signum: int) -> None:
        self.signals.append(signum)

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd="uvicorn", timeout=timeout or 0)
        return self.returncode

    def kill(self) -> None:
        self.signals.append(int(signal.SIGKILL))
        self.returncode = -int(signal.SIGKILL)


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
    monkeypatch.setattr(SeededDaemon, "_wait_healthy", lambda self, _timeout, **_kwargs: None)
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


def test_wrapper_reports_a_requested_stop_even_when_the_child_already_died(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """A requested stop decides the outcome, so the child's own exit is not reported.

    Teardown signals the whole process group, so the child may already be gone
    when the wrapper reads it. The wrapper was still asked to stop, and that
    request -- not the child's exit code -- decides how the run is reported.
    """

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

    assert run_seeded_daemon.main(["--port", "4321"]) == 0
    assert "child exited" not in capsys.readouterr().err


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

    assert run_seeded_daemon.main(["--port", "4321", "--ready-timeout", "0.01"]) == 2
    err = capsys.readouterr().err
    assert err.splitlines()[0] == "seeded studio daemon failed before ready: child code=2"
    assert "seeded daemon exited early" in err


def test_a_cooperative_stop_sends_no_thread_dump(monkeypatch, tmp_path: Path) -> None:
    """A child that leaves on SIGTERM is only asked to leave; no dump signal is sent.

    SIGUSR1 is a dump request, not a stop: sending it to every child on a normal
    stop would either kill a child without a handler or race its shutdown. Only
    a stop that has to kill asks for the dump (see the stop-kill tests).
    """
    if not hasattr(signal, "SIGUSR1"):
        pytest.skip("SIGUSR1 is POSIX-only")

    class RecordingProcess:
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def send_signal(self, signum: int) -> None:
            self.signals = getattr(self, "signals", [])
            self.signals.append(signum)

        def wait(self, timeout: float | None = None) -> int:
            self.returncode = 0
            return 0

        def kill(self) -> None:
            pass

    child = RecordingProcess()
    daemon = _stub_daemon(tmp_path, child)
    monkeypatch.setattr(run_seeded_daemon.SeededDaemon, "start", lambda **_kwargs: daemon)

    def stop_on_the_first_sleep(_seconds: float) -> None:
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)

    monkeypatch.setattr(
        run_seeded_daemon, "time", types.SimpleNamespace(sleep=stop_on_the_first_sleep)
    )

    assert run_seeded_daemon.main(["--port", "4321"]) == 0
    assert child.signals == [int(signal.SIGTERM)]


def test_requested_stop_reports_clean_when_the_group_signal_took_the_child_first(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """Teardown signals the whole group, so the child can exit before the wrapper reads it.

    The wrapper was asked to stop, so the child's exit is part of that stop: it
    must return 0, not report the child as having left on its own.
    """

    class GroupSignalledChild:
        """Still up when the loop checks; the group SIGTERM has taken it by stop time."""

        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def send_signal(self, _signum: int) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode or 0

        def kill(self) -> None:
            pass

    child = GroupSignalledChild()
    daemon = _stub_daemon(tmp_path, child)
    monkeypatch.setattr(run_seeded_daemon.SeededDaemon, "start", lambda **_kwargs: daemon)

    def group_sigterm_arrives(_seconds: float) -> None:
        # Both the wrapper and the child receive the teardown SIGTERM; the
        # child finishes shutting down before the wrapper leaves its loop.
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        child.returncode = 0

    monkeypatch.setattr(
        run_seeded_daemon, "time", types.SimpleNamespace(sleep=group_sigterm_arrives)
    )

    assert run_seeded_daemon.main(["--port", "4321"]) == 0
    err = capsys.readouterr().err
    assert "child exited" not in err


def test_stop_reports_a_hung_child_it_had_to_kill(monkeypatch, tmp_path: Path) -> None:
    """A stop that times out asks for a thread dump, kills, and says it had to kill."""
    if not hasattr(signal, "SIGUSR1"):
        pytest.skip("SIGUSR1 is POSIX-only")

    child = HungChild()
    daemon = _stub_daemon(tmp_path, child)

    assert daemon.stop(timeout=0.01) is True
    assert child.returncode == -int(signal.SIGKILL)
    dump_signal = int(signal.SIGUSR1)
    assert dump_signal in child.signals
    assert child.signals.index(dump_signal) < child.signals.index(int(signal.SIGKILL))


def test_stop_reports_a_child_that_left_after_sigterm(tmp_path: Path) -> None:
    """A cooperative stop never reports a kill."""
    assert _stub_daemon(tmp_path, RunningProcess()).stop(timeout=0.01) is False


def test_wrapper_returns_non_zero_when_its_stop_had_to_kill(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """A daemon that ignores SIGTERM and is killed fails the run with one stderr line."""
    child = HungChild()
    daemon = _stub_daemon(tmp_path, child)
    monkeypatch.setattr(run_seeded_daemon.SeededDaemon, "start", lambda **_kwargs: daemon)

    def stop_on_the_first_sleep(_seconds: float) -> None:
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)

    monkeypatch.setattr(
        run_seeded_daemon, "time", types.SimpleNamespace(sleep=stop_on_the_first_sleep)
    )

    # The child's wait() times out on the real stop() path, which then kills it.
    assert run_seeded_daemon.main(["--port", "4321"]) == 1
    assert child.returncode == -int(signal.SIGKILL)
    assert run_seeded_daemon.KILLED_MESSAGE in capsys.readouterr().err


def test_startup_timeout_asks_for_a_thread_dump_before_stopping(
    monkeypatch, tmp_path: Path
) -> None:
    """A startup hang must leave the child's stacks in the log, through stop()."""
    if not hasattr(signal, "SIGUSR1"):
        pytest.skip("SIGUSR1 is POSIX-only")

    child = HungChild()

    async def skip_seed(_db_path: Path, _tmp_dir: Path) -> None:
        pass

    def fake_popen(_args, *, env, stdout, **_kwargs):
        return child

    monkeypatch.setattr(harness, "_seed", skip_seed)
    monkeypatch.setattr(harness.subprocess, "Popen", fake_popen)
    # Force the startup timeout path start() takes when the daemon never answers.
    monkeypatch.setattr(
        SeededDaemon, "_wait_healthy", lambda self, _timeout, **_kwargs: _raise_timeout()
    )

    with pytest.raises(TimeoutError):
        SeededDaemon.start(port=1, ready_timeout=0.01)

    # start()'s own stop() is the path that must dump the stacks before killing.
    assert child.returncode == -int(signal.SIGKILL)
    assert int(signal.SIGUSR1) in child.signals


def _raise_timeout() -> None:
    raise TimeoutError("seeded daemon never became healthy")


def test_wait_healthy_stops_waiting_when_a_stop_is_requested(monkeypatch, tmp_path: Path) -> None:
    """A teardown signal during startup must not be waited out to the ready timeout."""

    child = RunningProcess()
    daemon = _stub_daemon(tmp_path, child)
    # A stop is already requested; _wait_healthy must raise promptly rather than
    # poll /health (nothing listens) until the deadline.
    with pytest.raises(harness.DaemonStopRequested):
        daemon._wait_healthy(30.0, stop_requested=lambda: True)


def test_wrapper_stops_the_child_when_a_stop_arrives_during_startup(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """A teardown signal during startup stops the child and reports a clean stop."""

    class StartingChild:
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

    child = StartingChild()
    daemon = _stub_daemon(tmp_path, child)

    # Record the wrapper's signal handlers instead of installing them, so the
    # test can raise the teardown signal the way the e2e teardown does.
    handlers: dict[int, object] = {}

    def record_handler(signum: int, handler: object) -> None:
        handlers[signum] = handler

    monkeypatch.setattr(run_seeded_daemon.signal, "signal", record_handler)

    def fake_start(cls, **kwargs):
        # The real startup, with the teardown signal already delivered: the
        # wrapper must stop the child through the usual path, not wait it out.
        assert kwargs["stop_requested"]() is False
        handler = handlers[signal.SIGTERM]
        assert callable(handler)
        handler(signal.SIGTERM, None)
        with pytest.raises(harness.DaemonStopRequested) as exc_info:
            daemon._wait_healthy(30.0, stop_requested=kwargs["stop_requested"])
        exc_info.value.had_to_kill = daemon.stop()
        raise exc_info.value

    monkeypatch.setattr(run_seeded_daemon.SeededDaemon, "start", classmethod(fake_start))

    assert run_seeded_daemon.main(["--port", "4321"]) == 0
    assert child.returncode == 0
    assert "failed before ready" not in capsys.readouterr().err
