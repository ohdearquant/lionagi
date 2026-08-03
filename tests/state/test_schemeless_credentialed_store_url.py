# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A store URL with credentials but no scheme is refused.

``LIONAGI_STATE_DB_URL=user:secret@db.internal/lionagi`` is a connection string
with the scheme left off. Nothing used to reject it: it has no ``://``, so it
resolved as a filesystem path, and a SQLite database appeared at a path built
out of the credential while the server it named was never contacted. The store
opened, it was empty, and everything downstream reported on it as if it were
the store.

There is no reading of that value under which resolving it is right, so it is
refused rather than logged. A value that really is a path of this shape is
spelled ``./`` first, which the pattern cannot match.
"""

from __future__ import annotations

import pytest

from lionagi.state.engine import normalize_state_db_url

PASSWORD = "hunter2-correct-horse"
TARGET = "db.internal"
CREDENTIALED = f"dbuser:{PASSWORD}@{TARGET}/lionagi"


def test_a_schemeless_connection_string_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        normalize_state_db_url(CREDENTIALED)

    assert TARGET in str(excinfo.value), (
        f"the error does not say which target was meant, so it cannot be acted on: {excinfo.value}"
    )
    assert "scheme" in str(excinfo.value)


def test_nothing_is_written_when_it_is_refused(tmp_path, monkeypatch):
    """The point of refusing is that the credential never reaches the disk."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        normalize_state_db_url(CREDENTIALED)

    on_disk = [p.name for p in tmp_path.iterdir()]
    assert on_disk == [], f"refusing still left something behind: {on_disk}"
    assert not any(PASSWORD in name for name in on_disk)


def test_the_error_does_not_carry_the_credential(tmp_path, monkeypatch):
    """An error about a leaked secret must not be the thing that logs it.

    Exception text reaches tracebacks, log aggregators and crash reporters, so
    it is subject to the same rule as any other output.
    """
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        normalize_state_db_url(CREDENTIALED)

    message = str(excinfo.value)
    assert PASSWORD not in message
    assert "dbuser" not in message


def test_the_error_names_the_escape_for_a_path_that_really_looks_like_this(tmp_path, monkeypatch):
    """A refusal that does not say how to proceed is a dead end.

    Tied to the behaviour rather than to the wording: whatever prefix the
    message names must be one that actually resolves.
    """
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        normalize_state_db_url(CREDENTIALED)

    assert "./" in str(excinfo.value)
    resolved = normalize_state_db_url(f"./{CREDENTIALED}")
    assert resolved.startswith("sqlite+aiosqlite:///")


@pytest.mark.parametrize(
    "value",
    [
        "state.db",
        "/var/lib/lionagi/state.db",
        "./nested/state.db",
        # An '@' in a file name is legal and odd, and refusing on it would be a
        # breaking change against a valid path. The colon before the '@' is
        # what distinguishes a credential from a name.
        "user@host.db",
        "backup@2026-08-03.db",
        # A drive letter is a colon with no '@' anywhere after it.
        "C:/data/state.db",
        ":memory:",
        # The escape from the error message, and an absolute spelling of the
        # same thing. Both are paths that would otherwise match the shape.
        f"./{CREDENTIALED}",
        f"/tmp/{CREDENTIALED}",
    ],
)
def test_ordinary_paths_still_resolve(value, tmp_path, monkeypatch):
    """Over-refusal is invisible to a suite made only of must-refuse checks."""
    monkeypatch.chdir(tmp_path)
    resolved = normalize_state_db_url(value)
    assert resolved.startswith("sqlite+aiosqlite:///")


@pytest.mark.parametrize(
    "value",
    [
        f"postgresql://dbuser:{PASSWORD}@{TARGET}/lionagi",
        f"postgres://dbuser:{PASSWORD}@{TARGET}/lionagi",
        f"postgresql+asyncpg://dbuser:{PASSWORD}@{TARGET}/lionagi",
    ],
)
def test_a_url_with_its_scheme_is_accepted(value):
    """These are correct configurations. The refusal is about the missing
    scheme, not about credentials existing."""
    resolved = normalize_state_db_url(value)
    assert resolved.startswith("postgresql+asyncpg://")


def test_a_path_object_is_never_subject_to_this(tmp_path):
    """A caller who passed a Path has already said what it is."""
    weird = tmp_path / CREDENTIALED.replace("/", "_")
    resolved = normalize_state_db_url(weird)
    assert resolved.startswith("sqlite+aiosqlite:///")
