# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""
Async PostgreSQL adapter for lionagi Nodes.

This module requires the ``lionagi[postgres]`` extra (pydapter[postgres],
sqlalchemy, asyncpg).  It is loaded lazily — only when a caller explicitly
uses the ``lionagi_async_pg`` adapter key via ``_ensure_postgres_adapter()``
in node.py.

The base class (AsyncPostgresAdapter) is sourced from pydapter's extras
because it provides a complete async SQLAlchemy/asyncpg write stack.  Only
pydapter[postgres] carries that dependency; it is NOT part of the core install.
"""

from __future__ import annotations

from typing import ClassVar, TypeVar

from lionagi.adapters._base import AsyncAdapter

T = TypeVar("T")


def create_lionagi_async_postgres_adapter() -> type[AsyncAdapter]:
    """Build the LionAGIAsyncPostgresAdapter class.

    This factory is intentionally deferred — calling it requires pydapter's
    async_postgres extras (sqlalchemy, asyncpg).  It is invoked lazily by
    ``_ensure_postgres_adapter()`` in node.py, never at import time.
    """
    from pydapter.extras.async_postgres_ import AsyncPostgresAdapter

    class LionAGIAsyncPostgresAdapter(AsyncPostgresAdapter[T]):
        """
        Streamlined async adapter for lionagi Nodes.

        Features:
        - Auto-creates tables with lionagi schema
        - Inherits all pydapter v1.0.4+ improvements (SQL write stack)
        - No workarounds needed for SQLite or raw SQL
        """

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


# LionAGIAsyncPostgresAdapter is NOT constructed at import time.
# Use create_lionagi_async_postgres_adapter() when the postgres extra is available.
# The _ensure_postgres_adapter() function in node.py handles the lazy construction.
#
# For test patching purposes, a sentinel attribute is exposed so that
# patch("lionagi.adapters.async_postgres_adapter.LionAGIAsyncPostgresAdapter")
# resolves without triggering the factory.
LionAGIAsyncPostgresAdapter = None  # populated lazily by _ensure_postgres_adapter()

__all__ = ("LionAGIAsyncPostgresAdapter", "create_lionagi_async_postgres_adapter")
