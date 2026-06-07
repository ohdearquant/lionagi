# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

__all__ = ("check_async_postgres_available",)


def check_async_postgres_available():
    from lionagi.utils import is_import_installed

    all_import_present = 0
    for pkg in ("sqlalchemy", "asyncpg"):
        if is_import_installed(pkg):
            all_import_present += 1
    if all_import_present == 2:
        return True
    return ImportError(
        "This adapter requires postgres option to be installed. "
        'Please install them using `uv pip install "lionagi[postgres]"`.'
    )
