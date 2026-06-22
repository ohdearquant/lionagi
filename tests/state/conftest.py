# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for state-backend tests."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session")
def pg_url():
    """A live Postgres URL: env override, else a testcontainers-provisioned PG.

    Skipped locally when neither is available; a hard failure under CI so the
    Postgres leg can never silently no-op on the runner.
    """
    env_url = os.environ.get("LIONAGI_TEST_PG_URL")
    if env_url:
        yield env_url
        return

    def _unavailable(msg: str) -> None:
        if os.environ.get("CI"):
            pytest.fail(f"Postgres backend unavailable in CI: {msg}")
        pytest.skip(msg)

    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError as exc:
        _unavailable(f"testcontainers not installed: {exc}")

    pg = None
    try:
        pg = PostgresContainer("postgres:16-alpine", driver="asyncpg")
        pg.start()
    except Exception as exc:  # Docker daemon down / image pull failed
        _unavailable(f"could not start Postgres container: {exc}")

    try:
        yield pg.get_connection_url()
    finally:
        pg.stop()
