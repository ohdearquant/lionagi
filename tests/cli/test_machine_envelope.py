# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The machine-result contract, pinned at the boundary a consumer sees.

These tests are written from the position of a consumer in another language that
reaches lionagi by running the CLI as a subprocess: it has two channels, one
integer version, and no way to ask what a field means. So what is asserted here
is the shape on stdout, the closedness of the error vocabulary, and the rule that
decides which of the exit status and the envelope answers.
"""

from __future__ import annotations

import errno
import importlib
import json
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from lionagi.cli import machine
from lionagi.cli._util import (
    EXIT_CODE_ENVIRONMENT_ERROR,
    clear_run_allocation,
    mark_run_allocated,
)

cli_main = importlib.import_module("lionagi.cli.main")

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _no_run_allocated():
    clear_run_allocation()
    yield
    clear_run_allocation()


def _one_object(out: str) -> dict:
    """Parse stdout under the invariant, rather than around it."""
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1, f"stdout carried {len(lines)} lines, not one object: {out!r}"
    return json.loads(lines[0])


# The envelope


def test_a_success_carries_data_and_no_error(capfd):
    assert cli_main.main(["handshake", "--machine"]) == 0
    envelope = _one_object(capfd.readouterr().out)

    assert envelope["ok"] is True
    assert envelope["error"] is None
    assert envelope["data"]["implementation"] == "lionagi"
    assert set(envelope) == {"ok", "contract_version", "data", "error"}


def test_a_refusal_carries_error_and_no_data(capfd):
    assert cli_main.main(["no-such-command", "--machine"]) == 0
    envelope = _one_object(capfd.readouterr().out)

    assert envelope["ok"] is False
    assert envelope["data"] is None
    assert envelope["error"]["kind"] == "invalid_input"
    assert isinstance(envelope["error"]["message"], str)


def test_the_version_is_an_integer_and_lives_in_one_place(capfd):
    """A consumer compares it, so `1` and `"1"` are not interchangeable."""
    assert cli_main.main(["handshake", "--machine"]) == 0
    envelope = _one_object(capfd.readouterr().out)

    assert envelope["contract_version"] == machine.CONTRACT_VERSION
    assert isinstance(envelope["contract_version"], int)
    assert not isinstance(envelope["contract_version"], bool)
    # The handshake governs registration and the envelope governs every call, so
    # the two must never be able to disagree.
    assert envelope["data"]["contract_version"] == machine.CONTRACT_VERSION
    assert envelope["data"]["min_supported_version"] == machine.MIN_SUPPORTED_CONTRACT_VERSION


@pytest.mark.parametrize(
    "malformed",
    [
        {"ok": True, "contract_version": 1, "data": None, "error": None},
        {"ok": True, "contract_version": 1, "data": {}, "error": {"kind": "internal", "m": "x"}},
        {"ok": True, "contract_version": "1", "data": {}, "error": None},
        {"ok": False, "contract_version": 1, "data": {}, "error": None},
        {"contract_version": 1, "data": {}, "error": None},
    ],
)
def test_a_malformed_envelope_is_refused_before_it_is_written(malformed):
    """Both-null and both-set are the two shapes a consumer cannot interpret."""
    with pytest.raises(ValueError):
        machine.validate_envelope(malformed)


# The closed error vocabulary


def test_the_error_kinds_are_exactly_the_contract_set():
    """Closedness is the property a consumer branches on, so it is asserted as a
    set rather than as membership."""
    assert set(machine.ERROR_KINDS) == {
        "not_found",
        "invalid_input",
        "conflict",
        "unavailable",
        "internal",
    }


@pytest.mark.parametrize("kind", ["not_a_kind", "error", "failure", "timeout", ""])
def test_a_kind_outside_the_set_cannot_be_constructed(kind):
    with pytest.raises(ValueError):
        machine.failure(kind, "message")
    with pytest.raises(ValueError):
        machine.MachineError(kind, "message")


def test_an_unsupported_command_refuses_inside_the_vocabulary(capfd, monkeypatch):
    """A command with no machine result still answers in the contract's terms.

    The alternative — argparse rejecting the flag — leaves the consumer with a
    nonzero exit, no envelope and nothing to distinguish it from a crash.
    """
    assert cli_main.main(["mirror", "--machine"]) == 0
    envelope = _one_object(capfd.readouterr().out)

    assert envelope["ok"] is False
    assert envelope["error"]["kind"] == "unavailable"
    assert "mirror" in envelope["error"]["message"]


def test_an_unexpected_crash_becomes_an_envelope(capfd, monkeypatch):
    """A traceback and no JSON is indistinguishable from the process dying."""

    def _boom(argv):
        raise RuntimeError("something nobody anticipated")

    monkeypatch.setitem(machine._MACHINE_COMMANDS, "handshake", _boom)

    assert cli_main.main(["handshake", "--machine"]) == 0
    envelope = _one_object(capfd.readouterr().out)

    assert envelope["error"]["kind"] == "internal"
    assert "something nobody anticipated" in envelope["error"]["message"]


# Exactly one JSON object on stdout


def test_nothing_else_reaches_stdout(capfd, monkeypatch):
    """Every writer a command has, pointed away from stdout at once.

    `print` and the four logging channels are Python-level writers; the raw
    descriptor write stands in for a spawned child that inherited stdout. All of
    them corrupt the result identically if any one is missed.
    """
    from lionagi.cli._logging import configure_cli_logging, hint, log_error, progress, warn

    configure_cli_logging(verbose=False)

    def _noisy(argv):
        print("a stray print")
        sys.stdout.write("a stray write\n")
        os.write(1, b"a stray descriptor write\n")
        progress("progress line")
        hint("hint line")
        warn("warning line")
        log_error("error line")
        return {"fine": True}

    monkeypatch.setitem(machine._MACHINE_COMMANDS, "handshake", _noisy)

    assert cli_main.main(["handshake", "--machine"]) == 0
    captured = capfd.readouterr()

    envelope = _one_object(captured.out)
    assert envelope["data"] == {"fine": True}
    for line in ("a stray print", "a stray write", "a stray descriptor write", "hint line"):
        assert line in captured.err
    assert "warning: warning line" in captured.err
    assert "error: error line" in captured.err


def test_stdout_is_restored_afterwards(capfd):
    """The reservation is scoped to the call, not to the process."""
    assert cli_main.main(["handshake", "--machine"]) == 0
    capfd.readouterr()

    print("back on stdout")
    assert "back on stdout" in capfd.readouterr().out


def test_no_descriptor_is_left_behind_when_the_redirect_cannot_be_installed(monkeypatch):
    """The two syscalls fail independently, so the first one's result needs closing.

    `dup` can succeed and the `dup2` on the next line fail — an embedding with no
    usable stderr is the realistic case. The exit path only closes a descriptor it
    was told to restore, so without an explicit close the duplicate survives the
    call, once per call, for as long as the process runs.
    """
    real_dup2 = os.dup2

    def no_stderr_redirect(source, target, *args, **kwargs):
        if (source, target) == (2, 1):
            raise OSError(errno.EBADF, "Bad file descriptor")
        return real_dup2(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "dup2", no_stderr_redirect)

    def open_descriptors() -> int:
        return len(os.listdir("/dev/fd"))

    before = open_descriptors()
    for _ in range(5):
        with machine.reserve_stdout() as channel:
            channel.emit(machine.ok({}))
    assert open_descriptors() == before


def test_a_second_envelope_is_refused():
    """One object is the framing; a second one silently breaks every parser."""
    with machine.reserve_stdout() as channel:
        channel.emit(machine.ok({}))
        with pytest.raises(RuntimeError):
            channel.emit(machine.ok({}))


def test_the_flag_after_a_sentinel_is_a_prompt_token():
    """A prompt containing `--machine` must not change the output mode."""
    assert machine.has_machine_flag(["agent", "--", "--machine"]) is False
    assert machine.strip_machine_flag(["agent", "--machine", "--", "--machine"]) == [
        "agent",
        "--",
        "--machine",
    ]


def test_one_object_on_stdout_end_to_end():
    """The same invariant with nothing patched and a real process boundary.

    In-process tests share an interpreter with the harness, and the descriptor
    layer is the part most easily faked by that. This is what the consumer
    actually reads.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "lionagi.cli", "handshake", "--machine"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        timeout=120,
    )

    assert proc.returncode == 0, proc.stderr
    envelope = _one_object(proc.stdout)
    assert envelope["ok"] is True
    assert envelope["contract_version"] == machine.CONTRACT_VERSION


# D7: absence and failure do not share an encoding


def test_an_empty_read_and_a_failed_read_are_different_answers(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()

    established = machine.list_directory(empty)
    assert established == {"available": True, "value": [], "reason_code": None, "detail": None}

    missing = machine.list_directory(tmp_path / "gone")
    assert missing["available"] is False
    assert missing["value"] is None
    assert missing["reason_code"] == "not_found"


def test_an_unreadable_directory_is_not_reported_as_empty(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    os.chmod(locked, 0o000)
    try:
        result = machine.list_directory(locked)
    finally:
        os.chmod(locked, 0o755)

    if os.geteuid() == 0:  # root reads it regardless, so there is nothing to assert
        pytest.skip("permissions do not constrain root")
    assert result["available"] is False
    assert result["reason_code"] == "unreadable"
    assert result["value"] is None


def test_a_missing_document_and_a_corrupt_one_are_told_apart(tmp_path):
    corrupt = tmp_path / "run.json"
    corrupt.write_text("{not json")

    assert machine.read_json_file(tmp_path / "absent.json")["reason_code"] == "not_found"
    assert machine.read_json_file(corrupt)["reason_code"] == "malformed"

    good = tmp_path / "good.json"
    good.write_text('{"status": "running"}')
    assert machine.read_json_file(good) == {
        "available": True,
        "value": {"status": "running"},
        "reason_code": None,
        "detail": None,
    }


def test_a_run_listing_wraps_every_read_it_makes(capfd, monkeypatch, tmp_path):
    runs_root = tmp_path / "runs"
    (runs_root / "20260725T000000-aaaaaa").mkdir(parents=True)
    (runs_root / "20260725T000000-aaaaaa" / "run.json").write_text("{}")
    # No artifacts directory: the producer always creates one, so its absence is
    # a read that did not establish an answer rather than a count of zero.
    monkeypatch.setattr("lionagi.cli._runs.RUNS_ROOT", runs_root)

    assert cli_main.main(["runs", "--machine"]) == 0
    envelope = _one_object(capfd.readouterr().out)

    listing = envelope["data"]["runs"]
    assert listing["available"] is True
    entry = listing["value"][0]
    assert entry["run_id"] == "20260725T000000-aaaaaa"
    assert entry["artifacts"]["available"] is False
    assert entry["artifacts"]["value"] is None
    assert entry["artifacts"]["reason_code"] == "not_found"


def test_no_runs_at_all_is_an_established_answer(capfd, monkeypatch, tmp_path):
    monkeypatch.setattr("lionagi.cli._runs.RUNS_ROOT", tmp_path / "never-created")

    assert cli_main.main(["runs", "--machine"]) == 0
    envelope = _one_object(capfd.readouterr().out)

    assert envelope["data"]["runs"] == {
        "available": True,
        "value": [],
        "reason_code": None,
        "detail": None,
    }


# D8: which signal answers


def test_a_refusal_still_exits_zero(capfd):
    """The envelope is the answer; repeating the refusal in the exit status
    would give one question two answers."""
    assert cli_main.main(["no-such-command", "--machine"]) == 0
    assert _one_object(capfd.readouterr().out)["ok"] is False


def test_an_unusable_environment_exits_78_with_no_envelope(capfd, monkeypatch):
    """Nothing executed, so there is nothing on stdout for a consumer to parse.

    An envelope here would describe a request that never ran, which sends the
    consumer looking for a result instead of at its own installation.
    """

    def _boom(argv):
        raise ModuleNotFoundError("No module named 'sniffio'", name="sniffio")

    monkeypatch.setitem(machine._MACHINE_COMMANDS, "handshake", _boom)

    assert cli_main.main(["handshake", "--machine"]) == EXIT_CODE_ENVIRONMENT_ERROR
    captured = capfd.readouterr()
    assert captured.out.strip() == ""
    assert "No run was started" in captured.err


def test_a_missing_import_after_a_run_exists_is_an_envelope(capfd, monkeypatch):
    """78 claims nothing ran, and once a run directory exists that is false.

    The failure belongs to the run, so it is reported the way every other
    handled request is: an envelope, on stdout, exit 0.
    """

    def _boom(argv):
        mark_run_allocated()
        raise ModuleNotFoundError("No module named 'some_provider'", name="some_provider")

    monkeypatch.setitem(machine._MACHINE_COMMANDS, "handshake", _boom)

    assert cli_main.main(["handshake", "--machine"]) == 0
    envelope = _one_object(capfd.readouterr().out)
    assert envelope["error"]["kind"] == "internal"


def test_78_survives_end_to_end_under_the_machine_flag():
    """The environment fault is decided before machine mode can answer at all.

    Asserted through a real interpreter because the import that fails here has
    to fail for real: the in-process test above supplies the error, which cannot
    prove that the code path a broken installation takes reaches this rule.
    """
    script = textwrap.dedent(
        """
        import sys

        BLOCKED = "lionagi.cli.machine"

        class _RefuseOne:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == BLOCKED:
                    raise ModuleNotFoundError(
                        f"No module named {fullname!r}", name=fullname
                    )
                return None

        sys.modules.pop(BLOCKED, None)
        sys.meta_path.insert(0, _RefuseOne())

        from lionagi.cli.main import main

        sys.exit(main(["handshake", "--machine"]))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        timeout=120,
    )

    assert proc.returncode == EXIT_CODE_ENVIRONMENT_ERROR, proc.stderr
    assert proc.stdout.strip() == ""


# the machine path leaves SIGPIPE where the interpreter put it


def _sigpipe_disposition_after(*argv: str) -> str:
    script = textwrap.dedent(
        """
        import signal, sys

        from lionagi.cli.main import _run

        _run(sys.argv[1:])
        print(
            "SIG_DFL" if signal.getsignal(signal.SIGPIPE) is signal.SIG_DFL else "ignored",
            file=sys.stderr,
        )
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script, *argv],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stderr.strip().splitlines()[-1]


@pytest.mark.skipif(not hasattr(signal, "SIGPIPE"), reason="no SIGPIPE on this platform")
def test_a_machine_call_never_runs_with_a_signal_that_can_kill_it_silently():
    """A command under the default SIGPIPE disposition can stop mid-answer.

    The default kills the process on any EPIPE, from any thread, with no
    envelope and nothing on stderr — and not every write is one the command
    made. A database driver's worker thread reporting a result to an event loop
    that is shutting down writes to the loop's own wakeup socket, and loses that
    race often enough to be a routine outcome rather than a rare one. The
    interpreter's own setting turns that into a catchable error; this surface
    needs it, because its whole contract is that a call always answers.
    """
    assert _sigpipe_disposition_after("handshake", "--machine") == "ignored"


@pytest.mark.skipif(not hasattr(signal, "SIGPIPE"), reason="no SIGPIPE on this platform")
def test_the_human_path_still_ends_quietly_when_its_reader_goes_away():
    # The reason the default is set at all: `li ... | head` should stop, not
    # print a traceback about a pipe the person closed on purpose.
    assert _sigpipe_disposition_after("handshake") == "SIG_DFL"


def test_lifecycle_hands_a_consumer_the_end_and_whether_it_was_observed():
    """A run's end reaches the consumer with its provenance attached.

    The consumer here is the one this file is written for: another language,
    reading JSON, with no way to ask what a field means. Given `ended_at`
    alone it cannot tell an end somebody observed from one reconstructed
    afterwards from leftover evidence, and the two are arithmetic-identical.
    The aggregate end IS one of the session ends, so it carries that row's
    provenance rather than a fresh judgement about the run.
    """
    from lionagi.cli.machine import _lifecycle_summary

    summary = _lifecycle_summary(
        [
            {
                "id": "s1",
                "status": "completed",
                "started_at": 100.0,
                "ended_at": 150.0,
                "ended_at_is_approximate": 0,
            },
            {
                "id": "s2",
                "status": "completed",
                "started_at": 150.0,
                "ended_at": 400.0,
                "ended_at_is_approximate": 1,
            },
        ]
    )

    assert summary["terminal"] is True
    assert [entry["ended_at_is_approximate"] for entry in summary["sessions"]] == [False, True]
    # The run's end is s2's, so it inherits s2's provenance.
    assert summary["ended_at"] == 400.0
    assert summary["ended_at_is_approximate"] is True


def test_lifecycle_states_the_provenance_key_on_every_branch():
    """A key that appears only once a run has ended forces the consumer to
    tell absent from null, and a consumer that does not will read absent as
    measured. Cheaper to always answer the question."""
    from lionagi.cli.machine import _lifecycle_summary

    nothing_recorded = _lifecycle_summary([])
    still_running = _lifecycle_summary(
        [{"id": "s1", "status": "running", "started_at": 100.0, "ended_at": None}]
    )

    assert nothing_recorded["found"] is False
    assert still_running["terminal"] is False
    for summary in (nothing_recorded, still_running):
        assert "ended_at_is_approximate" in summary
        assert summary["ended_at_is_approximate"] is None


def test_lifecycle_summary_reports_unknown_when_the_row_has_no_flag_column():
    """A store predating the column is a real shape, not a hypothetical.

    The machine readers open read-only where the backend supports it, and a
    read-only open deliberately does not reconcile the schema, because doing so
    would write to the store being reported on. The query is SELECT *, so rows
    from such a store arrive with no key at all. Coercing that to false would
    answer "this end was measured" about a row where nothing recorded whether
    it was, which is the confusion the column exists to remove.
    """
    from lionagi.cli.machine import _lifecycle_summary

    legacy = {
        "id": "s1",
        "status": "completed",
        "started_at": 100.0,
        "ended_at": 400.0,
    }
    assert "ended_at_is_approximate" not in legacy

    out = _lifecycle_summary([legacy])

    assert out["sessions"][0]["ended_at_is_approximate"] is None
    assert out["ended_at_is_approximate"] is None
    assert out["ended_at"] == 400.0


def test_lifecycle_summary_still_reports_false_when_the_column_says_measured():
    """Control for the test above: unknown is preserved, not manufactured.

    A row that carries the column and records a measured end must still come
    back false. Reporting null there would lose the very provenance the field
    was added to carry.
    """
    from lionagi.cli.machine import _lifecycle_summary

    measured = {
        "id": "s1",
        "status": "completed",
        "started_at": 100.0,
        "ended_at": 400.0,
        "ended_at_is_approximate": 0,
    }

    out = _lifecycle_summary([measured])

    assert out["sessions"][0]["ended_at_is_approximate"] is False
    assert out["ended_at_is_approximate"] is False
