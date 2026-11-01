# SPDX-License-Identifier: Apache-2.0
"""The attempt table's two rejection columns: creation, upgrade of an older ledger and settlement."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from lionagi.service.accounts import _ledger
from lionagi.service.accounts._ledger import CredentialLedger
from lionagi.service.accounts._types import (
    _PROVIDER_ERROR_TYPES,
    _REJECTION_CLASSES,
    AccountSpec,
    CompleteUsage,
    ProvenNotSent,
    Quote,
    Scope,
    UnknownOutcome,
)

NEW_COLUMNS = ("provider_error_type", "rejection_class")
ERROR_TYPE = min(_PROVIDER_ERROR_TYPES)
REJECTION_CLASS = min(_REJECTION_CLASSES)
SENTINEL = "sentinel-outside-every-set"
NOW = datetime(2026, 1, 15, tzinfo=timezone.utc)

needs_drop_column = pytest.mark.skipif(
    sqlite3.sqlite_version_info < (3, 35, 0),
    reason="ALTER TABLE ... DROP COLUMN needs SQLite 3.35 or newer",
)


def make_ledger(path: Path) -> CredentialLedger:
    return CredentialLedger(path, clock=lambda: NOW)


async def open_attempt(ledger: CredentialLedger) -> UUID:
    attempt_id = uuid4()
    await ledger.reserve(
        Scope("branch", uuid4(), "anthropic"),
        attempt_id,
        Quote("model-a", 8, "price-a", "a" * 64),
        (AccountSpec("alpha", "ALPHA_KEY", 10),),
    )
    return attempt_id


def attempt_columns(path: Path) -> list[str]:
    with closing(sqlite3.connect(path)) as connection:
        return [row[1] for row in connection.execute('PRAGMA table_info("attempt")')]


def read_fields(path: Path, attempt_id: UUID) -> tuple[object, ...] | None:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT outcome, provider_error_type, rejection_class FROM attempt WHERE id = ?",
            (str(attempt_id),),
        ).fetchone()


def drop_columns(path: Path, *names: str) -> None:
    """Remove the named columns that are present, as a ledger from before they existed lacks them."""
    present = attempt_columns(path)
    with closing(sqlite3.connect(path)) as connection:
        for name in names:
            if name in present:
                connection.execute(f'ALTER TABLE attempt DROP COLUMN "{name}"')
        connection.commit()


def add_columns_elsewhere(path: Path) -> None:
    """What another process opening the same older ledger does first."""
    with closing(sqlite3.connect(path)) as connection:
        for name in NEW_COLUMNS:
            connection.execute(f'ALTER TABLE attempt ADD COLUMN "{name}" TEXT')
        connection.commit()


def user_version(path: Path) -> int:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("PRAGMA user_version").fetchone()[0]


def assert_each_column_once(path: Path) -> None:
    columns = attempt_columns(path)
    for name in NEW_COLUMNS:
        assert columns.count(name) == 1, columns


# --- a new ledger and what settlement writes ---------------------------------------------------


@pytest.mark.anyio
async def test_a_new_ledger_attempt_table_lists_both_columns(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    await make_ledger(path).status("anthropic")

    with closing(sqlite3.connect(path)) as connection:
        info = {row[1]: row for row in connection.execute('PRAGMA table_info("attempt")')}
    for name in NEW_COLUMNS:
        assert name in info, sorted(info)
        assert info[name][2] == "TEXT"
        assert info[name][3] == 0


@pytest.mark.anyio
async def test_a_rejected_attempt_keeps_its_reason_and_both_fields(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    ledger = make_ledger(path)
    attempt_id = await open_attempt(ledger)
    expected = ("request_rejected", ERROR_TYPE, REJECTION_CLASS)

    rejected = UnknownOutcome("request_rejected", ERROR_TYPE, REJECTION_CLASS)
    assert await ledger.settle(attempt_id, rejected) == "still_open"
    assert read_fields(path, attempt_id) == expected

    assert await ledger.settle(attempt_id, UnknownOutcome("cancelled")) == "still_open"
    assert read_fields(path, attempt_id) == expected


@pytest.mark.anyio
async def test_the_first_reason_stands_and_a_later_rejection_writes_no_fields(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    ledger = make_ledger(path)
    attempt_id = await open_attempt(ledger)

    assert await ledger.settle(attempt_id, UnknownOutcome("rate_limited")) == "still_open"
    rejected = UnknownOutcome("request_rejected", ERROR_TYPE, REJECTION_CLASS)
    assert await ledger.settle(attempt_id, rejected) == "still_open"

    assert read_fields(path, attempt_id) == ("rate_limited", None, None)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "evidence",
    [
        pytest.param(CompleteUsage("model-a", 1, 2, 3, 4, 5, 4, None), id="complete"),
        pytest.param(ProvenNotSent(), id="not_sent"),
        pytest.param(UnknownOutcome("timeout"), id="reason_without_fields"),
        pytest.param(UnknownOutcome("request_rejected"), id="rejected_without_fields"),
    ],
)
async def test_other_settlements_leave_both_columns_null(tmp_path, evidence):
    path = tmp_path / "ledger.sqlite3"
    ledger = make_ledger(path)
    attempt_id = await open_attempt(ledger)

    await ledger.settle(attempt_id, evidence)

    row = read_fields(path, attempt_id)
    assert row[0] is not None
    assert row[1:] == (None, None)


# --- an older ledger ---------------------------------------------------------------------------


@needs_drop_column
@pytest.mark.anyio
async def test_an_older_ledger_gains_both_columns_and_keeps_its_rows(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    attempt_id = await open_attempt(make_ledger(path))
    drop_columns(path, *NEW_COLUMNS)
    assert not set(NEW_COLUMNS) & set(attempt_columns(path))

    reopened = make_ledger(path)
    await reopened.status("anthropic")

    assert_each_column_once(path)
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute(
            "SELECT state, ceiling_nusd, outcome FROM attempt WHERE id = ?", (str(attempt_id),)
        ).fetchone()
    assert row == ("open", 8, None)
    assert user_version(path) == 1

    rejected = UnknownOutcome("request_rejected", ERROR_TYPE, REJECTION_CLASS)
    assert await reopened.settle(attempt_id, rejected) == "still_open"
    assert read_fields(path, attempt_id) == ("request_rejected", ERROR_TYPE, REJECTION_CLASS)


@needs_drop_column
@pytest.mark.anyio
async def test_a_second_ledger_object_opens_a_ledger_the_first_already_completed(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    await make_ledger(path).status("anthropic")
    drop_columns(path, *NEW_COLUMNS)

    await make_ledger(path).status("anthropic")
    await make_ledger(path).status("anthropic")

    assert_each_column_once(path)
    assert user_version(path) == 1


@needs_drop_column
@pytest.mark.anyio
async def test_the_columns_are_read_again_under_the_lock_before_they_are_added(
    tmp_path, monkeypatch
):
    path = tmp_path / "ledger.sqlite3"
    await make_ledger(path).status("anthropic")
    drop_columns(path, *NEW_COLUMNS)
    real_transaction = _ledger._transaction
    entered: list[bool] = []

    @contextmanager
    def racing_transaction(connection: sqlite3.Connection) -> Iterator[None]:
        # Another process completes the ledger after this one's unlocked check and before
        # this one holds the write lock.
        if not entered:
            entered.append(True)
            add_columns_elsewhere(path)
        with real_transaction(connection):
            yield

    monkeypatch.setattr(_ledger, "_transaction", racing_transaction)

    await make_ledger(path).status("anthropic")

    assert entered, "an incomplete ledger must reach the locked path"
    assert_each_column_once(path)
    assert user_version(path) == 1


@needs_drop_column
@pytest.mark.anyio
@pytest.mark.parametrize("missing", NEW_COLUMNS)
async def test_a_ledger_missing_one_column_is_not_passed_by_the_fast_path(tmp_path, missing):
    path = tmp_path / "ledger.sqlite3"
    await make_ledger(path).status("anthropic")
    drop_columns(path, missing)
    with closing(sqlite3.connect(path)) as connection:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert _ledger._TABLES | {"warm_prefix"} <= tables
    assert {"warm_prefix_cert", "warm_prefix_expiry"} <= indexes

    await make_ledger(path).status("anthropic")

    assert_each_column_once(path)
    assert user_version(path) == 1


# --- the ledger's own check of the two values --------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("field", NEW_COLUMNS)
async def test_the_ledger_refuses_a_value_outside_its_set(tmp_path, field):
    path = tmp_path / "ledger.sqlite3"
    ledger = make_ledger(path)
    attempt_id = await open_attempt(ledger)
    evidence = UnknownOutcome("request_rejected", ERROR_TYPE, REJECTION_CLASS)
    object.__setattr__(evidence, field, SENTINEL)

    with pytest.raises(ValueError) as caught:
        await ledger.settle(attempt_id, evidence)

    assert SENTINEL not in str(caught.value)
    assert read_fields(path, attempt_id) == (None, None, None)
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute("SELECT * FROM attempt WHERE id = ?", (str(attempt_id),)).fetchone()
    assert all(SENTINEL not in str(value) for value in row)
