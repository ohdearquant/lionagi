# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The scheduler service validates ``on_success`` / ``on_fail`` chain actions at
create and at update with the same rules, and the same message text, as the
``li schedule`` command-line parser."""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")

from unittest.mock import AsyncMock, patch

from lionagi.cli._schedule_parsing import _MAX_CHAIN_DEPTH, _parse_chain_action
from lionagi.studio.services.schedules import create_schedule, update_schedule

FIELDS = ("on_success", "on_fail")


def _flag(field: str) -> str:
    return "--" + field.replace("_", "-")


def _chain(field: str, nodes: int) -> dict:
    """A chain of ``nodes`` actions, each nested in the previous one's ``field``;
    the innermost explicitly nulls its own ``field`` so nothing warns."""
    node: dict = {"prompt": "step", field: None}
    for _ in range(nodes - 1):
        node = {"prompt": "step", field: node}
    return node


# (case id, builder from field name to chain action, expected message fragment or None)
CASES = [
    (
        "unknown-key-depth-1",
        lambda f: {"prompt": "x", f: None, "trigger_type": "interval"},
        "unknown key",
    ),
    (
        "unknown-key-depth-2",
        lambda f: {"prompt": "x", f: {"prompt": "y", f: None, "trigger_type": "interval"}},
        "unknown key",
    ),
    ("non-object", lambda f: [1, 2, 3], "must be a JSON object"),
    ("valid-nested", lambda f: _chain(f, 2), None),
    ("valid-at-depth-limit", lambda f: _chain(f, _MAX_CHAIN_DEPTH), None),
    ("nested-past-depth-limit", lambda f: _chain(f, _MAX_CHAIN_DEPTH + 1), "depth limit"),
]

CASE_PARAMS = [pytest.param(c[1], c[2], id=c[0]) for c in CASES]


def _create_data(**overrides) -> dict:
    base = {
        "name": "chain-validation-test",
        "trigger_type": "cron",
        "cron_expr": "0 * * * *",
        "action_kind": "agent",
        "action_prompt": "ping",
    }
    base.update(overrides)
    return base


def _existing(**overrides) -> dict:
    base = {
        "id": "sid-chain-1",
        "name": "chain-validation-patch",
        "trigger_type": "cron",
        "cron_expr": "0 * * * *",
        "action_kind": "agent",
    }
    base.update(overrides)
    return base


def _run_create(data: dict):
    """Run create_schedule against a stubbed store; return (store, ValueError | None)."""

    async def _go():
        with patch("lionagi.studio.services.schedules.StateDB") as MockDB:
            mock_db = AsyncMock()
            mock_db.create_schedule = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)
            try:
                await create_schedule(data)
            except ValueError as exc:
                return mock_db, exc
            return mock_db, None

    return asyncio.run(_go())


def _run_update(fields: dict, existing: dict | None = None):
    """Run update_schedule against a stubbed store; return (store, ValueError | None)."""
    row = existing if existing is not None else _existing()

    async def _go():
        with patch("lionagi.studio.services.schedules.StateDB") as MockDB:
            mock_db = AsyncMock()
            mock_db.get_schedule = AsyncMock(return_value=row)
            mock_db.update_schedule = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)
            try:
                await update_schedule(row["id"], fields)
            except ValueError as exc:
                return mock_db, exc
            return mock_db, None

    return asyncio.run(_go())


# create_schedule


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("build, fragment", CASE_PARAMS)
def test_create_validates_chain_action(field, build, fragment):
    value = build(field)
    db, err = _run_create(_create_data(**{field: value}))

    if fragment is None:
        assert err is None
        db.create_schedule.assert_awaited_once()
        (written,), _kwargs = db.create_schedule.await_args
        assert written[field] == value
    else:
        assert err is not None
        assert fragment in str(err)
        db.create_schedule.assert_not_called()


@pytest.mark.parametrize("field", FIELDS)
def test_create_accepts_missing_and_null_chain_action(field):
    db, err = _run_create(_create_data())
    assert err is None
    db, err = _run_create(_create_data(**{field: None}))
    assert err is None
    (written,), _kwargs = db.create_schedule.await_args
    assert written[field] is None


# update_schedule


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("build, fragment", CASE_PARAMS)
def test_update_validates_chain_action(field, build, fragment):
    value = build(field)
    db, err = _run_update({field: value})

    if fragment is None:
        assert err is None
        db.update_schedule.assert_awaited_once_with("sid-chain-1", **{field: value})
    else:
        assert err is not None
        assert fragment in str(err)
        db.update_schedule.assert_not_called()


@pytest.mark.parametrize("field", FIELDS)
def test_update_without_chain_field_does_not_revalidate_stored_value(field):
    """A row stored before the check can still be edited elsewhere: the chain
    action is validated only when the update carries it."""
    stored = _existing(**{field: {"prompt": "old", "trigger_type": "interval"}})
    db, err = _run_update({"action_prompt": "new prompt"}, existing=stored)
    assert err is None
    db.update_schedule.assert_awaited_once_with("sid-chain-1", action_prompt="new prompt")


@pytest.mark.parametrize("field", FIELDS)
def test_update_can_clear_chain_action(field):
    stored = _existing(**{field: {"prompt": "old", field: None}})
    db, err = _run_update({field: None}, existing=stored)
    assert err is None
    db.update_schedule.assert_awaited_once_with("sid-chain-1", **{field: None})


# the command-line parser and the service agree


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("build, fragment", CASE_PARAMS)
def test_cli_parser_and_service_agree(field, build, fragment):
    value = build(field)
    parsed, cli_err = _parse_chain_action(json.dumps(value), _flag(field))

    create_db, create_err = _run_create(_create_data(**{field: value}))
    update_db, update_err = _run_update({field: value})

    if cli_err is None:
        assert parsed == value
        assert create_err is None and update_err is None
    else:
        assert fragment is not None and fragment in cli_err
        assert str(create_err) == cli_err
        assert str(update_err) == cli_err
        create_db.create_schedule.assert_not_called()
        update_db.update_schedule.assert_not_called()
