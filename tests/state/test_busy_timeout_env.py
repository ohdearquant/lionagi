# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The sqlite busy_timeout is a deployment property, resolved from the env.

The default is sized for the test suite (a test holding a write lock should
fail fast), so a production daemon has to be able to ask for more without the
suite inheriting it. The resolver refuses unusable values toward the default
rather than toward a crash or toward "never wait": a daemon that fails to
start over a malformed tuning knob, or one whose every momentary lock becomes
an error because 0 slipped through, are both worse than a slower lock wait.
"""

from __future__ import annotations

import logging

from lionagi.state.engine import _busy_timeout_from_env

_VAR = "LIONAGI_SQLITE_BUSY_TIMEOUT_MS"


def test_unset_returns_the_suite_default(monkeypatch):
    monkeypatch.delenv(_VAR, raising=False)
    assert _busy_timeout_from_env() == 5000


def test_a_deployment_value_is_used_verbatim(monkeypatch):
    monkeypatch.setenv(_VAR, "30000")
    assert _busy_timeout_from_env() == 30000


def test_garbage_falls_back_and_says_so(monkeypatch, caplog):
    monkeypatch.setenv(_VAR, "thirty seconds")
    with caplog.at_level(logging.WARNING, logger="lionagi.state.engine"):
        assert _busy_timeout_from_env() == 5000
    # The warning names the rejected value: an operator reading the log has to
    # be able to tell WHICH knob was ignored without reproducing the env.
    assert any("thirty seconds" in r.getMessage() for r in caplog.records)


def test_zero_and_negative_are_refused_like_garbage(monkeypatch, caplog):
    # busy_timeout=0 means "never wait", turning every momentary lock into an
    # error — never what a deployment that set the variable wanted.
    for bad in ("0", "-1"):
        caplog.clear()
        monkeypatch.setenv(_VAR, bad)
        with caplog.at_level(logging.WARNING, logger="lionagi.state.engine"):
            assert _busy_timeout_from_env() == 5000
        assert caplog.records, f"refusing {bad!r} must be logged, not silent"


def test_the_connection_pragma_reads_the_module_attribute_at_connect_time(monkeypatch):
    """Tests retune the module attribute; a frozen copy would ignore them.

    Asserted against a real connection because the coupling under test is
    between the attribute and the pragma actually applied, not between two
    Python names.
    """
    import asyncio

    import lionagi.state.engine as engine_mod
    from lionagi.state.engine import make_engine

    monkeypatch.setattr(engine_mod, "SQLITE_BUSY_TIMEOUT_MS", 1234)

    async def _applied_timeout() -> int:
        from sqlalchemy import text

        engine = make_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.connect() as conn:
                row = (await conn.execute(text("PRAGMA busy_timeout"))).first()
                return int(row[0])
        finally:
            await engine.dispose()

    assert asyncio.run(_applied_timeout()) == 1234
