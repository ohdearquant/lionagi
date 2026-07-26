# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for state-backend tests."""

from __future__ import annotations

import contextlib
import importlib
import os

import pytest


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
        yield url
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
