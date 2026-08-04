# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""What a process says about the busy_timeout its connections will use.

The value is a deployment property set once per config file, so a process
started from a config that never mentions it silently runs the built-in
default. These arms hold the announcement to naming the effective value AND
where it came from: a bare number cannot tell "deliberately set to the default"
from "nobody set anything", which is the whole distinction the line exists for.
"""

from __future__ import annotations

import logging

import pytest

from lionagi.state import engine as eng

ENV = "LIONAGI_SQLITE_BUSY_TIMEOUT_MS"


@pytest.fixture
def announce(monkeypatch, caplog):
    """Re-arm the once-per-process guard so each arm gets a fresh announcement."""
    monkeypatch.setattr(eng, "_busy_timeout_announced", False)

    def run() -> list[logging.LogRecord]:
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=eng._log.name):
            eng.announce_busy_timeout()
        return list(caplog.records)

    return run


class TestItNamesTheValueAndItsSource:
    def test_an_unset_variable_is_named_as_such_not_just_reported_as_a_number(
        self, monkeypatch, announce
    ):
        """The case the line exists for. A process that prints only "5000"
        leaves a reader unable to tell a chosen default from an unset one."""
        monkeypatch.delenv(ENV, raising=False)
        monkeypatch.setattr(eng, "SQLITE_BUSY_TIMEOUT_MS", 5000)

        records = announce()

        assert len(records) == 1
        msg = records[0].getMessage()
        assert "5000ms" in msg
        assert ENV in msg
        assert "not set" in msg
        assert records[0].levelno == logging.INFO

    def test_a_value_from_the_environment_says_where_it_came_from(self, monkeypatch, announce):
        monkeypatch.setenv(ENV, "30000")
        monkeypatch.setattr(eng, "SQLITE_BUSY_TIMEOUT_MS", 30000)

        records = announce()

        msg = records[0].getMessage()
        assert "30000ms" in msg
        assert f"from {ENV}" in msg
        assert records[0].levelno == logging.INFO

    def test_an_unusable_value_warns_and_names_what_was_asked_for(self, monkeypatch, announce):
        """Repeated here deliberately. The same condition is warned about when
        the variable is first read, which happens at import — possibly before
        the process configured logging, in which case that warning was emitted
        and never seen."""
        monkeypatch.setenv(ENV, "not-a-number")
        monkeypatch.setattr(eng, "SQLITE_BUSY_TIMEOUT_MS", 5000)

        records = announce()

        assert records[0].levelno == logging.WARNING
        msg = records[0].getMessage()
        assert "5000ms" in msg
        assert "not-a-number" in msg
        assert "not usable" in msg

    def test_zero_is_treated_as_unusable_rather_than_as_a_request(self, monkeypatch, announce):
        """busy_timeout=0 means never wait, which is never what a deployment
        that bothered to set the variable is asking for."""
        monkeypatch.setenv(ENV, "0")
        monkeypatch.setattr(eng, "SQLITE_BUSY_TIMEOUT_MS", 5000)

        records = announce()

        assert records[0].levelno == logging.WARNING
        assert "not usable" in records[0].getMessage()

    def test_an_in_process_override_is_not_reported_as_the_environments_value(
        self, monkeypatch, announce
    ):
        """The module attribute is writable and tests retune it. A provenance
        captured when the module was imported would keep crediting the
        environment for a value something else had replaced."""
        monkeypatch.setenv(ENV, "30000")
        monkeypatch.setattr(eng, "SQLITE_BUSY_TIMEOUT_MS", 250)

        records = announce()

        msg = records[0].getMessage()
        assert "250ms" in msg
        assert "in-process" in msg
        assert "30000ms" in msg
        assert f"from {ENV}" not in msg


class TestItSaysThisOncePerProcess:
    def test_a_second_call_adds_nothing(self, monkeypatch, caplog):
        """Every connection would otherwise repeat it, and a line repeated per
        connection is one a reader learns to skip."""
        monkeypatch.setattr(eng, "_busy_timeout_announced", False)
        monkeypatch.delenv(ENV, raising=False)
        monkeypatch.setattr(eng, "SQLITE_BUSY_TIMEOUT_MS", 5000)

        with caplog.at_level(logging.INFO, logger=eng._log.name):
            caplog.clear()
            eng.announce_busy_timeout()
            first = len(caplog.records)
            # The guard is deliberately NOT re-armed: this is the real second
            # call, and the assertion is on records captured across it rather
            # than on a container this test controls.
            eng.announce_busy_timeout()
            eng.announce_busy_timeout()
            total = len(caplog.records)

        assert first == 1, "the first call must actually say something"
        assert total == 1, f"two further calls added {total - first} lines"


class TestBothStoreEntryPointsAnnounce:
    """The engine and the Studio connection helper are two independent ways
    into one store file. A process that only used the second would never say
    which timeout it had."""

    def test_make_engine_announces_on_the_sqlite_path(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setattr(eng, "_busy_timeout_announced", False)
        monkeypatch.delenv(ENV, raising=False)

        caplog.clear()
        with caplog.at_level(logging.INFO, logger=eng._log.name):
            eng.make_engine(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")

        assert any("busy_timeout" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_the_studio_connection_helper_announces(self, monkeypatch, tmp_path, caplog):
        from lionagi.studio.services import _db

        monkeypatch.setattr(eng, "_busy_timeout_announced", False)
        monkeypatch.delenv(ENV, raising=False)

        caplog.clear()
        with caplog.at_level(logging.INFO, logger=eng._log.name):
            async with _db.open_db(str(tmp_path / "studio.db")):
                pass

        assert any("busy_timeout" in r.getMessage() for r in caplog.records)
