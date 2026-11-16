# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Progression-linked writes reject invalid message IDs before opening a transaction."""

import pytest
import pytest_asyncio

from lionagi.state.compat import _operation
from lionagi.state.db import StateDB
from tests.state.test_compatibility_owner_ops import (
    _live_message,
    _managed_store,
    _profile,
    _setup_run,
)


class _ScopedBatchStateDB(StateDB):
    @_operation("insert_message")
    async def insert_owned_batch(self, messages, *, progression_ids):
        # Compatibility mode requires an existing write scope for a batch.
        await self.insert_messages(messages, progression_ids=progression_ids)


@pytest_asyncio.fixture(params=["native", "compat"])
async def admitted_store(request, tmp_path):
    path = tmp_path / "state.db"
    options = {}
    if request.param == "compat":
        await _managed_store(path)
        options = {"compatibility": _profile(path), "owner": "principal:a"}
    async with _ScopedBatchStateDB(path, **options) as db:
        assert (db.compatibility is not None) is (request.param == "compat")
        await _setup_run(db)
        yield db


async def _write(db, entry_point, message_id, *, attach_progressions=True):
    message = {**_live_message(), "id": message_id}
    if entry_point == "live":
        await db._persist_live_message(
            message,
            session_id="s1",
            branch_progression_id="bp" if attach_progressions else None,
            session_progression_id="sp" if attach_progressions else None,
        )
    else:
        await db.insert_owned_batch(
            [message], progression_ids=("bp", "sp") if attach_progressions else ()
        )


@pytest.mark.parametrize("entry_point", ["live", "batch"])
@pytest.mark.parametrize("message_id", [12, "", None], ids=["integer", "empty", "null"])
async def test_invalid_message_id_does_not_enter_transaction(
    admitted_store, entry_point, message_id, monkeypatch
):
    def unexpected_transaction():
        pytest.fail("invalid message ID reached StateDB._tx")

    with monkeypatch.context() as patched:
        patched.setattr(admitted_store, "_tx", unexpected_transaction)
        with pytest.raises(ValueError, match=r"progression 'bp' requires non-empty string"):
            await _write(admitted_store, entry_point, message_id)

    assert await admitted_store.get_message(str(message_id)) is None
    assert await admitted_store.get_progression("bp") == []
    assert await admitted_store.get_progression("sp") == []


@pytest.mark.parametrize("entry_point", ["live", "batch"])
async def test_valid_message_id_is_listed_in_both_progressions(admitted_store, entry_point):
    await _write(admitted_store, entry_point, "m1")

    assert (await admitted_store.get_message("m1"))["id"] == "m1"
    assert await admitted_store.get_progression("bp") == ["m1"]
    assert await admitted_store.get_progression("sp") == ["m1"]


@pytest.mark.parametrize("entry_point", ["live", "batch"])
async def test_message_without_progression_keeps_existing_empty_id_behavior(
    admitted_store, entry_point
):
    await _write(admitted_store, entry_point, "", attach_progressions=False)

    assert (await admitted_store.get_message(""))["id"] == ""
    assert await admitted_store.get_progression("bp") == []
    assert await admitted_store.get_progression("sp") == []
