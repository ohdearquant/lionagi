# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi async postgres adapter and availability check."""


# ---------------------------------------------------------------------------
# A9 / A10: check_async_postgres_available
# ---------------------------------------------------------------------------


def test_check_async_postgres_available_reports_missing_optional_dependency(
    monkeypatch,
):
    import lionagi.utils as utils_mod

    monkeypatch.setattr(utils_mod, "is_import_installed", lambda pkg: pkg != "asyncpg")

    from lionagi.adapters._utils import check_async_postgres_available

    result = check_async_postgres_available()
    assert isinstance(result, ImportError)
    assert "lionagi[postgres]" in str(result)


def test_check_async_postgres_available_true_when_dependencies_present(monkeypatch):
    import lionagi.utils as utils_mod

    monkeypatch.setattr(utils_mod, "is_import_installed", lambda pkg: True)

    from lionagi.adapters._utils import check_async_postgres_available

    result = check_async_postgres_available()
    assert result is True


# ---------------------------------------------------------------------------
# A11: to_obj calls _ensure_table before delegating to parent
# ---------------------------------------------------------------------------


async def test_async_postgres_to_obj_ensures_table_for_dsn_before_delegating(
    monkeypatch,
):
    from unittest.mock import AsyncMock

    from pydapter.extras.async_postgres_ import AsyncPostgresAdapter

    from lionagi.adapters.async_postgres_adapter import LionAGIAsyncPostgresAdapter
    from lionagi.protocols.graph.node import Node

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
