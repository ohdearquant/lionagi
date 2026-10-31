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


# a value nested at the depth limit, and one level below it


def _nest_at(field: str, depth: int, value) -> dict:
    """A chain of ``depth`` actions whose deepest one carries ``value`` as its own ``field``."""
    node: dict = {"prompt": "step", field: value}
    for _ in range(depth - 1):
        node = {"prompt": "step", field: node}
    return node


def _nest_label(field: str, depth: int) -> str:
    """The label the validator gives the action at ``depth`` in a ``_nest_at`` chain."""
    return _flag(field) + f".{field}" * (depth - 1)


# (case id, builder from field name to the nested value, type name when it is not an object)
NESTED_VALUE_CASES = [
    ("empty-object", lambda f: {}, None),
    ("empty-list", lambda f: [], "list"),
    ("zero", lambda f: 0, "int"),
    ("empty-string", lambda f: "", "str"),
    ("false", lambda f: False, "bool"),
    ("object", lambda f: {"prompt": "leaf", f: None}, None),
]

NESTED_VALUE_PARAMS = [pytest.param(c[1], c[2], id=c[0]) for c in NESTED_VALUE_CASES]


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("where", ["at-limit", "one-below"])
@pytest.mark.parametrize("build, type_name", NESTED_VALUE_PARAMS)
def test_nested_value_at_and_below_depth_limit(field, where, build, type_name):
    """The engine fires a chain action only when its value is truthy, so a nested ``{}``
    is accepted at the limit as it is below it; a nested value that is not an object is
    refused as one at either depth; a non-empty object is refused only at the limit.
    The service create, the service update and the command-line parser agree."""
    depth = _MAX_CHAIN_DEPTH if where == "at-limit" else _MAX_CHAIN_DEPTH - 1
    value = build(field)
    chain = _nest_at(field, depth, value)
    label = _nest_label(field, depth)

    if type_name is not None:
        expected = f"{label}.{field}: must be a JSON object, got {type_name}"
    elif where == "at-limit" and value:
        expected = (
            f"{label}: {field} nests chain actions past the depth limit of {_MAX_CHAIN_DEPTH}"
        )
    else:
        expected = None

    parsed, cli_err = _parse_chain_action(json.dumps(chain), _flag(field))
    create_db, create_err = _run_create(_create_data(**{field: chain}))
    update_db, update_err = _run_update({field: chain})

    if expected is None:
        assert cli_err is None and parsed == chain
        assert create_err is None and update_err is None
        create_db.create_schedule.assert_awaited_once()
        (written,), _kwargs = create_db.create_schedule.await_args
        assert written[field] == chain
        update_db.update_schedule.assert_awaited_once_with("sid-chain-1", **{field: chain})
    else:
        assert cli_err == expected
        assert str(create_err) == expected
        assert str(update_err) == expected
        create_db.create_schedule.assert_not_called()
        update_db.update_schedule.assert_not_called()


# the chain-fire merge overlays only the keys a chain action may carry


def _parent_schedule() -> dict:
    return {
        "id": "sid-parent",
        "name": "parent",
        "trigger_type": "cron",
        "action_kind": "agent",
        "action_model": "base-model",
        "action_prompt": "base prompt",
        "action_agent": "base-agent",
        "action_playbook": "base-playbook",
        "action_cwd": "/work/parent",
        "action_command": "git",
        "on_success": {"prompt": "old"},
        "on_fail": None,
    }


def test_merge_keeps_parent_action_cwd_when_stored_chain_carries_one():
    from lionagi.studio.scheduler.engine import _merge_chain_action

    merged = _merge_chain_action(
        _parent_schedule(), {"prompt": "next", "action_cwd": "/somewhere/else"}
    )
    assert merged["action_cwd"] == "/work/parent"
    assert merged["action_prompt"] == "next"


@pytest.mark.parametrize(
    "key, value",
    [
        ("action_cwd", "/somewhere/else"),
        ("action_command", "rm"),
        ("action_prompt", "smuggled"),
        ("id", "other-id"),
        ("name", "other-name"),
        ("trigger_type", "interval"),
        ("enabled", False),
    ],
)
def test_merge_ignores_stored_chain_keys_that_are_not_allowed(key, value):
    from lionagi.studio.scheduler.engine import _merge_chain_action

    parent = _parent_schedule()
    merged = _merge_chain_action(parent, {"model": "m2", key: value})
    assert merged.get(key) == parent.get(key)
    assert merged["action_model"] == "m2"


def test_merge_applies_every_allowed_key():
    from lionagi.cli._schedule_parsing import _CHAIN_ACTION_ALLOWED_KEYS
    from lionagi.studio.scheduler.engine import _merge_chain_action

    nested = {"prompt": "after", "on_success": None}
    chain_action = {
        "kind": "play",
        "action_kind": "flow",
        "model": "m2",
        "prompt": "next",
        "agent": "next-agent",
        "playbook": "next-playbook",
        "on_success": nested,
        "on_fail": {"prompt": "recover", "on_fail": None},
    }
    assert set(chain_action) == _CHAIN_ACTION_ALLOWED_KEYS

    parent = _parent_schedule()
    merged = _merge_chain_action(parent, chain_action)
    assert merged["action_kind"] == "play", "kind wins over action_kind"
    assert merged["action_model"] == "m2"
    assert merged["action_prompt"] == "next"
    assert merged["action_agent"] == "next-agent"
    assert merged["action_playbook"] == "next-playbook"
    assert merged["on_success"] is nested
    assert merged["on_fail"] == {"prompt": "recover", "on_fail": None}
    assert parent["on_success"] == {"prompt": "old"}, "the parent schedule is not modified"
