# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The async PostgreSQL Node adapter: table creation on write, and registration by the core."""

from unittest.mock import AsyncMock

import pytest


def _async_postgres_module():
    # The module the adapter subclasses, not one of its dependencies: sqlalchemy is present
    # without the extra, so guarding on it lets the factory's import raise.
    return pytest.importorskip(
        "pydapter.extras.async_postgres_", reason="requires lionagi[postgres] extra"
    )


async def test_to_obj_ensures_table_for_dsn_before_delegating(monkeypatch):
    AsyncPostgresAdapter = _async_postgres_module().AsyncPostgresAdapter

    from lionagi.protocols.graph.node import Node
    from lionhub.integrations.postgres_adapter import create_lionagi_async_postgres_adapter

    LionAGIAsyncPostgresAdapter = create_lionagi_async_postgres_adapter()

    ensure_mock = AsyncMock()
    parent_to_obj_mock = AsyncMock(return_value="ok")

    monkeypatch.setattr(LionAGIAsyncPostgresAdapter, "_ensure_table", ensure_mock)
    monkeypatch.setattr(AsyncPostgresAdapter, "to_obj", parent_to_obj_mock)

    node = Node()
    result = await LionAGIAsyncPostgresAdapter.to_obj(
        node,
        table="nodes",
        dsn="postgresql+asyncpg://u:p@h/db",
    )

    ensure_mock.assert_awaited_once_with("postgresql+asyncpg://u:p@h/db", "nodes")
    assert result == "ok"


def test_the_core_registers_the_hub_adapter_under_its_key(monkeypatch):
    _async_postgres_module()

    from lionagi.protocols.graph.node import Node, _ensure_postgres_adapter

    registered = []
    monkeypatch.setattr(Node, "_postgres_adapter_checked", False, raising=False)
    monkeypatch.setattr(Node, "register_async_adapter", registered.append)

    _ensure_postgres_adapter()

    assert [adapter.obj_key for adapter in registered] == ["lionagi_async_pg"]
    assert registered[0].__module__ == "lionhub.integrations.postgres_adapter"
    assert Node._postgres_adapter_checked is True
