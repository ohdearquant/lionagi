# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The machine-result contract: one envelope, one JSON object on stdout.

Every `li <command> --machine` call answers with exactly one JSON object on
stdout, nothing else; every human-facing byte goes to stderr. See
docs/internals/cli.md for the module's pieces (envelope constructors,
availability wrapper, fd-level stdout reservation, dispatch).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from importlib import import_module
from pathlib import Path
from typing import Any

from lionagi._auto import CliDeclaration, auto_register

__all__ = (
    "CONTRACT_VERSION",
    "MIN_SUPPORTED_CONTRACT_VERSION",
    "ERROR_KINDS",
    "MachineError",
    "ok",
    "failure",
    "validate_envelope",
    "available",
    "unavailable",
    "read_json_file",
    "list_directory",
    "machine_parser",
    "parse_machine_argv",
    "machine_subcommand",
    "readonly_state_db",
    "state_db_absent",
    "store_unreachable",
    "reserve_stdout",
    "dispatch_machine",
    "handshake_data",
    "add_handshake_subparser",
    "run_handshake",
    "add_runs_subparser",
    "run_runs",
    "lifecycle_data",
    "add_lifecycle_subparser",
    "run_lifecycle",
)

# The one place the current contract version lives. Incremented only by a change
# that could break a conforming consumer — which includes any field that changes
# what an existing field means to a reader that ignores it.
CONTRACT_VERSION = 1

# The oldest contract this implementation still answers, so a consumer pinned to
# a version we have dropped can say so instead of failing mysteriously.
MIN_SUPPORTED_CONTRACT_VERSION = 1

# Closed, on purpose: this vocabulary describes our own refusal to answer, which
# we control, so a consumer may branch on it and a new kind costs a version
# increment. Run status is the opposite case and is not modelled here.
ERROR_KINDS = ("not_found", "invalid_input", "conflict", "unavailable", "internal")

# Availability reason codes are advisory qualifiers, not a closed vocabulary; a
# consumer surfaces them and must not need one to decide `available`.
REASON_NOT_FOUND = "not_found"
REASON_UNREADABLE = "unreadable"
REASON_MALFORMED = "malformed"


class MachineError(Exception):
    """A refusal a machine command states deliberately, with its kind."""

    def __init__(self, kind: str, message: str, detail: dict[str, Any] | None = None) -> None:
        if kind not in ERROR_KINDS:
            raise ValueError(f"error kind {kind!r} is not one of {ERROR_KINDS}")
        self.kind = kind
        self.detail = detail
        super().__init__(message)


# ── Envelope ────────────────────────────────────────────────────────────────


def ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "contract_version": CONTRACT_VERSION, "data": data, "error": None}


def failure(kind: str, message: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    if kind not in ERROR_KINDS:
        raise ValueError(f"error kind {kind!r} is not one of {ERROR_KINDS}")
    return {
        "ok": False,
        "contract_version": CONTRACT_VERSION,
        "data": None,
        "error": {"kind": kind, "message": message, "detail": detail},
    }


def validate_envelope(envelope: Any) -> None:
    """Raise if *envelope* is not a well-formed result of this contract.

    Checked before every emit rather than trusted, because the failure mode of a
    malformed envelope is a consumer that cannot tell our bug from a crash.
    """
    if not isinstance(envelope, dict):
        raise ValueError("envelope must be a JSON object")
    missing = {"ok", "contract_version", "data", "error"} - set(envelope)
    if missing:
        raise ValueError(f"envelope is missing {sorted(missing)}")
    if not isinstance(envelope["ok"], bool):
        raise ValueError("`ok` must be a boolean")
    if not isinstance(envelope["contract_version"], int) or isinstance(
        envelope["contract_version"], bool
    ):
        raise ValueError("`contract_version` must be an integer")
    has_data = envelope["data"] is not None
    has_error = envelope["error"] is not None
    if has_data == has_error:
        raise ValueError("exactly one of `data` / `error` must be non-null")
    if envelope["ok"] != has_data:
        raise ValueError("`ok` must be true when `data` is set and false when `error` is")
    if has_error:
        error = envelope["error"]
        if not isinstance(error, dict):
            raise ValueError("`error` must be a JSON object")
        if error.get("kind") not in ERROR_KINDS:
            raise ValueError(f"error kind {error.get('kind')!r} is not one of {ERROR_KINDS}")
        if not isinstance(error.get("message"), str):
            raise ValueError("`error.message` must be a string")


# ── D7 availability wrapper ─────────────────────────────────────────────────


def available(value: Any) -> dict[str, Any]:
    """A value that was established. An empty one means, definitively, none."""
    return {"available": True, "value": value, "reason_code": None, "detail": None}


def unavailable(reason_code: str, detail: str | None = None) -> dict[str, Any]:
    """A value that could not be established. Never rendered as "none"."""
    return {"available": False, "value": None, "reason_code": reason_code, "detail": detail}


def read_json_file(path: Path) -> dict[str, Any]:
    """Read a JSON document into the availability wrapper.

    Missing, unreadable and malformed are three different facts and stay three
    different answers; collapsing them to an empty document is what makes a
    not-yet-written file indistinguishable from a corrupt one.
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return unavailable(REASON_NOT_FOUND, f"{path} does not exist")
    except OSError as exc:
        return unavailable(REASON_UNREADABLE, f"{path}: {exc.strerror or exc}")
    try:
        return available(json.loads(text))
    except json.JSONDecodeError as exc:
        return unavailable(REASON_MALFORMED, f"{path}: {exc}")


def list_directory(path: Path, *, missing_is_empty: bool = False) -> dict[str, Any]:
    """List a directory's entry names into the availability wrapper.

    *missing_is_empty* is for a directory whose absence is itself the definitive
    answer — nothing was ever written there — and is off by default, because for
    a directory the producer creates up front, absence is an anomaly rather than
    a count of zero.
    """
    try:
        names = sorted(entry.name for entry in path.iterdir())
    except FileNotFoundError:
        if missing_is_empty:
            return available([])
        return unavailable(REASON_NOT_FOUND, f"{path} does not exist")
    except NotADirectoryError:
        return unavailable(REASON_UNREADABLE, f"{path} is not a directory")
    except OSError as exc:
        return unavailable(REASON_UNREADABLE, f"{path}: {exc.strerror or exc}")
    return available(names)


# ── argument parsing on the machine channel ─────────────────────────────────


class _MachineArgumentParser(argparse.ArgumentParser):
    """An argparse parser that refuses inside the envelope instead of exiting.

    ``ArgumentParser.error`` prints usage and raises ``SystemExit``, which no
    ``except Exception`` catches — the process would end having written a usage
    message to stderr and no envelope at all, which reads to a machine caller as
    a command that stopped answering rather than one that refused.
    """

    def error(self, message: str) -> None:  # type: ignore[override]
        raise MachineError("invalid_input", message)


def machine_parser(prog: str) -> argparse.ArgumentParser:
    """A parser for one machine command's arguments.

    Deliberately separate from the human-facing parser the same command
    registers: this one is reached with ``--machine`` already stripped and
    answers only what the machine path honours, so a flag that shapes the human
    printout is refused here rather than accepted and ignored.
    """
    return _MachineArgumentParser(prog=prog, add_help=False)


def parse_machine_argv(parser: argparse.ArgumentParser, argv: list[str]) -> argparse.Namespace:
    known, extras = parser.parse_known_args(argv)
    if extras:
        raise MachineError("invalid_input", f"unrecognized arguments: {' '.join(extras)}")
    return known


def machine_subcommand(
    command: str,
    argv: list[str],
    handlers: Mapping[str, Callable[[list[str]], dict[str, Any]]],
    *,
    without_seam: Mapping[str, str],
) -> dict[str, Any]:
    """Route ``li <command> <sub>`` to the subcommand's machine payload.

    Three answers, kept apart: a name that is not a subcommand of this command
    is bad input, a real subcommand with no machine result is unavailable and
    says why, and a qualified one runs. Collapsing the middle case into "no such
    subcommand" would tell a caller the capability does not exist when what is
    missing is only this surface's route to it.

    *without_seam* carries a reason per subcommand rather than a list of names,
    because the reasons differ — one command prints prose, the next writes to
    the store — and one sentence covering both would be true of neither.
    """
    if not argv:
        raise MachineError(
            "invalid_input",
            f"li {command} needs a subcommand; these answer on the machine "
            f"channel: {', '.join(sorted(handlers))}",
        )
    sub, rest = argv[0], argv[1:]
    handler = handlers.get(sub)
    if handler is not None:
        return handler(rest)
    reason = without_seam.get(sub)
    if reason is not None:
        raise MachineError(
            "unavailable",
            f"`li {command} {sub}` has no machine result in contract version "
            f"{CONTRACT_VERSION}: {reason}",
        )
    raise MachineError("invalid_input", f"no such subcommand: {command} {sub}")


# ── the lifecycle store, opened for reading only ────────────────────────────


@asynccontextmanager
async def readonly_state_db() -> AsyncIterator[tuple[Any | None, dict[str, Any] | None]]:
    """The lifecycle store open for reading, paired with why it is not.

    Read-only at the connection, not by convention: the ordinary open reconciles
    the schema, so a reporting command that used it would write to the store it
    is reporting on.

    Exactly one of the two is set. The reason travels with the ``None`` rather
    than being reconstructed by the caller, because there is more than one way
    to arrive here: the store may not exist yet, which is a definitive statement
    that nothing has been recorded, or it may exist and refuse to open, which
    says nothing at all about what it holds. A caller handed only ``None`` has
    to guess between those, and every one of them guessed "absent".

    A failure to open is a fact about the store, so it is reported rather than
    raised. The guard covers the open alone — the caller's own body runs outside
    it, and a bug in a reader still surfaces as the crash it is.
    """
    from lionagi.state.db import StateDB, read_only_open_supported, state_db_known_absent
    from lionagi.state.engine import mask_db_url

    # Asked of the configured store, not of the default path: the open below
    # honours LIONAGI_STATE_DB_URL, so a guard that consulted the file would
    # report "nothing recorded" for a store that is full of rows.
    if state_db_known_absent():
        yield None, state_db_absent()
        return
    async with AsyncExitStack() as stack:
        try:
            db = await stack.enter_async_context(StateDB(readonly=read_only_open_supported()))
            # The engine is lazy: it connects on the first statement, so without
            # this the store's refusal would surface in the middle of a caller's
            # query, where it is indistinguishable from that query being wrong.
            # One trivial statement moves the failure to the moment this seam
            # claims the store is open, which is what the claim has to mean.
            await db.fetch_all("SELECT 1")
        except Exception as exc:  # noqa: BLE001 — an unopenable store is an answer, not a crash
            # The exception's own message is dropped here, as it always was:
            # the availability wrapper's key set is a published contract with
            # a test enumerating it, so there is nowhere to put the message
            # that does not change the shape of an answer every reader parses.
            # It is masked at its producers instead, which is where the leak
            # was; what is lost is a diagnostic, not a control.
            yield (
                None,
                unavailable(
                    REASON_UNREADABLE, f"{mask_db_url(StateDB().url)}: {type(exc).__name__}"
                ),
            )
            return
        yield db, None


def state_db_absent() -> dict[str, Any]:
    """The unavailability every store-backed reader reports when there is no store.

    Names the store that was actually consulted. Naming the default path while a
    URL is configured sends the reader to a file that is not the one their
    command would have read. Naming it in full would print its password, so the
    name it gives is the masked one.
    """
    from lionagi.state.db import StateDB
    from lionagi.state.engine import mask_db_url

    return unavailable(
        REASON_NOT_FOUND,
        f"{mask_db_url(StateDB().url)} does not exist; nothing has been recorded yet",
    )


def store_unreachable(why: dict[str, Any], subject: str) -> MachineError:
    """The refusal a detail read makes when it never reached the store.

    A detail read answers about one record, so it has no availability wrapper to
    put this in and has to refuse. `not_found` stays `not_found` — with no store
    there is definitively no such record — but a store that would not open is
    `unavailable`, because the record may well be sitting in it.
    """
    reason = why.get("reason_code")
    detail = why.get("detail")
    if reason == REASON_NOT_FOUND:
        return MachineError("not_found", f"{subject}: {detail}")
    return MachineError(
        "unavailable", f"{subject}: the lifecycle store could not be read ({detail})"
    )


# ── stdout reservation ──────────────────────────────────────────────────────


class MachineChannel:
    """The one writer of stdout for the duration of a machine call."""

    def __init__(self, out_fd: int | None, fallback: Any) -> None:
        self._out_fd = out_fd
        self._fallback = fallback
        self.emitted = False

    def emit(self, envelope: dict[str, Any]) -> None:
        if self.emitted:
            raise RuntimeError("a machine call emits exactly one envelope")
        validate_envelope(envelope)
        text = json.dumps(envelope, ensure_ascii=False) + "\n"
        self.emitted = True
        if self._out_fd is not None:
            os.write(self._out_fd, text.encode())
            return
        self._fallback.write(text)
        self._fallback.flush()


@contextmanager
def reserve_stdout() -> Iterator[MachineChannel]:
    """Point everything except the envelope at stderr, then hand back stdout.

    Two layers, because they catch different writers. Rebinding ``sys.stdout``
    catches `print` in Python code; duplicating stderr onto file descriptor 1
    catches a child process that inherited the descriptor and anything that
    writes to the descriptor directly. Only the descriptor saved here — the real
    stdout — receives the envelope, and it receives nothing else.
    """
    try:
        sys.stdout.flush()
    except (ValueError, OSError):
        pass

    saved_fd: int | None = None
    try:
        saved_fd = os.dup(1)
        os.dup2(2, 1)
    except OSError:
        # dup/dup2 fail independently, so close here rather than at the exit
        # path (which only closes a descriptor it was told to restore) or a
        # long-lived process leaks one descriptor per call.
        if saved_fd is not None:
            try:
                os.close(saved_fd)
            except OSError:
                pass
        saved_fd = None

    original_stdout = sys.stdout
    sys.stdout = sys.stderr
    channel = MachineChannel(saved_fd, original_stdout)
    try:
        yield channel
    finally:
        try:
            sys.stdout.flush()
        except (ValueError, OSError):
            pass
        sys.stdout = original_stdout
        if saved_fd is not None:
            try:
                os.dup2(saved_fd, 1)
            finally:
                os.close(saved_fd)


# ── Command payloads ────────────────────────────────────────────────────────


def handshake_data() -> dict[str, Any]:
    from lionagi.version import __version__

    from ._code_identity import code_identity

    return {
        "contract_version": CONTRACT_VERSION,
        "min_supported_version": MIN_SUPPORTED_CONTRACT_VERSION,
        "implementation": "lionagi",
        "implementation_version": __version__,
        "module": str(Path(__file__).resolve().parent),
        # Which build answered, not which build was installed: a caller with no
        # shell on this host has no other way to tell the two apart.
        "code_identity": code_identity(),
    }


def doctor_data() -> dict[str, Any]:
    from .doctor import collect_checks

    checks = collect_checks()
    return {
        "checks": checks,
        "failed": sorted(name for name, result in checks.items() if result["status"] == "fail"),
        # Reported apart from `failed` because a check that could not be run is
        # not a check that passed, and a caller that only reads `failed` would
        # otherwise treat the two the same.
        "unknown": sorted(name for name, result in checks.items() if result["status"] == "unknown"),
    }


# How many runs a machine listing returns when the caller names no bound.
_DEFAULT_RUNS_LIMIT = 20


def runs_data(limit: int = _DEFAULT_RUNS_LIMIT) -> dict[str, Any]:
    """The run listing, every read-derived part of it carrying its availability.

    Nothing here is status-bearing: it reports which runs exist on disk and what
    each one wrote, not whether any of them finished or succeeded.
    """
    # Taken from `_runs` rather than from `lionagi._paths`, so this reads the
    # same root `list_runs` below walks; two bindings of one constant is how a
    # listing comes back describing a directory nobody wrote to.
    from ._runs import RUNS_ROOT, list_runs

    listing = list_directory(RUNS_ROOT, missing_is_empty=True)
    if not listing["available"]:
        return {"runs": listing, "truncated": False, "limit": limit}

    try:
        runs = list_runs(limit=limit + 1)
    except OSError as exc:
        return {
            "runs": unavailable(REASON_UNREADABLE, f"{RUNS_ROOT}: {exc.strerror or exc}"),
            "truncated": False,
            "limit": limit,
        }

    truncated = len(runs) > limit
    entries = [
        {
            "run_id": run.run_id,
            "state_root": str(run.state_root),
            "artifact_root": str(run.artifact_root),
            # A run directory the producer created always has an artifacts dir,
            # so its absence is a fact we failed to establish, not a zero.
            "artifacts": list_directory(run.artifact_root),
        }
        for run in runs[:limit]
    ]
    return {"runs": available(entries), "truncated": truncated, "limit": limit}


# Session statuses that mean the run was stopped on purpose, and the one that
# means the work came out right. Both are read off the lifecycle vocabulary the
# StateDB owns; everything else terminal is a failure. This mapping lives here,
# on the side that owns the vocabulary, so a consumer never keeps a copy.
_CANCELLED_SESSION_STATUSES = frozenset({"cancelled", "aborted"})
_SUCCEEDED_SESSION_STATUSES = frozenset({"completed"})


def _lifecycle_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold a run's session rows into one answer about the run.

    `found` and `terminal` are separate questions and stay separate: no rows
    means nothing was ever recorded, which is not the same fact as rows that
    record no end. `terminal` needs every row to have ended, because a run that
    persisted two sessions is over only when both are.
    """
    from lionagi.state.db import SESSION_TERMINAL_STATUSES

    sessions = [
        {
            "session_id": row.get("id"),
            "status": row.get("status"),
            "terminal": row.get("status") in SESSION_TERMINAL_STATUSES,
            "started_at": row.get("started_at"),
            "ended_at": row.get("ended_at"),
            "reason_code": row.get("status_reason_code"),
            "reason_summary": row.get("status_reason_summary"),
        }
        for row in rows
    ]
    if not sessions:
        return {
            "found": False,
            "terminal": False,
            "status": None,
            "outcome": None,
            "reason_code": None,
            "reason_summary": None,
            "ended_at": None,
            "sessions": [],
        }

    terminal = all(entry["terminal"] for entry in sessions)
    if not terminal:
        governing = next(entry for entry in sessions if not entry["terminal"])
        return {
            "found": True,
            "terminal": False,
            "status": governing["status"],
            "outcome": None,
            "reason_code": governing["reason_code"],
            "reason_summary": governing["reason_summary"],
            "ended_at": None,
            "sessions": sessions,
        }

    statuses = {entry["status"] for entry in sessions}
    if statuses & _CANCELLED_SESSION_STATUSES:
        outcome = "cancelled"
        governing = next(
            entry for entry in sessions if entry["status"] in _CANCELLED_SESSION_STATUSES
        )
    elif statuses <= _SUCCEEDED_SESSION_STATUSES:
        outcome = "succeeded"
        governing = sessions[-1]
    else:
        outcome = "failed"
        governing = next(
            entry for entry in sessions if entry["status"] not in _SUCCEEDED_SESSION_STATUSES
        )
    ends = [entry["ended_at"] for entry in sessions if entry["ended_at"] is not None]
    return {
        "found": True,
        "terminal": True,
        "status": governing["status"],
        "outcome": outcome,
        "reason_code": governing["reason_code"],
        "reason_summary": governing["reason_summary"],
        "ended_at": max(ends) if ends else None,
        "sessions": sessions,
    }


def lifecycle_data(run_id: str) -> dict[str, Any]:
    """What the lifecycle store records about CLI run *run_id*.

    This is the one machine-qualified path from a run_id to the rows the
    lifecycle writers — a normal teardown, and `li kill` — actually write. It
    reads only; nothing here changes a run.

    `lifecycle` carries its own availability, and the distinction is the whole
    point: an established answer with `found: false` means no session was ever
    recorded under this id, while an unavailable one means the store could not
    be read at all. A caller that collapsed the two would report a run as
    finished, or as never started, on the strength of a database it never
    opened.
    """
    from lionagi.ln.concurrency import run_async
    from lionagi.state.db import (
        StateDB,
        read_only_open_supported,
        state_db_file,
        state_db_known_absent,
    )
    from lionagi.state.engine import mask_credentials

    if state_db_known_absent():
        # No store at all is absence of every record, not evidence about this
        # run. Checked against the store this read will actually open (which
        # moves under LIONAGI_STATE_DB_URL), not the default path.
        return {
            "run_id": run_id,
            "lifecycle": unavailable(
                REASON_NOT_FOUND,
                f"{mask_credentials(str(state_db_file()))} does not exist",
            ),
        }

    async def _read() -> list[dict[str, Any]]:
        # Read-only where the backend has it: the ordinary open reconciles the
        # schema (create_all, seed inserts), which would write to the store this
        # is reporting on. Where read-only is unavailable the ordinary open is
        # the only open there is, and those writes are the price of the read.
        async with StateDB(readonly=read_only_open_supported()) as db:
            return await db.get_sessions_for_run(run_id)

    try:
        rows = run_async(_read())
    except Exception as exc:  # noqa: BLE001 — an unreadable store is an answer, not a crash
        return {
            "run_id": run_id,
            "lifecycle": unavailable(
                REASON_UNREADABLE,
                mask_credentials(f"{type(exc).__name__}: {exc}"),
            ),
        }
    return {"run_id": run_id, "lifecycle": available(_lifecycle_summary(rows))}


# ── Machine dispatch ────────────────────────────────────────────────────────


def _machine_handshake(argv: list[str]) -> dict[str, Any]:
    _reject_extra_arguments("handshake", argv)
    return handshake_data()


def _machine_doctor(argv: list[str]) -> dict[str, Any]:
    _reject_extra_arguments("doctor", argv)
    return doctor_data()


def _machine_runs(argv: list[str]) -> dict[str, Any]:
    parser = argparse.ArgumentParser(prog="li runs", add_help=False)
    parser.add_argument(
        "--limit",
        type=int,
        default=_DEFAULT_RUNS_LIMIT,
        help=(
            "How many of the most recent runs to return, newest first. The result says "
            "whether more exist than were returned."
        ),
    )
    known, extras = parser.parse_known_args(argv)
    if extras:
        raise MachineError("invalid_input", f"unrecognized arguments: {' '.join(extras)}")
    if known.limit < 1:
        raise MachineError("invalid_input", "--limit must be at least 1")
    return runs_data(known.limit)


def _machine_schedule(argv: list[str]) -> dict[str, Any]:
    from .machine_schedule import dispatch_schedule

    return dispatch_schedule(argv)


def _machine_lifecycle(argv: list[str]) -> dict[str, Any]:
    parser = argparse.ArgumentParser(prog="li lifecycle", add_help=False)
    parser.add_argument(
        "run_id",
        nargs="?",
        help="The run id to report the recorded lifecycle state of.",
    )
    known, extras = parser.parse_known_args(argv)
    if extras:
        raise MachineError("invalid_input", f"unrecognized arguments: {' '.join(extras)}")
    if not known.run_id or not known.run_id.strip():
        raise MachineError("invalid_input", "li lifecycle needs a run id")
    return lifecycle_data(known.run_id.strip())


def _reject_extra_arguments(name: str, argv: list[str]) -> None:
    if argv:
        raise MachineError("invalid_input", f"li {name} takes no arguments: {' '.join(argv)}")


_MACHINE_COMMANDS: dict[str, Callable[[list[str]], dict[str, Any]]] = {
    "handshake": _machine_handshake,
    "doctor": _machine_doctor,
    "runs": _machine_runs,
    "schedule": _machine_schedule,
    "lifecycle": _machine_lifecycle,
}

# Commands whose machine result lives beside the command it mirrors (each
# module exposes machine_result(argv)); alias spellings map to the same
# module so e.g. `li mon --machine` reaches the same result as `li monitor`.
_MACHINE_MODULES: dict[str, str] = {
    "monitor": ".monitor",
    "mon": ".monitor",
    "stats": ".stats",
    "invoke": ".invoke",
    "dispatch": ".dispatch",
    "state": ".state",
    "team": ".team",
    "plugin": ".plugin",
}


def strip_machine_flag(argv: list[str]) -> list[str]:
    """Remove `--machine` from the tokens the dispatcher routes on.

    Only before a `--` sentinel: after it the token belongs to a prompt, and a
    prompt that happens to contain the flag must not change how it is routed.
    """
    try:
        cut = argv.index("--")
    except ValueError:
        return [arg for arg in argv if arg != "--machine"]
    head = [arg for arg in argv[:cut] if arg != "--machine"]
    return [*head, *argv[cut:]]


def has_machine_flag(argv: list[str]) -> bool:
    try:
        cut = argv.index("--")
    except ValueError:
        cut = len(argv)
    return "--machine" in argv[:cut]


def dispatch_machine(argv: list[str]) -> int:
    """Run one machine command, emit exactly one envelope, and exit 0.

    The exit status answers at the transport level only: a well-formed envelope
    is the authoritative answer, including when it reports a refusal, so encoding
    the refusal a second time in the exit status would give a consumer two
    answers to one question.

    `ModuleNotFoundError` is the exception, in both senses. When nothing has been
    allocated yet, nothing executed, and the caller must be told that by exit
    status alone rather than handed an envelope describing a request that never
    ran — so it is re-raised for the entry point to report. Once a run exists on
    disk it is an ordinary failure of that run and gets an envelope like any
    other.
    """
    from ._util import run_was_allocated

    with reserve_stdout() as channel:
        try:
            data = _run_machine_command(argv)
        except MachineError as exc:
            channel.emit(failure(exc.kind, str(exc), exc.detail))
        except ModuleNotFoundError:
            if not run_was_allocated():
                raise
            channel.emit(failure("internal", "a required module is not installed"))
        except Exception as exc:  # noqa: BLE001 — a crash with no envelope is unreadable
            from lionagi.state.engine import mask_credentials

            # The only sink that prints a message from code we do not own. The
            # producers we do own mask at the source; a driver that quotes the
            # connection string it was handed has nowhere else to be caught.
            channel.emit(failure("internal", mask_credentials(f"{type(exc).__name__}: {exc}")))
        else:
            channel.emit(ok(data))
    return 0


def _run_machine_command(argv: list[str]) -> dict[str, Any]:
    if not argv:
        raise MachineError("invalid_input", "no command given")
    name, rest = argv[0], argv[1:]
    handler = _MACHINE_COMMANDS.get(name)
    if handler is not None:
        return handler(rest)

    module_name = _MACHINE_MODULES.get(name)
    if module_name is not None:
        return import_module(module_name, __package__).machine_result(rest)

    from .main import _COMMAND_BY_NAME

    if name in _COMMAND_BY_NAME or name in ("play", "skill", "wait"):
        raise MachineError(
            "unavailable",
            f"li {name} has no machine-mode result in contract version "
            f"{CONTRACT_VERSION}; it is a human-facing command",
        )
    raise MachineError("invalid_input", f"no such command: {name}")


# ── Human-facing surfaces for the two commands this module owns ─────────────


def add_handshake_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "handshake",
        help="Report the machine-result contract version this build speaks.",
        description=(
            "The version a machine consumer negotiates against at registration. "
            "Add --machine for the contract envelope on stdout."
        ),
    )
    p.add_argument("--machine", action="store_true", help="Emit the machine-result envelope.")


@auto_register(
    area="handshake",
    cli=CliDeclaration(seed="handshake", parser_factory=add_handshake_subparser),
)
def run_handshake(args: argparse.Namespace) -> int:
    data = handshake_data()
    for key, value in data.items():
        print(f"{key}: {value}")
    return 0


def add_runs_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "runs",
        help="List recorded runs and what each one wrote.",
        description="Read-only listing of ~/.lionagi/runs, newest first.",
    )
    p.add_argument("--limit", type=int, default=_DEFAULT_RUNS_LIMIT, help="How many runs to list.")
    p.add_argument("--machine", action="store_true", help="Emit the machine-result envelope.")


@auto_register(area="runs", cli=CliDeclaration(seed="runs", parser_factory=add_runs_subparser))
def run_runs(args: argparse.Namespace) -> int:
    data = runs_data(getattr(args, "limit", _DEFAULT_RUNS_LIMIT))
    listing = data["runs"]
    if not listing["available"]:
        from ._logging import log_error

        log_error(f"could not list runs ({listing['reason_code']}): {listing['detail']}")
        return 1
    for entry in listing["value"]:
        artifacts = entry["artifacts"]
        if artifacts["available"]:
            summary = f"{len(artifacts['value'])} artifact(s)"
        else:
            summary = f"artifacts unknown ({artifacts['reason_code']})"
        print(f"{entry['run_id']}  {summary}")
    if data["truncated"]:
        print(f"(truncated at {data['limit']}; pass --limit for more)")
    return 0


def add_lifecycle_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "lifecycle",
        help="Report the recorded lifecycle state of a run.",
        description=(
            "Read-only: what the lifecycle store records about one run id — whether a "
            "session was ever recorded for it, whether every one of them has ended, and "
            "with what outcome. A run stopped by `li kill` reads as cancelled here. "
            "Add --machine for the contract envelope on stdout."
        ),
    )
    p.add_argument("run_id", help="The run id to report on.")
    p.add_argument("--machine", action="store_true", help="Emit the machine-result envelope.")


@auto_register(
    area="lifecycle", cli=CliDeclaration(seed="lifecycle", parser_factory=add_lifecycle_subparser)
)
def run_lifecycle(args: argparse.Namespace) -> int:
    from ._logging import log_error

    data = lifecycle_data(args.run_id)
    state = data["lifecycle"]
    if not state["available"]:
        log_error(f"could not read the lifecycle store ({state['reason_code']}): {state['detail']}")
        return 1
    value = state["value"]
    if not value["found"]:
        print(f"{data['run_id']}: no session recorded for this run")
        return 1
    print(f"{data['run_id']}: {value['status']}")
    print(f"terminal: {value['terminal']}")
    if value["terminal"]:
        print(f"outcome: {value['outcome']}")
    if value["reason_code"]:
        print(f"reason: {value['reason_code']} — {value['reason_summary'] or ''}".rstrip(" —"))
    for entry in value["sessions"]:
        print(f"  session {entry['session_id']}  {entry['status']}")
    return 0
