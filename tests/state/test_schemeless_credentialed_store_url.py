# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A store URL with credentials but no scheme is read as a path, and says so.

``LIONAGI_STATE_DB_URL=user:secret@db.internal/lionagi`` is a connection string
with the scheme left off. Nothing rejects it: it has no ``://``, so it resolves
as a filesystem path, and a SQLite database appears at a path built out of the
credential while the server it named is never contacted. The store opens, it is
empty, and everything downstream reports on it as if it were the store.

The warning does not change that resolution, because a file name may legally
contain the characters involved. It makes the silent case audible.
"""

from __future__ import annotations

import logging

import pytest

from lionagi.state import engine as engine_mod
from lionagi.state.engine import normalize_state_db_url

PASSWORD = "hunter2-correct-horse"
TARGET = "db.internal"
CREDENTIALED = f"dbuser:{PASSWORD}@{TARGET}/lionagi"


@pytest.fixture(autouse=True)
def _fresh_warning_state(monkeypatch):
    """The once-per-target memo is module state; each test starts empty."""
    monkeypatch.setattr(engine_mod, "_schemeless_credential_targets", set())


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_schemeless_connection_string_warns_and_names_the_target(caplog, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.WARNING, logger=engine_mod.__name__):
        normalize_state_db_url(CREDENTIALED)

    messages = _warnings(caplog)
    assert len(messages) == 1, messages
    assert TARGET in messages[0], (
        f"the warning does not say which target was meant, so it cannot be acted on: {messages[0]!r}"
    )
    assert "scheme" in messages[0]


def test_the_warning_does_not_carry_the_credential(caplog, tmp_path, monkeypatch):
    """A warning about a leaked secret must not be the thing that logs it."""
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.WARNING, logger=engine_mod.__name__):
        normalize_state_db_url(CREDENTIALED)

    messages = _warnings(caplog)
    assert messages, "no warning at all, so this asserts nothing about its content"
    assert PASSWORD not in messages[0]
    assert "dbuser" not in messages[0]


def test_resolution_is_unchanged(tmp_path, monkeypatch):
    """The warning is a warning. What the URL resolves to is what it was."""
    monkeypatch.chdir(tmp_path)
    resolved = normalize_state_db_url(CREDENTIALED)
    assert resolved.startswith("sqlite+aiosqlite:///")
    assert resolved.endswith(f"{TARGET}/lionagi")


def test_it_warns_once_per_target(caplog, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.WARNING, logger=engine_mod.__name__):
        normalize_state_db_url(CREDENTIALED)
        normalize_state_db_url(CREDENTIALED)
        normalize_state_db_url(f"other:{PASSWORD}@{TARGET}/lionagi")

    assert len(_warnings(caplog)) == 1


def test_a_second_target_is_its_own_warning(caplog, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.WARNING, logger=engine_mod.__name__):
        normalize_state_db_url(CREDENTIALED)
        normalize_state_db_url(f"dbuser:{PASSWORD}@other.internal/lionagi")

    assert len(_warnings(caplog)) == 2


@pytest.mark.parametrize(
    "value",
    [
        "state.db",
        "/var/lib/lionagi/state.db",
        "./nested/state.db",
        # An '@' in a file name is legal and odd, and refusing or warning on it
        # would be a breaking change against a valid path. The colon before the
        # '@' is what distinguishes a credential from a name.
        "user@host.db",
        "backup@2026-08-03.db",
        # A drive letter is a colon with no '@' anywhere after it.
        "C:/data/state.db",
        ":memory:",
    ],
)
def test_ordinary_paths_do_not_warn(value, caplog, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.WARNING, logger=engine_mod.__name__):
        normalize_state_db_url(value)

    assert _warnings(caplog) == []


@pytest.mark.parametrize(
    "value",
    [
        f"postgresql://dbuser:{PASSWORD}@{TARGET}/lionagi",
        f"postgres://dbuser:{PASSWORD}@{TARGET}/lionagi",
        f"postgresql+asyncpg://dbuser:{PASSWORD}@{TARGET}/lionagi",
    ],
)
def test_a_url_with_its_scheme_does_not_warn(value, caplog):
    """These are correct configurations. The warning is about the missing scheme,
    not about credentials existing."""
    with caplog.at_level(logging.WARNING, logger=engine_mod.__name__):
        normalize_state_db_url(value)

    assert _warnings(caplog) == []
