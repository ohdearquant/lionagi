# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for state-backend tests."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import os
import re
from concurrent.futures import ThreadPoolExecutor

import pytest

# The CI setup action checks this capability in the test checkout before provisioning.
RUNNER_POSTGRES_PER_WORKER = True


def _runner_database_url(url, run_token, worker):
    """Give each worker using the runner-local server its own database."""
    if not run_token:
        return url

    from sqlalchemy.engine import make_url

    worker = worker or "main"
    if not re.fullmatch(r"[0-9a-f]{16}", run_token) or not re.fullmatch(r"main|gw[0-9]+", worker):
        raise ValueError("invalid PostgreSQL test run or worker identifier")
    parsed = make_url(url)
    name = f"{parsed.database}_{run_token}_{worker}"
    if not parsed.database or not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name):
        raise ValueError("invalid PostgreSQL test database name")
    return parsed.set(database=name).render_as_string(hide_password=False)


async def _manage_runner_database(admin_url, worker_url, *, create):
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine

    name = make_url(worker_url).database
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            identifier = conn.dialect.identifier_preparer.quote_identifier(name)
            if create:
                await conn.exec_driver_sql(f"CREATE DATABASE {identifier}")
            else:
                await conn.exec_driver_sql(f"DROP DATABASE {identifier} WITH (FORCE)")
    finally:
        await engine.dispose()


def _set_up_runner_database(admin_url, worker_url, *, create):
    """Create or drop the worker database on a thread with its own event loop.

    ``pg_url`` can be set up while an async test's event loop is already
    running, where ``asyncio.run`` refuses to start a second loop.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(
            asyncio.run, _manage_runner_database(admin_url, worker_url, create=create)
        ).result()


@pytest.fixture(scope="session")
def pg_url():
    """A live Postgres URL: env override, else a testcontainers-provisioned PG.

    Skipped locally when neither is available; a hard failure under CI so the
    Postgres leg can never silently no-op on the runner.
    """

    def _unavailable(msg: str) -> None:
        if os.environ.get("CI"):
            pytest.fail(f"Postgres backend unavailable in CI: {msg}")
        pytest.skip(msg)

    def _require_driver(driver: str) -> None:
        """A URL alone does not make Postgres reachable; the driver has to import.

        This is the guard that was missing. The fixture checked that the container
        could start and never that the driver existed, so the import fired inside
        SQLAlchemy in the test body instead: these tests skipped when Docker was
        busy and failed with a bare `ModuleNotFoundError` when it was free.
        Whether the suite reported a skip or a failure came down to whether a port
        happened to be available, which makes every "was this failure already
        here?" judgment against this suite unsound.
        """
        try:
            importlib.import_module(driver)
        except ImportError as exc:
            _unavailable(f"{driver} not installed: {exc}")

    env_url = os.environ.get("LIONAGI_TEST_PG_URL")
    if env_url:
        # Normalised with the project's own function, then guarded on whatever
        # driver the normalised URL names. Deriving the driver from the raw string
        # would guard the wrong one: `normalize_state_db_url` rewrites a bare
        # `postgres://` or `postgresql://` to `postgresql+asyncpg://`, so a bare
        # alias needs asyncpg here even though SQLAlchemy's own default for that
        # scheme is psycopg2. Guarding on psycopg2 would admit the override on a
        # machine that has it and asyncpg missing, and the tests would fail later
        # on the driver that is actually used.
        #
        # Normalising the yielded URL rather than only inspecting it also settles a
        # disagreement between this fixture's two kinds of consumer: StateBD-backed
        # tests normalise the URL themselves, while others hand it straight to
        # `create_async_engine`, where a bare `postgres://` is not a dialect at all
        # and raises `NoSuchModuleError`. Both now receive the same fully qualified
        # async URL.
        from lionagi.state.engine import normalize_state_db_url

        url = normalize_state_db_url(env_url)
        scheme = url.split("://", 1)[0]
        # The dialect is checked as well as the driver, so a URL for some other
        # database is refused as one rather than sent to import whatever driver it
        # names: `mysql+aiomysql://` would otherwise skip on a missing aiomysql and
        # read as a Postgres availability problem.
        if not scheme.startswith("postgresql+"):
            _unavailable(f"not a Postgres URL this suite can drive: {env_url!r}")
        _require_driver(scheme.split("+", 1)[1])
        worker_url = _runner_database_url(
            url, os.environ.get("LIONAGI_TEST_PG_RUNNER"), os.environ.get("PYTEST_XDIST_WORKER")
        )
        if worker_url == url:
            yield url
            return
        _set_up_runner_database(url, worker_url, create=True)
        try:
            yield worker_url
        finally:
            _set_up_runner_database(url, worker_url, create=False)
        return

    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError as exc:
        _unavailable(f"testcontainers not installed: {exc}")

    # Checked before the container starts rather than after: pulling an image and
    # waiting for readiness only to skip on a missing import wastes the slowest
    # part of the run.
    _require_driver("asyncpg")

    pg = None
    try:
        pg = PostgresContainer("postgres:16-alpine", driver="asyncpg")
        pg.start()
    except Exception as exc:  # Docker down / image pull / readiness failure
        if pg is not None:  # container may have started before readiness failed
            with contextlib.suppress(Exception):
                pg.stop()
        _unavailable(f"could not start Postgres container: {exc}")

    try:
        yield pg.get_connection_url()
    finally:
        pg.stop()
