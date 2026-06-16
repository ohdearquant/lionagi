# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Async PostgreSQL adapter for lionagi Nodes; requires the ``lionagi[postgres]`` extra, loaded lazily."""

from __future__ import annotations

from typing import ClassVar, TypeVar

from lionagi.adapters._base import AsyncAdapter

T = TypeVar("T")


def create_lionagi_async_postgres_adapter() -> type[AsyncAdapter]:
    """Build the LionAGIAsyncPostgresAdapter class (requires the postgres extra)."""
    from pydapter.extras.async_postgres_ import AsyncPostgresAdapter

    class LionAGIAsyncPostgresAdapter(AsyncPostgresAdapter[T]):
        """Async adapter for lionagi Nodes; auto-creates the table on write."""

        obj_key: ClassVar[str] = "lionagi_async_pg"

        @classmethod
        async def to_obj(
            cls,
            subj,
            /,
            *,
            many: bool = True,
            adapt_meth: str = None,
            **kw,
        ):
            """Write lionagi Node(s) to database with auto-table creation."""
            if table := kw.get("table"):
                if engine_url := (kw.get("dsn") or kw.get("engine_url")):
                    await cls._ensure_table(engine_url, table)
                elif engine := kw.get("engine"):
                    await cls._ensure_table(engine, table)

            return await super().to_obj(subj, many=many, adapt_meth=adapt_meth, **kw)

        @classmethod
        async def _ensure_table(cls, engine_or_url, table_name: str):
            """Create table with lionagi schema if it doesn't exist."""
            import sqlalchemy as sa
            from sqlalchemy.ext.asyncio import create_async_engine

            should_dispose = False
            if isinstance(engine_or_url, str):
                engine = create_async_engine(engine_or_url, future=True)
                should_dispose = True
            else:
                engine = engine_or_url

            try:
                async with engine.begin() as conn:
                    engine_url = str(engine.url)
                    json_type = (
                        sa.dialects.postgresql.JSONB if "postgresql" in engine_url else sa.JSON
                    )
                    await conn.run_sync(
                        lambda sync_conn: sa.Table(
                            table_name,
                            sa.MetaData(),
                            sa.Column("id", sa.String, primary_key=True),
                            sa.Column("content", json_type),
                            sa.Column("node_metadata", json_type),
                            sa.Column("created_at", sa.Float),
                            sa.Column("embedding", json_type, nullable=True),
                        ).create(sync_conn, checkfirst=True)
                    )
            finally:
                if should_dispose:
                    await engine.dispose()

    return LionAGIAsyncPostgresAdapter


# Built lazily by _ensure_postgres_adapter() in node.py, not at import time. The
# module-level name stays defined so test patching of this attribute resolves
# without triggering the factory.
LionAGIAsyncPostgresAdapter = None  # populated lazily by _ensure_postgres_adapter()

__all__ = ("LionAGIAsyncPostgresAdapter", "create_lionagi_async_postgres_adapter")
