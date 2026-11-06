# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Shared job state, process metadata and entity-resolution contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from lionagi._errors import ConfigurationError
from lionagi.state.db import StateDB
from lionhub.orchestration.job import (
    EXIT_CODE_BY_STATUS,
    EXIT_CODE_ENVIRONMENT_ERROR,
    AmbiguousIdError,
    begin_invocation,
    end_invocation,
    fetch_unique_row,
    mark_run_allocated,
    resolve_entity,
    run_was_allocated,
    validate_cwd_exists,
)
from tests._id_resolution_test_support import (
    FIRST,
    SECOND,
    SHARED,
    _seed_invocation,
    _seed_session,
)
from tests._id_resolution_test_support import (
    db_path as db_path,  # noqa: F401
)
from tests._job_test_support import _no_run_allocated  # noqa: F401


async def test_a_prefix_that_fits_two_kinds_is_refused(db_path: Path):
    """Search order says where to look first, not who wins a tie.

    A prefix that fits a session and an invocation equally well has no correct
    winner, and taking the one whose table is searched earlier answers a
    question about lookup order as if it were a question about intent.
    """
    async with StateDB(db_path) as db:
        await _seed_session(db, FIRST)
        await _seed_invocation(db, SECOND)

        with pytest.raises(AmbiguousIdError) as caught:
            await resolve_entity(db, SHARED)

    message = str(caught.value)
    assert "session" in message and "invocation" in message
    assert FIRST in message and SECOND in message


async def test_a_full_id_resolves_even_when_its_prefix_is_ambiguous(db_path: Path):
    """Refusing a prefix must not spread to the id it is a prefix of."""
    async with StateDB(db_path) as db:
        await _seed_session(db, FIRST)
        await _seed_invocation(db, SECOND)

        table, entity_type, row = await resolve_entity(db, FIRST)

    assert (table, entity_type) == ("sessions", "session")
    assert row["id"] == FIRST


async def test_an_unambiguous_prefix_still_resolves(db_path: Path):
    """The refusal is about collisions, not about prefixes."""
    async with StateDB(db_path) as db:
        await _seed_session(db, FIRST)

        table, entity_type, row = await resolve_entity(db, SHARED)

    assert (table, entity_type) == ("sessions", "session")
    assert row["id"] == FIRST


async def test_a_prefix_that_fits_two_rows_of_one_kind_is_refused(db_path: Path):
    async with StateDB(db_path) as db:
        await _seed_session(db, FIRST)
        await _seed_session(db, SECOND)

        with pytest.raises(AmbiguousIdError) as caught:
            await fetch_unique_row(db, "sessions", SHARED)

    assert "session" in str(caught.value)


async def test_an_upper_cased_prefix_does_not_match_a_lower_cased_id(db_path: Path):
    """LIKE compares ASCII case-insensitively on the default backend.

    Left at that, a prefix would match ids it is not actually a prefix of,
    while the exact comparison beside it would not — the same input resolving
    one way as a whole id and another way as a prefix.
    """
    async with StateDB(db_path) as db:
        await _seed_session(db, FIRST)

        assert await fetch_unique_row(db, "sessions", SHARED.upper()) is None
        assert await resolve_entity(db, SHARED.upper()) is None


class TestValidateCwdExists:
    def test_none_is_a_noop(self):
        assert validate_cwd_exists(None) is None  # must not raise

    def test_empty_string_is_a_noop(self):
        assert validate_cwd_exists("") == ""  # must not raise

    def test_existing_directory_passes(self, tmp_path):
        assert validate_cwd_exists(str(tmp_path)) == str(tmp_path)  # must not raise

    def test_tilde_path_to_existing_dir_returns_expanded(self, tmp_path, monkeypatch):
        """A `--cwd=~/...` value must come back tilde-expanded: validating the
        expanded path while forwarding the literal would pass validation and
        then fail deep in the provider layer, which never expands `~`."""
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "proj").mkdir()
        assert validate_cwd_exists("~/proj") == str(tmp_path / "proj")

    def test_tilde_path_to_nonexistent_dir_still_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        with pytest.raises(ConfigurationError) as exc_info:
            validate_cwd_exists("~/does-not-exist-xyz")
        assert "~/does-not-exist-xyz" in str(exc_info.value)

    def test_nonexistent_path_raises_naming_path_and_flag(self, tmp_path):
        bad = str(tmp_path / "does-not-exist-xyz")
        with pytest.raises(ConfigurationError) as exc_info:
            validate_cwd_exists(bad)
        msg = str(exc_info.value)
        assert bad in msg
        assert "--cwd" in msg

    def test_path_that_is_a_file_raises_not_a_directory(self, tmp_path):
        f = tmp_path / "im-a-file.txt"
        f.write_text("x")
        with pytest.raises(ConfigurationError) as exc_info:
            validate_cwd_exists(str(f))
        msg = str(exc_info.value)
        assert str(f) in msg
        assert "not a directory" in msg

    def test_custom_flag_name_is_reflected_in_message(self, tmp_path):
        bad = str(tmp_path / "nope")
        with pytest.raises(ConfigurationError) as exc_info:
            validate_cwd_exists(bad, flag="--workspace")
        assert "--workspace" in str(exc_info.value)
        assert "--cwd" not in str(exc_info.value)


def test_environment_code_collides_with_no_run_status() -> None:
    """The whole point is distinguishability, so a collision defeats it.

    Asserted against the status map rather than a second literal: adding a
    status that happens to use 78 would otherwise silently re-merge the two
    cases this separation exists to keep apart.
    """
    assert EXIT_CODE_ENVIRONMENT_ERROR not in set(EXIT_CODE_BY_STATUS.values())


def test_an_overlapping_invocation_does_not_erase_an_allocation():
    """The reset is the dangerous operation, so it only runs when nothing is in flight.

    The two ways overlapping invocations can go wrong are not equally bad.
    Seeing another invocation's allocation only re-raises, which is what
    happened before any of this existed. Losing one asserts that nothing ran
    while a run directory sits on disk, which is the false claim the exit code
    exists to prevent, so that direction has to be impossible.
    """
    begin_invocation()
    mark_run_allocated()

    begin_invocation()  # a second invocation starts while the first is running
    assert run_was_allocated(), "the second entry erased the first one's allocation"

    end_invocation()
    end_invocation()


def test_a_finished_invocation_lets_the_next_one_reset():
    """Guarding the reset must not disable it, or the marker latches on forever.

    Once nothing is in flight the fact is stale, and a later broken environment
    in the same process has to be reported as one.
    """
    begin_invocation()
    mark_run_allocated()
    end_invocation()

    begin_invocation()
    assert not run_was_allocated()
    end_invocation()


def test_classify_exception_sigterm_interrupt_maps_to_cancelled():
    """classify_exception(SigtermInterrupt(...)) must return 'cancelled' (exit code 143)."""
    from lionagi.ln.concurrency import SigtermInterrupt
    from lionhub.orchestration.job import classify_exception

    assert classify_exception(SigtermInterrupt("received SIGTERM")) == "cancelled"


def test_classify_exception_sigterm_interrupt_distinct_from_aborted():
    """SigtermInterrupt must not be classified as 'aborted' (that bucket is SIGINT/Ctrl-C only)."""
    from lionagi.ln.concurrency import SigtermInterrupt
    from lionhub.orchestration.job import classify_exception

    assert classify_exception(SigtermInterrupt("received SIGTERM")) != "aborted"


def test_recorded_pid_is_foreign_reads_only_the_host_marker(monkeypatch: pytest.MonkeyPatch):
    """Foreignness is a property of the host field alone — keyed on a readable pid too, a foreign row with an unparseable pid would read as local."""
    from lionhub.orchestration.job import recorded_pid_is_foreign

    monkeypatch.setattr("lionhub.orchestration.job.socket.gethostname", lambda: "this-host")

    assert recorded_pid_is_foreign({"pid_host": "other-host"}) is True
    assert recorded_pid_is_foreign({"pid_host": "other-host", "pid": "not-an-int"}) is True
    assert recorded_pid_is_foreign({"pid_host": "this-host"}) is False
    assert recorded_pid_is_foreign({"pid": 1234}) is False
    assert recorded_pid_is_foreign({"pid_host": ""}) is False
    assert recorded_pid_is_foreign({"pid_host": 12}) is False
    assert recorded_pid_is_foreign(None) is False


def test_an_absent_marker_and_a_null_marker_are_not_the_same_row():
    """The two cases the guard turns on, asserted side by side — a missing key stays judgeable by other checks, a null value must come back unrecognized so every mode check refuses it."""
    from lionhub.orchestration.job import UNRECOGNIZED_IDENTITY_MODE, recorded_identity_mode

    assert recorded_identity_mode({}) is None
    assert recorded_identity_mode({"pid": 123}) is None
    assert recorded_identity_mode({"process_identity_mode": None}) == UNRECOGNIZED_IDENTITY_MODE
    assert recorded_identity_mode({"process_identity_mode": 7}) == UNRECOGNIZED_IDENTITY_MODE
    assert recorded_identity_mode({"process_identity_mode": "local"}) == "local"
    assert recorded_identity_mode(None) is None
