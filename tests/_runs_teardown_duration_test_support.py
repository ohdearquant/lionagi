# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Setup shared by module and adapter contracts."""

from __future__ import annotations

import pytest_asyncio

from lionagi.state.db import StateDB


@pytest_asyncio.fixture
async def db(tmp_path):
    database = StateDB(tmp_path / "state.db")
    await database.open()
    try:
        yield database
    finally:
        await database.close()
