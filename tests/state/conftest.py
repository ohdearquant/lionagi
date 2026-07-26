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
        # Whatever driver the caller's URL names, not the one the container path
        # happens to use. A bare `postgresql://` is psycopg2 by SQLAlchemy's own
        # default, so that is what gets checked.
        scheme = env_url.split("://", 1)[0]
        _require_driver(scheme.split("+", 1)[1] if "+" in scheme else "psycopg2")
        yield env_url
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
