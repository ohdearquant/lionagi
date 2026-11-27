# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the async postgres availability check.

The adapter itself is in ``lionhub.integrations.postgres_adapter`` and is tested in
``lionhub/tests``.
"""

# check_async_postgres_available


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


def test_the_missing_extra_error_is_what_the_check_returns(monkeypatch):
    import lionagi.utils as utils_mod

    monkeypatch.setattr(utils_mod, "is_import_installed", lambda pkg: False)

    from lionagi.adapters._utils import check_async_postgres_available, postgres_extra_error

    returned = check_async_postgres_available()
    built = postgres_extra_error()
    assert type(returned) is type(built) is ImportError
    assert returned.args == built.args
