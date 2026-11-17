# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Validate progression writes and compatibility operation guards."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio

from lionagi.state.compat import StateCompatibilityRefused
from lionagi.state.db import StateDB
from tests.state.test_compatibility_owner_ops import _managed_store, _profile, _system_message

INVALID_COLLECTIONS = pytest.mark.parametrize(
    "collection",
    [[None], [""], [12]],
    ids=["null-element", "empty-string-element", "integer-element"],
)


@pytest_asyncio.fixture(params=["native", "managed"])
async def progression_db(tmp_path, request):
    if request.param == "native":
        db = StateDB(tmp_path / "state.db")
    else:
        path = await _managed_store(tmp_path / "managed.db")
        db = StateDB(path, compatibility=_profile(path), owner="principal:a")
    async with db:
        yield db


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "collection",
    [[None], [""], [12], 0, "a", {"a": "b"}],
    ids=["null-element", "owned-empty-id", "integer-element", "zero", "string", "mapping"],
)
async def test_create_rejects_invalid_collections_before_write(
    progression_db, monkeypatch, collection
):
    progression = "invalid-new-progression"
    await progression_db.insert_message(_system_message(""))
    await progression_db.insert_message(_system_message("a"))

    def unexpected_transaction():
        pytest.fail("invalid collection reached the write transaction")

    with monkeypatch.context() as patch:
        patch.setattr(progression_db, "_tx", unexpected_transaction)
        with pytest.raises(ValueError, match=progression):
            await progression_db.create_progression(progression, collection)
    row = await progression_db.fetch_one("SELECT id FROM progressions WHERE id = ?", (progression,))
    assert row is None


@pytest.mark.asyncio
@INVALID_COLLECTIONS
async def test_create_rejects_invalid_elements_even_if_key_exists(tmp_path, collection):
    progression = "existing-progression"
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_progression(progression, ["original"])
        with pytest.raises(ValueError) as error:
            await db.create_progression(progression, collection)
        assert progression in str(error.value)
        assert await db.get_progression(progression) == ["original"]


@pytest.mark.asyncio
@INVALID_COLLECTIONS
async def test_set_rejects_invalid_elements_without_changing_collection(tmp_path, collection):
    progression = "set-progression"
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_progression(progression, ["original"])
        with pytest.raises(ValueError) as error:
            await db.set_progression(progression, collection)
        assert progression in str(error.value)
        assert await db.get_progression(progression) == ["original"]


@pytest.mark.asyncio
async def test_normal_string_collections_and_empty_list_still_work(tmp_path):
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_progression("valid-progression", ["first", "second"])
        assert await db.get_progression("valid-progression") == ["first", "second"]
        await db.set_progression("valid-progression", ["third", "fourth"])
        assert await db.get_progression("valid-progression") == ["third", "fourth"]
        await db.set_progression("valid-progression", [])
        assert await db.get_progression("valid-progression") == []
        await db.create_progression("empty-progression")
        assert await db.get_progression("empty-progression") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("collection", ["single", b"bytes", {"key": "value"}, 0])
@pytest.mark.parametrize("method", ["create", "set"])
async def test_native_writer_rejects_non_sequence_containers(tmp_path, collection, method):
    progression = "invalid-container"
    async with StateDB(tmp_path / "state.db") as db:
        if method == "set":
            await db.create_progression(progression, ["original"])
        with pytest.raises(ValueError) as error:
            if method == "create":
                await db.create_progression(progression, collection)
            else:
                await db.set_progression(progression, collection)
        assert progression in str(error.value)
        if method == "set":
            assert await db.get_progression(progression) == ["original"]
        else:
            assert (
                await db.fetch_one("SELECT id FROM progressions WHERE id = ?", (progression,))
                is None
            )


@pytest.mark.asyncio
async def test_set_rejects_none_but_create_none_keeps_default(tmp_path):
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_progression("none-default", None)
        assert await db.get_progression("none-default") == []
        with pytest.raises(ValueError) as error:
            await db.set_progression("none-default", None)
        assert "none-default" in str(error.value)
        assert await db.get_progression("none-default") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("collection", [["first", "second"], ("first", "second")])
async def test_native_writer_accepts_string_lists_and_tuples(tmp_path, collection):
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_progression("sequence", collection)
        assert await db.get_progression("sequence") == ["first", "second"]
        await db.set_progression("sequence", collection)
        assert await db.get_progression("sequence") == ["first", "second"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["create", "set"])
async def test_native_writer_snapshots_collection_before_waiting_for_transaction(
    tmp_path, monkeypatch, method
):
    progression = "snapshot-progression"
    async with StateDB(tmp_path / "state.db") as db:
        if method == "set":
            await db.create_progression(progression, ["previous"])
        original_tx = db._tx
        entered, release = asyncio.Event(), asyncio.Event()

        @asynccontextmanager
        async def blocked_tx():
            entered.set()
            await release.wait()
            async with original_tx() as conn:
                yield conn

        monkeypatch.setattr(db, "_tx", blocked_tx)
        collection = ["safe-first", "safe-second"]
        if method == "create":
            operation = db.create_progression(progression, collection)
        else:
            operation = db.set_progression(progression, collection)
        task = asyncio.create_task(operation)
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            collection[:] = [None, "mutated-after-entry"]
        finally:
            release.set()
        await task
        assert await db.get_progression(progression) == ["safe-first", "safe-second"]


@pytest.mark.asyncio
@pytest.mark.parametrize("message_id", [None, "", 12], ids=["null", "empty", "integer"])
async def test_append_rejects_invalid_element_without_changing_collection(tmp_path, message_id):
    progression = "append-progression"
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_progression(progression, ["original"])
        with pytest.raises(ValueError) as error:
            await db.append_to_progression(progression, message_id)
        assert progression in str(error.value)
        assert await db.get_progression(progression) == ["original"]


@pytest.mark.asyncio
async def test_append_keeps_ordinary_order_and_idempotence(tmp_path):
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_progression("append-valid", ["first"])
        await db.append_to_progression("append-valid", "second")
        await db.append_to_progression("append-valid", "second")
        assert await db.get_progression("append-valid") == ["first", "second"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "collection, expected",
    [
        (None, []),
        (["second", "first"], ["second", "first"]),
        (("second", "first"), ["second", "first"]),
    ],
    ids=["default", "list", "tuple"],
)
async def test_create_preserves_valid_collections(progression_db, collection, expected):
    await progression_db.insert_message(_system_message("first"))
    await progression_db.insert_message(_system_message("second"))
    await progression_db.create_progression("valid-sequence", collection)
    assert await progression_db.get_progression("valid-sequence") == expected


@pytest.mark.asyncio
async def test_create_checks_and_writes_the_same_snapshot(progression_db):
    class ChangingCollection(list):
        passes = 0

        def __iter__(self):
            self.passes += 1
            original = tuple(super().__iter__())
            self[:] = ["first"]
            return iter(original)

    await progression_db.insert_message(_system_message("first"))
    await progression_db.insert_message(_system_message("second"))
    collection = ChangingCollection(["second", "first"])
    await progression_db.create_progression("snapshot", collection)
    assert await progression_db.get_progression("snapshot") == ["second", "first"]
    assert collection.passes == 1


@pytest.mark.asyncio
async def test_compat_create_preserves_operation_refusal_before_validation(tmp_path):
    path = await _managed_store(tmp_path / "managed.db")
    profile = _profile(path, operations={"insert_message"})
    async with StateDB(path, compatibility=profile, owner="principal:a") as db:
        with pytest.raises(StateCompatibilityRefused, match="profile does not name this operation"):
            await db.create_progression("compat-undeclared", [None])
        row = await db.fetch_one("SELECT id FROM progressions WHERE id = ?", ("compat-undeclared",))
        assert row is None


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["set", "append"])
async def test_compat_direct_write_preserves_missing_operation_scope_refusal(tmp_path, method):
    path = await _managed_store(tmp_path / "managed.db")
    async with StateDB(path, compatibility=_profile(path), owner="principal:a") as db:
        await db.create_progression("compat-owned")
        with pytest.raises(StateCompatibilityRefused, match="inside an operation"):
            if method == "set":
                await db.set_progression("compat-owned", [None])
            else:
                await db.append_to_progression("compat-owned", None)
        assert await db.get_progression("compat-owned") == []
