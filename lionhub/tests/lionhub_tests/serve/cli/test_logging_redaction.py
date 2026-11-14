# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging

import pytest


class _UnreadableStore:
    """A store whose error text carries a password outside any URL."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        # Synthetic credential, passed as a bare parameter rather than inside a URL.
        raise RuntimeError("could not connect: host=db.example.test password=example-store-secret")

    async def __aexit__(self, *exc):
        return False


@pytest.mark.parametrize(
    ("read", "logged"),
    [
        (
            ("_read_history", "agent", "reviewer"),
            "definition history is unreadable for agent/reviewer",
        ),
        (
            ("_read_version_row", "agent", "reviewer", 2),
            "definition version is unreadable for agent/reviewer",
        ),
    ],
)
async def test_an_unreadable_definition_store_is_logged_by_error_class_only(
    monkeypatch, caplog, read, logged
):
    pytest.importorskip("fastapi", reason="studio extra not installed")
    import lionagi.state.db as db_mod
    from lionagi.studio.services import definitions

    monkeypatch.setattr(db_mod, "StateDB", _UnreadableStore)
    name, *args = read
    with caplog.at_level(logging.WARNING, logger="lionagi.studio"):
        with pytest.raises(definitions.HistoryUnavailableError) as raised:
            await getattr(definitions, name)(*args)

    assert f"{logged}: RuntimeError" in caplog.text
    assert "example-store-secret" not in caplog.text
    # the error carries the store's own text, masked, to whatever reports it
    assert str(raised.value) == "could not connect: host=db.example.test password=***"
