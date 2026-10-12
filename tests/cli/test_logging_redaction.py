# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging

import pytest

from lionagi.cli._logging import configure_cli_logging, log_error


def test_error_log_masks_database_url_password(caplog, capsys):
    # Synthetic credential: the CLI error channel must retain the diagnosis while masking it.
    planted = "example-log-planted-value"
    url = f"postgresql://dbuser:{planted}@db.example.test/lionagi"
    logger = logging.getLogger("lionagi.cli.error")
    logger.handlers.clear()
    logger.propagate = False
    configure_cli_logging(verbose=False)
    logger.addHandler(caplog.handler)

    with caplog.at_level(logging.ERROR, logger="lionagi.cli.error"):
        log_error(f"could not connect to {url}")

    captured = capsys.readouterr()
    assert planted not in caplog.text
    assert planted not in captured.out + captured.err
    assert "could not connect" in caplog.text
    assert "dbuser:***@db.example.test/lionagi" in captured.err


def test_error_log_masks_a_password_given_as_a_keyword(caplog, capsys):
    # Synthetic credential, as a keyword/value connection string writes it.
    planted = "example-log-planted-value"
    logger = logging.getLogger("lionagi.cli.error")
    logger.handlers.clear()
    logger.propagate = False
    configure_cli_logging(verbose=False)
    logger.addHandler(caplog.handler)

    with caplog.at_level(logging.ERROR, logger="lionagi.cli.error"):
        log_error(f"could not connect: host=db.example.test password={planted} dbname=lionagi")

    captured = capsys.readouterr()
    assert planted not in caplog.text
    assert planted not in captured.out + captured.err
    assert "host=db.example.test password=*** dbname=lionagi" in captured.err


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
